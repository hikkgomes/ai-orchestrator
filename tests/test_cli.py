from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from ai_orchestrator.cli import main


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
        "logs",
        "doctor",
        "install-shell",
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
        "logs",
        "doctor",
        "install-shell",
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
        workflow_text = Path("workflows/default.yaml").read_text(encoding="utf-8")
        assert "authoritative workflow definition" in workflow_text
        assert ".ai-orchestrator/results/" in Path(".gitignore").read_text(encoding="utf-8")
        assert ".ai-orchestrator/feasibility/" in Path(".gitignore").read_text(encoding="utf-8")


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
