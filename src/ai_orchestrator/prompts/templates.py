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
from typing import Any


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


def build_scoping_initial_prompt(raw_task: str) -> str:
    """Build shared initial scoping prompt sent to all participants."""
    return raw_task.strip()


def build_scoping_cross_review_prompt(other_responses: dict[str, str]) -> str:
    """Build a generic N-AI cross-review prompt."""
    parts = []
    for ai_name, response in other_responses.items():
        parts.append(f"{ai_name} opinion:\n{response}\n")
    joined = "\n".join(parts)
    return (
        "Review the following peer AI responses:\n\n"
        f"{joined}\n"
        "Are you all in full agreement? Start with `agreement: true` or `agreement: false`.\n"
        "If you would change anything, provide the revised scope and reasoning.\n"
    )


def build_scoping_user_reply_prompt(user_input: str, previous_scope: str) -> str:
    """Build follow-up prompt after user feedback in scoping."""
    return (
        "User feedback on the proposed scope:\n\n"
        f"{user_input}\n\n"
        "Previous agreed scope:\n\n"
        f"{previous_scope}\n\n"
        "Update your scope proposal accordingly.\n"
    )


def build_planning_revision_prompt(user_feedback: str) -> str:
    return (
        "Revise the existing implementation plan based on user feedback.\n\n"
        f"USER FEEDBACK:\n{user_feedback}\n"
    )


def build_delivery_prompt(
    task_description: str,
    plan_text: str,
    execution_summary: str,
    review_summary: str,
) -> str:
    return (
        "Write a concise delivery handoff for the completed implementation.\n\n"
        f"TASK:\n{task_description}\n\n"
        f"PLAN:\n{plan_text}\n\n"
        f"EXECUTION SUMMARY:\n{execution_summary}\n\n"
        f"REVIEW SUMMARY:\n{review_summary}\n\n"
        "Include:\n"
        "- What was implemented\n"
        "- Watch-outs / risk areas\n"
        "- Suggested next steps\n"
    )


def build_full_execution_prompt(plan_text: str, result_file_path: str) -> str:
    """Build a single-session execution prompt for the full plan."""
    return (
        f"{plan_text}\n\n"
        "FULLY IMPLEMENT THE PLAN ABOVE\n"
        "- Do not commit or push any changes yet. Leave them for reviewing.\n"
        "- Update the documentation accordingly if needed.\n"
        "- If no changes are needed, explain that in the result summary.\n\n"
        "After making changes, write your result JSON to:\n"
        f"{result_file_path}\n\n"
        "Required JSON fields (no extra fields allowed):\n"
        '- status: "success", "partial", or "failed"\n'
        "- files_changed: array of {path (relative), action (created|modified|deleted), summary}\n"
        "- summary: string\n"
        "Optional: issues (array of strings), test_commands (array of strings).\n\n"
        "If you cannot write the file, respond with ONLY the raw JSON. No markdown fences. No commentary.\n"
    )


def build_review_prompt(
    step_results_json: str,
    heuristic_findings: list[dict[str, Any]] | None = None,
) -> str:
    """Build the review phase prompt."""
    heuristic_section = _review_heuristic_section(heuristic_findings)
    return (
        "Review the plan implementation.\n\n"
        "EXECUTION RESULTS:\n"
        f"{step_results_json}\n\n"
        f"{heuristic_section}"
        "Inspect the worktree directly with your tools and use /ai-review to consolidate findings.\n"
        "If /ai-review is unavailable, continue with your own review.\n\n"
        "If you cannot inspect the worktree, return verdict=request_changes with a finding explaining tool access failed. Do not invent findings from missing context.\n\n"
        "Return ONLY valid JSON with fields:\n"
        "- review_id (uuid)\n"
        "- verdict (approve|request_changes|reject)\n"
        "- score (1-10)\n"
        "- findings (array)\n"
        "- summary (string)\n"
        "- blocks_merge (boolean)\n\n"
        "Each finding needs: severity (critical|major|minor|info), description. Optional: file, line, suggestion. No extra fields.\n"
        "Use verdict=approve and blocks_merge=false only if the implementation should proceed.\n"
        "reject requires blocks_merge=true and at least one critical or major finding.\n"
        "No extra fields allowed.\n\n"
        "Respond with ONLY valid JSON. No markdown fences. No commentary.\n"
    )


def build_review_codex_prompt(task_description: str, review_json: str) -> str:
    """Build Codex's independent review prompt inside the REVIEWING phase."""
    return (
        f"Task: {task_description}\n\n"
        "Review this implementation independently and evaluate Claude's review.\n"
        "Inspect the code directly with your tools.\n\n"
        "If you cannot inspect the worktree, return verdict=request_changes with a finding explaining tool access failed. Do not invent findings from missing context.\n\n"
        "CLAUDE REVIEW REPORT:\n"
        f"{review_json}\n\n"
        "Return a JSON with these required fields: review_id (uuid), verdict (approve|request_changes|reject), score (1-10), findings (array), summary (string), blocks_merge (boolean).\n"
        "Each finding needs: severity (critical|major|minor|info), description. Optional: file, line, suggestion. No extra fields.\n"
        "Use verdict=approve and blocks_merge=false only if the implementation should proceed.\n"
        "reject requires blocks_merge=true and at least one critical or major finding.\n"
        "No extra fields allowed.\n"
        "Respond with ONLY valid JSON. No markdown fences. No commentary.\n"
    )


def build_review_final_claude_prompt(codex_review_json: str) -> str:
    """Build Claude Opus/max final review-debate prompt."""
    return (
        "Codex disagrees with your review.\n\n"
        "Decide whether the implementation can pass or must be fixed.\n"
        "Return ONLY JSON with fields:\n"
        "- position (issues_confirmed|issues_dismissed|issues_accepted)\n"
        "- reasoning (string)\n"
        "- issues (array)\n\n"
        "CODEX REVIEW REPORT AND PUSHBACK:\n"
        f"{codex_review_json}\n\n"
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
        "Start with a line exactly like: agreement: true or agreement: false.\n"
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


def json_block(data: Any) -> str:
    """Serialize a structure to stable, indented JSON for prompt inclusion."""
    return json.dumps(data, indent=2, sort_keys=True)


def _prescope_prompt(
    raw_task: str,
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
    )
