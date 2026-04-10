"""Rich terminal UI helpers for ai-orchestrator."""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Generator, Iterable

from rich import box
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.prompt import Confirm, Prompt
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from .models import Plan, RunState, WorkflowStatus


ACTIVE_STATES = {
    WorkflowStatus.SCOPING.value,
    WorkflowStatus.PLANNING.value,
    WorkflowStatus.FEASIBILITY.value,
    WorkflowStatus.EXECUTING.value,
    WorkflowStatus.REVIEWING.value,
    WorkflowStatus.ADJUDICATING.value,
    WorkflowStatus.MERGING.value,
    WorkflowStatus.BLOCKED_ON_CLI.value,
    WorkflowStatus.CONFLICT.value,
}

TERMINAL_STATES = {
    WorkflowStatus.DONE.value,
    WorkflowStatus.FAILED.value,
}

_ACTIVE_STATES = ACTIVE_STATES
_TERMINAL_STATES = TERMINAL_STATES

_STATUS_STYLES = {
    WorkflowStatus.INIT.value: "cyan",
    WorkflowStatus.SCOPING.value: "bright_cyan",
    WorkflowStatus.PLANNING.value: "cyan",
    WorkflowStatus.APPROVAL_PLAN.value: "yellow",
    WorkflowStatus.FEASIBILITY.value: "bright_magenta",
    WorkflowStatus.EXECUTING.value: "magenta",
    WorkflowStatus.REVIEWING.value: "blue",
    WorkflowStatus.ADJUDICATING.value: "bright_blue",
    WorkflowStatus.MERGING.value: "green",
    WorkflowStatus.DONE.value: "bold green",
    WorkflowStatus.FAILED.value: "bold red",
    WorkflowStatus.PAUSED.value: "yellow",
    WorkflowStatus.BLOCKED_ON_CLI.value: "bold yellow",
    WorkflowStatus.CONFLICT.value: "bold red",
}


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

    @contextmanager
    def phase_spinner(self, description: str) -> Generator[None, None, None]:
        """Display a progress spinner with elapsed time."""
        progress = Progress(
            SpinnerColumn(style="bold cyan"),
            TextColumn("[progress.description]{task.description}"),
            TimeElapsedColumn(),
            console=self.stderr_console,
            transient=True,
        )
        with progress:
            progress.add_task(description, total=None)
            yield

    def print_plan(
        self,
        plan: Plan | dict[str, Any],
        *,
        run_id: str | None = None,
        detailed: bool = False,
    ) -> None:
        """Render a plan summary table or full detail view."""
        payload = self._coerce_plan(plan)
        renderable = (
            self.render_plan_detail(payload, run_id=run_id)
            if detailed
            else self._render_plan(payload, run_id=run_id)
        )
        self.console.print(renderable)

    def print_scoping_result(self, result: dict[str, Any]) -> None:
        """Render the scoping output."""
        self.console.print(self._render_scoping_result(result))

    def print_feasibility_result(self, result: dict[str, Any]) -> None:
        """Render the feasibility output."""
        self.console.print(self._render_feasibility_result(result))

    def print_status(
        self,
        state: RunState,
        *,
        plan: Plan | dict[str, Any] | None = None,
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
        plan: Plan | dict[str, Any] | None = None,
        step_results: Iterable[dict[str, Any]] | None = None,
        log_entries: Iterable[dict[str, Any]] | None = None,
    ) -> RenderableType:
        """Build a status dashboard renderable."""
        payload_plan = self._coerce_plan(plan) if plan else None
        results = list(step_results or [])
        logs = list(log_entries or [])
        total_steps = len(payload_plan.steps) if payload_plan else max(len(results), 1)
        completed_steps = min(len(results), total_steps)

        summary = Table.grid(expand=True)
        summary.add_column(justify="left", ratio=1)
        summary.add_column(justify="left", ratio=2)
        summary.add_row("Run", state.run_id)
        summary.add_row("Task", self._truncate(state.task, 88))
        summary.add_row("Phase", state.current_phase)
        summary.add_row("Status", Text(state.status, style=self._status_style(state.status)))
        summary.add_row("Updated", state.updated_at)
        if state.normalized_task:
            summary.add_row("Normalized", self._truncate(state.normalized_task, 88))
        if state.complexity_tier:
            summary.add_row("Complexity", state.complexity_tier)
        if state.error:
            summary.add_row("Error", Text(self._truncate(state.error, 88), style="bold red"))

        progress = Progress(
            TextColumn("[bold]Progress[/bold]"),
            BarColumn(bar_width=None, complete_style="green", finished_style="green"),
            TaskProgressColumn(),
            console=self.console,
            expand=True,
        )
        progress.add_task(
            "steps",
            total=max(total_steps, 1),
            completed=completed_steps,
        )

        details = Table(
            box=box.SIMPLE_HEAVY,
            expand=True,
            show_header=True,
            header_style="bold cyan",
        )
        details.add_column("Step", width=6)
        details.add_column("Status", width=10)
        details.add_column("Summary")
        for result in results[-5:]:
            details.add_row(
                str(result.get("step_number", "?")),
                str(result.get("status", "unknown")),
                self._truncate(str(result.get("summary", "")), 72),
            )
        if not results:
            details.add_row("-", "-", "No completed steps yet.")

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
            Panel(progress, title="Execution"),
            Panel(details, title="Recent Step Results"),
            Panel(event_table, title="Recent Events"),
        ]
        if state.status == WorkflowStatus.FAILED.value and state.error:
            panels.insert(
                1,
                Panel(Text(state.error, overflow="fold"), title="Failure Detail", border_style="bold red"),
            )
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
                state.current_phase,
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
                title="[bold green]Changes staged — run these commands to commit",
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

    def rejection_reason(self, default: str) -> str:
        """Prompt for a rejection reason."""
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
        self.stderr_console.print(Panel(message, title="Error", border_style="bold red"))

    def warning(self, message: str) -> None:
        self.stderr_console.print(Panel(message, title="Warning", border_style="yellow"))

    def info(self, message: str) -> None:
        self.stderr_console.print(Text(message, style="dim"))

    def render_plan_detail(
        self,
        plan: Plan | dict[str, Any],
        *,
        run_id: str | None = None,
    ) -> RenderableType:
        """Render the full plan without truncation."""
        payload = self._coerce_plan(plan)
        sections: list[RenderableType] = [
            Text(payload.task, style="bold"),
            Panel(
                Text(payload.reasoning, overflow="fold"),
                title="Reasoning",
                border_style="dim",
            ),
        ]

        for step in payload.steps:
            detail = Table.grid(expand=True, padding=(0, 1))
            detail.add_column(style="bold cyan", width=12)
            detail.add_column(ratio=1)
            detail.add_row("Description", step.description)
            detail.add_row("Complexity", step.estimated_complexity.value)
            detail.add_row("Depends", ", ".join(str(item) for item in step.depends_on) or "-")
            detail.add_row("Read", "\n".join(step.files_to_read) or "-")
            detail.add_row("Modify", "\n".join(step.files_to_modify) or "-")
            sections.append(
                Panel(
                    detail,
                    title=f"Step {step.step_number}",
                    border_style="cyan",
                )
            )

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

    def _render_plan(self, plan: Plan, *, run_id: str | None = None) -> RenderableType:
        table = Table(
            box=box.ROUNDED,
            expand=True,
            header_style="bold cyan",
        )
        table.add_column("#", width=4, justify="right")
        table.add_column("Complexity", width=10)
        table.add_column("Description", ratio=3)
        table.add_column("Read", ratio=2)
        table.add_column("Modify", ratio=2)
        table.add_column("Depends", width=10)
        for step in plan.steps:
            table.add_row(
                str(step.step_number),
                step.estimated_complexity.value,
                self._truncate(step.description, 46),
                self._truncate(", ".join(step.files_to_read) or "-", 26),
                self._truncate(", ".join(step.files_to_modify) or "-", 26),
                ", ".join(str(item) for item in step.depends_on) or "-",
            )
        body: list[RenderableType] = [
            Text(plan.task, style="bold"),
            Text(self._truncate(plan.reasoning, 120), style="dim"),
            table,
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

    def _render_feasibility_result(self, result: dict[str, Any]) -> RenderableType:
        table = Table(box=box.ROUNDED, expand=True, header_style="bold magenta")
        table.add_column("Verdict", width=18)
        table.add_column("Summary")
        table.add_row(str(result.get("verdict", "")), self._truncate(str(result.get("summary", "")), 96))

        issues = Table(box=box.SIMPLE, expand=True, header_style="bold magenta")
        issues.add_column("Severity", width=12)
        issues.add_column("Description")
        blocking_issues = result.get("blocking_issues", [])
        if blocking_issues:
            for issue in blocking_issues:
                issues.add_row(str(issue.get("severity", "")), str(issue.get("description", "")))
        else:
            issues.add_row("-", "No blocking issues.")
        return Panel(Group(table, issues), title="Feasibility Result", border_style="bright_magenta")

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


__all__ = [
    "OrchestratorUI",
    "ACTIVE_STATES",
    "TERMINAL_STATES",
]
