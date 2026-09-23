"""Strict, non-lossy UTF-8 JSON parsing with safe errors."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class StrictJSONError(ValueError): pass


class SourceJSONNumber:
    """An exact finite JSON decimal token, permitted only in retained sources.

    This deliberately is not a ``str`` or a numeric Python scalar.  It cannot
    enter canonical JSON accidentally, while the raw source artifact remains
    the lossless representation of the number.
    """
    __slots__ = ("lexeme",)

    def __init__(self, lexeme: str): self.lexeme = lexeme



def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result: raise StrictJSONError("duplicate-key")
        result[key] = value
    return result


def _loads(raw: bytes, *, source_numbers: bool) -> Any:
    if raw.startswith(b"\xef\xbb\xbf"): raise StrictJSONError("bom")
    try: text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc: raise StrictJSONError("utf8") from exc
    try:
        value = json.loads(text, object_pairs_hook=_pairs,
                          parse_float=SourceJSONNumber if source_numbers else lambda _: (_ for _ in ()).throw(StrictJSONError("float")),
                          parse_constant=lambda _: (_ for _ in ()).throw(StrictJSONError("nonfinite")))
    except StrictJSONError: raise
    except (json.JSONDecodeError, RecursionError) as exc: raise StrictJSONError("syntax") from exc
    def check_strings(item: Any) -> None:
        if isinstance(item, str):
            if any(0xD800 <= ord(char) <= 0xDFFF for char in item):
                raise StrictJSONError("unicode-scalar")
        elif isinstance(item, dict):
            for key, child in item.items():
                check_strings(key); check_strings(child)
        elif isinstance(item, list):
            for child in item: check_strings(child)
    check_strings(value)
    return value


def loads_bytes(raw: bytes) -> Any:
    return _loads(raw, source_numbers=False)


def loads_source_bytes(raw: bytes) -> Any:
    """Parse source JSON while retaining finite decimal/exponent lexemes."""
    return _loads(raw, source_numbers=True)


def load_file(path: Path) -> Any:
    try: return loads_bytes(path.read_bytes())
    except OSError as exc: raise StrictJSONError("read") from exc


def load_ndjson(raw: bytes) -> list[Any]:
    if raw.startswith(b"\xef\xbb\xbf"): raise StrictJSONError("bom")
    if not raw or not raw.endswith(b"\n") or b"\r" in raw: raise StrictJSONError("newline")
    lines = raw[:-1].split(b"\n")
    if any(not line for line in lines): raise StrictJSONError("blank-line")
    return [loads_bytes(line) for line in lines]


def load_source_ndjson(raw: bytes) -> list[Any]:
    if raw.startswith(b"\xef\xbb\xbf"): raise StrictJSONError("bom")
    if not raw or not raw.endswith(b"\n") or b"\r" in raw: raise StrictJSONError("newline")
    lines = raw[:-1].split(b"\n")
    if any(not line for line in lines): raise StrictJSONError("blank-line")
    return [loads_source_bytes(line) for line in lines]
