"""Prompt template builders for each workflow phase.

All templates follow the contracts in AGENTS.md exactly.  Secret scanning
must be applied to file contents before calling these functions — these
functions assume the content has already been screened.

Usage::

    prompt = build_planning_prompt(
        task_description="Add login form validation",
        scope_md="...",
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
    scope_md: str,
) -> str:
    """Build the planning phase prompt.

    Parameters
    ----------
    task_description:
        The user's original task string.
    scope_md:
        Canonical scope markdown generated during scoping.

    Returns
    -------
    str
        Fully rendered prompt for the planner CLI.
    """
    return (
        "Plan for the implementation of the task below:\n"
        f"{task_description}\n\n"
        "SCOPE:\n"
        f"{scope_md}\n\n"
    )


def build_prescope_claude_prompt(raw_task: str) -> str:
    """Build Claude round-1 prompt that creates canonical scope.md."""
    return _prescope_prompt(
        raw_task=raw_task,
        repo_summary="",
        directory_tree="",
        canonical=True,
    )


def build_prescope_codex_prompt(raw_task: str, repo_summary: str, directory_tree: str) -> str:
    """Build Codex round-2 prompt that creates Codex's independent scope file."""
    return _prescope_prompt(
        raw_task=raw_task,
        repo_summary=repo_summary,
        directory_tree=directory_tree,
        canonical=False,
    )


def build_scope_compare_codex_prompt(
    claude_scope_md: str,
) -> str:
    """Build Codex round-3 prompt to compare both scopes."""
    return (
        "Claude's canonical scope is ready. Review it against your independent scope.\n"
        "Do not edit the canonical scope.md. Return ONLY markdown for codex-scope.md.\n\n"
        "Start with YAML frontmatter containing exactly:\n"
        "agreement: true|false\n\n"
        "For it to be true, either your initial scope must fully align with Claude's scope or you must be convinced by Claude's reasoning that their scope is absolutely correct.\n"
        "If you have any disagreements, notes, concerns or pushbacks, write concise reasoning that identifies what must change.\n\n"
        "CLAUDE CANONICAL SCOPE.MD:\n"
        f"{claude_scope_md}\n\n"
        "Remember: You need to reach full agreement with Claude before proceeding to planning. If you still disagree, explain why clearly and concisely in the body. Make sure to address any concerns they raise and make a compelling case for yourself.\n\n"
    )


def build_scope_respond_claude_prompt(codex_scope_md: str) -> str:
    """Build Claude round-4 prompt to respond to Codex reasoning."""
    return (
        "Codex's scope and reasoning are ready. Review it against your independent scope.\n"
        "Return ONLY markdown.\n\n"
        "Start with YAML frontmatter containing these keys:\n"
        "normalized_task, complexity_tier, actionable, key_files, context,\n"
        "agreement: true|false\n"
        "Set agreement true if you accept Codex's objection and have updated the canonical scope.md\n"
        "Set agreement false if you have any pushbacks to Codex and include your reasoning in the body.\n\n"
        "This must be a well-founded decision, so if you don't have a good reasoning to continue disagreeing with Codex, you should just update the canonical scope.md to align with Codex's scope and set agreement to true.\n\n"
        "CODEX REASONING:\n"
        f"{codex_scope_md}\n"
        "Remember: You need to reach full agreement with Codex before proceeding to planning. If you still disagree, explain why clearly and concisely in the body. Make sure to address any concerns they raise and make a compelling case for yourself.\n\n"
    )


def build_scope_final_codex_prompt(claude_scope_md: str) -> str:
    """Build Codex round-5 prompt for final xhigh scope assessment."""
    return (
        "Claude disagrees with your review. Review its reasoning against your independent reviews.\n"
        "Do not edit the canonical scope.md. Return ONLY markdown for codex-scope.md.\n\n"
        "Start with YAML frontmatter containing exactly:\n"
        "agreement: true|false\n\n"
        "This must be a well-founded decision, so if you don't have a good reasoning to continue disagreeing with Claude, you should give in and set agreement to true.\n\n"
        "CLAUDE CANONICAL SCOPE.MD:\n"
        f"{claude_scope_md}\n\n"
        "Remember: You need to reach full agreement with Claude before we proceed to planning. If you still disagree, explain why clearly and concisely in the body. Make sure to address any concerns they raise and make a compelling case for yourself.\n\n"
    )


def build_scope_final_claude_prompt(codex_scope_md: str) -> str:
    """Build Claude round-6 prompt for final Opus/max scope decision."""
    return (
        "Codex still disagrees with your scope and you are going to make the final decision on the scope. Read their assessment, review its updated reasoning and return ONLY the final canonical scope.md.\n"
        "If you are convinced by any of Codex's points, update the canonical scope.md accordingly. If you still disagree, keep the canonical scope.md as is.\n\n"
        "Preserve YAML frontmatter with normalized_task, complexity_tier, actionable, key_files, and context.\n\n"
        "CODEX FINAL ASSESSMENT:\n"
        f"{codex_scope_md}\n"
    )


