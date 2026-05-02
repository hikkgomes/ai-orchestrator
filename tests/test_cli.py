from __future__ import annotations

import json
import sys
from pathlib import Path

from click.testing import CliRunner
from rich.console import Console

from ai_orchestrator.bootstrap import DEFAULT_WORKFLOW
from ai_orchestrator.cli import (
    _adjust_execution_settings,
    _available_models_for_cli,
    _drive_interactive_approvals,
    main,
)
from ai_orchestrator.config import Config, PhaseRoutingOverride
from ai_orchestrator.models import RunState
from ai_orchestrator.state import StateManager
from ai_orchestrator.ui import OrchestratorUI


class _Ctx:
    def __init__(self, obj):
        self.obj = obj


def test_root_help_lists_primary_commands():
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])

    assert result.exit_code == 0
    for command in (
        "init",
        "new",
        "run",
        "analysis",
        "execute",
        "review",
        "auto",
        "sessions",
        "continue",
        "status",
        "approve",
        "reject",
        "show",
        "logs",
        "doctor",
        "update",
        "sync",
        "install-shell",
        "review-install",
        "review-analyze",
    ):
        assert command in result.output


def test_command_help_smoke():
    runner = CliRunner()
    for command in (
        "init",
        "new",
        "run",
        "analysis",
        "execute",
        "review",
        "auto",
        "sessions",
        "continue",
        "approve",
        "reject",
        "status",
        "show",
        "logs",
        "doctor",
        "update",
        "sync",
        "install-shell",
        "review-install",
        "review-analyze",
    ):
        result = runner.invoke(main, [command, "--help"])
        assert result.exit_code == 0, command


def test_version_smoke():
    runner = CliRunner()
    result = runner.invoke(main, ["--version"])
    assert result.exit_code == 0
    assert "orch" in result.output


def test_continue_dispatches_to_run_resume(monkeypatch):
    captured: dict[str, object] = {}

    class FakeEngine:
        def resume(self, run_id):
            captured["run_id"] = run_id
            return RunState(run_id=run_id, task="x", status="DONE")

    monkeypatch.setattr("ai_orchestrator.cli._resolve_session", lambda ctx, prefix: ("run-123", "run"))
    monkeypatch.setattr("ai_orchestrator.cli._build_engine", lambda ctx, **kwargs: FakeEngine())
    monkeypatch.setattr("ai_orchestrator.cli._render_run_snapshot", lambda ctx, run_id, state=None: captured.setdefault("rendered", run_id))

    runner = CliRunner()
    result = runner.invoke(main, ["continue", "run-123", "followup"])

    assert result.exit_code == 0
    assert captured["run_id"] == "run-123"
    assert captured["rendered"] == "run-123"
    assert "ignored for pipeline runs" in result.output


def test_continue_dispatches_to_analysis_runner(monkeypatch):
    captured: dict[str, object] = {}

    class FakeRunner:
        def __init__(self, *args, **kwargs):
            pass

        def continue_session(self, session_id, follow_up, settings):
            captured["session_id"] = session_id
            captured["follow_up"] = follow_up
            captured["rounds"] = settings.rounds

    monkeypatch.setattr("ai_orchestrator.cli._resolve_session", lambda ctx, prefix: ("session-123", "analysis"))
    monkeypatch.setattr("ai_orchestrator.cli.AnalysisRunner", FakeRunner)

    runner = CliRunner()
    result = runner.invoke(main, ["continue", "session-123", "followup"])

    assert result.exit_code == 0
    assert captured["session_id"] == "session-123"
    assert captured["follow_up"] == "followup"
    assert captured["rounds"] >= 1


def test_init_scaffolds_repo_files():
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(main, ["init"])

        assert result.exit_code == 0
        assert Path("aio.toml").exists()
        assert Path("workflows/default.yaml").exists()
        assert Path(".ai-review/config.json").exists()
        assert Path(".ai-review/rules.yaml").exists()
        workflow_text = Path("workflows/default.yaml").read_text(encoding="utf-8")
        assert "authoritative workflow definition" in workflow_text
        assert "scoping:\n    cli: claude\n    retries: 2" in workflow_text
        assert "reviewing:\n    cli: claude\n    retries: 3" in workflow_text
        assert "max_turns:" not in workflow_text
        gitignore_text = Path(".gitignore").read_text(encoding="utf-8")
        assert ".ai-orchestrator/" in gitignore_text
        assert ".ai-review/" in gitignore_text
        assert ".ai-orchestrator/results/" not in gitignore_text
        assert ".ai-orchestrator/feasibility/" not in gitignore_text


