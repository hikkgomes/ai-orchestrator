# Implementation Review

> Reviewer: Claude Opus 4.6
> Date: 2026-04-09
> Scope: Full codebase review against frozen design (2026-04-08)
> Schemas, source, tests, docs, prompts, config, and packaging

---

## Findings

### BLOCKING

#### B-1: Define and feasibility phases are documented but not implemented

- **What is wrong:** `docs/prompts/define.md` describes a pre-PLANNING define phase that normalizes the raw task. `docs/prompts/feasibility.md` describes a post-approval feasibility check. Neither phase exists in the engine. `engine.py:_run_planning` passes `state.task` directly to the planner with no TaskDefinition gate. The engine has no feasibility state, no routing key, and no schema for either. `CLAUDE.md` lists both prompt files in the prompt library table; `AGENTS.md` lists feasibility in the Codex agents table and define in the prompt library. Yet the engine skips straight from INIT to PLANNING and from APPROVAL_PLAN to EXECUTING.
- **Files:** `src/ai_orchestrator/engine.py`, `docs/prompts/define.md`, `docs/prompts/feasibility.md`, `AGENTS.md`
- **Why it matters:** Operators reading the docs and prompt library will expect a define gate and a feasibility check. The prompts are written, the agent files reference them, but the engine never invokes them. This is a contract violation between docs and code.
- **Fix direction:** Either (a) implement both phases in the engine with their schemas, routing keys, and state transitions, or (b) move `define.md` and `feasibility.md` to a `docs/prompts/deferred/` directory, mark them as "v2" in CLAUDE.md and AGENTS.md, and remove them from the active prompt library table. Option (b) is lower risk for a frozen design.
- **In scope:** Yes. The build plan (Phase 12) requires all docs to match implementation.

#### B-2: Finalize phase is documented but not implemented

- **What is wrong:** `docs/prompts/finalize.md` describes a post-merge run summary phase. `CLAUDE.md` lists it in the prompt library. The engine transitions directly from successful merge to DONE in `_run_merge` without invoking any CLI for finalization. There is no `build_finalize_prompt` in `templates.py`.
- **Files:** `src/ai_orchestrator/engine.py:584-588`, `docs/prompts/finalize.md`, `src/ai_orchestrator/prompts/templates.py`
- **Why it matters:** Same contract violation as B-1. The prompt template exists and is documented but never called.
- **Fix direction:** Same as B-1 — implement or explicitly defer with doc updates.
- **In scope:** Yes.

#### B-3: `datetime.utcnow()` is deprecated and produces naive datetimes

- **What is wrong:** `models.py:205-206` uses `datetime.utcnow()` for `created_at` and `updated_at` default factories. This has been deprecated since Python 3.12 (PEP 738) and produces timezone-naive strings. The rest of the codebase (`state.py:109`, `event_log.py:24`, `adapters/base.py:225`) correctly uses `datetime.now(timezone.utc)`.
- **Files:** `src/ai_orchestrator/models.py:205-206`
- **Why it matters:** On Python 3.12+ a deprecation warning is emitted. The timestamps are also inconsistent with other timestamps in the same run state file (updated_at is timezone-aware after the first save, but created_at remains naive).
- **Fix direction:** Replace `datetime.utcnow().isoformat()` with `datetime.now(timezone.utc).isoformat()` in both default factories.
- **In scope:** Yes.

---

### IMPORTANT

#### I-1: No graceful subprocess termination (SIGTERM before SIGKILL)

- **What is wrong:** `AGENTS.md` specifies "macOS/Linux: SIGTERM, wait 10s, SIGKILL if still alive" and "Windows: TerminateProcess". Both adapters use bare `subprocess.run()` with a `timeout` parameter. When `TimeoutExpired` is raised, Python's subprocess module sends SIGKILL (on Unix) or TerminateProcess (on Windows) immediately. There is no SIGTERM-then-wait-then-SIGKILL sequence.
- **Files:** `src/ai_orchestrator/adapters/claude.py:106-139`, `src/ai_orchestrator/adapters/codex.py:72-122`, `AGENTS.md` (Timeout Strategy section)
- **Why it matters:** Vendor CLIs may need a graceful shutdown period to flush caches or release lock files. Immediate SIGKILL can leave corrupted state. The documented contract is not honored.
- **Fix direction:** Replace `subprocess.run()` with `subprocess.Popen()` and implement the SIGTERM → wait → SIGKILL sequence manually. Alternatively, update the docs to state that immediate kill is the v1 behavior and graceful termination is deferred.
- **In scope:** Yes. This is a documented contract.

