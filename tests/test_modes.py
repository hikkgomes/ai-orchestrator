from __future__ import annotations

from dataclasses import asdict

from ai_orchestrator.artifacts import ArtifactStore
from ai_orchestrator.config import Config, load_config
from ai_orchestrator.models import AnalysisSession, RunState
from ai_orchestrator.modes import AnalysisSettings, Mode
from ai_orchestrator.state import StateManager


def test_mode_config_defaults_are_available():
    config = Config()

    assert config.modes.analysis_rounds == 3
    assert config.modes.autonomous_max_iterations == 5
    assert config.modes.review_escalation_effort == "xhigh"


def test_load_config_accepts_modes_section(tmp_path):
    (tmp_path / "aio.toml").write_text(
        "\n".join(
            [
                "[modes]",
                "analysis_rounds = 4",
                'analysis_escalation_effort = "max"',
                "autonomous_max_iterations = 7",
                "review_rounds = 2",
            ]
        ),
        encoding="utf-8",
    )

    config = load_config(tmp_path)

    assert config.modes.analysis_rounds == 4
    assert config.modes.analysis_escalation_effort == "max"
    assert config.modes.autonomous_max_iterations == 7
    assert config.modes.review_rounds == 2


def test_run_state_persists_mode(tmp_path):
    artifact_root = tmp_path / ".ai-orchestrator"
    manager = StateManager(artifact_root)
    state = RunState(
        run_id="11111111-1111-4111-8111-111111111111",
        task="Review this",
        mode=Mode.REVIEW.value,
    )

    manager.save(state)

    assert manager.load(state.run_id).mode == "review"


def test_analysis_session_round_trips_and_lists(tmp_path):
    store = ArtifactStore(tmp_path / ".ai-orchestrator")
    session = AnalysisSession(
        session_id="22222222-2222-4222-8222-222222222222",
        task="Analyze this",
        rounds=[{"round_number": 1, "actor": "claude", "text": "A"}],
        final_summary="Use option A.",
        settings=asdict(AnalysisSettings(rounds=1)),
    )

    store.save_analysis_session(session)

    loaded = store.load_analysis_session(session.session_id)
    listed = store.list_sessions("analysis")
    assert loaded.final_summary == "Use option A."
    assert listed[0]["session_id"] == session.session_id
    assert listed[0]["mode"] == "analysis"
