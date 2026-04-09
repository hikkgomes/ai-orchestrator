# Design Decisions

> **Design status: FROZEN** as of 2026-04-08.

This document records decisions made during design, including responses to the feasibility review. Each entry explains the decision, the alternatives considered, and why this choice was made.

---

## DD-1: Single worktree per run (not per step)

**Decision:** All mutating steps in a run execute sequentially in a single git worktree branch.

**Context:** The original design created a separate worktree branch per step. The feasibility review (Finding 1) correctly identified that dependent steps cannot see prior step changes under that model — step 2 would run from the base branch without step 1's output.

**Alternatives considered:**
1. Per-step worktrees with explicit chaining (branch step N from merged step N-1) — correct but complex, fragile merge ordering, hard to recover from mid-run failures
2. Single worktree per run, sequential execution — simple, each step sees all prior changes, one branch to merge

**Decision rationale:** Option 2 is simpler and correct. Parallel step execution (which would need multiple worktrees) is deferred to a future version and is not needed for v1.

---

## DD-2: Vendor CLI version pinning with fail-closed behavior

**Decision:** The orchestrator declares a tested CLI version range in config. `aio doctor` enforces it. `aio run` refuses to start if CLIs are outside the tested range.

**Context:** Feasibility Finding 4 correctly identified that depending on "latest" CLI versions with no compatibility strategy is not viable. CLI flags, output formats, and behavior can change between releases.

**Alternatives considered:**
1. Feature detection at runtime (probe each flag/behavior) — complex, slow, still fragile
2. Pin exact versions — too restrictive for users
3. Tested version range with startup check — pragmatic balance

**Decision rationale:** Option 3. The tested range is updated with each ai-orchestrator release after integration testing. Users outside the range get a clear error, not silent breakage.

---

## DD-3: BLOCKED_ON_CLI state for vendor CLI interactive/auth failures

**Decision:** When a vendor CLI hangs, prompts for auth, or exits with an auth-related error, the run transitions to `BLOCKED_ON_CLI` instead of retrying blindly.

**Context:** Feasibility Finding 2 identified that CLI-side approval prompts or auth refreshes would cause the orchestrator to hang until timeout and retry in a deadlock loop.

**Alternatives considered:**
1. Detect and suppress all interactive prompts — not possible without vendor CLI cooperation
2. Timeout and retry (original design) — creates deadlock loops
3. Classify the failure and transition to a resumable blocked state — correct

**Decision rationale:** Option 3. The adapter classifies exits: timeout with no output suggests interactive block; known auth-error exit codes or stderr patterns trigger `BLOCKED_ON_CLI`. The user fixes the issue externally and resumes.

---

## DD-4: Fresh subprocess, not fresh session

**Decision:** The "fresh context" guarantee means a fresh subprocess invocation per step. It does not mean the vendor CLI operates in a sandboxed environment.

**Context:** Feasibility Finding 3 correctly noted that vendor CLIs read persistent state from `HOME` (auth, caches, project metadata). A "fresh subprocess" is not the same as a "fresh model session."

**Alternatives considered:**
1. Launch each CLI with a per-run HOME sandbox — would break auth, impractical
2. Overstate isolation claims — misleading
3. Downgrade the claim to "fresh subprocess invocation" and document what persists — honest

**Decision rationale:** Option 3. The orchestrator guarantees no transcript carry-over between its invocations. Vendor CLI local state (auth, caches) persists intentionally.

---

## DD-5: Result file strategy for Codex adapter

**Decision:** The Codex adapter instructs the CLI to write a result JSON file to a known path in the worktree, rather than relying on stdout parsing as the primary strategy.

**Context:** Feasibility Finding 5 identified that backward-scanning stdout for JSON is fragile — false positives from code samples, diffs, and nested JSON are likely.

**Alternatives considered:**
1. Stdout parsing only — fragile, original design
2. Result file only — clean but may not work if Codex ignores the instruction
3. Result file primary, stdout fallback, git-diff-only last resort — layered resilience

**Decision rationale:** Option 3. The prompt instructs Codex to write a result file. If the file exists, it's used. If not, stdout is scanned. If that fails too, `files_changed` is reconstructed from `git diff` alone with default metadata. This provides graceful degradation.

---

## DD-6: Merge safety pre-checks

**Decision:** Merging requires a clean base branch working tree and verifies the base commit SHA has not changed since worktree creation.

**Context:** Feasibility Finding 6 identified that the merge sequence assumed a conveniently clean repository and had no handling for uncommitted changes, advanced base branches, or hook interactions.

**Alternatives considered:**
1. Stash-and-restore user changes automatically — risky, can lose work
2. Require clean state and abort otherwise — safe, predictable
3. Ignore dirty state — unsafe

**Decision rationale:** Option 2. The orchestrator checks for a clean working tree and matching base commit before merge. If the base branch has advanced, the user must explicitly approve. Git hooks are not disabled.

---

## DD-7: Opt-in log retention

**Decision:** Raw CLI output and prompt files are not retained by default. Only orchestrator event logs (state transitions, errors) are always kept.