#### I-2: Engine does not handle `status = "failed"` in step results

- **What is wrong:** `docs/prompts/implement.md` states: "On `status = "failed"` in a validated result: orchestrator treats it as a `StepFailure` and retries up to `max_retries`." However, `engine.py:_run_execution` accepts the validated result regardless of `status` and moves to the next step. It never checks `result["status"]`.
- **Files:** `src/ai_orchestrator/engine.py:360-386`, `docs/prompts/implement.md:205-207`
- **Why it matters:** A step that reports itself as failed will be treated as complete. The review phase will then review an implementation that the executor flagged as broken, wasting a review+adjudication cycle or producing a false PASS.
- **Fix direction:** After validation in `_run_execution`, check `result["status"]`. If `"failed"`, raise `StepFailure` to trigger the retry logic already in place.
- **In scope:** Yes.

#### I-3: Codex adapter `invoke()` signature differs from base class

- **What is wrong:** `BaseAdapter.invoke()` takes `(prompt, working_dir, timeout, schema)`. `CodexAdapter.invoke()` adds `step_number=0` as a keyword argument. The engine works around this with a lambda that captures `step_number`, but the signature mismatch means the Codex adapter cannot be used interchangeably with the Claude adapter via the base interface. Typing tools will flag the override.
- **Files:** `src/ai_orchestrator/adapters/codex.py:47-54`, `src/ai_orchestrator/adapters/base.py:113-145`
- **Why it matters:** Violates the Liskov substitution principle. If a future caller invokes `adapter.invoke(prompt, dir, timeout, schema)` without `step_number`, the Codex adapter silently uses `step_number=0`, which produces a result file at `pending-step-0.json` — an invalid path.
- **Fix direction:** Either add `step_number` to the base class signature with a default, or pass `step_number` through the schema dict or a context parameter, or accept this as a known deviation and document it.
- **In scope:** Yes.

#### I-4: Missing rework_steps validation against current plan in adjudication validator

- **What is wrong:** `docs/output-contracts.md` states: "`rework_steps` values must reference valid step numbers from the plan." The `validator.py:validate_adjudication` method checks that `rework_steps` is non-empty for REWORK verdicts, but does not check that the step numbers are valid references to the current plan. The schema-level `if/then` also does not cross-reference.
- **Files:** `src/ai_orchestrator/validator.py:256-288`
- **Why it matters:** An adjudication could request rework of step 99 in a 3-step plan. The engine would then look up step 99, not find it in `steps_by_number`, and raise a `KeyError`.
- **Fix direction:** Add an optional `plan_step_numbers` parameter to `validate_adjudication` and validate that all `rework_steps` values are within that set. The engine can pass the plan's step numbers when calling validation.
- **In scope:** Yes.

#### I-5: Secret scanning is not applied to execution prompts

- **What is wrong:** `collect_file_context` scans for secrets and excludes files. The planning prompt uses `collect_file_context` via `engine.py:_run_planning`. The execution prompt in `_run_execution` also uses `collect_file_context` (line 314), which is good. However, the review prompt passes `self._implementation_diff(state)` (a raw `git diff` output) directly into the prompt without any secret scanning. The adjudication prompt similarly passes raw review and step result JSON.
- **Files:** `src/ai_orchestrator/engine.py:394-398`, `src/ai_orchestrator/prompts/templates.py`
- **Why it matters:** `docs/security.md` and `AGENTS.md` promise secret scanning "before sending file content as part of a prompt." A git diff can contain full file contents including secrets. The diff bypasses the scanning.
- **Fix direction:** Run the secret pattern scan against the git diff string before passing it to `build_review_prompt`. If secrets are detected, either redact the relevant hunks or warn and truncate.
- **In scope:** Yes. Security contract.

#### I-6: `_parse_scalar` in workflow.py misparses negative integers and floats

