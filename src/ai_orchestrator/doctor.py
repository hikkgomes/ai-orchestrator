"""Environment and installation diagnostics."""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from shutil import which

from .bootstrap import ensure_runtime_gitignore
from .config import Config, ConfigError, load_config


@dataclass(frozen=True)
class DoctorCheck:
    """Single doctor check result."""

    name: str
    status: str
    summary: str
    hint: str | None = None


@dataclass(frozen=True)
class DoctorReport:
    """Aggregate doctor report."""

    checks: list[DoctorCheck]

    @property
    def overall_status(self) -> str:
        if any(check.status == "fail" for check in self.checks):
            return "fail"
        if any(check.status == "warn" for check in self.checks):
            return "warn"
        return "pass"


def run_doctor(repo_root: Path, artifact_root: Path, config: Config | None = None) -> DoctorReport:
    """Run workflow-scoped environment checks."""
    ensure_runtime_gitignore(repo_root)
    effective_config = config or Config()
    checks = [
        _check_python(),
        _check_git(),
        _check_cli("claude", minimum=getattr(effective_config.cli_compat, "claude_min_version", "")),
        _check_cli("codex", minimum=getattr(effective_config.cli_compat, "codex_min_version", "")),
        _check_write_permissions(repo_root, artifact_root),
        _check_repo_config(repo_root, effective_config),
    ]
    return DoctorReport(checks=checks)


def _check_python() -> DoctorCheck:
    version = sys.version_info
    if version < (3, 11):
        return DoctorCheck(
            name="python",
            status="fail",
            summary=f"Python {version.major}.{version.minor}.{version.micro} detected; 3.11+ is required.",
            hint="Install Python 3.11 or newer and reinstall orch.",
        )
    return DoctorCheck(
        name="python",
        status="pass",
        summary=f"Python {version.major}.{version.minor}.{version.micro} is supported.",
    )


def _check_git() -> DoctorCheck:
    binary = which("git")
    if not binary:
        return DoctorCheck(
            name="git",
            status="fail",
            summary="git is not available on PATH.",
            hint="Install Git 2.20+ and retry `orch doctor`.",
        )

    completed = subprocess.run(
        ["git", "--version"],
        capture_output=True,
        text=True,
        shell=False,
        check=False,
    )
    if completed.returncode != 0:
        return DoctorCheck(
            name="git",
            status="fail",
            summary="git was found but did not respond to `--version`.",
            hint=completed.stderr.strip() or "Reinstall git or fix PATH.",
        )

    version = _extract_version(completed.stdout)
    if version and _compare_versions(version, "2.20") < 0:
        return DoctorCheck(
            name="git",
            status="fail",
            summary=f"git {version} detected; 2.20+ is required for worktree support.",
            hint="Upgrade git and retry.",
        )
    return DoctorCheck(
        name="git",
        status="pass",
        summary=completed.stdout.strip(),
    )


def _check_cli(name: str, *, minimum: str) -> DoctorCheck:
    binary = which(name)
    if not binary:
        return DoctorCheck(
            name=name,
            status="warn",
            summary=f"{name} is not available on PATH.",
            hint=f"Install {name}, authenticate it, then rerun `orch doctor`.",
        )

    completed = subprocess.run(
        [name, "--version"],
        capture_output=True,
        text=True,
        shell=False,
        check=False,
    )
    if completed.returncode != 0:
        return DoctorCheck(
            name=name,
            status="warn",
            summary=f"{name} was found but `--version` failed.",
            hint=completed.stderr.strip() or f"Open {name} manually and refresh authentication if needed.",
        )

    version = _extract_version(completed.stdout)
    status = "pass"
    hint = f"Auth not verified. If needed, run `{name} --help` and then a small interactive-free command."
    if minimum and version and _compare_versions(version, minimum) < 0:
        status = "warn"
        hint = f"Installed version is below the tested minimum ({minimum}). Upgrade {name}."

    return DoctorCheck(
        name=name,
        status=status,
        summary=completed.stdout.strip(),
        hint=hint,
    )


def _check_write_permissions(repo_root: Path, artifact_root: Path) -> DoctorCheck:
    artifact_root.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=artifact_root,
            prefix=".doctor-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write("ok\n")
            temp_path = Path(handle.name)
        temp_path.unlink(missing_ok=True)
    except OSError as exc:
        return DoctorCheck(
            name="write-permissions",
            status="fail",
            summary=f"Cannot write to {artifact_root}.",
            hint=str(exc),
        )

    return DoctorCheck(
        name="write-permissions",
        status="pass",
        summary=f"Writable: {artifact_root.relative_to(repo_root) if artifact_root.is_relative_to(repo_root) else artifact_root}",
    )


def _check_repo_config(repo_root: Path, config: Config) -> DoctorCheck:
    config_path = repo_root / "aio.toml"
    if not config_path.exists():
        return DoctorCheck(
            name="repo-config",
            status="warn",
            summary="Repo-level aio.toml is missing.",
            hint="Run `orch init` in the repository root to scaffold the config.",
        )

    try:
        load_config(repo_root=repo_root)
    except ConfigError as exc:
        return DoctorCheck(
            name="repo-config",
            status="fail",
            summary="aio.toml exists but is invalid.",
            hint=str(exc),
        )

    workflow_path = repo_root / "workflows" / "default.yaml"
    if not workflow_path.exists():
        return DoctorCheck(
            name="repo-config",
            status="warn",
            summary="aio.toml is valid, but workflows/default.yaml is missing.",
            hint="Run `orch init --force` to restore default workflow files if needed.",
        )

    if config.workspace.repos:
        missing: list[str] = []
        for repo in config.workspace.repos:
            repo_path = repo_root / repo
            if not repo_path.exists() or not (repo_path / ".git").exists():
                missing.append(repo)
        if missing:
            return DoctorCheck(
                name="repo-config",
                status="warn",
                summary="aio.toml is valid, but some configured workspace repos are missing or not git repos.",
                hint=f"Check [workspace].repos entries: {', '.join(missing)}",
            )

    return DoctorCheck(
        name="repo-config",
        status="pass",
        summary="aio.toml and workflows/default.yaml are present.",
    )


def _extract_version(text: str) -> str:
    match = re.search(r"(\d+(?:\.\d+)+)", text)
    return match.group(1) if match else ""


def _compare_versions(left: str, right: str) -> int:
    left_parts = tuple(int(part) for part in left.split("."))
    right_parts = tuple(int(part) for part in right.split("."))
    max_len = max(len(left_parts), len(right_parts))
    left_parts = left_parts + (0,) * (max_len - len(left_parts))
    right_parts = right_parts + (0,) * (max_len - len(right_parts))
    if left_parts < right_parts:
        return -1
    if left_parts > right_parts:
        return 1
    return 0


__all__ = ["DoctorCheck", "DoctorReport", "run_doctor"]