def test_init_can_skip_review_setup():
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(main, ["init", "--skip-review-setup"])

        assert result.exit_code == 0
        assert Path("aio.toml").exists()
        assert not Path(".ai-review/config.json").exists()
        assert not Path(".ai-review/rules.yaml").exists()


def test_install_shell_writes_bash_integration(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    runner = CliRunner()
    result = runner.invoke(main, ["install-shell", "--shell", "bash"])

    assert result.exit_code == 0
    integration = home / ".config" / "ai-orchestrator" / "shell" / "orch.bash"
    assert integration.exists()
    assert 'alias aio=orch' in integration.read_text(encoding="utf-8")
    assert '.config/ai-orchestrator/shell/orch.bash' in (home / ".bashrc").read_text(encoding="utf-8")


def test_run_help_lists_skip_scoping_option():
    runner = CliRunner()
    result = runner.invoke(main, ["run", "--help"])

    assert result.exit_code == 0
    assert "--skip-scoping" in result.output


def test_runtime_command_adds_runtime_gitignore_without_init():
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(main, ["status"])

        assert result.exit_code == 0
        gitignore_text = Path(".gitignore").read_text(encoding="utf-8")
        assert ".ai-orchestrator/" in gitignore_text
        assert ".ai-review/" in gitignore_text
        assert Path(".ai-orchestrator/metadata.sqlite3").exists()


def test_new_defaults_to_interactive(monkeypatch):
    captured: dict[str, object] = {}

    def fake_start_run(ctx, task, interactive, *, skip_scoping, **kwargs):
        captured["task"] = task
        captured["interactive"] = interactive
        captured["skip_scoping"] = skip_scoping

    monkeypatch.setattr("ai_orchestrator.cli._start_run", fake_start_run)

    runner = CliRunner()
    result = runner.invoke(main, ["new", "Ship it"])

    assert result.exit_code == 0
    assert captured == {
        "task": "Ship it",
        "interactive": True,
        "skip_scoping": False,
    }


def test_run_detach_disables_interactive_default(monkeypatch):
    captured: dict[str, object] = {}

    def fake_start_run(ctx, task, interactive, *, skip_scoping, **kwargs):
        captured["task"] = task
        captured["interactive"] = interactive
        captured["skip_scoping"] = skip_scoping

    monkeypatch.setattr("ai_orchestrator.cli._start_run", fake_start_run)

    runner = CliRunner()
    result = runner.invoke(main, ["run", "Ship it", "--detach"])

    assert result.exit_code == 0
    assert captured["interactive"] is False


def test_debate_tiebreaker_gate_is_removed(monkeypatch):
    monkeypatch.setattr("ai_orchestrator.cli._resolve_run_id_arg", lambda ctx, run_id: run_id)
    monkeypatch.setattr("ai_orchestrator.cli._build_engine", lambda ctx: object())

    runner = CliRunner()
    result = runner.invoke(main, ["approve", "run-1", "debate_tiebreaker"])

    assert result.exit_code != 0
    assert "debate_tiebreaker" in result.output
    assert "is not one of 'scope', 'plan'" in result.output


def test_drive_interactive_approvals_returns_unknown_pause_gate(tmp_path, monkeypatch):
    artifact_root = tmp_path / ".ai-orchestrator"
    state = RunState(
        run_id="99999999-9999-4999-8999-999999999999",
        task="Autonomous task",
        status="PAUSED",
        current_phase="REVIEWING",
    )
    StateManager(artifact_root).save(state)

    class FakeEngine:
        def resume(self, run_id):
            raise AssertionError("unknown pause gates should not auto-resume")

    monkeypatch.setattr("ai_orchestrator.cli._build_engine", lambda ctx: FakeEngine())
    ctx = _Ctx(
        {
            "artifact_root": artifact_root,
            "repo_root": tmp_path,
            "ui": OrchestratorUI(console=Console(file=sys.stdout), stderr_console=Console(file=sys.stderr)),
            "config": Config(),
            "config_error": None,
        }
    )

    resumed = _drive_interactive_approvals(ctx, state.run_id)

    assert resumed.status == "PAUSED"
    assert resumed.current_phase == "REVIEWING"


def test_show_latest_plan_renders_full_plan(tmp_path, monkeypatch):
    repo_root = tmp_path
    artifact_root = repo_root / ".ai-orchestrator"
    (artifact_root / "state").mkdir(parents=True)
    (artifact_root / "plans").mkdir(parents=True)
    mgr = StateManager(artifact_root)
    plan_path = artifact_root / "plans" / "plan-12345678.json"
    plan_path.write_text(
        json.dumps(
            {
                "plan_id": "plan-1",
                "task": "Inspect the full plan",
                "approach": "The user needs the whole plan, not a truncated preview.",
                "implementation_steps": ["Read every file listed in the plan"],
                "key_files": ["src/ai_orchestrator/cli.py", "src/ai_orchestrator/ui.py"],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    state = RunState(run_id="12345678-0000-0000-0000-000000000000", task="Inspect plan")
    state.plan_id = "plans/plan-12345678.json"
    mgr.save(state)

    monkeypatch.chdir(repo_root)
    runner = CliRunner()
    result = runner.invoke(main, ["show", "latest", "plan"])

    assert result.exit_code == 0
    assert "The user needs the whole plan, not a truncated preview." in result.output
    assert "src/ai_orchestrator/ui.py" in result.output
    assert "orch approve 12345678 plan" in result.output


def test_show_latest_plan_renders_markdown_plan(tmp_path, monkeypatch):
    repo_root = tmp_path
    artifact_root = repo_root / ".ai-orchestrator"
    (artifact_root / "state").mkdir(parents=True)
    (artifact_root / "plans").mkdir(parents=True)
    mgr = StateManager(artifact_root)
    plan_path = artifact_root / "plans" / "plan-12345678.md"
    plan_path.write_text(
        "\n".join(
            [
                "---",
                "plan_id: plan-1",
                "task: Inspect markdown plan",
                "---",
                "",
                "## Approach",
                "Use markdown planning output.",
                "",
                "## Steps",
                "1. Read files",
                "",
                "## Key Files",
                "- src/ai_orchestrator/cli.py",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    state = RunState(run_id="12345678-0000-0000-0000-000000000000", task="Inspect plan")
    state.plan_id = "plans/plan-12345678.md"
    mgr.save(state)

    monkeypatch.chdir(repo_root)
    runner = CliRunner()
    result = runner.invoke(main, ["show", "latest", "plan"])

    assert result.exit_code == 0
    assert "Use markdown planning output." in result.output
    assert "src/ai_orchestrator/cli.py" in result.output
    assert "orch approve 12345678 plan" in result.output


def test_review_install_and_analyze_commands_manage_config_files():
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path("pyproject.toml").write_text(
            """
[project]
name = "demo"
dependencies = ["fastapi>=0.1"]
""".strip()
            + "\n",
            encoding="utf-8",
        )

        install_result = runner.invoke(main, ["review-install"])
        assert install_result.exit_code == 0
        assert Path(".ai-review/config.json").exists()
        assert Path(".ai-review/rules.yaml").exists()
        assert ".ai-review/" in Path(".gitignore").read_text(encoding="utf-8")

        second_install = runner.invoke(main, ["review-install"])
        assert second_install.exit_code != 0
        assert "Reviewer config already exists." in second_install.output

        forced_install = runner.invoke(main, ["review-install", "--force"])
        assert forced_install.exit_code == 0

        config = json.loads(Path(".ai-review/config.json").read_text(encoding="utf-8"))
        config["notes"].append("manual note")
        config["paths"]["critical"].append("manual/critical")
        Path(".ai-review/config.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

        analyze_result = runner.invoke(main, ["review-analyze"])
        assert analyze_result.exit_code == 0
        updated = json.loads(Path(".ai-review/config.json").read_text(encoding="utf-8"))
        assert "manual note" in updated["notes"]
        assert "manual/critical" in updated["paths"]["critical"]


def test_sync_refreshes_reviewer_config_without_overwriting_manual_fields():
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path("pyproject.toml").write_text(
            """
[project]
name = "demo"
dependencies = ["fastapi>=0.1"]
""".strip()
            + "\n",
            encoding="utf-8",
        )

        install_result = runner.invoke(main, ["review-install"])
        assert install_result.exit_code == 0

        config = json.loads(Path(".ai-review/config.json").read_text(encoding="utf-8"))
        config["notes"].append("manual note")
        Path(".ai-review/config.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        Path(".ai-review/rules.yaml").write_text("outdated: true\n", encoding="utf-8")

        sync_result = runner.invoke(main, ["sync"])
        assert sync_result.exit_code == 0

        updated = json.loads(Path(".ai-review/config.json").read_text(encoding="utf-8"))
        assert "manual note" in updated["notes"]
        assert "review_categories:" in Path(".ai-review/rules.yaml").read_text(encoding="utf-8")


def test_sync_updates_stale_workflow_file():
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path("pyproject.toml").write_text(
            """
[project]
name = "demo"
dependencies = ["fastapi>=0.1"]
""".strip()
            + "\n",
            encoding="utf-8",
        )
        Path("workflows").mkdir()
        stale_workflow = DEFAULT_WORKFLOW.replace("    retries: 3\n", "    retries: 4\n", 1)
        Path("workflows/default.yaml").write_text(stale_workflow, encoding="utf-8")

        result = runner.invoke(main, ["sync"])

        assert result.exit_code == 0
        assert Path("workflows/default.yaml").read_text(encoding="utf-8") == DEFAULT_WORKFLOW
        assert "workflows/default.yaml" in result.output
        assert "updated" in result.output


def test_sync_skips_up_to_date_workflow_file():
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path("pyproject.toml").write_text(
            """
[project]
name = "demo"
dependencies = ["fastapi>=0.1"]
""".strip()
            + "\n",
            encoding="utf-8",
        )
        Path("workflows").mkdir()
        Path("workflows/default.yaml").write_text(DEFAULT_WORKFLOW, encoding="utf-8")

        result = runner.invoke(main, ["sync"])

        assert result.exit_code == 0
        assert Path("workflows/default.yaml").read_text(encoding="utf-8") == DEFAULT_WORKFLOW
        assert "workflows/default.yaml" not in result.output


def test_self_update_local_pipx_pulls_and_reinstalls(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, *, check, shell):
        calls.append(cmd)
        return None

    monkeypatch.setattr("ai_orchestrator.cli.read_install_meta", lambda: {
        "install_mode": "local-pipx",
        "source_repo_path": "/tmp/ai-orchestrator",
    })
    monkeypatch.setattr("ai_orchestrator.cli.subprocess.run", fake_run)

    runner = CliRunner()
    result = runner.invoke(main, ["update"])

    assert result.exit_code == 0
    assert calls == [
        ["git", "-C", "/tmp/ai-orchestrator", "pull", "--ff-only"],
        ["pipx", "install", "--force", "/tmp/ai-orchestrator"],
    ]


def test_self_update_pypi_falls_back_to_pip(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, *, check, shell):
        calls.append(cmd)
        if cmd[:3] == ["pipx", "upgrade", "ai-orchestrator"]:
            raise FileNotFoundError("pipx")
        return None

    monkeypatch.setattr("ai_orchestrator.cli.read_install_meta", lambda: {
        "install_mode": "pypi",
        "source_repo_path": "",
    })
    monkeypatch.setattr("ai_orchestrator.cli.subprocess.run", fake_run)

    runner = CliRunner()
    result = runner.invoke(main, ["update"])

    assert result.exit_code == 0
    assert calls[0] == ["pipx", "upgrade", "ai-orchestrator"]
    assert calls[1] == [sys.executable, "-m", "pip", "install", "--upgrade", "ai-orchestrator"]


def test_self_update_pip_user_falls_back_to_pip(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, *, check, shell):
        calls.append(cmd)
        if cmd[:3] == ["pipx", "upgrade", "ai-orchestrator"]:
            raise FileNotFoundError("pipx")
        return None

    monkeypatch.setattr(
        "ai_orchestrator.cli.read_install_meta",
        lambda: {"install_mode": "pip-user", "source_repo_path": "/tmp/ai-orchestrator"},
    )
    monkeypatch.setattr("ai_orchestrator.cli.subprocess.run", fake_run)

    runner = CliRunner()
    result = runner.invoke(main, ["update"])

    assert result.exit_code == 0
    assert calls[0] == ["pipx", "upgrade", "ai-orchestrator"]
    assert calls[1] == [sys.executable, "-m", "pip", "install", "--upgrade", "ai-orchestrator"]


def test_available_models_for_cli_collects_default_and_phase_overrides():
    cfg = Config()
    cfg.routing.worker = "codex"
    cfg.models.codex.default = "gpt-5.4"
    cfg.models.claude.default = "claude-sonnet-4-6"
    cfg.routing.phases["executing"] = PhaseRoutingOverride(
        model="gpt-5.4-mini",
        model_simple="gpt-5.4-mini",
        model_moderate="gpt-5.3-codex",
        model_complex="gpt-5.4",
    )
    cfg.routing.phases["planning"] = PhaseRoutingOverride(
        cli="claude",
        model="claude-opus-4-6",
    )
    cfg.routing.phases["reviewing"] = PhaseRoutingOverride(
        cli="claude",
        model_moderate="claude-sonnet-4-6",
    )

    class StubEngine:
        _config = cfg

    codex_models = _available_models_for_cli(StubEngine(), "codex")
    assert codex_models[0] == "(default)"
    assert "gpt-5.4" in codex_models
    assert "gpt-5.4-mini" in codex_models
    assert "gpt-5.3-codex" in codex_models
    assert "claude-opus-4-6" not in codex_models

    claude_models = _available_models_for_cli(StubEngine(), "claude")
    assert claude_models[0] == "(default)"
    assert "claude-sonnet-4-6" in claude_models
    assert "claude-opus-4-6" in claude_models
    assert "gpt-5.3-codex" not in claude_models


def test_adjust_execution_settings_model_override(monkeypatch):
    cfg = Config()
    cfg.routing.worker = "codex"
    cfg.models.codex.default = "gpt-5.4"

    class StubEngine:
        _config = cfg

        @staticmethod
        def _phase_cli(workflow_phase: str, *, config_name: str) -> str:
            assert workflow_phase == "executing"
            assert config_name == "worker"
            return "codex"

    class StubStateMgr:
        called = 0

        def save(self, state):
            self.called += 1

    call_seq = iter(["Model", "gpt-5.4"])
    monkeypatch.setattr("ai_orchestrator.cli._select_choice", lambda *a, **kw: next(call_seq))

    ui = OrchestratorUI(
        console=Console(record=True, force_terminal=False),
        stderr_console=Console(record=True, force_terminal=False),
    )
    state = RunState(run_id="run-1", task="task")
    state_mgr = StubStateMgr()
    _adjust_execution_settings(ui, StubEngine(), state_mgr, state, "run-1")

    assert state.execution_overrides["model"] == "gpt-5.4"
    assert state_mgr.called == 1


def test_adjust_execution_settings_executor_swap(monkeypatch):
    cfg = Config()
    cfg.routing.worker = "codex"
    cfg.models.claude.default = "claude-sonnet-4-6"

    class StubEngine:
        _config = cfg

        @staticmethod
        def _phase_cli(workflow_phase: str, *, config_name: str) -> str:
            return "codex"

    class StubStateMgr:
        called = 0

        def save(self, state):
            self.called += 1

    call_seq = iter(["Executor (Claude/Codex)", "claude-sonnet-4-6"])
    monkeypatch.setattr("ai_orchestrator.cli._select_choice", lambda *a, **kw: next(call_seq))
    monkeypatch.setattr("ai_orchestrator.cli._confirm_choice", lambda *a, **kw: True)

    ui = OrchestratorUI(
        console=Console(record=True, force_terminal=False),
        stderr_console=Console(record=True, force_terminal=False),
    )
    state = RunState(run_id="run-2", task="task")
    state_mgr = StubStateMgr()
    _adjust_execution_settings(ui, StubEngine(), state_mgr, state, "run-2")

    assert state.execution_overrides["cli"] == "claude"
    assert state.execution_overrides["model"] == "claude-sonnet-4-6"
    assert state_mgr.called == 1


def test_adjust_execution_settings_cancel(monkeypatch):
    cfg = Config()

    class StubEngine:
        _config = cfg

        @staticmethod
        def _phase_cli(workflow_phase: str, *, config_name: str) -> str:
            return "codex"

    class StubStateMgr:
        called = 0

        def save(self, state):
            self.called += 1

    monkeypatch.setattr("ai_orchestrator.cli._select_choice", lambda *a, **kw: "Cancel")

    ui = OrchestratorUI(
        console=Console(record=True, force_terminal=False),
        stderr_console=Console(record=True, force_terminal=False),
    )
    state = RunState(run_id="run-3", task="task")
    state_mgr = StubStateMgr()
    _adjust_execution_settings(ui, StubEngine(), state_mgr, state, "run-3")

    assert state.execution_overrides == {}
    assert state_mgr.called == 0


def test_adjust_execution_settings_both(monkeypatch):
    cfg = Config()
    cfg.routing.worker = "codex"

    class StubEngine:
        _config = cfg

        @staticmethod
        def _phase_cli(workflow_phase: str, *, config_name: str) -> str:
            return "codex"

    class StubStateMgr:
        called = 0

        def save(self, state):
            self.called += 1

    call_seq = iter(["Both", "gpt-5.3-codex", "xhigh"])
    monkeypatch.setattr("ai_orchestrator.cli._select_choice", lambda *a, **kw: next(call_seq))

    ui = OrchestratorUI(
        console=Console(record=True, force_terminal=False),
        stderr_console=Console(record=True, force_terminal=False),
    )
    state = RunState(run_id="run-4", task="task")
    state_mgr = StubStateMgr()
    _adjust_execution_settings(ui, StubEngine(), state_mgr, state, "run-4")

    assert state.execution_overrides["model"] == "gpt-5.3-codex"
    assert state.execution_overrides["effort"] == "xhigh"
    assert state_mgr.called == 1
