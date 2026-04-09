from __future__ import annotations

from ai_orchestrator.workflow import _parse_scalar


def test_parse_scalar_supports_negative_integers():
    assert _parse_scalar("-1") == -1


def test_parse_scalar_supports_simple_floats():
    assert _parse_scalar("0.5") == 0.5
