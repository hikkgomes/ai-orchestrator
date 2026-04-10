from __future__ import annotations

import json
import sys
from pathlib import Path

from click.testing import CliRunner

from ai_orchestrator.bootstrap import DEFAULT_WORKFLOW
from ai_orchestrator.cli import main
from ai_orchestrator.models import RunState
from ai_orchestrator.state import StateManager


def test_root_help_lists_primary_commands():
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])

    assert result.exit_code == 0
    for command in (
        "init",
        "new",
        "run",
        "status",
        "approve",
        "reject",
        "resume",
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
        "resume",
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
        assert "scoping:\n    cli: claude\n    retries: 2\n    max_turns: 3" in workflow_text
        assert "reviewing:\n    cli: claude\n    retries: 3\n    max_turns: 5" in workflow_text
        assert ".ai-orchestrator/results/" in Path(".gitignore").read_text(encoding="utf-8")
        assert ".ai-orchestrator/feasibility/" in Path(".gitignore").read_text(encoding="utf-8")


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


def test_new_defaults_to_interactive(monkeypatch):
    captured: dict[str, object] = {}

    def fake_start_run(ctx, task, interactive, *, skip_scoping):
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

    def fake_start_run(ctx, task, interactive, *, skip_scoping):
        captured["task"] = task
        captured["interactive"] = interactive
        captured["skip_scoping"] = skip_scoping

    monkeypatch.setattr("ai_orchestrator.cli._start_run", fake_start_run)

    runner = CliRunner()
    result = runner.invoke(main, ["run", "Ship it", "--detach"])

    assert result.exit_code == 0
    assert captured["interactive"] is False


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
                "steps": [
                    {
                        "step_number": 1,
                        "description": "Read every file listed in the plan",
                        "files_to_read": ["src/ai_orchestrator/cli.py", "src/ai_orchestrator/ui.py"],
                        "files_to_modify": ["src/ai_orchestrator/ui.py"],
                        "depends_on": [],
                        "estimated_complexity": "medium",
                    }
                ],
                "reasoning": "The user needs the whole plan, not a truncated preview.",
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
        stale_workflow = DEFAULT_WORKFLOW.replace("    max_turns: 5\n", "", 1)
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
