from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _venv_bin(venv_dir: Path, name: str) -> Path:
    scripts_dir = "Scripts" if os.name == "nt" else "bin"
    suffix = ".exe" if os.name == "nt" else ""
    return venv_dir / scripts_dir / f"{name}{suffix}"


def test_python_module_startup_smoke():
    result = subprocess.run(
        [sys.executable, "-m", "ai_orchestrator.cli", "--help"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "install-shell" in result.stdout


def test_editable_install_smoke(tmp_path):
    if importlib.util.find_spec("hatchling") is None:
        pytest.skip("hatchling is required for the install smoke test")

    venv_dir = tmp_path / "venv"
    subprocess.run(
        [sys.executable, "-m", "venv", "--system-site-packages", str(venv_dir)],
        check=True,
        capture_output=True,
        text=True,
    )

    pip = _venv_bin(venv_dir, "pip")
    orch = _venv_bin(venv_dir, "orch")
    aio = _venv_bin(venv_dir, "aio")
    install = subprocess.run(
        [str(pip), "install", "--no-deps", "--no-build-isolation", str(PROJECT_ROOT)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert install.returncode == 0, install.stderr

    result = subprocess.run(
        [str(orch), "--help"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "doctor" in result.stdout

    alias_result = subprocess.run(
        [str(aio), "--help"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert alias_result.returncode == 0, alias_result.stderr
    assert "doctor" in alias_result.stdout
