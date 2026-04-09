from __future__ import annotations

from rich.console import Console

from ai_orchestrator.doctor import DoctorCheck, DoctorReport
from ai_orchestrator.models import RunState, WorkflowStatus
from ai_orchestrator.ui import OrchestratorUI


def _ui():
    console = Console(record=True, force_terminal=False, width=120)
    stderr_console = Console(record=True, force_terminal=False, width=120)
    return OrchestratorUI(console=console, stderr_console=stderr_console), console, stderr_console


def test_print_plan_renders_table():
    ui, console, _ = _ui()

    ui.print_plan(
        {
            "plan_id": "00000000-0000-0000-0000-000000000000",
            "task": "Ship the terminal UI",
            "steps": [
                {
                    "step_number": 1,
                    "description": "Render a status dashboard",
                    "files_to_read": ["src/ai_orchestrator/ui.py"],
                    "files_to_modify": ["src/ai_orchestrator/ui.py"],
                    "depends_on": [],
                    "estimated_complexity": "medium",
                }
            ],
            "reasoning": "Single focused UI change.",
        }
    )

    output = console.export_text()
    assert "Implementation Plan" in output
    assert "Render a status dashboard" in output


def test_render_status_includes_run_metadata():
    ui, console, _ = _ui()
    state = RunState(run_id="run-123", task="Test the status UI")
    state.status = WorkflowStatus.EXECUTING
    state.current_phase = "EXECUTING"

    ui.print_status(
        state,
        step_results=[
            {
                "step_number": 1,
                "status": "success",
                "summary": "Implemented the first step",
            }
        ],
        log_entries=[{"timestamp": "2026-04-09T12:00:00+00:00", "event": "state_saved"}],
    )

    output = console.export_text()
    assert "Run Summary" in output
    assert "run-123" in output
    assert "Implemented the first step" in output


def test_print_doctor_report_renders_all_checks():
    ui, console, _ = _ui()
    report = DoctorReport(
        checks=[
            DoctorCheck(name="python", status="pass", summary="Python 3.11.9 is supported."),
            DoctorCheck(name="repo-config", status="warn", summary="aio.toml is missing.", hint="Run `orch init`."),
        ]
    )

    ui.print_doctor_report(report)

    output = console.export_text()
    assert "WARN" in output
    assert "repo-config" in output
    assert "orch init" in output
