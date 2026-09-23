"""Draft 2020-12 batch and archive validation with value-safe diagnostics."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
from collections import Counter
from importlib import resources
from pathlib import Path
from typing import Any, NoReturn

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError

from .canonical import entity_identity, revision_identity
from .strictjson import StrictJSONError, load_file, load_ndjson, load_source_ndjson, loads_bytes, loads_source_bytes

ATTESTATION_FORMAT = "oca-validation-attestation/v0.1-experimental"
ATTESTATION_SPEC_VERSION = "0.1"
ATTESTATION_KIND = "oca-archive-validation"
ATTESTATION_STATUS = "passed"
ATTESTATION_VALIDATOR_NAME = "open-chat-archive-validate"
ATTESTATION_TARGET = "ATTESTATION_TARGET"
ATTESTATION_EXISTS = "ATTESTATION_EXISTS"
ATTESTATION_OVERLAP = "ATTESTATION_OVERLAP"
ATTESTATION_PUBLISH = "ATTESTATION_PUBLISH"
ATTESTATION_KEYS = (
    "archive_id", "attestation_kind", "batches", "format", "spec_version", "status", "validator",
)
ATTESTATION_VALIDATOR_KEYS = ("name", "package_version")
ATTESTATION_BATCH_KEYS = ("batch_id", "manifest", "records")
ATTESTATION_MANIFEST_KEYS = ("byte_digest", "path", "size")
ATTESTATION_RECORDS_KEYS = ("byte_digest", "count", "path", "size")

DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
SEMANTIC = {"session", "turn", "message", "event", "span", "artifact", "relationship", "revision"}
CONTROL = {"source_artifact", "import_batch", "observation", "proposal"}


class ValidationError(Exception):
    """Only stable rule codes and structural ordinals belong in messages."""


def fail(code: str, ordinal: int | None = None, record_type: str | None = None) -> NoReturn:
    suffix = f" ordinal={ordinal}" if ordinal is not None else ""
    suffix += f" type={record_type}" if record_type else ""
    raise ValidationError(f"{code}{suffix}")


def _schema(root: Path | None, name: str) -> tuple[dict[str, Any], Draft202012Validator]:
    """Load installed canonical bytes unless an explicit repository root is supplied."""
    try:
        data = (load_file(root / "schemas" / "v0.1" / name) if root is not None else
                loads_bytes(resources.files("open_chat_archive.schema_assets").joinpath("v0.1", name).read_bytes()))
    except StrictJSONError: fail("SCHEMA_JSON")
    try: Draft202012Validator.check_schema(data)
    except SchemaError: fail("SCHEMA_COMPILE")
    return data, Draft202012Validator(data, format_checker=FormatChecker())


def _regular_under(root: Path, relative: str, allowed: tuple[str, ...]) -> Path:
    if not isinstance(relative, str) or not any(re.fullmatch(pattern, relative) for pattern in allowed): fail("PATH_ALLOWLIST")
    candidate = root / relative
    try:
        cursor = root
        for component in Path(relative).parts:
            cursor = cursor / component
            if stat.S_ISLNK(cursor.lstat().st_mode): fail("PATH_SYMLINK")
        info = candidate.lstat()
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (OSError, ValueError): fail("PATH_MISSING_OR_ESCAPE")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode): fail("PATH_NOT_REGULAR")
    return resolved


def _read_nofollow_owner(path: Path) -> bytes:
    """Open a regular file with O_NOFOLLOW after owner checks; never follow a planted symlink."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(os.fspath(path), flags)
    except OSError:
        fail("FILE_READ")
    try:
        info = os.fstat(fd)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            fail("PATH_NOT_REGULAR")
        if info.st_uid != os.geteuid():
            fail("FILE_READ")
        size = info.st_size
        if size < 0:
            fail("FILE_READ")
        if size == 0:
            return b""
        data = bytearray()
        while len(data) < size:
            try:
                chunk = os.read(fd, size - len(data))
            except OSError:
                fail("FILE_READ")
            if not chunk:
                break
            data.extend(chunk)
        return bytes(data)
    finally:
        os.close(fd)


