# Output Contracts

> **Design status: UPDATED** as of 2026-04-17.

Workflow phases exchange JSON artifacts validated against schemas in `schemas/` plus a small set of markdown artifacts with machine-readable frontmatter. This document defines the contracts, their fields, and how they flow between phases.

---

## Artifact Flow

```
Phase 1 (Scoping)      → scoping/scope-<run>.md              [YAML frontmatter]
Phase 2 (Planning)     → plans/plan-<prefix>-<hash>.md      [markdown]
Phase 4 (Execution)    → results/execution-<uuid>.json      [execution_result.schema.json]
Phase 5 (Review)       → reviews/review-<uuid>.json         [review.schema.json]
Review debate rounds   → reviews/debate-round-*.json        [debate_response.schema.json]
```

---

## Scoping Contract (`scope.md`)

Produced by the Claude/Codex scoping debate. Consumed by the planner and engine.

| Field | Type | Required | Description |
|---|---|---|---|
| `actionable` | boolean | yes | Whether the task can proceed |
| `normalized_task` | string | yes | Normalized task text |
| `complexity_tier` | enum: simple/moderate/complex/architectural/extramax | yes | Task-level routing tier |
| `key_files` | array of string | yes | Likely relevant files/areas |
| `context` | string | yes | Assumptions, constraints, or blocking context |

---

## Plan Contract (Markdown)

Produced by the planner. Consumed by the executor and reviewer.

Artifact shape:

1. YAML frontmatter:
   - `plan_id` (UUID, generated server-side by the orchestrator)
   - `task` (single-line task string)
2. Markdown body with these sections:
   - `## Approach`
   - `## Steps`
   - `## Key Files`

The orchestrator parses `## Key Files` to build execution context. Paths are
validated in application code before use:

- Absolute paths are rejected
- Paths containing `..` segments are rejected
- Duplicate file paths are de-duplicated

`schemas/plan.schema.json` is retained only for legacy compatibility with older
JSON plan artifacts and is not used for new markdown plans.

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

Produced by Claude and Codex during REVIEWING. Consumed by the engine.

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

## Debate Response Contract (`debate_response.schema.json`)

Produced by Claude final review-debate rounds. Consumed by the merged REVIEWING state machine.

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
| `review_id` | string or null | Reference to current review |
| `fix_iteration_count` | integer | How many incremental fix-planning cycles so far |
| `session_ids` | object | Vendor session IDs keyed by phase |
| `scope_md_ref` | string or null | Canonical scope markdown reference |
| `debate_state` | object or null | Review debate progress |
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

For JSON-output phases, every prompt sent to a CLI includes:
1. The full JSON schema for the expected output
2. An explicit instruction: "Respond with ONLY valid JSON. No markdown fences. No commentary."

Planning and fix-planning are exceptions: they request markdown plans and do
not embed `plan.schema.json`.

If the CLI returns non-JSON or schema-invalid JSON:
1. The raw output is logged (if `logging.retain_raw_output` is enabled)
2. A retry prompt is constructed that includes the validation error message and the full original prompt
3. The retry prompt prepends retry guidance to the original prompt so the fresh subprocess still receives the complete task context
4. The engine performs 1 initial invocation plus up to `max_retries` retries
5. On a successful retry, the retry counter for that phase or step is reset to `0`

---

## File Naming Convention

Most artifact files use this pattern:

```
<type>-<uuid>.json
```

Where:
- `type`: `step-<n>`, `review`, `debate-round`; `adj` is legacy
- `uuid`: first 8 characters of a UUIDv4

Example: `step-3-a1b2c3d4.json`

Plan artifacts are the exception:

```
plan-<run-prefix>-<hash>.md
```

This ensures uniqueness across retries and incremental fix cycles. Old artifacts from failed attempts are preserved for debugging (not overwritten).
