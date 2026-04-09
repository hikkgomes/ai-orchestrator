# Final Hardening Implementation

## Files Changed

- `src/ai_orchestrator/worktree.py`
- `tests/test_engine.py`
- `tests/test_worktree.py`
- `AGENTS.md`
- `docs/output-contracts.md`
- `docs/final-hardening-implementation.md`

## Change Summary

- `src/ai_orchestrator/worktree.py`
  Replaced `git checkout -- .` with `git reset --hard HEAD` in `WorktreeManager.reset()`, while keeping `git clean -fd`. Retry cleanup now resets both the index and working tree to `HEAD` before removing untracked files.

- `tests/test_engine.py`
  Added `test_worktree_reset_clears_staged_index_changes` to prove an execution retry starts from a clean committed baseline even after the failed attempt staged both a modified tracked file and a newly added file. Also extended `test_engine_retries_when_step_reports_failed_status` to assert the per-step retry counter resets to `0` after a successful retry.

- `tests/test_worktree.py`
  Hardened `test_worktree_manager_reset` so it stages both a tracked-file modification and a new file before calling `reset()`, then verifies both `git status --porcelain` and `git diff --cached` are empty afterward.

- `AGENTS.md`
  Updated the retry protocol to match the engine contract: `1` initial invocation plus up to `max_retries` retries, execution-phase pre-retry worktree reset and pending-result cleanup, and retry counter reset on success.

- `docs/output-contracts.md`
  Corrected retry wording so it describes initial invocation plus retries, and documents that successful retries clear the retry counter.

- `docs/final-hardening-implementation.md`
  Recorded the scoped implementation, affected files, test coverage, and the remaining intentional limitation around ignored files.

## Tests Added Or Updated

- Added `tests/test_engine.py::test_worktree_reset_clears_staged_index_changes`
- Updated `tests/test_engine.py::test_engine_retries_when_step_reports_failed_status`
- Updated `tests/test_worktree.py::test_worktree_manager_reset`

Verified with:

- `PYTHONPATH=src .venv-test/bin/pytest tests/test_engine.py tests/test_worktree.py -q`

## Remaining Risks

- Ignored files are still preserved across retry cleanup because the reset path intentionally keeps `git clean -fd` rather than `git clean -fdx`. That remains the current contract to avoid deleting ignored local caches or vendor CLI state.