def build_full_execution_prompt(
    plan_text: str,
    result_file_path: str,
    schema_json: str,
) -> str:
    """Build a single-session execution prompt for the full plan."""
    return (
        f"{plan_text}\n\n"
        "FULLY IMPLEMENT THE PLAN ABOVE\n"
        "- Do not commit or push any changes yet. Leave them for reviewing.\n"
        "- Update the documentation accordingly if needed.\n"
        "- If no changes are needed, explain that in the result summary.\n\n"
        "After making changes, write your result JSON to:\n"
        f"{result_file_path}\n\n"
        "The JSON must conform to this schema:\n"
        f"{schema_json}\n\n"
        "If you cannot write the file, respond with ONLY the raw JSON. No markdown fences. No commentary.\n"
    )


def build_review_prompt(
    git_diff: str,
    step_results_json: str,
    schema_json: str,
    heuristic_findings: list[dict[str, Any]] | None = None,
    review_categories: dict[str, str] | list[tuple[str, str]] | None = None,
    reviewer_config: dict[str, Any] | None = None,
) -> str:
    """Build the review phase prompt."""
    heuristic_section = _review_heuristic_section(heuristic_findings)
    categories_section = _review_categories_section(review_categories)
    repo_context_section = _reviewer_context_section(reviewer_config)
    return (
        "Review the plan implementation.\n\n"
        "IMPLEMENTATION DIFF:\n"
        f"{git_diff}\n\n"
        "EXECUTION RESULTS:\n"
        f"{step_results_json}\n\n"
        f"{heuristic_section}"
        f"{categories_section}"
        f"{repo_context_section}"
        "Before writing the final JSON, invoke the AI review workflow using the /ai-review skill and consolidate its signal with your own review.\n"
        "If it does not exist or cannot run, continue with the provided diff and heuristic scan.\n\n"
        "Produce a JSON review conforming to this schema:\n"
        f"{schema_json}\n\n"
        "Codex is going to review your work afterwards.\n"
    )


def build_review_codex_prompt(
    task_description: str,
    git_diff: str,
    review_json: str,
    schema_json: str,
) -> str:
    """Build Codex's independent review prompt inside the REVIEWING phase."""
    return (
        "The following task was implemented in my codebase and reviewed by Claude. Perform an independent review of both the implementation and Claude's review.\n"
        "Additionally, invoke the AI review workflow using the /ai-review skill and consolidate its signal with your own review.\n"
        "TASK:\n"
        f"{task_description}\n\n"
        "IMPLEMENTATION DIFF:\n"
        f"{git_diff}\n\n"
        "CLAUDE REVIEW REPORT:\n"
        f"{review_json}\n\n"
        "Return a JSON with these required fields: review_id (uuid), verdict (approve|request_changes|reject), score (1-10), findings (array), summary (string), blocks_merge (boolean).\n"
        "Use verdict=approve and blocks_merge=false only if the implementation should proceed.\n"
        "If fixes are needed, include specific findings and set blocks_merge=true.\n\n"
        "OUTPUT SCHEMA:\n"
        f"{schema_json}\n\n"
        "Respond with ONLY valid JSON. No markdown fences. No commentary.\n"
        "Claude is going to review your work afterwards.\n"
    )


def build_review_final_claude_prompt(
    codex_review_json: str,
    schema_json: str,
) -> str:
    """Build Claude Opus/max final review-debate prompt."""
    return (
        "Codex disagrees with your review.\n\n"
        "Decide whether the implementation can pass or must be fixed.\n"
        "Return ONLY JSON matching the debate response schema. Use position=issues_confirmed when fixes are required, or position=issues_dismissed when the implementation can pass.\n\n"
        "CODEX REVIEW REPORT AND PUSHBACK:\n"
        f"{codex_review_json}\n\n"
        "OUTPUT SCHEMA:\n"
        f"{schema_json}\n\n"
    )


def build_analysis_prompt(task: str) -> str:
    """Build an independent analysis-mode prompt."""
    return (
        "Analyze this task independently. Identify the key considerations, risks, and viable approaches.\n"
        "Be concise, specific, and do not modify files.\n\n"
        f"TASK:\n{task}\n"
    )


def build_analysis_debate_prompt(other_analysis: str) -> str:
    """Build a debate-round prompt using existing session context."""
    return (
        "Review the other AI's latest analysis. State where you agree, disagree, and what you would add.\n"
        "Be concise and focus on decision-useful differences.\n\n"
        f"OTHER ANALYSIS:\n{other_analysis}\n"
    )


def build_analysis_synthesis_prompt() -> str:
    """Build the final analysis synthesis prompt."""
    return "Synthesize the debate into a final recommendation with key tradeoffs and next steps. Be concise.\n"


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


