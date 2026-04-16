# Output Contracts

> **Design status: UPDATED** as of 2026-04-09.

Workflow phases exchange JSON artifacts validated against schemas in `schemas/` plus a small set of markdown artifacts with machine-readable frontmatter. This document defines the contracts, their fields, and how they flow between phases.

---

## Artifact Flow

```
Phase 1 (Scoping)      → scoping/scope-<run>.md              [YAML frontmatter]
Phase 2 (Planning)     → plans/plan-<uuid>.json             [plan.schema.json]
Phase 4 (Feasibility)  → feasibility/feasibility-<uuid>.json [feasibility.schema.json]
Phase 5 (Execution)    → results/execution-<uuid>.json      [execution_result.schema.json]
Phase 6 (Review)       → reviews/review-<uuid>.json         [review.schema.json]
Phase 7 (Adjudication) → adjudications/adj-<uuid>.json      [adjudication.schema.json]
Phase 7 debate rounds  → adjudications/debate-round-*.json   [debate_response.schema.json]
```

---

## Scoping Contract (`scope.md`)

Produced by the Claude/Codex scoping debate. Consumed by the planner and engine.

| Field | Type | Required | Description |
|---|---|---|---|
| `actionable` | boolean | yes | Whether the task can proceed |
| `normalized_task` | string | yes | Normalized task text |
| `complexity_tier` | enum: simple/moderate/complex/architectural | yes | Task-level routing tier |
| `key_files` | array of string | yes | Likely relevant files/areas |
| `context` | string | yes | Assumptions, constraints, or blocking context |

---

## Plan Contract (`plan.schema.json`)

Produced by the planner. Consumed by the executor and reviewer.

| Field | Type | Required | Description |
|---|---|---|---|
| `plan_id` | string (uuid) | yes | Unique identifier |
| `task` | string | yes | Original task description |
| `approach` | string | yes | Reasoning, strategy, risks, and validation approach |
| `implementation_steps` | array of string | yes | Ordered natural-language implementation steps |
| `key_files` | array of string | yes | Flat list of likely relevant repository-relative paths |

**Schema validation rules:**
- `implementation_steps` must have at least 1 element
- File paths must not start with `..` or `/`

**Application-level validation (in `validator.py`):**
- All file paths are normalized and verified to stay within the repo root (reject paths containing `..` anywhere, e.g. `a/../../b`)

---

## Feasibility Contract (`feasibility.schema.json`)

Produced by the feasibility checker. Consumed by the engine before execution.

| Field | Type | Required | Description |
|---|---|---|---|
| `verdict` | enum: go/go_with_warnings/blocked | yes | Overall feasibility decision |
| `blocking_issues` | array | yes | Critical or warning issues |
| `blocking_issues[].severity` | enum: critical/warning | yes | Issue impact |
| `blocking_issues[].description` | string | yes | Problem summary |
| `blocking_issues[].suggestion` | string | no | Suggested operator action |
| `summary` | string | yes | High-level feasibility summary |

---

## Execution Result Contract (`execution_result.schema.json`)

Produced by the executor once for the full plan. Consumed by the reviewer.

| Field | Type | Required | Description |
|---|---|---|---|
| `status` | enum: success/partial/failed | yes | Self-assessed outcome |
| `files_changed` | array of FileChange | yes | What files were modified |
| `files_changed[].path` | string | yes | Relative file path |
| `files_changed[].action` | enum: created/modified/deleted | yes | Type of change |
| `files_changed[].summary` | string | yes | One-line description of change |
| `summary` | string | yes | Overall summary of implementation |
| `issues` | array of string | no | Known issues or caveats |
| `test_commands` | array of string | no | Commands to verify this step |

**Schema validation rules:**
- `files_changed` must have at least 1 element if `status` is `success`
- Each `path` must not start with `..` or `/`

**Application-level validation (in `validator.py`):**
- All paths normalized and confined to repo root
- For Codex results: `files_changed` is reconstructed from `git diff` in the worktree, not from CLI output. The AI-provided `files_changed` is metadata only.

---

## Review Contract (`review.schema.json`)

Produced by the reviewer. Consumed by the adjudicator.

| Field | Type | Required | Description |
|---|---|---|---|
| `review_id` | string (uuid) | yes | Unique identifier |
| `verdict` | enum: approve/request_changes/reject | yes | Overall verdict |
| `score` | integer (1-10) | yes | Quality score |
| `findings` | array of Finding | yes | Specific observations |
| `findings[].severity` | enum: critical/major/minor/info | yes | Impact level |
| `findings[].file` | string | no | Which file (if applicable) |
| `findings[].line` | integer | no | Which line (if applicable) |
| `findings[].description` | string | yes | What was found |
| `findings[].suggestion` | string | no | How to fix |
| `summary` | string | yes | Overall review narrative |
| `blocks_merge` | boolean | yes | Whether this review blocks merge |

