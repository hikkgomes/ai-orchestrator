"""Workflow engine and finite state machine."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any
from uuid import uuid4

from .adapters import AdapterRegistry
from .adapters.base import BlockedOnCLI, InvokeResult, StepFailure, TextInvokeResult
from .artifacts import ArtifactStore
from .bootstrap import ensure_runtime_gitignore
from .config import Config
from .models import DebateRound, DebateState, ReviewDebatePhase, RunState, WorkflowStatus
from .prompts.templates import (
    build_delivery_prompt,
    build_fix_planning_prompt,
    build_full_execution_prompt,
    build_planning_prompt,
    build_review_codex_prompt,
    build_review_final_claude_prompt,
    build_retry_prompt,
    build_review_prompt,
    json_block,
)
from .reviewer import load_config as load_reviewer_config
from .reviewer import run_review_scan
from .scoping import ScopingConversation
from .state import StateManager
from .validator import load_bundled_schema
from .workflow import WorkflowDefinition, load_workflow_definition
from .worktree import WorktreeError, WorktreeManager


TRANSITIONS: dict[WorkflowStatus, set[WorkflowStatus]] = {
    WorkflowStatus.INIT: {
        WorkflowStatus.SCOPING,
        WorkflowStatus.PLANNING,
        WorkflowStatus.EXECUTING,
        WorkflowStatus.REVIEWING,
        WorkflowStatus.FAILED,
        WorkflowStatus.TERMINATED,
    },
    WorkflowStatus.SCOPING: {
        WorkflowStatus.PLANNING,
        WorkflowStatus.PAUSED,
        WorkflowStatus.FAILED,
        WorkflowStatus.BLOCKED_ON_CLI,
    },
    WorkflowStatus.PLANNING: {
        WorkflowStatus.APPROVAL_PLAN,
        WorkflowStatus.EXECUTING,
        WorkflowStatus.FAILED,
        WorkflowStatus.BLOCKED_ON_CLI,
    },
    WorkflowStatus.APPROVAL_PLAN: {
        WorkflowStatus.EXECUTING,
        WorkflowStatus.PLANNING,
        WorkflowStatus.PAUSED,
        WorkflowStatus.TERMINATED,
    },
    WorkflowStatus.EXECUTING: {
        WorkflowStatus.REVIEWING,
        WorkflowStatus.MERGING,
        WorkflowStatus.FAILED,
        WorkflowStatus.BLOCKED_ON_CLI,
        WorkflowStatus.PAUSED,
    },
    WorkflowStatus.REVIEWING: {
        WorkflowStatus.MERGING,
        WorkflowStatus.PLANNING,
        WorkflowStatus.PAUSED,
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
    WorkflowStatus.FAILED: {
        WorkflowStatus.SCOPING,
        WorkflowStatus.PLANNING,
        WorkflowStatus.EXECUTING,
        WorkflowStatus.REVIEWING,
        WorkflowStatus.MERGING,
    },
    WorkflowStatus.TERMINATED: set(),
    WorkflowStatus.PAUSED: {
        WorkflowStatus.SCOPING,
        WorkflowStatus.PLANNING,
        WorkflowStatus.APPROVAL_PLAN,
        WorkflowStatus.EXECUTING,
        WorkflowStatus.REVIEWING,
    },
    WorkflowStatus.BLOCKED_ON_CLI: {
        WorkflowStatus.SCOPING,
        WorkflowStatus.PLANNING,
        WorkflowStatus.EXECUTING,
        WorkflowStatus.REVIEWING,
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
        skip_review: bool = False,
        autonomous_max_iterations: int | None = None,
        review_rounds: int | None = None,
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
        self._adapter_registry = AdapterRegistry(self._config, self._artifact_root)
        self._ui = ui
        self._skip_review = skip_review
        self._autonomous_max_iterations = autonomous_max_iterations
        self._review_rounds = review_rounds

    def _log_prompt_size(self, label: str, prompt: str) -> None:
        if self._ui:
            approx_tokens = len(prompt) // 4
            self._ui.info(f"  {label}: ~{approx_tokens:,} tokens")

    def start(
        self,
        task: str,
        run_id: str,
        *,
        is_workspace: bool = False,
        workspace_repos: list[str] | None = None,
        start_at: str | None = None,
        plan: dict[str, Any] | None = None,
        mode: str = "default",
    ) -> RunState:
        state = RunState(
            run_id=run_id,
            task=task,
            is_workspace=is_workspace,
            workspace_repos=list(workspace_repos or []),
            mode=mode,
        )
        if plan is not None:
            state.plan_id = self._artifacts.save_plan(run_id, plan)
        self._state_mgr.save(state)
        if start_at:
            phase = self._phase_from_start_at(start_at)
            state = self._transition(state, phase)
        elif self._config.scoping.enabled:
            state = self._transition(state, WorkflowStatus.SCOPING)
        else:
            state = self._transition(state, WorkflowStatus.PLANNING)
        return self._run(state)

    @staticmethod
    def _phase_from_start_at(start_at: str) -> WorkflowStatus:
        phase = start_at.strip().lower().replace("-", "_")
        mapping = {
            "scoping": WorkflowStatus.SCOPING,
            "planning": WorkflowStatus.PLANNING,
            "executing": WorkflowStatus.EXECUTING,
            "execution": WorkflowStatus.EXECUTING,
            "reviewing": WorkflowStatus.REVIEWING,
            "review": WorkflowStatus.REVIEWING,
        }
        if phase not in mapping:
            raise EngineError(f"Unsupported start phase: {start_at}")
        return mapping[phase]

    def resume(self, run_id: str) -> RunState:
        state = self._state_mgr.load(run_id)
        status = WorkflowStatus(state.status)
        if status in {WorkflowStatus.DONE, WorkflowStatus.TERMINATED}:
            raise EngineError(f"Run {run_id} is not resumable from {status.value}")
        if status == WorkflowStatus.FAILED:
            resume_phase = self._failed_resume_phase(state)
            self._clear_retry_counts_for_phase(state, resume_phase)
            state = self._transition(
                state,
                resume_phase,
                current_phase=resume_phase.value,
                error=None,
            )
            return self._run(state)
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

    def _failed_resume_phase(self, state: RunState) -> WorkflowStatus:
        phase = WorkflowStatus(state.current_phase)
        if (
            phase in {WorkflowStatus.EXECUTING, WorkflowStatus.REVIEWING, WorkflowStatus.MERGING}
            and not state.is_workspace
            and (not state.worktree_path or not Path(state.worktree_path).exists())
        ):
            return WorkflowStatus.EXECUTING
        return phase

    @staticmethod
    def _clear_retry_counts_for_phase(state: RunState, phase: WorkflowStatus) -> None:
        prefixes = {
            WorkflowStatus.REVIEWING: ("reviewing", "review-final"),
        }.get(phase, (phase.value.lower(),))
        for key in list(state.retry_counts):
            if key.startswith(prefixes):
                del state.retry_counts[key]

    def approve(
        self,
        run_id: str,
        gate: str,
        *,
        force: bool = False,
        decision: str | None = None,
    ) -> RunState:
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
        self._artifacts.save_approval_decision(
            run_id,
            gate,
            decision or "approve",
            force=force,
        )
        state = self._transition(
            state,
            WorkflowStatus(gate_phase),
            current_phase=gate_phase,
            error=None,
        )
        return self._run(state)

    def reject(self, run_id: str, gate: str, reason: str, *, full: bool = False) -> RunState:
        state = self._state_mgr.load(run_id)
        gate_phase = self._gate_phase(gate)
        if WorkflowStatus(state.status) != WorkflowStatus.PAUSED or state.current_phase != gate_phase:
            raise EngineError(f"Run {run_id} is not paused at the {gate} gate")
        if gate == "scope":
            state.task = reason
            state.normalized_task = None
            state.complexity_tier = None
            state.scope_md_ref = None
            state.ai_scope_refs = {}
            state.scoping_participants = []
            state.scoping_round = 0
            state.scoping_agreed = False
            state.error = None
            self._artifacts.save_feedback(run_id, "scoping", reason)
            self._state_mgr.save(state)
            state = self._transition(
                state,
                WorkflowStatus.SCOPING,
                current_phase=WorkflowStatus.SCOPING.value,
                error=None,
            )
            return self._run(state)
        self._artifacts.save_approval_decision(
            run_id,
            gate,
            "full_reject" if full else "reject",
            reason=reason,
        )
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
                WorkflowStatus.TERMINATED,
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
            if status == WorkflowStatus.EXECUTING:
                state = self._run_execution(state)
                continue
            if status == WorkflowStatus.REVIEWING:
                state = self._run_review(state)
                continue
            if status == WorkflowStatus.MERGING:
                state = self._run_merge(state)
                continue

            raise EngineError(f"Unhandled engine status: {status.value}")

    def _run_scoping(self, state: RunState) -> RunState:
        try:
            return ScopingConversation(self).run(state)
        except (StepFailure, EngineError) as exc:
            return self._fail_run(state, self._format_step_failure(exc) if isinstance(exc, StepFailure) else str(exc))
        except Exception as exc:
            return self._fail_run(state, str(exc))

    def _run_planning(self, state: RunState) -> RunState:
        task_description = state.normalized_task or state.task
        feedback_parts = []

        planning_feedback = self._artifacts.load_feedback(state.run_id, "planning")
        if planning_feedback:
            feedback_parts.append(f"Planning feedback:\n{planning_feedback}")
        if feedback_parts:
            task_description = (
                task_description
                + self._workspace_feedback_prefix(state)
                + "\n\nADDITIONAL FEEDBACK:\n"
                + "\n\n".join(feedback_parts)
            )

        scope_md = self._artifacts.read_text(state.scope_md_ref) if state.scope_md_ref else ""
        is_fix_planning = state.fix_iteration_count > 0 and bool(planning_feedback)
        if is_fix_planning:
            prompt = build_fix_planning_prompt(
                issues=planning_feedback or "",
            )
        else:
            prompt = build_planning_prompt(
                task_description=task_description,
                scope_md=scope_md or "<none>",
            )
        self._artifacts.save_prompt(f"planning-{state.run_id[:8]}.md", prompt)
        self._log_prompt_size("Planning prompt", prompt)

        adapter = self._adapter_for_phase("planning")
        cli_name = self._phase_cli("planning", config_name="planner")
        planning_tools = self._phase_allowed_tools("planning", default=["Read", "Grep", "Glob"])
        planning_timeout = self._phase_timeout("planning")
        resume_session_id: str | None = None
        if self._config.sessions.enable_planning_resume:
            if self._config.sessions.enable_unified_session:
                resume_session_id = state.session_ids.get("claude_main")
            elif feedback_parts:
                resume_session_id = state.session_ids.get("planning")
        planning_effort = self._resolve_effort_for_phase(state, "planning", cli_name)
        planning_model = self._resolve_model_for_phase("planning", cli_name, state)
        planning_label = self._model_label(planning_model, cli_name)
        planning_spinner = f"Planning with {planning_label} ({planning_effort or 'default'})"

        def clear_resume_on_retry() -> None:
            nonlocal resume_session_id
            resume_session_id = None

        try:
            invoke_result = self._invoke_with_retries(
                state,
                retry_key="planning",
                retries=self._retry_limit("planning"),
                spinner_label=planning_spinner,
                invoke=lambda current_prompt: self._coerce_text_to_invoke_result(
                    self._invoke_adapter_text(
                        adapter,
                        current_prompt,
                        self._repo_root,
                        spinner_label="",
                        reasoning_effort_override=planning_effort,
                        model_override=planning_model,
                        resume_session_id=resume_session_id,
                        allowed_tools=planning_tools,
                        timeout_seconds=planning_timeout,
                        with_spinner=False,
                    )
                ),
                initial_prompt=prompt,
                on_retry=clear_resume_on_retry,
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

        result_text = str(invoke_result.data.get("text") or "").strip()
        generated_plan_id = str(uuid4())
        frontmatter_task = json.dumps((state.normalized_task or state.task).replace("\n", " "))
        plan_markdown = (
            "---\n"
            f"plan_id: {generated_plan_id}\n"
            f"task: {frontmatter_task}\n"
            "---\n\n"
            f"{result_text}\n"
        )
        if invoke_result.session_id:
            state.session_ids["planning"] = invoke_result.session_id
            if self._config.sessions.enable_unified_session:
                state.session_ids["claude_main"] = invoke_result.session_id
        state.plan_id = self._artifacts.save_plan_md(state.run_id, plan_markdown)
        state.review_id = None
        state.debate_state = None
        self._artifacts.clear_feedback(state.run_id, "planning")
        state.error = None
        self._state_mgr.save(state)

        if self._config.approval.require_plan_approval:
            return self._transition(state, WorkflowStatus.APPROVAL_PLAN)
        return self._transition(state, WorkflowStatus.EXECUTING)

    def _handle_plan_approval(self, state: RunState) -> RunState:
        if not self._config.approval.require_plan_approval:
            return self._transition(state, WorkflowStatus.EXECUTING)

        decision = self._artifacts.consume_approval_decision(state.run_id, "plan")
        if decision is None:
            return self._transition(
                state,
                WorkflowStatus.PAUSED,
                current_phase=WorkflowStatus.APPROVAL_PLAN.value,
            )
        self._artifacts.clear_processed_approval(state.run_id, "plan")
        if decision.get("decision") == "full_reject":
            return self._terminate_run(
                state,
                str(decision.get("reason") or "Plan rejected by user"),
            )
        if decision.get("decision") == "reject":
            self._artifacts.save_feedback(
                state.run_id,
                "planning",
                str(decision.get("reason") or ""),
            )
            return self._transition(state, WorkflowStatus.PLANNING)
        return self._transition(state, WorkflowStatus.EXECUTING)

    def _run_execution(self, state: RunState) -> RunState:
        plan_text = self._load_plan_text(state.plan_id)
        worktree_dir = self._ensure_worktree(state)
        overrides = state.execution_overrides or {}
        worker_name = overrides.get("cli") or self._phase_cli("executing", config_name="worker")
        worker = self._adapter(worker_name)
        execution_effort = overrides.get("effort") or self._resolve_effort_for_phase(state, "executing", worker_name)
        execution_model = overrides.get("model") or self._resolve_model_for_phase("executing", worker_name, state)
        schema = load_bundled_schema("execution_result.schema.json")

        pending_result_path = str(self._artifacts.pending_execution_result_path(state.run_id))
        self._artifacts.clear_pending_execution_result(state.run_id)

        prompt = build_full_execution_prompt(
            plan_text=plan_text,
            result_file_path=pending_result_path,
        )

        def invoke(current_prompt: str) -> Any:
            resume_session_id = None
            if state.fix_iteration_count > 0:
                resume_session_id = state.session_ids.get("execution")
            return self._invoke_adapter_json(
                worker,
                current_prompt,
                worktree_dir,
                schema,
                reasoning_effort_override=execution_effort,
                model_override=execution_model,
                resume_session_id=resume_session_id,
            )

        attempt_number = 0

        def invoke_and_enforce_status(current_prompt: str) -> InvokeResult:
            nonlocal attempt_number
            if attempt_number > 0:
                self._artifacts.clear_pending_execution_result(state.run_id)
                if state.is_workspace:
                    self._reset_workspace_repos(state)
                else:
                    self._reset_worktree(worktree_dir)
            attempt_number += 1
            invoke_result = self._coerce_invoke_result(invoke(current_prompt))
            result = invoke_result.data
            if result.get("status") == "failed":
                detail = str(result.get("summary") or "Execution reported failure")
                issues = result.get("issues") or []
                if issues:
                    detail = detail + " Issues: " + "; ".join(str(issue) for issue in issues)
                raise StepFailure(
                    "Execution reported failed status",
                    validation_error=detail,
                )
            if state.is_workspace:
                result["workspace_diffs"] = self._collect_workspace_diffs(state)
            return InvokeResult(data=result, session_id=invoke_result.session_id)

        self._artifacts.save_prompt(f"execution-{state.run_id[:8]}.md", prompt)
        self._log_prompt_size("Execution prompt", prompt)

        try:
            invoke_result = self._invoke_with_retries(
                state,
                retry_key="execution",
                retries=self._retry_limit("executing"),
                spinner_label=self._executing_message(
                    "Codex is building the implementation..."
                    if worker_name == "codex"
                    else "Claude is building the implementation...",
                    cli=worker_name,
                ),
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

        result = invoke_result.data
        self._commit_worktree_all(
            state,
            worktree_dir,
            self._task_summary(state.normalized_task or state.task),
        )
        reference = self._artifacts.save_execution_result(state.run_id, result)
        state.execution_result_ref = reference
        state.step_results = [reference]
        if invoke_result.session_id:
            state.session_ids["execution"] = invoke_result.session_id
        state.error = None
        self._state_mgr.save(state)
        if self._skip_review:
            return self._transition(state, WorkflowStatus.MERGING)
        return self._transition(state, WorkflowStatus.REVIEWING)

    def _run_review(self, state: RunState) -> RunState:
        schema = load_bundled_schema("review.schema.json")
        debate_schema = load_bundled_schema("debate_response.schema.json")
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
        review_tools = self._phase_allowed_tools("reviewing", default=["Read", "Grep", "Glob", "Bash"])
        review_timeout = self._phase_timeout("reviewing")
        review_prompt = build_review_prompt(
            step_results_json=json_block(self._load_step_results(state)),
            heuristic_findings=heuristic_findings,
        )
        self._artifacts.save_prompt(f"review-{state.run_id[:8]}.md", review_prompt)
        self._log_prompt_size("Review (Claude) prompt", review_prompt)
        adapter = self._adapter("claude")
        cli_name = "claude"
        review_effort = self._resolve_effort_for_phase(state, "reviewing", cli_name)
        review_model = self._resolve_model_for_phase("reviewing", cli_name, state)
        review_resume_session_id: str | None = None
        if self._config.sessions.enable_unified_session and self._config.sessions.enable_review_resume:
            review_resume_session_id = state.session_ids.get("claude_main")
        if not state.debate_state or state.debate_state.debate_phase == ReviewDebatePhase.INITIAL_REVIEWS:
            state.session_ids.pop("reviewing", None)
            state.debate_state = DebateState(debate_phase=ReviewDebatePhase.INITIAL_REVIEWS)
        self._state_mgr.save(state)

        def clear_review_resume_on_retry() -> None:
            nonlocal review_resume_session_id
            review_resume_session_id = None

        try:
            assert state.debate_state is not None
            if state.debate_state.debate_phase == ReviewDebatePhase.INITIAL_REVIEWS:
                invoke_result = self._invoke_with_retries(
                    state,
                    retry_key="reviewing",
                    retries=self._retry_limit("reviewing"),
                    spinner_label=self._reviewing_message("claude_reviews", "Claude is reviewing the implementation..."),
                    invoke=lambda current_prompt: self._invoke_adapter_json(
                        adapter,
                        current_prompt,
                        review_root,
                        schema,
                        reasoning_effort_override=review_effort,
                        model_override=review_model,
                        resume_session_id=review_resume_session_id,
                        allowed_tools=review_tools,
                        timeout_seconds=review_timeout,
                    ),
                    initial_prompt=review_prompt,
                    on_retry=clear_review_resume_on_retry,
                )

                claude_review = invoke_result.data
                if invoke_result.session_id:
                    state.session_ids["reviewing"] = invoke_result.session_id
                    if self._config.sessions.enable_unified_session:
                        state.session_ids["claude_main"] = invoke_result.session_id
                state.review_id = self._artifacts.save_review(state.run_id, claude_review)
                self._record_debate_round(
                    state,
                    actor="claude",
                    model_used=review_model,
                    effort_used=review_effort,
                    position="issues_confirmed" if self._review_has_issues(claude_review) else "issues_dismissed",
                    reasoning=str(claude_review.get("summary") or ""),
                    issues=self._issues_from_review(claude_review),
                )
                claude_has_issues = self._review_has_issues(claude_review)
                if self._ui:
                    if claude_has_issues:
                        self._ui.info("Claude found issues in the implementation.")
                    else:
                        self._ui.info("Claude's review came back clean.")
                if state.debate_state:
                    state.debate_state.debate_phase = ReviewDebatePhase.CROSS_REVIEW
                self._state_mgr.save(state)
            else:
                claude_review = self._load_saved_claude_review(state)
                claude_has_issues = self._review_has_issues(claude_review)

            if self._review_rounds is not None and self._review_rounds <= 1:
                if claude_has_issues:
                    if state.debate_state:
                        state.debate_state.final_verdict = "fix"
                        state.debate_state.consolidated_issues = self._issues_from_review(claude_review)
                        state.debate_state.debate_phase = ReviewDebatePhase.RESOLVED
                    self._state_mgr.save(state)
                    return self._debate_resolve_fix(state)
                if state.debate_state:
                    state.debate_state.final_verdict = "pass"
                    state.debate_state.debate_phase = ReviewDebatePhase.RESOLVED
                self._state_mgr.save(state)
                return self._debate_resolve_pass(state)

            if state.debate_state.debate_phase == ReviewDebatePhase.CROSS_REVIEW:
                codex_prompt = build_review_codex_prompt(
                    task_description=state.normalized_task or state.task,
                    review_json=json_block(claude_review),
                )
                self._artifacts.save_prompt(f"review-codex-{state.run_id[:8]}.md", codex_prompt)
                self._log_prompt_size("Review (Codex) prompt", codex_prompt)
                codex = self._adapter("codex")
                codex_model = self._config.models.reviewing.codex or self._resolve_model_for_phase(
                    "reviewing",
                    "codex",
                    state,
                )
                codex_review_resume_session_id: str | None = None

                def clear_codex_review_resume_on_retry() -> None:
                    nonlocal codex_review_resume_session_id
                    codex_review_resume_session_id = None

                codex_result = self._invoke_with_retries(
                    state,
                    retry_key="reviewing-codex",
                    retries=self._retry_limit("reviewing"),
                    spinner_label=self._reviewing_message("codex_reviews", "Codex is forming its verdict..."),
                    invoke=lambda current_prompt: self._invoke_adapter_json(
                        codex,
                        current_prompt,
                        review_root,
                        schema,
                        reasoning_effort_override=self._config.efforts.reviewing.codex,
                        model_override=codex_model,
                        resume_session_id=codex_review_resume_session_id,
                    ),
                    initial_prompt=codex_prompt,
                    on_retry=clear_codex_review_resume_on_retry,
                )
                codex_review = codex_result.data
                if codex_result.session_id:
                    state.session_ids["scoping_codex"] = codex_result.session_id
                self._record_debate_round(
                    state,
                    actor="codex",
                    model_used=codex_model,
                    effort_used=self._config.efforts.reviewing.codex,
                    position="issues_confirmed" if self._review_has_issues(codex_review) else "issues_dismissed",
                    reasoning=str(codex_review.get("summary") or ""),
                    issues=self._issues_from_review(codex_review),
                )

                codex_has_issues = self._review_has_issues(codex_review)
                if self._ui:
                    if codex_has_issues:
                        if claude_has_issues:
                            self._ui.info("Codex also found issues.")
                        else:
                            self._ui.info("Codex disagrees: it found issues Claude missed.")
                    elif claude_has_issues:
                        self._ui.info("Codex disagrees: it thinks the issues are not real.")
                    else:
                        self._ui.info("Codex agrees with Claude.")
                if claude_has_issues and codex_has_issues and self._review_issues_match(
                    claude_review,
                    codex_review,
                ):
                    if state.debate_state:
                        state.debate_state.final_verdict = "fix"
                        state.debate_state.consolidated_issues = self._issues_from_review(claude_review)
                        state.debate_state.debate_phase = ReviewDebatePhase.RESOLVED
                    self._state_mgr.save(state)
                    if self._ui:
                        self._ui.info("Issues confirmed; sending back for fixes.")
                    return self._debate_resolve_fix(state)
                if not claude_has_issues and not codex_has_issues:
                    if state.debate_state:
                        state.debate_state.final_verdict = "pass"
                        state.debate_state.debate_phase = ReviewDebatePhase.RESOLVED
                    self._state_mgr.save(state)
                    if self._ui:
                        self._ui.info("Implementation approved; moving to merge.")
                    return self._debate_resolve_pass(state)

                scenario = self._review_disagreement_scenario(claude_has_issues, codex_has_issues)
                if state.debate_state:
                    state.debate_state.disagreement_case = scenario
                    state.debate_state.debate_phase = ReviewDebatePhase.ESCALATION
                self._state_mgr.save(state)
            else:
                codex_review = self._load_codex_review_from_debate(state)
            if self._review_rounds is not None and self._review_rounds <= 2:
                if claude_has_issues or self._review_has_issues(codex_review):
                    if state.debate_state:
                        state.debate_state.final_verdict = "fix"
                        state.debate_state.consolidated_issues = (
                            self._issues_from_review(claude_review)
                            or self._issues_from_review(codex_review)
                        )
                        state.debate_state.debate_phase = ReviewDebatePhase.RESOLVED
                    self._state_mgr.save(state)
                    return self._debate_resolve_fix(state)
                if state.debate_state:
                    state.debate_state.final_verdict = "pass"
                    state.debate_state.debate_phase = ReviewDebatePhase.RESOLVED
                self._state_mgr.save(state)
                return self._debate_resolve_pass(state)
            final_prompt = build_review_final_claude_prompt(
                codex_review_json=json_block(codex_review),
            )
            self._artifacts.save_prompt(f"review-final-claude-{state.run_id[:8]}.md", final_prompt)
            self._log_prompt_size("Review final (Claude) prompt", final_prompt)
            final_effort = self._review_final_effort(state)
            final_review_resume_session_id: str | None = None
            if self._config.sessions.enable_review_resume:
                if self._config.sessions.enable_unified_session:
                    final_review_resume_session_id = state.session_ids.get("claude_main")
                else:
                    final_review_resume_session_id = state.session_ids.get("reviewing")

            def clear_final_review_resume_on_retry() -> None:
                nonlocal final_review_resume_session_id
                final_review_resume_session_id = None

            final_result = self._invoke_with_retries(
                state,
                retry_key="review-final-claude",
                retries=self._retry_limit("reviewing"),
                spinner_label=self._reviewing_message("claude_final", "Claude Opus is making the final review call..."),
                invoke=lambda current_prompt: self._invoke_adapter_json(
                    self._adapter("claude"),
                    current_prompt,
                    review_root,
                    debate_schema,
                    reasoning_effort_override=final_effort,
                    model_override=self._config.models.debate.escalated_claude or review_model,
                    resume_session_id=final_review_resume_session_id,
                    allowed_tools=review_tools,
                    timeout_seconds=review_timeout,
                ),
                initial_prompt=final_prompt,
                on_retry=clear_final_review_resume_on_retry,
            )
            final = final_result.data
            if final_result.session_id:
                state.session_ids["reviewing"] = final_result.session_id
                if self._config.sessions.enable_unified_session:
                    state.session_ids["claude_main"] = final_result.session_id
            final_position = str(final.get("position") or "")
            final_issues = final.get("issues") if isinstance(final.get("issues"), list) else []
            self._record_debate_round(
                state,
                actor="claude",
                model_used=self._config.models.debate.escalated_claude or review_model,
                effort_used=final_effort,
                position=final_position,
                reasoning=str(final.get("reasoning") or ""),
                issues=final_issues,
            )
            if final_position in {"issues_confirmed", "issues_accepted"}:
                if state.debate_state:
                    state.debate_state.final_verdict = "fix"
                    state.debate_state.consolidated_issues = (
                        final_issues
                        or self._issues_from_review(claude_review)
                        or self._issues_from_review(codex_review)
                    )
                    state.debate_state.debate_phase = ReviewDebatePhase.RESOLVED
                self._state_mgr.save(state)
                if self._ui:
                    self._ui.info("Issues confirmed; sending back for fixes.")
                return self._debate_resolve_fix(state)
            if state.debate_state:
                state.debate_state.final_verdict = "pass"
                state.debate_state.debate_phase = ReviewDebatePhase.RESOLVED
            self._state_mgr.save(state)
            if self._ui:
                self._ui.info("Implementation approved; moving to merge.")
            return self._debate_resolve_pass(state)
        except BlockedOnCLI as exc:
            return self._transition(
                state,
                WorkflowStatus.BLOCKED_ON_CLI,
                current_phase=WorkflowStatus.REVIEWING.value,
                error=str(exc),
            )
        except StepFailure as exc:
            return self._fail_run(state, self._format_step_failure(exc))

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

    def _review_changed_files(self, state: RunState) -> list[str]:
        if state.is_workspace:
            if not state.workspace_repos:
                result = subprocess.run(
                    ["git", "diff", "--name-only", "HEAD"],
                    cwd=self._repo_root,
                    capture_output=True,
                    text=True,
                    shell=False,
                    check=False,
                )
                if result.returncode != 0:
                    raise EngineError(
                        f"Failed to list changed files: {result.stderr.strip() or result.stdout.strip()}"
                    )
                return [path.strip() for path in result.stdout.splitlines() if path.strip()]
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

    def _debate_resolve_fix(self, state: RunState) -> RunState:
        if (
            self._autonomous_max_iterations is not None
            and state.fix_iteration_count >= self._autonomous_max_iterations
        ):
            return self._transition(
                state,
                WorkflowStatus.PAUSED,
                current_phase=WorkflowStatus.REVIEWING.value,
                error=f"Autonomous limit: {state.fix_iteration_count} fix iterations",
            )
        issues = state.debate_state.consolidated_issues if state.debate_state else []
        feedback = "Issues to fix:\n" + json_block(issues)
        feedback += "\n\nDebate transcript:\n" + self._debate_history_text(state)
        self._artifacts.save_feedback(state.run_id, "planning", feedback)
        state.fix_iteration_count += 1
        state.error = None
        self._state_mgr.save(state)
        return self._transition(state, WorkflowStatus.PLANNING)

    def _debate_resolve_pass(self, state: RunState) -> RunState:
        state.error = None
        self._state_mgr.save(state)
        return self._transition(state, WorkflowStatus.MERGING)

    def _load_saved_claude_review(self, state: RunState) -> dict[str, Any]:
        if not state.review_id:
            raise EngineError("Cannot resume review after Claude phase: missing saved Claude review artifact")
        return self._artifacts.read_json(state.review_id)

    @staticmethod
    def _load_codex_review_from_debate(state: RunState) -> dict[str, Any]:
        if not state.debate_state:
            raise EngineError("Cannot resume review escalation: missing debate state")
        for round_ in reversed(state.debate_state.rounds):
            if round_.actor != "codex":
                continue
            blocks_merge = round_.position in {"issues_confirmed", "issues_accepted"}
            return {
                "review_id": str(uuid4()),
                "verdict": "request_changes" if blocks_merge else "approve",
                "score": 5 if blocks_merge else 9,
                "findings": round_.issues,
                "summary": round_.reasoning,
                "blocks_merge": blocks_merge,
            }
        raise EngineError("Cannot resume review escalation: missing Codex debate round")

    def _record_debate_round(
        self,
        state: RunState,
        *,
        actor: str,
        model_used: str | None,
        effort_used: str | None,
        position: str,
        reasoning: str,
        issues: list[dict[str, Any]],
    ) -> None:
        if state.debate_state is None:
            state.debate_state = DebateState()
        round_number = len(state.debate_state.rounds)
        payload = {
            "round_number": round_number,
            "actor": actor,
            "model_used": model_used,
            "effort_used": effort_used,
            "position": position,
            "reasoning": reasoning,
            "issues": issues,
        }
        ref = self._artifacts.save_debate_round(state.run_id, round_number, payload)
        state.debate_state.rounds.append(
            DebateRound(
                **payload,
                artifact_id=ref,
            )
        )
        self._state_mgr.save(state)

    @staticmethod
    def _review_has_issues(review: dict[str, Any]) -> bool:
        if review.get("blocks_merge"):
            return True
        if review.get("verdict") != "approve":
            return True
        return any(
            finding.get("severity") in {"critical", "major"}
            for finding in review.get("findings", [])
            if isinstance(finding, dict)
        )

    @staticmethod
    def _issues_from_review(review: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            dict(finding)
            for finding in review.get("findings", [])
            if isinstance(finding, dict)
        ]

    @staticmethod
    def _review_issues_match(claude_review: dict[str, Any], codex_review: dict[str, Any]) -> bool:
        claude_signatures = Engine._issue_signatures(claude_review)
        codex_signatures = Engine._issue_signatures(codex_review)
        if not claude_signatures or not codex_signatures:
            return bool(claude_review.get("blocks_merge") and codex_review.get("blocks_merge"))
        return bool(claude_signatures & codex_signatures)

    @staticmethod
    def _issue_signatures(review: dict[str, Any]) -> set[tuple[str, str]]:
        signatures: set[tuple[str, str]] = set()
        for finding in review.get("findings", []):
            if not isinstance(finding, dict):
                continue
            location = str(finding.get("file") or "").strip()
            description = " ".join(str(finding.get("description") or "").lower().split())
            signatures.add((location, description[:120]))
        return signatures

    @staticmethod
    def _review_disagreement_scenario(claude_has_issues: bool, codex_has_issues: bool) -> str:
        if claude_has_issues and codex_has_issues:
            return "B: Claude and Codex both found issues but disagree on specifics."
        if claude_has_issues:
            return "C: Claude found blocking issues and Codex did not."
        return "D: Claude passed the implementation and Codex found blocking issues."

    def _debate_history_text(self, state: RunState) -> str:
        if not state.debate_state or not state.debate_state.rounds:
            return ""
        return json_block(
            [round_item.model_dump(mode="json") for round_item in state.debate_state.rounds]
        )

    def _run_merge(self, state: RunState) -> RunState:
        base_branch = self._config.worktree.base_branch
        task_summary = self._commit_summary_for_state(state)
        execution_summary = ""
        review_summary = ""
        if state.execution_result_ref:
            try:
                execution_summary = str(self._artifacts.read_json(state.execution_result_ref).get("summary") or "")
            except Exception:
                execution_summary = ""
        if state.review_id:
            try:
                review_summary = str(self._artifacts.read_json(state.review_id).get("summary") or "")
            except Exception:
                review_summary = ""
        delivery_message = ""
        try:
            planner_cli = self._phase_cli("planning", config_name="planner")
            delivery_adapter = self._adapter(planner_cli)
            delivery_prompt = build_delivery_prompt(
                task_description=state.normalized_task or state.task,
                plan_text=self._load_plan_text(state.plan_id) if state.plan_id else "",
                execution_summary=execution_summary,
                review_summary=review_summary,
            )
            delivery_result = self._invoke_adapter_text(
                delivery_adapter,
                delivery_prompt,
                self._repo_root,
                spinner_label="Preparing delivery summary...",
                reasoning_effort_override=self._resolve_effort_for_phase(state, "planning", planner_cli),
                model_override=self._resolve_model_for_phase("planning", planner_cli, state),
                resume_session_id=state.session_ids.get("claude_main"),
                timeout_seconds=self._phase_timeout("planning"),
            )
            delivery_message = delivery_result.text.strip()
        except Exception as exc:
            if self._ui:
                self._ui.warn(f"Delivery summary skipped: {exc}")
            delivery_message = ""

        if not state.is_workspace:
            try:
                self._worktrees.verify_merge_preconditions(
                    base_branch,
                    state.base_commit,
                    allow_base_commit_mismatch=False,
                )
            except WorktreeError as exc:
                return self._fail_run(state, str(exc))
            branch = state.worktree_branch or ""
            needs_remote_push = not self._remote_branch_exists("origin", base_branch)
            commands = [
                "# Review changes on the worktree branch:",
                f"git diff {base_branch}..{branch}",
                "",
                "# Apply changes to your branch:",
                f"git merge --squash {branch}",
                f'git commit -m "aio: {task_summary}"',
                "",
                "# Clean up the worktree:",
                "orch clean",
            ]
            if needs_remote_push:
                commands.extend(
                    [
                        "",
                        "# If your base branch does not exist on origin yet:",
                        f"git push -u origin {base_branch}",
                    ]
                )
            if self._config.delivery.auto_commit:
                try:
                    subprocess.run(
                        ["git", "merge", "--squash", branch],
                        cwd=self._repo_root,
                        capture_output=True,
                        text=True,
                        shell=False,
                        check=True,
                    )
                    commit_message = f"aio: {task_summary}"
                    if self._config.delivery.commit_message_from_ai and delivery_message:
                        commit_message = delivery_message.splitlines()[0][:120] or commit_message
                    subprocess.run(
                        ["git", "commit", "-m", commit_message],
                        cwd=self._repo_root,
                        capture_output=True,
                        text=True,
                        shell=False,
                        check=True,
                    )
                    if self._config.delivery.auto_push:
                        subprocess.run(
                            ["git", "push"],
                            cwd=self._repo_root,
                            capture_output=True,
                            text=True,
                            shell=False,
                            check=True,
                        )
                    commands = [
                        "# Changes were committed automatically.",
                        "# Review the commit before sharing.",
                        "git show --stat",
                    ]
                    if self._config.delivery.auto_push:
                        commands.append("# Changes were pushed to remote.")
                except subprocess.CalledProcessError as exc:
                    return self._fail_run(state, f"Delivery auto-commit/push failed: {exc.stderr.strip() or exc.stdout.strip()}")
        else:
            commands = self._generate_workspace_commands(state)

        if delivery_message:
            commands = ["# AI delivery summary:", delivery_message, "", *commands]
        state.commit_commands = commands
        self._artifacts.clear_processed_approval(state.run_id, "merge")
        state.error = None
        self._state_mgr.save(state)
        return self._transition(state, WorkflowStatus.DONE)

    def _remote_branch_exists(self, remote: str, branch: str) -> bool:
        result = subprocess.run(
            ["git", "ls-remote", "--heads", remote, branch],
            cwd=self._repo_root,
            capture_output=True,
            text=True,
            shell=False,
            check=False,
        )
        if result.returncode != 0:
            return False
        return bool(result.stdout.strip())

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
        if self._ui:
            self._ui.phase_transition(state.current_phase)
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
        on_retry: Any | None = None,
    ) -> InvokeResult:
        prompt = initial_prompt
        attempt = 0
        last_error = "the previous attempt failed"
        while True:
            try:
                if self._ui is None:
                    result = self._coerce_invoke_result(invoke(prompt))
                else:
                    label = spinner_label
                    if attempt > 0:
                        label = f"Retrying after: {last_error}"
                    with self._ui.phase_spinner(label):
                        result = self._coerce_invoke_result(invoke(prompt))
            except BlockedOnCLI:
                raise
            except StepFailure as exc:
                attempt += 1
                state.retry_counts[retry_key] = attempt
                self._state_mgr.save(state)
                if attempt >= retries:
                    raise
                if on_retry is not None:
                    on_retry()
                last_error = self._format_step_failure(exc).splitlines()[0][:120]
                retry_error = self._retry_error_message(retry_key, self._format_step_failure(exc))
                prompt = build_retry_prompt(
                    original_prompt=initial_prompt,
                    error_message=retry_error,
                )
                continue

            state.retry_counts[retry_key] = 0
            self._state_mgr.save(state)
            return result

    @staticmethod
    def _coerce_invoke_result(result: Any) -> InvokeResult:
        if isinstance(result, InvokeResult):
            return result
        if isinstance(result, dict):
            return InvokeResult(data=result)
        raise StepFailure("Adapter returned an unsupported result type")

    @staticmethod
    def _coerce_text_to_invoke_result(result: TextInvokeResult) -> InvokeResult:
        return InvokeResult(data={"text": result.text}, session_id=result.session_id)

    def _invoke_adapter_text(
        self,
        adapter: Any,
        prompt: str,
        working_dir: Path,
        spinner_label: str,
        *,
        reasoning_effort_override: str | None = None,
        model_override: str | None = None,
        resume_session_id: str | None = None,
        allowed_tools: list[str] | None = None,
        timeout_seconds: int | None = None,
        legacy_fallback_text: str | None = None,
        with_spinner: bool = True,
    ) -> TextInvokeResult:
        def invoke() -> TextInvokeResult:
            if hasattr(adapter, "invoke_text"):
                kwargs: dict[str, Any] = {
                    "reasoning_effort_override": reasoning_effort_override,
                    "model_override": model_override,
                }
                if resume_session_id is not None:
                    kwargs["resume_session_id"] = resume_session_id
                if allowed_tools is not None:
                    kwargs["allowed_tools"] = allowed_tools
                timeout = timeout_seconds or self._config.orchestrator.watchdog_timeout
                while True:
                    snapshot = dict(kwargs)
                    try:
                        return self._coerce_text_invoke_result(
                            adapter.invoke_text(
                                prompt,
                                working_dir,
                                timeout,
                                **kwargs,
                            )
                        )
                    except TypeError as exc:
                        message = str(exc)
                        removed = False
                        if "allowed_tools" in message and "allowed_tools" in kwargs:
                            kwargs.pop("allowed_tools", None)
                            removed = True
                        if "resume_session_id" in message and "resume_session_id" in kwargs:
                            kwargs.pop("resume_session_id", None)
                            removed = True
                        if not removed or kwargs == snapshot:
                            raise
                        continue
            if legacy_fallback_text is not None:
                return TextInvokeResult(legacy_fallback_text)
            schema = load_bundled_schema("scoping.schema.json")
            result = self._coerce_invoke_result(
                self._invoke_adapter_json(
                    adapter,
                    prompt,
                    working_dir,
                    schema,
                    reasoning_effort_override=reasoning_effort_override,
                    model_override=model_override,
                    resume_session_id=resume_session_id,
                    allowed_tools=allowed_tools,
                    timeout_seconds=timeout_seconds,
                )
            )
            return TextInvokeResult(
                text=self._scope_md_from_scoping_result(result.data),
                session_id=result.session_id,
            )

        if self._ui is None or not with_spinner:
            return invoke()
        with self._ui.phase_spinner(spinner_label):
            return invoke()

    @staticmethod
    def _coerce_text_invoke_result(result: Any) -> TextInvokeResult:
        if isinstance(result, TextInvokeResult):
            return result
        if isinstance(result, str):
            return TextInvokeResult(text=result)
        if isinstance(result, tuple) and len(result) == 2:
            text, session_id = result
            return TextInvokeResult(
                text=str(text),
                session_id=session_id if isinstance(session_id, str) else None,
            )
        raise StepFailure("Adapter returned an unsupported text result type")

    def _invoke_adapter_json(
        self,
        adapter: Any,
        prompt: str,
        working_dir: Path,
        schema: dict[str, Any],
        *,
        reasoning_effort_override: str | None = None,
        model_override: str | None = None,
        resume_session_id: str | None = None,
        allowed_tools: list[str] | None = None,
        timeout_seconds: int | None = None,
    ) -> Any:
        kwargs: dict[str, Any] = {
            "reasoning_effort_override": reasoning_effort_override,
            "model_override": model_override,
        }
        if resume_session_id is not None:
            kwargs["resume_session_id"] = resume_session_id
        if allowed_tools is not None:
            kwargs["allowed_tools"] = allowed_tools
        timeout = timeout_seconds or self._config.orchestrator.watchdog_timeout
        while True:
            snapshot = dict(kwargs)
            try:
                return adapter.invoke(
                    prompt,
                    working_dir,
                    timeout,
                    schema,
                    **kwargs,
                )
            except TypeError as exc:
                message = str(exc)
                removed = False
                if "allowed_tools" in message and "allowed_tools" in kwargs:
                    kwargs.pop("allowed_tools", None)
                    removed = True
                if "resume_session_id" in message and "resume_session_id" in kwargs:
                    kwargs.pop("resume_session_id", None)
                    removed = True
                if not removed or kwargs == snapshot:
                    raise

    @staticmethod
    def _scope_md_from_scoping_result(result: dict[str, Any]) -> str:
        key_files = result.get("key_files") or []
        key_file_lines = "\n".join(f"  - {path}" for path in key_files) or "  - README.md"
        return (
            "---\n"
            f"normalized_task: {result.get('normalized_task', '')}\n"
            f"complexity_tier: {result.get('complexity_tier', 'moderate')}\n"
            f"actionable: {str(result.get('actionable', True)).lower()}\n"
            "key_files:\n"
            f"{key_file_lines}\n"
            f"context: {result.get('blocking_reason') or 'Scoped from legacy JSON result.'}\n"
            "---\n\n"
            f"{result.get('normalized_task', '')}\n"
        )

    @staticmethod
    def _scope_review_agreed(markdown: str) -> bool:
        frontmatter = Engine._parse_scope_frontmatter(markdown)
        if "agreement" in frontmatter:
            return Engine._coerce_bool(frontmatter["agreement"], default=False)
        lowered = markdown.lower()
        if re.search(r"\bagreement\s*:\s*true\b", lowered):
            return True
        return False

    @staticmethod
    def _parse_scope_frontmatter(markdown: str) -> dict[str, Any]:
        text = markdown.strip()
        if not text.startswith("---"):
            return {}
        end = text.find("\n---", 3)
        if end == -1:
            return {}
        block = text[3:end].strip()
        parsed: dict[str, Any] = {}
        current_key: str | None = None
        for raw_line in block.splitlines():
            line = raw_line.rstrip()
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("-") and current_key:
                parsed.setdefault(current_key, []).append(stripped[1:].strip().strip("'\""))
                continue
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip()
            current_key = key
            if not value:
                parsed[key] = []
            elif value.startswith("[") and value.endswith("]"):
                try:
                    parsed[key] = json.loads(value)
                except json.JSONDecodeError:
                    parsed[key] = value
            else:
                parsed[key] = value.strip("'\"")
        return parsed

    @staticmethod
    def _coerce_bool(value: Any, *, default: bool) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"true", "yes", "1"}:
                return True
            if lowered in {"false", "no", "0"}:
                return False
        return default

    def _adapter_for_phase(self, phase_name: str) -> Any:
        cli_name = self._phase_cli(
            phase_name,
            config_name={
                "scoping": "scoper",
                "planning": "planner",
                "executing": "worker",
                "reviewing": "reviewer",
            }[phase_name],
        )
        return self._adapter(cli_name)

    def _adapter(self, cli_name: str) -> Any:
        if cli_name in self._adapters:
            return self._adapters[cli_name]
        try:
            adapter = self._adapter_registry.get(cli_name)
        except KeyError as exc:
            raise EngineError(f"Unsupported adapter: {cli_name}")
        except Exception as exc:
            raise EngineError(f"Failed to initialize adapter: {cli_name}") from exc
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

        complexity_tier = state.execution_overrides.get("complexity_tier") or state.complexity_tier
        if complexity_tier:
            tier_map = getattr(self._config.efforts.complexity, complexity_tier, None)
            if tier_map:
                effort = getattr(tier_map, phase_name, "")
                if effort:
                    return effort

        return getattr(getattr(self._config.efforts, cli_name), "default", "") or None

    def _review_final_effort(self, state: RunState) -> str:
        tier = state.execution_overrides.get("complexity_tier") or state.complexity_tier or "moderate"
        return getattr(self._config.efforts.review_final, tier, "high")

    def _resolve_model_for_phase(
        self,
        phase_name: str,
        cli_name: str,
        state: RunState | None = None,
    ) -> str | None:
        override = self._config.routing.phases.get(phase_name)
        if override and override.model:
            return override.model
        complexity_tier = (
            (state.execution_overrides.get("complexity_tier") if state else None)
            or (state.complexity_tier if state else None)
        )
        if override and complexity_tier:
            tier_model = getattr(override, f"model_{complexity_tier}", "")
            if tier_model:
                return tier_model
        if complexity_tier and phase_name in {"planning", "executing"}:
            tier_models = getattr(self._config.models, phase_name, None)
            if tier_models:
                tier_model = getattr(tier_models, complexity_tier, "")
                if tier_model:
                    return tier_model
        if phase_name == "reviewing" and cli_name == "codex":
            codex_model = getattr(self._config.models.reviewing, "codex", "")
            if codex_model:
                return codex_model
        if phase_name == "scoping":
            if cli_name == "claude":
                scoped_model = getattr(self._config.models.scoping, "claude", "")
                if scoped_model:
                    return scoped_model
            if cli_name == "gemini":
                scoped_model = getattr(self._config.models.scoping, "gemini", "")
                if scoped_model:
                    return scoped_model
            scoped_codex = getattr(self._config.models.scoping, "codex", "")
            if scoped_codex:
                return scoped_codex
        return getattr(getattr(self._config.models, cli_name), "default", "") or None

    def _phase_allowed_tools(self, phase_name: str, *, default: list[str]) -> list[str] | None:
        override = self._config.routing.phases.get(phase_name)
        if override and override.allowed_tools:
            return list(override.allowed_tools)
        return list(default)

    def _phase_timeout(self, phase_name: str) -> int:
        override = self._config.routing.phases.get(phase_name)
        if override and override.timeout_seconds > 0:
            return override.timeout_seconds
        base = self._config.orchestrator.watchdog_timeout
        if phase_name in {"planning", "reviewing"}:
            return max(base, 5400)
        return base

    @staticmethod
    def _model_label(model: str | None, cli_name: str) -> str:
        if not model:
            return cli_name.title()
        parts = model.split("-")
        if len(parts) > 1 and parts[1]:
            return parts[1].title()
        return model

    def _scoping_message(self, key: str, fallback: str) -> str:
        if self._ui and hasattr(self._ui, "scoping_message"):
            return self._ui.scoping_message(key)
        return fallback

    def _reviewing_message(self, key: str, fallback: str) -> str:
        if self._ui and hasattr(self._ui, "reviewing_message"):
            return self._ui.reviewing_message(key)
        return fallback

    def _executing_message(self, fallback: str, cli: str = "codex") -> str:
        if self._ui and hasattr(self._ui, "executing_message"):
            return self._ui.executing_message(cli)
        return fallback

    def _retry_limit(self, workflow_phase: str) -> int:
        config_limit = self._config.orchestrator.max_retries
        phase_limit = self._workflow.phase(workflow_phase).retries
        return config_limit or phase_limit

    @staticmethod
    def _retry_error_message(retry_key: str, error_message: str) -> str:
        if "is a required property" not in error_message:
            return error_message
        guidance = "A required property is missing. Re-read OUTPUT SCHEMA and include every required field."
        if retry_key in {"reviewing", "reviewing-codex"}:
            guidance += (
                " Review responses must include: verdict and score at minimum "
                "(review_id, findings, summary, and blocks_merge are auto-generated if omitted)."
            )
        elif retry_key == "review-final-claude":
            guidance += (
                " Debate responses must include: position and reasoning at minimum "
                "(issues defaults to [] if omitted)."
            )
        return f"{error_message}\n\n{guidance}"

    def resolve_execution_settings(self, state: RunState) -> dict[str, str | bool | None]:
        """Return resolved execution settings for display and approval decisions."""
        overrides = state.execution_overrides or {}
        worker_name = overrides.get("cli") or self._phase_cli("executing", config_name="worker")
        return {
            "cli": worker_name,
            "model": overrides.get("model") or self._resolve_model_for_phase("executing", worker_name, state),
            "effort": overrides.get("effort") or self._resolve_effort_for_phase(state, "executing", worker_name),
            "complexity_tier": state.complexity_tier,
            "has_overrides": bool(overrides),
        }

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

    def _commit_worktree_all(
        self,
        state: RunState,
        worktree_dir: Path,
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
            ["git", "commit", "-m", f"aio: {description}"],
            cwd=worktree_dir,
            capture_output=True,
            text=True,
            shell=False,
            check=False,
        )
        if commit.returncode != 0 and "nothing to commit" not in commit.stderr.lower():
            raise EngineError(f"git commit failed: {commit.stderr.strip() or commit.stdout.strip()}")

    def _load_step_results(self, state: RunState) -> list[dict[str, Any]]:
        return [self._artifacts.read_json(reference) for reference in state.step_results]

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
        if not state.workspace_repos:
            result = subprocess.run(
                ["git", "diff", "HEAD"],
                cwd=self._repo_root,
                capture_output=True,
                text=True,
                shell=False,
                check=False,
            )
            if result.returncode != 0:
                raise EngineError(
                    f"Failed to collect workspace diff: {result.stderr.strip() or result.stdout.strip()}"
                )
            if result.stdout.strip():
                diffs["."] = result.stdout
            return diffs
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
        task_summary = self._commit_summary_for_state(state)
        base_branch = self._config.worktree.base_branch
        commands: list[str] = []
        if not state.workspace_repos:
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=self._repo_root,
                capture_output=True,
                text=True,
                shell=False,
                check=False,
            )
            if result.returncode != 0:
                raise EngineError(
                    f"Failed to inspect repository: {result.stderr.strip() or result.stdout.strip()}"
                )
            if result.stdout.strip():
                return [
                    "git add .",
                    f'git commit -m "aio: {task_summary}"',
                    f"git push origin {base_branch}",
                ]
            return ["# No changes detected."]
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

    def _load_plan_text(self, reference: str | None) -> str:
        if not reference:
            raise EngineError("Required artifact reference is missing")
        if reference.endswith(".md"):
            return self._artifacts.read_text(reference)
        return json_block(self._artifacts.read_json(reference))

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
        first_line = next((line.strip() for line in task.splitlines() if line.strip()), "")
        summary = " ".join(first_line.lstrip("#").split())
        return summary[:72] if len(summary) > 72 else summary

    def _commit_summary_for_state(self, state: RunState) -> str:
        if state.normalized_task and state.normalized_task.strip() != state.task.strip():
            return self._task_summary(state.normalized_task)
        if state.scope_md_ref:
            frontmatter = self._parse_scope_frontmatter(self._artifacts.read_text(state.scope_md_ref))
            normalized = str(frontmatter.get("normalized_task") or "").strip()
            if normalized:
                return self._task_summary(normalized)
        return self._task_summary(state.task)

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
        self._create_execution_history(state, terminal_reason=message)
        return self._transition(
            state,
            WorkflowStatus.FAILED,
            current_phase=state.current_phase,
            error=message,
        )

    def _terminate_run(self, state: RunState, message: str) -> RunState:
        self._create_execution_history(state, terminal_reason=message)
        return self._transition(
            state,
            WorkflowStatus.TERMINATED,
            current_phase=state.current_phase,
            error=message,
        )

    def _create_execution_history(self, state: RunState, *, terminal_reason: str) -> str:
        lines = [
            f"# Execution History: {state.run_id}",
            "",
            f"Task: {state.task}",
            f"Status: {state.status}",
            f"Current phase: {state.current_phase}",
            f"Terminal reason: {terminal_reason}",
            "",
            "## Scoping",
            f"- Scope: {state.scope_md_ref or '<none>'}",
            f"- Participant scopes: {json.dumps(state.ai_scope_refs, sort_keys=True) if state.ai_scope_refs else '<none>'}",
            f"- Normalized task: {state.normalized_task or '<none>'}",
            f"- Complexity: {state.complexity_tier or '<none>'}",
            "",
            "## Artifacts",
            f"- Plan: {state.plan_id or '<none>'}",
            f"- Review: {state.review_id or '<none>'}",
            "",
            "## Step Results",
        ]
        lines.extend(f"- {reference}" for reference in state.step_results)
        if not state.step_results:
            lines.append("- <none>")
        if state.debate_state:
            lines.extend(["", "## Debate", self._debate_history_text(state) or "<none>"])
        ref = self._artifacts.save_execution_history(state.run_id, "\n".join(lines) + "\n")
        return ref
