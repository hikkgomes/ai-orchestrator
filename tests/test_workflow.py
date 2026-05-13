from __future__ import annotations

from pathlib import Path

from ai_orchestrator.workflow import _parse_scalar
from ai_orchestrator.workflow import load_workflow_definition


def test_parse_scalar_supports_negative_integers():
    assert _parse_scalar("-1") == -1


def test_parse_scalar_supports_simple_floats():
    assert _parse_scalar("0.5") == 0.5


def test_load_workflow_definition_ignores_legacy_phase_keys(tmp_path):
    workflow_dir = tmp_path / ".ai-orchestrator"
    workflow_dir.mkdir()
    (workflow_dir / "workflow.yaml").write_text(
        "\n".join(
            [
                "name: default",
                "description: test",
                "phases:",
                "  planning:",
                "    cli: claude",
                "    retries: 3",
                "    max_turns: 5",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    workflow = load_workflow_definition(tmp_path)

    assert workflow.phase("planning").retries == 3
    assert not hasattr(workflow.phase("planning"), "max_turns")
