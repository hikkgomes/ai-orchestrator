# Review Phase Prompt

> Workflow phase: REVIEWING
> CLI: Claude review, Codex cross-check, Claude final tiebreaker
> Output artifact: `reviews/review-<uuid>.json`
> Schema: `schemas/review.schema.json`
> State transitions: REVIEWING -> MERGING or PLANNING

## Purpose

Review the implementation diff and execution results. Claude review prompts are trimmed and rely on session continuity for task, plan, and scoping context instead of re-sending those artifacts every round. Codex review runs in a fresh session with explicit task context and review schema requirements.

## Flow

1. Claude review: `build_review_prompt(git_diff, step_results_json, schema_json, ...)`.
2. Codex review: `build_review_codex_prompt(task_description, git_diff, review_json, schema_json)` runs in a fresh Codex session.
3. Claude tiebreaker: `build_review_final_claude_prompt(codex_review_json, schema_json)` runs only when Claude and Codex disagree.

## Claude Review Template

```text
Review the plan implementation.

IMPLEMENTATION DIFF:
{git_diff}

EXECUTION RESULTS:
{step_results_json}

{heuristic_scan_section}
{review_categories_section}
{repository_context_section}
Before writing the final JSON, invoke the repository-local AI review workflow using the /ai-review skill and consolidate its signal with your own review.
If it does not exist or cannot run, continue with the provided diff and heuristic scan.

Produce a JSON review conforming to this schema:
{schema_json}

Codex is going to review your work afterwards.
```

## Codex Review Template

```text
The following task was implemented in my codebase and reviewed by Claude. Perform an independent review of both the implementation and Claude's review.
IMPLEMENTATION DIFF:
{git_diff}

CLAUDE REVIEW REPORT:
{review_json}

Return a review JSON. Use verdict=approve and blocks_merge=false only if the implementation should proceed.
If fixes are needed, include specific findings, the reasoning and set blocks_merge=true.

OUTPUT SCHEMA:
{schema_json}

Respond with ONLY valid JSON. No markdown fences. No commentary.
Claude is going to review your work afterwards.
```

## Final Claude Template

```text
Codex disagrees with your review.

Decide whether the implementation can pass or must be fixed.
Return ONLY JSON matching the debate response schema. Use position=issues_confirmed when fixes are required, or position=issues_dismissed when the implementation can pass.

CODEX REVIEW REPORT AND PUSHBACK:
{codex_review_json}

OUTPUT SCHEMA:
{schema_json}
```
