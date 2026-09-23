"""Shared, deliberately small writer for built-in source adapters.

This module is local-only.  It never interprets a source while copying its
bytes, and all failures use fixed diagnostic codes.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any, Iterable, NoReturn

from .adapter import BatchResult, Diagnostic
from .canonical import digest, entity_identity, finalize_record, identity_input
from .projection import profile_path_for_pointer
from .shape_profile import profile_value
from .shape_profile import ProfileError


class AdapterInputError(ValueError):
    """A fixed-code adapter failure; never include source-derived detail."""


_OPAQUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def reject(code: str) -> NoReturn:
    raise AdapterInputError(code)


def read_regular(path: Path, *, max_bytes: int = 64 * 1024 * 1024) -> bytes:
    """Read one local regular, non-symlink file with a bounded size."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
                reject("INPUT_KIND_OR_SIZE")
            data = handle.read(max_bytes + 1)
        if len(data) > max_bytes:
            reject("INPUT_KIND_OR_SIZE")
        return data
    except AdapterInputError:
        raise
    except OSError:
        reject("INPUT_READ")
    raise AssertionError("unreachable")


class BatchWriter:
    """Write source/control records and evidence around supplied semantic records."""

    def __init__(self, *, namespace: str, version: str, source_scope_id: str,
                 archive_id: str, batch_id: str, archive_root: Path,
                 raw: bytes, media_type: str, fingerprint: str, parsed: Any,
                 profile_max_items: int = 10_000):
        if not all(_OPAQUE.fullmatch(item) for item in (source_scope_id, archive_id, batch_id)):
            reject("OPAQUE_IDENTIFIER")
        self.namespace, self.version, self.scope = namespace, version, source_scope_id
        self.archive_id, self.batch_id, self.root = archive_id, batch_id, archive_root
        if archive_root.name != archive_id or (archive_root.exists() and (archive_root.is_symlink() or not archive_root.is_dir())):
            reject("ARCHIVE_ROOT")
        self.raw, self.media_type, self.fingerprint, self.parsed = raw, media_type, fingerprint, parsed
        self.profile_max_items = profile_max_items
        self.locations: dict[tuple[str, str], dict[str, Any]] = {}
        self.mapped: list[tuple[str, list[str]]] = []

    def identity(self, record_type: str, logical_key: str, *, scheme: str = "native-id",
                 fallback: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = identity_input(
            archive_id=self.archive_id, record_type=record_type,
            adapter_namespace=self.namespace, identity_scheme=scheme,
            identity_version="1", source_scope_id=self.scope, logical_key=logical_key)
        if fallback:
            result["fallback"] = {"kind": "artifact-local", "stability": "same-artifact-only"}
        return result

    def record(self, record_type: str, logical_key: str, body: dict[str, Any], *,
               extensions: dict[str, Any] | None = None, scheme: str = "native-id",
               fallback: bool = False, location: dict[str, Any] | None = None,
               mapped_destinations: list[str] | None = None) -> dict[str, Any]:
        identity = self.identity(record_type, logical_key, scheme=scheme, fallback=fallback)
        record: dict[str, Any] = {"spec_version": "0.1", "record_type": record_type,
            "entity_id": entity_identity(identity), "revision_id": "sha256:" + "0" * 64,
            "sensitivity": "restricted-unclassified", "identity": identity, "body": body}
        if extensions:
            record["extensions"] = extensions
        result = finalize_record(record)
        if location:
            self.locations[(result["entity_id"], result["revision_id"])] = location
            pointer = location.get("json_pointer")
            if mapped_destinations and isinstance(pointer, str):
                self.mapped.append((pointer, mapped_destinations))
        return result

    def _store_source(self) -> tuple[dict[str, Any], dict[str, Any]]:
        source_digest = "sha256:" + hashlib.sha256(self.raw).hexdigest()
        name = "object-" + source_digest[7:19]
        sources = self.root / "sources"
        sources.mkdir(parents=True, exist_ok=True)
        if sources.is_symlink() or not sources.is_dir(): reject("ARCHIVE_PATH")
        target = sources / name
        if target.exists():
            if target.is_symlink() or not target.is_file() or target.read_bytes() != self.raw:
                reject("SOURCE_OBJECT_COLLISION")
        else:
            target.write_bytes(self.raw)
        item = {"path": "sources/" + name, "byte_digest": source_digest,
                "media_type": self.media_type, "size": len(self.raw)}
        source = self.record("source_artifact", source_digest, {
            "archive_id": self.archive_id, "byte_digest": source_digest,
            "media_type": self.media_type, "size": len(self.raw), "storage_path": item["path"],
            "acquisition_method": "local-file"})
        return source, item

    def write(self, semantic: Iterable[dict[str, Any]]) -> BatchResult:
        batch_dir = self.root / "batches" / self.batch_id
        if batch_dir.exists() or batch_dir.is_symlink():
            reject("BATCH_EXISTS")
        try:
            profile = profile_value(self.parsed, max_items=self.profile_max_items)
        except ProfileError:
            reject("PROFILE_LIMIT")
            raise AssertionError("unreachable")
        source, source_item = self._store_source()
        records = [source]
        semantic_records = list(semantic)
        records.extend(semantic_records)
        whole = {"start": 0, "end": len(self.raw)}
        for target in semantic_records:
            location = dict(self.locations.get((target["entity_id"], target["revision_id"]), {}))
            if "byte_range" not in location:
                location["byte_range"] = whole
            if self.media_type == "application/json" and "json_pointer" not in location:
                location["json_pointer"] = ""
            if self.media_type == "application/x-ndjson" and "jsonl_line" not in location:
                location["jsonl_line"] = 1
            body = {"subject": {"entity_id": target["entity_id"], "revision_id": target["revision_id"]},
                "bindings": [{"field_path": "/body", "claim_basis": "parsed",
                "sources": [{"artifact_id": source["entity_id"], "location": location}]}]}
            if "extensions" in target:
                body["bindings"].append({"field_path": "/extensions", "claim_basis": "parsed",
                    "sources": [{"artifact_id": source["entity_id"], "location": location}]})
            records.append(self.record("observation", digest(body), body))
        paths = [item["path"] for item in profile["paths"] if item["path"] != "/"]
        mapped_by_path: dict[str, set[str]] = {}
        for pointer, destinations in self.mapped:
            alias = profile_path_for_pointer(self.parsed, pointer)
            if alias and alias != "/":
                mapped_by_path.setdefault(alias, set()).update(destinations)
        ledger = []
        for path in paths:
            if path in mapped_by_path:
                ledger.append({"path": path, "disposition": "mapped", "destinations": sorted(mapped_by_path[path])})
            else:
                ledger.append({"path": path, "disposition": "preserved", "preservation_target": "source-artifact"})
        if not ledger:
            ledger = [{"path": "/", "disposition": "preserved", "preservation_target": "source-artifact"}]
            paths = ["/"]
        batch = self.record("import_batch", self.batch_id, {"archive_id": self.archive_id,
            "batch_id": self.batch_id, "adapter": {"namespace": self.namespace, "version": self.version},
            "format_fingerprint": self.fingerprint, "profile_paths": paths, "coverage_ledger": ledger})
        records.append(batch)
        records.sort(key=lambda item: (item["record_type"], item["entity_id"], item["revision_id"]))
        raw_records = b"".join(json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False).encode("utf-8") + b"\n" for item in records)
        batches = self.root / "batches"
        batches.mkdir(parents=True, exist_ok=True)
        if batches.is_symlink() or not batches.is_dir(): reject("ARCHIVE_PATH")
        batch_dir.mkdir()
        (batch_dir / "records.ndjson").write_bytes(raw_records)
        manifest = {"spec_version": "0.1", "profile": "batch-ndjson", "sensitivity": "restricted-unclassified",
            "archive_id": self.archive_id, "batch_id": self.batch_id,
            "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "adapter": {"namespace": self.namespace, "version": self.version}, "sources": [source_item], "blobs": [],
            "records": {"path": "records.ndjson", "byte_digest": "sha256:" + hashlib.sha256(raw_records).hexdigest(),
                "size": len(raw_records), "count": len(records),
                "type_counts": dict(sorted(Counter(item["record_type"] for item in records).items()))}}
        (batch_dir / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        # Keep this dependency local to the write operation.  Importing a
        # shape-only profiler must not eagerly load the JSON Schema validator
        # merely because the package exposes the built-in adapters.
        try:
            from .validation import ValidationError as ArchiveValidationError, validate_archive
        except ImportError:
            reject("ARCHIVE_VALIDATION")
        try:
            validate_archive(self.root)
        except ArchiveValidationError:
            reject("ARCHIVE_VALIDATION")
        return BatchResult(batch_dir=batch_dir, coverage_ledger=ledger, diagnostics=())
