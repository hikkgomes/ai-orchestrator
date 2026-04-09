# Fix-Plan Phase Prompt

> Workflow phase: PLANNING (replan loop)
> CLI: `claude -p` (default); configurable via `routing.planner`
> Output artifact: `plans/plan-<uuid>.json` (new plan, supersedes prior plan)
> Schema: `schemas/plan.schema.json`
> State transitions: PLANNING(replan) → APPROVAL_PLAN (or EXECUTING if approval skipped)
> Triggered by: adjudication verdict = REPLAN

---

## Purpose

Produce a corrected plan that addresses the specific structural failure identified
in the prior adjudication. This is NOT a retry of the original plan prompt — it
is a deliberate redesign guided by explicit feedback.

The fix-plan prompt differs from the initial plan prompt in three ways:
1. It includes the prior plan for reference (to avoid repeating the same mistake)
2. It includes the adjudication's `replan_feedback` as a hard constraint
3. It requires the `reasoning` field to explicitly acknowledge what changed and why

---

## Variables

| Variable | Source | Description |
|---|---|---|
| `{task_description}` | run state | Normalized task description (unchanged from original run) |
| `{directory_tree}` | file system | Depth-3 tree of the repo root (fresh scan; worktree was discarded) |
| `{key_file_contents}` | file system | Full contents of key files (fresh read) |
| `{prior_plan_json}` | `plans/plan-<uuid>.json` | The plan that was rejected (for reference — do not repeat its structure) |
| `{replan_feedback}` | adjudication | `replan_feedback` field from the adjudication that triggered this replan |
| `{review_summary}` | `reviews/review-<uuid>.json` | `summary` field from the review that led to the REPLAN verdict |
| `{replan_count}` | run state | Current replan loop number (1-indexed; max is `max_replan_loops`) |
| `{max_replan_loops}` | config | Maximum replan loops allowed |
| `{plan_schema}` | `schemas/plan.schema.json` | Full JSON Schema for the plan artifact |

---

## Escalation Policy

**Always use highest reasoning effort** (`--reasoning-effort high`). A replan means
the prior plan was architecturally wrong. This requires more careful analysis.

**Stop and set state = FAILED** (do not retry planning) when:
- `replan_count > max_replan_loops` — this should have been caught by the adjudicator,
  but if reached here, abort immediately
- The new plan is structurally identical to the prior plan (same files, same step
  structure) — application-level validation must detect and reject this

**Retry** (up to `max_retries`) when:
- Output is not valid JSON
- Output fails schema or application-level validation
- The new plan is identical to the prior plan (treat as validation failure)

---

## Scope Constraints

- The new plan must differ meaningfully from the prior plan. Specifically:
  - If the prior plan targeted wrong files: the new plan must target different files
  - If the prior plan used a wrong decomposition: the new plan must use a different one
  - If the prior plan had the wrong approach: the new plan must use a different approach
- Do not add steps that are not required by the task.
- The `reasoning` field must explicitly state:
  1. What was wrong with the prior plan (referencing `replan_feedback`)
  2. How the new plan addresses it
- All path and schema constraints from the original PLANNING phase apply.
- File paths must be relative, must not start with `/`, and must not contain `..`.
- `plan_id` must be a fresh UUID v4 (distinct from the prior plan's `plan_id`).

---

## Template

```
You are a software planning agent for an automated orchestrator.

A previous plan was executed, reviewed, and rejected because the plan itself
was structurally wrong. You must produce a corrected plan.

This is a single-pass invocation. Do not ask questions. Do not produce prose
outside the JSON. Do not repeat the rejected plan.

TASK:
{task_description}

REPOSITORY STRUCTURE:
{directory_tree}

KEY FILE CONTENTS:
{key_file_contents}

REJECTED PLAN (do not repeat this structure):
{prior_plan_json}

WHY THE PLAN WAS REJECTED:
{replan_feedback}

REVIEW SUMMARY (additional context):
{review_summary}

REPLAN ATTEMPT: {replan_count} of {max_replan_loops}

OUTPUT SCHEMA:
{plan_schema}

RULES:
1. Produce a plan that is meaningfully different from the rejected plan.
   If the rejected plan targeted wrong files, target different ones.
   If it used the wrong approach, use a different one.
   Do not reorder or rename steps from the rejected plan — start fresh.

2. The "reasoning" field MUST:
   a. Identify what was structurally wrong with the prior plan
   b. Explain specifically how this new plan addresses the problem
   c. Be at least two sentences

3. All other plan rules apply:
   - step_number must start at 1, be sequential with no gaps
   - files_to_modify must be non-empty per step
   - paths must be relative, no leading /, no .. segments
   - depends_on must reference only step numbers < current step
   - plan_id must be a fresh UUID v4

Respond with ONLY valid JSON. No markdown fences. No commentary.
```

---

## Retry Prompt (on schema/parse/validation failure)

```
Your previous response was not valid JSON, did not match the required schema,
failed application-level validation, or was too similar to the rejected plan.

Error: {validation_error}

Retry. Produce a new plan that is meaningfully different from the rejected plan.

REJECTED PLAN (do not repeat):
{prior_plan_json}

REJECTION REASON:
{replan_feedback}

OUTPUT SCHEMA:
{plan_schema}

Respond with ONLY valid JSON. No markdown fences. No commentary.
```

---

## Application-Level Validation (fix-plan specific)

In addition to standard plan validation, the orchestrator checks that the new plan
is sufficiently different from the prior plan:

- If the new plan has the same set of `files_to_modify` across all steps as the
  prior plan, it is rejected with error: "New plan targets the same files as the
  rejected plan. The plan must use a different approach."
- If the new plan's `reasoning` field does not mention the rejection reason
  (at minimum 10 chars of overlap with `replan_feedback`), it is rejected with
  error: "Plan reasoning must acknowledge the prior rejection."

These checks are in addition to — not instead of — standard path, numbering, and
dependency checks.

---

## Engine Behaviour After This Phase

- Validated plan is written to `plans/plan-<uuid>.json` (new UUID, new file).
- Prior plan artifact is preserved for debugging.
- Prior worktree branch has already been removed (`git worktree remove --force`)
  before this prompt is invoked.
- If `approval.require_plan_approval = true`: engine transitions to APPROVAL_PLAN (PAUSED).
  The approval UI shows both the new plan and the rejection context.
- If `approval.require_plan_approval = false`: engine creates a new worktree and
  transitions to EXECUTING with the new plan.
