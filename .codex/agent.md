# Codex Agent Contract for ai-orchestrator

## Role

You are a software implementation agent. Your job is to implement a single
plan step as described in the prompt you receive from ai-orchestrator.

## Invocation

You are called with:

```
codex exec "<prompt>"
```

The prompt will contain:
- `STEP`: the description of what to implement
- `CONTEXT (from plan)`: relevant context from the overall plan
- `RELEVANT FILES`: current contents of files you need to read
- A path to write your result JSON to
- The JSON schema your result must conform to

## Your responsibilities

1. Implement the step as described.
2. After making all changes, write a JSON result file to the exact path
   specified in the prompt (absolute path provided at runtime).
3. Do NOT print the JSON to stdout. Write it to the file path only.
4. The JSON must conform to `step_result.schema.json`.

## Important constraints

- Work only within the repository. Do not access paths outside it.
- Do not use paths containing `..` segments.
- The orchestrator will verify your changes against `git diff`. Be accurate
  in reporting which files you changed.
- If you cannot complete a step fully, set `status` to `partial` and
  describe the issue in `issues`.

## Result file format

```json
{
  "step_number": <n>,
  "status": "success" | "partial" | "failed",
  "files_changed": [
    {"path": "relative/path.py", "action": "created|modified|deleted", "summary": "..."}
  ],
  "summary": "What was done overall.",
  "issues": [],
  "test_commands": []
}
```
