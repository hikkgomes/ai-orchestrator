from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess

import pytest

from ai_orchestrator.adapters.base import BlockedOnCLI, StepFailure
from ai_orchestrator.artifacts import ArtifactStore
from ai_orchestrator.config import PhaseRoutingOverride
from ai_orchestrator.engine import Engine, EngineError
from ai_orchestrator.models import RunState, WorkflowStatus
from ai_orchestrator.state import StateManager
from ai_orchestrator.workflow import load_workflow_definition


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FakeClaudeAdapter:
    def __init__(self, plans, reviews, adjudications, scopings=None, feasibilities=None):
        self._scopings = list(scopings or [])
        self._plans = list(plans)
        self._feasibilities = list(feasibilities or [])
        self._reviews = list(reviews)
        self._adjudications = list(adjudications)
        self.scoping_calls = 0
        self.planning_calls = 0
        self.feasibility_calls = 0
        self.review_calls = 0
        self.adjudication_calls = 0
        self.invocations: list[dict[str, object]] = []

    def invoke(
        self,
        prompt,
        working_dir,
        timeout,
        schema,
        *,
        step_number=None,
        reasoning_effort_override=None,
        model_override=None,
    ):
        self.invocations.append(
            {
                "title": schema["title"],
                "prompt": prompt,
                "reasoning_effort_override": reasoning_effort_override,
                "model_override": model_override,
            }
        )
        title = schema["title"]
        if title == "TaskDefinition":
            self.scoping_calls += 1
            if self._scopings:
                return self._scopings.pop(0)
            return {
                "actionable": True,
                "normalized_task": "Implement feature",
                "assumptions": [],
                "complexity_tier": "moderate",
            }
        if title == "Plan":
            self.planning_calls += 1
            return self._plans.pop(0)
        if title == "FeasibilityResult":
            self.feasibility_calls += 1
            if self._feasibilities:
                return self._feasibilities.pop(0)
            return {
                "verdict": "go",
                "blocking_issues": [],
                "summary": "The plan is feasible.",
            }
        if title == "Review":
            self.review_calls += 1
            return self._reviews.pop(0)
        if title == "Adjudication":
            self.adjudication_calls += 1
            return self._adjudications.pop(0)
        raise AssertionError(f"Unexpected schema title: {title}")


