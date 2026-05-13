from __future__ import annotations

from dataclasses import asdict

from ai_orchestrator.adapters.base import BlockedOnCLI, TextInvokeResult
from ai_orchestrator.analysis import AnalysisRunner, DebateLoop, _check_consensus, _safe_invoke_text
from ai_orchestrator.artifacts import ArtifactStore
from ai_orchestrator.config import Config, load_config
from ai_orchestrator.models import AnalysisSession, RunState
from ai_orchestrator.modes import AnalysisSettings, Mode
from ai_orchestrator.state import StateManager


def test_mode_config_defaults_are_available():
    config = Config()

    assert config.modes.analysis.rounds == 3
    assert config.modes.autonomous.max_iterations == 5
    assert config.modes.review.escalation_effort == "xhigh"


def test_load_config_accepts_modes_section(tmp_path):
    config_dir = tmp_path / ".ai-orchestrator"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        "\n".join(
            [
                "[modes.analysis]",
                "rounds = 4",
                'escalation_effort = "max"',
                "[modes.autonomous]",
                "max_iterations = 7",
                "[modes.review]",
                "rounds = 2",
            ]
        ),
        encoding="utf-8",
    )

    config = load_config(tmp_path)

    assert config.modes.analysis.rounds == 4
    assert config.modes.analysis.escalation_effort == "max"
    assert config.modes.autonomous.max_iterations == 7
    assert config.modes.review.rounds == 2


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
    loaded_by_prefix = store.load_analysis_session(session.session_id[:8])
    listed = store.list_sessions("analysis")
    assert loaded.final_summary == "Use option A."
    assert loaded_by_prefix.session_id == session.session_id
    assert listed[0]["session_id"] == session.session_id
    assert listed[0]["mode"] == "analysis"


class _TextAdapter:
    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.calls = 0

    def invoke_text(self, *args, **kwargs):
        self.calls += 1
        text = self.responses.pop(0) if self.responses else "agreement: false\nMore analysis."
        return TextInvokeResult(text=text, session_id=f"session-{self.calls}")


def test_debate_loop_stops_on_consensus(tmp_path):
    claude = _TextAdapter(["agreement: true\nI agree."])
    codex = _TextAdapter(["agreement: true\nI agree too."])
    loop = DebateLoop(tmp_path, 30)

    rounds, _, _, consensus = loop.run(
        claude,
        codex,
        "Claude initial",
        "Codex initial",
        3,
        {"model": "", "effort": ""},
    )

    assert consensus is True
    assert len(rounds) == 2
    assert claude.calls == 1
    assert codex.calls == 1


def test_debate_loop_requires_both_speakers_for_consensus(tmp_path):
    claude = _TextAdapter(
        [
            "agreement: true\nI agree.",
            "agreement: true\nStill agreed.",
        ]
    )
    codex = _TextAdapter(
        [
            "agreement: false\nOne addition.",
            "agreement: true\nNow agreed.",
        ]
    )
    loop = DebateLoop(tmp_path, 30)

    rounds, _, _, consensus = loop.run(
        claude,
        codex,
        "Claude initial",
        "Codex initial",
        3,
        {"model": "", "effort": ""},
    )

    assert consensus is True
    assert [(round_.round_number, round_.actor) for round_ in rounds] == [
        (1, "codex"),
        (1, "claude"),
        (2, "claude"),
        (2, "codex"),
    ]


def test_check_consensus_requires_exact_true_value():
    assert _check_consensus("agreement: true") is True
    assert _check_consensus("agreement: false") is False
    assert _check_consensus("agreement: true, but with concerns") is False
    assert _check_consensus("agreement: it is not true") is False


def test_safe_invoke_text_catches_blocked_cli():
    result = _safe_invoke_text(lambda: (_ for _ in ()).throw(BlockedOnCLI("login required")))

    assert result.text == "Analysis failed: login required"


def test_debate_loop_alternates_round_order(tmp_path):
    claude = _TextAdapter(["agreement: false\nClaude r1.", "agreement: false\nClaude r2."])
    codex = _TextAdapter(["agreement: false\nCodex r1.", "agreement: false\nCodex r2."])
    loop = DebateLoop(tmp_path, 30)

    rounds, _, _, consensus = loop.run(
        claude,
        codex,
        "Claude initial",
        "Codex initial",
        2,
        {"model": "", "effort": ""},
    )

    assert consensus is False
    assert [(round_.round_number, round_.actor) for round_ in rounds] == [
        (1, "codex"),
        (1, "claude"),
        (2, "claude"),
        (2, "codex"),
    ]


def test_continue_session_does_not_create_orphan(monkeypatch, tmp_path):
    class FakeClaude(_TextAdapter):
        def __init__(self, *args, **kwargs):
            super().__init__(
                [
                    "Claude initial.",
                    "agreement: true\nClaude agrees.",
                    "Final synthesis.",
                ]
            )

    class FakeCodex(_TextAdapter):
        def __init__(self, *args, **kwargs):
            super().__init__(["Codex initial.", "agreement: false\nCodex adds detail."])

    monkeypatch.setattr("ai_orchestrator.analysis.ClaudeAdapter", FakeClaude)
    monkeypatch.setattr("ai_orchestrator.analysis.CodexAdapter", FakeCodex)
    artifact_root = tmp_path / ".ai-orchestrator"
    store = ArtifactStore(artifact_root)
    session = AnalysisSession(
        session_id="33333333-3333-4333-8333-333333333333",
        task="Analyze this",
        rounds=[],
        final_summary="Prior summary.",
    )
    store.save_analysis_session(session)

    runner = AnalysisRunner(Config(), tmp_path, artifact_root)
    runner.continue_session(session.session_id[:8], "Follow up", AnalysisSettings(rounds=2))

    files = sorted((artifact_root / "analyses").glob("session-*.json"))
    assert [path.name for path in files] == [f"session-{session.session_id}.json"]
