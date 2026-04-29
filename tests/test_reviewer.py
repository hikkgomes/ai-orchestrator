from __future__ import annotations

import json
import os

from ai_orchestrator.reviewer import run_review_scan
from ai_orchestrator.reviewer.detect_architecture import detect_architecture
from ai_orchestrator.reviewer.detect_commands import detect_commands
from ai_orchestrator.reviewer.installer import analyze_repo, install_reviewer


def test_run_review_scan_detects_placeholder_and_respects_ignore_paths(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "build").mkdir()
    (tmp_path / "src" / "app.py").write_text('dummy_key = "changeme"\n', encoding="utf-8")
    (tmp_path / "build" / "generated.py").write_text('dummy_key = "changeme"\n', encoding="utf-8")

    findings = run_review_scan(
        tmp_path,
        changed_files=["src/app.py", "build/generated.py"],
        config={"paths": {"ignore": ["build/"], "generated": []}, "workspaces": {}},
    )

    assert findings
    assert all(finding["file"] == "src/app.py" for finding in findings)
    assert {finding["rule_id"] for finding in findings} == {"placeholder"}


def test_detect_commands_finds_python_stack_commands_and_risk_paths(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        """
[project]
name = "demo"
dependencies = ["fastapi>=0.1", "sqlalchemy>=2.0"]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    (tmp_path / "src" / "auth").mkdir(parents=True)
    (tmp_path / "migrations").mkdir()

    detected = detect_commands(tmp_path)

    assert "python" in detected["project"]["stack"]
    assert detected["commands"]["install"] == "uv sync"
    assert detected["commands"]["test"] == "uv run pytest"
    assert "migrations" in detected["paths"]["critical"]
    assert "migrations" in detected["risk"]["migration_sensitive"]


def test_detect_architecture_finds_patterns_libraries_and_naming(tmp_path):
    (tmp_path / "README.md").write_text("Sample service.\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        """
[project]
name = "demo"
dependencies = ["fastapi>=0.1", "sqlalchemy>=2.0", "pydantic>=2.0"]
description = "Demo app"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    for folder in ("services", "repositories", "entities"):
        (tmp_path / folder).mkdir()
    (tmp_path / "services" / "user_service.py").write_text(
        """
def fetch_user():
    return 1
""".strip()
        + "\n",
        encoding="utf-8",
    )

    architecture = detect_architecture(tmp_path)["architecture"]

    assert "layered" in architecture["patterns"]
    assert "fastapi" in architecture["key_libraries"]["framework"]
    assert architecture["naming"]["files"] == "snake_case"
    assert architecture["project_description"] == "Sample service."


def test_install_and_analyze_repo_write_files_and_preserve_curated_fields(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        """
[project]
name = "demo"
dependencies = ["fastapi>=0.1"]
""".strip()
        + "\n",
        encoding="utf-8",
    )

    install_result = install_reviewer(tmp_path)

    config_path = install_result["config_path"]
    rules_path = install_result["rules_path"]
    assert config_path.exists()
    assert rules_path.exists()
    static_paths = install_result["static_paths"]
    assert static_paths["skill"] == tmp_path / ".ai-review" / "SKILL.md"
    assert static_paths["report_template"] == (
        tmp_path / ".ai-review" / "templates" / "review-report.md"
    )
    for path in static_paths.values():
        assert path.exists()
        assert "local.json" not in path.read_text(encoding="utf-8")
    for key in ("scan_script", "changed_review_script", "full_review_script"):
        assert os.access(static_paths[key], os.X_OK)

    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["paths"]["critical"].append("manual/critical")
    config["risk"]["auth_sensitive"].append("manual-auth.py")
    config["notes"].append("keep me")
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

    analyzed = analyze_repo(tmp_path)
    updated = analyzed["config"]

    assert analyzed["static_paths"]["skill"].exists()
    assert "manual/critical" in updated["paths"]["critical"]
    assert "manual-auth.py" in updated["risk"]["auth_sensitive"]
    assert "keep me" in updated["notes"]
    assert updated["analyzed_at"]