def build_fix_planning_prompt(
    issues: str,
) -> str:
    """Build a planning prompt for incremental fix plans."""
    return (
        "Alright, plan to fix the issues we found after reviewing the implementation:\n\n"
        f"{issues}\n\n"
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


def repo_summary(repo_root: Path) -> str:
    """Return the first non-empty README line, or a fallback marker."""
    for name in ("README.md", "README.rst", "README.txt", "README"):
        path = repo_root / name
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if stripped:
                return stripped
    return "<no README>"


def _review_heuristic_section(findings: list[dict[str, Any]] | None) -> str:
    if not findings:
        return ""
    lines = []
    for finding in findings:
        workspace = str(finding.get("workspace") or "").strip()
        prefix = f"[{workspace}]" if workspace else ""
        lines.append(
            f"{prefix}[{finding['rule_id']}] {finding['file']}:{finding['line']} :: {finding['snippet']}"
        )
    return (
        "HEURISTIC SCAN RESULTS:\n"
        "The following patterns were detected in changed files. Verify each finding\n"
        "against the actual code. Include confirmed issues in your findings array.\n"
        "Silently discard false positives.\n\n"
        + "\n".join(lines)
        + "\n\n"
    )


def _review_categories_section(categories: dict[str, str] | list[tuple[str, str]] | None) -> str:
    if not categories:
        return ""
    entries = categories.items() if isinstance(categories, dict) else categories
    lines = [
        f"{index}. {category} - {description}"
        for index, (category, description) in enumerate(entries, start=1)
    ]
    return (
        "AI FAILURE CATEGORIES:\n"
        "Review against these categories in order of priority:\n"
        + "\n".join(lines)
        + "\n\n"
    )


def _reviewer_context_section(config: dict[str, Any] | None) -> str:
    if not config:
        return ""

    project = config.get("project") or {}
    paths = config.get("paths") or {}
    risk = config.get("risk") or {}
    architecture = config.get("architecture") or {}
    naming = architecture.get("naming") or {}
    key_libraries = architecture.get("key_libraries") or {}

    lines: list[str] = []
    if project.get("stack"):
        lines.append(f"Stack: {', '.join(project['stack'])}")
    if paths.get("critical"):
        lines.append(f"Critical paths: {', '.join(paths['critical'])}")

    risk_parts = [
        f"{name}=[{', '.join(values)}]"
        for name, values in risk.items()
        if values
    ]
    if risk_parts:
        lines.append(f"Risk areas: {', '.join(risk_parts)}")

    if architecture.get("patterns"):
        lines.append(f"Architecture: {', '.join(architecture['patterns'])}")

    library_parts = [
        f"{category}=[{', '.join(values)}]"
        for category, values in key_libraries.items()
        if values
    ]
    if library_parts:
        lines.append(f"Key libraries: {', '.join(library_parts)}")

    naming_parts = [f"{key}={value}" for key, value in naming.items() if value]
    if naming_parts:
        lines.append(f"Naming: {', '.join(naming_parts)}")

    if architecture.get("project_description"):
        lines.append(f"Description: {architecture['project_description']}")

    if not lines:
        return ""

    return "REPOSITORY CONTEXT:\n" + "\n".join(lines) + "\n\n"


def json_block(data: Any) -> str:
    """Serialize a structure to stable, indented JSON for prompt inclusion."""
    return json.dumps(data, indent=2, sort_keys=True)


def _prescope_prompt(
    raw_task: str,
    repo_summary: str,
    directory_tree: str,
    *,
    canonical: bool,
) -> str:
    if canonical:
        output_rules = (
            "Return ONLY markdown for the canonical scope.md.\n\n"
            "The output MUST start with YAML frontmatter containing these keys:\n"
            "normalized_task, complexity_tier, actionable, key_files, context.\n"
            "complexity_tier must be one of simple, moderate, complex, architectural, extramax.\n"
            "actionable must be true or false. key_files must be a YAML list.\n\n"
            "After the frontmatter, include concise notes on assumptions, risks, and\n"
            "important boundaries for planning.\n\n"
            "Codex is also scoping this task and you are going to review each others output.\n"
            "You both will need to reach an agreement before we proceed."
        )
    else:
        output_rules = (
            "Return ONLY markdown for your own codex-scope.md. DO NOT write, edit or make any changes you are editing the canonical scope.md.\n\n"
            "Your markdown should include:\n"
            "- normalized task\n"
            "- actionable: true or false\n"
            "- complexity tier: simple, moderate, complex, architectural, or extramax\n"
            "- key files or areas likely involved\n"
            "- assumptions and risks\n\n"
            "Claude Code is also scoping this task and you are going to review each others output.\n"
            "You both will need to reach an agreement before we proceed."
        )
    return (
        f"I received the task below. Scope the request for implementation across this project\n"
        "DO NOT MAKE ANY CHANGES TO THE CODEBASE YET. DO NOT EVER PUSH OR COMMIT ANYTHING.\n\n"
        "TASK:\n"
        f"{raw_task}\n\n"
        f"{output_rules}"
        + (
            ""
            if not repo_summary and not directory_tree
            else (
                "REPOSITORY SUMMARY:\n"
                f"{repo_summary}\n\n"
                "REPOSITORY STRUCTURE:\n"
                f"{directory_tree}\n"
            )
        )
    )


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
