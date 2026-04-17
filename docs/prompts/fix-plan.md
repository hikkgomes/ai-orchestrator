# Fix-Plan Phase Prompt

> Workflow phase: PLANNING (incremental fix loop)
> CLI: `claude -p --resume <planning-session>` when available, otherwise fresh `claude -p`
> Output artifact: `plans/plan-<uuid>.json`
> Schema: `schemas/plan.schema.json`
> State transitions: PLANNING(fix) → APPROVAL_PLAN (or EXECUTING if approval skipped)
> Triggered by: review debate outcome requiring fixes

---

## Purpose

Produce the smallest incremental plan needed to fix reviewed issues on top of
the existing implementation worktree. This is not a full replacement plan and
the worktree is not discarded.

---

## Variables

| Variable | Source | Description |
|---|---|---|
| `{task}` | run state | Original user task |
| `{scope_md}` | scoping artifact | Canonical scope markdown |
| `{original_plan}` | `plans/` | Plan that produced the current implementation |
| `{step_results}` | `results/` | Existing execution result artifacts |
| `{diff}` | git | Current implementation diff |
| `{issues}` | review/debate | Consolidated issues to fix |
| `{debate_context}` | review debate artifacts | Debate transcript |
| `{plan_schema}` | `schemas/plan.schema.json` | Full JSON Schema |

---

## Template

```
You are a software planning agent creating incremental fix steps.

The worktree already contains implementation changes. Do NOT produce a full
replacement plan. Produce only the smallest set of follow-up steps needed to
fix the issues below on top of the existing worktree.

TASK:
{task}

SCOPE.MD:
{scope_md}

ORIGINAL PLAN:
{original_plan}

EXISTING EXECUTION RESULTS:
{step_results}

CURRENT DIFF:
{diff}

ISSUES TO FIX:
{issues}

DEBATE CONTEXT:
{debate_context}

OUTPUT SCHEMA:
{plan_schema}

Respond with ONLY valid JSON. No markdown fences. No commentary.
```

---

## Constraints

- Plan only follow-up fixes; do not restate completed work.
- Keep file paths relative and confined to the repository.
- Implementation steps are natural-language strings and should cover only the
  incremental fixes.
- Existing execution result artifacts remain in run state; new fix execution
  results are appended through the legacy `step_results` state field.
