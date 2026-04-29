"""Analysis mode and reusable text debate loop."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .adapters.base import StepFailure, TextInvokeResult
from .adapters.claude import ClaudeAdapter
from .adapters.codex import CodexAdapter
from .artifacts import ArtifactStore
from .models import AnalysisSession
from .modes import AnalysisSettings
from .parallel import invoke_parallel
from .prompts.templates import (
    build_analysis_debate_prompt,
    build_analysis_prompt,
    build_analysis_synthesis_prompt,
)


@dataclass
class Round:
    round_number: int
    actor: str
    text: str


class DebateLoop:
    """Small text debate loop shared by analysis-like workflows."""

    def __init__(self, repo_root: Path, timeout: int, *, ui: Any | None = None) -> None:
        self._repo_root = repo_root
        self._timeout = timeout
        self._ui = ui

    def run(
        self,
        claude: Any,
        codex: Any,
        initial_claude: str,
        initial_codex: str,
        rounds: int,
        escalation_settings: dict[str, str],
        *,
        claude_session_id: str | None = None,
        codex_session_id: str | None = None,
    ) -> tuple[list[Round], str, str, bool]:
        rows: list[Round] = []
        latest_claude = initial_claude
        latest_codex = initial_codex
        consensus_reached = False
        for round_number in range(1, max(0, rounds) + 1):
            is_final = round_number == rounds
            model = escalation_settings.get("model") if is_final else None
            effort = escalation_settings.get("effort") if is_final else None

            if round_number % 2 == 1:
                codex_result = codex.invoke_text(
                    build_analysis_debate_prompt(latest_claude),
                    self._repo_root,
                    self._timeout,
                    model_override=model or None,
                    reasoning_effort_override=effort or None,
                    resume_session_id=codex_session_id,
                )
                latest_codex = codex_result.text
                codex_session_id = codex_result.session_id or codex_session_id
                rows.append(Round(round_number, "codex", latest_codex))
                if self._ui:
                    self._ui.print_analysis_round(round_number, "codex", latest_codex)

                claude_result = claude.invoke_text(
                    build_analysis_debate_prompt(latest_codex),
                    self._repo_root,
                    self._timeout,
                    model_override=model or None,
                    reasoning_effort_override=effort or None,
                    resume_session_id=claude_session_id,
                )
                latest_claude = claude_result.text
                claude_session_id = claude_result.session_id or claude_session_id
                rows.append(Round(round_number, "claude", latest_claude))
                if self._ui:
                    self._ui.print_analysis_round(round_number, "claude", latest_claude)
                consensus_reached = _check_consensus(latest_claude)
            else:
                claude_result = claude.invoke_text(
                    build_analysis_debate_prompt(latest_codex),
                    self._repo_root,
                    self._timeout,
                    model_override=model or None,
                    reasoning_effort_override=effort or None,
                    resume_session_id=claude_session_id,
                )
                latest_claude = claude_result.text
                claude_session_id = claude_result.session_id or claude_session_id
                rows.append(Round(round_number, "claude", latest_claude))
                if self._ui:
                    self._ui.print_analysis_round(round_number, "claude", latest_claude)

                codex_result = codex.invoke_text(
                    build_analysis_debate_prompt(latest_claude),
                    self._repo_root,
                    self._timeout,
                    model_override=model or None,
                    reasoning_effort_override=effort or None,
                    resume_session_id=codex_session_id,
                )
                latest_codex = codex_result.text
                codex_session_id = codex_result.session_id or codex_session_id
                rows.append(Round(round_number, "codex", latest_codex))
                if self._ui:
                    self._ui.print_analysis_round(round_number, "codex", latest_codex)
                consensus_reached = _check_consensus(latest_codex)
            if consensus_reached:
                break
        return rows, claude_session_id or "", codex_session_id or "", consensus_reached


class AnalysisRunner:
    """Run saved analysis sessions outside the workflow FSM."""

    def __init__(self, config: Any, repo_root: Path, artifact_root: Path, *, ui: Any | None = None) -> None:
        self._config = config
        self._repo_root = repo_root
        self._artifact_root = artifact_root
        self._artifacts = ArtifactStore(artifact_root, retain_prompts=config.logging.retain_prompts)
        self._ui = ui

    def run(self, task: str, settings: AnalysisSettings) -> AnalysisSession:
        session = self._execute_analysis(task, settings)
        self._artifacts.save_analysis_session(session)
        if self._ui:
            self._ui.print_analysis_result(session)
        return session

    def continue_session(self, session_id: str, follow_up: str, settings: AnalysisSettings) -> AnalysisSession:
        prior = self._artifacts.load_analysis_session(session_id)
        task = prior.task if not follow_up else f"{prior.task}\n\nFOLLOW-UP:\n{follow_up}"
        session = self._execute_analysis(task, settings)
        session.session_id = prior.session_id
        session.created_at = prior.created_at
        session.rounds = [*prior.rounds, *session.rounds]
        self._artifacts.save_analysis_session(session)
        if self._ui:
            self._ui.print_analysis_result(session)
        return session

    def _execute_analysis(self, task: str, settings: AnalysisSettings) -> AnalysisSession:
        session_id = str(uuid4())
        claude = ClaudeAdapter(self._config, self._artifact_root)
        codex = CodexAdapter(self._config, self._artifact_root)
        prompt = build_analysis_prompt(task)
        timeout = self._config.orchestrator.watchdog_timeout

        claude_result, codex_result = invoke_parallel(
            [
                lambda: _safe_invoke_text(lambda: claude.invoke_text(
                    prompt,
                    self._repo_root,
                    timeout,
                    model_override=settings.claude_model or None,
                    reasoning_effort_override=settings.claude_effort,
                )),
                lambda: _safe_invoke_text(lambda: codex.invoke_text(
                    prompt,
                    self._repo_root,
                    timeout,
                    model_override=settings.codex_model or None,
                    reasoning_effort_override=settings.codex_effort,
                )),
            ]
        )
        if self._ui:
            self._ui.print_analysis_round(0, "claude", claude_result.text)
            self._ui.print_analysis_round(0, "codex", codex_result.text)

        loop = DebateLoop(self._repo_root, timeout, ui=self._ui)
        rounds, claude_session_id, _, consensus_reached = loop.run(
            claude,
            codex,
            claude_result.text,
            codex_result.text,
            settings.rounds,
            {"model": settings.escalation_model, "effort": settings.escalation_effort},
            claude_session_id=claude_result.session_id,
            codex_session_id=codex_result.session_id,
        )
        synthesis = claude.invoke_text(
            build_analysis_synthesis_prompt(),
            self._repo_root,
            timeout,
            model_override=settings.escalation_model or None,
            reasoning_effort_override=settings.escalation_effort,
            resume_session_id=claude_session_id or None,
        ).text
        now = datetime.now(timezone.utc).isoformat()
        session = AnalysisSession(
            session_id=session_id,
            task=task,
            rounds=[asdict(round_) for round_ in rounds],
            claude_initial=claude_result.text,
            codex_initial=codex_result.text,
            consensus_reached=consensus_reached,
            final_summary=synthesis,
            settings=asdict(settings),
            created_at=now,
            updated_at=now,
        )
        return session


def _check_consensus(text: str) -> bool:
    for line in text.splitlines()[:10]:
        stripped = line.strip().lower()
        if stripped.startswith("agreement:"):
            return "true" in stripped
    return False


def _safe_invoke_text(invoke: Any) -> TextInvokeResult:
    try:
        return invoke()
    except StepFailure as exc:
        detail = exc.validation_error or str(exc)
        return TextInvokeResult(text=f"Analysis failed: {detail}")
