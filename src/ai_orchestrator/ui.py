"""Rich terminal UI helpers for ai-orchestrator."""

from __future__ import annotations

import json
import random
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Generator, Iterable

from rich import box
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.progress import (
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.prompt import Confirm, Prompt
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

try:  # pragma: no cover - optional dependency fallback for minimal test envs
    from prompt_toolkit.formatted_text import FormattedText
except Exception:  # pragma: no cover
    FormattedText = list  # type: ignore[misc,assignment]

from .models import Plan, RunState, WorkflowStatus

try:  # pragma: no cover - optional dependency fallback for minimal test envs
    import questionary
except Exception:  # pragma: no cover
    questionary = None
try:  # pragma: no cover - optional dependency fallback for minimal test envs
    from prompt_toolkit import PromptSession
    from prompt_toolkit.key_binding import KeyBindings

    _prompt_toolkit_available = True
except Exception:  # pragma: no cover
    PromptSession = None
    KeyBindings = None
    _prompt_toolkit_available = False


ACTIVE_STATES = {
    WorkflowStatus.SCOPING.value,
    WorkflowStatus.PLANNING.value,
    WorkflowStatus.EXECUTING.value,
    WorkflowStatus.REVIEWING.value,
    WorkflowStatus.MERGING.value,
    WorkflowStatus.BLOCKED_ON_CLI.value,
    WorkflowStatus.CONFLICT.value,
}

TERMINAL_STATES = {
    WorkflowStatus.DONE.value,
    WorkflowStatus.FAILED.value,
    WorkflowStatus.TERMINATED.value,
}

_ACTIVE_STATES = ACTIVE_STATES
_TERMINAL_STATES = TERMINAL_STATES

_STATUS_STYLES = {
    WorkflowStatus.INIT.value: "cyan",
    WorkflowStatus.SCOPING.value: "bright_cyan",
    WorkflowStatus.PLANNING.value: "cyan",
    WorkflowStatus.APPROVAL_PLAN.value: "yellow",
    WorkflowStatus.EXECUTING.value: "cyan",
    WorkflowStatus.REVIEWING.value: "cyan",
    WorkflowStatus.MERGING.value: "cyan",
    WorkflowStatus.DONE.value: "bold green",
    WorkflowStatus.FAILED.value: "bold red",
    WorkflowStatus.TERMINATED.value: "bold yellow",
    WorkflowStatus.PAUSED.value: "yellow",
    WorkflowStatus.BLOCKED_ON_CLI.value: "bold red",
    WorkflowStatus.CONFLICT.value: "bold red",
}

_FRIENDLY_PHASE_NAMES = {
    WorkflowStatus.INIT.value: "Starting",
    WorkflowStatus.SCOPING.value: "Scoping",
    WorkflowStatus.PLANNING.value: "Planning",
    WorkflowStatus.APPROVAL_PLAN.value: "Plan Review",
    WorkflowStatus.EXECUTING.value: "Building",
    WorkflowStatus.REVIEWING.value: "Code Review",
    WorkflowStatus.MERGING.value: "Handoff",
    WorkflowStatus.DONE.value: "Complete",
    WorkflowStatus.FAILED.value: "Failed",
    WorkflowStatus.TERMINATED.value: "Stopped",
    WorkflowStatus.PAUSED.value: "Waiting on You",
    WorkflowStatus.BLOCKED_ON_CLI.value: "Blocked",
    WorkflowStatus.CONFLICT.value: "Conflict",
}

_PHASE_SUBTITLES = {
    WorkflowStatus.SCOPING.value: "Multiple AIs are sizing up the task",
    WorkflowStatus.PLANNING.value: "Claude is designing the approach",
    WorkflowStatus.APPROVAL_PLAN.value: "Your turn to review",
    WorkflowStatus.EXECUTING.value: "Codex is building",
    WorkflowStatus.REVIEWING.value: "Time for a code review",
    WorkflowStatus.MERGING.value: "Preparing your changes for review",
    WorkflowStatus.DONE.value: "All done",
}

_SCOPING_MESSAGES = {
    "initial": [
        "AIs are drafting their initial scope...",
        "Participants are independently sizing up the task...",
    ],
    "cross_review": [
        "AIs are cross-reviewing each other...",
        "Participants are reconciling differences...",
    ],
}

_EXECUTING_MESSAGES = {
    "codex": [
        "Codex is building the implementation...",
        "Codex is writing code...",
        "Codex is working through the plan...",
        "Implementation in progress...",
        "Codex is on it...",
    ],
    "claude": [
        "Claude is building the implementation...",
        "Claude is writing code...",
        "Claude is working through the plan...",
        "Implementation in progress...",
        "Claude is on it...",
    ],
}

_REVIEWING_MESSAGES = {
    "claude_reviews": [
        "Claude is reviewing the implementation...",
        "Claude is checking the code...",
        "Claude is running the review pipeline...",
    ],
    "codex_reviews": [
        "Codex is forming its verdict...",
        "Codex is cross-checking Claude's review...",
        "Codex is doing an independent assessment...",
    ],
    "claude_final": [
        "Claude Opus is making the final review call...",
        "Claude Opus is resolving the review debate...",
        "Claude is making the code review decision...",
    ],
}

MODE_COLORS = {
    "default": "#888888",
    "analysis": "#b58900",
    "quick_execute": "#859900",
    "review": "#268bd2",
    "autonomous": "#dc322f",
}

MODE_LABELS = {
    "default": "",
    "analysis": "analysis",
    "quick_execute": "execute",
    "review": "review",
    "autonomous": "auto",
}

CLAUDE_MODELS = [
    "(default)",
    "opus",
    "sonnet",
    "haiku",
    "opus[1m]",
    "sonnet[1m]",
]

CODEX_MODELS = [
    "(default)",
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.3-codex",
    "gpt-5.2",
]

GEMINI_MODELS = [
    "(default)",
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
]

EFFORT_LEVELS = ["low", "medium", "high", "xhigh", "max"]


def build_prompt_message(mode: str) -> FormattedText:
    label = MODE_LABELS.get(mode, mode)
    if label:
        color = MODE_COLORS.get(mode, "#888888")
        return FormattedText(
            [
                (f"bold {color}", f"[{label}]"),
                ("", " "),
                ("#666666", "❯ "),
            ]
        )
    return FormattedText([("#666666", "❯ ")])


def random_message(pool: list[str]) -> str:
    return random.choice(pool)


def friendly_phase(phase: str) -> str:
    return _FRIENDLY_PHASE_NAMES.get(phase, phase.title().replace("_", " "))


def _next_step_info(state: RunState) -> tuple[str, str]:
    phase = state.current_phase
    if phase == WorkflowStatus.INIT.value:
        return "The orchestrator is initializing your run.", "cyan"
    if phase == WorkflowStatus.SCOPING.value:
        return "Claude and Codex are evaluating the task. No action needed.", "cyan"
    if phase == WorkflowStatus.PLANNING.value:
        return "Claude is designing the implementation plan. No action needed.", "cyan"
    if phase == WorkflowStatus.APPROVAL_PLAN.value:
        return "The plan is ready for your review.", "yellow"
    if phase == WorkflowStatus.EXECUTING.value:
        return "The executor is writing code. No action needed.", "cyan"
    if phase == WorkflowStatus.REVIEWING.value:
        return "Claude and Codex are reviewing the code. No action needed.", "cyan"
    if phase == WorkflowStatus.MERGING.value:
        return "Changes are being prepared for handoff.", "cyan"
    if phase == WorkflowStatus.DONE.value:
        return "Run complete! Review the suggested commands below.", "green"
    if phase == WorkflowStatus.FAILED.value:
        return "This run failed. Use `orch continue` to retry, or start a new run.", "red"
    if phase == WorkflowStatus.TERMINATED.value:
        return "This run was stopped.", "dim"
    if phase == WorkflowStatus.BLOCKED_ON_CLI.value:
        return "A CLI tool is unavailable. Run `orch doctor` to check.", "red"
    if phase == WorkflowStatus.CONFLICT.value:
        return "Merge conflict detected. Resolve manually, then `orch continue`.", "red"
    return "No action needed right now.", "dim"


class OrchestratorUI:
    """Rich terminal UI helper."""

    def __init__(
        self,
        *,
        console: Console | None = None,
        stderr_console: Console | None = None,
    ) -> None:
        self.console = console or Console()
        self.stderr_console = stderr_console or Console(stderr=True)
        self._last_banner_phase: str | None = None

    @contextmanager
    def phase_spinner(self, description: str) -> Generator[None, None, None]:
        """Display a progress spinner with elapsed time."""
        started = time.monotonic()
        progress = Progress(
            SpinnerColumn(style="bold cyan"),
            TextColumn("[progress.description]{task.description}"),
            TimeElapsedColumn(),
            console=self.stderr_console,
            transient=False,
        )
        with progress:
            progress.add_task(description, total=None)
            yield
        elapsed = time.monotonic() - started
        self.stderr_console.print(Text(f"{description} complete ({elapsed:.0f}s)", style="dim"))

    def phase_transition(self, phase: str) -> None:
        """Emit a persistent phase transition status line."""
        if phase == self._last_banner_phase:
            return
        self._last_banner_phase = phase
        self.phase_banner(phase)

    def phase_banner(self, phase: str, detail: str = "") -> None:
        """Print a visible phase separator."""
        subtitle = detail or _PHASE_SUBTITLES.get(phase, "")
        label = f"▸ {friendly_phase(phase)}"
        if subtitle:
            label += f": {subtitle}"
        self.stderr_console.print(f"\n[bold cyan]{label}[/bold cyan]")

    def scoping_message(self, key: str) -> str:
        return random_message(_SCOPING_MESSAGES.get(key, ["Scoping the task..."]))

    def executing_message(self, cli: str = "codex") -> str:
        return random_message(_EXECUTING_MESSAGES.get(cli, ["Implementation in progress..."]))

    def reviewing_message(self, key: str) -> str:
        return random_message(_REVIEWING_MESSAGES.get(key, ["Reviewing the implementation..."]))

    def print_plan(
        self,
        plan: Plan | dict[str, Any] | str,
        *,
        run_id: str | None = None,
        detailed: bool = False,
    ) -> None:
        """Render a plan summary table or full detail view."""
        payload = plan if isinstance(plan, str) else self._coerce_plan(plan)
        renderable = (
            self.render_plan_detail(payload, run_id=run_id)
            if detailed
            else self._render_plan(payload, run_id=run_id)
        )
        self.console.print(renderable)

    def print_scoping_result(self, result: dict[str, Any]) -> None:
        """Render the scoping output."""
        self.console.print(self._render_scoping_result(result))

    def print_scoping_conversation(self, responses: dict[str, str], round_num: int) -> None:
        for ai_name, response in responses.items():
            self.console.print(
                Panel(
                    Markdown(response or "_No response_"),
                    title=f"Scoping Round {round_num} · {ai_name}",
                    border_style="cyan",
                )
            )

    def print_status(
        self,
        state: RunState,
        *,
        plan: Plan | dict[str, Any] | str | None = None,
        step_results: Iterable[dict[str, Any]] | None = None,
        log_entries: Iterable[dict[str, Any]] | None = None,
    ) -> None:
        """Render a single-run status dashboard."""
        self.console.print(
            self.render_status(
                state,
                plan=plan,
                step_results=step_results,
                log_entries=log_entries,
            )
        )

    def render_status(
        self,
        state: RunState,
        *,
        plan: Plan | dict[str, Any] | str | None = None,
        step_results: Iterable[dict[str, Any]] | None = None,
        log_entries: Iterable[dict[str, Any]] | None = None,
    ) -> RenderableType:
        """Build a status dashboard renderable."""
        results = list(step_results or [])
        logs = [
            entry
            for entry in list(log_entries or [])
            if str(entry.get("event", "")) not in {"state_saved"}
        ]

        summary = Table.grid(expand=True)
        summary.add_column(justify="left", ratio=1)
        summary.add_column(justify="left", ratio=2)
        summary.add_row("Run", state.run_id)
        summary.add_row("Task", Text(state.task, overflow="fold"))
        summary.add_row("Phase", friendly_phase(state.current_phase))
        summary.add_row("Status", Text(state.status, style=self._status_style(state.status)))
        summary.add_row("Updated", state.updated_at)
        if state.normalized_task and state.normalized_task.strip() != state.task.strip():
            summary.add_row("Normalized", Text(state.normalized_task, overflow="fold"))
        if state.complexity_tier:
            summary.add_row("Complexity", state.complexity_tier)
        if state.error:
            summary.add_row("Error", Text(state.error, style="bold red", overflow="fold"))

        details = Table(
            box=box.SIMPLE_HEAVY,
            expand=True,
            show_header=True,
            header_style="bold cyan",
        )
        details.add_column("Result", width=10)
        details.add_column("Status", width=10)
        details.add_column("Summary")
        for index, result in enumerate(results[-5:], start=1):
            details.add_row(
                str(result.get("step_number", index)),
                str(result.get("status", "unknown")),
                Text(str(result.get("summary", "")), overflow="fold"),
            )
        if not results:
            details.add_row("-", "-", "No execution result yet.")

        event_table = Table(
            box=box.SIMPLE,
            expand=True,
            show_header=True,
            header_style="bold blue",
        )
        event_table.add_column("Time", width=14)
        event_table.add_column("Event")
        if logs:
            for entry in logs[-5:]:
                timestamp = str(entry.get("timestamp", ""))[-14:]
                event_table.add_row(timestamp, str(entry.get("event", "unknown")))
        else:
            event_table.add_row("-", "No event log entries yet.")

        panels: list[RenderableType] = [
            Panel(summary, title="Run Summary", border_style=self._status_style(state.status)),
            Panel(details, title="Execution Results"),
        ]
        if state.status == WorkflowStatus.DONE.value:
            panels.insert(1, Panel("Complete", title="Execution", border_style="green"))
        elif state.status in ACTIVE_STATES:
            panels.insert(1, Panel("In progress", title="Execution", border_style="cyan"))
        if logs:
            panels.append(Panel(event_table, title="Recent Events"))
        if state.debate_state and self._enum_value(
            state.debate_state.debate_phase
        ) != "initial_reviews":
            debate = state.debate_state
            debate_table = Table.grid(expand=True)
            debate_table.add_column(justify="left", ratio=1)
            debate_table.add_column(justify="left", ratio=2)
            debate_table.add_row("Sub-phase", self._enum_value(debate.debate_phase))
            debate_table.add_row("Disagreement", debate.disagreement_case or "-")
            debate_table.add_row("Rounds", str(len(debate.rounds)))
            if debate.rounds:
                latest = debate.rounds[-1]
                debate_table.add_row(
                    "Latest",
                    (
                    f"{latest.actor}: {latest.position} - "
                    f"{self._truncate(latest.reasoning, 60)}"
                    ),
                )
            panels.insert(
                2,
                Panel(
                    debate_table,
                    title="Review Debate",
                    border_style="yellow",
                ),
            )
        if state.status == WorkflowStatus.FAILED.value and state.error:
            panels.insert(
                1,
                Panel(Text(state.error, overflow="fold"), title="Failure Detail", border_style="bold red"),
            )
        next_msg, next_style = _next_step_info(state)
        panels.append(Panel(next_msg, title="What happens next", border_style=next_style))
        body = Group(*panels)
        return body

    def render_runs_overview(self, states: Iterable[RunState]) -> RenderableType:
        """Build an overview table for multiple runs."""
        table = Table(
            box=box.ROUNDED,
            expand=True,
            header_style="bold cyan",
        )
        table.add_column("Run ID", style="bold")
        table.add_column("Status")
        table.add_column("Phase")
        table.add_column("Task")
        table.add_column("Updated", width=26)
        has_rows = False
        for state in states:
            has_rows = True
            table.add_row(
                state.run_id,
                Text(state.status, style=self._status_style(state.status)),
                friendly_phase(state.current_phase),
                self._truncate(state.task, 60),
                state.updated_at,
            )
        if not has_rows:
            table.add_row("-", "-", "-", "No runs found.", "-")
        return table

    def watch(
        self,
        render: Callable[[], RenderableType],
        *,
        stop_when: Callable[[], bool] | None = None,
        refresh_per_second: float = 4.0,
    ) -> None:
        """Continuously refresh a renderable until interrupted or complete."""
        with Live(render(), console=self.console, refresh_per_second=refresh_per_second) as live:
            while True:
                if stop_when and stop_when():
                    live.update(render())
                    break
                time.sleep(max(0.1, 1.0 / refresh_per_second))
                live.update(render())

    def print_diff_summary(self, diff: str) -> None:
        """Render a diff or diff-stat summary."""
        self.console.print(self._render_diff(diff))

    def print_commit_suggestions(self, commands: list[str]) -> None:
        """Render suggested handoff commands after a successful run."""
        self.console.print()
        self.console.print(
            Panel(
                "\n".join(commands),
                title="[bold green]Handoff — review and commit when ready",
                border_style="green",
            )
        )

    def print_doctor_fix_actions(self, actions: list[dict[str, str]]) -> None:
        table = Table(box=box.ROUNDED, expand=True, header_style="bold cyan")
        table.add_column("Check", style="bold")
        table.add_column("Result", width=12)
        table.add_column("Action")
        for action in actions:
            status = action.get("status", "warn")
            marker = "[green]fixed[/green]" if status == "fixed" else "[yellow]manual[/yellow]"
            table.add_row(action.get("name", "unknown"), marker, action.get("detail", ""))
        self.console.print(Panel(table, title="Doctor Fix Actions", border_style="cyan"))

    def print_doctor_ready(self) -> None:
        self.console.print(
            Panel(
                "All checks passed. You're ready to go.\n"
                '  orch run "your task"    Start a new run\n'
                "  orch                    Open the interactive shell",
                title="Ready",
                border_style="green",
            )
        )

    def print_logs(self, text: str, *, title: str = "Logs") -> None:
        """Render log output with syntax highlighting when possible."""
        renderable: RenderableType
        if self._looks_like_jsonl(text):
            renderable = Syntax(text, "json", line_numbers=False, word_wrap=True)
        else:
            renderable = Syntax(text, "text", line_numbers=False, word_wrap=True)
        self.console.print(Panel(renderable, title=title))

    def print_mode_header(self, mode: str, settings: Any | None = None) -> None:
        details = ""
        if settings is not None:
            details = " | " + " | ".join(
                f"{key}: {value}" for key, value in vars(settings).items() if value not in {"", None}
            )
        self.console.print(Panel(f"Mode: {mode}{details}", title="Orchestrator Mode", border_style="cyan"))

    def print_analysis_round(self, round_num: int, actor: str, text: str) -> None:
        title = f"{actor.title()} Analysis" if round_num == 0 else f"Round {round_num}: {actor.title()}"
        self.console.print(Panel(Markdown(text), title=title, border_style="blue"))

    def print_analysis_result(self, session: Any) -> None:
        self.console.print(
            Panel(
                Markdown(str(session.final_summary or "")),
                title=f"Analysis Result {str(session.session_id)[:8]}",
                border_style="green",
            )
        )

    def print_doctor_report(self, report: Any) -> None:
        """Render doctor checks as a rich table."""
        table = Table(
            box=box.ROUNDED,
            expand=True,
            header_style="bold cyan",
        )
        table.add_column("Check", style="bold")
        table.add_column("Status", width=10)
        table.add_column("Summary")
        table.add_column("Hint")
        for check in report.checks:
            status_text = Text(check.status.upper(), style=self._doctor_style(check.status))
            table.add_row(check.name, status_text, check.summary, check.hint or "")
        title = f"Doctor: {report.overall_status.upper()}"
        self.console.print(Panel(table, title=title, border_style=self._doctor_style(report.overall_status)))

    def approval_prompt(self, gate: str, context: str) -> bool:
        """Prompt the user to approve or reject a gate."""
        panel = Panel(
            context,
            title=f"{gate.title()} Approval",
            border_style="yellow",
        )
        self.console.print(panel)
        return Confirm.ask(f"Approve {gate}?", console=self.console, default=True)

    def approval_choice(
        self,
        gate: str,
        context: str,
        *,
        choices: list[str],
        default: str,
    ) -> str:
        """Prompt the user for a named gate decision."""
        panel = Panel(
            context,
            title="Your Decision",
            border_style="yellow",
        )
        self.console.print(panel)
        if questionary is not None and sys.stdin.isatty():
            label_map = {
                "approve": "Approve — start building",
                "soft-reject": "Request changes — revise the plan",
                "full-reject": "Reject — stop this run",
                "adjust": "Adjust execution settings",
                "approve-override": "Approve override",
                "approve-claude": "Approve Claude plan",
                "approve-codex": "Accept Codex assessment",
                "fix": "Fix issues",
                "pass": "Pass",
            }
            reverse = {label_map.get(choice, choice): choice for choice in choices}
            selected = questionary.select(
                f"{gate.title()} decision:",
                choices=list(reverse),
                default=label_map.get(default, default),
            ).ask()
            if selected:
                return reverse[selected]
        return Prompt.ask(
            f"Decision for {gate}",
            choices=choices,
            default=default,
            console=self.console,
        )

    def render_home_screen(
        self,
        *,
        repo_name: str,
        version: str,
        has_config: bool,
        active_runs: list[RunState],
        paused_runs: list[RunState],
    ) -> RenderableType:
        lines: list[RenderableType] = []
        if paused_runs:
            lines.append(Text(f"{len(paused_runs)} run waiting on you", style="yellow"))
            for state in paused_runs[:3]:
                lines.append(Text(f"  {state.run_id[:8]}  {friendly_phase(state.current_phase):<12} {self._truncate(state.task, 60)}"))
            lines.append(Text(""))
        if active_runs:
            lines.append(Text(f"{len(active_runs)} run in progress", style="cyan"))
            for state in active_runs[:3]:
                lines.append(Text(f"  {state.run_id[:8]}  {friendly_phase(state.current_phase):<12} {self._truncate(state.task, 60)}"))
            lines.append(Text(""))
        if not has_config:
            lines.append(Text("Run `orch init` to scaffold repository config.", style="yellow"))
            lines.append(Text(""))
        lines.extend(
            [
                Text("Quick start:", style="dim"),
                Text('  orch run "your task"'),
                Text("  orch doctor"),
                Text("  Type a task here to start"),
            ]
        )
        return Panel(
            Group(*lines),
            title=f"ai-orchestrator v{version} - ~/{repo_name}",
            border_style="cyan",
        )

    def print_first_run_tutorial(self) -> None:
        self.console.print(
            Panel(
                "Your repository is set up.\n\n"
                "1. Check your environment:  orch doctor\n"
                '2. Start your first run:    orch run "task"\n'
                "3. Or review changes:       orch review\n\n"
                "Tip: type 'orch' to enter the interactive shell, where you can just type your task.",
                title="Getting Started",
                border_style="cyan",
            )
        )

    def rejection_reason(self, default: str) -> str:
        """Prompt for a rejection reason."""
        if _prompt_toolkit_available and sys.stdin.isatty():
            bindings = KeyBindings()

            @bindings.add("enter")
            def submit(event) -> None:
                event.current_buffer.validate_and_handle()

            @bindings.add("escape", "enter")
            def newline(event) -> None:
                event.current_buffer.insert_text("\n")

            self.stderr_console.print(
                "[yellow]Enter feedback (Enter to submit, Alt+Enter for new line):[/yellow]"
            )
            session = PromptSession(
                key_bindings=bindings,
                multiline=True,
                enable_open_in_editor=True,
            )
            response = session.prompt("  > ")
            return (response or "").strip() or default
        return Prompt.ask("Rejection reason", default=default, console=self.console)

    def print_file_updates(self, actions: Iterable[tuple[str, str]]) -> None:
        """Render files created or updated by a command."""
        table = Table(box=box.ROUNDED, header_style="bold cyan")
        table.add_column("Action", width=12)
        table.add_column("Path")
        for action, path in actions:
            table.add_row(action, path)
        self.console.print(table)

    def print_install_shell_result(self, shell: str, destination: Path) -> None:
        """Render a shell-install success panel."""
        self.console.print(
            Panel.fit(
                f"{shell} integration written to [bold]{destination}[/bold]",
                title="Shell Installed",
                border_style="green",
            )
        )

    def error(self, message: str) -> None:
        if "\n" in message:
            self.stderr_console.print(Panel(message, title="Error", border_style="bold red"))
            return
        self.stderr_console.print(f"  [bold red]✗ {message}[/bold red]")

    def warning(self, message: str) -> None:
        self.stderr_console.print(f"  [yellow]⚠ {message}[/yellow]")

    def info(self, message: str) -> None:
        self.stderr_console.print(Text(message, style="dim"))

    def print_execution_info(self, settings: dict[str, str | bool | None]) -> None:
        tier = str(settings.get("complexity_tier") or "unknown")
        cli = str(settings.get("cli") or "codex").capitalize()
        model = str(settings.get("model") or "default")
        effort = str(settings.get("effort") or "default")
        suffix = " (overridden)" if settings.get("has_overrides") else ""
        self.stderr_console.print(
            Text(
                f"Scoping complexity: {tier} (assessed by Claude + Codex)  |  "
                f"Executor: {cli}  |  Model: {model}  |  Reasoning: {effort}{suffix}",
                style="dim",
            )
        )

    def render_plan_detail(
        self,
        plan: Plan | dict[str, Any] | str,
        *,
        run_id: str | None = None,
    ) -> RenderableType:
        """Render the full plan without truncation."""
        if isinstance(plan, str):
            sections: list[RenderableType] = [Markdown(plan)]
            if run_id:
                short_id = run_id[:8]
                sections.append(
                    Text(
                        f"Approve with `orch approve {short_id} plan` or reject with "
                        f"`orch reject {short_id} plan --reason ...`",
                        style="dim",
                    )
                )
            return Panel(Group(*sections), title="Implementation Plan", border_style="cyan")

        payload = self._coerce_plan(plan)
        steps = "\n".join(
            f"{index}. {step}" for index, step in enumerate(payload.implementation_steps, start=1)
        )
        key_files = "\n".join(payload.key_files) or "-"
        sections: list[RenderableType] = [
            Text(payload.task, style="bold"),
            Panel(
                Text(payload.approach, overflow="fold"),
                title="Approach",
                border_style="dim",
            ),
            Panel(Text(steps, overflow="fold"), title="Implementation Steps", border_style="cyan"),
            Panel(Text(key_files, overflow="fold"), title="Key Files", border_style="cyan"),
        ]

        if run_id:
            short_id = run_id[:8]
            sections.append(
                Text(
                    f"Approve with `orch approve {short_id} plan` or reject with "
                    f"`orch reject {short_id} plan --reason ...`",
                    style="dim",
                )
            )

        return Panel(Group(*sections), title="Implementation Plan", border_style="cyan")

    def _render_plan(self, plan: Plan | str, *, run_id: str | None = None) -> RenderableType:
        if isinstance(plan, str):
            body: list[RenderableType] = [
                Markdown(plan),
            ]
            if run_id:
                body.append(Text(f"Run `orch show {run_id[:8]} plan` to view the full plan.", style="dim"))
            return Panel(
                Group(*body),
                title="Implementation Plan",
                border_style="cyan",
            )

        steps = Table(box=box.ROUNDED, expand=True, header_style="bold cyan")
        steps.add_column("#", width=4, justify="right")
        steps.add_column("Implementation Step")
        for index, step in enumerate(plan.implementation_steps, start=1):
            steps.add_row(str(index), self._truncate(step, 92))

        key_files = Text(", ".join(plan.key_files) if plan.key_files else "-", style="dim")
        body: list[RenderableType] = [
            Text(plan.task, style="bold"),
            Panel(Text(plan.approach, overflow="fold"), title="Approach", border_style="dim"),
            steps,
            Panel(key_files, title="Key Files", border_style="dim"),
        ]
        if run_id:
            body.append(Text(f"Run `orch show {run_id[:8]} plan` to view the full plan.", style="dim"))
        return Panel(
            Group(*body),
            title="Implementation Plan",
            border_style="cyan",
        )

    def _render_scoping_result(self, result: dict[str, Any]) -> RenderableType:
        table = Table(box=box.ROUNDED, expand=True, header_style="bold cyan")
        table.add_column("Field", width=16)
        table.add_column("Value")
        table.add_row("Normalized Task", str(result.get("normalized_task", "")))
        table.add_row("Complexity Tier", str(result.get("complexity_tier", "")))
        if result.get("blocking_reason"):
            table.add_row("Blocking Reason", str(result["blocking_reason"]))
        return Panel(table, title="Scoping Result", border_style="bright_cyan")

    def _render_diff(self, diff: str) -> RenderableType:
        lines = [line for line in diff.splitlines() if line.strip()]
        if not lines:
            return Panel("No diff available.", title="Diff Summary")

        table = Table(box=box.SIMPLE_HEAVY, header_style="bold cyan", expand=True)
        table.add_column("Type", width=12)
        table.add_column("Content")
        if all(("|" in line or "changed" in line) for line in lines[: min(8, len(lines))]):
            for line in lines[:20]:
                table.add_row("stat", line)
        else:
            added = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
            removed = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))
            table.add_row("summary", f"{added} additions, {removed} deletions")
            preview = "\n".join(lines[:20])
            table.add_row("preview", preview)
        return Panel(table, title="Diff Summary")

    @staticmethod
    def _looks_like_jsonl(text: str) -> bool:
        try:
            first = next(line for line in text.splitlines() if line.strip())
        except StopIteration:
            return False
        try:
            json.loads(first)
        except json.JSONDecodeError:
            return False
        return True

    @staticmethod
    def _truncate(value: str, limit: int) -> str:
        if len(value) <= limit:
            return value
        return value[: limit - 3] + "..."

    @staticmethod
    def _coerce_plan(plan: Plan | dict[str, Any]) -> Plan:
        if isinstance(plan, Plan):
            return plan
        return Plan.model_validate(plan)

    @staticmethod
    def _status_style(status: str) -> str:
        return _STATUS_STYLES.get(status, "white")

    @staticmethod
    def _doctor_style(status: str) -> str:
        return {
            "pass": "green",
            "warn": "yellow",
            "fail": "bold red",
        }.get(status, "white")

    @staticmethod
    def _enum_value(value: Any) -> str:
        return str(getattr(value, "value", value))


__all__ = [
    "OrchestratorUI",
    "ACTIVE_STATES",
    "TERMINAL_STATES",
]
