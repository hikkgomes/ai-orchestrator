from __future__ import annotations

from rich.console import Console

from ai_orchestrator.doctor import DoctorCheck, DoctorReport
from ai_orchestrator.models import RunState, WorkflowStatus
from ai_orchestrator.ui import OrchestratorUI


def _plan_dict(approach: str = "Single focused UI change."):
    return {
        "plan_id": "00000000-0000-0000-0000-000000000000",
        "task": "Ship the terminal UI",
        "approach": approach,
        "implementation_steps": ["Render a status dashboard"],
        "key_files": ["src/ai_orchestrator/ui.py", "src/ai_orchestrator/cli.py"],
    }


def _ui():
    console = Console(record=True, force_terminal=False, width=120)
    stderr_console = Console(record=True, force_terminal=False, width=120)
    return OrchestratorUI(console=console, stderr_console=stderr_console), console, stderr_console


def test_print_plan_renders_table():
    ui, console, _ = _ui()

    ui.print_plan(_plan_dict())

    output = console.export_text()
    assert "Implementation Plan" in output
    assert "Render a status dashboard" in output


def test_print_plan_summary_includes_show_hint_when_run_id_is_available():
    ui, console, _ = _ui()

    ui.print_plan(_plan_dict(), run_id="12345678-0000-0000-0000-000000000000")

    output = console.export_text()
    assert "orch show 12345678 plan" in output


def test_render_plan_detail_shows_full_approach_and_file_lists():
    ui, console, _ = _ui()
    reasoning = "Because the approval decision depends on reading the full step context without truncation."

    ui.print_plan(
        _plan_dict(reasoning),
        run_id="12345678-0000-0000-0000-000000000000",
        detailed=True,
    )

    output = console.export_text()
    assert reasoning in output
    assert "src/ai_orchestrator/ui.py" in output
    assert "src/ai_orchestrator/cli.py" in output
    assert "orch approve 12345678 plan" in output


def test_render_plan_detail_supports_markdown_plan():
    ui, console, _ = _ui()
    markdown_plan = "## Approach\nShip safely.\n\n## Steps\n1. Update code\n\n## Key Files\n- src/app.py\n"

    ui.print_plan(markdown_plan, run_id="12345678-0000-0000-0000-000000000000", detailed=True)

    output = console.export_text()
    assert "Ship safely." in output
    assert "src/app.py" in output
    assert "orch approve 12345678 plan" in output


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


def test_render_status_shows_failure_detail_panel_for_failed_runs():
    ui, console, _ = _ui()
    state = RunState(run_id="run-fail", task="Test failure detail")
    state.status = WorkflowStatus.FAILED
    state.current_phase = "SCOPING"
    state.error = "Claude CLI exited with a non-zero status\nstderr: error: unknown option '--effort'\nexit_code: 2"

    ui.print_status(state)

    output = console.export_text()
    assert "Failure Detail" in output
    assert "error: unknown option '--effort'" in output
    assert "exit_code: 2" in output


def test_render_status_wraps_paused_error_without_truncating():
    ui, console, _ = _ui()
    state = RunState(run_id="run-paused", task="Test paused error")
    state.status = WorkflowStatus.PAUSED
    state.current_phase = "SCOPING"
    state.error = (
        "Task is blocked because the scoping debate reached the feasibility limit "
        "and needs operator clarification before planning can continue."
    )

    ui.print_status(state)

    output = console.export_text()
    assert "operator clarification before planning can continue" in output
    assert "..." not in output


def test_print_scoping_result():
    ui, console, _ = _ui()

    ui.print_scoping_result(
        {
            "normalized_task": "Fix typo in README",
            "complexity_tier": "simple",
            "blocking_reason": "Need a repository-scoped task.",
        }
    )
    output = console.export_text()
    assert "Scoping Result" in output
    assert "Fix typo in README" in output


def test_approval_choice_panel_omits_options_line(monkeypatch):
    ui, console, _ = _ui()
    monkeypatch.setattr("ai_orchestrator.ui.Prompt.ask", lambda *args, **kwargs: "approve")

    choice = ui.approval_choice(
        "plan",
        "Run paused at plan approval.",
        choices=["approve", "soft-reject"],
        default="approve",
    )

    assert choice == "approve"
    output = console.export_text()
    assert "Run paused at plan approval." in output
    assert "Options:" not in output


def test_print_execution_info_shows_overrides_marker():
    ui, _, stderr_console = _ui()
    ui.print_execution_info(
        {
            "complexity_tier": "moderate",
            "cli": "codex",
            "model": "gpt-5.4",
            "effort": "high",
            "has_overrides": True,
        }
    )

    output = stderr_console.export_text()
    assert "Scoping complexity: moderate" in output
    assert "Executor: Codex" in output
    assert "Model: gpt-5.4" in output
    assert "Reasoning: high" in output
    assert "(overridden)" in output


def test_executing_message_respects_cli(monkeypatch):
    ui, _, _ = _ui()
    monkeypatch.setattr("ai_orchestrator.ui.random_message", lambda pool: pool[0])

    assert ui.executing_message("codex").startswith("Codex is")
    assert ui.executing_message("claude").startswith("Claude is")
    assert ui.executing_message("unknown") == "Implementation in progress..."
