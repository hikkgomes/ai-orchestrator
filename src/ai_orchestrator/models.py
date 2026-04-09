"""Central Pydantic models for all workflow artifacts and orchestrator state.

All models map directly to the JSON schemas in schemas/ and the contracts
described in docs/output-contracts.md. Do not add fields that are not in the
frozen schema contracts.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class WorkflowStatus(str, Enum):
    """Canonical orchestrator run states (docs/workflow.md)."""

    INIT = "INIT"
    SCOPING = "SCOPING"
    PLANNING = "PLANNING"
    APPROVAL_PLAN = "APPROVAL_PLAN"
    FEASIBILITY = "FEASIBILITY"
    EXECUTING = "EXECUTING"
    REVIEWING = "REVIEWING"
    ADJUDICATING = "ADJUDICATING"
    MERGING = "MERGING"
    DONE = "DONE"
    FAILED = "FAILED"
    PAUSED = "PAUSED"
    BLOCKED_ON_CLI = "BLOCKED_ON_CLI"
    CONFLICT = "CONFLICT"


class Complexity(str, Enum):
    """Step complexity hint used for routing and timeout decisions."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ComplexityTier(str, Enum):
    """Repository-level task complexity tier resolved during scoping."""

    SIMPLE = "simple"
    MODERATE = "moderate"
    COMPLEX = "complex"
    ARCHITECTURAL = "architectural"


class StepStatus(str, Enum):
    """Self-assessed outcome of a plan step execution."""

    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


class FileAction(str, Enum):
    """Type of file change performed by a step."""

    CREATED = "created"
    MODIFIED = "modified"
    DELETED = "deleted"


class ReviewVerdict(str, Enum):
    """Overall verdict from the review phase."""

    APPROVE = "approve"
    REQUEST_CHANGES = "request_changes"
    REJECT = "reject"


class FindingSeverity(str, Enum):
    """Impact level of a review finding."""

    CRITICAL = "critical"
    MAJOR = "major"
    MINOR = "minor"
    INFO = "info"


class AdjudicationVerdict(str, Enum):
    """Decision from the adjudication phase."""

    PASS = "PASS"
    REWORK = "REWORK"
    REPLAN = "REPLAN"
    FAIL = "FAIL"


# ---------------------------------------------------------------------------
# Plan artifact (plan.schema.json)
# ---------------------------------------------------------------------------


class PlanStep(BaseModel):
    """A single step in a decomposed implementation plan."""

    step_number: int = Field(ge=1)
    description: str = Field(min_length=1)
    files_to_read: list[str] = Field(default_factory=list)
    files_to_modify: list[str] = Field(default_factory=list)
    depends_on: list[int] = Field(default_factory=list)
    estimated_complexity: Complexity


class Plan(BaseModel):
    """Decomposed implementation plan produced by the planning phase."""

    plan_id: str
    task: str = Field(min_length=1)
    steps: list[PlanStep] = Field(min_length=1)
    reasoning: str = Field(min_length=1)


# ---------------------------------------------------------------------------
# Step result artifact (step_result.schema.json)
# ---------------------------------------------------------------------------


class FileChange(BaseModel):
    """A single file change within a step result."""

    path: str
    action: FileAction
    summary: str = Field(min_length=1)


class StepResult(BaseModel):
    """Result of executing a single plan step."""

    step_number: int = Field(ge=1)
    status: StepStatus
    files_changed: list[FileChange] = Field(default_factory=list)
    summary: str = Field(min_length=1)
    issues: list[str] = Field(default_factory=list)
    test_commands: list[str] = Field(default_factory=list)
    workspace_diffs: dict[str, str] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Review artifact (review.schema.json)
# ---------------------------------------------------------------------------


class Finding(BaseModel):
    """A specific observation from the code review."""

    severity: FindingSeverity
    file: str | None = None
    line: int | None = Field(default=None, ge=1)
    description: str = Field(min_length=1)
    suggestion: str | None = None


class Review(BaseModel):
    """Code review of an implementation produced by the review phase."""

    review_id: str
    verdict: ReviewVerdict
    score: int = Field(ge=1, le=10)
    findings: list[Finding] = Field(default_factory=list)
    summary: str = Field(min_length=1)
    blocks_merge: bool


# ---------------------------------------------------------------------------
# Adjudication artifact (adjudication.schema.json)
# ---------------------------------------------------------------------------


class Adjudication(BaseModel):
    """Decision on whether an implementation passes review or needs rework."""

    adjudication_id: str
    verdict: AdjudicationVerdict
    reasoning: str = Field(min_length=1)
    rework_steps: list[int] | None = None
    rework_feedback: str | None = None
    replan_feedback: str | None = None
    failure_reason: str | None = None


# ---------------------------------------------------------------------------
# Orchestrator run state (docs/output-contracts.md — internal, not AI-produced)
# ---------------------------------------------------------------------------


class RunState(BaseModel):
    """Persisted orchestrator run state written to state/run-<uuid>.json.

    This is the single source of truth for resumability. All fields must be
    serialisable to JSON. Timestamps are stored as ISO 8601 strings.
    """

    run_id: str
    task: str
    status: WorkflowStatus = WorkflowStatus.INIT
    current_phase: str = "INIT"
    plan_id: str | None = None
    normalized_task: str | None = None
    complexity_tier: str | None = None
    step_results: list[str] = Field(default_factory=list)
    commit_commands: list[str] = Field(default_factory=list)
    review_id: str | None = None
    adjudication_id: str | None = None
    feasibility_id: str | None = None
    rework_count: int = 0
    replan_count: int = 0
    retry_counts: dict[str, int] = Field(default_factory=dict)
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    error: str | None = None
    base_commit: str = ""
    worktree_path: str | None = None
    worktree_branch: str | None = None
    is_workspace: bool = False
    workspace_repos: list[str] = Field(default_factory=list)

    model_config = {"use_enum_values": True}
