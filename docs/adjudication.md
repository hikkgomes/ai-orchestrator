# Review Adjudication

> Adjudicator: Codex
> Date: 2026-04-09
> Scope: `docs/review.md`

## Blocking

### B-1
- Decision: partial accept
- Rationale: The finding is correct, but adding new engine phases would expand the frozen workflow. The lower-risk fix is to defer the unused prompt drafts explicitly.
- Scope impact: Documentation only.
- Implementation action: Moved `define.md` and `feasibility.md` to `docs/prompts/deferred/` and updated `AGENTS.md` and `CLAUDE.md` so only implemented phases appear in the active prompt library.

### B-2
- Decision: partial accept
- Rationale: The finalize prompt is documented but not wired into the engine. Deferring the prompt draft is lower risk than adding a new post-merge phase.
- Scope impact: Documentation only.
- Implementation action: Moved `finalize.md` to `docs/prompts/deferred/` and updated prompt-library references in `AGENTS.md` and `CLAUDE.md`.

### B-3
- Decision: accept
- Rationale: The deprecation and timestamp inconsistency are real and easy to correct without changing behavior elsewhere.
- Scope impact: `RunState` timestamp defaults only.
- Implementation action: Switched `created_at` and `updated_at` defaults to `datetime.now(timezone.utc).isoformat()` and added coverage for timezone-aware timestamps.

## Important

### I-1
- Decision: accept
- Rationale: The adapter contract requires graceful timeout handling on Unix-like systems, and the previous `subprocess.run(..., timeout=...)` behavior did not provide it.
- Scope impact: Adapter subprocess execution path and related documentation wording.
- Implementation action: Added a shared subprocess runner that uses `Popen`, sends `terminate()`, waits for a grace period, and only then kills if needed. Updated docs that previously claimed `subprocess.run()` specifically.

### I-2
- Decision: accept
- Rationale: A validated execution result with `status = "failed"` must not be treated as a successful step completion.
- Scope impact: Execution retry behavior only.
- Implementation action: Enforced `status != "failed"` inside the execution retry loop so failed step results now trigger `StepFailure` and retry.

### I-3
- Decision: accept
- Rationale: The Codex adapter should remain substitutable through the shared adapter interface instead of requiring a special keyword argument.
- Scope impact: Codex adapter invocation and engine call site.
- Implementation action: Extended the shared adapter interface with an optional `step_number` keyword, updated both adapters to match it, and kept prompt-path extraction as a Codex fallback.

### I-4
- Decision: accept
- Rationale: `REWORK` adjudications must reference real plan steps to avoid runtime lookup failures.
- Scope impact: Adjudication validation only.
- Implementation action: Extended adjudication validation with `plan_step_numbers` and had the engine validate adjudication results against the current plan before accepting them.

### I-5
- Decision: accept
- Rationale: Review prompts were bypassing the documented secret-scanning guarantee by embedding raw git diffs.
- Scope impact: Review prompt input sanitization only.
- Implementation action: Added diff redaction for secret-like inline content and secret-bearing `.env*` diff hunks before constructing the review prompt.

### I-6
- Decision: accept
- Rationale: The YAML scalar parser should not silently misclassify basic negative integers or simple floats.
- Scope impact: Workflow-definition parsing only.
- Implementation action: Updated `_parse_scalar` to recognize signed integers and simple floats, and added focused parser tests.

## Minor

### M-1
- Decision: accept
- Rationale: Public exports should not require underscore-prefixed names.
- Scope impact: UI module exports and CLI import only.
- Implementation action: Added `ACTIVE_STATES` and `TERMINAL_STATES` as the public exports, updated the CLI to import the public name, and left underscore aliases for compatibility.

### M-2
- Decision: accept
- Rationale: The missing annotation is a straightforward inconsistency in an otherwise typed module.
- Scope impact: Type hints only.
- Implementation action: Added `-> RunState` to `_drive_interactive_approvals`.

### M-3
- Decision: accept
- Rationale: The missing annotation is a straightforward inconsistency in an otherwise typed module.
- Scope impact: Type hints only.
- Implementation action: Added `-> RenderableType` to `_render_status`.

### M-4
- Decision: accept
- Rationale: The command naming mismatch is real, and `orch` should be presented as primary while retaining the alias note.
- Scope impact: Documentation only.
- Implementation action: Updated `CLAUDE.md` command references to use `orch` and added an explicit note that `aio` remains the compatibility alias.

### M-5
- Decision: accept
- Rationale: Treating every path containing `.env` as secret-bearing is too broad and can hide normal source files.
- Scope impact: Prompt file-context filtering only.
- Implementation action: Tightened `.env` detection to basenames starting with `.env` and added a regression test for `src/environment.py`.

### M-6
- Decision: accept
- Rationale: The compatibility alias is part of the packaged CLI contract and should have install-time coverage.
- Scope impact: Packaging smoke tests only.
- Implementation action: Extended the packaging smoke coverage to invoke both `orch --help` and `aio --help` when the install smoke test can run.

### M-7
- Decision: accept
- Rationale: Silently dropping unknown config keys hides common configuration mistakes.
- Scope impact: Config loading warnings only.
- Implementation action: Added warnings for unknown top-level and section keys while preserving the existing ignore-and-continue behavior.

## Optional

### O-1
- Decision: reject
- Rationale: A dedicated artifact test suite is useful but broader than the behavior fixes required by this review pass.
- Scope impact: None for this change set.
- Implementation action: None.

### O-2
- Decision: reject
- Rationale: Dedicated metadata-store tests are a worthwhile follow-up, but they are not required to resolve the accepted review findings.
- Scope impact: None for this change set.
- Implementation action: None.

### O-3
- Decision: reject
- Rationale: A full workflow-definition test suite is broader than the scoped parser fix. Targeted coverage was added for the accepted scalar-parsing bug instead.
- Scope impact: None beyond the targeted parser tests already added under I-6.
- Implementation action: None.

### O-4
- Decision: reject
- Rationale: Bootstrap edge-case coverage is outside the narrow review-driven fixes being applied here.
- Scope impact: None for this change set.
- Implementation action: None.

### O-5
- Decision: reject
- Rationale: The current implementation is functionally correct and the performance concern is low priority relative to the contract and correctness findings above.
- Scope impact: None for this change set.
- Implementation action: None.
