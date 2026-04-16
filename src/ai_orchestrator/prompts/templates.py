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
    workspace_trees: dict[str, str] | None = None,
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
    workspace_section = _workspace_section(workspace_trees)
    return (
        "You are a software planning agent. Think like Claude Plan mode: produce a\n"
        "natural, implementation-ready plan without pretending you know every file\n"
        "that will change in advance. The plan must still be valid JSON conforming\n"
        "to the schema below.\n\n"
        "TASK:\n"
        f"{task_description}\n\n"
        "REPOSITORY STRUCTURE:\n"
        f"{directory_tree}{workspace_section}\n\n"
        "KEY FILE CONTENTS:\n"
        f"{key_file_contents}\n\n"
        "PLAN SHAPE:\n"
        "- task: concise restatement of the work\n"
        "- approach: strategy, reasoning, risks, and validation approach\n"
        "- implementation_steps: ordered plain-language actions, not rigid step objects\n"
        "- key_files: flat list of likely relevant repository-relative paths\n\n"
        "Do not include per-step file lists, dependency graphs, or complexity hints.\n\n"
        "OUTPUT SCHEMA:\n"
        f"{schema_json}\n\n"
        "Respond with ONLY valid JSON. No markdown fences. No commentary.\n"
    )


def build_scoping_prompt(
    raw_task: str,
    repo_summary: str,
    directory_tree: str,
    schema_json: str,
    workspace_trees: dict[str, str] | None = None,
) -> str:
    """Build the scoping phase prompt."""
    workspace_section = _workspace_section(workspace_trees)
    return (
        "You are a task intake agent for an automated software orchestrator.\n\n"
        "Your only job is to validate and normalize the task below. Do not implement\n"
        "anything. Do not discuss implementation. Do not ask questions. Produce output\n"
        "in exactly one pass.\n\n"
        "RAW TASK:\n"
        f"{raw_task}\n\n"
        "REPOSITORY SUMMARY:\n"
        f"{repo_summary}\n\n"
        "REPOSITORY STRUCTURE (depth 2):\n"
        f"{directory_tree}{workspace_section}\n\n"
        "---\n\n"
        "RULES:\n"
        "1. If the task is actionable and scoped to this repository:\n"
        '   - Set "actionable" to true\n'
        '   - Set "normalized_task" to a clean, precise restatement of what must be done\n'
        '   - List any assumptions you made to resolve ambiguity in "assumptions"\n'
        '   - Omit "blocking_reason"\n\n'
        "2. If the task cannot proceed (targets external systems, requires credentials\n"
        "   you cannot scope, is too vague to plan even conservatively, or requests\n"
        "   destructive actions on production systems):\n"
        '   - Set "actionable" to false\n'
        '   - Set "normalized_task" to the raw task verbatim\n'
        '   - Set "blocking_reason" to a one-sentence explanation for the human operator\n'
        '   - Set "assumptions" to []\n\n'
        "3. Assess complexity:\n"
        '   - "simple": single-file or config change, no architectural impact\n'
        '   - "moderate": multi-file change, clear scope\n'
        '   - "complex": cross-cutting, tricky dependencies, weak test coverage\n'
        '   - "architectural": system design change, new patterns, ambiguous requirements\n\n'
        "4. When in doubt: default to actionable = true. Record your uncertainty in\n"
        '   "assumptions". Do not block unless you are certain.\n\n'
        "OUTPUT SCHEMA:\n"
        f"{schema_json}\n\n"
        "REQUIRED FIELDS - every response must include all four:\n"
        '  - "actionable": true or false (never omit - use true if uncertain)\n'
        '  - "normalized_task": string\n'
        '  - "assumptions": array (use [] if none)\n'
        '  - "complexity_tier": one of "simple", "moderate", "complex", "architectural"\n\n'
        "Respond with ONLY valid JSON. No markdown fences. No commentary.\n"
    )