class FakeCodexAdapter:
    def __init__(self, adjudications=None):
        self.executed_steps: list[int] = []
        self.feasibility_calls = 0
        self.adjudication_calls = 0
        self._adjudications = list(adjudications or [])
        self.invocations: list[dict[str, object]] = []

    def invoke(
        self,
        prompt,
        working_dir,
        timeout,
        schema,
        *,
        step_number=None,
        reasoning_effort_override=None,
        model_override=None,
    ):
        self.invocations.append(
            {
                "title": schema["title"],
                "prompt": prompt,
                "reasoning_effort_override": reasoning_effort_override,
                "model_override": model_override,
            }
        )
        if schema["title"] == "FeasibilityResult":
            self.feasibility_calls += 1
            return {
                "verdict": "go",
                "blocking_issues": [],
                "summary": "Environment checks passed.",
            }
        if schema["title"] == "Adjudication":
            self.adjudication_calls += 1
            if self._adjudications:
                return self._adjudications.pop(0)
            return _pass_adjudication()
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


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=path, check=True, capture_output=True)
    (path / "README.md").write_text(f"# {path.name}\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=path, check=True, capture_output=True)


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
    assert state.normalized_task == "Implement feature"
    assert state.complexity_tier == "moderate"
    assert state.feasibility_id is not None
    assert codex.executed_steps == [1, 2]
    assert (tmp_repo / "step-1.txt").exists()
    assert (tmp_repo / "step-2.txt").exists()
    assert len(state.step_results) == 2
    assert state.commit_commands == [
        "# Review staged changes:",
        "git status",
        "git diff --cached",
        "",
        'git commit -m "aio: Implement feature"',
        f"git push origin {default_config.worktree.base_branch}",
    ]


def test_review_prompt_includes_heuristics_categories_and_repo_context(tmp_repo, artifact_root, default_config):
    default_config.approval.require_plan_approval = False
    default_config.approval.require_merge_approval = False
    reviewer_dir = tmp_repo / ".ai-review"
    reviewer_dir.mkdir()
    (reviewer_dir / "config.json").write_text(
        json.dumps(
            {
                "project": {"stack": ["python", "fastapi"], "package_managers": ["uv"], "monorepo": False},
                "paths": {"generated": [], "ignore": [], "critical": ["src/auth/"]},
                "risk": {"auth_sensitive": ["middleware.py"]},
                "architecture": {"patterns": ["layered"], "key_libraries": {}, "naming": {}, "project_description": ""},
                "workspaces": {},
                "notes": [],
                "uncertain": [],
            }
        ),
        encoding="utf-8",
    )
    subprocess.run(["git", "add", ".ai-review/config.json"], cwd=tmp_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Add reviewer config"], cwd=tmp_repo, check=True, capture_output=True)

    class HeuristicCodexAdapter(FakeCodexAdapter):
        def invoke(
            self,
            prompt,
            working_dir,
            timeout,
            schema,
            *,
            step_number=None,
            reasoning_effort_override=None,
            model_override=None,
        ):
            result = super().invoke(
                prompt,
                working_dir,
                timeout,
                schema,
                step_number=step_number,
                reasoning_effort_override=reasoning_effort_override,
                model_override=model_override,
            )
            if schema["title"] == "StepResult" and step_number == 1:
                (working_dir / "step-1.txt").write_text('dummy_key = "changeme"\n', encoding="utf-8")
            return result

    claude = FakeClaudeAdapter([_plan()], [_review()], [_pass_adjudication()])
    codex = HeuristicCodexAdapter()
    engine = Engine(
        default_config,
        tmp_repo,
        artifact_root,
        adapters={"claude": claude, "codex": codex},
        workflow=_workflow(),
    )

    state = engine.start("Implement feature", "aeaeaeae-aeae-aeae-aeae-aeaeaeaeaeae")

    assert state.status == "DONE"
    review_prompts = [item["prompt"] for item in claude.invocations if item["title"] == "Review"]
    assert review_prompts
    prompt = review_prompts[0]
    assert "HEURISTIC SCAN RESULTS:" in prompt
    assert "[placeholder] step-1.txt:1 ::" in prompt
    assert "AI FAILURE CATEGORIES:" in prompt
    assert "REPOSITORY CONTEXT:" in prompt
    assert "Stack: python, fastapi" in prompt


def test_review_changed_files_failure_degrades_gracefully(tmp_repo, artifact_root, default_config, monkeypatch):
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

    def boom(_state):
        raise EngineError("git diff failed")

    monkeypatch.setattr(engine, "_review_changed_files", boom)

    state = engine.start("Implement feature", "adadadad-adad-adad-adad-adadadadadad")

    assert state.status == "DONE"
    review_prompts = [item["prompt"] for item in claude.invocations if item["title"] == "Review"]
    assert review_prompts
    assert "AI FAILURE CATEGORIES:" in review_prompts[0]


def test_adjudication_prompt_uses_normalized_task(tmp_repo, artifact_root, default_config):
    default_config.approval.require_plan_approval = False
    default_config.approval.require_merge_approval = False
    claude = FakeClaudeAdapter(
        [_plan()],
        [_review()],
        [_pass_adjudication()],
        scopings=[
            {
                "actionable": True,
                "normalized_task": "Normalized implementation task",
                "assumptions": [],
                "complexity_tier": "moderate",
            }
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

    state = engine.start("raw user wording", "afafafaf-afaf-afaf-afaf-afafafafafaf")

    assert state.status == "DONE"
    adjudication_prompts = [item["prompt"] for item in codex.invocations if item["title"] == "Adjudication"]
    assert adjudication_prompts
    assert "ORIGINAL TASK:\nNormalized implementation task" in adjudication_prompts[0]
    assert "ORIGINAL TASK:\nraw user wording" not in adjudication_prompts[0]


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
    assert claude.scoping_calls == 1

    state = engine.reject(state.run_id, "plan", "Need a different plan")
    assert state.status == "PAUSED"
    assert state.current_phase == "APPROVAL_PLAN"
    assert claude.planning_calls == 2

    state = engine.approve(state.run_id, "plan")
    assert state.status == "DONE"
    assert state.commit_commands
    assert codex.adjudication_calls == 1


def test_workspace_execution_prompt_includes_workspace_trees(tmp_path, artifact_root, default_config):
    default_config.approval.require_plan_approval = False
    default_config.approval.require_merge_approval = False
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    _init_repo(workspace_root / "frontend")
    _init_repo(workspace_root / "backend")

    claude = FakeClaudeAdapter([_plan()], [_review()], [_pass_adjudication()])
    codex = FakeCodexAdapter()
    engine = Engine(
        default_config,
        workspace_root,
        artifact_root,
        adapters={"claude": claude, "codex": codex},
        workflow=_workflow(),
    )

    state = engine.start(
        "Implement feature",
        "cccccccc-cccc-cccc-cccc-cccccccccccc",
        is_workspace=True,
        workspace_repos=["frontend", "backend"],
    )

    assert state.status == "DONE"
    execution_prompts = [item["prompt"] for item in codex.invocations if item["title"] == "StepResult"]
    assert execution_prompts
    assert "Workspace repos:" in execution_prompts[0]
    assert "## frontend/" in execution_prompts[0]
    assert "## backend/" in execution_prompts[0]


def test_workspace_resume_revalidates_cleanliness(tmp_path, artifact_root, default_config):
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    _init_repo(workspace_root / "frontend")
    _init_repo(workspace_root / "backend")
    (workspace_root / "frontend" / "dirty.txt").write_text("dirty\n", encoding="utf-8")

    engine = Engine(default_config, workspace_root, artifact_root, workflow=_workflow())
    state = RunState(
        run_id="dddddddd-dddd-dddd-dddd-dddddddddddd",
        task="Implement feature",
        status=WorkflowStatus.BLOCKED_ON_CLI,
        current_phase=WorkflowStatus.EXECUTING.value,
        is_workspace=True,
        workspace_repos=["frontend", "backend"],
    )
    StateManager(artifact_root).save(state)

    with pytest.raises(EngineError, match="Repo 'frontend' has uncommitted changes"):
        engine.resume(state.run_id)


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
    codex = FakeCodexAdapter(
        adjudications=[
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
        ]
    )
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
            self.prompts: list[str] = []
            self.failures = 0

        def invoke(
            self,
            prompt,
            working_dir,
            timeout,
            schema,
            *,
            step_number=None,
            reasoning_effort_override=None,
            model_override=None,
        ):
            if schema["title"] == "FeasibilityResult":
                return {
                    "verdict": "go",
                    "blocking_issues": [],
                    "summary": "feasible",
                }
            if schema["title"] == "Adjudication":
                return _pass_adjudication()
            if step_number is None:
                match = re.search(r"pending-step-(\d+)\.json", prompt)
                step_number = int(match.group(1)) if match else 0
            self.calls.append(step_number)
            self.prompts.append(prompt)
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
    assert state.retry_counts["step-1"] == 0
    assert "Create first file" in codex.prompts[1]
    assert "The full original prompt follows." in codex.prompts[1]


def test_invoke_with_retries_passes_full_prompt(tmp_repo, artifact_root, default_config):
    default_config.approval.require_plan_approval = False
    default_config.approval.require_merge_approval = False

    class RetryingClaudeAdapter:
        def __init__(self):
            self.prompts: list[str] = []
            self._plan_attempts = 0

        def invoke(
            self,
            prompt,
            working_dir,
            timeout,
            schema,
            *,
            step_number=None,
            reasoning_effort_override=None,
            model_override=None,
        ):
            self.prompts.append(prompt)
            title = schema["title"]
            if title == "TaskDefinition":
                return {
                    "actionable": True,
                    "normalized_task": "Implement feature",
                    "assumptions": [],
                    "complexity_tier": "moderate",
                }
            if title == "Plan":
                self._plan_attempts += 1
                if self._plan_attempts == 1:
                    raise StepFailure("invalid plan", validation_error="missing task context")
                return _plan()
            if title == "Review":
                return _review()
            if title == "Adjudication":
                return _pass_adjudication()
            raise AssertionError(f"Unexpected schema title: {title}")

    claude = RetryingClaudeAdapter()
    codex = FakeCodexAdapter()
    engine = Engine(
        default_config,
        tmp_repo,
        artifact_root,
        adapters={"claude": claude, "codex": codex},
        workflow=_workflow(),
    )

    state = engine.start("Implement feature", "ffffffff-ffff-ffff-ffff-ffffffffffff")

    assert state.status == "DONE"
    assert len(claude.prompts) >= 2
    retry_prompt = next(prompt for prompt in claude.prompts if "The full original prompt follows." in prompt)
    assert "TASK:\nImplement feature" in retry_prompt
    assert "KEY FILE CONTENTS:" in retry_prompt
    assert "missing task context" in retry_prompt


def test_worktree_reset_before_retry(tmp_repo, artifact_root, default_config):
    default_config.approval.require_plan_approval = False
    default_config.approval.require_merge_approval = False
    claude = FakeClaudeAdapter([_plan()], [_review()], [_pass_adjudication()])

    class DirtyRetryCodexAdapter:
        def __init__(self):
            self.calls = 0

        def invoke(
            self,
            prompt,
            working_dir,
            timeout,
            schema,
            *,
            step_number=None,
            reasoning_effort_override=None,
            model_override=None,
        ):
            if schema["title"] == "FeasibilityResult":
                return {
                    "verdict": "go",
                    "blocking_issues": [],
                    "summary": "feasible",
                }
            if schema["title"] == "Adjudication":
                return _pass_adjudication()
            if step_number is None:
                match = re.search(r"pending-step-(\d+)\.json", prompt)
                step_number = int(match.group(1)) if match else 0
            self.calls += 1
            if self.calls == 1:
                (working_dir / "leftover.txt").write_text("stale\n", encoding="utf-8")
                (working_dir / "README.md").write_text("dirty\n", encoding="utf-8")
                raise StepFailure("execution failed", validation_error="retry requested")

            assert not (working_dir / "leftover.txt").exists()
            assert (working_dir / "README.md").read_text(encoding="utf-8") == "# Test repo\n"
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

    codex = DirtyRetryCodexAdapter()
    engine = Engine(
        default_config,
        tmp_repo,
        artifact_root,
        adapters={"claude": claude, "codex": codex},
        workflow=_workflow(),
    )

    state = engine.start("Implement feature", "abababab-abab-abab-abab-abababababab")

    assert state.status == "DONE"
    assert codex.calls == 3


def test_worktree_reset_clears_staged_index_changes(tmp_repo, artifact_root, default_config):
    default_config.approval.require_plan_approval = False
    default_config.approval.require_merge_approval = False
    claude = FakeClaudeAdapter([_plan()], [_review()], [_pass_adjudication()])

    class DirtyIndexRetryCodexAdapter:
        def __init__(self):
            self.calls = 0

        def invoke(
            self,
            prompt,
            working_dir,
            timeout,
            schema,
            *,
            step_number=None,
            reasoning_effort_override=None,
            model_override=None,
        ):
            if schema["title"] == "FeasibilityResult":
                return {
                    "verdict": "go",
                    "blocking_issues": [],
                    "summary": "feasible",
                }
            if schema["title"] == "Adjudication":
                return _pass_adjudication()
            if step_number is None:
                match = re.search(r"pending-step-(\d+)\.json", prompt)
                step_number = int(match.group(1)) if match else 0
            self.calls += 1
            if self.calls == 1:
                (working_dir / "README.md").write_text("staged dirty\n", encoding="utf-8")
                (working_dir / "staged-new.txt").write_text("staged\n", encoding="utf-8")
                subprocess.run(
                    ["git", "add", "README.md", "staged-new.txt"],
                    cwd=working_dir,
                    check=True,
                    capture_output=True,
                )
                raise StepFailure("execution failed", validation_error="retry requested")

            assert (working_dir / "README.md").read_text(encoding="utf-8") == "# Test repo\n"
            assert not (working_dir / "staged-new.txt").exists()
            status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=working_dir,
                check=True,
                capture_output=True,
                text=True,
            )
            assert status.stdout.strip() == ""
            cached_diff = subprocess.run(
                ["git", "diff", "--cached"],
                cwd=working_dir,
                check=True,
                capture_output=True,
                text=True,
            )
            assert cached_diff.stdout.strip() == ""
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

    codex = DirtyIndexRetryCodexAdapter()
    engine = Engine(
        default_config,
        tmp_repo,
        artifact_root,
        adapters={"claude": claude, "codex": codex},
        workflow=_workflow(),
    )

    state = engine.start("Implement feature", "acacacac-acac-acac-acac-acacacacacac")

    assert state.status == "DONE"
    assert codex.calls == 3


def test_resume_paused_re_enters_gate(tmp_repo, artifact_root, default_config):
    default_config.approval.require_plan_approval = True
    default_config.approval.require_merge_approval = False

    class BlockingCodexAdapter:
        def invoke(
            self,
            prompt,
            working_dir,
            timeout,
            schema,
            *,
            step_number=None,
            reasoning_effort_override=None,
            model_override=None,
        ):
            raise BlockedOnCLI("auth refresh required", exit_code=1, stderr="login required")

    claude = FakeClaudeAdapter([_plan()], [_review()], [_pass_adjudication()])
    codex = BlockingCodexAdapter()
    engine = Engine(
        default_config,
        tmp_repo,
        artifact_root,
        adapters={"claude": claude, "codex": codex},
        workflow=_workflow(),
    )

    paused = engine.start("Implement feature", "cdcdcdcd-cdcd-cdcd-cdcd-cdcdcdcdcdcd")
    assert paused.status == "PAUSED"
    assert paused.current_phase == "APPROVAL_PLAN"

    ArtifactStore(artifact_root).save_approval_decision(paused.run_id, "plan", "approve")
    resumed = engine.resume(paused.run_id)

    assert resumed.status == "BLOCKED_ON_CLI"
    assert resumed.current_phase == "FEASIBILITY"


def test_resume_paused_without_decision_re_pauses(tmp_repo, artifact_root, default_config):
    default_config.approval.require_plan_approval = True
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

    paused = engine.start("Implement feature", "edededed-eded-eded-eded-edededededed")
    assert paused.status == "PAUSED"
    assert paused.current_phase == "APPROVAL_PLAN"

    resumed = engine.resume(paused.run_id)

    assert resumed.status == "PAUSED"
    assert resumed.current_phase == "APPROVAL_PLAN"


def test_reset_worktree_failure_raises_engine_error(tmp_repo, artifact_root, default_config):
    default_config.approval.require_plan_approval = False
    default_config.approval.require_merge_approval = False
    claude = FakeClaudeAdapter([_plan()], [_review()], [_pass_adjudication()])

    class DirtyRetryCodexAdapter:
        def __init__(self):
            self.calls = 0

        def invoke(
            self,
            prompt,
            working_dir,
            timeout,
            schema,
            *,
            step_number=None,
            reasoning_effort_override=None,
            model_override=None,
        ):
            if schema["title"] == "FeasibilityResult":
                return {
                    "verdict": "go",
                    "blocking_issues": [],
                    "summary": "feasible",
                }
            self.calls += 1
            if self.calls == 1:
                raise StepFailure("execution failed", validation_error="retry requested")
            raise AssertionError("retry should not reach adapter after reset failure")

    codex = DirtyRetryCodexAdapter()
    engine = Engine(
        default_config,
        tmp_repo,
        artifact_root,
        adapters={"claude": claude, "codex": codex},
        workflow=_workflow(),
    )

    def _boom(worktree_dir: Path) -> None:
        raise EngineError("Failed to reset worktree: boom")

    engine._reset_worktree = _boom  # type: ignore[method-assign]

    with pytest.raises(EngineError, match="Failed to reset worktree: boom"):
        engine.start("Implement feature", "12121212-1212-1212-1212-121212121212")


def test_scoping_not_actionable_pauses_and_reject_rescopes(tmp_repo, artifact_root, default_config):
    default_config.approval.require_plan_approval = False
    default_config.approval.require_merge_approval = False
    claude = FakeClaudeAdapter(
        [_plan()],
        [_review()],
        [_pass_adjudication()],
        scopings=[
            {
                "actionable": False,
                "normalized_task": "Original task",
                "assumptions": [],
                "blocking_reason": "Task is too vague.",
                "complexity_tier": "complex",
            },
            {
                "actionable": True,
                "normalized_task": "Implement feature",
                "assumptions": [],
                "complexity_tier": "simple",
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

    paused = engine.start("Do the thing", "13131313-1313-1313-1313-131313131313")

    assert paused.status == "PAUSED"
    assert paused.current_phase == "SCOPING"
    assert paused.error == "Task is too vague."

    resumed = engine.reject(paused.run_id, "scope", "Implement feature")

    assert resumed.status == "DONE"
    assert resumed.task == "Implement feature"
    assert resumed.complexity_tier == "simple"
    assert claude.scoping_calls == 2


def test_scoping_disabled_skips_directly_to_planning(tmp_repo, artifact_root, default_config):
    default_config.scoping.enabled = False
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

    state = engine.start("Implement feature", "14141414-1414-1414-1414-141414141414")

    assert state.status == "DONE"
    assert claude.scoping_calls == 0
    assert state.normalized_task is None


def test_feasibility_blocked_replans_with_feedback(tmp_repo, artifact_root, default_config):
    default_config.approval.require_plan_approval = False
    default_config.approval.require_merge_approval = False
    claude = FakeClaudeAdapter(
        [_plan(plan_id="p1"), _plan(plan_id="p2")],
        [_review()],
        [_pass_adjudication()],
    )

    class BlockingFeasibilityCodex(FakeCodexAdapter):
        def __init__(self):
            super().__init__()
            self._first = True

        def invoke(
            self,
            prompt,
            working_dir,
            timeout,
            schema,
            *,
            step_number=None,
            reasoning_effort_override=None,
            model_override=None,
        ):
            if schema["title"] == "FeasibilityResult":
                self.feasibility_calls += 1
                if self._first:
                    self._first = False
                    return {
                        "verdict": "blocked",
                        "blocking_issues": [
                            {"severity": "critical", "description": "Missing dependency"}
                        ],
                        "summary": "Plan is blocked.",
                    }
                return {
                    "verdict": "go",
                    "blocking_issues": [],
                    "summary": "Feasible after replan.",
                }
            return super().invoke(
                prompt,
                working_dir,
                timeout,
                schema,
                step_number=step_number,
                reasoning_effort_override=reasoning_effort_override,
                model_override=model_override,
            )

    codex = BlockingFeasibilityCodex()
    engine = Engine(
        default_config,
        tmp_repo,
        artifact_root,
        adapters={"claude": claude, "codex": codex},
        workflow=_workflow(),
    )

    state = engine.start("Implement feature", "15151515-1515-1515-1515-151515151515")

    assert state.status == "DONE"
    assert state.replan_count == 1
    assert claude.planning_calls == 2
    assert codex.feasibility_calls == 2


def test_feasibility_blocked_at_replan_limit_fails(tmp_repo, artifact_root, default_config):
    default_config.approval.require_plan_approval = False
    default_config.approval.require_merge_approval = False
    default_config.orchestrator.max_replan_loops = 0
    claude = FakeClaudeAdapter([_plan()], [_review()], [_pass_adjudication()])

    class AlwaysBlockedCodex(FakeCodexAdapter):
        def invoke(
            self,
            prompt,
            working_dir,
            timeout,
            schema,
            *,
            step_number=None,
            reasoning_effort_override=None,
            model_override=None,
        ):
            if schema["title"] == "FeasibilityResult":
                self.feasibility_calls += 1
                return {
                    "verdict": "blocked",
                    "blocking_issues": [
                        {"severity": "critical", "description": "Broken toolchain"}
                    ],
                    "summary": "Cannot execute.",
                }
            return super().invoke(
                prompt,
                working_dir,
                timeout,
                schema,
                step_number=step_number,
                reasoning_effort_override=reasoning_effort_override,
                model_override=model_override,
            )

    codex = AlwaysBlockedCodex()
    engine = Engine(
        default_config,
        tmp_repo,
        artifact_root,
        adapters={"claude": claude, "codex": codex},
        workflow=_workflow(),
    )

    state = engine.start("Implement feature", "16161616-1616-1616-1616-161616161616")

    assert state.status == "FAILED"
    assert "Replan loop limit exceeded" in (state.error or "")


def test_feasibility_disabled_skips_to_execution(tmp_repo, artifact_root, default_config):
    default_config.approval.require_plan_approval = False
    default_config.approval.require_merge_approval = False
    default_config.feasibility.enabled = False
    claude = FakeClaudeAdapter([_plan()], [_review()], [_pass_adjudication()])
    codex = FakeCodexAdapter()
    engine = Engine(
        default_config,
        tmp_repo,
        artifact_root,
        adapters={"claude": claude, "codex": codex},
        workflow=_workflow(),
    )

    state = engine.start("Implement feature", "17171717-1717-1717-1717-171717171717")

    assert state.status == "DONE"
    assert codex.feasibility_calls == 0


def test_complexity_drives_reasoning_effort_and_phase_override_wins(tmp_repo, artifact_root, default_config):
    default_config.approval.require_plan_approval = False
    default_config.approval.require_merge_approval = False
    default_config.routing.phases["reviewing"] = PhaseRoutingOverride(reasoning_effort="max")
    claude = FakeClaudeAdapter(
        [_plan()],
        [_review()],
        [_pass_adjudication()],
        scopings=[
            {
                "actionable": True,
                "normalized_task": "Implement feature",
                "assumptions": [],
                "complexity_tier": "architectural",
            }
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

    state = engine.start("Implement feature", "18181818-1818-1818-1818-181818181818")

    assert state.status == "DONE"
    planning_call = next(call for call in claude.invocations if call["title"] == "Plan")
    review_call = next(call for call in claude.invocations if call["title"] == "Review")
    feasibility_call = next(call for call in codex.invocations if call["title"] == "FeasibilityResult")
    execute_call = next(call for call in codex.invocations if call["title"] == "StepResult")
    assert planning_call["reasoning_effort_override"] == "max"
    assert feasibility_call["reasoning_effort_override"] == "max"
    assert execute_call["reasoning_effort_override"] == "xhigh"
    assert review_call["reasoning_effort_override"] == "max"


def test_step_failure_stderr_surfaces_in_failed_run_error(tmp_repo, artifact_root, default_config):
    default_config.approval.require_plan_approval = False
    default_config.approval.require_merge_approval = False

    class FailingClaudeAdapter(FakeClaudeAdapter):
        def invoke(
            self,
            prompt,
            working_dir,
            timeout,
            schema,
            *,
            step_number=None,
            reasoning_effort_override=None,
            model_override=None,
        ):
            if schema["title"] == "TaskDefinition":
                raise StepFailure(
                    "Claude CLI exited with a non-zero status",
                    exit_code=2,
                    stderr="error: unknown option '--effort'",
                )
            return super().invoke(
                prompt,
                working_dir,
                timeout,
                schema,
                step_number=step_number,
                reasoning_effort_override=reasoning_effort_override,
                model_override=model_override,
            )

    engine = Engine(
        default_config,
        tmp_repo,
        artifact_root,
        adapters={
            "claude": FailingClaudeAdapter([_plan()], [_review()], [_pass_adjudication()]),
            "codex": FakeCodexAdapter(),
        },
        workflow=_workflow(),
    )

    state = engine.start("Implement feature", "19191919-1919-1919-1919-191919191919")

    assert state.status == "FAILED"
    assert state.current_phase == "SCOPING"
    assert state.error is not None
    assert "stderr: error: unknown option '--effort'" in state.error
    assert "exit_code: 2" in state.error
