"""Prompt template builders for each workflow phase.

All templates follow the contracts in AGENTS.md exactly.  Secret scanning
must be applied to file contents before calling these functions — these
functions assume the content has already been screened.

Usage::

    prompt = build_planning_prompt(
        task_description="Add login form validation",
        directory_tree="src/\n  main.py\n",
        key_file_contents="# main.py\n...",
        schema_json=json.dumps(plan_schema),
    )
    result = claude_adapter.invoke(prompt, ...)
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_PROMPT_TREE_MAX_DEPTH = 3
_PROMPT_TREE_MAX_CHARS = 50_000
_PROMPT_FILES_MAX_CHARS = 100_000

_SECRET_PATTERNS = (
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(
        r"(?im)\b(?:api[_-]?key|secret|token|password)\b\s*[:=]\s*['\"]?[A-Za-z0-9_\-+/=]{12,}"
    ),
)


def build_planning_prompt(
    task_description: str,
    directory_tree: str,
    key_file_contents: str,
    schema_json: str,
) -> str:
    """Build the planning phase prompt.

    Parameters
    ----------
    task_description:
        The user's original task string.
    directory_tree:
        Repo directory tree (truncated at depth 3, max ~50K chars).
    key_file_contents:
        Contents of key files (README, config, entry points).
    schema_json:
        JSON-serialised ``plan.schema.json``.

    Returns
    -------
    str
        Fully rendered prompt for the planner CLI.
    """
    return (
        "You are a software planning agent. Given the following task and repository context,\n"
        "produce a JSON plan conforming to the schema below.\n\n"
        "TASK:\n"
        f"{task_description}\n\n"
        "REPOSITORY STRUCTURE:\n"
        f"{directory_tree}\n\n"
        "KEY FILE CONTENTS:\n"
        f"{key_file_contents}\n\n"
        "OUTPUT SCHEMA:\n"
        f"{schema_json}\n\n"
        "Respond with ONLY valid JSON. No markdown fences. No commentary.\n"
    )


def build_execution_prompt_codex(
    step_description: str,
    plan_context: str,
    file_contents: str,
    result_file_path: str,
    schema_json: str,
) -> str:
    """Build the execution phase prompt for the Codex adapter.

    The prompt instructs Codex to write its result to *result_file_path*.
    """
    return (
        "You are a software implementation agent. Implement the following step.\n\n"
        "STEP:\n"
        f"{step_description}\n\n"
        "CONTEXT (from plan):\n"
        f"{plan_context}\n\n"
        "RELEVANT FILES:\n"
        f"{file_contents}\n\n"
        "After making changes, write a JSON result file to the path:\n"
        f"{result_file_path}\n\n"
        "The JSON must conform to this schema:\n"
        f"{schema_json}\n\n"
        "Do not print the JSON to stdout. Write it to the file path above.\n"
    )


def build_execution_prompt_claude(
    step_description: str,
    plan_context: str,
    file_contents: str,
    schema_json: str,
) -> str:
    """Build the execution phase prompt for the Claude adapter."""
    return (
        "You are a software implementation agent. Implement the following step.\n\n"
        "STEP:\n"
        f"{step_description}\n\n"
        "CONTEXT (from plan):\n"
        f"{plan_context}\n\n"
        "RELEVANT FILES:\n"
        f"{file_contents}\n\n"
        "OUTPUT SCHEMA:\n"
        f"{schema_json}\n\n"
        "Respond with ONLY valid JSON. No markdown fences. No commentary.\n"
    )


def build_review_prompt(
    task_description: str,
    plan_json: str,
    git_diff: str,
    step_results_json: str,
    schema_json: str,
) -> str:
    """Build the review phase prompt."""
    return (
        "You are a code review agent. Review the following implementation.\n\n"
        "ORIGINAL TASK:\n"
        f"{task_description}\n\n"
        "PLAN:\n"
        f"{plan_json}\n\n"
        "IMPLEMENTATION DIFF:\n"
        f"{git_diff}\n\n"
        "STEP RESULTS:\n"
        f"{step_results_json}\n\n"
        "Produce a JSON review conforming to this schema:\n"
        f"{schema_json}\n\n"
        "Respond with ONLY valid JSON. No markdown fences. No commentary.\n"
    )


def build_adjudication_prompt(
    task_description: str,
    review_json: str,
    step_results_json: str,
    schema_json: str,
) -> str:
    """Build the adjudication phase prompt."""
    return (
        "You are an adjudication agent. Decide whether this implementation should be merged,\n"
        "reworked, replanned, or abandoned.\n\n"
        "ORIGINAL TASK:\n"
        f"{task_description}\n\n"
        "REVIEW:\n"
        f"{review_json}\n\n"
        "STEP RESULTS:\n"
        f"{step_results_json}\n\n"
        "Produce a JSON adjudication conforming to this schema:\n"
        f"{schema_json}\n\n"
        "Respond with ONLY valid JSON. No markdown fences. No commentary.\n"
    )


def build_retry_prompt(
    original_prompt: str,
    error_message: str,
) -> str:
    """Build a retry prompt when the previous response was invalid.

    Per AGENTS.md Retry Protocol, includes the validation error and the
    full original prompt because each retry runs in a fresh subprocess.
    """
    return (
        "Your previous response was not valid. Error: "
        f"{error_message}\n\n"
        "Fix the error and try again. The full original prompt follows.\n\n"
        "---\n\n"
        f"{original_prompt}"
    )


def render_directory_tree(
    repo_root: Path,
    *,
    max_depth: int = _PROMPT_TREE_MAX_DEPTH,
    max_chars: int = _PROMPT_TREE_MAX_CHARS,
) -> str:
    """Render a truncated directory tree rooted at *repo_root*."""
    root = repo_root.resolve()
    lines: list[str] = []

    def walk(path: Path, depth: int) -> None:
        if len("\n".join(lines)) >= max_chars:
            return
        if depth > max_depth:
            return
        names = sorted(
            child.name
            for child in path.iterdir()
            if child.name not in {".git", ".ai-orchestrator", "__pycache__", ".pytest_cache"}
        )
        for name in names:
            child = path / name
            lines.append(f"{'  ' * depth}{name}")
            if child.is_dir():
                walk(child, depth + 1)

    lines.append(root.name)
    walk(root, 1)
    rendered = "\n".join(lines)
    if len(rendered) > max_chars:
        return rendered[: max_chars - 16] + "\n... [truncated]"
    return rendered


def collect_file_context(
    base_dir: Path,
    file_paths: list[str],
    *,
    max_chars: int = _PROMPT_FILES_MAX_CHARS,
) -> tuple[str, list[str]]:
    """Read and concatenate prompt file context, excluding secret-bearing files."""
    included: list[str] = []
    skipped: list[str] = []
    chunks: list[str] = []
    total = 0

    for relative_path in file_paths:
        path = base_dir / relative_path
        if not path.exists() or not path.is_file():
            skipped.append(relative_path)
            continue

        content = path.read_text(encoding="utf-8", errors="replace")
        if _contains_secret(relative_path, content):
            skipped.append(relative_path)
            continue

        chunk = f"# {relative_path}\n{content.rstrip()}\n"
        if total + len(chunk) > max_chars:
            remaining = max_chars - total
            if remaining <= 0:
                break
            chunk = chunk[: max(0, remaining - 16)] + "\n... [truncated]\n"
        chunks.append(chunk)
        included.append(relative_path)
        total += len(chunk)
        if total >= max_chars:
            break

    if skipped:
        chunks.append("# excluded_files\n" + "\n".join(skipped) + "\n")

    return "\n".join(chunks).strip(), skipped


def default_planning_files(repo_root: Path) -> list[str]:
    """Return a small set of high-signal files for the planning prompt."""
    candidates = [
        "README.md",
        "aio.toml",
        "AGENTS.md",
        "workflows/default.yaml",
        "docs/architecture.md",
        "docs/workflow.md",
        "src/ai_orchestrator/cli.py",
        "src/ai_orchestrator/engine.py",
    ]
    return [candidate for candidate in candidates if (repo_root / candidate).exists()]


def json_block(data: Any) -> str:
    """Serialize a structure to stable, indented JSON for prompt inclusion."""
    return json.dumps(data, indent=2, sort_keys=True)


def _contains_secret(path: str, content: str) -> bool:
    if _is_sensitive_env_file(path):
        return True
    return any(pattern.search(content) for pattern in _SECRET_PATTERNS)


def redact_secret_text(text: str) -> str:
    """Redact inline secret-like content from unstructured prompt text."""
    if not text:
        return text

    redacted_lines: list[str] = []
    redact_current_file = False
    for line in text.splitlines():
        if line.startswith("diff --git "):
            current_path = _diff_path(line)
            redact_current_file = bool(current_path and _is_sensitive_env_file(current_path))
            redacted_lines.append(line)
            continue

        if redact_current_file and line[:1] in {"+", "-", " "}:
            redacted_lines.append(f"{line[:1]}[REDACTED SECRET-BEARING DIFF CONTENT]")
            continue

        redacted_line = line
        for pattern in _SECRET_PATTERNS:
            redacted_line = pattern.sub("[REDACTED SECRET]", redacted_line)
        redacted_lines.append(redacted_line)

    return "\n".join(redacted_lines)


def _is_sensitive_env_file(path: str) -> bool:
    return Path(path).name.startswith(".env")


def _diff_path(line: str) -> str | None:
    parts = line.split()
    if len(parts) >= 4 and parts[-1].startswith("b/"):
        return parts[-1][2:]
    return None
