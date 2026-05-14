"""Generalized N-participant review debate helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ReviewDebate:
    """Coordinates multi-AI review agreement rounds.

    This module currently provides a reusable shell used by the engine's
    review phase while preserving existing execution contracts.
    """

    engine: Any

    def run(self, state: Any) -> tuple[str, list[dict[str, Any]]]:
        # Backward-compatible placeholder used for phased migration.
        issues = state.debate_state.consolidated_issues if state.debate_state else []
        verdict = (state.debate_state.final_verdict if state.debate_state else None) or "pass"
        return verdict, issues
