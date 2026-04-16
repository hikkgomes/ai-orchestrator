# Plan Phase Prompt

> Workflow phase: PLANNING
> CLI: `claude -p` (default); configurable via `routing.planner`
> Output artifact: `plans/plan-<uuid>.json`
> Schema: `schemas/plan.schema.json`
> State transitions: PLANNING → APPROVAL_PLAN (or FEASIBILITY/EXECUTING if approval skipped)

---

## Purpose

Produce a natural implementation plan, similar to Claude Plan mode. The planner
describes the overall approach, ordered implementation steps as plain strings,
and one flat list of likely relevant files. It does not predict exact per-step
file lists, dependencies, or complexity hints.

---

## Variables

| Variable | Source | Description |
|---|---|---|
| `{task_description}` | run state + `scope.md` | Validated task and canonical scope |
| `{directory_tree}` | file system | Depth-3 tree of repo root, truncated to 50 000 chars |
| `{key_file_contents}` | file system | Full contents of README, config, entry points; each file prefixed with its path |
| `{plan_schema}` | `schemas/plan.schema.json` | Full JSON Schema for the plan artifact |
| `{planning_feedback}` | human, feasibility, or debate feedback (optional) | Feedback for iterative refinement |
| `{scope_md}` | scoping artifact | Canonical scope with YAML frontmatter |

---

## Scope Constraints

- Plan only what the task requires.
- Use `approach` for strategy, reasoning, risks, and validation notes.
- Use `implementation_steps` for ordered natural-language actions.
- Use `key_files` for a flat list of repository-relative paths likely relevant to execution.
- Do not include per-step file lists, dependency graphs, or complexity hints.
- File paths must be relative, must not start with `/`, and must not contain `..` segments.

---

## Template

```
You are a software planning agent. Think like Claude Plan mode: produce a
natural, implementation-ready plan without pretending you know every file
that will change in advance. The plan must still be valid JSON conforming
to the schema below.

TASK:
{task_description}

REPOSITORY STRUCTURE:
{directory_tree}

KEY FILE CONTENTS:
{key_file_contents}

PLAN SHAPE:
- task: concise restatement of the work
- approach: strategy, reasoning, risks, and validation approach
- implementation_steps: ordered plain-language actions, not rigid step objects
- key_files: flat list of likely relevant repository-relative paths

OUTPUT SCHEMA:
{plan_schema}

Respond with ONLY valid JSON. No markdown fences. No commentary.
```
