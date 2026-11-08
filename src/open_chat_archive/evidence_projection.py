"""Experimental, local evidence projection over validated OCA archives.

The projection is a Tier 2 consumer view. It retains each full canonical
envelope and does not alter Tier 1 identity or schema semantics.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any

from .inspection import InspectionModel, load_archive
from .validation import ValidationError


def _text(record: dict[str, Any]) -> str:
    body = record.get("body", {})
    if record.get("record_type") != "message" or body.get("visibility") != "user_visible":
        return ""
    chunks = [part["text"] for part in body.get("content", [])
              if part.get("type") in {"text", "code"} and isinstance(part.get("text"), str)]
    return "\n".join(chunks)


def _citations(model: InspectionModel, key: tuple[str, str],
               sources_by_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    citations: list[dict[str, Any]] = []
    observations = sorted(
        (record for record in model.records.values()
         if record["record_type"] == "observation"
         and record["body"]["subject"] == {"entity_id": key[0], "revision_id": key[1]}),
        key=lambda record: (record["entity_id"], record["revision_id"]),
    )
    for observation in observations:
        for binding in observation["body"]["bindings"]:
            bound_sources = []
            for source in binding.get("sources", []):
                source_record = sources_by_id[source["artifact_id"]]
                bound_sources.append({
                    "artifact_id": source["artifact_id"],
                    "byte_digest": source_record["body"]["byte_digest"],
                    "location": deepcopy(source["location"]),
                })
            bound_sources.sort(key=lambda value: (
                value["artifact_id"], value["byte_digest"],
                json.dumps(value["location"], ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            ))
            citations.append({
                "observation_entity_id": observation["entity_id"],
                "observation_revision_id": observation["revision_id"],
                "field_path": binding["field_path"],
                "claim_basis": binding["claim_basis"],
                "sources": bound_sources,
            })
    return sorted(citations, key=lambda item: (
        item["observation_entity_id"], item["observation_revision_id"],
        item["field_path"], item["claim_basis"],
        json.dumps(item["sources"], ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    ))


def _project_model(model: InspectionModel) -> dict[str, Any]:
    sources_by_id = {record["entity_id"]: record for record in model.records.values()
                     if record["record_type"] == "source_artifact"}
    entries = []
    for key in sorted(model.records):
        record = model.records[key]
        occurrences = sorted(
            ({"batch_id": item.batch_id, "ordinal": item.ordinal}
             for item in model.occurrences[key]),
            key=lambda item: (item["batch_id"], item["ordinal"]),
        )
        entries.append({
            "record": deepcopy(dict(record)),
            "occurrences": occurrences,
            "session_ids": sorted(model.record_sessions.get(key, ())),
            "text": _text(dict(record)),
            "citations": _citations(model, key, sources_by_id),
        })
    return {"projection_version": "oca-evidence-v1", "archive_id": model.archive_id,
            "records": entries}


def project_archive(path: Path) -> dict[str, Any]:
    """Validate and deterministically project an archive without modifying it."""
    return _project_model(load_archive(Path(path)))


def _write_private_atomic(path: Path, payload: bytes) -> None:
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _ensure_output_outside_archive(archive_root: Path, output: Path) -> None:
    archive = archive_root.resolve(strict=True)
    parent = output.parent.resolve(strict=True) if output.parent.exists() else output.parent.resolve()
    target = parent / output.name
    try:
        target.relative_to(archive)
    except ValueError:
        return
    raise ValueError("OUTPUT_INSIDE_ARCHIVE")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write a private experimental projection of a validated OCA archive")
    parser.add_argument("archive_root", type=Path)
    parser.add_argument("--output", required=True, type=Path,
                        help="explicit output file for the full sensitive projection")
    args = parser.parse_args(argv)
    try:
        _ensure_output_outside_archive(args.archive_root, args.output)
        projection = project_archive(args.archive_root)
        data = (json.dumps(projection, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
        _write_private_atomic(args.output, data)
        return 0
    except (ValidationError, OSError, ValueError, KeyError, TypeError) as exc:
        # Validation errors contain stable rule codes only. Never print payloads.
        print(f"evidence projection failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
