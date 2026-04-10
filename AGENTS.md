# AGENTS.md — CLI Adapter Contracts

> **Design status: UPDATED** as of 2026-04-09.

This document defines the exact contracts for how ai-orchestrator invokes Claude Code and Codex as subprocess workers.

---

## General Adapter Contract

Both adapters implement the same interface:

```
class BaseAdapter:
    def invoke(
        prompt: str,
        working_dir: Path,
        timeout: int,
        schema: dict,
        *,
        step_number: int | None = None,
        reasoning_effort_override: str | None = None,
        model_override: str | None = None,
    ) -> dict
```

**Inputs:**
- `prompt` — rendered prompt string (task + context + schema)
- `working_dir` — absolute path to run the subprocess in (repo root or worktree)
- `timeout` — seconds before the subprocess is killed
- `schema` — JSON schema dict to validate the output against
- `step_number` — optional execution-step context used by worker adapters
- `reasoning_effort_override` — optional phase-specific override
- `model_override` — optional phase-specific override

**Outputs:**
- Returns a validated `dict` matching the schema
- Raises `AdapterError` subclass on any failure:
  - `StepFailure(exit_code, stdout, stderr, validation_error)` — generic execution failure
  - `BlockedOnCLI(exit_code, stderr)` — CLI requires interactive input or auth refresh

**Behavior:**
- Runs subprocess with `shell=False`
- Captures stdout and stderr separately
- Logs raw output to `logs/<cli>-<uuid>.log` (when `logging.retain_raw_output = true`)
- Parses output per adapter-specific strategy (see below)
- Validates parsed JSON against `schema` (structural) and application-level checks
- Returns validated dict or raises

**Environment filtering:**
- Inherits only: `PATH`, `HOME`, `USER`, `LANG`, `TERM`, `GIT_DIR`, `GIT_WORK_TREE`
- All other env vars are stripped (especially credential vars like `AWS_SECRET_ACCESS_KEY`, `GITHUB_TOKEN`, etc.)
- The vendor CLI's own auth/config state in `HOME` is intentionally accessible — this is required for the CLI to authenticate

**Exit code classification:**
- Exit 0 with valid output → success
- Exit 0 with invalid output → `StepFailure` (retry)
- Non-zero exit with auth-related stderr patterns → `BlockedOnCLI`
- Timeout with no output for extended period → `BlockedOnCLI`
- Non-zero exit (other) → `StepFailure` (retry)
- Timeout with partial output → `StepFailure` (retry)

---

## Claude Code Adapter

### Invocation

```bash
claude -p "<prompt>" --output-format json --max-turns 1
```

### Flags

| Flag | Purpose | Always used |
|---|---|---|
| `-p "<prompt>"` | Non-interactive prompt mode | Yes |
| `--output-format json` | Request JSON output | Yes |
| `--max-turns 1` | Prevent multi-turn behavior | Yes |

### Output parsing strategy

1. **Strict:** `json.loads(stdout)`. If `--output-format json` wraps the response in an envelope (e.g., a `result` field), extract the inner content.
2. **Lenient fallback:** Strip markdown fences (```` ```json ... ``` ````), find JSON object/array boundaries, parse. Log a warning on lenient success.
3. **Failure:** If both fail, raise `StepFailure` with the raw output for retry.

### ANSI handling

Strip ANSI escape codes from stdout before JSON parsing. The CLI may emit them if it detects a terminal.

### Reasoning effort

If configured in `aio.toml` (`routing.claude.reasoning_effort`), the adapter attempts to pass the flag. If the CLI does not support the flag (error on that flag specifically), the adapter retries without it. This is graceful degradation, not a hard dependency.

### Routing defaults

| Phase | Used by default | Reasoning effort |
|---|---|---|
| Scoping | Yes | high |
| Planning | Yes | high |
| Execution | No (Codex default) | medium |
| Review | Yes | high |
| Adjudication | No (Codex default) | medium |

---

## Codex Adapter

### Invocation

```bash
codex exec --skip-git-repo-check "<prompt>"
```

### Flags

| Flag | Purpose | Always used |
|---|---|---|
| `exec "<prompt>"` | Execute a task | Yes |
| `--skip-git-repo-check` | Allow orchestrator-managed worktrees and other trusted non-standard git layouts | Yes |

