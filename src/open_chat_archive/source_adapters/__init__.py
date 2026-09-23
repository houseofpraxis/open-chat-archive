"""Built-in, explicit-format source adapters."""
from __future__ import annotations

from .claude_ai import ClaudeAIAdapter
from .claude_code import ClaudeCodeAdapter
from .open_webui import OpenWebUIAdapter
from .codex import CodexAdapter
from .grok import GrokBuildAdapter

ADAPTERS = {
    "claude-ai": ClaudeAIAdapter,
    "claude-code": ClaudeCodeAdapter,
    "open-webui": OpenWebUIAdapter,
    "codex": CodexAdapter,
    "grok-build": GrokBuildAdapter,
}


def create_adapter(format_name: str, source_scope_id: str):
    try:
        return ADAPTERS[format_name](source_scope_id)
    except KeyError as exc:
        raise ValueError("UNKNOWN_FORMAT") from exc


__all__ = ["ADAPTERS", "ClaudeAIAdapter", "ClaudeCodeAdapter", "OpenWebUIAdapter", "CodexAdapter", "GrokBuildAdapter", "create_adapter"]
