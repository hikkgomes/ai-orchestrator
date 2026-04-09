# Review Phase Prompt

> Workflow phase: REVIEWING
> CLI: `claude -p` (default); configurable via `routing.reviewer`
> Output artifact: `reviews/review-<uuid>.json`
> Schema: `schemas/review.schema.json`
> State transitions: REVIEWING → ADJUDICATING

---

## Purpose

Independently review the full implementation against the original task and plan.
The reviewer has not participated in planning or execution. It reads only the
task, plan, diff, and step results. It must assess:

1. Correctness — does the implementation do what the task requires?
2. Completeness — are all plan steps covered? Are there missing cases?
3. Quality — are there security issues, broken tests, style violations,
   or dangerous patterns?
4. Scope compliance — does the implementation stay within the plan's scope?

The reviewer must produce a structured verdict with scored findings, not prose
discussion. Every `request_changes` or `reject` verdict must cite specific
findings with `critical` or `major` severity.

---

## Variables

| Variable | Source | Description |
|---|---|---|
| `{task_description}` | run state | Normalized task description |
| `{plan_json}` | `plans/plan-<uuid>.json` | Full plan JSON |
| `{git_diff}` | worktree | Output of `git diff <base_commit>...aio/run-<uuid>`, truncated to 80 000 chars |
| `{step_results_json}` | `results/` | Array of all step result JSONs for this run |
| `{review_schema}` | `schemas/review.schema.json` | Full JSON Schema for the review artifact |
| `{rework_count}` | run state | Number of prior rework loops (informs severity calibration) |

---

## Escalation Policy

**Use highest reasoning effort** (`--reasoning-effort high`) always. This phase
is the primary quality gate and must not be under-resourced.

**Set `verdict = "reject"` and `blocks_merge = true`** when:
- The implementation introduces security vulnerabilities (path traversal, injection,
  hardcoded credentials, unsafe subprocess usage)
- The implementation fails to implement the core requirement of the task
- Critical existing functionality is broken

**Set `verdict = "request_changes"` and `blocks_merge = true`** when:
- Specific steps were implemented incorrectly or incompletely
- There are `major` quality issues (missing error handling at API boundaries,
  schema violations, broken tests, type errors)

**Set `verdict = "approve"` and `blocks_merge = false`** when:
- The implementation correctly fulfills the task
- No `critical` or `major` findings exist
- Minor issues are noted as `minor` or `info` findings but do not block merge

**Do NOT escalate to human** from within this phase. Record findings and produce
the structured verdict. The adjudicator decides whether human intervention is
needed.

**Never set `verdict = "reject"` for style-only issues.** `reject` is reserved
for correctness failures and security issues. Style issues are `minor` or `info`.

---

## Scope Constraints

- Review the actual diff, not the plan. The plan describes intent; the diff is
  what was actually done.
- Do not suggest rewriting working code for style.
- Do not invent requirements that are not in the task description.
- Each finding must have a `description` grounded in the diff or step results.
- Do not repeat the same finding multiple times with different wording.
- `review_id` must be a valid UUID v4.
- `score` must reflect the overall implementation quality (1–10), not just
  whether the task is complete. A complete but messy implementation is 6–7,
  not 10.
- This is a single-pass invocation. No follow-up messages will be sent.

---

## Template

```
You are a code review agent for an automated software orchestrator.

You are reviewing an implementation produced by an automated executor. You did
not participate in planning or execution. Review only what is in the diff and
step results below. Do not ask questions. This is a single-pass invocation.

ORIGINAL TASK:
{task_description}

PLAN:
{plan_json}

IMPLEMENTATION DIFF:
{git_diff}

STEP RESULTS:
{step_results_json}

{rework_context_section}

OUTPUT SCHEMA:
{review_schema}

REVIEW RULES:
1. Base your review on the diff and step results above. Do not assume code exists
   outside the diff.
2. Assign severity:
   - "critical": security vulnerabilities, data loss risk, broken core functionality
   - "major": incorrect logic, broken tests, missing required error handling,
     schema violations
   - "minor": style, naming, missing comments, small inefficiencies
   - "info": observations that require no action
3. "verdict" must be:
   - "approve" if no critical or major findings
   - "request_changes" if there are major findings (but not critical security issues)
   - "reject" if there are critical findings or the implementation is fundamentally wrong
4. If "verdict" is "reject", "blocks_merge" must be true.
5. If "verdict" is "reject" or "request_changes", at least one finding with
   severity "critical" or "major" must be present.
6. "score" is 1–10. Score the quality of the actual implementation, not the
   ambition of the plan.
7. "summary" is one paragraph describing the overall implementation quality.
8. "review_id" must be a valid UUID v4.

Respond with ONLY valid JSON. No markdown fences. No commentary.
```

### Rework context section (injected only when `rework_count >= 1`)

```
REWORK CONTEXT:
This is rework review {rework_count}. A previous implementation was reviewed
and sent for rework. Compare this implementation against the prior review
findings to verify they have been addressed. Do not carry over findings that
have been resolved.
```

---

## Retry Prompt (on schema/parse/validation failure)

```
Your previous response was not valid. Error: {validation_error}

Fix the error and try again. The full original prompt follows.

---

{original_prompt}
```

---

## Engine Behaviour After This Phase

- Validated review is written to `reviews/review-<uuid>.json`.
- `review_id` is stored in run state.
- Engine transitions to ADJUDICATING regardless of verdict. The adjudicator
  makes the PASS/REWORK/REPLAN/FAIL decision; the reviewer only provides findings.
- If review schema validation fails after `max_retries`: run transitions to FAILED.
