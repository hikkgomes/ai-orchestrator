# Feasibility Review

This product is directionally interesting but materially overclaims what a subscription CLI wrapper can guarantee. The current design depends on undocumented or weakly supported CLI behavior, mixes several brittle control loops, and has at least one workflow flaw that breaks correctness even if the adapters themselves work.

## Finding 1
**Classification:** fatal issue

- **Issue:** The execution/worktree design does not correctly support dependent steps.
- **Why it matters:** [docs/workflow.md](docs/workflow.md) says each step gets its own worktree branch and that branches are merged only at the end. That means step 2 either runs from the base branch and cannot see step 1 changes, or it must be manually rebased/chained onto step 1 output. The current design does not specify any chaining. A multi-step plan with dependencies is therefore structurally wrong, not just risky.
- **Affected files or design areas:** `docs/workflow.md`, `docs/architecture.md`, `docs/output-contracts.md`, worktree manager design.
- **Proposed fix:** Change execution to one mutable branch/worktree per run, or explicitly chain dependent steps by branching step `n` from the merged result of completed dependencies. Define the exact branch topology and recovery rules.

## Finding 2
**Classification:** fatal issue

- **Issue:** The product assumes the worker CLIs can always run to completion non-interactively, but the design has no answer for CLI-side approval prompts or follow-up questions.
- **Why it matters:** The orchestrator itself has human approval gates, but [AGENTS.md](AGENTS.md) and [docs/workflow.md](docs/workflow.md) also rely on `claude -p` and `codex exec` as black-box subprocesses. If a CLI pauses for approval, confirmation, login refresh, or a clarifying question, the orchestrator can only hang until timeout and then blindly retry. That creates a deadlock loop rather than a controlled state transition.
- **Affected files or design areas:** `AGENTS.md`, `docs/workflow.md`, timeout/retry design, approval manager design.
- **Proposed fix:** Add explicit capability detection for non-interactive mode, detect interactive/approval-required exits separately from generic failure, and introduce a dedicated `BLOCKED_ON_VENDOR_CLI` state with resumable recovery.

## Finding 3
**Classification:** fatal issue

- **Issue:** The clean-session guarantee is overstated and currently unsupported.
- **Why it matters:** [docs/workflow.md](docs/workflow.md), [docs/architecture.md](docs/architecture.md), and [CLAUDE.md](CLAUDE.md) claim every invocation starts cold with no transcript carry-over. But [AGENTS.md](AGENTS.md) explicitly preserves `HOME`, `TERM`, and git environment, which is enough for vendor CLIs to read persistent auth state, config, plugins, caches, previous local sessions, or project metadata outside the prompt. A fresh subprocess is not the same thing as a fresh model session.
- **Affected files or design areas:** `docs/workflow.md`, `docs/architecture.md`, `CLAUDE.md`, `AGENTS.md`, security model.
- **Proposed fix:** Downgrade the claim to “fresh subprocess invocation” unless the product can enforce isolated home/config directories per run. If true cleanliness matters, launch each CLI with a per-run home/config sandbox and document exactly what remains shared.

## Finding 4
**Classification:** fatal issue

- **Issue:** The adapter contracts treat vendor CLI behavior as stable product surface without any compatibility strategy.
- **Why it matters:** `claude -p`, `--output-format json`, the wrapper `result` field, ANSI behavior, `codex exec` mutating files directly, and stdout containing a final JSON object are all presented as dependable contracts in [AGENTS.md](AGENTS.md). The install story in [docs/install.md](docs/install.md) then says to use the “latest” CLI versions. That combination is not viable: latest-version subscription CLIs change behavior, flags, envelopes, and auth flows without compatibility guarantees comparable to APIs.
- **Affected files or design areas:** `AGENTS.md`, `docs/install.md`, `docs/architecture.md`, adapter design, `aio doctor`.
- **Proposed fix:** Pin and test against an explicit supported version matrix, add startup capability probes for each required flag/behavior, and fail closed when a CLI version does not match a known compatibility profile.

## Finding 5
**Classification:** strong concern

- **Issue:** The JSON reliability strategy is brittle, especially for `codex exec`.
- **Why it matters:** Backward scanning stdout for the “last valid JSON object” is vulnerable to false positives from code samples, diffs, logs, fenced content, or nested JSON emitted during execution. Even when a syntactically valid object is found, schema validation only proves shape, not that it describes the actual filesystem changes. The design is effectively trying to recover a machine protocol from a human-facing terminal transcript.
- **Affected files or design areas:** `AGENTS.md`, `docs/output-contracts.md`, `docs/architecture.md`, adapter parsing logic.
- **Proposed fix:** Prefer an out-of-band result file written to a known path in the worktree, or require a unique delimiter protocol that cannot collide with normal output. Treat stdout parsing as fallback, not primary control flow.

## Finding 6
**Classification:** strong concern

- **Issue:** Git merge safety is underspecified and unsafe for dirty repositories.
- **Why it matters:** [docs/workflow.md](docs/workflow.md) says merge does `git checkout <base_branch>` and then merges step branches. There is no clean-tree preflight, no handling for uncommitted user changes on the base checkout, no hook policy, no rebase/update step, and no definition of what happens when parallel branches touch the same files. A tool that promises safety cannot rely on the user repository being conveniently clean.
- **Affected files or design areas:** `docs/workflow.md`, `docs/architecture.md`, `docs/security.md`, worktree manager design.
- **Proposed fix:** Require a clean base branch before merge, record and verify the exact base commit used for all worktrees, disable or explicitly account for hooks, and define a conflict-handling path before any checkout or merge happens.

