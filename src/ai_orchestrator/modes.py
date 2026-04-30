"""Mode settings for alternate orchestrator entrypoints."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Mode(str, Enum):
    DEFAULT = "default"
    ANALYSIS = "analysis"
    QUICK_EXECUTE = "quick_execute"
    REVIEW = "review"
    AUTONOMOUS = "autonomous"


@dataclass
class AnalysisSettings:
    rounds: int = 0
    claude_model: str = ""
    codex_model: str = ""
    claude_effort: str = ""
    codex_effort: str = ""
    escalation_model: str = ""
    escalation_effort: str = ""


@dataclass
class ReviewSettings:
    rounds: int = 0
    claude_model: str = ""
    codex_model: str = ""
    claude_effort: str = ""
    codex_effort: str = ""
    escalation_model: str = ""
    escalation_effort: str = ""


@dataclass
class AutonomousSettings:
    max_iterations: int = 0
    claude_model: str = ""
    codex_model: str = ""
    claude_effort: str = ""
    codex_effort: str = ""


@dataclass
class ModeConfig:
    mode: Mode = Mode.DEFAULT
    analysis: AnalysisSettings = field(default_factory=AnalysisSettings)
    review: ReviewSettings = field(default_factory=ReviewSettings)
    autonomous: AutonomousSettings = field(default_factory=AutonomousSettings)
    skip_review: bool = False
