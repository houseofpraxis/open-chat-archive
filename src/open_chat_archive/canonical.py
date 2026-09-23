"""Canonical JSON and semantic identity primitives."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any


IMPORT_BOOKKEEPING_KEYS = frozenset({"batch_id", "created_at", "imported_at", "import_host", "run_id"})


def _check(value: Any) -> None:
    if isinstance(value, float):
        raise ValueError("floating-point values are forbidden")
    if isinstance(value, dict):
        if not all(isinstance(k, str) for k in value):
            raise ValueError("object keys must be strings")
        for child in value.values(): _check(child)
    elif isinstance(value, list):
        for child in value: _check(child)
    elif not isinstance(value, (str, int, bool, type(None))):
        raise ValueError("non-JSON value")


def canonical_bytes(value: Any) -> bytes:
    _check(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def identity_input(*, archive_id: str, record_type: str, adapter_namespace: str,
                   identity_scheme: str, identity_version: str,
                   source_scope_id: str, logical_key: str) -> dict[str, str]:
    return {"archive_id": archive_id, "record_type": record_type,
            "adapter_namespace": adapter_namespace, "identity_scheme": identity_scheme,
            "identity_version": identity_version, "source_scope_id": source_scope_id,
            "logical_key": logical_key}


def entity_identity(identity: dict[str, Any]) -> str:
    """Logical identity never includes artifact coordinates or import bookkeeping."""
    return digest(identity)


def observation_identity(body: dict[str, Any]) -> str:
    """Evidence identity includes its exact subjects, field bindings, and sources."""
    return digest({"record_type": "observation", "body": body})


def physical_batch_identity(archive_id: str, batch_id: str) -> str:
    return digest({"archive_id": archive_id, "record_type": "import_batch", "batch_id": batch_id})


def semantic_projection(record: dict[str, Any]) -> dict[str, Any]:
    """Return only semantic envelope data covered by a revision ID."""
    projected = {k: deepcopy(v) for k, v in record.items() if k in {"spec_version", "record_type", "entity_id", "sensitivity", "identity", "body", "extensions"}}
    body = projected.get("body")
    if isinstance(body, dict):
        # Generic names such as created_at can be genuine source facts. Never
        # discard them from semantic records merely because an importer might
        # use the same spelling for a clock.
        if record.get("record_type") in {"source_artifact", "artifact"}:
            body.pop("storage_path", None)
        elif record.get("record_type") == "import_batch":
            for key in IMPORT_BOOKKEEPING_KEYS: body.pop(key, None)
    return projected


def revision_identity(record: dict[str, Any]) -> str:
    return digest(semantic_projection(record))


def finalize_record(record: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(record)
    result["revision_id"] = revision_identity(result)
    return result
