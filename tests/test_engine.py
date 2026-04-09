from __future__ import annotations

import json
from pathlib import Path
import re

from ai_orchestrator.artifacts import ArtifactStore
from ai_orchestrator.engine import Engine
from ai_orchestrator.models import RunState
from ai_orchestrator.state import StateManager
from ai_orchestrator.workflow import load_workflow_definition


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FakeClaudeAdapter:
    def __init__(self, plans, reviews, adjudications):
        self._plans = list(plans)
        self._reviews = list(reviews)
        self._adjudications = list(adjudications)
        self.planning_calls = 0
        self.review_calls = 0
        self.adjudication_calls = 0

    def invoke(self, prompt, working_dir, timeout, schema):
        title = schema["title"]
        if title == "Plan":
            self.planning_calls += 1
            return self._plans.pop(0)
        if title == "Review":
            self.review_calls += 1
            return self._reviews.pop(0)
        if title == "Adjudication":
            self.adjudication_calls += 1
            return self._adjudications.pop(0)
        raise AssertionError(f"Unexpected schema title: {title}")


class FakeCodexAdapter:
    def __init__(self):
        self.executed_steps: list[int] = []

    def invoke(self, prompt, working_dir, timeout, schema, *, step_number=None):
        if step_number is None:
            match = re.search(r"pending-step-(\d+)\.json", prompt)
            step_number = int(match.group(1)) if match else 0
        self.executed_steps.append(step_number)
        target = working_dir / f"step-{step_number}.txt"
        target.write_text(f"step {step_number}\n", encoding="utf-8")
        return {
            "step_number": step_number,
            "status": "success",
            "files_changed": [
                {
                    "path": target.name,
                    "action": "created",
                    "summary": f"Created {target.name}",
                }
            ],
            "summary": f"Implemented step {step_number}",
            "issues": [],
            "test_commands": [],
        }


def _workflow():
    return load_workflow_definition(PROJECT_ROOT)


def _plan(*, plan_id: str = "11111111-1111-1111-1111-111111111111"):
    return {
        "plan_id": plan_id,
        "task": "Implement feature",
        "steps": [
            {
                "step_number": 1,
                "description": "Create first file",
                "files_to_read": ["README.md"],
                "files_to_modify": ["step-1.txt"],
                "depends_on": [],
                "estimated_complexity": "low",
            },
            {
                "step_number": 2,
                "description": "Create second file",
                "files_to_read": ["README.md"],
                "files_to_modify": ["step-2.txt"],
                "depends_on": [1],
                "estimated_complexity": "low",
            },
        ],
        "reasoning": "Two small sequential steps.",
    }


def _review():
    return {
        "review_id": "22222222-2222-2222-2222-222222222222",
        "verdict": "approve",
        "score": 9,
        "findings": [],
        "summary": "Looks good.",
        "blocks_merge": False,
    }


def _pass_adjudication():
    return {
        "adjudication_id": "33333333-3333-3333-3333-333333333333",
        "verdict": "PASS",
        "reasoning": "Ship it.",
    }


