"""Gemini CLI adapter.

Implements the same contract as Claude/Codex adapters with graceful flag degradation.
"""

from __future__ import annotations

from .claude import ClaudeAdapter


class GeminiAdapter(ClaudeAdapter):
    """Adapter for the standalone ``gemini`` CLI.

    The implementation intentionally mirrors ``ClaudeAdapter`` because both CLIs
    are prompt-oriented JSON emitters. Unknown/unsupported effort flags are
    retried without the flag via the inherited logic.
    """

    CLI_NAME = "gemini"
    _EFFORT_FLAG = "--thinking"
    _MODEL_FLAG = "--model"

    @classmethod
    def supports_session_resume(cls) -> bool:
        # Some Gemini CLI versions support session continuation; if unsupported,
        # invocation still degrades gracefully at runtime.
        return True

    @classmethod
    def list_available_models(cls) -> list[str]:
        return [
            "gemini-2.5-pro",
            "gemini-2.5-flash",
            "gemini-2.0-flash",
        ]
