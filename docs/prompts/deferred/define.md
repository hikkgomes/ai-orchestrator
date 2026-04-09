# Define Phase Prompt (Deferred)

> Workflow phase: pre-PLANNING (INIT gate)
> CLI: `claude -p`
> Output artifact: none (inline result consumed by engine before PLANNING begins)
> Schema: inline (no external schema file; output is a transient engine input)

> Deferred draft: not invoked by the v1 engine.

---

## Purpose

Validate and normalize the raw task string submitted via `aio run <task>` before
the PLANNING phase begins. This step:

1. Confirms the task is actionable and repo-scoped
2. Produces a normalized task description suitable for the planner
3. Surfaces unresolvable scope problems before any CLI invocation or worktree creation
4. Stops execution and requests human input if the task cannot be safely bounded

This phase is **read-only**. No code changes. No file writes except the transient
output consumed immediately by the engine.

---

## Variables

| Variable | Source | Description |
|---|---|---|
| `{raw_task}` | CLI argument | Raw string from `aio run "<task>"` |
| `{repo_summary}` | file system | First non-empty line of README, or `<no README>` |
| `{directory_tree}` | file system | Depth-2 tree of repo root, truncated to 2 000 chars |
| `{define_schema}` | inline below | JSON Schema for the expected output |

---

## Escalation Policy

**Stop and set state = PAUSED** (do not proceed to PLANNING) if:
- `actionable` is `false` in the output
- The orchestrator cannot parse a valid JSON response after `max_retries` attempts

**Do not stop for:**
- Normal ambiguity resolvable by reading the codebase
- Missing context that will be supplied in the PLANNING phase
- Plurals, typos, or imprecise language that can be safely normalized

---

## Scope Constraints

- This agent operates in one pass. No multi-turn dialogue.
- Do not ask clarifying questions in the output.
- Do not suggest implementation strategies.
- Do not produce prose outside the JSON structure.
- If unsure whether a task is actionable, default to `actionable: true` with an
  assumption recorded in `assumptions`. Lean toward enabling execution.

---

## Output Schema (inline)

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "TaskDefinition",
  "type": "object",
  "required": ["actionable", "normalized_task", "assumptions"],
  "additionalProperties": false,
  "properties": {
    "actionable": {
      "type": "boolean",
      "description": "True if the task is safe to pass to the planner."
    },
    "normalized_task": {
      "type": "string",
      "minLength": 1,
      "maxLength": 500,
      "description": "Cleaned, unambiguous version of the task. Required even when actionable is false — use the raw task verbatim in that case."
    },
    "assumptions": {
      "type": "array",
      "items": { "type": "string" },
      "description": "List of assumptions made to resolve ambiguity. Empty array if none needed."
    },
    "blocking_reason": {
      "type": "string",
      "description": "Why the task cannot proceed. Required only when actionable is false."
    }
  },
  "if": { "properties": { "actionable": { "const": false } } },
  "then": { "required": ["actionable", "normalized_task", "assumptions", "blocking_reason"] }
}
```

---

## Template

```
You are a task intake agent for an automated software orchestrator.

Your only job is to validate and normalize the task below. Do not implement
anything. Do not discuss implementation. Do not ask questions. Produce output
in exactly one pass.

RAW TASK:
{raw_task}

REPOSITORY SUMMARY:
{repo_summary}

REPOSITORY STRUCTURE (depth 2):
{directory_tree}

---

RULES:
1. If the task is actionable and scoped to this repository:
   - Set "actionable" to true
   - Set "normalized_task" to a clean, precise restatement of what must be done
   - List any assumptions you made to resolve ambiguity in "assumptions"
   - Omit "blocking_reason"

2. If the task cannot proceed (targets external systems, requires credentials
   you cannot scope, is too vague to plan even conservatively, or requests
   destructive actions on production systems):
   - Set "actionable" to false
   - Set "normalized_task" to the raw task verbatim
   - Set "blocking_reason" to a one-sentence explanation for the human operator
   - Set "assumptions" to []

3. When in doubt: default to actionable = true. Record your uncertainty in
   "assumptions". Do not block unless you are certain.

OUTPUT SCHEMA:
{define_schema}

Respond with ONLY valid JSON. No markdown fences. No commentary.
```

---

## Retry Prompt (on schema/parse failure)

```
Your previous response was not valid JSON or did not match the required schema.

Error: {validation_error}

Retry. Respond with ONLY valid JSON matching this schema:
{define_schema}

No markdown fences. No commentary.
```

---

## Engine Behaviour After This Phase

- If `actionable = true`: engine writes `normalized_task` into run state and transitions to PLANNING.
- If `actionable = false`: engine writes PAUSED state. `blocking_reason` is printed to the terminal. Human must update the task via `aio run` with a revised description.
- The `TaskDefinition` JSON is NOT persisted to disk as a named artifact. It is held in memory and used to populate `{task_description}` in the PLANNING prompt.
