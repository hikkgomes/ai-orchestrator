"""N-participant scoping conversation flow."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .adapters.base import BlockedOnCLI, TextInvokeResult
from .models import RunState, WorkflowStatus
from .parallel import invoke_parallel
from .prompts.templates import (
    build_scoping_cross_review_prompt,
    build_scoping_initial_prompt,
    build_scoping_user_reply_prompt,
)


@dataclass
class ScopingConversation:
    """Generic user-in-the-loop scoping flow for N adapters."""

    engine: Any

    def run(self, state: RunState) -> RunState:
        participants = list(self.engine._config.scoping.participants or ["claude", "codex"])
        state.scoping_participants = participants
        prior_feedback = self.engine._artifacts.load_feedback(state.run_id, "scoping")

        try:
            if len(participants) == 1:
                responses = self.run_initial_round(state.task, state, participants, prior_feedback)
                agreed_scope = next(iter(responses.values()))
                self._persist_scoping(state, responses, agreed_scope, round_num=1, agreed=True)
            else:
                responses = self.run_initial_round(state.task, state, participants, prior_feedback)
                agreed_scope = responses.get(participants[0], next(iter(responses.values())))
                agreed = False
                max_rounds = max(1, int(self.engine._config.scoping.max_rounds or 6))
                for round_num in range(2, max_rounds + 1):
                    responses, agreed = self.run_cross_review_round(state, round_num, responses)
                    agreed_scope = responses.get(participants[0], next(iter(responses.values())))
                    self._persist_scoping(state, responses, agreed_scope, round_num=round_num, agreed=agreed)
                    if agreed:
                        break

                decider = (self.engine._config.scoping.designated_decider or "").strip()
                if not agreed and decider and decider in responses:
                    agreed_scope = responses[decider]
                    agreed = True
                    self._persist_scoping(state, responses, agreed_scope, round_num=state.scoping_round, agreed=True)

                if not agreed and decider:
                    agreed = True

            scope_md = self.engine._artifacts.read_text(state.scope_md_ref)
            frontmatter = self.engine._parse_scope_frontmatter(scope_md)
            state.normalized_task = str(frontmatter.get("normalized_task") or state.task)
            state.complexity_tier = str(frontmatter.get("complexity_tier") or "moderate")
            actionable = self.engine._coerce_bool(frontmatter.get("actionable"), default=True)
            state.error = None
            self.engine._state_mgr.save(state)
            if self.engine._ui and state.complexity_tier:
                self.engine._ui.info(f"Complexity assessed as: {state.complexity_tier}")

            if self.engine._config.scoping.require_user_approval:
                return self.engine._transition(
                    state,
                    WorkflowStatus.PAUSED,
                    current_phase=WorkflowStatus.SCOPING.value,
                    error=None,
                )
            if actionable:
                return self.engine._transition(state, WorkflowStatus.PLANNING)
            return self.engine._transition(
                state,
                WorkflowStatus.PAUSED,
                current_phase=WorkflowStatus.SCOPING.value,
                error=str(frontmatter.get("context") or "Task requires scoping review"),
            )
        except BlockedOnCLI as exc:
            return self.engine._transition(
                state,
                WorkflowStatus.BLOCKED_ON_CLI,
                current_phase=WorkflowStatus.SCOPING.value,
                error=str(exc),
            )

    def run_initial_round(
        self,
        raw_task: str,
        state: RunState,
        participants: list[str],
        prior_feedback: str | None,
    ) -> dict[str, str]:
        prompt = build_scoping_initial_prompt(raw_task)
        if prior_feedback:
            previous_scope = self.engine._artifacts.read_text(state.scope_md_ref) if state.scope_md_ref else ""
            prompt = build_scoping_user_reply_prompt(prior_feedback, previous_scope)
        tasks = []
        for cli_name in participants:
            adapter = self.engine._adapter(cli_name)
            effort = self.engine._config.efforts.scoping.initial
            model = self.engine._resolve_model_for_phase("scoping", cli_name, state)
            tasks.append(
                lambda a=adapter, p=prompt, c=cli_name, e=effort, m=model: self.engine._invoke_adapter_text(
                    a,
                    p,
                    self.engine._repo_root,
                    spinner_label=f"Scoping with {c}...",
                    reasoning_effort_override=e,
                    model_override=m,
                    resume_session_id=state.session_ids.get(f"scoping_{c}"),
                    allowed_tools=self.engine._phase_allowed_tools("scoping", default=["Read", "Grep", "Glob"]),
                    timeout_seconds=self.engine._phase_timeout("scoping"),
                )
            )
        results = invoke_parallel(tasks)
        out: dict[str, str] = {}
        for cli_name, result in zip(participants, results):
            assert isinstance(result, TextInvokeResult)
            out[cli_name] = result.text
            if result.session_id:
                state.session_ids[f"scoping_{cli_name}"] = result.session_id
                if cli_name == "claude" and self.engine._config.sessions.enable_unified_session:
                    state.session_ids["claude_main"] = result.session_id
        return out

    def run_cross_review_round(
        self,
        state: RunState,
        round_num: int,
        previous_responses: dict[str, str],
    ) -> tuple[dict[str, str], bool]:
        participants = list(state.scoping_participants)
        tasks = []
        for cli_name in participants:
            adapter = self.engine._adapter(cli_name)
            other = {k: v for k, v in previous_responses.items() if k != cli_name}
            prompt = build_scoping_cross_review_prompt(other)
            effort = (
                self.engine._config.efforts.scoping.escalation
                if round_num >= max(2, self.engine._config.scoping.max_rounds - 1)
                else self.engine._config.efforts.scoping.comparison
            )
            model = self.engine._resolve_model_for_phase("scoping", cli_name, state)
            tasks.append(
                lambda a=adapter, p=prompt, c=cli_name, e=effort, m=model: self.engine._invoke_adapter_text(
                    a,
                    p,
                    self.engine._repo_root,
                    spinner_label=f"Cross-review with {c}...",
                    reasoning_effort_override=e,
                    model_override=m,
                    resume_session_id=state.session_ids.get(f"scoping_{c}"),
                    allowed_tools=self.engine._phase_allowed_tools("scoping", default=["Read", "Grep", "Glob"]),
                    timeout_seconds=self.engine._phase_timeout("scoping"),
                )
            )
        results = invoke_parallel(tasks)
        out: dict[str, str] = {}
        for cli_name, result in zip(participants, results):
            assert isinstance(result, TextInvokeResult)
            out[cli_name] = result.text
            if result.session_id:
                state.session_ids[f"scoping_{cli_name}"] = result.session_id
                if cli_name == "claude" and self.engine._config.sessions.enable_unified_session:
                    state.session_ids["claude_main"] = result.session_id
        agreed = all(self.engine._scope_review_agreed(text) for text in out.values())
        return out, agreed

    def _persist_scoping(
        self,
        state: RunState,
        responses: dict[str, str],
        agreed_scope: str,
        *,
        round_num: int,
        agreed: bool,
    ) -> None:
        for cli_name, text in responses.items():
            state.ai_scope_refs[cli_name] = self.engine._artifacts.save_ai_scope(
                state.run_id,
                cli_name,
                round_num,
                text,
            )
        state.scope_md_ref = self.engine._artifacts.save_scope_md(state.run_id, agreed_scope)
        state.scoping_round = round_num
        state.scoping_agreed = agreed
        self.engine._state_mgr.save(state)
        if self.engine._ui:
            self.engine._ui.print_scoping_conversation(responses, round_num)