def build_prescope_claude_prompt(raw_task: str, repo_summary: str, directory_tree: str) -> str:
    """Build Claude round-0 pre-scope prompt."""
    return _prescope_prompt(
        actor="Claude",
        raw_task=raw_task,
        repo_summary=repo_summary,
        directory_tree=directory_tree,
    )


def build_prescope_codex_prompt(raw_task: str, repo_summary: str, directory_tree: str) -> str:
    """Build Codex round-0 pre-scope prompt."""
    return _prescope_prompt(
        actor="Codex",
        raw_task=raw_task,
        repo_summary=repo_summary,
        directory_tree=directory_tree,
    )


def build_scope_synthesis_prompt(
    claude_scope_md: str,
    codex_scope_md: str,
    raw_task: str,
) -> str:
    """Build Claude prompt to synthesize canonical scope.md."""
    return (
        "You are resolving task scope for an automated software orchestrator.\n\n"
        "Read Claude's and Codex's independent scope notes, then write the canonical\n"
        "scope.md content. Return ONLY markdown for scope.md.\n\n"
        "The output MUST start with YAML frontmatter containing these keys:\n"
        "normalized_task, complexity_tier, actionable, key_files, context.\n"
        "complexity_tier must be one of simple, moderate, complex, architectural.\n"
        "actionable must be true or false. key_files must be a YAML list.\n\n"
        "RAW TASK:\n"
        f"{raw_task}\n\n"
        "CLAUDE PRE-SCOPE:\n"
        f"{claude_scope_md}\n\n"
        "CODEX PRE-SCOPE:\n"
        f"{codex_scope_md}\n"
    )


def build_scope_review_codex_prompt(claude_scope_md: str, scope_md: str) -> str:
    """Build Codex prompt to review canonical scope.md."""
    return (
        "You are reviewing the canonical scope for an automated software workflow.\n\n"
        "Codex has the last word on whether the scope is safe to plan from, but must\n"
        "not edit scope.md. Return ONLY markdown for codex-scope.md.\n\n"
        "Start with YAML frontmatter containing:\n"
        "agreement: true|false\n"
        "If agreement is false, include concise blocking comments under a Comments\n"
        "heading. If agreement is true, briefly explain why the scope is acceptable.\n\n"
        "CLAUDE NOTES:\n"
        f"{claude_scope_md}\n\n"
        "CANONICAL SCOPE.MD:\n"
        f"{scope_md}\n"
    )


def build_scope_final_codex_prompt(claude_scope_md: str, scope_md: str, codex_scope_md: str) -> str:
    """Build Codex final xhigh scope assessment prompt."""
    return (
        "You are making the FINAL Codex scope assessment at escalated reasoning.\n\n"
        "This is the last Codex review before planning. Do not edit scope.md. Return\n"
        "ONLY markdown for codex-scope.md with YAML frontmatter containing:\n"
        "agreement: true|false\n\n"
        "If agreement is false, explain the remaining concern concisely. If agreement\n"
        "is true, explain why the final scope is safe to plan from.\n\n"
        "CLAUDE NOTES:\n"
        f"{claude_scope_md}\n\n"
        "PREVIOUS CODEX COMMENTS:\n"
        f"{codex_scope_md}\n\n"
        "CANONICAL SCOPE.MD:\n"
        f"{scope_md}\n"
    )


def build_scope_rebuttal_claude_prompt(scope_md: str, codex_scope_md: str) -> str:
    """Build Claude prompt to address Codex scope feedback."""
    return (
        "You are updating canonical scope.md after Codex review.\n\n"
        "Read the current scope and Codex comments. Return ONLY the updated markdown\n"
        "for scope.md. Preserve YAML frontmatter with normalized_task,\n"
        "complexity_tier, actionable, key_files, and context. If you disagree with\n"
        "Codex, encode the chosen scope clearly in scope.md and explain the rationale\n"
        "in the body.\n\n"
        "CURRENT SCOPE.MD:\n"
        f"{scope_md}\n\n"
        "CODEX COMMENTS:\n"
        f"{codex_scope_md}\n"
    )


