"""Canonical-safe projection helpers shared by built-in adapters."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")


def pointer_escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def json_pointer(*parts: str | int) -> str:
    if not parts:
        return ""
    return "/" + "/".join(pointer_escape(str(part)) for part in parts)


def canonical_datetime(value: Any) -> str | None:
    """Return an RFC 3339 UTC timestamp when the source value is unambiguous."""
    if isinstance(value, str):
        return value if _ISO.fullmatch(value) else None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    seconds = value // 1000 if value > 10_000_000_000 else value
    try:
        return datetime.fromtimestamp(seconds, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None


def canonical_model(name: Any, *, provider: str) -> dict[str, str] | None:
    if isinstance(name, str) and name and isinstance(provider, str) and provider:
        return {"provider": provider, "model_name": name}
    return None


def profile_path_for_pointer(root: Any, pointer: str) -> str | None:
    """Convert a source JSON Pointer into a shape-only-v1 alias path."""
    if pointer == "":
        return "/"
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        return None
    current = root
    path = ""
    for token in pointer[1:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            try:
                index = int(token)
            except ValueError:
                return None
            if index < 0 or index >= len(current):
                return None
            path += "/*"
            current = current[index]
        elif isinstance(current, dict):
            if token not in current:
                return None
            alias = "k%04d" % (sorted(current).index(token) + 1)
            path += "/" + alias
            current = current[token]
        else:
            return None
    return path or "/"
