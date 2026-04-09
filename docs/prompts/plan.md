# Plan Phase Prompt

> Workflow phase: PLANNING
> CLI: `claude -p` (default); configurable via `routing.planner`
> Output artifact: `plans/plan-<uuid>.json`
> Schema: `schemas/plan.schema.json`
> State transitions: PLANNING → APPROVAL_PLAN (or EXECUTING if approval skipped)

---

## Purpose

Decompose the normalized task into an ordered list of implementation steps.
Each step must be atomic, scoped to specific files, and executable without
human input. The plan is the single source of truth for what the executor does.

---

## Variables

| Variable | Source | Description |
|---|---|---|
| `{task_description}` | run state (`normalized_task`) | Validated task from the define phase |
| `{directory_tree}` | file system | Depth-3 tree of repo root, truncated to 50 000 chars |
| `{key_file_contents}` | file system | Full contents of README, config, entry points; each file prefixed with its path |
| `{plan_schema}` | `schemas/plan.schema.json` | Full JSON Schema for the plan artifact |
| `{replan_feedback}` | prior adjudication (optional) | Populated only on replan loops; empty string on first attempt |
| `{rework_count}` | run state | Number of prior rework loops (0 on first attempt) |
| `{replan_count}` | run state | Number of prior replan loops (0 on first attempt) |

---

## Escalation Policy

**Increase reasoning effort** (set `--reasoning-effort high` if not already default) when:
- `replan_count >= 1` (this is a replan loop; prior plan was rejected)
- The repository has more than 50 files or multiple packages

**Stop and set state = FAILED** (do not retry planning) when:
- `replan_count >= max_replan_loops` — loop limit exceeded
- The same `replan_feedback` appears twice with no change in the plan

**Stop and set state = PAUSED** (request human approval) when:
- `approval.require_plan_approval = true` (always, after a valid plan is produced)

**Retry** (up to `max_retries`) when:
- Output is not valid JSON
- Output fails schema validation
- Output fails application-level validation (path traversal, circular deps, numbering)

---

## Scope Constraints

- Plan only what the task requires. Do not add steps for "nice to have" improvements.
- Each step must target specific files (`files_to_modify` must be non-empty).
- Steps must be sequential; `depends_on` is recorded but v1 executes all steps in order.
- Do not include steps that require interactive user input during execution.
- Do not include steps that access external network resources.
- Do not include steps that modify files outside the repository root.
- File paths must be relative, must not start with `/`, and must not contain `..` segments.
- Paths are validated by the orchestrator after output; invalid paths cause a retry.

---

## Template

```
You are a software planning agent for an automated orchestrator. Your output
will be executed without human review of individual steps. Plan conservatively.

This is a single-pass invocation. You will receive no follow-up messages.
Do not ask questions. Do not explain your reasoning in prose outside the JSON.
Do not produce markdown. Do not produce code.

TASK:
{task_description}

REPOSITORY STRUCTURE:
{directory_tree}

KEY FILE CONTENTS:
{key_file_contents}

{replan_section}

OUTPUT SCHEMA:
{plan_schema}

RULES:
1. Decompose the task into the minimum number of steps needed. Prefer fewer,
   larger steps over many small ones unless a step genuinely requires isolation.
2. Every step must have at least one entry in "files_to_modify".
3. Step numbers must start at 1 and be sequential with no gaps.
4. "depends_on" must only reference step numbers less than the current step.
   Empty array means the step has no prerequisites.
5. "estimated_complexity" must be one of: "low", "medium", "high".
   Use "high" for steps touching core logic, auth, or schema changes.
6. "reasoning" must explain the decomposition strategy in one paragraph.
7. "plan_id" must be a valid UUID v4.
8. File paths in "files_to_read" and "files_to_modify" must be relative,
   must not start with "/" or contain ".." anywhere.

Respond with ONLY valid JSON. No markdown fences. No commentary.
```

### Replan section (injected only when `replan_count >= 1`)

```
PRIOR PLAN REJECTION:
This is replan attempt {replan_count} of {max_replan_loops}.
The previous plan was rejected for the following reason:

{replan_feedback}

You must produce a new plan that addresses this feedback. Do not repeat the
same structure or approach that was rejected.
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

- Validated plan is written to `plans/plan-<uuid>.json`.
- If `approval.require_plan_approval = true`: engine transitions to APPROVAL_PLAN (PAUSED).
- If `approval.require_plan_approval = false`: engine transitions directly to EXECUTING.
- On rejection via `aio reject <run-id> plan`: engine re-enters PLANNING with `replan_feedback` set to the rejection reason and `replan_count` incremented.
