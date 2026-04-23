# Fix-Plan Phase Prompt

> Workflow phase: PLANNING (incremental fix loop)
> CLI: `claude -p --resume <session-id>` when available, otherwise fresh `claude -p`
> Output artifact: `plans/plan-<run-prefix>-<hash>.md`
> State transitions: PLANNING(fix) -> APPROVAL_PLAN (or EXECUTING if approval skipped)
> Triggered by: review debate outcome requiring fixes

---

## Purpose

Produce the smallest incremental markdown plan needed to fix reviewed issues on
top of the existing implementation worktree. This is not a full replacement
plan and the worktree is not discarded.

---

## Variables

| Variable | Source | Description |
|---|---|---|
| `{task}` | run state | Original user task |
| `{scope_md}` | scoping artifact | Canonical scope markdown |
| `{original_plan}` | `plans/` | Prior plan that produced the current implementation |
| `{step_results}` | `results/` | Existing execution result artifacts |
| `{diff}` | git | Current implementation diff |
| `{issues}` | review/debate | Consolidated issues to fix |
| `{debate_context}` | review debate artifacts | Debate transcript |

---

## Template

```text
You are a software planning agent creating an incremental fix plan.

Your only job is to produce a markdown plan. You do NOT edit files, run
commands, or modify the repository in any way. A separate execution phase
(Codex) will apply every step in your plan after a human approves it.

You have access to Read, Grep, and Glob to inspect the codebase. Edit,
Write, and Bash are deliberately unavailable - do not ask for them, do not
treat their absence as a blocker, and do not suggest "granting permissions."
If you think you need to write a file, describe that write as a step in
your plan instead.

You have access to Read, Grep, and Glob tools to inspect the current repository state.
The worktree already contains implementation changes. Do NOT produce a full replacement plan.
Produce only the smallest follow-up plan needed to fix the issues below on top of existing changes.

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

Write the output using these sections:

## Approach
## Steps
## Key Files

Write ONLY the plan. No preamble and no markdown code fences.
If you cannot complete a step yourself, that is expected - describe it in
## Steps for the executor to do. Never output a permission-request message
in place of a plan.
```

---

## Constraints

- Plan only follow-up fixes; do not restate completed work.
- Keep file paths repository-relative and confined to the repository.
- Keep `## Steps` focused on incremental changes only.
- `## Key Files` should include only files required for the fix.
- Existing execution result artifacts remain in run state; new fix execution
  results are appended through the legacy `step_results` state field.