- **What is wrong:** The custom YAML parser's `_parse_scalar` function uses `value.isdigit()` to detect integers. This returns False for negative numbers (e.g., `-1`) and floats (e.g., `0.5`). While the current `default.yaml` has no such values, any future workflow definition with negative or decimal scalars will be parsed as strings.
- **Files:** `src/ai_orchestrator/workflow.py:209-218`
- **Why it matters:** The custom YAML parser is a maintenance risk. If anyone adds a timeout like `0.5` or a loop limit of `-1` (meaning unlimited), it will silently be treated as a string and fail downstream in unexpected ways.
- **Fix direction:** Improve `_parse_scalar` to handle negative integers and simple floats. Add a comment explaining the supported subset. Alternatively, consider using PyYAML as an optional dependency with a stdlib-only fallback.
- **In scope:** Yes, but low urgency since current data is unaffected.

---

### MINOR

#### M-1: `_TERMINAL_STATES` exported with underscore prefix in `__all__`

- **What is wrong:** `ui.py:393-397` exports `_ACTIVE_STATES` and `_TERMINAL_STATES` in `__all__`. Both have a leading underscore, signaling private. `cli.py:19` imports `_TERMINAL_STATES`. This is not harmful but violates naming convention — public exports should not have leading underscores.
- **Files:** `src/ai_orchestrator/ui.py:393-397`, `src/ai_orchestrator/cli.py:19`
- **Why it matters:** Minor style inconsistency. Could confuse contributors.
- **Fix direction:** Either rename to `TERMINAL_STATES` / `ACTIVE_STATES` or remove from `__all__` and keep them module-private.
- **In scope:** Yes.

#### M-2: `_drive_interactive_approvals` missing return type annotation

- **What is wrong:** `cli.py:266` defines `_drive_interactive_approvals` without a return type hint. All other functions in the module have type annotations.
- **Files:** `src/ai_orchestrator/cli.py:266`
- **Why it matters:** Inconsistent with the project's own coding standard ("Type hints on all public functions"). While this is private, it is the only function in the file without a return annotation.
- **Fix direction:** Add `-> RunState` return annotation.
- **In scope:** Yes.

#### M-3: `_render_status` missing return type annotation

- **What is wrong:** `cli.py:289` defines `_render_status` without a return type hint.
- **Files:** `src/ai_orchestrator/cli.py:289`
- **Why it matters:** Same as M-2.
- **Fix direction:** Add `-> RenderableType` return annotation.
- **In scope:** Yes.

#### M-4: README references `orch` as primary command but CLAUDE.md uses `aio`

- **What is wrong:** README.md line 8 introduces `orch` as primary and `aio` as compatibility alias. CLAUDE.md's command table uses `aio` exclusively. The pyproject.toml registers both entry points, which is correct.
- **Files:** `README.md`, `CLAUDE.md`
- **Why it matters:** New contributors reading CLAUDE.md will use `aio`, which works, but will be confused when docs and README say `orch`.
- **Fix direction:** Align CLAUDE.md command table to use `orch` (or add a note that `aio` is the alias).
- **In scope:** Yes (Phase 12 doc consistency).

#### M-5: `.env` secret detection is overly broad

- **What is wrong:** `templates.py:284` checks `".env" in lowered_path`. This matches any path containing the substring `.env`, such as `src/environment.py` or `config/.environment/settings.json`. These are legitimate code files, not secret stores.
- **Files:** `src/ai_orchestrator/prompts/templates.py:282-286`
- **Why it matters:** False positives could exclude important context files from prompts.
- **Fix direction:** Tighten the check to match only files whose basename is `.env` or matches `.env.*` (e.g., `.env.local`, `.env.production`). Something like `Path(path).name.startswith(".env")`.
- **In scope:** Yes.

#### M-6: No test for the `aio` entry point alias

- **What is wrong:** `pyproject.toml` registers both `orch` and `aio` as entry points. The test suite only validates `orch`. There is no test confirming `aio` resolves to the same CLI.
- **Files:** `tests/test_install.py`, `tests/test_cli.py`
- **Why it matters:** The `aio` alias could silently break during a packaging change.
- **Fix direction:** Add a smoke test that invokes `aio --help` after install.
- **In scope:** Yes.

#### M-7: Config `_apply_section` silently drops unknown keys

- **What is wrong:** `config.py:133-137` filters out unknown keys when constructing dataclass sections. If a user adds `[orchestrator]\nunknown_key = 42` to `aio.toml`, it is silently ignored. There is no warning.
- **Files:** `src/ai_orchestrator/config.py:133-137`
- **Why it matters:** Users may misspell config keys (e.g., `max_retry` instead of `max_retries`) and wonder why their configuration has no effect.
- **Fix direction:** Emit a warning via `logging` or `warnings.warn` when unknown keys are encountered in config sections.
- **In scope:** Yes, but low priority for v1.

