"""Adapter registry for resolving CLI adapters by name."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import BaseAdapter
from .claude import ClaudeAdapter
from .codex import CodexAdapter
from .gemini import GeminiAdapter


class AdapterRegistry:
    """Factory-backed adapter registry."""

    _FACTORIES: dict[str, type[BaseAdapter]] = {
        "claude": ClaudeAdapter,
        "codex": CodexAdapter,
        "gemini": GeminiAdapter,
    }

    def __init__(self, config: Any, artifact_root: Path) -> None:
        self._config = config
        self._artifact_root = artifact_root
        self._instances: dict[str, BaseAdapter] = {}

    def get(self, cli_name: str) -> BaseAdapter:
        if cli_name in self._instances:
            return self._instances[cli_name]
        factory = self._FACTORIES.get(cli_name)
        if factory is None:
            raise KeyError(cli_name)
        adapter = factory(self._config, self._artifact_root)
        self._instances[cli_name] = adapter
        return adapter

    @classmethod
    def available_names(cls) -> list[str]:
        return sorted(cls._FACTORIES)

    @classmethod
    def supports_session_resume(cls, cli_name: str) -> bool:
        factory = cls._FACTORIES.get(cli_name)
        if factory is None:
            return False
        return factory.supports_session_resume()
