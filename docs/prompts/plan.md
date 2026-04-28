# Plan Phase Prompt

> Workflow phase: PLANNING
> CLI: `claude -p` by default; configurable via `routing.planner`
> Output artifact: `plans/plan-<run-prefix>-<hash>.md`
> State transitions: PLANNING -> APPROVAL_PLAN or EXECUTING

## Purpose

Produce a markdown implementation plan. The prompt is intentionally small; detailed planning behavior comes from `.claude/skills/orchestration-architect/SKILL.md` for Claude or the equivalent CLI-side role instructions.

## Variables

| Variable | Source | Description |
|---|---|---|
| `{task_description}` | run state + feedback | Normalized task plus operator/review feedback when present |
| `{scope_md}` | scoping artifact | Canonical scope markdown, passed once |

## Template

```text
Plan for the implementation of the task below:
{task_description}

SCOPE:
{scope_md}
```

Fix-planning uses `docs/prompts/fix-plan.md` instead of this template.
