# Workflow

> **Design status: FROZEN** as of 2026-04-08.

## Workflow Phases

Every orchestrated run moves through these phases in order. Each phase is a distinct CLI invocation (fresh subprocess). All mutating phases operate within a single worktree branch per run.

`workflows/default.yaml` is the authoritative definition of this phase structure and its default phase-level settings. `aio.toml` overrides the supported routing, retry, timeout, and loop-limit values.

```
 ┌─────────┐     ┌──────────────┐     ┌──────────┐     ┌───────────┐     ┌────────────┐     ┌──────────────┐     ┌───────┐
 │ PLANNING │────▶│APPROVAL_PLAN │────▶│EXECUTING │────▶│ REVIEWING │────▶│ADJUDICATING│────▶│APPROVAL_MERGE│────▶│MERGING│──▶ DONE
 └─────────┘     └──────────────┘     └──────────┘     └───────────┘     └────────────┘     └──────────────┘     └───────┘
      │               │                │                 │                  │
      ▼               ▼                ▼                 ▼                  ▼
  plan.json       (human)        step_result.json   review.json      adjudication.json
```

---

## Phase 1: PLANNING

**Purpose:** Decompose a high-level task into an ordered list of implementation steps.

| Property | Value |
|---|---|
| Default CLI | `claude -p` |
| Config key | `routing.planner` |
| Input | User task description + repo file listing + any prior adjudication feedback |
| Output | `plans/plan-<uuid>.json` validated against `plan.schema.json` |
| Worktree | No (read-only phase) |
| Retries | Up to `max_retries` on schema/validation failure |

**Prompt construction:**

```
You are a software planning agent. Given the following task and repository context,
produce a JSON plan conforming to the schema below.

TASK:
{task_description}

REPOSITORY STRUCTURE:
{directory_tree}

KEY FILE CONTENTS:
{key_file_contents}

OUTPUT SCHEMA:
{plan.schema.json contents}

Respond with ONLY valid JSON. No markdown fences. No commentary.
```

**Repo context strategy:** Directory tree (truncated at depth 3) for orientation, plus full contents of key files (README, config, entry points). For large repos, the tree is truncated to fit within ~50K chars. The planner's `files_to_read` field guides subsequent phases.

---

## Phase 2: APPROVAL_PLAN

**Purpose:** Human reviews the generated plan before execution begins.

| Property | Value |
|---|---|
| Gate type | Manual approval |
| Configurable | `approval.require_plan_approval` (default: `true`) |
| Behavior | Engine writes `PAUSED` state, prints plan summary to terminal, waits |
| Resume | `aio approve <run-id> plan` or interactive prompt |

When rejected (`aio reject <run-id> plan --reason "..."`), the rejection reason is fed back into Phase 1 as additional context and planning re-runs.

**Skip behavior:** If `require_plan_approval = false`, this phase is skipped and the engine transitions directly to EXECUTING.

---

## Phase 3: EXECUTING

**Purpose:** Implement each step in the plan, sequentially, in a single worktree.

All steps execute in one worktree branch. Each step sees the filesystem state left by prior steps.

| Property | Value |
|---|---|
| Default CLI | `codex exec` |
| Config key | `routing.worker` |
| Input | Step description + relevant file contents (read from worktree) + plan context |
| Output | `results/step-<n>-<uuid>.json` validated against `step_result.schema.json` |
| Worktree | Yes (single worktree for the entire run, created on first step) |
| Retries | Up to `max_retries` per step |

**Worktree lifecycle:**

1. On first step: create worktree `git worktree add .ai-orchestrator/worktrees/run-<uuid> -b aio/run-<uuid>`. Record the base commit SHA.
2. All subsequent steps execute in the same worktree.
3. If a step attempt fails and is retried, the engine resets the worktree to the last committed step baseline before re-invoking the worker.

**Per-step sequence:**