def test_engine_happy_path(tmp_repo, artifact_root, default_config):
    default_config.approval.require_plan_approval = False
    default_config.approval.require_merge_approval = False
    claude = FakeClaudeAdapter([_plan()], [_review()], [_pass_adjudication()])
    codex = FakeCodexAdapter()
    engine = Engine(
        default_config,
        tmp_repo,
        artifact_root,
        adapters={"claude": claude, "codex": codex},
        workflow=_workflow(),
    )

    state = engine.start("Implement feature", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

    assert state.status == "DONE"
    assert codex.executed_steps == [1, 2]
    assert (tmp_repo / "step-1.txt").exists()
    assert (tmp_repo / "step-2.txt").exists()
    assert len(state.step_results) == 2


def test_engine_approval_flow(tmp_repo, artifact_root, default_config):
    default_config.approval.require_plan_approval = True
    default_config.approval.require_merge_approval = True
    claude = FakeClaudeAdapter(
        [_plan(plan_id="aaaaaaaa-1111-1111-1111-111111111111"), _plan(plan_id="bbbbbbbb-1111-1111-1111-111111111111")],
        [_review(), _review()],
        [_pass_adjudication(), _pass_adjudication()],
    )
    codex = FakeCodexAdapter()
    engine = Engine(
        default_config,
        tmp_repo,
        artifact_root,
        adapters={"claude": claude, "codex": codex},
        workflow=_workflow(),
    )

    state = engine.start("Implement feature", "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
    assert state.status == "PAUSED"
    assert state.current_phase == "APPROVAL_PLAN"

    state = engine.reject(state.run_id, "plan", "Need a different plan")
    assert state.status == "PAUSED"
    assert state.current_phase == "APPROVAL_PLAN"
    assert claude.planning_calls == 2

    state = engine.approve(state.run_id, "plan")
    assert state.status == "PAUSED"
    assert state.current_phase == "APPROVAL_MERGE"

    state = engine.reject(state.run_id, "merge", "Run adjudication again")
    assert state.status == "PAUSED"
    assert state.current_phase == "APPROVAL_MERGE"
    assert claude.adjudication_calls == 2

    state = engine.approve(state.run_id, "merge")
    assert state.status == "DONE"


def test_engine_rework_loop_limit_fails(tmp_repo, artifact_root, default_config):
    default_config.approval.require_plan_approval = False
    default_config.approval.require_merge_approval = False
    default_config.orchestrator.max_rework_loops = 1
    claude = FakeClaudeAdapter(
        [_plan()],
        [_review(), _review()],
        [
            {
                "adjudication_id": "44444444-4444-4444-4444-444444444444",
                "verdict": "REWORK",
                "reasoning": "Try again.",
                "rework_steps": [1],
                "rework_feedback": "Adjust step 1.",
            },
            {
                "adjudication_id": "55555555-5555-5555-5555-555555555555",
                "verdict": "REWORK",
                "reasoning": "Still not enough.",
                "rework_steps": [1],
                "rework_feedback": "Adjust step 1 again.",
            },
        ],
    )
    codex = FakeCodexAdapter()
    engine = Engine(
        default_config,
        tmp_repo,
        artifact_root,
        adapters={"claude": claude, "codex": codex},
        workflow=_workflow(),
    )

    state = engine.start("Implement feature", "cccccccc-cccc-cccc-cccc-cccccccccccc")
    assert state.status == "FAILED"
    assert "Rework loop limit exceeded" in (state.error or "")


def test_resume_from_executing_uses_manifest(tmp_repo, artifact_root, default_config):
    default_config.approval.require_plan_approval = False
    default_config.approval.require_merge_approval = False
    run_id = "dddddddd-dddd-dddd-dddd-dddddddddddd"
    state_mgr = StateManager(artifact_root)
    store = ArtifactStore(artifact_root)
    plan_ref = store.save_plan(run_id, _plan())
    first_result_ref = store.save_step_result(
        run_id,
        1,
        {
            "step_number": 1,
            "status": "success",
            "files_changed": [
                {
                    "path": "step-1.txt",
                    "action": "created",
                    "summary": "Created step-1.txt",
                }
            ],
            "summary": "Implemented step 1",
            "issues": [],
            "test_commands": [],
        },
    )

    from ai_orchestrator.worktree import WorktreeManager

    worktrees = WorktreeManager(tmp_repo, artifact_root)
    worktree_path, branch_name, base_commit = worktrees.create(
        run_id,
        default_config.worktree.base_branch,
        default_config.worktree.branch_prefix,
    )
    (worktree_path / "step-1.txt").write_text("step 1\n", encoding="utf-8")
    store.save_execution_manifest(
        run_id,
        {
            "run_id": run_id,
            "plan_artifact": plan_ref,
            "mode": "plan",
            "target_steps": [1, 2],
            "completed_steps": [1],
            "feedback": None,
        },
    )

    state = RunState(run_id=run_id, task="Implement feature")
    state.status = "EXECUTING"
    state.current_phase = "EXECUTING"
    state.plan_id = plan_ref
    state.step_results = [first_result_ref]
    state.worktree_path = str(worktree_path)
    state.worktree_branch = branch_name
    state.base_commit = base_commit
    state_mgr.save(state)

    claude = FakeClaudeAdapter([_plan()], [_review()], [_pass_adjudication()])
    codex = FakeCodexAdapter()
    engine = Engine(
        default_config,
        tmp_repo,
        artifact_root,
        adapters={"claude": claude, "codex": codex},
        workflow=_workflow(),
    )

    resumed = engine.resume(run_id)

    assert resumed.status == "DONE"
    assert codex.executed_steps == [2]


def test_engine_retries_when_step_reports_failed_status(tmp_repo, artifact_root, default_config):
    default_config.approval.require_plan_approval = False
    default_config.approval.require_merge_approval = False
    claude = FakeClaudeAdapter([_plan()], [_review()], [_pass_adjudication()])

    class FlakyCodexAdapter:
        def __init__(self):
            self.calls: list[int] = []
            self.failures = 0

        def invoke(self, prompt, working_dir, timeout, schema, *, step_number=None):
            if step_number is None:
                match = re.search(r"pending-step-(\d+)\.json", prompt)
                step_number = int(match.group(1)) if match else 0
            self.calls.append(step_number)
            target = working_dir / f"step-{step_number}.txt"
            target.write_text(f"step {step_number}\n", encoding="utf-8")
            if step_number == 1 and self.failures == 0:
                self.failures += 1
                return {
                    "step_number": 1,
                    "status": "failed",
                    "files_changed": [],
                    "summary": "Required file was not ready yet.",
                    "issues": ["retry requested"],
                    "test_commands": [],
                }
            return {
                "step_number": step_number,
                "status": "success",
                "files_changed": [
                    {
                        "path": target.name,
                        "action": "created",
                        "summary": f"Created {target.name}",
                    }
                ],
                "summary": f"Implemented step {step_number}",
                "issues": [],
                "test_commands": [],
            }

    codex = FlakyCodexAdapter()
    engine = Engine(
        default_config,
        tmp_repo,
        artifact_root,
        adapters={"claude": claude, "codex": codex},
        workflow=_workflow(),
    )

    state = engine.start("Implement feature", "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")

    assert state.status == "DONE"
    assert codex.calls == [1, 1, 2]
