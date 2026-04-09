# Feasibility Phase Prompt (Deferred)

> Workflow phase: post-APPROVAL_PLAN, pre-EXECUTING (optional gate)
> CLI: `codex exec` (default); configurable via `routing.feasibility_checker`
> Output artifact: transient — result consumed by engine; not persisted as a named artifact
> Schema: inline (no external schema file)
> State transitions: APPROVAL_PLAN → (feasibility check) → EXECUTING or PAUSED

> Deferred draft: not invoked by the v1 engine.

---

## Purpose

Before the executor touches any files, run a lightweight environment probe to
verify the plan is executable in the current repository state. This phase:

1. Checks that all `files_to_read` and `files_to_modify` paths exist or are
   creatable within the repo root
2. Detects missing dependencies, broken build environment, or failing tests
   that would cause every execution step to fail immediately
3. Reports a go/no-go verdict with specific blocking issues if no-go

This phase is **non-mutating**. No files are written or modified. No commits.
If the check requires running commands, they must be read-only (e.g., `pip check`,
`pytest --collect-only`, `tsc --noEmit`).

---

## Variables

| Variable | Source | Description |
|---|---|---|
| `{task_description}` | run state | Normalized task description |
| `{plan_json}` | `plans/plan-<uuid>.json` | Full plan JSON produced by the planning phase |
| `{directory_tree}` | file system (worktree root) | Depth-3 tree of the worktree before any changes |
| `{result_file_path}` | engine | Absolute path to write the feasibility result JSON |
| `{feasibility_schema}` | inline below | JSON Schema for the expected output |

---

## Escalation Policy

**Stop and set state = PAUSED** (require human decision) when:
- `verdict = "blocked"` and `blocking_issues` contains a `severity = "critical"` entry
  (e.g., broken toolchain, missing required files the plan cannot create)

**Proceed to EXECUTING** when:
- `verdict = "go"` — no blocking issues found
- `verdict = "go_with_warnings"` — warnings present but none are critical

**Do not escalate for:**
- Minor warnings (style, optional deps, non-critical test failures)
- Issues the plan explicitly addresses (e.g., plan step 1 installs a missing dep)

---

## Scope Constraints

- This agent runs in the worktree but must not modify any files.
- Read-only commands only. No `pip install`, `git commit`, `npm install` etc.
- Do not attempt to fix issues found. Report them only.
- One pass. No follow-up. No questions.
- If a check command fails to run (not found, permissions), record it as a
  warning with `severity = "warning"`, not `severity = "critical"`.

---

## Output Schema (inline)

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "FeasibilityResult",
  "type": "object",
  "required": ["verdict", "blocking_issues", "summary"],
  "additionalProperties": false,
  "properties": {
    "verdict": {
      "type": "string",
      "enum": ["go", "go_with_warnings", "blocked"],
      "description": "Overall go/no-go decision."
    },
    "blocking_issues": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["severity", "description"],
        "additionalProperties": false,
        "properties": {
          "severity": {
            "type": "string",
            "enum": ["critical", "warning"]
          },
          "description": {
            "type": "string",
            "minLength": 1
          },
          "suggestion": {
            "type": "string"
          }
        }
      },
      "description": "Issues found. Empty array if verdict is 'go'."
    },
    "summary": {
      "type": "string",
      "minLength": 1,
      "description": "One-paragraph summary of the feasibility check result."
    }
  },
  "allOf": [
    {
      "if": { "properties": { "verdict": { "const": "blocked" } } },
      "then": {
        "properties": {
          "blocking_issues": { "minItems": 1 }
        }
      }
    }
  ]
}
```

---

## Template (Codex variant — writes result file)

```
You are a feasibility checker for an automated software orchestrator.

Your job is to verify that the following plan can be executed in the current
repository environment. This is a READ-ONLY check. Do not modify any files.
Do not install packages. Do not run mutating commands.

TASK:
{task_description}

PLAN:
{plan_json}

REPOSITORY STRUCTURE:
{directory_tree}

CHECKS TO PERFORM:
1. Verify that all paths listed in "files_to_read" across all plan steps exist
   in the repository. Paths that don't exist and aren't listed in any step's
   "files_to_modify" are potential issues.
2. Check that the build/test environment is intact. Run read-only probes only
   (e.g., `python -c "import <dep>"`, `which <tool>`, `git status`).
3. Check for obvious blockers: broken imports, missing config files the plan
   depends on, etc.
4. Do NOT attempt to fix anything. Report only.

After checking, write your result JSON to:
{result_file_path}

The JSON must conform to this schema:
{feasibility_schema}

Do NOT print the JSON to stdout. Write it to the file path above only.
Do NOT modify any source files. Do NOT commit anything.
```

## Template (Claude variant — JSON on stdout)

```
You are a feasibility checker for an automated software orchestrator.

Your job is to review the following plan and identify any conditions in the
current repository that would prevent execution. This is a STATIC ANALYSIS
only — you cannot run commands. Use the repository structure and plan contents
to reason about feasibility.

TASK:
{task_description}

PLAN:
{plan_json}

REPOSITORY STRUCTURE:
{directory_tree}

CHECKS TO PERFORM:
1. Verify all "files_to_read" paths exist or will exist by the time the step
   runs (i.e., an earlier step creates them).
2. Identify any "files_to_modify" paths that are outside the repository root
   or contain path traversal (automatic blocking issue).
3. Flag any steps where the description implies network access, credential use,
   or interactive input — all of which cannot proceed.
4. Note any ambiguous or contradictory step dependencies.

OUTPUT SCHEMA:
{feasibility_schema}

Respond with ONLY valid JSON. No markdown fences. No commentary.
```

---

## Retry Prompt (on schema/parse failure)

```
Your previous response was not valid. Error: {validation_error}

Fix the error and try again. The full original prompt follows.

---

{original_prompt}
```

---

## Engine Behaviour After This Phase

- If `verdict = "go"` or `verdict = "go_with_warnings"`: engine transitions to EXECUTING.
  Warnings are logged but do not block. They are included in the final run log.
- If `verdict = "blocked"` and any issue has `severity = "critical"`: engine transitions
  to PAUSED. The `summary` and `blocking_issues` are printed to the terminal.
  Human must resolve the issue and run `aio resume <run-id>` to retry feasibility.
- The `FeasibilityResult` JSON is NOT stored as a named artifact in `plans/` or
  `results/`. It is logged to `logs/run-<uuid>.log` and held in run state only.
- This phase is optional. It is skipped if `feasibility.enabled = false` in `aio.toml`
  (default: `true`).
