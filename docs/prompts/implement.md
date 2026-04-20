# Implement Phase Prompt

> Workflow phase: EXECUTING (one prompt for the full plan)
> CLI: `codex exec` (default); configurable via `routing.worker`
> Output artifact: `results/execution-<uuid>.json`
> Schema: `schemas/execution_result.schema.json`
> State transitions: EXECUTING → REVIEWING

---

## Purpose

Execute the full natural plan in one continuous worker session. Codex receives
the complete plan, relevant file contents from the flat `key_files` list, and a
single output schema. The worker may commit after logical chunks; if it leaves
uncommitted changes, the engine creates one fallback commit.

---

## Variables

| Variable | Source | Description |
|---|---|---|
| `{plan_text}` | plan artifact | Full plan text (markdown for new plans, JSON block for legacy plans) |
| `{file_contents}` | worktree | Contents of all `key_files`, each prefixed with its path |
| `{result_file_path}` | engine | Absolute path: `.ai-orchestrator/results/pending-execution-<run>.json` |
| `{execution_result_schema}` | `schemas/execution_result.schema.json` | Full JSON Schema for the result artifact |
| `{workspace_trees}` | workspace mode | Per-repo directory trees, when applicable |

---

## Scope Constraints

- Implement the whole plan, not just the first implementation step.
- Keep changes limited to the repository/workspace.
- Do not use paths containing `..` segments.
- Do not access the network unless the existing project workflow requires it and the environment already supports it.
- Write one execution result JSON artifact.
- Prefer writing the result JSON to the specified result file path. If that fails, respond with only raw JSON on stdout.

---

## Template (Codex variant)

```
You are a software implementation agent. Execute the full plan in this
single Codex session. Maintain context across all implementation steps and
make the smallest correct set of changes.

PLAN:
{plan_text}

RELEVANT FILES:
{file_contents}

IMPLEMENTATION RULES:
- Implement the whole plan, not just the first listed item.
- Commit after each logical chunk when running in a git worktree, using:
  git add -A && git commit -m "aio: <description>"
- If no changes are needed, explain that in the result summary.

After making changes, write your result JSON to:
{result_file_path}

The JSON must conform to this schema:
{execution_result_schema}

If you cannot write the file, respond with ONLY the raw JSON. No markdown fences. No commentary.
```

## Template (Claude variant)

```
You are a software implementation agent. Execute the full plan in one
continuous pass and then return one JSON result.

PLAN:
{plan_text}

RELEVANT FILES:
{file_contents}

OUTPUT SCHEMA:
{execution_result_schema}

Respond with ONLY valid JSON. No markdown fences. No commentary.
```