def build_scope_final_claude_prompt(scope_md: str, codex_scope_md: str) -> str:
    """Build Claude final max-effort scope decision prompt."""
    return (
        "You are making the FINAL Claude scope decision at maximum effort.\n\n"
        "Read Codex's final assessment and return ONLY the final canonical scope.md.\n"
        "Preserve YAML frontmatter with normalized_task, complexity_tier, actionable,\n"
        "key_files, and context. If Codex still disagrees, decide whether to adjust\n"
        "the scope or proceed with your current scope, and make that decision clear\n"
        "in the body.\n\n"
        "CURRENT SCOPE.MD:\n"
        f"{scope_md}\n\n"
        "CODEX FINAL ASSESSMENT:\n"
        f"{codex_scope_md}\n"
    )


def build_full_execution_prompt_codex(
    plan_json: str,
    file_contents: str,
    result_file_path: str,
    schema_json: str,
    workspace_trees: dict[str, str] | None = None,
) -> str:
    """Build a single-session Codex execution prompt for the full plan."""
    workspace_section = _workspace_section(workspace_trees)
    return (
        "You are a software implementation agent. Execute the full plan in this\n"
        "single Codex session. Maintain context across all implementation steps and\n"
        "make the smallest correct set of changes.\n\n"
        "FULL PLAN JSON:\n"
        f"{plan_json}\n\n"
        "RELEVANT FILES:\n"
        f"{file_contents}\n\n"
        f"{workspace_section}"
        "IMPLEMENTATION RULES:\n"
        "- Implement the whole plan, not just the first listed item.\n"
        "- Commit after each logical chunk when running in a git worktree, using:\n"
        "  git add -A && git commit -m \"aio: <description>\"\n"
        "- Do not commit in workspace mode if multiple repos are present unless the\n"
        "  repo policy clearly allows it.\n"
        "- If no changes are needed, explain that in the result summary.\n\n"
        "After making changes, write your result JSON to:\n"
        f"{result_file_path}\n\n"
        "The JSON must conform to this schema:\n"
        f"{schema_json}\n\n"
        "If you cannot write the file, respond with ONLY the raw JSON. No markdown fences. No commentary.\n"
    )


def build_full_execution_prompt_claude(
    plan_json: str,
    file_contents: str,
    schema_json: str,
    workspace_trees: dict[str, str] | None = None,
) -> str:
    """Build a single-session Claude execution prompt for the full plan."""
    workspace_section = _workspace_section(workspace_trees)
    return (
        "You are a software implementation agent. Execute the full plan in one\n"
        "continuous pass and then return one JSON result.\n\n"
        "FULL PLAN JSON:\n"
        f"{plan_json}\n\n"
        "RELEVANT FILES:\n"
        f"{file_contents}\n\n"
        f"{workspace_section}"
        "OUTPUT SCHEMA:\n"
        f"{schema_json}\n\n"
        "Respond with ONLY valid JSON. No markdown fences. No commentary.\n"
    )


def build_feasibility_prompt_codex(
    task_description: str,
    plan_json: str,
    directory_tree: str,
    result_file_path: str,
    schema_json: str,
    workspace_trees: dict[str, str] | None = None,
) -> str:
    """Build the feasibility phase prompt for the Codex adapter."""
    workspace_section = _workspace_section(workspace_trees)
    return (
        "You are a feasibility checker for an automated software orchestrator.\n\n"
        "Your job is to verify that the following plan can be executed in the current\n"
        "repository environment. This is a READ-ONLY check. Do not modify any files.\n"
        "Do not install packages. Do not run mutating commands.\n\n"
        "TASK:\n"
        f"{task_description}\n\n"
        "PLAN:\n"
        f"{plan_json}\n\n"
        "REPOSITORY STRUCTURE:\n"
        f"{directory_tree}{workspace_section}\n\n"
        "CHECKS TO PERFORM:\n"
        '1. Review "key_files" from the plan when present. Missing key files are\n'
        "   warnings unless the plan clearly depends on an existing file.\n"
        "2. Check that the build/test environment is intact. Run read-only probes only\n"
        '   (e.g., `python -c "import <dep>"`, `which <tool>`, `git status`).\n'
        "3. Check for obvious blockers: broken imports, missing config files the plan\n"
        "   depends on, etc.\n"
        "4. Do NOT attempt to fix anything. Report only.\n\n"
        "After checking, write your result JSON to:\n"
        f"{result_file_path}\n\n"
        "The JSON must conform to this schema:\n"
        f"{schema_json}\n\n"
        "If you cannot write the file, respond with ONLY the raw JSON. No markdown fences. No commentary.\n"
        "Do NOT modify any source files. Do NOT commit anything.\n"
    )


