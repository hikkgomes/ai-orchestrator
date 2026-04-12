from __future__ import annotations

import subprocess
from pathlib import Path

from ai_orchestrator.config import Config
from ai_orchestrator.doctor import run_doctor


def test_doctor_reports_expected_checks(monkeypatch, tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    artifact_root = repo_root / ".ai-orchestrator"
    artifact_root.mkdir()

    def fake_which(name: str):
        return f"/usr/bin/{name}"

    def fake_run(cmd, **kwargs):
        if cmd == ["git", "--version"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="git version 2.45.1\n", stderr="")
        if cmd == ["claude", "--version"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="claude 1.2.3\n", stderr="")
        if cmd == ["codex", "--version"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="codex 0.9.0\n", stderr="")
        raise AssertionError(cmd)

    monkeypatch.setattr("ai_orchestrator.doctor.which", fake_which)
    monkeypatch.setattr("ai_orchestrator.doctor.subprocess.run", fake_run)

    report = run_doctor(repo_root, artifact_root, Config())
    checks = {check.name: check for check in report.checks}

    gitignore_text = (repo_root / ".gitignore").read_text(encoding="utf-8")
    assert ".ai-orchestrator/" in gitignore_text
    assert ".ai-review/" in gitignore_text
    assert set(checks) == {
        "python",
        "git",
        "claude",
        "codex",
        "write-permissions",
        "repo-config",
    }
    assert checks["git"].status == "pass"
    assert checks["claude"].status == "pass"
    assert checks["repo-config"].status == "warn"
    assert "orch init" in (checks["repo-config"].hint or "")


def test_doctor_detects_invalid_repo_config(monkeypatch, tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "aio.toml").write_text("[orchestrator]\nmax_retries = 'bad'\n", encoding="utf-8")
    artifact_root = repo_root / ".ai-orchestrator"
    artifact_root.mkdir()

    monkeypatch.setattr("ai_orchestrator.doctor.which", lambda name: None)

    report = run_doctor(repo_root, artifact_root, Config())
    checks = {check.name: check for check in report.checks}

    assert checks["repo-config"].status == "fail"
    assert "invalid" in checks["repo-config"].summary.lower()
