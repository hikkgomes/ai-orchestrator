# Build Plan

> **Design status: FROZEN** as of 2026-04-08.

Implementation phases in dependency order. Each phase produces testable, working code before the next begins.

---

## Phase 1: Package Scaffold

**Goal:** Runnable Python package with `aio --help` working.

- Create `pyproject.toml` with hatchling build system, PEP 621 metadata, entry point `aio = ai_orchestrator.cli:main`
- Create `src/ai_orchestrator/__init__.py` with version reading from `importlib.metadata`
- Create `src/ai_orchestrator/cli.py` with click group and placeholder commands (`run`, `resume`, `approve`, `reject`, `status`, `log`, `clean`, `config`, `doctor`)
- Verify `pip install -e .` works and `aio --help` prints command list
- Set up `tests/` directory with `conftest.py`

**Exit criteria:** `pip install -e ".[dev]" && aio --help` succeeds.

---

## Phase 2: Config / State / Logging

**Goal:** Config loading, state persistence, and structured logging operational.

- Implement config loader: read `aio.toml` (repo root) with `~/.config/ai-orchestrator/config.toml` fallback, merge with defaults, validate structure
- Implement `state.py`: `RunState` pydantic model matching the orchestrator state contract, atomic read/write with `filelock`, `os.replace()` for atomic rename
- Implement structured logging: orchestrator event log to `logs/run-<uuid>.log` with timestamps
- Wire config into CLI context (`click.Context.obj`)

**Exit criteria:** Unit tests for config loading (defaults, overrides, invalid config), state read/write/atomicity, and log output.

---

## Phase 3: Claude CLI Adapter

**Goal:** Working Claude adapter that invokes `claude -p`, parses output, validates against schema.

- Implement `adapters/base.py`: abstract `BaseAdapter` with `invoke(prompt, working_dir, timeout, schema) -> dict` interface and `AdapterError` hierarchy
- Implement `adapters/claude.py`: subprocess invocation with `claude -p "<prompt>" --output-format json --max-turns 1`, environment filtering, timeout handling
- JSON parsing: strict `json.loads()` first, lenient (strip fences, find boundaries) fallback, warning on lenient success
- Schema validation of parsed output via `validator.py`
- Exit code classification: auth/interactive detection → `BLOCKED_ON_CLI`, generic failure → `STEP_FAILURE`
- Raw output logging (when `logging.retain_raw_output = true`)

**Exit criteria:** Unit tests with mock subprocess. Manual integration test with real `claude -p` (if available).

---

## Phase 4: Codex CLI Adapter

**Goal:** Working Codex adapter with result-file-primary output strategy.

- Implement `adapters/codex.py`: subprocess invocation with `codex exec "<prompt>"`, environment filtering, timeout handling
- Result file strategy: check `.ai-orchestrator/results/pending-step-<n>.json` after execution
- Stdout fallback: scan from end for last valid JSON object
- Git diff fallback: reconstruct `files_changed` from `git diff --name-status`
- Merge strategy: `files_changed` from git, metadata from result file or stdout, defaults if both fail
- Exit code classification same as Claude adapter

**Exit criteria:** Unit tests with mock subprocess. Manual integration test with real `codex exec` (if available).

---

## Phase 5: Workflow Engine

**Goal:** State machine that drives the full workflow through all phases.

- Implement `engine.py`: finite state machine with canonical states (INIT, PLANNING, APPROVAL_PLAN, EXECUTING, REVIEWING, ADJUDICATING, APPROVAL_MERGE, MERGING, DONE, FAILED, PAUSED, BLOCKED_ON_CLI, CONFLICT)
- Implement allowed transitions as an explicit transition table
- State persistence after every transition via `state.py`
- Adapter routing based on config (`routing.planner`, `routing.worker`, etc.)
- Retry logic: per-step retry counter, rework loop counter, replan loop counter, all with configurable limits
- Resume logic: read state file, re-enter at `current_phase`
- Wire `aio run` and `aio resume` CLI commands to the engine

**Exit criteria:** Unit tests with mock adapters covering: happy path (all phases), retry on failure, rework loop, replan loop, loop limit → FAILED, resume from each resumable state.

---

## Phase 6: Artifact System

