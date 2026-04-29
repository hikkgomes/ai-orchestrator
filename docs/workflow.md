# Workflow

> **Design status: UPDATED** as of 2026-04-17.

## Workflow Phases

Every orchestrated run moves through these phases in order. Each phase is a distinct CLI subprocess invocation. Claude phases can share one unified `claude_main` session across scoping, planning, and reviewing when enabled. Review contains its own bounded Claude/Codex debate. All mutating phases operate within a single preserved worktree branch per run.

`workflows/default.yaml` is the authoritative definition of this phase structure and its default phase-level settings. `aio.toml` overrides supported routing, retry, session, debate, and watchdog values.

## Modes

`orch run <task>` and `orch new <task>` use the default full pipeline. The CLI also exposes mode-specific entrypoints:

| Mode | Command | Behavior |
|---|---|---|
| Default | `orch run <task>` | Full scoping, planning, approval, execution, review, merge handoff |
| Analysis | `orch analysis <task>` | Parallel Claude/Codex analysis plus saved debate outside the FSM |
| Quick Execute | `orch execute <task|plan.json>` | Starts at execution; `--no-review` jumps from execution to merge handoff |
| Review | `orch review [task]` | Starts at review against current uncommitted changes |
| Autonomous | `orch auto <task>` | Full pipeline with approval gates disabled and a configurable fix-loop limit |

Interactive shell mode tracks the current mode and cycles modes with Shift+Tab. The prompt and bottom toolbar show the active mode and key settings.

```
INIT -> SCOPING(debate) -> PLANNING(session) -> APPROVAL_PLAN -> EXECUTING
          ^                    ^                    |
          |                    |                    +-- request changes
          |                    +---- incremental fixes <---- REVIEWING(debate)
          |
          +---- reject/update task while paused at SCOPING

REVIEWING(pass) -> MERGING -> DONE
APPROVAL_PLAN -> TERMINATED on reject and terminate
```

---

## Phase 1: SCOPING

**Purpose:** Normalize the raw task and classify a task-level `complexity_tier` before planning.

| Property | Value |
|---|---|
| Default CLI | `claude -p` and `codex exec` |
| Config key | `routing.scoper` plus built-in cross-model debate |
| Input | Raw task + repo summary + shallow directory tree |
| Output | `scoping/scope-<run>.md` with YAML frontmatter |
| Worktree | No |
| Rounds | Fixed debate flow, 3-6 prompts with early exit |

Rounds 1 and 2 run in parallel. Claude Sonnet/medium writes canonical `scope.md`; Codex 5.4/medium writes `codex-scope.md`. Codex then compares both scopes and either agrees or writes reasoning. If needed, Claude Sonnet/high responds, Codex 5.4/xhigh makes a final case, and Claude Opus/max makes the final scope decision. Codex never edits the canonical file.

If the final scope is not actionable, the run pauses at the `SCOPING` gate. The operator can approve to continue anyway or reject with a replacement task, which re-runs scoping.

---

## Phase 2: PLANNING

**Purpose:** Decompose a high-level task into an ordered list of implementation steps.

| Property | Value |
|---|---|
| Default CLI | `claude -p` |
| Config key | `routing.planner` |
| Input | User task description + `scope.md` + any operator/review feedback |
| Output | `plans/plan-<run-prefix>-<hash>.md` |
| Worktree | No (read-only phase) |
| Retries | Up to `max_retries` on schema/validation failure |
| Session | Unified Claude session (`claude --resume <session-id>`) shared with scoping/review when enabled |

**Prompt construction:**

```
Plan for the implementation of the task below:
{task_description}

SCOPE:
{scope_md}
```

**Repo context strategy:** Planner explores the repository dynamically with agentic tools instead of relying on a pre-rendered context dump.

**Timeout default:** Planning and reviewing use an extended default timeout of 5400 seconds (90 minutes) to allow tool-driven exploration. Other phases keep the global `orchestrator.watchdog_timeout` unless explicitly overridden.

---

## Phase 3: APPROVAL_PLAN

**Purpose:** Human reviews the generated plan before execution begins.

| Property | Value |
|---|---|
| Gate type | Manual approval |
| Configurable | `approval.require_plan_approval` (default: `true`) |
| Behavior | Engine writes `PAUSED` state, prints plan summary to terminal, waits |
| Resume | `orch approve <run-id> plan` or interactive prompt |

Plan approval has three outcomes:

