# Design Risks

> **Design status: FROZEN** as of 2026-04-08.

Risks inherent in the current architecture. Each entry notes what has been mitigated by the frozen design and what residual risk remains.

---

## R1: CLI Output Parsing Fragility

**Risk:** `claude -p` and `codex exec` may emit non-JSON content mixed with the JSON response.

**Mitigations applied:**
- Claude adapter: strict parse → lenient fallback (strip fences, find boundaries) → retry
- Codex adapter: result-file primary → stdout fallback → git-diff-only reconstruction (DD-5)
- Retries with explicit JSON-only instructions

**Residual risk:** CLI output format changes can still break parsing. The version-pinning strategy (DD-2) limits exposure to tested versions only.

---

## R2: Prompt Size vs. Context Window

**Risk:** Large repos produce prompts that exceed CLI context windows.

**Mitigations applied:** Directory tree (depth-limited) + key file contents for planning. Planner-specified `files_to_read` for execution. Truncation with priority ordering. 100K char limit.

**Residual risk:** No reliable way to know the effective context window behind the CLI. Lost context degrades output quality silently.

---

## R3: No Progress Feedback During Steps

**Risk:** Blocking CLI calls show no progress during long-running steps.

**Mitigations applied:** Spinner with elapsed time. Configurable timeouts per phase and complexity level.

**Residual risk:** Cannot distinguish "thinking" from "hung." The `BLOCKED_ON_CLI` heuristic (no output for extended period) partially addresses this but is imperfect.

---

## R4: AI Output Non-Determinism

**Risk:** Same prompt may produce different results. Rework loops may not converge.

**Mitigations applied:** Bounded loop limits (3 rework, 2 replan). Feedback from adjudication included in retry prompts.

**Residual risk:** Fundamental LLM limitation. Some tasks may exhaust loop limits without producing acceptable output.

---

## R5: CLI Version Drift

**Risk:** `claude` and `codex` CLIs update independently. Flags, output formats, or behavior may change.

**Mitigations applied:** Tested version range in `[cli_compat]` config. `aio doctor` enforces version range. Fail closed on untested versions (DD-2).

**Residual risk:** Users must update ai-orchestrator when they update their CLIs. Lag between CLI release and tested range update.

---

## R6: Vendor CLI Operational Risk

**Risk:** Auth expiry, rate limits, seat restrictions, pricing changes, enterprise policy changes.

**Mitigations applied:** `BLOCKED_ON_CLI` state with resumable recovery (DD-3). Classified error types for auth/quota failures.

**Residual risk:** The orchestrator cannot fix vendor-side issues. It can only detect and surface them. This is a core product dependency, not a solvable engineering problem.

---

## R7: Cross-Platform Subprocess Behavior (Windows)

**Risk:** Process termination, path lengths, file handle cleanup, shell quoting differ on Windows.

**Mitigations applied:** `filelock` for cross-platform locking (DD-16). `pathlib.Path` everywhere. Windows classified as experimental (DD-15). CI testing on Windows.

**Residual risk:** Incomplete Windows edge case coverage. Users may hit issues not caught in CI.

---

## R8: Secrets Leaking Through AI Context

**Risk:** Repository files included in prompts may contain secrets.

**Mitigations applied:** Best-effort regex scanning before prompt inclusion. Opt-in raw output/prompt retention (DD-7). Documentation of transcript exposure as first-order tradeoff.

**Residual risk:** Regex cannot catch all secret formats. Code sent to vendor APIs is outside the orchestrator's control.

---

## R9: Unbounded Disk Usage

**Risk:** Each run creates worktrees, logs, and artifact files.

**Mitigations applied:** Single worktree per run (reduced from per-step). Opt-in raw log retention. `aio clean` command.

**Residual risk:** Users who never run `aio clean`. Low impact.