1. Read relevant files from the worktree (using `files_to_read` from the plan)
2. Render prompt with step description, file contents, and output schema
3. Invoke CLI adapter with `working_dir` set to the worktree
4. Capture output: for Codex, check result file first, then stdout, then git-diff-only fallback
5. Validate result (schema + application-level)
6. Commit changes in the worktree: `git add -A && git commit -m "aio: step <n> — <description>"`
7. Write validated result to `results/`
8. Advance state

**Prompt construction (Codex):**

```
You are a software implementation agent. Implement the following step.

STEP:
{step_description}

CONTEXT (from plan):
{plan_context}

RELEVANT FILES:
{file_contents}

After making changes, write a JSON result file to the path:
{result_file_path}

The JSON must conform to this schema:
{step_result.schema.json contents}

Do not print the JSON to stdout. Write it to the file path above.
```

**Prompt construction (Claude):**

```
You are a software implementation agent. Implement the following step.

STEP:
{step_description}

CONTEXT (from plan):
{plan_context}

RELEVANT FILES:
{file_contents}

OUTPUT SCHEMA:
{step_result.schema.json contents}

Respond with ONLY valid JSON. No markdown fences. No commentary.
```

**Relevant file selection:** The plan specifies which files each step reads and modifies. The orchestrator reads those files from the worktree and includes them in the prompt. If total content exceeds 100K chars, files are prioritized by plan relevance and truncated.

---

## Phase 4: REVIEWING

**Purpose:** A second AI reviews the implementation for correctness, style, and completeness.

| Property | Value |
|---|---|
| Default CLI | `claude -p` |
| Config key | `routing.reviewer` |
| Input | Original task + plan + step results + git diff from worktree |
| Output | `reviews/review-<uuid>.json` validated against `review.schema.json` |
| Worktree | No (reads diffs, does not mutate) |
| Retries | Up to `max_retries` on schema failure |

**Prompt construction:**

```
You are a code review agent. Review the following implementation.

ORIGINAL TASK:
{task_description}

PLAN:
{plan_json}

IMPLEMENTATION DIFF:
{git_diff}

STEP RESULTS:
{step_results_json}

Produce a JSON review conforming to this schema:
{review.schema.json contents}

Respond with ONLY valid JSON. No markdown fences. No commentary.
```

The git diff is obtained by `git diff <base_commit>...aio/run-<uuid>`.

---

## Phase 5: ADJUDICATING

**Purpose:** Decide whether the implementation passes review or needs rework.

| Property | Value |
|---|---|
| Default CLI | `claude -p` |
| Config key | `routing.adjudicator` |
| Input | Review JSON + step results + original task |
| Output | `adjudications/adj-<uuid>.json` validated against `adjudication.schema.json` |
| Worktree | No |
| Retries | Up to `max_retries` on schema failure |

**Possible verdicts:**

- `PASS` — proceed to merge approval
- `REWORK` — re-execute specific steps with feedback (loops back to Phase 3). Reworked steps execute in the same worktree, seeing all prior changes.
- `REPLAN` — the plan itself is flawed; loop back to Phase 1 with feedback. The existing worktree is discarded and a new one is created.
- `FAIL` — unrecoverable; stop the run

**Loop limits:**

| Loop | Max iterations | Config key |
|---|---|---|
| Step retry (same step, schema/timeout failure) | 3 | `orchestrator.max_retries` |
| Rework loop (adjudication → re-execute) | 3 | `orchestrator.max_rework_loops` |
| Replan loop (adjudication → re-plan) | 2 | `orchestrator.max_replan_loops` |

When any loop limit is hit, the run transitions to `FAILED` with a summary of all attempts.

---

## Phase 6: APPROVAL_MERGE

**Purpose:** Human reviews final implementation before merge.

| Property | Value |
|---|---|
| Gate type | Manual approval |
| Configurable | `approval.require_merge_approval` (default: `true`) |
| Behavior | Engine writes `PAUSED` state, prints diff summary, waits |
| Resume | `aio approve <run-id> merge` |

