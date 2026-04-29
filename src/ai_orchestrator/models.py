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
    EXECUTING = "EXECUTING"
    REVIEWING = "REVIEWING"
    MERGING = "MERGING"
    DONE = "DONE"
    FAILED = "FAILED"
    TERMINATED = "TERMINATED"
    PAUSED = "PAUSED"
    BLOCKED_ON_CLI = "BLOCKED_ON_CLI"
    CONFLICT = "CONFLICT"


class ComplexityTier(str, Enum):
    """Repository-level task complexity tier resolved during scoping."""

    SIMPLE = "simple"
    MODERATE = "moderate"
    COMPLEX = "complex"
    ARCHITECTURAL = "architectural"
    EXTRAMAX = "extramax"


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


class ReviewDebatePhase(str, Enum):
    """Review debate sub-state."""

    CLAUDE_REVIEW = "claude_review"
    CODEX_REVIEW = "codex_review"
    ESCALATION = "escalation"
    RESOLVED = "resolved"


DebatePhase = ReviewDebatePhase


# ---------------------------------------------------------------------------
# Plan artifact (plan.schema.json)
# ---------------------------------------------------------------------------


class Plan(BaseModel):
    """Natural implementation plan produced by the planning phase."""

    plan_id: str
    task: str = Field(min_length=1)
    approach: str = Field(min_length=1)
    implementation_steps: list[str] = Field(min_length=1)
    key_files: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Execution result artifact (execution_result.schema.json)
# ---------------------------------------------------------------------------


class FileChange(BaseModel):
    """A single file change within an execution result."""

    path: str
    action: FileAction
    summary: str = Field(min_length=1)


class StepResult(BaseModel):
    """LEGACY result of executing a single plan step."""

    step_number: int = Field(ge=1)
    status: StepStatus
    files_changed: list[FileChange] = Field(default_factory=list)
    summary: str = Field(min_length=1)
    issues: list[str] = Field(default_factory=list)
    test_commands: list[str] = Field(default_factory=list)
    workspace_diffs: dict[str, str] = Field(default_factory=dict)


class ExecutionResult(BaseModel):
    """Result of executing a full implementation plan."""

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
# Debate state (internal)
# ---------------------------------------------------------------------------


class DebateRound(BaseModel):
    """A recorded round in the review debate."""

    round_number: int = Field(ge=0)
    actor: str
    model_used: str | None = None
    effort_used: str | None = None
    position: str
    reasoning: str
    issues: list[dict[str, Any]] = Field(default_factory=list)
    artifact_id: str | None = None


class DebateState(BaseModel):
    """Persisted debate state for the merged review phase."""

    debate_phase: ReviewDebatePhase = ReviewDebatePhase.CLAUDE_REVIEW
    disagreement_case: str | None = None
    rounds: list[DebateRound] = Field(default_factory=list)
    final_verdict: str | None = None
    consolidated_issues: list[dict[str, Any]] = Field(default_factory=list)


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
    mode: str = "default"
    status: WorkflowStatus = WorkflowStatus.INIT
    current_phase: str = "INIT"
    plan_id: str | None = None
    normalized_task: str | None = None
    complexity_tier: str | None = None
    execution_result_ref: str | None = None
    step_results: list[str] = Field(default_factory=list)
    commit_commands: list[str] = Field(default_factory=list)
    review_id: str | None = None
    rework_count: int = 0
    replan_count: int = 0
    fix_iteration_count: int = 0
    retry_counts: dict[str, int] = Field(default_factory=dict)
    session_ids: dict[str, str] = Field(default_factory=dict)
    execution_overrides: dict[str, str] = Field(default_factory=dict)
    scope_md_ref: str | None = None
    claude_scope_ref: str | None = None
    codex_scope_ref: str | None = None
    scoping_round: int = 0
    scoping_agreed: bool = False
    debate_state: DebateState | None = None
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    error: str | None = None
    base_commit: str = ""
    worktree_path: str | None = None
    worktree_branch: str | None = None
    is_workspace: bool = False
    workspace_repos: list[str] = Field(default_factory=list)

    model_config = {"use_enum_values": True}


class AnalysisSession(BaseModel):
    """Persisted analysis-mode session."""

    session_id: str
    task: str
    mode: str = "analysis"
    rounds: list[dict[str, Any]] = Field(default_factory=list)
    claude_initial: str = ""
    codex_initial: str = ""
    consensus_reached: bool = False
    final_summary: str = ""
    settings: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