**Goal:** Schema validation, application validation, artifact storage.

- Implement `validator.py`: JSON schema validation via `jsonschema`, plus application-level checks:
  - Plan: step ordering, sequential numbering, dependency acyclicity, path normalization
  - Step result: path normalization, files_changed correspondence with git diff (for Codex)
  - Review: conditional field requirements
  - Adjudication: conditional field requirements
- Bundle schemas in `src/ai_orchestrator/schemas/` (copy from `schemas/`)
- Implement artifact read/write: plans, results, reviews, adjudications stored in `.ai-orchestrator/` subdirectories
- Implement prompt construction: f-string templates for each phase, file content inclusion with truncation, secret scanning before inclusion

**Exit criteria:** Unit tests for all schema validations, application validations (especially path traversal edge cases), and prompt construction.

---

## Phase 7: Approvals

**Goal:** Human approval gates functional in both batch and interactive modes.

- Implement approval manager: write PAUSED state at gates, check config for skip
- Implement `aio approve <run-id> <gate>` and `aio reject <run-id> <gate> --reason "..."` commands
- Implement interactive mode (`aio run --interactive`): inline approval prompts in terminal
- On rejection: feed reason back to appropriate phase (planning or adjudication)

**Exit criteria:** Unit tests for approval flow. Manual test of interactive approval.

---

## Phase 8: Git Worktree Isolation

**Goal:** Worktree lifecycle management integrated with the engine.

- Implement `worktree.py`: create worktree (`git worktree add`), record base commit SHA, remove worktree (`git worktree remove`)
- Single worktree per run: `run-<short-uuid>` naming
- Merge pre-checks: clean working tree verification, base commit SHA verification
- Merge execution: `git merge --no-ff`, conflict detection → CONFLICT state
- Cleanup: remove worktree and branch on DONE or FAILED
- `aio clean`: find and remove orphaned worktrees from completed/failed runs
- `aio doctor`: check for orphaned worktrees

**Exit criteria:** Unit tests with a real git repo (temp dir). Test worktree create/execute/merge/cleanup cycle. Test conflict detection. Test orphan cleanup.

---

## Phase 9: Terminal UI

**Goal:** Rich terminal output for all commands.

- Implement `ui.py` with `rich`: progress spinners during CLI invocations (with elapsed time), plan display tables, diff summaries, status dashboard, log viewer
- Wire UI into all CLI commands
- Interactive approval prompts with rich formatting
- Error display with actionable messages

**Exit criteria:** Manual verification of all UI states. Automated smoke test that commands don't crash.

---

## Phase 10: Install / Distribution

**Goal:** Installable package with doctor checks.

- Finalize `pyproject.toml`: dependencies, optional `[dev]` extras (pytest, etc.), entry points
- Implement full `aio doctor`: Python version, git version, CLI version range checks, capability probes, write permission check, config validation
- Create `.gitignore` template for users
- Test `pip install`, `pipx install`, editable install
- Test on macOS, Linux (CI), Windows (CI, experimental)

**Exit criteria:** `pip install ai-orchestrator && aio doctor` works on macOS and Linux CI. Windows CI passes with known-gap documentation.

---

## Phase 11: Tests

**Goal:** Comprehensive test suite.

- Unit tests for every module (mock adapters for adapter tests, real git for worktree tests)
- Contract tests: verify all schemas match documentation, all artifact types validate correctly
- Integration tests (mock): full workflow run with fixture adapter responses
- Integration tests (live, optional/nightly): full workflow with real CLIs against a test repo
- Edge case tests: path traversal, circular dependencies, retry limits, state corruption, dirty working tree

**Exit criteria:** >90% line coverage on orchestrator code (excluding UI). All contract tests pass. Mock integration test covers happy path and all error paths.

---

## Phase 12: Docs Finalisation

**Goal:** All documentation accurate and complete.

- Verify all docs match implementation (architecture, workflow, contracts, install, security)
- Update CLAUDE.md and AGENTS.md to match final implementation
- Write user-facing README with quickstart
- Update design-decisions.md with any decisions made during implementation
- Remove or update design-risks.md to reflect resolved/remaining risks
- Final review of unresolved-decisions.md

**Exit criteria:** Every doc is consistent with the code. No stale references.
