# Implement Phase Prompt

> Workflow phase: EXECUTING
> CLI: `codex exec --json` by default; configurable via `routing.worker`
> Output artifact: `results/execution-<uuid>.json`
> Schema: `schemas/execution_result.schema.json`
> State transitions: EXECUTING -> REVIEWING

## Purpose

Execute the full approved plan in one worker session and write one execution result. The prompt no longer embeds `file_contents` or `workspace_trees`; workers inspect the repository directly. Commits are deferred to the engine merge/commit handling instead of being requested in the prompt.

## Variables

| Variable | Source | Description |
|---|---|---|
| `{plan_text}` | plan artifact | Full approved plan |
| `{result_file_path}` | engine | Absolute pending result path |
| `{schema_json}` | `schemas/execution_result.schema.json` | Execution result schema |

## Template

```text
{plan_text}

FULLY IMPLEMENT THE PLAN ABOVE
- Do not commit or push any changes yet. Leave them for reviewing.
- Update the documentation accordingly if needed.
- If no changes are needed, explain that in the result summary.

After making changes, write your result JSON to:
{result_file_path}

The JSON must conform to this schema:
{schema_json}

If you cannot write the file, respond with ONLY the raw JSON. No markdown fences. No commentary.
```
