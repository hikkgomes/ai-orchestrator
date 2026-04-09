# Fix Implementation 01

## Files changed

- `src/ai_orchestrator/prompts/templates.py`: changed `build_retry_prompt()` to prepend retry guidance to the full original prompt instead of replacing context with schema-only text.
- `src/ai_orchestrator/engine.py`: removed the `schema_json` retry parameter, preserved the original prompt across retries, reset execution step state before retry attempts, and made `resume()` re-enter paused approval gates.
- `src/ai_orchestrator/worktree.py`: added `WorktreeManager.reset()` to discard tracked and untracked worktree changes with `git checkout -- .` and `git clean -fd`.
- `src/ai_orchestrator/workflow.py`: clarified in the module contract that `workflows/default.yaml` is authoritative and `aio.toml` only overrides supported settings.
- `src/ai_orchestrator/bootstrap.py`: updated the scaffolded `workflows/default.yaml` header comment to match the real workflow contract.
- `workflows/default.yaml`: replaced the misleading header comment so the checked-in workflow definition is documented as authoritative.
- `AGENTS.md`: updated the retry protocol to show the new retry prompt shape with the full original prompt.
- `CLAUDE.md`: added the workflow-definition contract note in the project layout section.
- `docs/architecture.md`: documented the workflow definition override hierarchy and the execution-retry worktree reset behavior.
- `docs/workflow.md`: documented that `workflows/default.yaml` is authoritative and that execution retries reset the worktree to the last committed step baseline.
- `docs/output-contracts.md`: updated retry-output documentation to describe retry preamble plus original prompt preservation.
- `docs/prompts/plan.md`: updated the documented retry prompt to the full-original-prompt form.
- `docs/prompts/implement.md`: updated the documented retry prompt to the full-original-prompt form.
- `docs/prompts/review.md`: updated the documented retry prompt to the full-original-prompt form.
- `docs/prompts/adjudicate.md`: updated the documented retry prompt to the full-original-prompt form.
- `docs/prompts/fix-plan.md`: updated the documented retry prompt to the full-original-prompt form.
- `docs/prompts/deferred/define.md`: updated the deferred retry prompt documentation to the same contract.
- `docs/prompts/deferred/feasibility.md`: updated the deferred retry prompt documentation to the same contract.
- `tests/test_prompts.py`: added retry-prompt coverage for original-context preservation.
- `tests/test_engine.py`: added retry-context, clean-retry, paused-resume, and reset-failure coverage; updated execution retry assertions.
- `tests/test_worktree.py`: added unit coverage for `WorktreeManager.reset()`.
- `tests/test_cli.py`: updated `orch init` coverage to verify the scaffolded workflow comment matches the authoritative-contract wording.

## Change summary

- Retry flow now preserves the full original prompt on every retry and only prepends retry-specific error guidance.
- Execution retries now start from a clean step baseline by clearing any pending step result file and resetting the worktree before attempt 2+.
- `resume()` now re-enters paused approval gates instead of returning immediately; filed approval decisions are consumed on resume, and missing decisions re-pause the run.
- Workflow definition docs and comments now consistently state that `workflows/default.yaml` defines phase structure and default phase settings, while `aio.toml` supplies supported overrides.

## Tests added or updated

- Added `tests/test_prompts.py::test_retry_prompt_includes_original_context`.
- Added `tests/test_engine.py::test_invoke_with_retries_passes_full_prompt`.
- Updated `tests/test_engine.py::test_engine_retries_when_step_reports_failed_status`.
- Added `tests/test_engine.py::test_worktree_reset_before_retry`.
- Added `tests/test_engine.py::test_resume_paused_re_enters_gate`.
- Added `tests/test_engine.py::test_resume_paused_without_decision_re_pauses`.
- Added `tests/test_engine.py::test_reset_worktree_failure_raises_engine_error`.
- Added `tests/test_worktree.py::test_worktree_manager_reset`.
- Updated `tests/test_cli.py::test_init_scaffolds_repo_files`.
- Verified with `PYTHONPATH=src .venv-test/bin/python -m pytest tests/test_prompts.py tests/test_engine.py tests/test_worktree.py tests/test_cli.py` and the suite passed.

## Remaining risks

- The retry docs were updated across active and deferred prompt docs, but future prompt-template edits must keep the retry preamble contract aligned with `build_retry_prompt()`.
- Execution retry reset assumes the step baseline is the last committed worktree state; any future execution flow that intentionally leaves uncommitted state between attempts would need a new explicit contract.
- The focused test run covered the changed behaviors; the full repository test suite was not rerun in this task.
