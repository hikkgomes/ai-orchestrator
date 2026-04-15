# Finalize Phase Prompt (Deferred)

> Workflow phase: post-MERGING (DONE entry)
> CLI: `claude -p` (default); configurable via `routing.finalizer`
> Output artifact: transient — run summary written to `logs/run-<uuid>.log`;
>   optionally printed to terminal
> Schema: inline (no external schema file)
> State transitions: MERGING → DONE (finalize runs as part of the DONE transition)

> Deferred draft: not invoked by the v1 engine.

---

## Purpose

After the worktree branch has been successfully merged into the base branch,
produce a structured run summary for the operator. This summary:

1. States what was accomplished relative to the original task
2. Lists all files changed across all steps
3. Records any issues or caveats noted by the executor or reviewer
4. Provides next actions (test commands, follow-up tasks, documentation updates)

This phase is **optional and non-blocking**. It does not gate the DONE transition.
If this prompt fails or times out, the run is still marked DONE. The summary
is best-effort metadata.

---

## Variables

| Variable | Source | Description |
|---|---|---|
| `{task_description}` | run state | Normalized task description |
| `{plan_json}` | `plans/plan-<uuid>.json` | Final executed plan |
| `{step_results_json}` | `results/` | Array of all step result JSONs for this run |
| `{review_json}` | `reviews/review-<uuid>.json` | Final review JSON |
| `{adjudication_json}` | `adjudications/adj-<uuid>.json` | Final adjudication JSON (verdict = PASS) |
| `{git_diff_stat}` | worktree | Output of `git diff --stat <base_commit>...aio/run-<uuid>` |
| `{run_id}` | run state | UUID of this run |
| `{fix_iteration_count}` | run state | Total incremental fix cycles used |
| `{feasibility_replan_count}` | run state | Total feasibility replans used |
| `{finalize_schema}` | inline below | JSON Schema for the summary output |

---

## Escalation Policy

This phase has no escalation paths. It is best-effort.

- If the CLI invocation fails or times out: log the error and mark run DONE anyway.
- If the output does not parse: log a warning and use a minimal fallback summary.
- Do not retry more than once. Do not block the DONE transition.

---

## Scope Constraints

- This phase is read-only. The merge is already complete.
- Do not suggest changes that were not implemented.
- Do not re-review the implementation. That was the reviewer's job.
- Keep `next_actions` focused and actionable — 3–5 items maximum.
- Do not include AI caveats, disclaimers, or confidence statements.
- This is a single-pass invocation. No follow-up messages will be sent.

---

## Output Schema (inline)

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "RunSummary",
  "type": "object",
  "required": ["run_id", "task", "outcome", "files_changed", "next_actions", "notes"],
  "additionalProperties": false,
  "properties": {
    "run_id": {
      "type": "string",
      "description": "UUID of the completed run."
    },
    "task": {
      "type": "string",
      "description": "The task that was completed."
    },
    "outcome": {
      "type": "string",
      "minLength": 1,
      "description": "One-paragraph statement of what was accomplished."
    },
    "files_changed": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["path", "action"],
        "additionalProperties": false,
        "properties": {
          "path": { "type": "string" },
          "action": {
            "type": "string",
            "enum": ["created", "modified", "deleted"]
          }
        }
      },
      "description": "Deduplicated list of all files changed across all steps."
    },
    "next_actions": {
      "type": "array",
      "items": { "type": "string" },
      "maxItems": 5,
      "description": "Specific follow-up actions for the operator (tests to run, docs to update, etc.)."
    },
    "notes": {
      "type": "array",
      "items": { "type": "string" },
      "description": "Issues, caveats, or warnings from the implementation. Empty array if none."
    },
    "review_score": {
      "type": "integer",
      "minimum": 1,
      "maximum": 10,
      "description": "Quality score from the reviewer."
    },
    "loops_used": {
      "type": "object",
      "required": ["rework", "replan"],
      "additionalProperties": false,
      "properties": {
        "rework": { "type": "integer", "minimum": 0 },
        "replan": { "type": "integer", "minimum": 0 }
      }
    }
  }
}
```

---

## Template

```
You are a run summary agent for an automated software orchestrator.

A run has completed successfully. Produce a structured summary for the operator.
Do not discuss what could have been done differently. Do not add caveats.
Report what was done. This is a single-pass invocation.

TASK:
{task_description}

RUN ID: {run_id}

PLAN:
{plan_json}

STEP RESULTS:
{step_results_json}

REVIEW SUMMARY:
{review_json}

DIFF STAT:
{git_diff_stat}

LOOP STATS:
- Rework loops: {rework_count}
- Replan loops: {replan_count}

OUTPUT SCHEMA:
{finalize_schema}

SUMMARY RULES:
1. "run_id" must be exactly "{run_id}"
2. "task" must be exactly "{task_description}"
3. "outcome" is one paragraph: what was implemented and where it lives in the repo
4. "files_changed" is the deduplicated union of all files_changed across all step
   results, using the action from the last step that touched each file
5. "next_actions" lists 1–5 specific actions the operator should take
   (e.g., "Run pytest tests/test_auth.py to verify the new login flow")
6. "notes" includes any issues or partial completions from step results; empty if none
7. "review_score" is the score from the review JSON
8. "loops_used.rework" is {rework_count}, "loops_used.replan" is {replan_count}

Respond with ONLY valid JSON. No markdown fences. No commentary.
```

---

## Engine Behaviour After This Phase

- Output is parsed (strict JSON, then lenient fallback).
- On success: `RunSummary` JSON is written to `logs/run-<uuid>.log` (appended as a
  final entry). It is also printed to the terminal in a formatted rich table.
- On failure: engine logs a warning, writes a minimal fallback summary, and
  transitions to DONE. The run is not degraded by finalize failures.
- After finalize (success or failure): run state is set to DONE.
- `aio status` reads the RunSummary from the log for display.
