# Plan Phase Prompt

> Workflow phase: PLANNING
> CLI: `claude -p` (default); configurable via `routing.planner`
> Output artifact: `plans/plan-<run-prefix>-<hash>.md`
> State transitions: PLANNING -> APPROVAL_PLAN (or EXECUTING if approval is skipped)

---

## Purpose

Produce an implementation plan using agentic repository exploration. The planner
may inspect the codebase with `Read`, `Grep`, and `Glob`, then return a concise
markdown plan for execution.

---

## Variables

| Variable | Source | Description |
|---|---|---|
| `{task_description}` | run state + feedback | Validated task and optional planning feedback |
| `{scope_md}` | scoping artifact | Canonical scope markdown from SCOPING |

---

## Scope Constraints

- Explore the repository before writing the plan.
- Write only implementation guidance, no progress narrative.
- Prefer concrete references to files/functions discovered during exploration.
- Keep steps ordered and actionable.
- List key files as repository-relative paths.

---

## Template

```
You are a software planning agent. You have access to Read, Grep, and Glob
tools to explore the codebase.

TASK:
{task_description}

SCOPE:
{scope_md}

Explore the codebase to understand the relevant code, then write an
implementation plan. Structure your plan with these sections:

## Approach
Strategy, reasoning, risks, and validation approach.

## Steps
Ordered implementation actions. Be specific and reference files or functions
you found during exploration.

## Key Files
List the files that will need changes using a bullet list of repository-relative paths.

Write ONLY the plan. No preamble and no markdown code fences.
```