---

### OPTIONAL

#### O-1: No artifact tests

- **What is wrong:** There are no tests for `artifacts.py`. All artifact operations (save_plan, save_step_result, read_json, approval flow, execution manifest, atomic_write) are exercised only indirectly through the engine integration test.
- **Files:** `tests/` (missing `test_artifacts.py`)
- **Why it matters:** A bug in `_atomic_write`, `_write_versioned_json`, or `consume_approval_decision` would not be caught by a focused unit test. The engine test could pass while artifact edge cases (permission errors, corrupt files) go untested.
- **Fix direction:** Add `test_artifacts.py` with unit tests for `ArtifactStore`.
- **In scope:** Yes (Phase 11 test targets).

#### O-2: No metadata store tests

- **What is wrong:** There are no direct tests for `metadata.py`. SQLite operations are verified only as side effects in `test_state.py` and `test_adapters.py`.
- **Files:** `tests/` (missing `test_metadata.py`)
- **Why it matters:** Schema migration, concurrent access, and edge cases in `record_invocation` are untested.
- **Fix direction:** Add `test_metadata.py`.
- **In scope:** Yes (Phase 11).

#### O-3: No workflow definition tests

- **What is wrong:** There are no tests for `workflow.py` or the custom YAML parser. The parser is non-trivial (~130 lines) and handles indentation, folded scalars, lists, and mappings.
- **Files:** `tests/` (missing `test_workflow.py`)
- **Why it matters:** The custom YAML parser is the most fragile part of the codebase. A whitespace or indentation edge case in `default.yaml` could break silently.
- **Fix direction:** Add `test_workflow.py` with tests for the parser and `load_workflow_definition`.
- **In scope:** Yes (Phase 11).

#### O-4: No bootstrap tests beyond CLI smoke

- **What is wrong:** `scaffold_repository` and `install_shell_integration` are tested through CLI smoke tests, but edge cases (existing gitignore without newline, fish shell, PowerShell, `--force` flag behavior) are not tested.
- **Files:** `tests/` (missing dedicated `test_bootstrap.py`)
- **Why it matters:** Shell integration writes to user dotfiles. Edge cases could corrupt `.bashrc` or `.zshrc`.
- **Fix direction:** Add focused unit tests for `bootstrap.py` functions.
- **In scope:** Yes (Phase 11).

#### O-5: Engine `_update_step_result_reference` reads all step results on every step

- **What is wrong:** `engine.py:842-848` rebuilds the entire step results mapping from disk on every step completion by calling `self._artifacts.read_json(ref)` for every existing reference. For a 20-step plan, step 20 reads all 19 prior results from disk just to insert one new reference.
- **Files:** `src/ai_orchestrator/engine.py:842-848`
- **Why it matters:** Performance. For typical plans (2-5 steps) this is negligible. For larger plans it's linear overhead per step.
- **Fix direction:** Maintain an in-memory mapping of `step_number -> reference` rather than reconstructing from disk on each step.
- **In scope:** Yes, but low priority.

---

## Summary

The implementation is a solid, well-structured orchestrator that follows the frozen design closely. The code quality is high: consistent style, good use of Pydantic models, atomic writes, proper error classification, and thorough use of `shell=False` everywhere. The test suite covers the critical paths (engine happy path, approval flow, rework loop limits, resume, worktree lifecycle, merge conflicts, adapter output parsing, config loading, validation edge cases, CLI smoke tests, and UI rendering).

The three blocking findings (B-1, B-2, B-3) are all doc-vs-code mismatches or deprecation issues, not architectural flaws. The important findings (I-1 through I-6) identify genuine gaps in contract enforcement (subprocess termination, step failure handling, secret scanning of diffs, adjudication validation). These are fixable without redesign.

The test suite has good coverage of the engine and adapters but is missing dedicated unit tests for artifacts, metadata, workflow parsing, and bootstrap — all of which are exercised indirectly but deserve focused coverage per the build plan's Phase 11 target.

---

## Verdict

**APPROVE WITH FIXES**

The blocking findings (B-1, B-2, B-3) and important findings (I-1 through I-5) must be resolved before the implementation is considered complete per the frozen design contract. All fixes are incremental and do not require architectural changes.
