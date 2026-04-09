# Implement Phase Prompt

> Workflow phase: EXECUTING (one prompt per plan step)
> CLI: `codex exec` (default); configurable via `routing.worker`
> Output artifact: `results/step-<n>-<uuid>.json`
> Schema: `schemas/step_result.schema.json`
> State transitions: EXECUTING (step N) → EXECUTING (step N+1) → REVIEWING

---

## Purpose

Execute a single plan step: make the code changes described in the step,
then write a result JSON file reporting what was changed.

Each step runs in the shared worktree branch for this run. Steps execute
sequentially. Step N sees all file changes made by steps 1 through N-1.

---

## Variables

| Variable | Source | Description |
|---|---|---|
| `{step_number}` | plan | Integer step number (1-indexed) |
| `{step_description}` | plan step | What this step must accomplish |
| `{estimated_complexity}` | plan step | `low`, `medium`, or `high` |
| `{plan_context}` | plan | `reasoning` field from the plan JSON |
| `{all_steps_summary}` | plan | Compact list of all step numbers and descriptions |
| `{file_contents}` | worktree | Contents of all `files_to_read` for this step, each prefixed with its path |
| `{files_to_modify}` | plan step | Newline-separated list of target file paths |
| `{result_file_path}` | engine | Absolute path: `.ai-orchestrator/results/pending-step-{step_number}.json` |
| `{step_result_schema}` | `schemas/step_result.schema.json` | Full JSON Schema for the result artifact |
| `{rework_feedback}` | adjudication (optional) | Populated only on rework loops; empty string on first attempt |
| `{rework_attempt}` | run state | 0 on first attempt; increments on each rework |
| `{max_retries}` | config | Maximum retry attempts per step |

---

## Escalation Policy

**Increase effort** when:
- `estimated_complexity = "high"` — allow full timeout; do not short-circuit
- `rework_attempt >= 1` — a prior attempt was adjudicated as needing rework;
  pay close attention to `rework_feedback`

**Set `status = "partial"` and continue** (do not abort) when:
- A non-critical part of the step cannot be completed
- A secondary file cannot be created/modified but the core change is done
- Record all issues in the `issues` array

**Set `status = "failed"` in the result** (do not abort mid-step) when:
- The step description is contradictory or impossible given the current file state
- A required file listed in `files_to_read` does not exist and cannot be created
- Do NOT crash. Write the result file with `status = "failed"` and describe the issue.

**Do NOT escalate to human** from within this phase. Escalation happens only at
approval gates and is managed by the orchestrator engine. Write the result file
and let the engine decide what to do next.

---

## Scope Constraints

- Implement only what the step description says. Do not implement adjacent steps.
- Do not read or modify files outside the repository root.
- Do not use paths containing `..` segments.
- Do not access the network.
- Do not run tests or verification commands unless `test_commands` is explicitly
  requested in the step description. Record any test commands you suggest in
  `test_commands` but do not run them.
- Do not commit changes. The orchestrator commits after reading the result file.
- Do not write to `.ai-orchestrator/` except the result file path specified.
- Do not print the result JSON to stdout when using Codex. Write it to the file.

---

## Template (Codex variant — primary; writes result file)

```
You are a software implementation agent for an automated orchestrator.

Implement exactly the step described below. Do not implement any other steps.
Do not ask questions. This is a single-pass invocation with no follow-up.

STEP {step_number}:
{step_description}

PLAN CONTEXT:
{plan_context}

ALL STEPS IN THIS RUN (for context only — implement only STEP {step_number}):
{all_steps_summary}

RELEVANT FILE CONTENTS:
{file_contents}

FILES YOU MAY MODIFY:
{files_to_modify}

{rework_section}

After making all changes, write your result JSON to EXACTLY this path:
{result_file_path}

Do NOT print the JSON to stdout. Write it to the file path above only.

The JSON must conform to this schema:
{step_result_schema}

RESULT RULES:
- "step_number" must be {step_number}
- "status" must be "success" if all intended changes were made, "partial" if
  some were made but not all, "failed" if no useful changes were made
- If "status" is "success", "files_changed" must have at least one entry
- Each entry in "files_changed" must have a relative path (no leading /, no ..)
- "summary" must describe what was actually done (not what was planned)
- "issues" lists problems encountered; empty array if none
- "test_commands" lists commands to verify this step; empty array if none
```

### Rework section (injected only when `rework_attempt >= 1`)

```
REWORK ATTEMPT {rework_attempt}:
A previous implementation of this step was reviewed and rejected.
You must address the following feedback:

{rework_feedback}

Do not repeat the same approach. Read the feedback carefully and implement
a corrected version.
```

---

## Template (Claude variant — JSON on stdout)

```
You are a software implementation agent for an automated orchestrator.

Implement exactly the step described below. Do not implement any other steps.
Do not ask questions. This is a single-pass invocation with no follow-up.

STEP {step_number}:
{step_description}

PLAN CONTEXT:
{plan_context}

ALL STEPS IN THIS RUN (for context only — implement only STEP {step_number}):
{all_steps_summary}

RELEVANT FILE CONTENTS:
{file_contents}

FILES YOU MAY MODIFY:
{files_to_modify}

{rework_section}

OUTPUT SCHEMA:
{step_result_schema}

RESULT RULES:
- "step_number" must be {step_number}
- "status" must be "success", "partial", or "failed"
- If "status" is "success", "files_changed" must have at least one entry
- Paths in "files_changed" must be relative (no leading /, no .. segments)
- "summary" describes what was actually done
- "issues" lists problems; empty array if none
- "test_commands" lists verification commands; empty array if none

Respond with ONLY valid JSON. No markdown fences. No commentary.
```

---

## Retry Prompt (on schema/parse/validation failure)

```
Your previous response was not valid JSON, did not match the required schema,
or failed validation.

Error: {validation_error}

Retry implementing step {step_number}. Produce a fresh implementation.
Do not reference your previous attempt.

STEP {step_number}:
{step_description}

RELEVANT FILE CONTENTS:
{file_contents}

OUTPUT SCHEMA:
{step_result_schema}

Respond with ONLY valid JSON. No markdown fences. No commentary.
```

---

## Engine Behaviour After This Phase

Per-step sequence:
1. Orchestrator renders this prompt with the step's variables and invokes the CLI.
2. For Codex: reads result from `{result_file_path}`. Falls back to stdout scan, then git-diff reconstruction. See `AGENTS.md` for full fallback strategy.
3. For Claude: parses JSON from stdout (strict, then lenient fallback).
4. `files_changed` is always reconciled against `git diff` in the worktree. The git diff is ground truth.
5. Validated result is written to `results/step-{step_number}-<uuid>.json`.
6. Orchestrator commits: `git add -A && git commit -m "aio: step {step_number} — {step_description[:60]}"`.
7. Advances to next step or (after all steps) to REVIEWING.

On `status = "failed"` in a validated result: orchestrator treats it as a `StepFailure`
and retries up to `max_retries`. If retries exhausted, run transitions to FAILED.
