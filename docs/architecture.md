# Architecture

> **Design status: UPDATED** as of 2026-04-09.

## Overview

**ai-orchestrator** is a local orchestrator that coordinates Claude Code (`claude -p`) and Codex (`codex exec`) as subprocess worker processes. The orchestrator is a Python package with a rich terminal UI. It never calls any API directly — all AI interaction happens through locally installed, pre-authenticated CLIs.

## Core Principles

1. **CLI-only AI access** — no API keys, no SDKs, no HTTP calls to model providers.
2. **Fresh subprocess per invocation, resumable vendor sessions** — every CLI call is a new subprocess. Claude phases may intentionally resume one unified session (`claude_main`) across scoping, planning, and reviewing via `--resume`. Codex runs with `--json`, captures the emitted `thread_id`, and can resume that thread for later Codex rounds. Note: vendor CLIs may retain their own local state (auth, caches, project metadata) in the user's home directory; the orchestrator does not sandbox this.
3. **Disk-artifact communication** — steps exchange data through JSON files in `.ai-orchestrator/`, never through stdout chaining.
4. **Resumable orchestrator state** — the orchestrator persists its own state to disk so it can crash and resume.
5. **Single worktree per run** — all mutating steps execute in one ephemeral git worktree branch per run, in sequence. The main branch is never touched until merge.
6. **Structured outputs** — planning emits markdown; other AI outputs emit JSON validated against schemas before the orchestrator advances. Application-level validation supplements schema checks for invariants like path normalization and diff correspondence.
7. **Human approval gates** — configurable points where the orchestrator blocks until a human approves.
8. **Fail closed on unknowns** — unsupported CLI versions, unexpected exit codes, and unrecognized output formats cause deterministic failures, not silent degradation.

## System Diagram

```
┌──────────────────────────────────────────────────────────────┐
│                      ai-orchestrator CLI                      │
│  (Python – click + rich)                                      │
│                                                               │
│  ┌──────────┐  ┌──────────┐  ┌──────────────────────────┐ │
│  │  Scoper  │→ │  Planner │→ │  Reviewer + Fix Debate   │ │
│  └────┬─────┘  └────┬─────┘  └────┬─────────────────────┘ │
│       │              │             │                       │
│       ▼              ▼             ▼                       │
│  ┌─────────────────────────────────────────────────────────┐ │
│  │              Artifact Store (.ai-orchestrator/)          │ │
│  │ scoping/ plans/ results/ reviews/                       │ │
│  │ state/ logs/                                             │ │
│  └─────────────────────────────────────────────────────────┘ │
│       │              │                                        │
│       ▼              ▼                                        │
│  ┌──────────┐  ┌──────────┐                                  │
│  │claude -p │  │codex exec│   (subprocess; Claude may resume) │
│  └──────────┘  └──────────┘                                  │
└──────────────────────────────────────────────────────────────┘
```

## Directory Layout (Runtime)

All orchestrator state lives under `.ai-orchestrator/` at the repo root:

```
.ai-orchestrator/
├── metadata.sqlite3            # run + adapter invocation metadata
├── state/
│   └── run-<uuid>.json          # orchestrator run state (resumable)
├── scoping/
│   └── scope-<run>.md            # canonical scope with YAML frontmatter
├── plans/
│   └── plan-<prefix>-<hash>.md  # markdown plan with YAML frontmatter
├── results/
│   └── execution-<uuid>.json    # validated against execution_result.schema.json
├── reviews/
│   ├── review-<uuid>.json       # validated against review.schema.json
│   └── debate-round-<n>-<run>.json
├── worktrees/
│   └── run-<uuid>/              # single git worktree per run
├── prompts/
│   └── <phase>-<run>.md         # rendered prompt sent to CLI (opt-in retention)
└── logs/
    ├── run-<uuid>.log           # orchestrator events
    ├── claude-<uuid>.log        # raw stdout/stderr from claude -p (opt-in)
    └── codex-<uuid>.log         # raw stdout/stderr from codex exec (opt-in)
```

## Component Architecture

### 1. CLI Layer (`aio` command)

Built with `click` for command parsing and `rich` for terminal UI. Entry points:

| Command | Description |
|---|---|
| `aio run <task>` | Start a new orchestrated run |
| `aio resume <run-id>` | Resume a crashed/paused run |
| `aio status [run-id]` | Show run status in terminal UI |
| `aio approve <run-id> <gate>` | Approve a pending gate |
| `aio reject <run-id> <gate>` | Reject with feedback |
| `aio log <run-id> [step]` | View logs |
| `aio clean [--all]` | Remove completed run artifacts |
| `aio config` | Show/edit orchestrator config |
| `aio doctor` | Verify CLI dependencies, versions, and authentication |