## Finding 7
**Classification:** strong concern

- **Issue:** Subscription CLI operational risk is treated like a solved dependency problem when it is actually a core product risk.
- **Why it matters:** This design depends on locally installed, pre-authenticated, vendor-controlled CLIs rather than stable APIs. That introduces device-specific login state, expiring sessions, seat limits, opaque rate limits, pricing changes, enterprise policy restrictions, and behavior drift. None of that is modeled in the state machine, install docs, or recovery logic.
- **Affected files or design areas:** `docs/install.md`, `docs/architecture.md`, `docs/security.md`, routing and doctor design.
- **Proposed fix:** Add explicit vendor-auth and quota failure classes, document that this product only supports a tested subset of subscription plans and operating systems, and treat CLI availability as an environmental dependency that can pause or abort runs deterministically.

## Finding 8
**Classification:** strong concern

- **Issue:** The state machine is not defined consistently enough to support trustworthy recovery.
- **Why it matters:** [docs/architecture.md](docs/architecture.md) lists `APPROVAL_RESULT`; [docs/workflow.md](docs/workflow.md) does not. [CLAUDE.md](CLAUDE.md) uses `APPROVAL_MERGE`; [docs/workflow.md](docs/workflow.md) calls it “Merge Approval.” `CONFLICT` appears in workflow prose but not the main state list. Recovery from “last valid checkpoint” is claimed in [docs/security.md](docs/security.md), yet only a single mutable state file is described. Inconsistent states make resume logic brittle and ambiguous.
- **Affected files or design areas:** `docs/architecture.md`, `docs/workflow.md`, `docs/output-contracts.md`, `CLAUDE.md`, state manager design.
- **Proposed fix:** Publish one canonical finite state machine with exact state names, allowed transitions, persisted fields, and resume semantics for every failure mode, including vendor CLI block, merge conflict, and orphaned worktree cleanup.

## Finding 9
**Classification:** moderate concern

- **Issue:** Portability and installability are overstated.
- **Why it matters:** [docs/install.md](docs/install.md) and [docs/architecture.md](docs/architecture.md) present Windows/macOS/Linux support as straightforward, but the design depends on locally authenticated third-party CLIs, git worktrees, file locking, signal-based timeout cleanup, and terminal behavior. Windows is especially hand-waved: `SIGTERM`/`SIGKILL` semantics, path length issues, open-handle worktree cleanup, and shell/environment differences are not actually specified.
- **Affected files or design areas:** `docs/install.md`, `docs/architecture.md`, `AGENTS.md`, timeout/process management design.
- **Proposed fix:** Reduce the support claim to a tested matrix, spell out platform-specific behaviors, and gate GA on integration tests that exercise actual CLI installs on each supported OS.

## Finding 10
**Classification:** moderate concern

- **Issue:** Prompt, log, and artifact handling still creates meaningful transcript leakage risk.
- **Why it matters:** [docs/security.md](docs/security.md) admits raw stdout, stderr, and prompts are logged. [docs/workflow.md](docs/workflow.md) stores exact prompts for auditability. Secret scanning is explicitly best-effort. Passing repo content into subscription CLIs means code and potentially sensitive context leave the local machine, while the local logs preserve another copy. The current write-up understates how much sensitive context this product centralizes.
- **Affected files or design areas:** `docs/security.md`, `docs/workflow.md`, `docs/architecture.md`, logging design, prompt builder design.
- **Proposed fix:** Make prompt and raw-output retention opt-in, add structured redaction before logging, separate high-sensitivity repos from normal mode, and document transcript exposure as a first-order tradeoff rather than a minor hygiene note.

## Finding 11
**Classification:** moderate concern

- **Issue:** Schema enforcement is not strong enough to support the guarantees being claimed.
- **Why it matters:** The schemas only check surface shape. They do not prove step ordering, dependency validity, circularity, semantic correctness, or correspondence between `files_changed` and actual diffs. The path regexes reject only leading `..` or `/`, so values like `a/../../b` still match. [docs/output-contracts.md](docs/output-contracts.md) also says prompts include examples from schema `examples` fields, but the provided schemas do not contain such examples.
- **Affected files or design areas:** `schemas/*.json`, `docs/output-contracts.md`, `docs/security.md`.
- **Proposed fix:** Move critical invariants into deterministic application validation, tighten path normalization checks, and remove claims that depend on schema features not actually present.

## Finding 12
**Classification:** suggestion

- **Issue:** The overall design is heavier than the reliability of its control surfaces justifies.
- **Why it matters:** Planning, execution, review, adjudication, multiple approval gates, retries, replans, reworks, and worktree orchestration create a large state space on top of two human-facing subscription CLIs. Complexity is compounding brittleness rather than absorbing it.
- **Affected files or design areas:** `docs/architecture.md`, `docs/workflow.md`, overall product scope.
- **Proposed fix:** Cut the first version down to a narrower contract: one planner, one executor, one optional human review gate, one worktree per run, and minimal resumability. Prove that loop first before adding review/adjudication automation.

NOT FEASIBLE AS DESIGNED