- `orch approve <run-id> plan` proceeds.
- `orch reject <run-id> plan --reason "..."` requests changes and resumes the same planning session with the feedback.
- `orch reject <run-id> plan --full --reason "..."` writes execution history and terminates the run.

**Skip behavior:** If `require_plan_approval = false`, this phase is skipped and the engine transitions directly to EXECUTING.

---

## Phase 4: EXECUTING

**Purpose:** Implement the full natural plan in one Codex or Claude execution session, in a single worktree or in-place across a workspace.

| Property | Value |
|---|---|
| Default CLI | `codex exec` |
| Config key | `routing.worker` |
| Input | Full plan text + result file path + output schema |
| Output | `results/execution-<uuid>.json` validated against `execution_result.schema.json` |
| Worktree | Yes (single worktree for the entire run, created on execution entry) |
| Retries | Up to `max_retries` for the full execution session |

**Worktree lifecycle:**

1. On execution entry: create worktree `git worktree add .ai-orchestrator/worktrees/run-<uuid> -b aio/run-<uuid>`. Record the base commit SHA.
2. Codex executes the full plan in one session and leaves changes uncommitted for review.
3. The engine commits outstanding changes after a successful execution result.
4. If an execution attempt fails and is retried, the engine resets the worktree before re-invoking the worker.

**Execution sequence:**

1. Resolve the pending execution result path
2. Render prompt with the full plan, result file path, and output schema
3. Invoke CLI adapter with `working_dir` set to the worktree
4. Capture output: for Codex, check result file first, then stdout, then git-diff-only fallback
5. Validate result (schema + application-level)
6. Commit outstanding changes in the worktree using the run task summary
7. Write validated result to `results/`
8. Advance to review

**Prompt construction:**

```
{plan_text}

FULLY IMPLEMENT THE PLAN ABOVE
- Do not commit or push any changes yet. Leave them for reviewing.
- Update the documentation accordingly if needed.
- If no changes are needed, explain that in the result summary.

After making changes, write your result JSON to:
{result_file_path}

The JSON must conform to this schema:
{execution_result.schema.json contents}

If you cannot write the file, respond with ONLY the raw JSON.
```

**Repo context strategy:** The executor inspects the worktree directly. The prompt does not include `file_contents` or workspace directory trees.

---

## Phase 5: REVIEWING

**Purpose:** Review the implementation and decide whether it can merge or needs incremental fixes.

| Property | Value |
|---|---|
| Default CLI | `claude -p` for initial review, then `codex exec` for cross-check |
| Config key | `routing.reviewer` for Claude review defaults; Codex review uses built-in routing |
| Input | Original task + `scope.md` + plan + execution results + git diff from worktree |
| Output | `reviews/review-<uuid>.json` plus debate rounds when needed |
| Worktree | Uses the implementation worktree as working directory for review context |
| Retries | Up to `max_retries` on schema failure |
| Session | Unified Claude session resumes from scoping/planning when enabled |

**Prompt construction:**

```
Review the plan implementation.

IMPLEMENTATION DIFF:
{git_diff}

EXECUTION RESULTS:
{step_results_json}

Produce a JSON review conforming to this schema:
{review.schema.json contents}

Codex is going to review your work afterwards.
```

The git diff is obtained by `git diff <base_commit>...aio/run-<uuid>`.

Review routing is direct:
- If both Claude and Codex pass the implementation, the run advances to MERGING.
- If both find matching blocking issues, the run returns to PLANNING for an incremental fix plan.
- If they disagree, Codex's review serves as the pushback and Claude Opus/max makes one final decision. There is no user tiebreaker gate.

Fix cycles never discard the worktree. A return to planning means a new incremental plan that sees the original task, `scope.md`, existing execution results, current diff, review issues, and debate transcript.

---

## Phase 6: MERGING

**Purpose:** Hand off the final implementation without committing on the user's behalf.

### Single-repo mode

**Pre-merge checks:**
1. Verify the base working tree is clean.
2. Verify the base branch still points at the recorded base commit.

**Handoff sequence:**
1. `git checkout <base_branch>`
2. `git merge --squash aio/run-<uuid>`
3. Remove the worktree and delete its branch.
4. Leave the squashed result as staged changes in the main working tree.
5. Print suggested `git status`, `git diff --cached`, `git commit`, and `git push` commands.
6. Mark the run as `DONE`

### Workspace mode

The engine does not create worktrees. It inspects each configured repo in place, leaves the file changes untouched, and prints per-repo `git add .`, `git commit`, and `git push` suggestions.

