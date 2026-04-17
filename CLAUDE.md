# CLAUDE.md — ai-orchestrator

> **Design status: ACTIVE** as of 2026-04-09.

## What is this project?

A local orchestrator that coordinates Claude Code (`claude -p`) and Codex (`codex exec`) as stateless worker processes. No API keys. No SDKs. CLI-only AI access.

## Key architecture rules

- **No API calls** — all AI interaction is via `claude -p` and `codex exec` subprocesses.
- **Fresh subprocess per invocation** — every CLI call is a new subprocess. Planning and review may explicitly resume the same Claude session with `--resume` when the workflow requires iterative continuity.
- **Disk artifacts only** — steps communicate through JSON files in `.ai-orchestrator/`, never through stdout chaining.
- **Schema + application validation** — every AI output is validated against a JSON schema in `schemas/` and then against application-level invariants before the orchestrator acts on it.
- **Single preserved worktree per run** — all mutating steps execute sequentially in one worktree branch. Fix cycles build on existing changes instead of discarding the worktree.
- **Resumable state** — orchestrator state is persisted to `.ai-orchestrator/state/run-<uuid>.json` after every phase change.
- **Fail closed** — unsupported CLI versions, unrecognized output, and unexpected failures cause deterministic errors, not silent degradation.

## Project layout

```
src/ai_orchestrator/    # Python package source
  cli.py                # click CLI (entry point: orch; aio alias)
  engine.py             # orchestrator state machine
  adapters/             # claude.py, codex.py — subprocess wrappers
  reviewer/             # heuristic scanner + repo analysis helpers
  state.py              # state persistence
  worktree.py           # git worktree manager
  validator.py          # JSON schema + application validation
  ui.py                 # rich terminal UI
schemas/                # JSON schemas for all artifact types
docs/                   # architecture, workflow, contracts, install, security
tests/                  # unit + integration tests
```

`workflows/default.yaml` is the authoritative workflow definition; `aio.toml` only overrides supported settings.

## CLI commands

| Command | Purpose |
|---|---|
| `orch run <task>` | Start orchestrated run |
| `orch resume <run-id>` | Resume paused/crashed run |
| `orch approve <run-id> <gate>` | Approve a pending gate |
| `orch reject <run-id> <gate>` | Soft-reject with feedback; use `--full` to terminate where supported |
| `orch status` | Show run status |
| `orch log <run-id>` | View logs |
| `orch review-install` | Install reviewer config and bundled rules |
| `orch review-analyze` | Refresh reviewer config from repo heuristics |
| `orch clean` | Remove completed artifacts |
| `orch config` | Show/edit config |
| `orch doctor` | Verify environment and CLI versions |

`aio` remains available as a compatibility alias for the same CLI entry point.

## Canonical workflow states

INIT → SCOPING → PLANNING → APPROVAL_PLAN → EXECUTING → REVIEWING → MERGING → DONE

Also: FAILED, TERMINATED, PAUSED, BLOCKED_ON_CLI, CONFLICT

SCOPING is a Claude/Codex debate that produces `scope.md`. PLANNING preserves Claude session IDs for refinement. REVIEWING includes Claude review, Codex cross-check, and one Claude Opus/max final decision when they disagree; fixes return to planning as incremental work on top of the existing worktree.

## Coding standards

- Python 3.11+
- Type hints on all public functions
- `pathlib.Path` for all file paths
- `subprocess` APIs with `shell=False` always
- `jsonschema` for schema validation, application code for semantic validation
- `click` for CLI, `rich` for terminal UI
- `filelock` for cross-platform file locking
- No AI SDK imports. No HTTP client libraries.
- Tests: pytest, mock adapters for unit tests, real CLIs for integration tests

## Config

- Repo-level: `aio.toml`
- Global: `~/.config/ai-orchestrator/config.toml` (macOS/Linux) or `%APPDATA%\ai-orchestrator\config.toml` (Windows)

## When working on this codebase

- Read `docs/architecture.md` for system design
- Read `docs/workflow.md` for phase details and canonical state machine
- Read `docs/output-contracts.md` for schema contracts
- Read `docs/design-decisions.md` for rationale behind key choices
- Validate all schema changes against `docs/output-contracts.md` — they must stay in sync

## Prompt library

Canonical prompt templates for every workflow phase live in `docs/prompts/`:

| File | Phase | Produces |
|---|---|---|
| `scope.md` | SCOPING | transient scoping result |
| `plan.md` | PLANNING (first plan) | `plans/plan-<uuid>.json` |
| `implement.md` | EXECUTING (full plan) | `results/execution-<uuid>.json` |
| `review.md` | REVIEWING | `reviews/review-<uuid>.json` |
| `fix-plan.md` | PLANNING (incremental fix loop) | `plans/plan-<uuid>.json` (new) |

Deferred prompt drafts live under `docs/prompts/deferred/` and are not wired into the current engine:

| File | Intended phase | Status |
|---|---|---|
| `deferred/finalize.md` | DONE entry | Deferred |

Each prompt file defines: variables, escalation policy, scope constraints, template
text (with `{variable}` placeholders for Python f-string substitution), and retry
prompt. The orchestrator renders these via `engine.py` before each CLI invocation.

## Claude Code skills

`.claude/skills/` contains per-role instruction files used when Claude Code acts
as a workflow agent:

| Directory | Role | Phase |
|---|---|---|
| `orchestration-architect/SKILL.md` | Planner / fix-planner | PLANNING |
| `orchestration-reviewer/SKILL.md` | Reviewer | REVIEWING |
| `fix-planner/SKILL.md` | Replanner | PLANNING (incremental fix loop) |

## Codex agents

`.codex/agents/` contains per-role instruction files for Codex worker agents:

| File | Role | Phase |
|---|---|---|
| `implementer.md` | Step executor | EXECUTING |
| `adjudicator.md` | Legacy review cross-checker | REVIEWING |
| `repairer.md` | Fix executor | EXECUTING (incremental fix loop) |