On rejection, reason is fed to Phase 5 for re-adjudication.

**Skip behavior:** If `require_merge_approval = false`, this phase is skipped and the engine transitions directly to MERGING.

---

## Phase 7: MERGING

**Purpose:** Merge the worktree branch into the base branch.

**Pre-merge checks:**
1. Verify the working tree on the base branch is clean (no uncommitted changes). If dirty, transition to `FAILED` with message instructing user to commit or stash.
2. Verify the base commit SHA matches what was recorded at worktree creation. If the base branch has advanced, warn the user and require explicit `aio approve <run-id> merge --force` to proceed.

**Merge sequence:**
1. `git checkout <base_branch>`
2. `git merge --no-ff aio/run-<uuid> -m "aio: <task_summary>"`
3. If merge conflict: transition to `CONFLICT` state. User resolves, then `aio resume <run-id>`.
4. On success: clean up worktree `git worktree remove .ai-orchestrator/worktrees/run-<uuid>`
5. Clean up branch: `git branch -d aio/run-<uuid>`
6. Mark run as `DONE`

---

## Canonical State Machine

```
INIT ──▶ PLANNING ──▶ APPROVAL_PLAN ──▶ EXECUTING ──▶ REVIEWING ──▶ ADJUDICATING
                         │                                              │
                         │ (reject)                    ┌────────────────┤
                         ▼                             ▼                ▼
                      PLANNING                     EXECUTING         PLANNING
                      (with feedback)              (rework)          (replan)
                                                                        │
ADJUDICATING(PASS) ──▶ APPROVAL_MERGE ──▶ MERGING ──▶ DONE            │
                           │                  │                         │
                           │ (reject)         │ (conflict)              │
                           ▼                  ▼                         │
                        ADJUDICATING       CONFLICT ──(resolved)──▶ MERGING
                        (with feedback)

Any state ──▶ FAILED        (unrecoverable error or loop limit exceeded)
Any state ──▶ PAUSED        (approval gate reached)
Any state ──▶ BLOCKED_ON_CLI (vendor CLI needs interactive input / auth refresh)
```

**Resume semantics by state:**

| State | `aio resume` behavior |
|---|---|
| `PAUSED` | Re-enter at the approval gate |
| `FAILED` | Not resumable. User must `aio clean` and re-run. |
| `BLOCKED_ON_CLI` | Re-attempt the CLI invocation that was blocked |
| `CONFLICT` | Verify conflict is resolved, then continue merge |
| `EXECUTING` (crashed mid-step) | Re-run the current step from the beginning in the same worktree |
| Any other (crashed) | Re-enter at `current_phase` |

---

## Session Isolation

Every CLI invocation is a fresh subprocess:

1. **No transcript reuse** — each `claude -p` or `codex exec` call receives only the prompt constructed for that specific phase.
2. **Controlled environment** — adapters pass only `PATH`, `HOME`, `USER`, `LANG`, `TERM`, `GIT_DIR`, `GIT_WORK_TREE`, and explicitly allowlisted vars. Credential vars are stripped.
3. **No temp file reuse** — prompts are written to `prompts/step-<n>.md` for auditability (when enabled) but are not reused across steps.
4. **Vendor CLI local state** — the orchestrator does not sandbox the vendor CLI's home directory. Auth state, caches, and project metadata managed by the CLI persist between invocations. This is intentional: it lets auth and config work normally.

---

## Logging

| Log | Location | Retention |
|---|---|---|
| Orchestrator events | `logs/run-<uuid>.log` | Always retained |
| CLI stdout/stderr | `logs/claude-<uuid>.log` / `logs/codex-<uuid>.log` | Opt-in (`logging.retain_raw_output`) |
| Prompts | `prompts/step-<n>.md` | Opt-in (`logging.retain_prompts`) |

Logs are never deleted automatically. `aio clean` removes them for completed runs only.