### Output strategy (three-tier fallback)

Because `codex exec` mutates files directly and may not produce clean JSON on stdout:

1. **Result file (primary):** The prompt instructs Codex to write a JSON result file. Execution uses `.ai-orchestrator/results/pending-step-<n>.json`; feasibility uses `.ai-orchestrator/feasibility/pending-<run-id>.json`.
2. **Stdout fallback:** If the result file is missing, scan stdout from the end backwards for the last valid JSON object. Parse and validate.
3. **Git-diff-only fallback:** Execution only. If both above fail for a step result, reconstruct a minimal `step_result` from `git diff --name-status` in the worktree. `files_changed` comes from git. `summary` defaults to "Changes detected via git diff." `status` defaults to `partial`. Metadata fields (`issues`, `test_commands`) are empty.

In all cases, `files_changed` is verified against `git diff` in the worktree. The git diff is the ground truth for what files changed; the AI-provided `files_changed` is treated as metadata only.

### Routing defaults

| Phase | Used by default |
|---|---|
| Feasibility | Yes |
| Planning | No (Claude default) |
| Execution | Yes |
| Review | No (Claude default) |
| Adjudication | Yes |

---

## Adapter Selection (Routing)

Configured in `aio.toml` under `[routing]`:

```toml
[routing]
scoper = "claude"
planner = "claude"
feasibility_checker = "codex"
worker = "codex"
reviewer = "claude"
adjudicator = "codex"
```

Any phase can be routed to either CLI:

```toml
[routing]
worker = "claude"  # use Claude for execution instead of Codex
```

---

## Retry Protocol

Both adapters follow the same retry protocol, driven by
`_invoke_with_retries` in `engine.py`:

1. **Initial invocation** — standard prompt.
2. **On `StepFailure`** — increment retry counter. If retries exhausted,
   raise to engine (step marked FAILED). Otherwise construct retry prompt:
   ```
   Your previous response was not valid. Error: {error_message}

   Fix the error and try again. The full original prompt follows.

   ---

   {original_prompt}
   ```
3. **Retry invocation** — fresh subprocess with the retry prompt.
4. Repeat from step 2 until success or retry limit reached.

**Total invocations:** 1 initial + up to `max_retries` retries. With the
default `max_retries = 3`, a step can be invoked up to **4 times** before
failing.

**Execution-phase retries** have an additional pre-retry step: before each
retry invocation, the engine resets the worktree to the last committed state
(`git reset --hard HEAD` followed by `git clean -fd`) and clears the pending
step result file. This ensures each retry starts from an identical filesystem
baseline. Planning, review, and adjudication retries do not modify the
worktree and skip this step.

**On success:** the retry counter for that step/phase is reset to 0, so
subsequent steps start with a fresh retry budget.

**On `BlockedOnCLI`:** no retry. Transition to `BLOCKED_ON_CLI` immediately.

Max retries configurable via `orchestrator.max_retries` (default: 3).

---

## Timeout Strategy

All CLI invocations use a single global watchdog timeout from `orchestrator.watchdog_timeout` (default: 3600 seconds). This watchdog exists only as a safety net for genuinely hung subprocesses; normal phase completion is governed by the vendor CLI's own lifecycle controls (`--max-turns` for Claude, task completion for Codex), so long-running but healthy planning or execution work is not cut off by per-phase limits.

**Termination sequence:**
- macOS/Linux: SIGTERM, wait 10s, SIGKILL if still alive
- Windows: `TerminateProcess` (no graceful shutdown equivalent)

On timeout, the step is treated as `StepFailure` (retry if under limit), unless the no-output heuristic triggers `BlockedOnCLI`.

---

## Prompt Library

Canonical prompt templates live in `docs/prompts/`. Each file documents the
complete template, variable table, escalation policy, scope constraints, and
retry prompt for its phase. The orchestrator renders these via f-string
substitution before each CLI invocation.

