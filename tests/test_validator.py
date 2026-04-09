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
        plan = _minimal_plan(files_to_modify=["/etc/passwd"])
        with pytest.raises(ValidationError):
            v.validate_plan(plan)

    def test_dotdot_in_middle_rejected(self, tmp_path):
        v = Validator(tmp_path)
        plan = _minimal_plan(files_to_modify=["a/../../etc/passwd"])
        with pytest.raises(ValidationError):
            v.validate_plan(plan)

    def test_safe_path_accepted(self, tmp_path):
        v = Validator(tmp_path)
        plan = _minimal_plan(files_to_modify=["src/foo.py"])
        result = v.validate_plan(plan)
        assert result is not None


class TestPlanValidation:
    def test_sequential_step_numbers(self, tmp_path):
        v = Validator(tmp_path)
        plan = _minimal_plan(step_numbers=[1, 3])  # gap — invalid
        with pytest.raises(ValidationError):
            v.validate_plan(plan)

    def test_circular_dependency_rejected(self, tmp_path):
        v = Validator(tmp_path)
        plan = _minimal_plan(depends_on={1: [2], 2: [1]})
        with pytest.raises(ValidationError):
            v.validate_plan(plan)


class TestReviewValidation:
    def test_reject_requires_blocks_merge_true(self, tmp_path):
        v = Validator(tmp_path)
        review = _minimal_review(verdict="reject", blocks_merge=False)
        with pytest.raises(ValidationError):
            v.validate_review(review)


class TestAdjudicationValidation:
    def test_rework_steps_must_reference_current_plan_steps(self, tmp_path):
        v = Validator(tmp_path)
        adjudication = {
            "adjudication_id": "00000000-0000-0000-0000-000000000000",
            "verdict": "REWORK",
            "reasoning": "Fix step selection.",
            "rework_steps": [99],
            "rework_feedback": "Retry the correct step.",
        }

        with pytest.raises(ValidationError):
            v.validate_adjudication(adjudication, plan_step_numbers={1, 2, 3})

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

    def test_success_requires_files_changed(self, tmp_path):
        v = Validator(tmp_path)
        result = {
            "step_number": 1,
            "status": "success",
            "files_changed": [],
            "summary": "Done",
        }
        with pytest.raises(ValidationError):
            v.validate_step_result(result, step_number=1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_plan(
    files_to_modify: list[str] | None = None,
    step_numbers: list[int] | None = None,
    depends_on: dict[int, list[int]] | None = None,
) -> dict:
    steps = []
    numbers = step_numbers or [1]
    for n in numbers:
        steps.append({
            "step_number": n,
            "description": f"Step {n}",
            "files_to_read": [],
            "files_to_modify": files_to_modify or [],
            "depends_on": (depends_on or {}).get(n, []),
            "estimated_complexity": "low",
        })
    return {
        "plan_id": "00000000-0000-0000-0000-000000000000",
        "task": "Test task",
        "steps": steps,
        "reasoning": "Test",
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
