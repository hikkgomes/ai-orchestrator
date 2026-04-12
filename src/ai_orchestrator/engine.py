"""Workflow engine and finite state machine."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from .adapters.base import BlockedOnCLI, StepFailure
from .adapters.claude import ClaudeAdapter
from .adapters.codex import CodexAdapter
from .artifacts import ArtifactStore
from .bootstrap import ensure_runtime_gitignore
from .config import Config
from .models import RunState, WorkflowStatus
from .prompts.templates import (
    build_adjudication_prompt,
    build_execution_prompt_claude,
    build_execution_prompt_codex,
    build_feasibility_prompt_claude,
    build_feasibility_prompt_codex,
    build_planning_prompt,
    build_scoping_prompt,
    build_retry_prompt,
    build_review_prompt,
    collect_file_context,
    default_planning_files,
    json_block,
    redact_secret_text,
    render_directory_tree,
    repo_summary,
)
from .reviewer import load_config as load_reviewer_config
from .reviewer import load_rules as load_reviewer_rules
from .reviewer import run_review_scan
from .state import StateManager
from .validator import ValidationError, Validator, load_bundled_schema
from .workflow import WorkflowDefinition, load_workflow_definition
from .worktree import WorktreeError, WorktreeManager


TRANSITIONS: dict[WorkflowStatus, set[WorkflowStatus]] = {
    WorkflowStatus.INIT: {
        WorkflowStatus.SCOPING,
        WorkflowStatus.PLANNING,
        WorkflowStatus.FAILED,
    },
    WorkflowStatus.SCOPING: {
        WorkflowStatus.PLANNING,
        WorkflowStatus.PAUSED,
        WorkflowStatus.FAILED,
        WorkflowStatus.BLOCKED_ON_CLI,
    },
    WorkflowStatus.PLANNING: {
        WorkflowStatus.APPROVAL_PLAN,
        WorkflowStatus.FEASIBILITY,
        WorkflowStatus.EXECUTING,
        WorkflowStatus.FAILED,
        WorkflowStatus.BLOCKED_ON_CLI,
    },
    WorkflowStatus.APPROVAL_PLAN: {
        WorkflowStatus.FEASIBILITY,
        WorkflowStatus.EXECUTING,
        WorkflowStatus.PLANNING,
        WorkflowStatus.PAUSED,
    },
    WorkflowStatus.FEASIBILITY: {
        WorkflowStatus.EXECUTING,
        WorkflowStatus.PLANNING,
        WorkflowStatus.FAILED,
        WorkflowStatus.BLOCKED_ON_CLI,
    },
    WorkflowStatus.EXECUTING: {
        WorkflowStatus.REVIEWING,
        WorkflowStatus.FAILED,
        WorkflowStatus.BLOCKED_ON_CLI,
        WorkflowStatus.PAUSED,
    },
    WorkflowStatus.REVIEWING: {
        WorkflowStatus.ADJUDICATING,
        WorkflowStatus.FAILED,
        WorkflowStatus.BLOCKED_ON_CLI,
    },
    WorkflowStatus.ADJUDICATING: {
        WorkflowStatus.MERGING,
        WorkflowStatus.EXECUTING,
        WorkflowStatus.PLANNING,
        WorkflowStatus.FAILED,
        WorkflowStatus.BLOCKED_ON_CLI,
    },
    WorkflowStatus.MERGING: {
        WorkflowStatus.DONE,
        WorkflowStatus.CONFLICT,
        WorkflowStatus.FAILED,
        WorkflowStatus.PAUSED,
    },
    WorkflowStatus.DONE: set(),
    WorkflowStatus.FAILED: set(),
    WorkflowStatus.PAUSED: {
        WorkflowStatus.SCOPING,
        WorkflowStatus.APPROVAL_PLAN,
    },
    WorkflowStatus.BLOCKED_ON_CLI: {
        WorkflowStatus.SCOPING,
        WorkflowStatus.PLANNING,
        WorkflowStatus.FEASIBILITY,
        WorkflowStatus.EXECUTING,
        WorkflowStatus.REVIEWING,
        WorkflowStatus.ADJUDICATING,
    },
    WorkflowStatus.CONFLICT: {WorkflowStatus.MERGING},
}


class EngineError(Exception):
    """Raised for invalid state transitions or unrecoverable engine errors."""


class Engine:
    """Finite state machine that drives a run end-to-end."""

    def __init__(
        self,
        config: Config,
        repo_root: Path,
        artifact_root: Path,
        *,
        adapters: dict[str, Any] | None = None,
        workflow: WorkflowDefinition | None = None,
        ui: Any | None = None,
    ) -> None:
        self._config = config
        self._repo_root = repo_root.resolve()
        self._artifact_root = artifact_root
        ensure_runtime_gitignore(self._repo_root)
        self._state_mgr = StateManager(artifact_root)
        self._artifacts = ArtifactStore(
            artifact_root,
            retain_prompts=config.logging.retain_prompts,
        )
        self._worktrees = WorktreeManager(self._repo_root, artifact_root)
        self._workflow = workflow or load_workflow_definition(self._repo_root)
        self._adapters = adapters or {}
        self._ui = ui

    def start(
        self,
        task: str,
        run_id: str,
        *,
        is_workspace: bool = False,
        workspace_repos: list[str] | None = None,
    ) -> RunState:
        state = RunState(
            run_id=run_id,
            task=task,
            is_workspace=is_workspace,
            workspace_repos=list(workspace_repos or []),
        )
        self._state_mgr.save(state)
        if self._config.scoping.enabled:
            state = self._transition(state, WorkflowStatus.SCOPING)
        else:
            state = self._transition(state, WorkflowStatus.PLANNING)
        return self._run(state)

    def resume(self, run_id: str) -> RunState:
        state = self._state_mgr.load(run_id)
        status = WorkflowStatus(state.status)
        if status in {WorkflowStatus.DONE, WorkflowStatus.FAILED}:
            raise EngineError(f"Run {run_id} is not resumable from {status.value}")
        if state.is_workspace and state.current_phase in {
            WorkflowStatus.EXECUTING.value,
            WorkflowStatus.REVIEWING.value,
            WorkflowStatus.ADJUDICATING.value,
        }:
            self._check_workspace_clean(state)
        if status == WorkflowStatus.PAUSED:
            state = self._transition(
                state,
                WorkflowStatus(state.current_phase),
                current_phase=state.current_phase,
                error=None,
            )
            return self._run(state)
        if status == WorkflowStatus.BLOCKED_ON_CLI:
            state = self._transition(
                state,
                WorkflowStatus(state.current_phase),
                current_phase=state.current_phase,
                error=None,
            )
        elif status == WorkflowStatus.CONFLICT:
            state = self._transition(state, WorkflowStatus.MERGING)
        return self._run(state)

    def approve(self, run_id: str, gate: str, *, force: bool = False) -> RunState:
        state = self._state_mgr.load(run_id)
        gate_phase = self._gate_phase(gate)
        if WorkflowStatus(state.status) != WorkflowStatus.PAUSED or state.current_phase != gate_phase:
            raise EngineError(f"Run {run_id} is not paused at the {gate} gate")
        if gate == "scope":
            state.error = None
            self._state_mgr.save(state)
            state = self._transition(
                state,
                WorkflowStatus.PLANNING,
                current_phase=WorkflowStatus.PLANNING.value,
                error=None,
            )
            return self._run(state)
        self._artifacts.save_approval_decision(run_id, gate, "approve", force=force)
        state = self._transition(
            state,
            WorkflowStatus(gate_phase),
            current_phase=gate_phase,
            error=None,
        )
        return self._run(state)

    def reject(self, run_id: str, gate: str, reason: str) -> RunState:
        state = self._state_mgr.load(run_id)
        gate_phase = self._gate_phase(gate)
        if WorkflowStatus(state.status) != WorkflowStatus.PAUSED or state.current_phase != gate_phase:
            raise EngineError(f"Run {run_id} is not paused at the {gate} gate")
        if gate == "scope":
            state.task = reason
            state.normalized_task = None
            state.complexity_tier = None
            state.error = None
            self._state_mgr.save(state)
            state = self._transition(
                state,
                WorkflowStatus.SCOPING,
                current_phase=WorkflowStatus.SCOPING.value,
                error=None,
            )
            return self._run(state)
        self._artifacts.save_approval_decision(run_id, gate, "reject", reason=reason)
        state = self._transition(
            state,
            WorkflowStatus(gate_phase),
            current_phase=gate_phase,
            error=None,
        )
        return self._run(state)

    def _run(self, state: RunState) -> RunState:
        while True:
            status = WorkflowStatus(state.status)
            if status in {
                WorkflowStatus.DONE,
                WorkflowStatus.FAILED,
                WorkflowStatus.PAUSED,
                WorkflowStatus.BLOCKED_ON_CLI,
                WorkflowStatus.CONFLICT,
            }:
                return state

            if status == WorkflowStatus.SCOPING:
                state = self._run_scoping(state)
                continue
            if status == WorkflowStatus.PLANNING:
                state = self._run_planning(state)
                continue
            if status == WorkflowStatus.APPROVAL_PLAN:
                state = self._handle_plan_approval(state)
                continue
            if status == WorkflowStatus.FEASIBILITY:
                state = self._run_feasibility(state)
                continue
            if status == WorkflowStatus.EXECUTING:
                state = self._run_execution(state)
                continue
            if status == WorkflowStatus.REVIEWING:
                state = self._run_review(state)
                continue
            if status == WorkflowStatus.ADJUDICATING:
                state = self._run_adjudication(state)
                continue
            if status == WorkflowStatus.MERGING:
                state = self._run_merge(state)
                continue

            raise EngineError(f"Unhandled engine status: {status.value}")

    def _run_scoping(self, state: RunState) -> RunState:
        schema = load_bundled_schema("scoping.schema.json")
        prompt = build_scoping_prompt(
            raw_task=state.task,
            repo_summary=repo_summary(self._repo_root),
            directory_tree=render_directory_tree(self._repo_root, max_depth=2),
            workspace_trees=self._workspace_trees(state, max_depth=2),
            schema_json=json_block(schema),
        )
        self._artifacts.save_prompt(f"scoping-{state.run_id[:8]}.md", prompt)

        adapter = self._adapter_for_phase("scoping")
        cli_name = self._phase_cli("scoping", config_name="scoper")
        try:
            result = self._invoke_with_retries(
                state,
                retry_key="scoping",
                retries=self._retry_limit("scoping"),
                spinner_label="Scoping",
                invoke=lambda current_prompt: adapter.invoke(
                    current_prompt,
                    self._repo_root,
                    self._config.orchestrator.watchdog_timeout,
                    schema,
                    reasoning_effort_override=self._resolve_effort_for_phase(
                        state,
                        "scoping",
                        cli_name,
                    ),
                    model_override=self._resolve_model_for_phase("scoping", cli_name),
                ),
                initial_prompt=prompt,
            )
        except BlockedOnCLI as exc:
            return self._transition(
                state,
                WorkflowStatus.BLOCKED_ON_CLI,
                current_phase=WorkflowStatus.SCOPING.value,
                error=str(exc),
            )
        except StepFailure as exc:
            return self._fail_run(state, self._format_step_failure(exc))

        state.normalized_task = result["normalized_task"]
        state.complexity_tier = result["complexity_tier"]
        state.error = None
        self._state_mgr.save(state)

        if result["actionable"]:
            return self._transition(state, WorkflowStatus.PLANNING)
        return self._transition(
            state,
            WorkflowStatus.PAUSED,
            current_phase=WorkflowStatus.SCOPING.value,
            error=str(result.get("blocking_reason") or "Task requires scoping review"),
        )

    def _run_planning(self, state: RunState) -> RunState:
        schema = load_bundled_schema("plan.schema.json")
        task_description = state.normalized_task or state.task
        feedback_parts = []

        planning_feedback = self._artifacts.load_feedback(state.run_id, "planning")
        if planning_feedback:
            feedback_parts.append(f"Planning feedback:\n{planning_feedback}")
        if state.adjudication_id:
            adjudication = self._artifacts.read_json(state.adjudication_id)
            if adjudication.get("verdict") == "REPLAN" and adjudication.get("replan_feedback"):
                feedback_parts.append(
                    f"Replan feedback:\n{adjudication['replan_feedback']}"
                )
        if feedback_parts:
            task_description = (
                task_description
                + self._workspace_feedback_prefix(state)
                + "\n\nADDITIONAL FEEDBACK:\n"
                + "\n\n".join(feedback_parts)
            )

        prompt = build_planning_prompt(
            task_description=task_description,
            directory_tree=render_directory_tree(self._repo_root),
            workspace_trees=self._workspace_trees(state, max_depth=2),
            key_file_contents=collect_file_context(
                self._repo_root,
                default_planning_files(self._repo_root),
            )[0],
            schema_json=json_block(schema),
        )
        self._artifacts.save_prompt(f"planning-{state.run_id[:8]}.md", prompt)

        adapter = self._adapter_for_phase("planning")
        try:
            result = self._invoke_with_retries(
                state,
                retry_key="planning",
                retries=self._retry_limit("planning"),
                spinner_label="Planning",
                invoke=lambda current_prompt: adapter.invoke(
                    current_prompt,
                    self._repo_root,
                    self._config.orchestrator.watchdog_timeout,
                    schema,
                    reasoning_effort_override=self._resolve_effort_for_phase(
                        state,
                        "planning",
                        self._phase_cli("planning", config_name="planner"),
                    ),
                    model_override=self._resolve_model_for_phase(
                        "planning",
                        self._phase_cli("planning", config_name="planner"),
                    ),
                ),
                initial_prompt=prompt,
            )
        except BlockedOnCLI as exc:
            return self._transition(
                state,
                WorkflowStatus.BLOCKED_ON_CLI,
                current_phase=WorkflowStatus.PLANNING.value,
                error=str(exc),
            )
        except StepFailure as exc:
            return self._fail_run(state, self._format_step_failure(exc))

        state.plan_id = self._artifacts.save_plan(state.run_id, result)
        state.feasibility_id = None
        state.review_id = None
        state.adjudication_id = None
        self._artifacts.clear_feedback(state.run_id, "planning")
        self._artifacts.clear_execution_manifest(state.run_id)
        state.error = None
        self._state_mgr.save(state)

        if self._config.approval.require_plan_approval:
            return self._transition(state, WorkflowStatus.APPROVAL_PLAN)
        if self._config.feasibility.enabled:
            return self._transition(state, WorkflowStatus.FEASIBILITY)
        return self._transition(state, WorkflowStatus.EXECUTING)

    def _handle_plan_approval(self, state: RunState) -> RunState:
        if not self._config.approval.require_plan_approval:
            if self._config.feasibility.enabled:
                return self._transition(state, WorkflowStatus.FEASIBILITY)
            return self._transition(state, WorkflowStatus.EXECUTING)

        decision = self._artifacts.consume_approval_decision(state.run_id, "plan")
        if decision is None:
            return self._transition(
                state,
                WorkflowStatus.PAUSED,
                current_phase=WorkflowStatus.APPROVAL_PLAN.value,
            )
        self._artifacts.clear_processed_approval(state.run_id, "plan")
        if decision.get("decision") == "reject":
            self._artifacts.save_feedback(
                state.run_id,
                "planning",
                str(decision.get("reason") or ""),
            )
            return self._transition(state, WorkflowStatus.PLANNING)
        if self._config.feasibility.enabled:
            return self._transition(state, WorkflowStatus.FEASIBILITY)
        return self._transition(state, WorkflowStatus.EXECUTING)

    def _run_feasibility(self, state: RunState) -> RunState:
        if not self._config.feasibility.enabled:
            return self._transition(state, WorkflowStatus.EXECUTING)

        schema = load_bundled_schema("feasibility.schema.json")
        plan = self._require_artifact(state.plan_id)
        worktree_dir = self._ensure_worktree(state)
        result_path = (self._artifact_root / "feasibility" / f"pending-{state.run_id}.json").resolve()
        if result_path.exists():
            result_path.unlink()

        task_description = state.normalized_task or state.task
        plan_json = json_block(plan)
        directory_tree = render_directory_tree(worktree_dir)
        workspace_trees = self._workspace_trees(state, max_depth=2)
        adapter = self._adapter_for_phase("feasibility")
        cli_name = self._phase_cli("feasibility", config_name="feasibility_checker")
        if cli_name == "codex":
            prompt = build_feasibility_prompt_codex(
                task_description=task_description,
                plan_json=plan_json,
                directory_tree=directory_tree,
                workspace_trees=workspace_trees,
                result_file_path=str(result_path),
                schema_json=json_block(schema),
            )
        else:
            prompt = build_feasibility_prompt_claude(
                task_description=task_description,
                plan_json=plan_json,
                directory_tree=directory_tree,
                workspace_trees=workspace_trees,
                schema_json=json_block(schema),
            )
        self._artifacts.save_prompt(f"feasibility-{state.run_id[:8]}.md", prompt)

        try:
            result = self._invoke_with_retries(
                state,
                retry_key="feasibility",
                retries=self._retry_limit("feasibility"),
                spinner_label="Checking feasibility",
                invoke=lambda current_prompt: adapter.invoke(
                    current_prompt,
                    worktree_dir,
                    self._config.orchestrator.watchdog_timeout,
                    schema,
                    reasoning_effort_override=self._resolve_effort_for_phase(
                        state,
                        "feasibility",
                        cli_name,
                    ),
                    model_override=self._resolve_model_for_phase("feasibility", cli_name),
                ),
                initial_prompt=prompt,
            )
        except BlockedOnCLI as exc:
            return self._transition(
                state,
                WorkflowStatus.BLOCKED_ON_CLI,
                current_phase=WorkflowStatus.FEASIBILITY.value,
                error=str(exc),
            )
        except StepFailure as exc:
            return self._fail_run(state, self._format_step_failure(exc))
        finally:
            if result_path.exists():
                result_path.unlink()

        state.feasibility_id = self._artifacts.save_feasibility(state.run_id, result)
        state.error = None
        self._state_mgr.save(state)

        if result["verdict"] in {"go", "go_with_warnings"}:
            return self._transition(state, WorkflowStatus.EXECUTING)

        if state.replan_count >= self._replan_limit():
            return self._fail_run(state, "Replan loop limit exceeded")

        state.replan_count += 1
        self._state_mgr.save(state)
        issues = [
            f"- {issue['severity']}: {issue['description']}"
            for issue in result.get("blocking_issues", [])
        ]
        feedback = str(result.get("summary") or "").strip()
        if issues:
            feedback = (feedback + "\n\nBlocking issues:\n" + "\n".join(issues)).strip()
        self._artifacts.save_feedback(state.run_id, "planning", feedback)
        if state.is_workspace:
            self._reset_workspace_repos(state)
        else:
            self._discard_worktree(state, force=True)
        self._artifacts.clear_execution_manifest(state.run_id)
        return self._transition(state, WorkflowStatus.PLANNING)

    def _run_execution(self, state: RunState) -> RunState:
        plan = self._require_artifact(state.plan_id)
        manifest = self._ensure_execution_manifest(state, plan)
        target_steps = manifest["target_steps"]
        completed_steps = set(manifest["completed_steps"])
        steps_by_number = {step["step_number"]: step for step in plan["steps"]}

        worktree_dir = self._ensure_worktree(state)
        if state.is_workspace and not state.step_results and not manifest.get("completed_steps"):
            self._check_workspace_clean(state)
        worker_name = self._phase_cli("executing", config_name="worker")
        worker = self._adapter_for_phase("executing")
        schema = load_bundled_schema("step_result.schema.json")

        for step_number in target_steps:
            if step_number in completed_steps:
                continue
            step = steps_by_number[step_number]
            files_to_read = list(dict.fromkeys(step["files_to_read"] + step["files_to_modify"]))
            file_contents, _ = collect_file_context(worktree_dir, files_to_read)
            plan_context = json_block(
                {
                    "plan_artifact": state.plan_id,
                    "execution_manifest": manifest,
                    "current_step": step,
                    "dependency_results": self._dependency_results(
                        state,
                        step.get("depends_on", []),
                    ),
                }
            )
            pending_result_path = str(self._artifacts.pending_step_result_path(step_number))
            self._artifacts.clear_pending_step_result(step_number)

            if worker_name == "codex":
                prompt = build_execution_prompt_codex(
                    step_description=step["description"],
                    plan_context=plan_context,
                    file_contents=file_contents,
                    result_file_path=pending_result_path,
                    schema_json=json_block(schema),
                    workspace_trees=self._workspace_trees(state, max_depth=2),
                )
                invoke = lambda current_prompt, step_number=step_number: worker.invoke(
                    current_prompt,
                    worktree_dir,
                    self._config.orchestrator.watchdog_timeout,
                    schema,
                    step_number=step_number,
                    reasoning_effort_override=self._resolve_effort_for_phase(
                        state,
                        "executing",
                        worker_name,
                    ),
                    model_override=self._resolve_model_for_phase("executing", worker_name),
                )
            else:
                prompt = build_execution_prompt_claude(
                    step_description=step["description"],
                    plan_context=plan_context,
                    file_contents=file_contents,
                    schema_json=json_block(schema),
                    workspace_trees=self._workspace_trees(state, max_depth=2),
                )
                invoke = lambda current_prompt: worker.invoke(
                    current_prompt,
                    worktree_dir,
                    self._config.orchestrator.watchdog_timeout,
                    schema,
                    reasoning_effort_override=self._resolve_effort_for_phase(
                        state,
                        "executing",
                        worker_name,
                    ),
                    model_override=self._resolve_model_for_phase("executing", worker_name),
                )

            attempt_number = 0

            def invoke_and_enforce_status(current_prompt: str) -> dict[str, Any]:
                nonlocal attempt_number
                if attempt_number > 0:
                    self._artifacts.clear_pending_step_result(step_number)
                    if state.is_workspace:
                        self._reset_workspace_repos(state)
                    else:
                        self._reset_worktree(worktree_dir)
                attempt_number += 1
                result = invoke(current_prompt)
                if result.get("status") == "failed":
                    detail = str(result.get("summary") or "Execution step reported failure")
                    issues = result.get("issues") or []
                    if issues:
                        detail = detail + " Issues: " + "; ".join(str(issue) for issue in issues)
                    raise StepFailure(
                        "Execution step reported failed status",
                        validation_error=detail,
                    )
                if state.is_workspace:
                    result["workspace_diffs"] = self._collect_workspace_diffs(state)
                return result

            self._artifacts.save_prompt(f"step-{step_number}.md", prompt)

            try:
                result = self._invoke_with_retries(
                    state,
                    retry_key=f"step-{step_number}",
                    retries=self._retry_limit("executing"),
                    spinner_label=f"Executing step {step_number}",
                    invoke=invoke_and_enforce_status,
                    initial_prompt=prompt,
                )
            except BlockedOnCLI as exc:
                return self._transition(
                    state,
                    WorkflowStatus.BLOCKED_ON_CLI,
                    current_phase=WorkflowStatus.EXECUTING.value,
                    error=str(exc),
                )
            except StepFailure as exc:
                return self._fail_run(state, self._format_step_failure(exc))

            self._commit_worktree_step(state, worktree_dir, step_number, step["description"])
            reference = self._artifacts.save_step_result(state.run_id, step_number, result)
            self._update_step_result_reference(state, step_number, reference)
            manifest = self._mark_step_completed(manifest, step_number)
            self._artifacts.save_execution_manifest(state.run_id, manifest)
            state.error = None
            self._state_mgr.save(state)

        self._artifacts.clear_execution_manifest(state.run_id)
        return self._transition(state, WorkflowStatus.REVIEWING)

    def _run_review(self, state: RunState) -> RunState:
        schema = load_bundled_schema("review.schema.json")
        plan = self._require_artifact(state.plan_id)
        git_diff = redact_secret_text(self._implementation_diff(state))
        review_root = self._repo_root if state.is_workspace else self._ensure_worktree(state)
        try:
            changed_files = self._review_changed_files(state)
        except (EngineError, OSError):
            changed_files = []
        reviewer_config = self._load_reviewer_config(review_root)
        heuristic_findings = self._run_heuristic_scan(
            review_root,
            changed_files=changed_files,
            reviewer_config=reviewer_config,
        )
        reviewer_rules = self._load_reviewer_rules()
        review_prompt = build_review_prompt(
            task_description=state.normalized_task or state.task,
            plan_json=json_block(plan),
            git_diff=git_diff,
            step_results_json=json_block(self._load_step_results(state)),
            schema_json=json_block(schema),
            heuristic_findings=heuristic_findings,
            review_categories=reviewer_rules.get("review_categories") or {},
            reviewer_config=reviewer_config,
        )
        self._artifacts.save_prompt(f"review-{state.run_id[:8]}.md", review_prompt)
        adapter = self._adapter_for_phase("reviewing")
        cli_name = self._phase_cli("reviewing", config_name="reviewer")

        try:
            result = self._invoke_with_retries(
                state,
                retry_key="reviewing",
                retries=self._retry_limit("reviewing"),
                spinner_label="Reviewing changes",
                invoke=lambda current_prompt: adapter.invoke(
                    current_prompt,
                    self._repo_root,
                    self._config.orchestrator.watchdog_timeout,
                    schema,
                    reasoning_effort_override=self._resolve_effort_for_phase(
                        state,
                        "reviewing",
                        cli_name,
                    ),
                    model_override=self._resolve_model_for_phase("reviewing", cli_name),
                ),
                initial_prompt=review_prompt,
            )
        except BlockedOnCLI as exc:
            return self._transition(
                state,
                WorkflowStatus.BLOCKED_ON_CLI,
                current_phase=WorkflowStatus.REVIEWING.value,
                error=str(exc),
            )
        except StepFailure as exc:
            return self._fail_run(state, self._format_step_failure(exc))

        state.review_id = self._artifacts.save_review(state.run_id, result)
        state.error = None
        self._state_mgr.save(state)
        return self._transition(state, WorkflowStatus.ADJUDICATING)

    def _run_heuristic_scan(
        self,
        root: Path,
        *,
        changed_files: list[str],
        reviewer_config: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        try:
            return run_review_scan(root, changed_files=changed_files, config=reviewer_config)
        except Exception:
            return []

    def _load_reviewer_config(self, root: Path) -> dict[str, Any] | None:
        config = load_reviewer_config(root)
        if config is not None or root == self._repo_root:
            return config
        return load_reviewer_config(self._repo_root)

    def _load_reviewer_rules(self) -> dict[str, Any]:
        try:
            return load_reviewer_rules()
        except Exception:
            return {"review_categories": []}

    def _review_changed_files(self, state: RunState) -> list[str]:
        if state.is_workspace:
            changed: list[str] = []
            for repo in state.workspace_repos:
                result = subprocess.run(
                    ["git", "diff", "--name-only", "HEAD"],
                    cwd=self._repo_root / repo,
                    capture_output=True,
                    text=True,
                    shell=False,
                    check=False,
                )
                if result.returncode != 0:
                    raise EngineError(
                        f"Failed to list changed files for workspace repo '{repo}': {result.stderr.strip() or result.stdout.strip()}"
                    )
                changed.extend(
                    f"{repo}/{path.strip()}"
                    for path in result.stdout.splitlines()
                    if path.strip()
                )
            return list(dict.fromkeys(changed))

        if not state.base_commit or not state.worktree_branch:
            return []
        result = subprocess.run(
            ["git", "diff", "--name-only", f"{state.base_commit}...{state.worktree_branch}"],
            cwd=self._repo_root,
            capture_output=True,
            text=True,
            shell=False,
            check=False,
        )
        if result.returncode != 0:
            raise EngineError(
                f"Failed to list changed files for review: {result.stderr.strip() or result.stdout.strip()}"
            )
        return list(dict.fromkeys(path.strip() for path in result.stdout.splitlines() if path.strip()))

    def _run_adjudication(self, state: RunState) -> RunState:
        schema = load_bundled_schema("adjudication.schema.json")
        plan = self._require_artifact(state.plan_id)
        plan_step_numbers = {int(step["step_number"]) for step in plan["steps"]}
        validator = Validator(self._repo_root)
        review = self._require_artifact(state.review_id)
        task_description = state.normalized_task or state.task
        adjudication_feedback = self._artifacts.load_feedback(state.run_id, "adjudication")
        if adjudication_feedback:
            task_description = (
                task_description
                + self._workspace_feedback_prefix(state)
                + "\n\nMERGE REJECTION FEEDBACK:\n"
                + adjudication_feedback
            )

        prompt = build_adjudication_prompt(
            task_description=task_description,
            review_json=json_block(review),
            step_results_json=json_block(self._load_step_results(state)),
            schema_json=json_block(schema),
        )
        self._artifacts.save_prompt(f"adjudication-{state.run_id[:8]}.md", prompt)
        adapter = self._adapter_for_phase("adjudicating")
        cli_name = self._phase_cli("adjudicating", config_name="adjudicator")

        try:
            result = self._invoke_with_retries(
                state,
                retry_key="adjudicating",
                retries=self._retry_limit("adjudicating"),
                spinner_label="Adjudicating result",
                invoke=lambda current_prompt: self._validate_adjudication_result(
                    adapter.invoke(
                        current_prompt,
                        self._repo_root,
                        self._config.orchestrator.watchdog_timeout,
                        schema,
                        reasoning_effort_override=self._resolve_effort_for_phase(
                            state,
                            "adjudicating",
                            cli_name,
                        ),
                        model_override=self._resolve_model_for_phase("adjudicating", cli_name),
                    ),
                    validator,
                    plan_step_numbers,
                ),
                initial_prompt=prompt,
            )
        except BlockedOnCLI as exc:
            return self._transition(
                state,
                WorkflowStatus.BLOCKED_ON_CLI,
                current_phase=WorkflowStatus.ADJUDICATING.value,
                error=str(exc),
            )
        except StepFailure as exc:
            return self._fail_run(state, self._format_step_failure(exc))

        state.adjudication_id = self._artifacts.save_adjudication(state.run_id, result)
        self._artifacts.clear_feedback(state.run_id, "adjudication")
        state.error = None
        self._state_mgr.save(state)

        verdict = result["verdict"]
        if verdict == "PASS":
            return self._transition(state, WorkflowStatus.MERGING)
        if verdict == "REWORK":
            state.rework_count += 1
            self._state_mgr.save(state)
            if state.rework_count > self._rework_limit():
                return self._fail_run(state, "Rework loop limit exceeded")
            self._artifacts.clear_execution_manifest(state.run_id)
            return self._transition(state, WorkflowStatus.EXECUTING)
        if verdict == "REPLAN":
            state.replan_count += 1
            self._state_mgr.save(state)
            if state.replan_count > self._replan_limit():
                return self._fail_run(state, "Replan loop limit exceeded")
            if state.is_workspace:
                self._reset_workspace_repos(state)
            else:
                self._discard_worktree(state, force=True)
            state.step_results = []
            state.review_id = None
            self._state_mgr.save(state)
            self._artifacts.clear_execution_manifest(state.run_id)
            return self._transition(state, WorkflowStatus.PLANNING)
        return self._fail_run(state, str(result.get("failure_reason") or "Adjudication failed"))

    def _run_merge(self, state: RunState) -> RunState:
        base_branch = self._config.worktree.base_branch
        task_summary = self._task_summary(state.task)

        if not state.is_workspace:
            try:
                self._worktrees.verify_merge_preconditions(
                    base_branch,
                    state.base_commit,
                    allow_base_commit_mismatch=False,
                )
            except WorktreeError as exc:
                return self._fail_run(state, str(exc))

            checkout = subprocess.run(
                ["git", "checkout", base_branch],
                cwd=self._repo_root,
                capture_output=True,
                text=True,
                shell=False,
                check=False,
            )
            if checkout.returncode != 0:
                return self._fail_run(
                    state,
                    f"git checkout {base_branch} failed: {checkout.stderr.strip() or checkout.stdout.strip()}",
                )

            branch = state.worktree_branch or ""
            result = subprocess.run(
                ["git", "merge", "--squash", branch],
                cwd=self._repo_root,
                capture_output=True,
                text=True,
                shell=False,
                check=False,
            )
            if result.returncode != 0:
                return self._fail_run(
                    state,
                    f"git merge --squash failed: {result.stderr.strip() or result.stdout.strip()}",
                )

            self._discard_worktree(state, force=True)
            commands = [
                "# Review staged changes:",
                "git status",
                "git diff --cached",
                "",
                f'git commit -m "aio: {task_summary}"',
                f"git push origin {base_branch}",
            ]
        else:
            commands = self._generate_workspace_commands(state)

        state.commit_commands = commands
        self._artifacts.clear_processed_approval(state.run_id, "merge")
        state.error = None
        self._state_mgr.save(state)
        if self._ui:
            self._ui.print_commit_suggestions(commands)
        return self._transition(state, WorkflowStatus.DONE)

    def _transition(
        self,
        state: RunState,
        new_status: WorkflowStatus,
        *,
        current_phase: str | None = None,
        error: str | None = None,
    ) -> RunState:
        current = WorkflowStatus(state.status)
        if new_status not in TRANSITIONS.get(current, set()):
            raise EngineError(f"Invalid transition: {current.value} → {new_status.value}")
        state.status = new_status
        state.current_phase = current_phase or new_status.value
        state.error = error
        self._state_mgr.save(state)
        return state

    def _invoke_with_retries(
        self,
        state: RunState,
        *,
        retry_key: str,
        retries: int,
        spinner_label: str,
        invoke: Any,
        initial_prompt: str,
    ) -> dict[str, Any]:
        prompt = initial_prompt
        attempt = 0
        while True:
            try:
                if self._ui is None:
                    result = invoke(prompt)
                else:
                    with self._ui.phase_spinner(f"{spinner_label} (attempt {attempt + 1}/{retries})"):
                        result = invoke(prompt)
            except BlockedOnCLI:
                raise
            except StepFailure as exc:
                attempt += 1
                state.retry_counts[retry_key] = attempt
                self._state_mgr.save(state)
                if attempt >= retries:
                    raise
                prompt = build_retry_prompt(
                    original_prompt=initial_prompt,
                    error_message=self._format_step_failure(exc),
                )
                continue

            state.retry_counts[retry_key] = 0
            self._state_mgr.save(state)
            return result

    def _adapter_for_phase(self, phase_name: str) -> Any:
        cli_name = self._phase_cli(
            phase_name,
            config_name={
                "scoping": "scoper",
                "planning": "planner",
                "feasibility": "feasibility_checker",
                "executing": "worker",
                "reviewing": "reviewer",
                "adjudicating": "adjudicator",
            }[phase_name],
        )
        return self._adapter(cli_name)

    def _adapter(self, cli_name: str) -> Any:
        if cli_name in self._adapters:
            return self._adapters[cli_name]
        if cli_name == "claude":
            adapter = ClaudeAdapter(self._config, self._artifact_root)
        elif cli_name == "codex":
            adapter = CodexAdapter(self._config, self._artifact_root)
        else:
            raise EngineError(f"Unsupported adapter: {cli_name}")
        self._adapters[cli_name] = adapter
        return adapter

    def _phase_cli(self, workflow_phase: str, *, config_name: str) -> str:
        phase = self._workflow.phase(workflow_phase)
        override = self._config.routing.phases.get(workflow_phase)
        if override and override.cli:
            return override.cli
        configured = getattr(self._config.routing, config_name)
        return configured or phase.cli or ""

    def _resolve_effort_for_phase(
        self,
        state: RunState,
        phase_name: str,
        cli_name: str,
    ) -> str | None:
        override = self._config.routing.phases.get(phase_name)
        if override and override.reasoning_effort:
            return override.reasoning_effort

        complexity_tier = state.complexity_tier
        if complexity_tier:
            tier_map = getattr(self._config.complexity_routing, complexity_tier, {})
            if phase_name in tier_map:
                return tier_map[phase_name]

        return getattr(getattr(self._config.routing, cli_name), "reasoning_effort", "") or None

    def _resolve_model_for_phase(self, phase_name: str, cli_name: str) -> str | None:
        override = self._config.routing.phases.get(phase_name)
        if override and override.model:
            return override.model
        return getattr(getattr(self._config.routing, cli_name), "model", "") or None

    def _retry_limit(self, workflow_phase: str) -> int:
        config_limit = self._config.orchestrator.max_retries
        phase_limit = self._workflow.phase(workflow_phase).retries
        return config_limit or phase_limit

    def _rework_limit(self) -> int:
        return self._config.orchestrator.max_rework_loops

    def _replan_limit(self) -> int:
        return self._config.orchestrator.max_replan_loops

    def _ensure_worktree(self, state: RunState) -> Path:
        if state.is_workspace:
            return self._repo_root
        if state.worktree_path:
            return Path(state.worktree_path)
        worktree_path, branch_name, base_commit = self._worktrees.create(
            state.run_id,
            self._config.worktree.base_branch,
            self._config.worktree.branch_prefix,
        )
        state.worktree_path = str(worktree_path)
        state.worktree_branch = branch_name
        state.base_commit = base_commit
        self._state_mgr.save(state)
        return worktree_path

    def _reset_worktree(self, worktree_dir: Path) -> None:
        try:
            self._worktrees.reset(worktree_dir)
        except WorktreeError as exc:
            raise EngineError(f"Failed to reset worktree: {exc}") from exc

    def _discard_worktree(self, state: RunState, *, force: bool) -> None:
        if state.is_workspace:
            return
        if not state.worktree_path or not state.worktree_branch:
            return
        try:
            self._worktrees.remove(Path(state.worktree_path), state.worktree_branch, force=force)
        except WorktreeError:
            if not force:
                raise
        state.worktree_path = None
        state.worktree_branch = None
        state.base_commit = ""
        self._state_mgr.save(state)

    def _ensure_execution_manifest(self, state: RunState, plan: dict[str, Any]) -> dict[str, Any]:
        manifest = self._artifacts.load_execution_manifest(state.run_id)
        if manifest:
            return manifest

        adjudication = self._artifacts.read_json(state.adjudication_id) if state.adjudication_id else None
        if adjudication and adjudication.get("verdict") == "REWORK":
            target_steps = list(adjudication.get("rework_steps") or [])
            feedback = adjudication.get("rework_feedback")
            completed_steps: list[int] = []
            mode = "rework"
        else:
            target_steps = [step["step_number"] for step in plan["steps"]]
            completed_steps = self._completed_step_numbers(state)
            feedback = None
            mode = "plan"

        manifest = {
            "run_id": state.run_id,
            "plan_artifact": state.plan_id,
            "mode": mode,
            "target_steps": target_steps,
            "completed_steps": completed_steps,
            "feedback": feedback,
        }
        self._artifacts.save_execution_manifest(state.run_id, manifest)
        return manifest

    def _mark_step_completed(self, manifest: dict[str, Any], step_number: int) -> dict[str, Any]:
        completed = list(manifest.get("completed_steps", []))
        if step_number not in completed:
            completed.append(step_number)
        completed.sort()
        updated = dict(manifest)
        updated["completed_steps"] = completed
        return updated

    def _commit_worktree_step(
        self,
        state: RunState,
        worktree_dir: Path,
        step_number: int,
        description: str,
    ) -> None:
        if state.is_workspace:
            return
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=worktree_dir,
            capture_output=True,
            text=True,
            shell=False,
            check=False,
        )
        if status.returncode != 0:
            raise EngineError(f"Failed to inspect worktree status: {status.stderr.strip()}")
        if not status.stdout.strip():
            return

        add = subprocess.run(
            ["git", "add", "-A"],
            cwd=worktree_dir,
            capture_output=True,
            text=True,
            shell=False,
            check=False,
        )
        if add.returncode != 0:
            raise EngineError(f"git add failed: {add.stderr.strip()}")

        commit = subprocess.run(
            ["git", "commit", "-m", f"aio: step {step_number} - {description}"],
            cwd=worktree_dir,
            capture_output=True,
            text=True,
            shell=False,
            check=False,
        )
        if commit.returncode != 0 and "nothing to commit" not in commit.stderr.lower():
            raise EngineError(f"git commit failed: {commit.stderr.strip() or commit.stdout.strip()}")

    def _implementation_diff(self, state: RunState) -> str:
        if state.is_workspace:
            return self._aggregate_workspace_diffs(state)
        if not state.base_commit or not state.worktree_branch:
            return ""
        completed = subprocess.run(
            ["git", "diff", f"{state.base_commit}...{state.worktree_branch}"],
            cwd=self._repo_root,
            capture_output=True,
            text=True,
            shell=False,
            check=False,
        )
        if completed.returncode != 0:
            raise EngineError(f"Failed to compute implementation diff: {completed.stderr.strip()}")
        return completed.stdout

    def _load_step_results(self, state: RunState) -> list[dict[str, Any]]:
        return [self._artifacts.read_json(reference) for reference in state.step_results]

    def _workspace_trees(self, state: RunState, *, max_depth: int) -> dict[str, str] | None:
        if not state.is_workspace:
            return None
        return {
            repo: render_directory_tree(self._repo_root / repo, max_depth=max_depth)
            for repo in state.workspace_repos
        }

    def _check_workspace_clean(self, state: RunState) -> None:
        for repo in state.workspace_repos:
            repo_path = self._repo_root / repo
            if not repo_path.exists() or not (repo_path / ".git").exists():
                raise EngineError(f"Workspace repo '{repo}' is missing or is not a git repository.")
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                shell=False,
                check=False,
            )
            if result.returncode != 0:
                raise EngineError(
                    f"Failed to inspect workspace repo '{repo}': {result.stderr.strip() or result.stdout.strip()}"
                )
            if result.stdout.strip():
                raise EngineError(
                    f"Repo '{repo}' has uncommitted changes. Commit or stash all changes before starting a workspace run."
                )

    def _reset_workspace_repos(self, state: RunState) -> None:
        for repo in state.workspace_repos:
            repo_path = self._repo_root / repo
            subprocess.run(
                ["git", "checkout", "--", "."],
                cwd=repo_path,
                capture_output=True,
                text=True,
                shell=False,
                check=False,
            )
            subprocess.run(
                ["git", "clean", "-fd"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                shell=False,
                check=False,
            )

    def _collect_workspace_diffs(self, state: RunState) -> dict[str, str]:
        diffs: dict[str, str] = {}
        for repo in state.workspace_repos:
            repo_path = self._repo_root / repo
            result = subprocess.run(
                ["git", "diff", "HEAD"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                shell=False,
                check=False,
            )
            if result.returncode != 0:
                raise EngineError(
                    f"Failed to collect workspace diff for '{repo}': {result.stderr.strip() or result.stdout.strip()}"
                )
            if result.stdout.strip():
                diffs[repo] = result.stdout
        return diffs

    def _aggregate_workspace_diffs(self, state: RunState) -> str:
        all_diffs: list[str] = []
        for ref in state.step_results:
            result = self._artifacts.read_json(ref)
            for repo, diff in (result.get("workspace_diffs") or {}).items():
                all_diffs.append(f"--- {repo}/ ---\n{diff}")
        return "\n\n".join(all_diffs)

    def _generate_workspace_commands(self, state: RunState) -> list[str]:
        task_summary = self._task_summary(state.task)
        base_branch = self._config.worktree.base_branch
        commands: list[str] = []
        for repo in state.workspace_repos:
            repo_path = self._repo_root / repo
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                shell=False,
                check=False,
            )
            if result.returncode != 0:
                raise EngineError(
                    f"Failed to inspect workspace repo '{repo}': {result.stderr.strip() or result.stdout.strip()}"
                )
            if result.stdout.strip():
                commands.extend(
                    [
                        f"# {repo}/",
                        f"cd {repo}",
                        "git add .",
                        f'git commit -m "aio: {task_summary}"',
                        f"git push origin {base_branch}",
                        "cd ..",
                        "",
                    ]
                )
        if not commands:
            return ["# No changes detected in any workspace repo."]
        return commands

    def _completed_step_numbers(self, state: RunState) -> list[int]:
        numbers: list[int] = []
        for result in self._load_step_results(state):
            step_number = int(result["step_number"])
            if step_number not in numbers:
                numbers.append(step_number)
        return sorted(numbers)

    def _dependency_results(self, state: RunState, depends_on: list[int]) -> list[dict[str, Any]]:
        if not depends_on:
            return []
        wanted = set(depends_on)
        results = []
        for payload in self._load_step_results(state):
            if int(payload["step_number"]) in wanted:
                results.append(payload)
        results.sort(key=lambda item: item["step_number"])
        return results

    def _require_artifact(self, reference: str | None) -> dict[str, Any]:
        if not reference:
            raise EngineError("Required artifact reference is missing")
        return self._artifacts.read_json(reference)

    def _update_step_result_reference(self, state: RunState, step_number: int, reference: str) -> None:
        existing: dict[int, str] = {}
        for ref in state.step_results:
            payload = self._artifacts.read_json(ref)
            existing[int(payload["step_number"])] = ref
        existing[step_number] = reference
        state.step_results = [existing[number] for number in sorted(existing)]

    def _gate_phase(self, gate: str) -> str:
        return {
            "scope": WorkflowStatus.SCOPING.value,
            "plan": WorkflowStatus.APPROVAL_PLAN.value,
        }[gate]

    def _workspace_feedback_prefix(self, state: RunState) -> str:
        if not state.is_workspace:
            return ""
        aggregated = self._aggregate_workspace_diffs(state)
        if not aggregated:
            return ""
        return f"\n\nWorkspace changes at time of failure:\n{aggregated}\n"

    def _task_summary(self, task: str) -> str:
        summary = " ".join(task.split())
        return summary[:72] if len(summary) > 72 else summary

    @staticmethod
    def _extract_stdout_error_messages(stdout: str) -> list[str]:
        text = stdout.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return []
        if not isinstance(parsed, dict):
            return []

        messages: list[str] = []

        def append_message(value: Any) -> None:
            if isinstance(value, str):
                message = value.strip()
                if message:
                    messages.append(message)
                return
            if isinstance(value, dict):
                for key in ("message", "detail", "result", "error"):
                    nested = value.get(key)
                    if isinstance(nested, str) and nested.strip():
                        messages.append(nested.strip())
                        break

        append_message(parsed.get("error"))
        append_message(parsed.get("message"))
        if isinstance(parsed.get("errors"), list):
            for item in parsed["errors"]:
                append_message(item)
        if parsed.get("is_error"):
            append_message(parsed.get("result"))

        return list(dict.fromkeys(messages))

    def _format_step_failure(self, exc: StepFailure) -> str:
        parts: list[str] = []
        if exc.validation_error:
            parts.append(exc.validation_error.strip())
        elif str(exc):
            parts.append(str(exc).strip())
        if exc.stderr and exc.stderr.strip():
            parts.append(f"stderr: {exc.stderr.strip()[:1000]}")
        stdout_errors = self._extract_stdout_error_messages(exc.stdout)
        if stdout_errors:
            parts.append(f"stdout_errors: {'; '.join(stdout_errors)[:1000]}")
        if exc.exit_code is not None:
            parts.append(f"exit_code: {exc.exit_code}")
        return "\n".join(parts)

    def _fail_run(self, state: RunState, message: str) -> RunState:
        try:
            self._discard_worktree(state, force=True)
        except Exception:
            pass
        self._artifacts.clear_execution_manifest(state.run_id)
        return self._transition(
            state,
            WorkflowStatus.FAILED,
            current_phase=state.current_phase,
            error=message,
        )

    @staticmethod
    def _validate_adjudication_result(
        result: dict[str, Any],
        validator: Validator,
        plan_step_numbers: set[int],
    ) -> dict[str, Any]:
        try:
            return validator.validate_adjudication(
                result,
                plan_step_numbers=plan_step_numbers,
            )
        except ValidationError as exc:
            raise StepFailure(
                "Adjudication referenced invalid plan steps",
                validation_error=exc.detail or str(exc),
            ) from exc
