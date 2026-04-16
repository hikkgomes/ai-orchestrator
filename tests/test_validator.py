"""Tests for JSON schema + application validation (src/ai_orchestrator/validator.py).

Phase 6 from build-plan.md: all schema validations, application validations,
path traversal edge cases.
"""

from __future__ import annotations

import pytest

from ai_orchestrator.validator import ValidationError, Validator


class TestPathTraversal:
    """Path traversal checks — critical security invariant (DD-8)."""

    def test_leading_slash_rejected(self, tmp_path):
        v = Validator(tmp_path)
        plan = _minimal_plan(key_files=["/etc/passwd"])
        with pytest.raises(ValidationError):
            v.validate_plan(plan)

    def test_dotdot_in_middle_rejected(self, tmp_path):
        v = Validator(tmp_path)
        plan = _minimal_plan(key_files=["a/../../etc/passwd"])
        with pytest.raises(ValidationError):
            v.validate_plan(plan)

    def test_safe_path_accepted(self, tmp_path):
        v = Validator(tmp_path)
        plan = _minimal_plan(key_files=["src/foo.py"])
        result = v.validate_plan(plan)
        assert result is not None


class TestPlanValidation:
    def test_implementation_steps_required(self, tmp_path):
        v = Validator(tmp_path)
        plan = _minimal_plan(implementation_steps=[])
        with pytest.raises(ValidationError):
            v.validate_plan(plan)


class TestReviewValidation:
    def test_reject_requires_blocks_merge_true(self, tmp_path):
        v = Validator(tmp_path)
        review = _minimal_review(verdict="reject", blocks_merge=False)
        with pytest.raises(ValidationError):
            v.validate_review(review)


class TestAdjudicationValidation:
    def test_rework_requires_feedback(self, tmp_path):
        v = Validator(tmp_path)
        adjudication = {
            "adjudication_id": "00000000-0000-0000-0000-000000000000",
            "verdict": "REWORK",
            "reasoning": "Fix step selection.",
        }

        with pytest.raises(ValidationError):
            v.validate_adjudication(adjudication)

    def test_request_changes_requires_major_or_critical_finding(self, tmp_path):
        v = Validator(tmp_path)
        review = _minimal_review(verdict="request_changes", blocks_merge=False)
        with pytest.raises(ValidationError):
            v.validate_review(review)


class TestStepResultValidation:
    def test_step_number_must_match(self, tmp_path):
        v = Validator(tmp_path)
        result = {
            "step_number": 2,
            "status": "partial",
            "files_changed": [],
            "summary": "Done",
        }
        with pytest.raises(ValidationError):
            v.validate_step_result(result, step_number=1)


class TestExecutionResultValidation:
    def test_success_requires_files_changed(self, tmp_path):
        v = Validator(tmp_path)
        result = {
            "status": "success",
            "files_changed": [],
            "summary": "Done",
        }
        with pytest.raises(ValidationError):
            v.validate_execution_result(result)


class TestScopingValidation:
    def test_non_actionable_requires_blocking_reason(self, tmp_path):
        v = Validator(tmp_path)
        scoping = {
            "actionable": False,
            "normalized_task": "raw task",
            "assumptions": [],
            "complexity_tier": "simple",
        }
        with pytest.raises(ValidationError):
            v.validate_scoping(scoping)

    def test_actionable_scoping_is_valid(self, tmp_path):
        v = Validator(tmp_path)
        scoping = {
            "actionable": True,
            "normalized_task": "Fix typo in README",
            "assumptions": [],
            "complexity_tier": "simple",
        }
        assert v.validate_scoping(scoping)["complexity_tier"] == "simple"

    def test_missing_actionable_no_blocking_reason_defaults_to_true(self, tmp_path):
        v = Validator(tmp_path)
        result = v.validate_scoping({
            "normalized_task": "Fix typo",
            "assumptions": [],
            "complexity_tier": "simple",
        })
        assert result["actionable"] is True

    def test_missing_actionable_with_blocking_reason_infers_false(self, tmp_path):
        v = Validator(tmp_path)
        result = v.validate_scoping({
            "normalized_task": "raw task",
            "assumptions": [],
            "complexity_tier": "simple",
            "blocking_reason": "Task targets external system",
        })
        assert result["actionable"] is False

    def test_missing_assumptions_defaults_to_empty_list(self, tmp_path):
        v = Validator(tmp_path)
        result = v.validate_scoping({
            "actionable": True,
            "normalized_task": "Fix typo",
            "complexity_tier": "simple",
        })
        assert result["assumptions"] == []


class TestFeasibilityValidation:
    def test_blocked_requires_critical_issue(self, tmp_path):
        v = Validator(tmp_path)
        feasibility = {
            "verdict": "blocked",
            "blocking_issues": [{"severity": "warning", "description": "maybe"}],
            "summary": "Blocked",
        }
        with pytest.raises(ValidationError):
            v.validate_feasibility(feasibility)

    def test_go_with_warnings_is_valid(self, tmp_path):
        v = Validator(tmp_path)
        feasibility = {
            "verdict": "go_with_warnings",
            "blocking_issues": [{"severity": "warning", "description": "Optional tool missing"}],
            "summary": "Can proceed.",
        }
        assert v.validate_feasibility(feasibility)["verdict"] == "go_with_warnings"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_plan(
    key_files: list[str] | None = None,
    implementation_steps: list[str] | None = None,
) -> dict:
    return {
        "plan_id": "00000000-0000-0000-0000-000000000000",
        "task": "Test task",
        "approach": "Test",
        "implementation_steps": ["Step 1"] if implementation_steps is None else implementation_steps,
        "key_files": key_files or [],
    }


def _minimal_review(verdict: str = "approve", blocks_merge: bool = False) -> dict:
    return {
        "review_id": "00000000-0000-0000-0000-000000000000",
        "verdict": verdict,
        "score": 8,
        "findings": [],
        "summary": "Looks good.",
        "blocks_merge": blocks_merge,
    }
