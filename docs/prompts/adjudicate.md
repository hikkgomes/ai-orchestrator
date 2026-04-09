# Adjudicate Phase Prompt

> Workflow phase: ADJUDICATING
> CLI: `claude -p` (default); configurable via `routing.adjudicator`
> Output artifact: `adjudications/adj-<uuid>.json`
> Schema: `schemas/adjudication.schema.json`
> State transitions:
>   ADJUDICATING(PASS)   → APPROVAL_MERGE
>   ADJUDICATING(REWORK) → EXECUTING (rework loop)
>   ADJUDICATING(REPLAN) → PLANNING (replan loop)
>   ADJUDICATING(FAIL)   → FAILED

---

## Purpose

Make the definitive decision on whether the reviewed implementation should
proceed to merge, be reworked, be replanned, or be abandoned.

The adjudicator synthesizes the review findings, step results, and loop state
into a single actionable verdict. It is the final automated gate before human
approval for merge.

---

## Variables

| Variable | Source | Description |
|---|---|---|
| `{task_description}` | run state | Normalized task description |
| `{plan_json}` | `plans/plan-<uuid>.json` | Full plan JSON |
| `{review_json}` | `reviews/review-<uuid>.json` | Full review JSON |
| `{step_results_json}` | `results/` | Array of all step result JSONs for this run |
| `{rework_count}` | run state | Number of rework loops used so far |
| `{replan_count}` | run state | Number of replan loops used so far |
| `{max_rework_loops}` | config | Maximum rework loops allowed |
| `{max_replan_loops}` | config | Maximum replan loops allowed |
| `{adjudication_schema}` | `schemas/adjudication.schema.json` | Full JSON Schema |
| `{merge_rejection_feedback}` | approval gate (optional) | Human feedback if APPROVAL_MERGE was rejected |

---

## Escalation Policy

**Use highest reasoning effort** (`--reasoning-effort high`) always.

**Verdict selection rules (in priority order):**

1. `FAIL` — use when:
   - The review verdict is `reject` AND `rework_count >= max_rework_loops`
   - The review verdict is `reject` AND the failure is a security vulnerability
     that cannot be fixed by rework (e.g., hardcoded credentials, path traversal)
   - `replan_count >= max_replan_loops` AND the review still has critical findings
   - The task is fundamentally impossible given the repository state

2. `REPLAN` — use when:
   - The review identifies that the implementation is correct code but solves
     the wrong problem (plan was wrong, not implementation)
   - The plan structure itself caused the failure (wrong decomposition, wrong
     files targeted, wrong approach)
   - `rework_count >= max_rework_loops` AND the root cause is the plan
   - Do NOT use REPLAN to avoid a difficult rework — only use it when the plan
     is genuinely wrong

3. `REWORK` — use when:
   - The review has `request_changes` or `reject` verdict with specific fixable findings
   - The implementation is mostly correct but has specific correctable issues
   - `rework_count < max_rework_loops`
   - `rework_steps` must name exactly which steps need to be re-executed
   - `rework_feedback` must be specific enough for the executor to act on without
     further guidance

4. `PASS` — use when:
   - The review verdict is `approve`
   - OR the review has only `minor`/`info` findings and the task requirements
     are met (adjudicator may override `request_changes` for minor-only findings)

**IMPORTANT:** Do not loop when at the limit. If `rework_count >= max_rework_loops`
and the only valid verdict would be `REWORK`, escalate to `FAIL` instead.
If `replan_count >= max_replan_loops` and the only valid verdict would be `REPLAN`,
escalate to `FAIL` instead.

---

## Scope Constraints

- Base the verdict only on the review JSON, step results, and loop state provided.
- Do not invent findings not present in the review.
- `rework_feedback` must be actionable: specify which files, which logic, what to change.
  Do not write generic feedback like "fix the bugs".
- `replan_feedback` must explain what was wrong with the plan structure, not the code.
- `failure_reason` must be specific about why the run cannot continue.
- `adjudication_id` must be a valid UUID v4.
- This is a single-pass invocation. No follow-up messages will be sent.

---

## Template

```
You are an adjudication agent for an automated software orchestrator.

You must produce a single binding verdict on whether the implementation below
should be merged, reworked, replanned, or abandoned. Your verdict drives the
next state transition. Make it count.

Do not ask questions. Do not hedge. This is a single-pass invocation.

ORIGINAL TASK:
{task_description}

PLAN:
{plan_json}

REVIEW:
{review_json}

STEP RESULTS:
{step_results_json}

LOOP STATE:
- Rework loops used: {rework_count} of {max_rework_loops} allowed
- Replan loops used: {replan_count} of {max_replan_loops} allowed

{merge_rejection_section}

OUTPUT SCHEMA:
{adjudication_schema}

VERDICT SELECTION RULES (apply in order):
1. FAIL if:
   - The review has critical security findings (path traversal, credentials,
     injection) that cannot be fixed by rework
   - rework_count >= {max_rework_loops} AND review still has critical/major findings
   - replan_count >= {max_replan_loops} AND the plan was wrong again
   - The task is objectively impossible in this repository

2. REPLAN if:
   - The plan targeted wrong files, used wrong approach, or decomposed incorrectly
   - AND replan_count < {max_replan_loops}
   - Use "replan_feedback" to explain exactly what the planner must do differently

3. REWORK if:
   - The implementation has specific fixable issues identified in the review
   - AND rework_count < {max_rework_loops}
   - "rework_steps" must list exactly which step numbers need re-execution
   - "rework_feedback" must describe exactly what to change (specific files, logic,
     patterns to avoid) — generic feedback is not acceptable

4. PASS if:
   - Review verdict is "approve", OR
   - All findings are minor/info and the core task requirements are met

REQUIRED FIELD RULES:
- If verdict = "REWORK": include "rework_steps" (non-empty) and "rework_feedback"
- If verdict = "REPLAN": include "replan_feedback"
- If verdict = "FAIL": include "failure_reason"
- "adjudication_id" must be a valid UUID v4

Respond with ONLY valid JSON. No markdown fences. No commentary.
```

### Merge rejection section (injected only when `merge_rejection_feedback` is present)

```
MERGE REJECTION:
A human reviewer rejected the merge with this feedback:
{merge_rejection_feedback}

Incorporate this feedback into your verdict. If the feedback identifies specific
fixable issues, use REWORK. If it identifies a fundamental plan problem, use REPLAN.
If the issues are unresolvable, use FAIL.
```

---

## Retry Prompt (on schema/parse/validation failure)

```
Your previous response was not valid. Error: {validation_error}

Fix the error and try again. The full original prompt follows.

---

{original_prompt}
```

---

## Engine Behaviour After This Phase

- Validated adjudication is written to `adjudications/adj-<uuid>.json`.
- `adjudication_id` is stored in run state.
- Engine transitions based on `verdict`:
  - `PASS` → APPROVAL_MERGE (or MERGING if `require_merge_approval = false`)
  - `REWORK` → EXECUTING (`rework_steps` are re-executed in the same worktree;
    `rework_count` is incremented)
  - `REPLAN` → PLANNING (existing worktree is discarded; `replan_count` incremented;
    `replan_feedback` is passed to the planner prompt)
  - `FAIL` → FAILED (run ends; `failure_reason` is stored in run state and
    printed to the terminal)