---

## Canonical State Machine

```
INIT ──▶ SCOPING ──▶ PLANNING ──▶ APPROVAL_PLAN ──▶ EXECUTING
              (debate)     │   ▲          │                  │
                           │   │          │ (reject and terminate)    ▼
                           │   │          ▼              REVIEWING
                           │   │       TERMINATED            │
                           │   └── (request changes) ───┐   │
                           │                            └───┘
                           └── (fix needed, preserved worktree)
                                                              │ PASS
                                                              ▼
                                                           MERGING ──▶ DONE

Any state ──▶ FAILED         (unrecoverable error)
Any state ──▶ TERMINATED     (user reject and terminate)
Any state ──▶ PAUSED         (approval gate)
Any state ──▶ BLOCKED_ON_CLI (vendor CLI needs interactive input / auth refresh)
```

**Resume semantics by state:**

| State | `orch resume` behavior |
|---|---|
| `PAUSED` | Re-enter at the approval gate |
| `FAILED` | Not resumable. User must `orch clean` and re-run. |
| `TERMINATED` | Not resumable. User chose to end the run. |
| `BLOCKED_ON_CLI` | Re-attempt the CLI invocation that was blocked |
| `CONFLICT` | Verify conflict is resolved, then continue merge |
| `EXECUTING` (crashed mid-session) | Re-run the full execution session from the beginning in the same worktree |
| Any other (crashed) | Re-enter at `current_phase` |

---

## Workspace Mode

A workspace root is a directory without its own `.git/` that contains one or more git repos as subdirectories.

- Detection: use `[workspace] repos = [...]` from `aio.toml`, or auto-detect git subdirectories when the current directory is not a repo.
- Working directory: all AI phases run from the workspace root.
- Execution: no orchestrator-managed worktrees; the worker runs in place across configured repos.
- Retry/reset: execution retries reset the current execution baseline. Replan and fix cycles preserve existing worktree changes.
- Review input: execution results may include `workspace_diffs`, and the review prompt aggregates those per-repo diffs.
- Completion: `MERGING` prints per-repo handoff commands instead of changing git history.

---

## Session Continuity

CLI invocations are still isolated subprocesses, but Claude can intentionally
resume one unified session across phases:

1. **Unified Claude session** — scoping starts a Claude session and stores it as `session_ids["claude_main"]`. Planning and reviewing resume that same session when enabled.
2. **Fix loops keep continuity** — when review sends work back to planning, `claude_main` is preserved so replanning keeps scoping context, previous plan context, and review feedback.
3. **Compaction behavior** — long sessions rely on vendor-side auto-compaction; recent review/fix context remains available when the session is resumed.
4. **Codex thread continuity** — Codex scoping rounds resume the captured `scoping_codex` thread when available. Codex review and execution calls use fresh subprocesses.
5. **Controlled environment** — adapters pass only `PATH`, `HOME`, `USER`, `LANG`, `TERM`, `GIT_DIR`, `GIT_WORK_TREE`, and explicitly allowlisted vars. Credential vars are stripped.
6. **Vendor CLI local state** — the orchestrator does not sandbox the vendor CLI's home directory. Auth state, caches, and project metadata managed by the CLI persist between invocations.

---

## Resuming Failed Runs

`FAILED` runs can be resumed with `orch resume <run-id>`:

- `SCOPING` and `PLANNING` failures resume at the failed phase.
- `EXECUTING`, `REVIEWING`, and `MERGING` failures resume at the failed phase when the managed worktree still exists.
- If the managed worktree was discarded or is missing, resume falls back to `EXECUTING` so the implementation can be recreated from the saved plan.
- Review sub-phases are preserved: a failure after Claude review resumes at Codex review, and a failure after Codex review resumes at the final tiebreaker instead of repeating completed review work.
- Retry counters for the resumed phase are cleared before re-entry so the resumed attempt receives a fresh retry budget.

---

## Logging

| Log | Location | Retention |
|---|---|---|
| Orchestrator events | `logs/run-<uuid>.log` | Always retained |
| CLI stdout/stderr | `logs/claude-<uuid>.log` / `logs/codex-<uuid>.log` | Opt-in (`logging.retain_raw_output`) |
| Prompts | `prompts/<phase>-<run>.md` | Opt-in (`logging.retain_prompts`) |

Logs are never deleted automatically. `orch clean` removes them for completed runs only.
