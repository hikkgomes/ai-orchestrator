# Unresolved Decisions

> **Design status: FROZEN** as of 2026-04-08.
>
> Most decisions from the original list have been resolved and recorded in `docs/design-decisions.md`. Only truly unresolved items remain here.

---

## UD-1: Exact Claude CLI flags for reasoning effort

**Context:** The design specifies per-phase reasoning effort (high/medium) passed to `claude -p`. The exact flag name (`--reasoning-effort`, `--thinking`, or similar) needs to be confirmed against the tested CLI version.

**Current approach:** The adapter will attempt to pass the configured reasoning effort flag. If the CLI does not recognize the flag (non-zero exit with flag-related error), the adapter silently omits it and retries without the flag. This is a graceful degradation path, not a hard dependency.

**Resolves during:** Phase 3 (Claude CLI adapter implementation).

---

## UD-2: Codex exec result file compliance

**Context:** The design instructs Codex to write a JSON result file to a known path. Whether `codex exec` reliably follows this instruction needs to be tested empirically.

**Current approach:** Three-tier fallback: result file → stdout parsing → git-diff-only reconstruction. If result file compliance is low, the git-diff-only path becomes the effective primary strategy, which is acceptable but loses AI-provided metadata.

**Resolves during:** Phase 4 (Codex CLI adapter implementation).

---

## UD-3: Tested CLI version ranges

**Context:** The `[cli_compat]` config section needs concrete version numbers. These can only be determined by running integration tests against actual CLI versions.

**Current approach:** Leave `claude_min_version` and `codex_min_version` empty during initial development. Set them after Phase 11 (tests) based on the versions available in CI.

**Resolves during:** Phase 10-11 (install/distribution and tests).

---

## UD-4: Interactive prompt detection heuristics

**Context:** Detecting that a vendor CLI is waiting for interactive input (auth, approval, clarification) vs. simply taking a long time is heuristic. The exact detection strategy (exit code patterns, stderr patterns, timeout-with-no-output) needs to be tuned against real CLI behavior.

**Current approach:** Start with timeout-with-no-output detection (if the subprocess produces no stdout/stderr for N seconds, assume it's waiting for input). Refine based on observed exit codes and stderr patterns during integration testing.

**Resolves during:** Phase 3-4 (adapter implementation) and Phase 11 (integration tests).
