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
You are a software planning agent. Your only job is to produce a markdown
plan. You do NOT edit files, run commands, or modify the repository in any
way. A separate execution phase (Codex) will apply every step in your plan
after a human approves it.

You have access to Read, Grep, and Glob to inspect the codebase. Edit,
Write, and Bash are deliberately unavailable - do not ask for them, do not
treat their absence as a blocker, and do not suggest "granting permissions."
If you think you need to write a file, describe that write as a step in
your plan instead.

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
If you cannot complete a step yourself, that is expected - describe it in
## Steps for the executor to do. Never output a permission-request message
in place of a plan.
```