**Context:** Feasibility Finding 10 identified that storing raw stdout, stderr, and prompts creates meaningful transcript leakage risk. The original design retained everything by default.

**Alternatives considered:**
1. Retain everything (original) — convenient for debugging, bad for security
2. Retain nothing — hard to debug
3. Opt-in retention with secure defaults — balanced

**Decision rationale:** Option 3. `logging.retain_raw_output` and `logging.retain_prompts` default to `false`. Users enable them when debugging.

---

## DD-8: Application-level validation supplements schemas

**Decision:** Critical invariants (path normalization, dependency graphs, file correspondence) are enforced in application code, not just JSON schemas.

**Context:** Feasibility Finding 11 correctly identified that schemas only check surface shape. Path regexes in schemas reject only leading `..` or `/`, so `a/../../b` passes. Dependency validity and step ordering can't be enforced by JSON Schema alone.

**Alternatives considered:**
1. More complex JSON Schema with custom keywords — hard to maintain, limited expressiveness
2. Schema for shape, application code for semantics — clean separation
3. Skip schema validation and do everything in application code — loses the self-documenting benefit

**Decision rationale:** Option 2. Schemas validate structure. `validator.py` validates semantics: full path normalization, dependency graph analysis, cross-field consistency.

---

## DD-9: Sequential execution only for v1

**Decision:** v1 supports sequential step execution only. Parallel execution is deferred.

**Context:** Feasibility Finding 12 suggested cutting scope. The original design supported parallel execution of independent steps, which adds merge complexity, error handling complexity, and requires multiple worktrees.

**Decision rationale:** Sequential execution in a single worktree is simpler, correct, and sufficient for v1. `depends_on` is retained in the plan schema for future use but is not acted on in v1 — all steps execute in order.

---

## DD-10: Keep review/adjudication loop in v1

**Decision:** The review → adjudication → rework/replan loop is retained in v1, contrary to Feasibility Finding 12's suggestion to cut it.

**Context:** Finding 12 suggested cutting to "one planner, one executor, one optional review gate." This would remove the automated quality feedback loop, which is the core differentiator of this tool over a simple script.

**Pushback:** The review/adjudication loop is not accidental complexity — it is the product. Without it, this is just a prompt runner. The loop is bounded by configurable limits (max 3 rework, max 2 replan), so it cannot cycle indefinitely. The complexity it adds is manageable within the single-worktree sequential model.

---

## DD-11: Config format is TOML

**Decision:** TOML via `tomllib` (stdlib in Python 3.11+).

**Alternatives:** YAML (whitespace-sensitive, security footguns), JSON (no comments, painful to edit).

**Decision rationale:** Standard for Python tooling. Zero external dependency.

---

## DD-12: State persistence is single JSON file

**Decision:** Single `state/run-<uuid>.json` file with atomic writes and `filelock`.

**Alternatives:** SQLite (heavier, unnecessary), directory-of-files (more complex).

**Decision rationale:** Simplest correct approach. Human-readable for debugging. Atomic rename prevents corruption.

---

## DD-13: Prompt templates via Python f-strings

**Decision:** Prompt templates use Python f-strings for v1.

**Alternatives:** Jinja2 (adds dependency), string.Template (limited).

**Decision rationale:** Prompt logic is straightforward variable substitution. No conditionals or loops needed. If complexity grows, Jinja2 can be added later.

---

## DD-14: Git-only repositories

**Decision:** ai-orchestrator requires git. Non-git repos are not supported.

**Decision rationale:** The tool depends fundamentally on git worktrees, branches, and diffs. Abstracting this for non-git repos would be a massive effort with no clear user demand.

---

## DD-15: Windows is experimental

**Decision:** Windows is tested in CI but classified as experimental, not primary.

**Context:** Feasibility Finding 9 correctly identified that Windows support is hand-waved. Process termination, path lengths, open-handle cleanup, and shell quoting all behave differently.

**Decision rationale:** Honest classification. Primary platforms are macOS and Linux. Windows is tested but users should expect edge cases.

---

## DD-16: filelock instead of fcntl/msvcrt

**Decision:** Use the `filelock` package for cross-platform file locking instead of platform-specific `fcntl`/`msvcrt`.

**Context:** The original design specified `fcntl` on POSIX and `msvcrt` on Windows, requiring platform-specific code paths.

**Decision rationale:** `filelock` provides a single API across platforms. It's a well-maintained, minimal dependency (Unlicense). Eliminates a platform-specific code path.

---

## DD-17: Reasoning effort is user-configurable with sensible defaults

**Decision:** Reasoning effort per phase is configurable in `aio.toml` with defaults: planner=high, worker=medium, reviewer=high, adjudicator=high.

**Context:** The exact flags supported by `claude -p` for reasoning effort need to be tested during implementation. If the flag is not available, the adapter silently omits it.

**Decision rationale:** Expose the knob but don't block on it. The adapter attempts to pass the flag; if the CLI doesn't support it, execution continues without it.