def _verify_file(root: Path, item: dict[str, Any], allowed: tuple[str, ...]) -> None:
    relative = item.get("path", "")
    _regular_under(root, relative, allowed)
    raw = _read_nofollow_owner(root / relative)
    if len(raw) != item.get("size") or "sha256:" + hashlib.sha256(raw).hexdigest() != item.get("byte_digest"): fail("FILE_INTEGRITY")


def _source_bytes(archive_root: Path, record: dict[str, Any]) -> bytes:
    relative = record["body"]["storage_path"]
    _regular_under(archive_root, relative, (r"sources/[A-Za-z0-9][A-Za-z0-9._-]{0,127}",))
    return _read_nofollow_owner(archive_root / relative)


def _ranges(value: Any) -> None:
    if isinstance(value, dict):
        if set(value) == {"start", "end"} and all(isinstance(value[k], int) for k in value) and value["start"] > value["end"]: fail("RANGE_ORDER")
        for child in value.values(): _ranges(child)
    elif isinstance(value, list):
        for child in value: _ranges(child)


def _artifact_references(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        artifact_id = value.get("artifact_id")
        if isinstance(artifact_id, str): found.add(artifact_id)
        for child in value.values(): found.update(_artifact_references(child))
    elif isinstance(value, list):
        for child in value: found.update(_artifact_references(child))
    return found


def reconcile_coverage(profile_paths: list[str], ledger: list[dict[str, Any]]) -> None:
    paths = [entry.get("path") for entry in ledger]
    if len(paths) != len(set(paths)): fail("COVERAGE_DUPLICATE")
    if set(paths) != set(profile_paths) or len(paths) != len(profile_paths): fail("COVERAGE_INCOMPLETE")


def _covered(field_path: str, bindings: list[dict[str, Any]]) -> bool:
    # A binding covers its exact JSON Pointer and every descendant.
    return any(field_path == p or field_path.startswith(p + "/") or p == "" for p in (b["field_path"] for b in bindings))


def _pointer(value: Any, pointer: str) -> Any:
    if pointer == "": return value
    current = value
    try:
        for token in pointer[1:].split("/"):
            token = token.replace("~1", "/").replace("~0", "~")
            current = current[int(token)] if isinstance(current, list) else current[token]
    except (KeyError, IndexError, ValueError, TypeError): fail("EVIDENCE_LOCATION")
    return current


def _leaf_paths(value: Any, path: str = "/body") -> list[str]:
    if isinstance(value, dict):
        result: list[str] = []
        for key, child in value.items(): result += _leaf_paths(child, path + "/" + key.replace("~", "~0").replace("/", "~1"))
        return result
    if isinstance(value, list):
        result = []
        for index, child in enumerate(value): result += _leaf_paths(child, path + f"/{index}")
        return result
    return [path]


def validate_batch(batch_dir: Path, *, schema_root: Path | None = None, archive_root: Path | None = None) -> list[dict[str, Any]]:
    archive_root = archive_root or (batch_dir.parents[1] if batch_dir.parent.name == "batches" else batch_dir)
    _, manifest_validator = _schema(schema_root, "import-manifest.schema.json")
    _, record_validator = _schema(schema_root, "archive-record.schema.json")
    try: manifest = loads_bytes(_read_nofollow_owner(batch_dir / "manifest.json"))
    except StrictJSONError: fail("MANIFEST_JSON")
    if list(manifest_validator.iter_errors(manifest)): fail("MANIFEST_SCHEMA")
    if batch_dir.name != manifest["batch_id"] or archive_root.name != manifest["archive_id"]: fail("MANIFEST_PATH_METADATA")
    all_files = manifest["sources"] + manifest["blobs"]
    paths = [item["path"] for item in all_files]
    if len(paths) != len(set(paths)): fail("MANIFEST_FILE_DUPLICATE")
    for item in manifest["sources"]: _verify_file(archive_root, item, (r"sources/[A-Za-z0-9][A-Za-z0-9._-]{0,127}",))
    for item in manifest["blobs"]: _verify_file(archive_root, item, (r"blobs/[A-Za-z0-9][A-Za-z0-9._-]{0,127}",))
    _regular_under(batch_dir, manifest["records"]["path"], (r"records\.ndjson",))
    raw = _read_nofollow_owner(batch_dir / manifest["records"]["path"])
    if len(raw) != manifest["records"]["size"] or "sha256:" + hashlib.sha256(raw).hexdigest() != manifest["records"]["byte_digest"]: fail("RECORD_FILE_INTEGRITY")
    try: records = load_ndjson(raw)
    except StrictJSONError: fail("NDJSON_JSON")
    seen: dict[tuple[str, str], bytes] = {}
    entity_types: dict[str, str] = {}
    for ordinal, record in enumerate(records, 1):
        rtype = record.get("record_type") if isinstance(record, dict) else None
        if list(record_validator.iter_errors(record)): fail("RECORD_SCHEMA", ordinal, rtype)
        _ranges(record)
        if record["identity"]["record_type"] != rtype or record["identity"]["archive_id"] != manifest["archive_id"] or record["identity"]["adapter_namespace"] != manifest["adapter"]["namespace"]: fail("IDENTITY_METADATA", ordinal, rtype)
        if record["entity_id"] != entity_identity(record["identity"]): fail("ENTITY_ID", ordinal, rtype)
        if record["revision_id"] != revision_identity(record): fail("REVISION_ID", ordinal, rtype)
        key = (record["entity_id"], record["revision_id"])
        encoded = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
        if key in seen: fail("DUPLICATE_RECORD" if seen[key] == encoded else "ID_COLLISION", ordinal, rtype)
        seen[key] = encoded
        if record["entity_id"] in entity_types and entity_types[record["entity_id"]] != rtype: fail("ENTITY_TYPE_COLLISION", ordinal, rtype)
        entity_types[record["entity_id"]] = rtype
    if records != sorted(records, key=lambda r: (r["record_type"], r["entity_id"], r["revision_id"])): fail("RECORD_ORDER")
    counts = dict(sorted(Counter(r["record_type"] for r in records).items()))
    if len(records) != manifest["records"]["count"] or counts != manifest["records"]["type_counts"]: fail("RECORD_COUNTS")
    source_list = [r for r in records if r["record_type"] == "source_artifact"]
    sources = {r["body"]["byte_digest"]: r for r in source_list}
    if len(sources) != len(source_list): fail("SOURCE_RECORD_DUPLICATE")
    if set(sources) != {s["byte_digest"] for s in manifest["sources"]}: fail("SOURCE_RECONCILE")
    for item in manifest["sources"]:
        body = sources[item["byte_digest"]]["body"]
        if body["archive_id"] != manifest["archive_id"]: fail("SOURCE_METADATA")
        if any(body[k] != item[k] for k in ("byte_digest", "media_type", "size", "storage_path") if k in body and (k != "storage_path" or item["path"] != body[k])): fail("SOURCE_METADATA")
    batch_records = [r for r in records if r["record_type"] == "import_batch"]
    if len(batch_records) != 1: fail("BATCH_RECORD_COUNT")
    batch = batch_records[0]["body"]
    if batch["archive_id"] != manifest["archive_id"] or batch["batch_id"] != manifest["batch_id"] or batch["adapter"] != manifest["adapter"]: fail("BATCH_METADATA")
    reconcile_coverage(batch["profile_paths"], batch["coverage_ledger"])
    by_revision = {(r["entity_id"], r["revision_id"]): r for r in records}
    proposals = {r["entity_id"]: r for r in records if r["record_type"] == "proposal"}
    observations: dict[tuple[str, str], list[dict[str, Any]]] = {}
    source_records = {r["entity_id"]: r for r in records if r["record_type"] == "source_artifact"}
    source_ids = set(source_records)
    source_values: dict[str, Any] = {}
    source_bytes: dict[str, bytes] = {}
    for r in records:
        if r["record_type"] != "observation": continue
        subject = r["body"]["subject"]
        target = by_revision.get((subject["entity_id"], subject["revision_id"]))
        if not target: fail("EVIDENCE_SUBJECT")
        for binding in r["body"]["bindings"]:
            _pointer(target, binding["field_path"])
            for source in binding.get("sources", []):
                if source["artifact_id"] not in source_ids: fail("EVIDENCE_SOURCE")
                source_record = source_records[source["artifact_id"]]
                location = source["location"]
                if source["artifact_id"] not in source_bytes:
                    source_bytes[source["artifact_id"]] = _source_bytes(archive_root, source_record)
                source_raw = source_bytes[source["artifact_id"]]
                if "byte_range" in location and location["byte_range"]["end"] > len(source_raw): fail("EVIDENCE_RANGE")
                if "json_pointer" in location:
                    if source_record["body"]["media_type"] not in {"application/json", "application/x-ndjson"}: fail("EVIDENCE_COORDINATE")
                    if source["artifact_id"] not in source_values:
                        try:
                            source_values[source["artifact_id"]] = (load_source_ndjson(source_raw) if source_record["body"]["media_type"] == "application/x-ndjson" else loads_source_bytes(source_raw))
                        except StrictJSONError: fail("SOURCE_JSON")
                    _pointer(source_values[source["artifact_id"]], location["json_pointer"])
                if "jsonl_line" in location:
                    if source_record["body"]["media_type"] != "application/x-ndjson": fail("EVIDENCE_COORDINATE")
                    if source["artifact_id"] not in source_values:
                        try: source_values[source["artifact_id"]] = load_source_ndjson(source_raw)
                        except StrictJSONError: fail("SOURCE_JSON")
                    line_count = len(source_values[source["artifact_id"]])
                    if location["jsonl_line"] > line_count: fail("EVIDENCE_RANGE")
                if "markdown_lines" in location:
                    if source_record["body"]["media_type"] not in {"text/markdown", "text/plain"}: fail("EVIDENCE_COORDINATE")
                    try: line_count = len(source_raw.decode("utf-8", errors="strict").splitlines())
                    except UnicodeDecodeError: fail("EVIDENCE_COORDINATE")
                    if location["markdown_lines"]["end"] > line_count: fail("EVIDENCE_RANGE")
                if binding["claim_basis"] in {"parsed", "observed"}:
                    media = source_record["body"]["media_type"]
                    if media == "application/json" and "json_pointer" not in location: fail("EVIDENCE_COORDINATE")
                    if media == "application/x-ndjson" and "jsonl_line" not in location: fail("EVIDENCE_COORDINATE")
            if binding["claim_basis"] == "model_inferred":
                proposal = proposals.get(binding.get("proposal_id"))
                if not proposal or proposal["body"]["basis"] != "model_inferred" or proposal["body"]["status"] != "approved" or proposal["body"]["subject"] != subject or proposal["body"]["field_path"] != binding["field_path"]: fail("PROPOSAL_GATE")
        observations.setdefault((subject["entity_id"], subject["revision_id"]), []).extend(r["body"]["bindings"])
    for r in records:
        if r["record_type"] == "proposal":
            body = r["body"]
            proposed_target = by_revision.get((body["subject"]["entity_id"], body["subject"]["revision_id"]))
            if not proposed_target: fail("PROPOSAL_SUBJECT")
            proposed_value = _pointer(proposed_target, body["field_path"])
            for evidence in body["evidence"]:
                if evidence["artifact_id"] not in source_ids: fail("EVIDENCE_SOURCE")
            if body["status"] in {"approved", "rejected"}:
                if body.get("approval", {}).get("status") != body["status"]: fail("PROPOSAL_LIFECYCLE")
                if body["status"] == "approved" and proposed_value != body["proposed_value"]: fail("PROPOSAL_VALUE")
            elif "approval" in body: fail("PROPOSAL_LIFECYCLE")
        if r["record_type"] in SEMANTIC:
            bindings = observations.get((r["entity_id"], r["revision_id"]), [])
            paths = _leaf_paths(r["body"])
            if "extensions" in r: paths += _leaf_paths(r["extensions"], "/extensions")
            if any(not _covered(path, bindings) for path in paths): fail("EVIDENCE_COVERAGE", record_type=r["record_type"])
    blob_manifest = {item["path"]: item for item in manifest["blobs"]}
    blob_records: dict[str, dict[str, Any]] = {}
    artifacts = {r["entity_id"]: r for r in records if r["record_type"] == "artifact"}
    for record in artifacts.values():
        body = record["body"]
        if body["storage_path"].startswith("blobs/"):
            if body["storage_path"] in blob_records: fail("BLOB_RECORD_DUPLICATE")
            blob_records[body["storage_path"]] = body
    if set(blob_records) != set(blob_manifest): fail("BLOB_RECONCILE")
    for path, item in blob_manifest.items():
        body = blob_records[path]
        if any(body[key] != item[key] for key in ("byte_digest", "media_type", "size")): fail("BLOB_METADATA")
    artifact_ids = source_ids | set(artifacts)
    for record in records:
        for artifact_id in _artifact_references(record["body"]):
            if artifact_id not in artifact_ids: fail("ARTIFACT_REFERENCE", record_type=record["record_type"])
    entity_ids = set(by_revision)
    ids = {record["entity_id"] for record in records}
    for record in records:
        body = record["body"]
        rtype = record["record_type"]
        if rtype in {"message", "turn", "event", "span"} and body["session_id"] not in ids: fail("SUBJECT_REFERENCE", record_type=rtype)
        if rtype == "span" and body.get("parent_span_id") is not None and body["parent_span_id"] not in ids: fail("SUBJECT_REFERENCE", record_type=rtype)
        if rtype == "relationship" and (body["from_id"] not in ids or body["to_id"] not in ids): fail("SUBJECT_REFERENCE", record_type=rtype)
        if rtype == "revision" and ((body["entity_id"], body["from_revision_id"]) not in entity_ids or (body["entity_id"], body["to_revision_id"]) not in entity_ids): fail("REVISION_REFERENCE")
        if rtype == "event":
            for name in ("request_refs", "response_refs"):
                if any(item not in ids for item in body["payload"].get(name, [])): fail("SUBJECT_REFERENCE", record_type=rtype)
            if body.get("message_id") is not None and body["message_id"] not in ids: fail("SUBJECT_REFERENCE", record_type=rtype)
        if rtype == "session" and body.get("active_message_id") is not None and body["active_message_id"] not in ids:
            fail("SUBJECT_REFERENCE", record_type=rtype)
    ordinals: dict[tuple[str, str, int], str] = {}
    for record in records:
        body = record["body"]; rtype = record["record_type"]
        if rtype in {"message", "turn", "event", "span"} and isinstance(body.get("source_ordinal"), int):
            key = (body["session_id"], rtype, body["source_ordinal"])
            if key in ordinals: fail("SOURCE_ORDINAL_UNIQUE", record_type=rtype)
            ordinals[key] = record["entity_id"]
    return records


def validate_archive(archive_root: Path, *, schema_root: Path | None = None) -> list[Path]:
    allowed = [re.compile(r"^(?:sources|blobs)/[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"), re.compile(r"^batches/[A-Za-z0-9][A-Za-z0-9._-]{0,127}/(?:manifest\.json|records\.ndjson)$"), re.compile(r"^import-manifests/[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.json$")]
    for dirpath, dirnames, filenames in os.walk(archive_root, topdown=True, followlinks=False):
        base = Path(dirpath)
        keep: list[str] = []
        for name in dirnames:
            child = base / name
            if child.is_symlink():
                fail("ARCHIVE_SYMLINK")
            keep.append(name)
        dirnames[:] = keep
        for name in filenames:
            child = base / name
            if child.is_symlink():
                fail("ARCHIVE_SYMLINK")
            rel = child.relative_to(archive_root).as_posix()
            if not any(pattern.fullmatch(rel) for pattern in allowed):
                fail("ARCHIVE_PATH_ALLOWLIST")
    manifests = sorted(archive_root.glob("batches/*/manifest.json"))
    if not manifests: fail("ARCHIVE_NO_BATCH")
    declared: set[str] = set()
    revisions: dict[tuple[str, str], tuple[str, bytes]] = {}
    for manifest in manifests:
        batch_records = validate_batch(manifest.parent, schema_root=schema_root, archive_root=archive_root)
        for record in batch_records:
            key = (record["entity_id"], record["revision_id"])
            encoded = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
            prior = revisions.get(key)
            if prior and prior != (record["record_type"], encoded): fail("ARCHIVE_ID_COLLISION")
            revisions[key] = (record["record_type"], encoded)
        try: data = loads_bytes(_read_nofollow_owner(manifest))
        except StrictJSONError: fail("MANIFEST_JSON")
        declared.update(item["path"] for item in data["sources"] + data["blobs"])
    discovered: set[str] = set()
    for kind in ("sources", "blobs"):
        parent = archive_root / kind
        if not parent.exists():
            continue
        if parent.is_symlink():
            fail("ARCHIVE_SYMLINK")
        for item in parent.iterdir():
            if item.is_symlink():
                fail("ARCHIVE_SYMLINK")
            if item.is_file():
                discovered.add(item.relative_to(archive_root).as_posix())
    if discovered != declared: fail("ARCHIVE_FILE_RECONCILE")
    for import_manifest in archive_root.glob("import-manifests/*.json"):
        if import_manifest.is_symlink():
            fail("ARCHIVE_SYMLINK")
        try:
            imported_raw = _read_nofollow_owner(import_manifest)
        except ValidationError:
            fail("IMPORT_MANIFEST_JSON")
        try:
            imported = loads_bytes(imported_raw)
        except StrictJSONError:
            fail("IMPORT_MANIFEST_JSON")
        _, validator = _schema(schema_root, "import-manifest.schema.json")
        if list(validator.iter_errors(imported)): fail("IMPORT_MANIFEST_SCHEMA")
        batch_manifest = archive_root / "batches" / imported["batch_id"] / "manifest.json"
        try:
            authoritative_raw = _read_nofollow_owner(batch_manifest)
        except ValidationError:
            fail("IMPORT_MANIFEST_TARGET")
        try:
            authoritative = loads_bytes(authoritative_raw)
        except StrictJSONError:
            fail("IMPORT_MANIFEST_TARGET")
        if imported != authoritative: fail("IMPORT_MANIFEST_MISMATCH")
    return [p.parent for p in manifests]


def _attestation_from_validated(archive_root: Path) -> dict[str, Any]:
    """Build the Python/Go attestation object. Caller must have validated the archive."""
    from . import __version__
    archive_id = archive_root.name
    batches: list[dict[str, Any]] = []
    for manifest_path in sorted(archive_root.glob("batches/*/manifest.json")):
        batch_id = manifest_path.parent.name
        records_path = manifest_path.parent / "records.ndjson"
        try:
            manifest_raw = _read_nofollow_owner(manifest_path)
            records_raw = _read_nofollow_owner(records_path)
            manifest = loads_bytes(manifest_raw)
        except (OSError, StrictJSONError, ValidationError):
            fail("ATTESTATION_PUBLISH")
        if not isinstance(manifest, dict):
            fail("ATTESTATION_PUBLISH")
        records_info = manifest.get("records")
        count = records_info.get("count") if isinstance(records_info, dict) else None
        if not isinstance(count, int) or isinstance(count, bool):
            fail("ATTESTATION_PUBLISH")
        batches.append({
            "batch_id": batch_id,
            "manifest": {
                "byte_digest": "sha256:" + hashlib.sha256(manifest_raw).hexdigest(),
                "path": "batches/" + batch_id + "/manifest.json",
                "size": len(manifest_raw),
            },
            "records": {
                "byte_digest": "sha256:" + hashlib.sha256(records_raw).hexdigest(),
                "count": count,
                "path": "batches/" + batch_id + "/records.ndjson",
                "size": len(records_raw),
            },
        })
    batches.sort(key=lambda item: item["batch_id"])
    return {
        "archive_id": archive_id,
        "attestation_kind": ATTESTATION_KIND,
        "batches": batches,
        "format": ATTESTATION_FORMAT,
        "spec_version": ATTESTATION_SPEC_VERSION,
        "status": ATTESTATION_STATUS,
        "validator": {
            "name": ATTESTATION_VALIDATOR_NAME,
            "package_version": __version__,
        },
    }


def build_validation_attestation(archive_root: Path) -> dict[str, Any]:
    """Archive-only attestation. Always runs ``validate_archive``; never reports passed without it."""
    validate_archive(archive_root)
    return _attestation_from_validated(archive_root)


def write_validation_attestation(archive_root: Path, dest: Path) -> None:
    """Exclusive owner-only publication after a successful ``validate_archive``."""
    from .gather_store import (
        ARGUMENT_INVALID,
        GatherStoreError,
        canonical_object_bytes,
        exclusive_publish,
        paths_overlap,
    )
    text = os.fspath(dest)
    if not os.path.isabs(text):
        fail(ATTESTATION_TARGET)
    try:
        if paths_overlap(text, False, os.fspath(archive_root), True):
            fail(ATTESTATION_OVERLAP)
    except GatherStoreError as error:
        if error.code == ARGUMENT_INVALID:
            fail(ATTESTATION_TARGET)
        fail(ATTESTATION_PUBLISH)
    payload = canonical_object_bytes(build_validation_attestation(archive_root))
    try:
        exclusive_publish(text, payload, exist_code=ATTESTATION_EXISTS, exist_exit=2)
    except GatherStoreError as error:
        if error.code == ATTESTATION_EXISTS:
            fail(ATTESTATION_EXISTS)
        fail(ATTESTATION_PUBLISH)


def _check_attributes(root: Path) -> None:
    probes = ["archives/a/sources/o", "archives/a/blobs/o", "archives/a/batches/b/records.ndjson", "archives/a/batches/b/manifest.json", "archives/a/import-manifests/m.json"]
    for path in root.glob("archives/**/*"):
        if path.is_file(): probes.append(path.relative_to(root).as_posix())
    try:
        result = subprocess.run(["git", "check-attr", "filter", "diff", "--", *probes], cwd=root, text=True, capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError): fail("GIT_ATTR_CHECK")
    lines = result.stdout.splitlines()
    if len(lines) != len(probes) * 2 or any(not line.endswith(": git-crypt") for line in lines): fail("GIT_ATTR_EFFECTIVE")


def validate_repository(root: Path) -> list[Path]:
    _schema(root, "archive-record.schema.json"); _schema(root, "import-manifest.schema.json")
    archives = sorted(root.glob("examples/v0.1/*/archives/*"))
    batches: list[Path] = []
    for archive in archives: batches += validate_archive(archive, schema_root=root)
    for archive in sorted((root / "archives").glob("*")) if (root / "archives").is_dir() else []:
        if archive.is_symlink() or not archive.is_dir(): fail("ARCHIVE_ROOT_KIND")
        batches += validate_archive(archive, schema_root=root)
    if len(batches) < 2: fail("EXAMPLE_COUNT")
    _check_attributes(root)
    clean = (root / "docs/clean-room.md").read_text(encoding="utf-8")
    for phrase in ("public README", "advertised feature set", "AGPL license", "file-level structure", "implementation bodies", "tests", "schemas", "fixtures", "internal algorithms"):
        if phrase not in clean: fail("PROVENANCE_STATEMENT")
    for removed in ("docs/clean-room-provenance.md", "docs/spec/v0.1/overview.md", "docs/spec/v0.1/batch-ndjson.md"):
        if (root / removed).exists(): fail("REDUNDANT_DOC")
    return batches


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a batch, archive, or repository (no source values in diagnostics)")
    parser.add_argument("path", nargs="?", type=Path, default=Path.cwd())
    parser.add_argument("--archive-root", type=Path)
    parser.add_argument("--attestation-output", type=Path)
    args = parser.parse_args(argv)
    is_batch = (args.path / "manifest.json").is_file()
    is_archive = (args.path / "batches").is_dir()
    if args.attestation_output is not None and (is_batch or not is_archive):
        print(f"validation failed: {ATTESTATION_TARGET}")
        return 2
    try:
        if is_batch: validate_batch(args.path, archive_root=args.archive_root)
        elif is_archive:
            validate_archive(args.path)
            if args.attestation_output is not None:
                write_validation_attestation(args.path, args.attestation_output)
        else: validate_repository(args.path)
    except ValidationError as error:
        print(f"validation failed: {error}")
        return 1
    print("validation passed")
    return 0


if __name__ == "__main__": raise SystemExit(main())