**Validation rules:**
- `score` must be 1-10
- If `verdict` is `reject`, `blocks_merge` must be `true`
- If `verdict` is `reject` or `request_changes`, at least one finding with severity `critical` or `major` must exist

---

## Adjudication Contract (`adjudication.schema.json`)

Produced by the adjudicator. Consumed by the orchestrator engine.

| Field | Type | Required | Description |
|---|---|---|---|
| `adjudication_id` | string (uuid) | yes | Unique identifier |
| `verdict` | enum: PASS/REWORK/REPLAN/FAIL | yes | Initial Codex decision; non-PASS enters fix/debate handling |
| `reasoning` | string | yes | Why this verdict was chosen |
| `rework_steps` | array of integer | no | Legacy step numbers for older artifacts |
| `rework_feedback` | string | no | Guidance for rework (if REWORK) |
| `replan_feedback` | string | no | Guidance for replanning (if REPLAN) |
| `failure_reason` | string | no | Why this is unrecoverable (if FAIL) |

**Validation rules:**
- If `verdict` is `REWORK`, `rework_feedback` must be present
- If `verdict` is `REPLAN`, `replan_feedback` must be present
- If `verdict` is `FAIL`, `failure_reason` must be present

---

## Debate Response Contract (`debate_response.schema.json`)

Produced by Claude/Codex rebuttal rounds. Consumed by the adjudication debate state machine.

| Field | Type | Required | Description |
|---|---|---|---|
| `position` | enum: issues_confirmed/issues_dismissed/issues_accepted | yes | Whether the actor believes fixes are required |
| `reasoning` | string | yes | Argument for the position |
| `issues` | array of objects | yes | Issues accepted or confirmed by this round |

---

## Orchestrator State Contract

Internal use only — not produced by AI. Stored in `state/run-<uuid>.json`.

| Field | Type | Description |
|---|---|---|
| `run_id` | string (uuid) | Unique run identifier |
| `task` | string | Original task |
| `status` | enum (see canonical states) | Current workflow state |
| `current_phase` | string | Which phase is active |
| `plan_id` | string or null | Reference to current plan |
| `normalized_task` | string or null | Scoping-normalized task |
| `complexity_tier` | string or null | Scoping-derived routing tier |
| `step_results` | array of string | References to execution result artifacts (legacy field name) |
| `feasibility_id` | string or null | Reference to current feasibility result |
| `review_id` | string or null | Reference to current review |
| `adjudication_id` | string or null | Reference to current adjudication |
| `fix_iteration_count` | integer | How many incremental fix-planning cycles so far |
| `feasibility_replan_count` | integer | How many feasibility-driven replans so far |
| `session_ids` | object | Vendor session IDs keyed by phase |
| `scope_md_ref` | string or null | Canonical scope markdown reference |
| `debate_state` | object or null | Adjudication debate progress |
| `retry_counts` | object | Per-phase retry counts, plus legacy per-step keys |
| `created_at` | string (ISO 8601) | When the run started |
| `updated_at` | string (ISO 8601) | Last state change |
| `error` | string or null | Error message if FAILED |
| `base_commit` | string | SHA of the base commit when worktree was created |
| `worktree_path` | string or null | Path to the run's worktree |
| `worktree_branch` | string or null | Branch name of the run's worktree |

This state file is the single source of truth for resumability. On `aio resume`, the engine reads this file and re-enters the state machine at `current_phase`.

## Local Metadata Store

Internal use only — stored in `.ai-orchestrator/metadata.sqlite3`.

- `runs` mirrors the latest persisted `RunState` fields for local querying.
- `invocations` records adapter execution metadata such as CLI name, command, working directory, timeout, exit code, output source, and typed step-result fields when available.

---

## Prompt-to-Schema Enforcement

Every prompt sent to a CLI includes:
1. The full JSON schema for the expected output
2. An explicit instruction: "Respond with ONLY valid JSON. No markdown fences. No commentary."

If the CLI returns non-JSON or schema-invalid JSON:
1. The raw output is logged (if `logging.retain_raw_output` is enabled)
2. A retry prompt is constructed that includes the validation error message and the full original prompt
3. The retry prompt prepends retry guidance to the original prompt so the fresh subprocess still receives the complete task context
4. The engine performs 1 initial invocation plus up to `max_retries` retries
5. On a successful retry, the retry counter for that phase or step is reset to `0`

---

## File Naming Convention

All artifact files use this pattern:

```
<type>-<uuid>.json
```

Where:
- `type`: `plan`, `step-<n>`, `review`, `adj`
- `uuid`: first 8 characters of a UUIDv4

Example: `step-3-a1b2c3d4.json`

This ensures uniqueness across retries and incremental fix cycles. Old artifacts from failed attempts are preserved for debugging (not overwritten).
