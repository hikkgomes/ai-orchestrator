# Adjudicate Phase Prompt

> Workflow phase: ADJUDICATING
> CLI: `codex exec` by default; debate rounds may resume `claude -p`
> Output artifacts: `adjudications/adj-<uuid>.json` and `adjudications/debate-round-*.json`
> Schemas: `schemas/adjudication.schema.json`, `schemas/debate_response.schema.json`
> State transitions:
>   ADJUDICATING(PASS) → MERGING
>   ADJUDICATING(fix needed) → PLANNING (incremental fix plan, worktree preserved)
>   ADJUDICATING(disagreement) → PAUSED (`debate_tiebreaker`)
>   ADJUDICATING(FAIL) → FAILED

---

## Purpose

Codex performs the first adjudication pass over Claude's review and the step
results. The engine compares Codex's position with Claude's review:

- Claude found issues and Codex agrees: produce incremental fix-planning feedback.
- Claude found no issues and Codex agrees: proceed to merge.
- They disagree: run the debate tree, resuming Claude's review session and
  escalating model/effort where configured.

The adjudication prompt itself still asks for a single JSON verdict. Debate
rebuttal prompts use `debate_response.schema.json`.

---

## Variables

| Variable | Source | Description |
|---|---|---|
| `{task_description}` | run state | Normalized task description |
| `{review_json}` | `reviews/review-<uuid>.json` | Full review JSON |
| `{step_results_json}` | `results/` | Array of all step result JSONs for this run |
| `{adjudication_schema}` | `schemas/adjudication.schema.json` | Full JSON Schema |

---

## Template

```
You are an adjudication agent. Decide whether this implementation should be merged,
reworked, replanned, or abandoned.

ORIGINAL TASK:
{task_description}

REVIEW:
{review_json}

STEP RESULTS:
{step_results_json}

Produce a JSON adjudication conforming to this schema:
{adjudication_schema}

Respond with ONLY valid JSON. No markdown fences. No commentary.
```

---

## Debate Behaviour

When the initial adjudication disagrees with the review, the engine creates
structured debate prompts:

- Claude rebuttal: review session is resumed with `--resume <session-id>`.
- Codex rebuttal: reasoning effort escalates to `debate.escalated_codex_effort`.
- Final Claude round: model and effort escalate to `debate.escalated_claude_*`.
- If disagreement remains, the run pauses at `debate_tiebreaker`.

Fix outcomes return to PLANNING with the original task, `scope.md`, the original
plan, all existing step results, current diff, consolidated issues, and the
debate transcript. The worktree is preserved.

---

## Retry Prompt

```
Your previous response was not valid. Error: {validation_error}

Fix the error and try again. The full original prompt follows.

---

{original_prompt}
```
