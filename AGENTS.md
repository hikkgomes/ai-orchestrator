# AGENTS.md — CLI Adapter Contracts

> **Design status: FROZEN** as of 2026-04-08.

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
    ) -> dict
```

**Inputs:**
- `prompt` — rendered prompt string (task + context + schema)
- `working_dir` — absolute path to run the subprocess in (repo root or worktree)
- `timeout` — seconds before the subprocess is killed
- `schema` — JSON schema dict to validate the output against
- `step_number` — optional execution-step context used by worker adapters

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
| Planning | Yes | high |
| Execution | No (Codex default) | medium |
| Review | Yes | high |
| Adjudication | Yes | high |

---

## Codex Adapter

### Invocation

```bash
codex exec "<prompt>"
```

### Flags

| Flag | Purpose | Always used |
|---|---|---|
| `exec "<prompt>"` | Execute a task | Yes |

### Output strategy (three-tier fallback)

Because `codex exec` mutates files directly and may not produce clean JSON on stdout:

1. **Result file (primary):** The prompt instructs Codex to write a JSON result to `.ai-orchestrator/results/pending-step-<n>.json`. If this file exists after execution, it is read and validated.
2. **Stdout fallback:** If the result file is missing, scan stdout from the end backwards for the last valid JSON object. Parse and validate.
3. **Git-diff-only fallback:** If both above fail, reconstruct a minimal `step_result` from `git diff --name-status` in the worktree. `files_changed` comes from git. `summary` defaults to "Changes detected via git diff." `status` defaults to `partial`. Metadata fields (`issues`, `test_commands`) are empty.

In all cases, `files_changed` is verified against `git diff` in the worktree. The git diff is the ground truth for what files changed; the AI-provided `files_changed` is treated as metadata only.

### Routing defaults

| Phase | Used by default |
|---|---|
| Planning | No (Claude default) |
| Execution | Yes |
| Review | No (Claude default) |
| Adjudication | No (Claude default) |

---

## Adapter Selection (Routing)

Configured in `aio.toml` under `[routing]`:

```toml
[routing]
planner = "claude"
worker = "codex"
reviewer = "claude"
adjudicator = "claude"
```

Any phase can be routed to either CLI:

```toml
[routing]
worker = "claude"  # use Claude for execution instead of Codex
```

---

## Retry Protocol

Both adapters follow the same retry protocol:

1. **Attempt 1** — standard prompt
2. **On `StepFailure`** — log error, construct retry prompt:
   ```
   Your previous response was not valid. Error: {error_message}

   Fix the error and try again. The full original prompt follows.

   ---

   {original_prompt}
   ```
3. **Attempt 2** — retry prompt (fresh subprocess, fresh context)
4. **On `StepFailure`** — same as above with accumulated error context
5. **Attempt 3** — final retry
6. **On `StepFailure`** — raise to engine, step marked FAILED

On `BlockedOnCLI` — no retry. Transition to `BLOCKED_ON_CLI` state immediately.

Max retries configurable via `orchestrator.max_retries` (default: 3).

---

## Timeout Strategy

| Phase | Default timeout | Config key |
|---|---|---|
| Planning | 120s | `orchestrator.planning_timeout` |
| Execution (low complexity) | 180s | `orchestrator.execution_timeout_low` |
| Execution (medium complexity) | 300s | `orchestrator.execution_timeout_medium` |
| Execution (high complexity) | 600s | `orchestrator.execution_timeout_high` |
| Review | 180s | `orchestrator.review_timeout` |
| Adjudication | 120s | `orchestrator.adjudication_timeout` |

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
| `docs/prompts/plan.md` | PLANNING | `claude -p` | `plan.schema.json` |
| `docs/prompts/implement.md` | EXECUTING | `codex exec` or `claude -p` | `step_result.schema.json` |
| `docs/prompts/review.md` | REVIEWING | `claude -p` | `review.schema.json` |
| `docs/prompts/adjudicate.md` | ADJUDICATING | `claude -p` | `adjudication.schema.json` |
| `docs/prompts/fix-plan.md` | PLANNING (replan) | `claude -p` | `plan.schema.json` |

Deferred prompt drafts are kept under `docs/prompts/deferred/` and are not invoked by the v1 engine:

| Prompt file | Intended phase | Status |
|---|---|---|
| `docs/prompts/deferred/define.md` | INIT pre-gate | Deferred |
| `docs/prompts/deferred/feasibility.md` | pre-EXECUTING | Deferred |
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
| `adjudicator.md` | ADJUDICATING (alternate) | `docs/prompts/adjudicate.md` |
| `repairer.md` | EXECUTING (rework loop) | `docs/prompts/implement.md` (rework variant) |

Deferred Codex agent drafts:

| Agent file | Intended phase | Prompt used |
|---|---|---|
| `feasibility.md` | pre-EXECUTING | `docs/prompts/deferred/feasibility.md` |

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
