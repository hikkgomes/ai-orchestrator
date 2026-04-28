# Fix-Plan Phase Prompt

> Workflow phase: PLANNING (incremental fix loop)
> CLI: `claude -p --resume <session-id>` when available, otherwise fresh `claude -p`
> Output artifact: `plans/plan-<run-prefix>-<hash>.md`
> State transitions: PLANNING(fix) -> APPROVAL_PLAN or EXECUTING

## Purpose

Produce the smallest incremental markdown plan needed to fix reviewed issues on top of the existing implementation worktree. The prompt is issues-only because the resumed planning session already has the task, scope, prior plan, and review context.

## Variables

| Variable | Source | Description |
|---|---|---|
| `{issues}` | review/debate | Consolidated issues to fix |

## Template

```text
Alright, plan to fix the issues we found after reviewing the implementation:

{issues}
```
