from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess

import pytest

from ai_orchestrator.adapters.base import BlockedOnCLI, InvokeResult, StepFailure, TextInvokeResult
from ai_orchestrator.artifacts import ArtifactStore
from ai_orchestrator.config import PhaseRoutingOverride
from ai_orchestrator.engine import Engine, EngineError
from ai_orchestrator.models import DebateState, ReviewDebatePhase, RunState, WorkflowStatus
from ai_orchestrator.state import StateManager
from ai_orchestrator.workflow import load_workflow_definition


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FakeClaudeAdapter:
    def __init__(
        self,
        plans,
        reviews,
        review_placeholders=None,
        scopings=None,
        debate_responses=None,
    ):
        self._scopings = list(scopings or [])
        self._plans = list(plans)
        self._reviews = list(reviews)
        self._debate_responses = list(debate_responses or [])
        self.scoping_calls = 0
        self.planning_calls = 0
        self.review_calls = 0
        self.invocations: list[dict[str, object]] = []
        self.text_invocations: list[dict[str, object]] = []
        self._last_scope = None

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
        resume_session_id=None,
        allowed_tools=None,
    ):
        self.invocations.append(
            {
                "title": schema["title"],
                "prompt": prompt,
                "reasoning_effort_override": reasoning_effort_override,
                "model_override": model_override,
                "resume_session_id": resume_session_id,
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
        if title == "Review":
            self.review_calls += 1
            return InvokeResult(self._reviews.pop(0), session_id="review-session")
        if title == "DebateResponse":
            if self._debate_responses:
                return InvokeResult(self._debate_responses.pop(0))
            return InvokeResult({"position": "issues_dismissed", "reasoning": "Convinced.", "issues": []})
        raise AssertionError(f"Unexpected schema title: {title}")

    def invoke_text(
        self,
        prompt,
        working_dir,
        timeout,
        *,
        reasoning_effort_override=None,
        model_override=None,
        resume_session_id=None,
        allowed_tools=None,
    ):
        self.text_invocations.append(
            {
                "prompt": prompt,
                "reasoning_effort_override": reasoning_effort_override,
                "model_override": model_override,
                "resume_session_id": resume_session_id,
                "allowed_tools": allowed_tools,
            }
        )
        if (
            "Plan for the implementation of the task below:" in prompt
            or "plan to fix the issues we found" in prompt
        ):
            self.planning_calls += 1
            self.invocations.append(
                {
                    "title": "Plan",
                    "prompt": prompt,
                    "reasoning_effort_override": reasoning_effort_override,
                    "model_override": model_override,
                    "resume_session_id": resume_session_id,
                }
            )
            return TextInvokeResult(_plan_markdown(self._plans.pop(0)), session_id="planning-session")
        if "Scope the request for implementation across this project" in prompt:
            self.scoping_calls += 1
            if self._scopings:
                self._last_scope = self._scopings.pop(0)
            else:
                self._last_scope = {
                    "actionable": True,
                    "normalized_task": "Implement feature",
                    "assumptions": [],
                    "complexity_tier": "moderate",
                }
            return TextInvokeResult(_scope_md(self._last_scope), session_id="scoping-claude-session")
        if (
            "Codex reviewed your scope and has feedback:" in prompt
            or "Codex still disagrees." in prompt
        ):
            return TextInvokeResult(
                _scope_md(self._last_scope or {
                    "actionable": True,
                    "normalized_task": "Implement feature",
                    "assumptions": [],
                    "complexity_tier": "moderate",
                }),
                session_id="scoping-claude-session",
            )
        raise AssertionError(f"Unexpected Claude text prompt: {prompt[:80]}")


class FakeCodexAdapter:
    def __init__(self, reviews=None, debate_responses=None):
        self.executed_steps: list[int] = []
        self.execution_calls = 0
        self.scoping_calls = 0
        self.review_calls = 0
        self._reviews = list(reviews or [])
        self._debate_responses = list(debate_responses or [])
        self.invocations: list[dict[str, object]] = []
        self.text_invocations: list[dict[str, object]] = []

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
        resume_session_id=None,
        allowed_tools=None,
    ):
        self.invocations.append(
            {
                "title": schema["title"],
                "prompt": prompt,
                "reasoning_effort_override": reasoning_effort_override,
                "model_override": model_override,
                "resume_session_id": resume_session_id,
            }
        )
        if schema["title"] == "Review":
            self.review_calls += 1
            if self._reviews:
                return InvokeResult(self._reviews.pop(0), session_id="codex-review-session")
            return InvokeResult(_review(), session_id="codex-review-session")
        if schema["title"] == "DebateResponse":
            if self._debate_responses:
                return InvokeResult(self._debate_responses.pop(0))
            return InvokeResult({"position": "issues_dismissed", "reasoning": "Convinced.", "issues": []})
        if schema["title"] == "ExecutionResult":
            self.execution_calls += 1
            files_changed = []
            for step_number in (1, 2):
                self.executed_steps.append(step_number)
                target = working_dir / f"step-{step_number}.txt"
                target.write_text(f"step {step_number}\n", encoding="utf-8")
                files_changed.append(
                    {
                        "path": target.name,
                        "action": "created",
                        "summary": f"Created {target.name}",
                    }
                )
            return InvokeResult({
                "status": "success",
                "files_changed": files_changed,
                "summary": "Implemented the full plan",
                "issues": [],
                "test_commands": [],
            })
        if step_number is None:
            match = re.search(r"pending-step-(\d+)\.json", prompt)
            step_number = int(match.group(1)) if match else 0
        self.executed_steps.append(step_number)
        target = working_dir / f"step-{step_number}.txt"
        target.write_text(f"step {step_number}\n", encoding="utf-8")
        return InvokeResult({
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
        })

    def invoke_text(
        self,
        prompt,
        working_dir,
        timeout,
        *,
        reasoning_effort_override=None,
        model_override=None,
        resume_session_id=None,
        allowed_tools=None,
    ):
        self.text_invocations.append(
            {
                "prompt": prompt,
                "reasoning_effort_override": reasoning_effort_override,
                "model_override": model_override,
                "resume_session_id": resume_session_id,
                "allowed_tools": allowed_tools,
            }
        )
        self.scoping_calls += 1
        return TextInvokeResult("---\nagreement: true\n---\n\nScope is acceptable.\n")


def _workflow():
    return load_workflow_definition(PROJECT_ROOT)


def _scope_md(payload: dict) -> str:
    actionable = str(payload.get("actionable", True)).lower()
    return (
        "---\n"
        f"normalized_task: {payload.get('normalized_task', 'Implement feature')}\n"
        f"complexity_tier: {payload.get('complexity_tier', 'moderate')}\n"
        f"actionable: {actionable}\n"
        "key_files:\n"
        "  - README.md\n"
        f"context: {payload.get('blocking_reason') or 'Test scope.'}\n"
        "---\n\n"
        f"{payload.get('normalized_task', 'Implement feature')}\n"
    )


def _plan(*, plan_id: str = "11111111-1111-1111-1111-111111111111"):
    return {
        "plan_id": plan_id,
        "task": "Implement feature",
        "approach": "Two small sequential changes in one execution session.",
        "implementation_steps": [
            "Create the first file.",
            "Create the second file.",
        ],
        "key_files": ["README.md"],
    }


def _plan_markdown(plan: dict) -> str:
    steps = "\n".join(f"{idx}. {step}" for idx, step in enumerate(plan["implementation_steps"], start=1))
    key_files = "\n".join(f"- {path}" for path in plan.get("key_files", [])) or "- README.md"
    return (
        "## Approach\n"
        f"{plan['approach']}\n\n"
        "## Steps\n"
        f"{steps}\n\n"
        "## Key Files\n"
        f"{key_files}\n"
    )


def _review():
    return {
        "review_id": "22222222-2222-2222-2222-222222222222",
        "verdict": "approve",
        "score": 9,
        "findings": [],
        "summary": "Looks good.",
        "blocks_merge": False,
    }


def _review_with_issue():
    return {
        "review_id": "22222222-2222-2222-2222-222222222222",
        "verdict": "request_changes",
        "score": 5,
        "findings": [
            {
                "severity": "major",
                "file": "step-1.txt",
                "line": 1,
                "description": "Fix the generated content.",
                "suggestion": "Update step 1.",
            }
        ],
        "summary": "Needs a fix.",
        "blocks_merge": True,
    }


def _codex_approve_review():
    return {
        "review_id": "33333333-3333-3333-3333-333333333333",
        "verdict": "approve",
        "score": 9,
        "findings": [],
        "summary": "Ship it.",
        "blocks_merge": False,
    }


def _codex_request_changes_review(*, feedback: str = "Fix step 1."):
    return {
        "review_id": "44444444-4444-4444-4444-444444444444",
        "verdict": "request_changes",
        "score": 5,
        "findings": [
            {
                "severity": "major",
                "file": "step-1.txt",
                "line": 1,
                "description": feedback,
                "suggestion": "Address the Codex review finding.",
            }
        ],
        "summary": feedback,
        "blocks_merge": True,
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


def _run_case_b_to_tiebreaker(tmp_repo, artifact_root, default_config):
    default_config.approval.require_plan_approval = False
    default_config.approval.require_merge_approval = False
    claude = FakeClaudeAdapter(
        [
            _plan(plan_id="11111111-1111-1111-1111-111111111111"),
            _plan(plan_id="22222222-1111-1111-1111-111111111111"),
        ],
        [_review()],
        [],
        debate_responses=[
            {
                "position": "issues_dismissed",
                "reasoning": "Claude still sees no issue.",
                "issues": [],
            },
            {
                "position": "issues_dismissed",
                "reasoning": "Claude still disagrees after escalation.",
                "issues": [],
            },
        ],
    )
    codex = FakeCodexAdapter(
        reviews=[_codex_request_changes_review(feedback="Codex found a real issue.")],
        debate_responses=[
            {
                "position": "issues_confirmed",
                "reasoning": "Codex insists the issue blocks merge.",
                "issues": [{"severity": "major", "description": "Codex issue."}],
            }
        ],
    )
    engine = Engine(
        default_config,
        tmp_repo,
        artifact_root,
        adapters={"claude": claude, "codex": codex},
        workflow=_workflow(),
    )
    state = engine.start("Implement feature", "cbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
    return engine, state


def test_engine_happy_path(tmp_repo, artifact_root, default_config):
    default_config.approval.require_plan_approval = False
    default_config.approval.require_merge_approval = False
    claude = FakeClaudeAdapter([_plan()], [_review()], [_codex_approve_review()])
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
    assert codex.executed_steps == [1, 2]
    worktree = Path(state.worktree_path)
    assert (worktree / "step-1.txt").exists()
    assert (worktree / "step-2.txt").exists()
    assert len(state.step_results) == 1
    assert state.execution_result_ref == state.step_results[0]
    assert state.commit_commands[0] == "# Review changes on the worktree branch:"


def test_skip_review_transitions_execution_to_merge(tmp_repo, artifact_root, default_config):
    default_config.approval.require_plan_approval = False
    default_config.approval.require_merge_approval = False
    claude = FakeClaudeAdapter([_plan()], [_review()])
    codex = FakeCodexAdapter()
    engine = Engine(
        default_config,
        tmp_repo,
        artifact_root,
        adapters={"claude": claude, "codex": codex},
        workflow=_workflow(),
        skip_review=True,
    )

    state = engine.start(
        "Implement feature",
        "abababab-abab-4bab-8bab-abababababab",
        start_at="executing",
        plan=_plan(),
    )

    assert state.status == "DONE"
    assert codex.execution_calls == 1
    assert claude.review_calls == 0


def test_autonomous_limit_pauses_review_fix_loop(tmp_repo, artifact_root, default_config):
    engine = Engine(
        default_config,
        tmp_repo,
        artifact_root,
        adapters={"claude": FakeClaudeAdapter([], []), "codex": FakeCodexAdapter()},
        workflow=_workflow(),
        autonomous_max_iterations=1,
    )
    state = RunState(
        run_id="acacacac-acac-4cac-8cac-acacacacacac",
        task="Implement feature",
        status=WorkflowStatus.REVIEWING,
        current_phase=WorkflowStatus.REVIEWING.value,
        fix_iteration_count=1,
        debate_state=DebateState(
            debate_phase=ReviewDebatePhase.RESOLVED,
            final_verdict="fix",
            consolidated_issues=[{"severity": "major", "description": "Fix it."}],
        ),
    )

    paused = engine._debate_resolve_fix(state)

    assert paused.status == "PAUSED"
    assert paused.current_phase == "REVIEWING"
    assert paused.error == "Autonomous limit: 1 fix iterations"


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
            resume_session_id=None,
            allowed_tools=None,
        ):
            result = super().invoke(
                prompt,
                working_dir,
                timeout,
                schema,
                step_number=step_number,
                reasoning_effort_override=reasoning_effort_override,
                model_override=model_override,
                resume_session_id=resume_session_id,
                allowed_tools=allowed_tools,
            )
            if schema["title"] == "ExecutionResult":
                (working_dir / "step-1.txt").write_text('dummy_key = "changeme"\n', encoding="utf-8")
            return result

    claude = FakeClaudeAdapter([_plan()], [_review()], [_codex_approve_review()])
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
    assert "Inspect the worktree directly with your tools" in prompt
    assert "Return ONLY valid JSON" in prompt


def test_review_changed_files_failure_degrades_gracefully(tmp_repo, artifact_root, default_config, monkeypatch):
    default_config.approval.require_plan_approval = False
    default_config.approval.require_merge_approval = False
    claude = FakeClaudeAdapter([_plan()], [_review()], [_codex_approve_review()])
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
    assert "Review the plan implementation." in review_prompts[0]


def test_codex_review_prompt_uses_trimmed_context(tmp_repo, artifact_root, default_config):
    default_config.approval.require_plan_approval = False
    default_config.approval.require_merge_approval = False
    claude = FakeClaudeAdapter(
        [_plan()],
        [_review()],
        [_codex_approve_review()],
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
    codex_review_prompts = [item["prompt"] for item in codex.invocations if item["title"] == "Review"]
    assert codex_review_prompts
    assert "CLAUDE REVIEW REPORT:" in codex_review_prompts[0]
    assert "Task: Normalized implementation task" in codex_review_prompts[0]
    assert "Inspect the code directly with your tools" in codex_review_prompts[0]
    assert "review_id (uuid), verdict (approve|request_changes|reject), score (1-10)" in codex_review_prompts[0]
    assert "ORIGINAL TASK:" not in codex_review_prompts[0]


def test_scoping_debate_parallel_and_convergence(tmp_repo, artifact_root, default_config):
    default_config.approval.require_plan_approval = False
    default_config.approval.require_merge_approval = False
    claude = FakeClaudeAdapter([_plan()], [_review()], [_codex_approve_review()])
    codex = FakeCodexAdapter()
    engine = Engine(
        default_config,
        tmp_repo,
        artifact_root,
        adapters={"claude": claude, "codex": codex},
        workflow=_workflow(),
    )

    state = engine.start("Implement feature", "a1a1a1a1-a1a1-a1a1-a1a1-a1a1a1a1a1a1")

    assert state.status == "DONE"
    assert claude.scoping_calls == 1
    assert codex.scoping_calls == 2
    assert state.scoping_agreed is True
    assert state.scope_md_ref is not None
    assert claude.text_invocations[0]["model_override"] == "claude-sonnet-4-6"
    assert codex.text_invocations[0]["model_override"] == "gpt-5.4-mini"
    assert codex.text_invocations[0]["reasoning_effort_override"] == "medium"
    assert codex.text_invocations[1]["model_override"] == "gpt-5.4"
    assert codex.text_invocations[1]["reasoning_effort_override"] == "high"


def test_scoping_reuses_claude_session_and_passes_prescope_notes(
    tmp_repo,
    artifact_root,
    default_config,
):
    default_config.approval.require_plan_approval = False
    default_config.approval.require_merge_approval = False

    class DistinctScopeClaude(FakeClaudeAdapter):
        def invoke_text(
            self,
            prompt,
            working_dir,
            timeout,
            *,
            reasoning_effort_override=None,
            model_override=None,
            resume_session_id=None,
            allowed_tools=None,
        ):
            if "Scope the request for implementation across this project" in prompt:
                self.text_invocations.append(
                    {
                        "prompt": prompt,
                        "reasoning_effort_override": reasoning_effort_override,
                        "model_override": model_override,
                        "resume_session_id": resume_session_id,
                        "allowed_tools": allowed_tools,
                    }
                )
                self.scoping_calls += 1
                return TextInvokeResult("## Claude pre-scope\n\nUse the router notes.", "scope-session-1")
            return super().invoke_text(
                prompt,
                working_dir,
                timeout,
                reasoning_effort_override=reasoning_effort_override,
                model_override=model_override,
                resume_session_id=resume_session_id,
                allowed_tools=allowed_tools,
            )

    claude = DistinctScopeClaude([_plan()], [_review()], [_codex_approve_review()])
    codex = FakeCodexAdapter()
    engine = Engine(
        default_config,
        tmp_repo,
        artifact_root,
        adapters={"claude": claude, "codex": codex},
        workflow=_workflow(),
    )

    state = engine.start("Implement feature", "a3a3a3a3-a3a3-a3a3-a3a3-a3a3a3a3a3a3")

    assert state.status == "DONE"
    assert state.session_ids["scoping_claude"] == "scope-session-1"
    assert any("Scope the request for implementation across this project" in str(item["prompt"]) for item in claude.text_invocations)
    codex_review_prompt = str(codex.text_invocations[1]["prompt"])
    assert "I had another analysis of this task:\n\n## Claude pre-scope" in codex_review_prompt


def test_scoping_debate_max_rounds_proceeds_without_agreement(
    tmp_repo,
    artifact_root,
    default_config,
):
    default_config.approval.require_plan_approval = False
    default_config.approval.require_merge_approval = False
    class DisagreeingCodex(FakeCodexAdapter):
        def invoke_text(
            self,
            prompt,
            working_dir,
            timeout,
            *,
            reasoning_effort_override=None,
            model_override=None,
            resume_session_id=None,
            allowed_tools=None,
        ):
            self.text_invocations.append(
                {
                    "prompt": prompt,
                    "reasoning_effort_override": reasoning_effort_override,
                    "model_override": model_override,
                    "resume_session_id": resume_session_id,
                    "allowed_tools": allowed_tools,
                }
            )
            self.scoping_calls += 1
            if "Scope the request" in prompt:
                return "## Codex pre-scope\n\nThe task is implementable."
            return "---\nagreement: false\n---\n\nStill disagreeing.\n"

    claude = FakeClaudeAdapter([_plan()], [_review()], [_codex_approve_review()])
    codex = DisagreeingCodex()
    engine = Engine(
        default_config,
        tmp_repo,
        artifact_root,
        adapters={"claude": claude, "codex": codex},
        workflow=_workflow(),
    )

    state = engine.start("Implement feature", "a2a2a2a2-a2a2-a2a2-a2a2-a2a2a2a2a2a2")

    assert state.status == "DONE"
    assert codex.scoping_calls == 3
    assert state.scoping_agreed is False
    assert state.scope_md_ref is not None
    assert codex.text_invocations[2]["model_override"] == "gpt-5.4"
    assert codex.text_invocations[2]["reasoning_effort_override"] == "high"
    assert any(
        invocation["model_override"] == "claude-opus-4-6"
        and invocation["reasoning_effort_override"] == "xhigh"
        for invocation in claude.text_invocations
    )


def test_scope_review_agreement_requires_structured_marker():
    assert (
        Engine._scope_review_agreed(
            "---\nagreement: false\n---\n\nI agree with some points, but the scope is correct."
        )
        is False
    )
    assert Engine._scope_review_agreed("I agree with some points.") is False
    assert Engine._scope_review_agreed("agreement: true\n\nAccepted after review.") is True


def test_engine_approval_flow(tmp_repo, artifact_root, default_config):
    default_config.approval.require_plan_approval = True
    default_config.approval.require_merge_approval = True
    claude = FakeClaudeAdapter(
        [_plan(plan_id="aaaaaaaa-1111-1111-1111-111111111111"), _plan(plan_id="bbbbbbbb-1111-1111-1111-111111111111")],
        [_review(), _review()],
        [_codex_approve_review(), _codex_approve_review()],
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
    planning_calls = [item for item in claude.invocations if item["title"] == "Plan"]
    assert planning_calls[0]["resume_session_id"] == "scoping-claude-session"
    assert planning_calls[1]["resume_session_id"] == "planning-session"

    state = engine.approve(state.run_id, "plan")
    assert state.status == "DONE"


def test_unified_session_disabled_does_not_set_claude_main_or_resume_across_phases(
    tmp_repo, artifact_root, default_config
):
    default_config.approval.require_plan_approval = False
    default_config.approval.require_merge_approval = False
    default_config.sessions.enable_unified_session = False
    default_config.sessions.enable_planning_resume = True
    default_config.sessions.enable_review_resume = True
    claude = FakeClaudeAdapter([_plan()], [_review()], [_codex_approve_review()])
    codex = FakeCodexAdapter()
    engine = Engine(
        default_config,
        tmp_repo,
        artifact_root,
        adapters={"claude": claude, "codex": codex},
        workflow=_workflow(),
    )

    state = engine.start("Implement feature", "c1c1c1c1-c1c1-c1c1-c1c1-c1c1c1c1c1c1")

    assert state.status == "DONE"
    planning_call = next(call for call in claude.invocations if call["title"] == "Plan")
    review_call = next(call for call in claude.invocations if call["title"] == "Review")
    assert planning_call["resume_session_id"] is None
    assert review_call["resume_session_id"] is None
    assert "claude_main" not in state.session_ids
    assert state.commit_commands
    assert codex.review_calls == 1


def test_workspace_execution_prompt_omits_workspace_trees(tmp_path, artifact_root, default_config):
    default_config.approval.require_plan_approval = False
    default_config.approval.require_merge_approval = False
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    _init_repo(workspace_root / "frontend")
    _init_repo(workspace_root / "backend")

    claude = FakeClaudeAdapter([_plan()], [_review()], [_codex_approve_review()])
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
    execution_prompts = [item["prompt"] for item in codex.invocations if item["title"] == "ExecutionResult"]
    assert execution_prompts
    assert "FULLY IMPLEMENT THE PLAN ABOVE" in execution_prompts[0]
    assert "Workspace repos:" not in execution_prompts[0]
    assert "## frontend/" not in execution_prompts[0]
    assert "## backend/" not in execution_prompts[0]


def test_workspace_resume_allows_uncommitted_changes(tmp_path, artifact_root, default_config):
    default_config.approval.require_merge_approval = False
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    _init_repo(workspace_root / "frontend")
    _init_repo(workspace_root / "backend")
    (workspace_root / "frontend" / "dirty.txt").write_text("dirty\n", encoding="utf-8")

    run_id = "dddddddd-dddd-dddd-dddd-dddddddddddd"
    store = ArtifactStore(artifact_root)
    plan_ref = store.save_plan(run_id, _plan())
    state = RunState(
        run_id=run_id,
        task="Implement feature",
        status=WorkflowStatus.BLOCKED_ON_CLI,
        current_phase=WorkflowStatus.EXECUTING.value,
        is_workspace=True,
        workspace_repos=["frontend", "backend"],
    )
    state.plan_id = plan_ref
    StateManager(artifact_root).save(state)

    claude = FakeClaudeAdapter([_plan()], [_review()], [_codex_approve_review()])
    codex = FakeCodexAdapter()
    engine = Engine(
        default_config,
        workspace_root,
        artifact_root,
        adapters={"claude": claude, "codex": codex},
        workflow=_workflow(),
    )

    resumed = engine.resume(state.run_id)

    assert resumed.status == "DONE"
    assert codex.executed_steps == [1, 2]
    assert (workspace_root / "frontend" / "dirty.txt").read_text(encoding="utf-8") == "dirty\n"


def test_review_disagreement_can_resolve_to_pass(tmp_repo, artifact_root, default_config):
    default_config.approval.require_plan_approval = False
    default_config.approval.require_merge_approval = False
    claude = FakeClaudeAdapter(
        [_plan()],
        [_review(), _review()],
        [],
    )
    codex = FakeCodexAdapter(
        reviews=[
            _codex_request_changes_review(feedback="Adjust step 1."),
            _codex_request_changes_review(feedback="Adjust step 1 again."),
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
    assert state.status == "DONE"
    assert state.debate_state is not None
    assert state.debate_state.final_verdict == "pass"


def test_debate_case_a_claude_insists_triggers_fix(tmp_repo, artifact_root, default_config):
    default_config.approval.require_plan_approval = True
    default_config.approval.require_merge_approval = False
    claude = FakeClaudeAdapter(
        [_plan(plan_id="11111111-1111-1111-1111-111111111111"), _plan(plan_id="22222222-1111-1111-1111-111111111111")],
        [_review_with_issue()],
        [],
        debate_responses=[
            {
                "position": "issues_confirmed",
                "reasoning": "The review finding still blocks merge.",
                "issues": [{"severity": "major", "description": "Fix step 1."}],
            }
        ],
    )
    codex = FakeCodexAdapter(reviews=[_codex_approve_review()])
    engine = Engine(
        default_config,
        tmp_repo,
        artifact_root,
        adapters={"claude": claude, "codex": codex},
        workflow=_workflow(),
    )
    discard_phases = []
    original_discard = engine._discard_worktree

    def tracked_discard(state, *, force):
        discard_phases.append(state.current_phase)
        return original_discard(state, force=force)

    engine._discard_worktree = tracked_discard  # type: ignore[method-assign]

    paused = engine.start("Implement feature", "c1c1c1c1-c1c1-c1c1-c1c1-c1c1c1c1c1c1")
    assert paused.status == "PAUSED"

    state = engine.approve(paused.run_id, "plan")

    assert state.status == "PAUSED"
    assert state.current_phase == "APPROVAL_PLAN"
    assert state.fix_iteration_count == 1
    assert discard_phases == []


def test_debate_case_a_claude_convinced_passes(tmp_repo, artifact_root, default_config):
    default_config.approval.require_plan_approval = True
    default_config.approval.require_merge_approval = False
    claude = FakeClaudeAdapter(
        [_plan()],
        [_review_with_issue()],
        [],
        debate_responses=[
            {
                "position": "issues_dismissed",
                "reasoning": "Codex is right; the issue does not block.",
                "issues": [],
            }
        ],
    )
    codex = FakeCodexAdapter(reviews=[_codex_approve_review()])
    engine = Engine(
        default_config,
        tmp_repo,
        artifact_root,
        adapters={"claude": claude, "codex": codex},
        workflow=_workflow(),
    )

    paused = engine.start("Implement feature", "c2c2c2c2-c2c2-c2c2-c2c2-c2c2c2c2c2c2")
    state = engine.approve(paused.run_id, "plan")

    assert state.status == "DONE"
    assert state.debate_state.final_verdict == "pass"


def test_review_disagreement_final_claude_decision_passes(tmp_repo, artifact_root, default_config):
    engine, state = _run_case_b_to_tiebreaker(tmp_repo, artifact_root, default_config)

    assert state.status == "DONE"
    assert state.debate_state is not None
    assert state.debate_state.debate_phase == "resolved"
    assert state.debate_state.disagreement_case == "D: Claude passed the implementation and Codex found blocking issues."
    assert state.debate_state.final_verdict == "pass"
    assert len(state.debate_state.rounds) == 3
    assert state.debate_state.rounds[1].model_used == "gpt-5.4"
    assert state.debate_state.rounds[2].model_used == "claude-opus-4-6"
    assert state.debate_state.rounds[2].effort_used == "high"
    assert engine is not None


def test_debate_tiebreaker_gate_is_removed_in_engine(tmp_repo, artifact_root, default_config):
    engine, state = _run_case_b_to_tiebreaker(tmp_repo, artifact_root, default_config)

    with pytest.raises(KeyError):
        engine.approve(state.run_id, "debate_tiebreaker", decision="fix")


def test_review_fix_planning_preserves_existing_step_results(
    tmp_repo,
    artifact_root,
    default_config,
):
    default_config.approval.require_plan_approval = False
    default_config.approval.require_merge_approval = False
    claude = FakeClaudeAdapter(
        [_plan(plan_id="11111111-1111-1111-1111-111111111111"), _plan(plan_id="22222222-1111-1111-1111-111111111111")],
        [_review_with_issue(), _review()],
        [_codex_approve_review()],
    )
    codex = FakeCodexAdapter(
        reviews=[
            _codex_request_changes_review(feedback="Fix the generated content."),
            _codex_approve_review(),
        ]
    )
    engine = Engine(
        default_config,
        tmp_repo,
        artifact_root,
        adapters={"claude": claude, "codex": codex},
        workflow=_workflow(),
    )
    discard_phases = []
    original_discard = engine._discard_worktree

    def tracked_discard(state, *, force):
        discard_phases.append(state.current_phase)
        return original_discard(state, force=force)

    engine._discard_worktree = tracked_discard  # type: ignore[method-assign]

    state = engine.start("Implement feature", "cfcfcfcf-cfcf-cfcf-cfcf-cfcfcfcfcfcf")

    assert state.status == "DONE"
    assert state.fix_iteration_count == 1
    assert len(state.step_results) == 1
    assert discard_phases == []


def test_review_session_id_stored_and_reused(tmp_repo, artifact_root, default_config):
    default_config.approval.require_plan_approval = False
    default_config.approval.require_merge_approval = False
    claude = FakeClaudeAdapter(
        [_plan(plan_id="11111111-1111-1111-1111-111111111111"), _plan(plan_id="22222222-1111-1111-1111-111111111111")],
        [_review_with_issue(), _review()],
        [],
    )
    codex = FakeCodexAdapter(reviews=[_codex_request_changes_review(), _codex_approve_review()])
    engine = Engine(
        default_config,
        tmp_repo,
        artifact_root,
        adapters={"claude": claude, "codex": codex},
        workflow=_workflow(),
    )

    state = engine.start("Implement feature", "c8c8c8c8-c8c8-c8c8-c8c8-c8c8c8c8c8c8")

    review_invocations = [item for item in claude.invocations if item["title"] == "Review"]
    assert state.status == "DONE"
    assert state.session_ids["reviewing"] == "review-session"
    assert len(review_invocations) == 1
    assert review_invocations[0]["resume_session_id"] == "planning-session"


def test_codex_review_uses_fresh_session(tmp_repo, artifact_root, default_config):
    default_config.approval.require_plan_approval = False
    default_config.approval.require_merge_approval = False

    class SessionCodex(FakeCodexAdapter):
        def invoke_text(
            self,
            prompt,
            working_dir,
            timeout,
            *,
            reasoning_effort_override=None,
            model_override=None,
            resume_session_id=None,
            allowed_tools=None,
        ):
            result = super().invoke_text(
                prompt,
                working_dir,
                timeout,
                reasoning_effort_override=reasoning_effort_override,
                model_override=model_override,
                resume_session_id=resume_session_id,
                allowed_tools=allowed_tools,
            )
            return TextInvokeResult(result.text, session_id="codex-scope-thread")

    claude = FakeClaudeAdapter([_plan()], [_review()], [_codex_approve_review()])
    codex = SessionCodex()
    engine = Engine(
        default_config,
        tmp_repo,
        artifact_root,
        adapters={"claude": claude, "codex": codex},
        workflow=_workflow(),
    )

    state = engine.start("Implement feature", "abab1212-abab-1212-abab-1212abab1212")

    codex_review_invocations = [item for item in codex.invocations if item["title"] == "Review"]
    assert state.status == "DONE"
    assert codex_review_invocations
    assert codex_review_invocations[0]["resume_session_id"] is None
    assert state.session_ids["scoping_codex"] == "codex-review-session"


def test_resume_from_executing_runs_full_plan(tmp_repo, artifact_root, default_config):
    default_config.approval.require_plan_approval = False
    default_config.approval.require_merge_approval = False
    run_id = "dddddddd-dddd-dddd-dddd-dddddddddddd"
    state_mgr = StateManager(artifact_root)
    store = ArtifactStore(artifact_root)
    plan_ref = store.save_plan(run_id, _plan())
    from ai_orchestrator.worktree import WorktreeManager

    worktrees = WorktreeManager(tmp_repo, artifact_root)
    worktree_path, branch_name, base_commit = worktrees.create(
        run_id,
        default_config.worktree.base_branch,
        default_config.worktree.branch_prefix,
    )

    state = RunState(run_id=run_id, task="Implement feature")
    state.status = "EXECUTING"
    state.current_phase = "EXECUTING"
    state.plan_id = plan_ref
    state.worktree_path = str(worktree_path)
    state.worktree_branch = branch_name
    state.base_commit = base_commit
    state_mgr.save(state)

    claude = FakeClaudeAdapter([_plan()], [_review()], [_codex_approve_review()])
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
    assert codex.executed_steps == [1, 2]


def test_engine_retries_when_step_reports_failed_status(tmp_repo, artifact_root, default_config):
    default_config.approval.require_plan_approval = False
    default_config.approval.require_merge_approval = False
    claude = FakeClaudeAdapter([_plan()], [_review()], [_codex_approve_review()])

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
            if schema["title"] == "Review":
                return _review()
            self.calls.append(1)
            self.prompts.append(prompt)
            target = working_dir / "step-1.txt"
            target.write_text("step 1\n", encoding="utf-8")
            if self.failures == 0:
                self.failures += 1
                return {
                    "status": "failed",
                    "files_changed": [],
                    "summary": "Required file was not ready yet.",
                    "issues": ["retry requested"],
                    "test_commands": [],
                }
            return {
                "status": "success",
                "files_changed": [
                    {
                        "path": target.name,
                        "action": "created",
                        "summary": f"Created {target.name}",
                    }
                ],
                "summary": "Implemented the full plan",
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
    assert codex.calls == [1, 1]
    assert state.retry_counts["execution"] == 0
    assert "FULLY IMPLEMENT THE PLAN ABOVE" in codex.prompts[1]
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
            if title == "Review":
                return _review()
            raise AssertionError(f"Unexpected schema title: {title}")

        def invoke_text(
            self,
            prompt,
            working_dir,
            timeout,
            *,
            reasoning_effort_override=None,
            model_override=None,
            resume_session_id=None,
            allowed_tools=None,
        ):
            self.prompts.append(prompt)
            if "Plan for the implementation of the task below:" in prompt:
                self._plan_attempts += 1
                if self._plan_attempts == 1:
                    raise StepFailure("invalid plan", validation_error="missing task context")
                return TextInvokeResult(_plan_markdown(_plan()), session_id="planning-session")
            if "Scope the request for implementation across this project" in prompt:
                return TextInvokeResult(_scope_md({
                    "actionable": True,
                    "normalized_task": "Implement feature",
                    "assumptions": [],
                    "complexity_tier": "moderate",
                }))
            raise AssertionError(f"Unexpected text prompt: {prompt[:80]}")

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
    assert "Plan for the implementation of the task below:\nImplement feature" in retry_prompt
    assert "SCOPE:" in retry_prompt
    assert "missing task context" in retry_prompt


def test_worktree_reset_before_retry(tmp_repo, artifact_root, default_config):
    default_config.approval.require_plan_approval = False
    default_config.approval.require_merge_approval = False
    claude = FakeClaudeAdapter([_plan()], [_review()], [_codex_approve_review()])

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
            if schema["title"] == "Review":
                return _review()
            self.calls += 1
            if self.calls == 1:
                (working_dir / "leftover.txt").write_text("stale\n", encoding="utf-8")
                (working_dir / "README.md").write_text("dirty\n", encoding="utf-8")
                raise StepFailure("execution failed", validation_error="retry requested")

            assert not (working_dir / "leftover.txt").exists()
            assert (working_dir / "README.md").read_text(encoding="utf-8") == "# Test repo\n"
            target = working_dir / "step-1.txt"
            target.write_text("step 1\n", encoding="utf-8")
            return {
                "status": "success",
                "files_changed": [
                    {
                        "path": target.name,
                        "action": "created",
                        "summary": f"Created {target.name}",
                    }
                ],
                "summary": "Implemented the full plan",
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
    assert codex.calls == 2


def test_worktree_reset_clears_staged_index_changes(tmp_repo, artifact_root, default_config):
    default_config.approval.require_plan_approval = False
    default_config.approval.require_merge_approval = False
    claude = FakeClaudeAdapter([_plan()], [_review()], [_codex_approve_review()])

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
            if schema["title"] == "Review":
                return _review()
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
            target = working_dir / "step-1.txt"
            target.write_text("step 1\n", encoding="utf-8")
            return {
                "status": "success",
                "files_changed": [
                    {
                        "path": target.name,
                        "action": "created",
                        "summary": f"Created {target.name}",
                    }
                ],
                "summary": "Implemented the full plan",
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
    assert codex.calls == 2


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

    claude = FakeClaudeAdapter([_plan()], [_review()], [_codex_approve_review()])
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
    assert resumed.current_phase == "EXECUTING"


def test_resume_paused_without_decision_re_pauses(tmp_repo, artifact_root, default_config):
    default_config.approval.require_plan_approval = True
    default_config.approval.require_merge_approval = False
    claude = FakeClaudeAdapter([_plan()], [_review()], [_codex_approve_review()])
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


def test_resume_failed_scoping_re_enters_scoping(tmp_repo, artifact_root, default_config):
    default_config.approval.require_plan_approval = False
    default_config.approval.require_merge_approval = False
    state_mgr = StateManager(artifact_root)
    state_mgr.save(
        RunState(
            run_id="f1f1f1f1-f1f1-f1f1-f1f1-f1f1f1f1f1f1",
            task="Implement feature",
            status=WorkflowStatus.FAILED,
            current_phase=WorkflowStatus.SCOPING.value,
            error="scoping failed",
        )
    )
    claude = FakeClaudeAdapter([_plan()], [_review()], [_codex_approve_review()])
    codex = FakeCodexAdapter()
    engine = Engine(
        default_config,
        tmp_repo,
        artifact_root,
        adapters={"claude": claude, "codex": codex},
        workflow=_workflow(),
    )

    resumed = engine.resume("f1f1f1f1-f1f1-f1f1-f1f1-f1f1f1f1f1f1")

    assert resumed.status == "DONE"
    assert claude.scoping_calls >= 1


def test_resume_failed_reviewing_at_codex_review_skips_claude_review(
    tmp_repo,
    artifact_root,
    default_config,
):
    default_config.approval.require_plan_approval = False
    default_config.approval.require_merge_approval = False
    run_id = "f2f2f2f2-f2f2-f2f2-f2f2-f2f2f2f2f2f2"
    store = ArtifactStore(artifact_root)
    execution_ref = store.save_execution_result(
        run_id,
        {
            "status": "success",
            "files_changed": [{"path": "step-1.txt", "action": "created", "summary": "Created step 1."}],
            "summary": "Implemented.",
            "issues": [],
            "test_commands": [],
            "workspace_diffs": {"repo": "diff --git a/step-1.txt b/step-1.txt\n"},
        },
    )
    review_ref = store.save_review(run_id, _review_with_issue())
    StateManager(artifact_root).save(
        RunState(
            run_id=run_id,
            task="Implement feature",
            status=WorkflowStatus.FAILED,
            current_phase=WorkflowStatus.REVIEWING.value,
            normalized_task="Implement feature",
            execution_result_ref=execution_ref,
            step_results=[execution_ref],
            review_id=review_ref,
            retry_counts={"reviewing-codex": 3},
            debate_state=DebateState(debate_phase=ReviewDebatePhase.CROSS_REVIEW),
            is_workspace=True,
            error="'verdict' is a required property",
        )
    )
    claude = FakeClaudeAdapter([_plan()], [], [])
    codex = FakeCodexAdapter(reviews=[_codex_approve_review()])
    engine = Engine(
        default_config,
        tmp_repo,
        artifact_root,
        adapters={"claude": claude, "codex": codex},
        workflow=_workflow(),
    )

    resumed = engine.resume(run_id)

    assert resumed.status == "DONE"
    assert claude.review_calls == 0
    assert codex.review_calls == 1
    assert codex.invocations[0]["resume_session_id"] is None


def test_resume_failed_reviewing_without_worktree_falls_back_to_executing(
    tmp_repo,
    artifact_root,
    default_config,
):
    default_config.approval.require_plan_approval = False
    default_config.approval.require_merge_approval = False
    run_id = "f3f3f3f3-f3f3-f3f3-f3f3-f3f3f3f3f3f3"
    store = ArtifactStore(artifact_root)
    plan_ref = store.save_plan_md(run_id, _plan_markdown(_plan()))
    StateManager(artifact_root).save(
        RunState(
            run_id=run_id,
            task="Implement feature",
            status=WorkflowStatus.FAILED,
            current_phase=WorkflowStatus.REVIEWING.value,
            normalized_task="Implement feature",
            complexity_tier="moderate",
            plan_id=plan_ref,
            error="review failed",
        )
    )
    claude = FakeClaudeAdapter([_plan()], [_review()], [_codex_approve_review()])
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
    assert codex.execution_calls == 1


def test_failed_resume_clears_retry_counters_for_phase():
    state = RunState(
        run_id="f4f4f4f4-f4f4-f4f4-f4f4-f4f4f4f4f4f4",
        task="Implement feature",
        retry_counts={
            "reviewing": 3,
            "reviewing-codex": 3,
            "review-final-claude": 3,
            "planning": 2,
        },
    )

    Engine._clear_retry_counts_for_phase(state, WorkflowStatus.REVIEWING)

    assert state.retry_counts == {"planning": 2}


def test_reset_worktree_failure_raises_engine_error(tmp_repo, artifact_root, default_config):
    default_config.approval.require_plan_approval = False
    default_config.approval.require_merge_approval = False
    claude = FakeClaudeAdapter([_plan()], [_review()], [_codex_approve_review()])

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
        [_codex_approve_review()],
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
    claude = FakeClaudeAdapter([_plan()], [_review()], [_codex_approve_review()])
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


def test_complexity_drives_reasoning_effort_and_phase_override_wins(tmp_repo, artifact_root, default_config):
    default_config.approval.require_plan_approval = False
    default_config.approval.require_merge_approval = False
    default_config.routing.phases["reviewing"] = PhaseRoutingOverride(reasoning_effort="max")
    claude = FakeClaudeAdapter(
        [_plan()],
        [_review()],
        [_codex_approve_review()],
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
    execute_call = next(call for call in codex.invocations if call["title"] == "ExecutionResult")
    assert planning_call["reasoning_effort_override"] == "xhigh"
    assert execute_call["reasoning_effort_override"] == "high"
    assert review_call["reasoning_effort_override"] == "max"


def test_resolve_execution_settings_reflects_overrides(tmp_repo, artifact_root, default_config):
    claude = FakeClaudeAdapter([_plan()], [_review()], [_codex_approve_review()])
    codex = FakeCodexAdapter()
    engine = Engine(
        default_config,
        tmp_repo,
        artifact_root,
        adapters={"claude": claude, "codex": codex},
        workflow=_workflow(),
    )
    state = RunState(run_id="39393939-3939-3939-3939-393939393939", task="Implement feature")
    state.complexity_tier = "complex"
    state.execution_overrides = {
        "cli": "claude",
        "model": "claude-opus-4-6",
        "effort": "max",
    }

    settings = engine.resolve_execution_settings(state)

    assert settings["cli"] == "claude"
    assert settings["model"] == "claude-opus-4-6"
    assert settings["effort"] == "max"
    assert settings["complexity_tier"] == "complex"
    assert settings["has_overrides"] is True


def test_execution_model_and_effort_overrides_apply(tmp_repo, artifact_root, default_config):
    default_config.approval.require_plan_approval = True
    default_config.approval.require_merge_approval = False
    claude = FakeClaudeAdapter([_plan()], [_review()], [_codex_approve_review()])
    codex = FakeCodexAdapter()
    engine = Engine(
        default_config,
        tmp_repo,
        artifact_root,
        adapters={"claude": claude, "codex": codex},
        workflow=_workflow(),
    )

    state = engine.start("Implement feature", "59595959-5959-5959-5959-595959595959")
    assert state.status == "PAUSED"
    assert state.current_phase == "APPROVAL_PLAN"

    state.execution_overrides = {"model": "gpt-5.4-mini", "effort": "max"}
    StateManager(artifact_root).save(state)
    final = engine.approve(state.run_id, "plan")

    assert final.status == "DONE"
    execute_call = next(call for call in codex.invocations if call["title"] == "ExecutionResult")
    assert execute_call["model_override"] == "gpt-5.4-mini"
    assert execute_call["reasoning_effort_override"] == "max"


def test_retry_error_message_adds_required_property_guidance_for_review():
    message = Engine._retry_error_message("reviewing", "'verdict' is a required property")
    assert "required property is missing" in message
    assert "verdict and score at minimum" in message


def test_step_failure_stderr_surfaces_in_failed_run_error(tmp_repo, artifact_root, default_config):
    default_config.approval.require_plan_approval = False
    default_config.approval.require_merge_approval = False

    class FailingClaudeAdapter(FakeClaudeAdapter):
        def invoke_text(
            self,
            prompt,
            working_dir,
            timeout,
            *,
            reasoning_effort_override=None,
            model_override=None,
            resume_session_id=None,
            allowed_tools=None,
        ):
            raise StepFailure(
                "Claude CLI exited with a non-zero status",
                exit_code=2,
                stderr="error: unknown option '--effort'",
            )

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
            resume_session_id=None,
            allowed_tools=None,
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
                resume_session_id=resume_session_id,
                allowed_tools=allowed_tools,
            )

    engine = Engine(
        default_config,
        tmp_repo,
        artifact_root,
        adapters={
            "claude": FailingClaudeAdapter([_plan()], [_review()], [_codex_approve_review()]),
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


def test_step_failure_stdout_json_errors_surface_in_failed_run_error(
    tmp_repo, artifact_root, default_config
):
    default_config.approval.require_plan_approval = False
    default_config.approval.require_merge_approval = False

    class FailingClaudeAdapter(FakeClaudeAdapter):
        def invoke_text(
            self,
            prompt,
            working_dir,
            timeout,
            *,
            reasoning_effort_override=None,
            model_override=None,
            resume_session_id=None,
            allowed_tools=None,
        ):
            raise StepFailure(
                "Claude CLI exited with a non-zero status",
                exit_code=1,
                stdout=json.dumps(
                    {
                        "type": "result",
                        "subtype": "error_max_turns",
                        "is_error": True,
                        "result": "Reached maximum number of turns (1)",
                    }
                ),
            )

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
            resume_session_id=None,
            allowed_tools=None,
        ):
            if schema["title"] == "TaskDefinition":
                raise StepFailure(
                    "Claude CLI exited with a non-zero status",
                    exit_code=1,
                    stdout=json.dumps(
                        {
                            "type": "result",
                            "subtype": "error_max_turns",
                            "is_error": True,
                            "result": "Reached maximum number of turns (1)",
                        }
                    ),
                )
            return super().invoke(
                prompt,
                working_dir,
                timeout,
                schema,
                step_number=step_number,
                reasoning_effort_override=reasoning_effort_override,
                model_override=model_override,
                resume_session_id=resume_session_id,
                allowed_tools=allowed_tools,
            )

    engine = Engine(
        default_config,
        tmp_repo,
        artifact_root,
        adapters={
            "claude": FailingClaudeAdapter([_plan()], [_review()], [_codex_approve_review()]),
            "codex": FakeCodexAdapter(),
        },
        workflow=_workflow(),
    )

    state = engine.start("Implement feature", "4d4d4d4d-4d4d-4d4d-4d4d-4d4d4d4d4d4d")

    assert state.status == "FAILED"
    assert state.current_phase == "SCOPING"
    assert state.error is not None
    assert "stdout_errors: Reached maximum number of turns (1)" in state.error
    assert "exit_code: 1" in state.error