| Prompt file | Phase | CLI | Output schema |
|---|---|---|---|
| `docs/prompts/scope.md` | SCOPING | `claude -p` | `scoping.schema.json` |
| `docs/prompts/plan.md` | PLANNING | `claude -p` | `plan.schema.json` |
| `docs/prompts/feasibility.md` | FEASIBILITY | `codex exec` or `claude -p` | `feasibility.schema.json` |
| `docs/prompts/implement.md` | EXECUTING | `codex exec` or `claude -p` | `step_result.schema.json` |
| `docs/prompts/review.md` | REVIEWING | `claude -p` | `review.schema.json` |
| `docs/prompts/adjudicate.md` | ADJUDICATING | `codex exec` or `claude -p` | `adjudication.schema.json` |
| `docs/prompts/fix-plan.md` | PLANNING (replan) | `claude -p` | `plan.schema.json` |

Deferred prompt drafts are kept under `docs/prompts/deferred/` and are not invoked by the current engine:

| Prompt file | Intended phase | Status |
|---|---|---|
| `docs/prompts/deferred/finalize.md` | DONE entry | Deferred |

## Claude Code Skills

`.claude/skills/` contains per-role instruction files loaded by Claude Code when
acting as a workflow agent. These supplement the prompt with role-specific rules
and hard constraints.

| Skill directory | Active phase | Prompt used |
|---|---|---|
| `orchestration-architect/` | PLANNING | `docs/prompts/plan.md` |
| `orchestration-reviewer/` | REVIEWING | `docs/prompts/review.md` |
| `fix-planner/` | PLANNING (replan loop) | `docs/prompts/fix-plan.md` |

## Codex Agents

`.codex/agents/` contains per-role instruction files read by Codex when acting as
a workflow agent. These supplement the prompt with Codex-specific output conventions
(result file vs. stdout) and role-specific rules.

| Agent file | Active phase | Prompt used |
|---|---|---|
| `implementer.md` | EXECUTING | `docs/prompts/implement.md` |
| `adjudicator.md` | ADJUDICATING | `docs/prompts/adjudicate.md` |
| `feasibility.md` | FEASIBILITY | `docs/prompts/feasibility.md` |
| `repairer.md` | EXECUTING (rework loop) | `docs/prompts/implement.md` (rework variant) |

## Prompt Templates

Prompts are constructed using Python f-strings. Each phase has a template:

### Planning prompt

```
You are a software planning agent. Given the following task and repository context,
produce a JSON plan conforming to the schema below.

TASK:
{task_description}

REPOSITORY STRUCTURE:
{directory_tree}

KEY FILE CONTENTS:
{key_file_contents}

OUTPUT SCHEMA:
{plan.schema.json contents}

Respond with ONLY valid JSON. No markdown fences. No commentary.
```

### Execution prompt (Codex)

```
You are a software implementation agent. Implement the following step.

STEP:
{step_description}

CONTEXT (from plan):
{plan_context}

RELEVANT FILES:
{file_contents}

After making changes, write a JSON result file to the path:
{result_file_path}

The JSON must conform to this schema:
{step_result.schema.json contents}

Do not print the JSON to stdout. Write it to the file path above.
```

### Execution prompt (Claude)

```
You are a software implementation agent. Implement the following step.

STEP:
{step_description}

CONTEXT (from plan):
{plan_context}

RELEVANT FILES:
{file_contents}

OUTPUT SCHEMA:
{step_result.schema.json contents}

Respond with ONLY valid JSON. No markdown fences. No commentary.
```

### Review prompt

```
You are a code review agent. Review the following implementation.

ORIGINAL TASK:
{task_description}

PLAN:
{plan_json}

IMPLEMENTATION DIFF:
{git_diff}

STEP RESULTS:
{step_results_json}

Produce a JSON review conforming to this schema:
{review.schema.json contents}

Respond with ONLY valid JSON. No markdown fences. No commentary.
```

### Adjudication prompt

```
You are an adjudication agent. Decide whether this implementation should be merged,
reworked, replanned, or abandoned.

ORIGINAL TASK:
{task_description}

REVIEW:
{review_json}

STEP RESULTS:
{step_results_json}

Produce a JSON adjudication conforming to this schema:
{adjudication.schema.json contents}

Respond with ONLY valid JSON. No markdown fences. No commentary.
```

### Secret scanning

Before including any file content in a prompt, the orchestrator scans for common secret patterns (AWS keys, private keys, high-entropy token assignments, `.env` content). Detected files are excluded with a warning. This is best-effort, not a guarantee.