def build_feasibility_prompt_claude(
    task_description: str,
    plan_json: str,
    directory_tree: str,
    schema_json: str,
    workspace_trees: dict[str, str] | None = None,
) -> str:
    """Build the feasibility phase prompt for the Claude adapter."""
    workspace_section = _workspace_section(workspace_trees)
    return (
        "You are a feasibility checker for an automated software orchestrator.\n\n"
        "Your job is to review the following plan and identify any conditions in the\n"
        "current repository that would prevent execution. This is a STATIC ANALYSIS\n"
        "only - you cannot run commands. Use the repository structure and plan contents\n"
        "to reason about feasibility.\n\n"
        "TASK:\n"
        f"{task_description}\n\n"
        "PLAN:\n"
        f"{plan_json}\n\n"
        "REPOSITORY STRUCTURE:\n"
        f"{directory_tree}{workspace_section}\n\n"
        "CHECKS TO PERFORM:\n"
        '1. Review "key_files" from the plan when present. Missing key files are\n'
        "   warnings unless the plan clearly depends on an existing file.\n"
        '2. Identify any "key_files" paths that are outside the repository root or\n'
        "   contain path traversal (automatic blocking issue).\n"
        "3. Flag any implementation steps where the description implies network access, credential use,\n"
        "   or interactive input - all of which cannot proceed.\n"
        "4. Note any ambiguous or contradictory implementation instructions.\n\n"
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
    heuristic_findings: list[dict[str, Any]] | None = None,
    review_categories: dict[str, str] | list[tuple[str, str]] | None = None,
    reviewer_config: dict[str, Any] | None = None,
) -> str:
    """Build the review phase prompt."""
    heuristic_section = _review_heuristic_section(heuristic_findings)
    categories_section = _review_categories_section(review_categories)
    repo_context_section = _reviewer_context_section(reviewer_config)
    return (
        "You are a code review agent. Review the following implementation.\n\n"
        "ORIGINAL TASK:\n"
        f"{task_description}\n\n"
        "PLAN:\n"
        f"{plan_json}\n\n"
        "IMPLEMENTATION DIFF:\n"
        f"{git_diff}\n\n"
        "EXECUTION RESULTS:\n"
        f"{step_results_json}\n\n"
        f"{heuristic_section}"
        f"{categories_section}"
        f"{repo_context_section}"
        "Before writing the final JSON, invoke the repository-local AI review workflow\n"
        "if it exists at `.ai-review/scripts/review_changed.sh`, and consolidate its\n"
        "signal with your own review. If it does not exist or cannot run, continue\n"
        "with the provided diff and heuristic scan.\n\n"
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
        "EXECUTION RESULTS:\n"
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


def build_debate_claude_rebuttal_prompt(
    review: str,
    adjudication: str,
    debate_history: str,
    task: str,
    diff: str,
    schema_json: str,
) -> str:
    """Build a Claude debate prompt."""
    return (
        "You are continuing the SAME review session for adjudication debate.\n\n"
        "Re-evaluate the disputed implementation issues. Return ONLY JSON matching\n"
        "the schema. Use position=issues_confirmed if the implementation still needs\n"
        "fixes, or position=issues_dismissed if the disputed issues should not block.\n\n"
        "TASK:\n"
        f"{task}\n\n"
        "REVIEW:\n"
        f"{review}\n\n"
        "CODEX ADJUDICATION OR REBUTTAL:\n"
        f"{adjudication}\n\n"
        "DEBATE HISTORY:\n"
        f"{debate_history}\n\n"
        "DIFF:\n"
        f"{diff}\n\n"
        "OUTPUT SCHEMA:\n"
        f"{schema_json}\n\n"
        "Respond with ONLY valid JSON. No markdown fences. No commentary.\n"
    )


def build_debate_codex_rebuttal_prompt(
    adjudication: str,
    claude_rebuttal: str,
    debate_history: str,
    task: str,
    step_results: str,
    schema_json: str,
) -> str:
    """Build a Codex debate rebuttal prompt."""
    return (
        "You are continuing an adjudication debate for an automated workflow.\n\n"
        "Respond to Claude's argument. Return ONLY JSON matching the schema. Use\n"
        "position=issues_confirmed if you still believe fixes are required, or\n"
        "position=issues_dismissed if Claude convinced you the implementation can pass.\n\n"
        "TASK:\n"
        f"{task}\n\n"
        "INITIAL CODEX ADJUDICATION:\n"
        f"{adjudication}\n\n"
        "CLAUDE REBUTTAL:\n"
        f"{claude_rebuttal}\n\n"
        "DEBATE HISTORY:\n"
        f"{debate_history}\n\n"
        "EXECUTION RESULTS:\n"
        f"{step_results}\n\n"
        "OUTPUT SCHEMA:\n"
        f"{schema_json}\n\n"
        "Respond with ONLY valid JSON. No markdown fences. No commentary.\n"
    )


def build_fix_planning_prompt(
    task: str,
    scope_md: str,
    original_plan: str,
    step_results: str,
    diff: str,
    issues: str,
    debate_context: str,
    schema_json: str,
) -> str:
    """Build a planning prompt for incremental fix steps only."""
    return (
        "You are a software planning agent creating an incremental natural fix plan.\n\n"
        "The worktree already contains implementation changes. Do NOT produce a full\n"
        "replacement plan. Produce only the smallest follow-up implementation_steps\n"
        "needed to fix the issues below on top of the existing worktree.\n\n"
        "TASK:\n"
        f"{task}\n\n"
        "SCOPE.MD:\n"
        f"{scope_md}\n\n"
        "ORIGINAL PLAN:\n"
        f"{original_plan}\n\n"
        "EXISTING EXECUTION RESULTS:\n"
        f"{step_results}\n\n"
        "CURRENT DIFF:\n"
        f"{diff}\n\n"
        "ISSUES TO FIX:\n"
        f"{issues}\n\n"
        "DEBATE CONTEXT:\n"
        f"{debate_context}\n\n"
        "OUTPUT SCHEMA:\n"
        f"{schema_json}\n\n"
        "Respond with ONLY valid JSON. No markdown fences. No commentary.\n"
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


def _workspace_section(workspace_trees: dict[str, str] | None) -> str:
    if not workspace_trees:
        return ""
    return "\n\nWorkspace repos:\n" + "\n".join(
        f"## {name}/\n{tree}" for name, tree in workspace_trees.items()
    )


def _prescope_prompt(actor: str, raw_task: str, repo_summary: str, directory_tree: str) -> str:
    return (
        f"You are {actor}, independently scoping a user request for an automated\n"
        "software orchestrator. Do not implement anything. Return ONLY markdown.\n\n"
        "Your markdown should include:\n"
        "- normalized task\n"
        "- actionable: true or false\n"
        "- complexity tier: simple, moderate, complex, or architectural\n"
        "- key files or areas likely involved\n"
        "- assumptions and risks\n\n"
        "RAW TASK:\n"
        f"{raw_task}\n\n"
        "REPOSITORY SUMMARY:\n"
        f"{repo_summary}\n\n"
        "REPOSITORY STRUCTURE:\n"
        f"{directory_tree}\n"
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