### 2. Orchestrator Engine

The engine is a finite state machine that advances through workflow phases. State transitions are persisted to `state/run-<uuid>.json` after every phase change. `workflows/default.yaml` defines the phase structure and default phase-level settings; `aio.toml` overrides supported routing, retry, session, debate, and watchdog values.

#### Canonical State Machine

States:

| State | Description |
|---|---|
| `INIT` | Run created, not yet started |
| `SCOPING` | Scoper CLI invocation in progress |
| `PLANNING` | Planner CLI invocation in progress |
| `APPROVAL_PLAN` | Waiting for human to approve/reject the plan |
| `EXECUTING` | Worker CLI executing the full plan |
| `REVIEWING` | Claude review, Codex cross-check, and final review decision |
| `MERGING` | Merging worktree branch into base branch |
| `DONE` | Run completed successfully |
| `FAILED` | Unrecoverable error or loop limit exceeded |
| `PAUSED` | Waiting at an approval gate |
| `BLOCKED_ON_CLI` | Vendor CLI requires interactive input or auth refresh |
| `CONFLICT` | Merge conflict detected, requires manual resolution |

Allowed transitions are defined in `docs/workflow.md`.

The engine is strictly sequential: one full-plan execution session at a time, one worktree per run.

### 3. CLI Adapters

Two adapter classes implementing a common interface:

#### ClaudeAdapter

```
Invocation: claude -p "<prompt>" --output-format json
Working dir: worktree path or repo root
Timeout: global watchdog timeout
Output: JSON parsed from stdout (strict, then lenient fallback)
```

#### CodexAdapter

```
Invocation: codex exec [resume <thread-id>] --skip-git-repo-check --sandbox workspace-write --json "<prompt>"
Working dir: worktree path
Timeout: global watchdog timeout
Output: thread id from JSONL; files_changed from git diff; metadata from result file or JSONL/stdout fallback
```

Both adapters:
- Run as subprocesses via the `subprocess` module with `shell=False`
- Capture stdout and stderr separately
- Log raw output to `logs/` (when `logging.retain_raw_output = true`)
- Validate output against the step's expected schema
- Return a typed result object or raise `AdapterError` with classified failure type
- Detect interactive/auth-required exits and surface them as `BLOCKED_ON_CLI`

#### Output Strategies

**Claude adapter (primary):** Parse `--output-format json` stdout. Try `json.loads(stdout)` first. On failure, strip markdown fences and find JSON boundaries (lenient mode). Log a warning on lenient success.

**Codex adapter (primary):** After `codex exec --json` completes, read a result file from a known path. Execution writes `.ai-orchestrator/results/pending-execution-<run>.json`. Execution reconstructs `files_changed` from `git diff` in the worktree. If the result file is missing, fall back to `item.completed` JSONL agent-message content and then legacy stdout JSON scanning. If both fail during execution, construct a minimal result from git diff alone.

### 4. Schema Validator

Uses `jsonschema` for structural validation. Application-level validation (in `validator.py`) supplements schemas for:

- Plan step ordering and sequential numbering
- Dependency graph acyclicity
- Path normalization: reject paths containing `..` segments anywhere (not just leading), resolve and verify all paths stay within the repo root
- Correspondence between `files_changed` and actual git diff (for Codex results)

Validation failures are non-recoverable for the current step attempt (trigger retry or fail).

### 5. Worktree Manager

- Creates one worktree per run: `git worktree add .ai-orchestrator/worktrees/run-<uuid> -b aio/run-<uuid>`
- All steps execute sequentially in this worktree, so each step sees the output of prior steps
- Resets the worktree to the last committed step baseline before an execution retry
- On success + merge approval: merges the single worktree branch back to base
- On failure/rejection: `git worktree remove --force`
- Records the base commit SHA at worktree creation; verifies it before merge
- Worktrees are named deterministically: `run-<short-uuid>`

### 6. State Manager

- Reads/writes `state/run-<uuid>.json`
- Uses `filelock` (cross-platform) to prevent concurrent access
- Every state transition is atomic: write to temp file, then `os.replace()`
- Mirrors run metadata into `.ai-orchestrator/metadata.sqlite3` for local querying
- On corruption: attempt parse of the state file; if invalid JSON, the run is unrecoverable and must be cleaned up manually

### 6a. Metadata Store

- Uses SQLite (`.ai-orchestrator/metadata.sqlite3`) via the Python stdlib `sqlite3` module
- Stores run metadata snapshots and adapter invocation metadata
- Persists step-level execution metadata independently from JSON artifacts

### 7. Approval Manager

