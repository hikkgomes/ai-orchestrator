# Output Contracts

> **Design status: FROZEN** as of 2026-04-08.

Every artifact exchanged between workflow phases is a JSON file validated against a schema in `schemas/`. This document defines the contracts, their fields, and how they flow between phases.

---

## Artifact Flow

```
Phase 1 (Planning)     → plans/plan-<uuid>.json         [plan.schema.json]
Phase 3 (Execution)    → results/step-<n>-<uuid>.json   [step_result.schema.json]
Phase 4 (Review)       → reviews/review-<uuid>.json     [review.schema.json]
Phase 5 (Adjudication) → adjudications/adj-<uuid>.json  [adjudication.schema.json]
```

---

## Plan Contract (`plan.schema.json`)

Produced by the planner. Consumed by the executor and reviewer.

| Field | Type | Required | Description |
|---|---|---|---|
| `plan_id` | string (uuid) | yes | Unique identifier |
| `task` | string | yes | Original task description |
| `steps` | array of Step | yes | Ordered implementation steps |
| `steps[].step_number` | integer | yes | 1-indexed position |
| `steps[].description` | string | yes | What this step does |
| `steps[].files_to_read` | array of string | yes | Files to include in prompt context |
| `steps[].files_to_modify` | array of string | yes | Files this step will create or edit |
| `steps[].depends_on` | array of integer | yes | Step numbers that must complete first (empty = independent) |
| `steps[].estimated_complexity` | enum: low/medium/high | yes | Complexity hint for routing and timeouts |
| `reasoning` | string | yes | Explanation of decomposition strategy |

**Schema validation rules:**
- `steps` must have at least 1 element
- `step_number` values must be sequential starting from 1
- `depends_on` values must reference existing step numbers < current step
- File paths must not start with `..` or `/`

**Application-level validation (in `validator.py`):**
- No circular dependencies in the dependency graph
- All file paths are normalized and verified to stay within the repo root (reject paths containing `..` anywhere, e.g. `a/../../b`)
- `depends_on` references are valid

---

## Step Result Contract (`step_result.schema.json`)

Produced by the executor (one per plan step). Consumed by the reviewer.

| Field | Type | Required | Description |
|---|---|---|---|
| `step_number` | integer | yes | Which plan step this fulfills |
| `status` | enum: success/partial/failed | yes | Self-assessed outcome |
| `files_changed` | array of FileChange | yes | What files were modified |
| `files_changed[].path` | string | yes | Relative file path |
| `files_changed[].action` | enum: created/modified/deleted | yes | Type of change |
| `files_changed[].summary` | string | yes | One-line description of change |
| `summary` | string | yes | Overall summary of implementation |
| `issues` | array of string | no | Known issues or caveats |
| `test_commands` | array of string | no | Commands to verify this step |

**Schema validation rules:**
- `step_number` must match the step being executed
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
| `verdict` | enum: PASS/REWORK/REPLAN/FAIL | yes | Decision |
| `reasoning` | string | yes | Why this verdict was chosen |
| `rework_steps` | array of integer | no | Which steps need rework (if REWORK) |
| `rework_feedback` | string | no | Guidance for rework (if REWORK) |
| `replan_feedback` | string | no | Guidance for replanning (if REPLAN) |
| `failure_reason` | string | no | Why this is unrecoverable (if FAIL) |

**Validation rules:**
- If `verdict` is `REWORK`, `rework_steps` must be non-empty and `rework_feedback` must be present
- If `verdict` is `REPLAN`, `replan_feedback` must be present
- If `verdict` is `FAIL`, `failure_reason` must be present
- `rework_steps` values must reference valid step numbers from the plan

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
| `step_results` | array of string | References to completed step results |
| `review_id` | string or null | Reference to current review |
| `adjudication_id` | string or null | Reference to current adjudication |
| `rework_count` | integer | How many rework loops so far |
| `replan_count` | integer | How many replan loops so far |
| `retry_counts` | object | Per-step retry counts |
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
4. Up to `max_retries` attempts

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

This ensures uniqueness across retries and rework loops. Old artifacts from failed attempts are preserved for debugging (not overwritten).
