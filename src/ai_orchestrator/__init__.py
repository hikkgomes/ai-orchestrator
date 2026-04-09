"""ai-orchestrator: local orchestrator for Claude Code and Codex subprocesses."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("ai-orchestrator")
except PackageNotFoundError:
    __version__ = "0.0.0.dev0"

__all__ = ["__version__"]