- When the engine reaches an approval gate, it writes `PAUSED` state and exits the event loop
- The `aio approve` / `aio reject` commands modify state and re-trigger the engine
- In interactive mode (`aio run --interactive`), gates are presented inline in the terminal UI

## Configuration

`aio.toml` at repo root (or `~/.config/ai-orchestrator/config.toml` for global defaults):

```toml
[orchestrator]
max_retries = 3
watchdog_timeout = 3600

[routing]
planner = "claude"
worker = "codex"
reviewer = "claude"
scoper = "claude"

[routing.claude]
reasoning_effort = "high"

[scoping]
enabled = true

[sessions]
enable_unified_session = true
enable_planning_resume = true
enable_review_resume = true

[debate]
escalated_claude_model = "claude-opus-4-7"
escalated_claude_effort = "xhigh"
escalated_codex_effort = "high"
review_codex_model = "gpt-5.4"

[approval]
require_plan_approval = true
require_merge_approval = true

[worktree]
base_branch = "main"
branch_prefix = "aio/"

[logging]
retain_raw_output = false        # opt-in raw stdout/stderr retention
retain_prompts = false           # opt-in prompt file retention

[cli_compat]
claude_min_version = ""          # set after testing; aio doctor enforces
codex_min_version = ""           # set after testing; aio doctor enforces
```

## Error Model

| Error Type | Classification | Behavior |
|---|---|---|
| CLI not found | `ENV_ERROR` | `aio doctor` reports it; run aborts with actionable message |
| CLI version unsupported | `ENV_ERROR` | `aio doctor` reports it; run aborts |
| CLI auth failure / interactive prompt | `BLOCKED_ON_CLI` | Run transitions to `BLOCKED_ON_CLI`; user fixes auth, then `aio resume` |
| CLI timeout | `STEP_FAILURE` | Step marked FAILED, retry if under limit |
| Non-zero exit (generic) | `STEP_FAILURE` | Stderr logged, step marked FAILED, retry if under limit |
| Invalid JSON output | `STEP_FAILURE` | Retry with stricter prompt |
| Schema validation failure | `STEP_FAILURE` | Retry if under limit |
| Application validation failure | `STEP_FAILURE` | Retry if under limit |
| Worktree creation failure | `RUN_FAILURE` | Abort run with diagnostic |
| Merge conflict | `CONFLICT` | Transition to `CONFLICT`; user resolves, then `aio resume` |
| Dirty working tree at merge | `RUN_FAILURE` | Abort merge; user must commit or stash |
| State file corruption | `RUN_FAILURE` | Run is unrecoverable; `aio clean` to remove |

## Platform Support

| Platform | Support level | Notes |
|---|---|---|
| macOS (ARM/Intel) | Primary | Fully tested |
| Linux (x86_64) | Primary | Fully tested |
| Windows | Experimental | Known gaps: signal handling, path length, open-handle cleanup. Tested in CI but not all edge cases covered. |

| Concern | macOS/Linux | Windows |
|---|---|---|
| Subprocess | `subprocess.Popen` / `subprocess.run` | `subprocess.Popen` / `subprocess.run` |
| File locking | `filelock` (cross-platform) | `filelock` (cross-platform) |
| Process termination | SIGTERM → SIGKILL | `TerminateProcess` |
| Git worktrees | native | native |
| Path handling | `pathlib.Path` everywhere | `pathlib.Path` everywhere |
| Terminal UI | `rich` | `rich` (Windows Terminal recommended) |
| Config paths | `~/.config/ai-orchestrator/` | `%APPDATA%\ai-orchestrator\` |

## Dependency Inventory

| Dependency | Purpose | License |
|---|---|---|
| click | CLI framework | BSD-3 |
| rich | Terminal UI, progress, tables | MIT |
| jsonschema | JSON Schema validation | MIT |
| tomli / tomllib | TOML config parsing (tomllib in stdlib 3.11+) | MIT |
| pydantic | Internal data models | MIT |
| filelock | Cross-platform file locking | Unlicense |

No AI SDKs. No API client libraries. No network dependencies at runtime.

## Assumptions (Frozen)

These assumptions are accepted for v1. If any proves false during implementation, the affected phase must be redesigned.

1. `claude -p --output-format json` produces parseable JSON on stdout for the tested version range.
2. `codex exec` can be instructed via prompt to write a result file to a known path.
3. Both CLIs support non-interactive execution without hanging for user input when properly invoked.
4. `git worktree` is available on all supported platforms with git 2.20+.
5. A single sequential worktree is sufficient for v1; parallel execution is not needed.
6. TOML config via `tomllib` (stdlib) is sufficient; no need for Jinja2 or YAML.
7. `filelock` provides adequate cross-platform locking for single-machine use.
