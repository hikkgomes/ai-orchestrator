"""Tests for Pydantic models (src/ai_orchestrator/models.py)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ai_orchestrator.models import (
    Adjudication,
    AdjudicationVerdict,
    ExecutionResult,
    FileAction,
    FileChange,
    Finding,
    FindingSeverity,
    Plan,
    Review,
    ReviewVerdict,
    RunState,
    StepResult,
    StepStatus,
    WorkflowStatus,
)


class TestRunState:
    def test_default_status_is_init(self):
        state = RunState(run_id="abc", task="do something")
        assert state.status == WorkflowStatus.INIT

    def test_created_at_set(self):
        state = RunState(run_id="abc", task="t")
        assert state.created_at  # not empty

    def test_timestamps_are_timezone_aware(self):
        state = RunState(run_id="abc", task="t")
        assert state.created_at.endswith("+00:00")
        assert state.updated_at.endswith("+00:00")


class TestPlan:
    def test_valid_plan(self):
        plan = Plan(
            plan_id="00000000-0000-0000-0000-000000000001",
            task="Do something",
            approach="Keep the change focused.",
            implementation_steps=["Update the implementation."],
            key_files=["README.md"],
        )
        assert plan.implementation_steps == ["Update the implementation."]

    def test_requires_implementation_step(self):
        with pytest.raises(ValidationError):
            Plan(
                plan_id="00000000-0000-0000-0000-000000000001",
                task="Do something",
                approach="Keep the change focused.",
                implementation_steps=[],
                key_files=[],
            )


class TestStepResult:
    def test_valid_result(self):
        r = StepResult(
            step_number=1,
            status=StepStatus.SUCCESS,
            files_changed=[
                FileChange(path="src/foo.py", action=FileAction.MODIFIED, summary="Updated foo")
            ],
            summary="Done",
        )
        assert r.status == StepStatus.SUCCESS


class TestExecutionResult:
    def test_valid_result(self):
        r = ExecutionResult(
            status=StepStatus.SUCCESS,
            files_changed=[
                FileChange(path="src/foo.py", action=FileAction.MODIFIED, summary="Updated foo")
            ],
            summary="Done",
        )
        assert r.status == StepStatus.SUCCESS


class TestReview:
    def test_valid_approve(self):
        r = Review(
            review_id="00000000-0000-0000-0000-000000000001",
            verdict=ReviewVerdict.APPROVE,
            score=9,
            findings=[],
            summary="LGTM",
            blocks_merge=False,
        )
        assert r.verdict == ReviewVerdict.APPROVE

    def test_score_out_of_range(self):
        with pytest.raises(ValidationError):
            Review(
                review_id="x",
                verdict=ReviewVerdict.APPROVE,
                score=11,
                findings=[],
                summary="x",
                blocks_merge=False,
            )


class TestAdjudication:
    def test_pass_verdict(self):
        a = Adjudication(
            adjudication_id="00000000-0000-0000-0000-000000000001",
            verdict=AdjudicationVerdict.PASS,
            reasoning="Looks good.",
        )
        assert a.verdict == AdjudicationVerdict.PASS
