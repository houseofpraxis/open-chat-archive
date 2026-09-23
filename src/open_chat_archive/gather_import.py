"""Verify, plan, and import supported gather receipts into canonical archives.

Never opens receipt ``origin_path``. Never invents a Pi importer. Default
stdout is one aggregate JSON object with fixed codes and counts.
"""
from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import os
import posixpath
import secrets
import shutil
import stat
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping, NoReturn

from .batch_writer import AdapterInputError
from .gather_store import (
    ADAPTER_MAX_BYTES,
    ARGUMENT_INVALID,
    GATHER_PLATFORM_UNSUPPORTED,
    IMPORTABLE_ADAPTERS,
    OUTPUT_EXISTS,
    OUTPUT_UNTRUSTED,
    PARTIAL_OR_DEFERRED,
    SOURCE_STORE_OVERLAP,
    STORE_BUSY,
    STORE_OBJECT_CORRUPT,
    STORE_UNTRUSTED,
    GatherStore,
    GatherStoreError,
    Receipt,
    SHA256_PREFIX,
    StoreStatus,
    archive_id_for,
    canonical_object_bytes,
    exclusive_publish,
    go_json,
    is_hex64,
    locked_store,
    open_child_dir,
    open_child_file,
    open_dir_abs,
    owner_only_dir,
    owner_only_file,
    paths_overlap,
    pread_all,
    replace_publish,
    sha_hex_prefixed,
    split_leaf,
    valid_named_adapter,
    valid_scope,
)
from .source_adapters import create_adapter
from .strictjson import StrictJSONError, loads_bytes
from .validation import ValidationError, validate_archive


IMPORTER_UNAVAILABLE = "IMPORTER_UNAVAILABLE"
SCOPE_MISSING = "SCOPE_MISSING"
SCOPE_PLAN_CONFLICT = "SCOPE_PLAN_CONFLICT"
PLAN_EXISTS = "PLAN_EXISTS"
PLAN_UNTRUSTED = "PLAN_UNTRUSTED"
IMPORT_CONFLICT = "IMPORT_CONFLICT"
ADAPTER_SIZE_LIMIT = "ADAPTER_SIZE_LIMIT"
ADAPTER_IMPORT_REJECTED = "ADAPTER_IMPORT_REJECTED"
ARCHIVE_PARENT_UNTRUSTED = "ARCHIVE_PARENT_UNTRUSTED"
OK = "OK"

PLAN_FORMAT = "oca-gather-import-plan/v0.1-experimental"
LEDGER_FORMAT = "oca-bridge-import-ledger/v0.1-experimental"
ARCHIVES_MARKER_NAME = ".oca-bridge-archives"
ARCHIVES_MARKER_TEXT = b"oca-bridge-archives/v0.1-experimental\n"
LEDGER_NAME = ".oca-bridge-import-ledger.json"
PLAN_KEYS = ("format", "rows")
PLAN_ROW_KEYS = (
    "receipt_id", "adapter", "scope_state", "source_scope_id",
    "origin_path", "assigned_source_scope_id",
)
LEDGER_KEYS = ("format", "entries")
LEDGER_ENTRY_KEYS = (
    "receipt_id", "adapter", "source_scope_id", "archive_id", "batch_id",
    "records_sha256", "source_sha256",
)
COUNT_KEYS = (
    "selected", "added", "unchanged", "conflict", "importer_unavailable",
    "scope_missing", "deferred", "limit_hits",
)


class GatherImportError(Exception):
    def __init__(self, code: str, exit_code: int = 4) -> None:
        self.code = code
        self.exit_code = exit_code
        super().__init__(code)

    def __str__(self) -> str:
        return self.code

    def __repr__(self) -> str:
        return f"GatherImportError({self.code!r}, {self.exit_code})"


def _fail(code: str, exit_code: int = 4) -> NoReturn:
    raise GatherImportError(code, exit_code)


def _empty_counts() -> dict[str, int]:
    return {key: 0 for key in COUNT_KEYS}


def _wrap_store(error: GatherStoreError) -> NoReturn:
    raise GatherImportError(error.code, error.exit_code) from None


def _require_abs(value: str | None) -> str:
    if not isinstance(value, str) or not posixpath.isabs(value):
        _fail(ARGUMENT_INVALID, 2)
    return posixpath.normpath(value)


def _overlap_or_fail(left: str, left_is_dir: bool, right: str, right_is_dir: bool) -> None:
    try:
        if paths_overlap(left, left_is_dir, right, right_is_dir):
            _fail(SOURCE_STORE_OVERLAP, 8)
    except GatherStoreError as error:
        if error.code == ARGUMENT_INVALID:
            _fail(ARGUMENT_INVALID, 2)
        if error.code == SOURCE_STORE_OVERLAP:
            _fail(SOURCE_STORE_OVERLAP, 8)
        _wrap_store(error)


def _parse_json_object(data: bytes, untrusted: str) -> dict[str, Any]:
    if not data or data[-1:] != b"\n" or data.count(b"\n") != 1:
        _fail(untrusted)
    try:
        value = loads_bytes(data)
    except StrictJSONError:
        _fail(untrusted)
    if not isinstance(value, dict):
        _fail(untrusted)
    return value


def parse_plan(data: bytes) -> dict[str, Any]:
    try:
        raw = loads_bytes(data)
    except StrictJSONError:
        _fail(PLAN_UNTRUSTED)
    if not isinstance(raw, dict):
        _fail(PLAN_UNTRUSTED)
    if set(raw) != set(PLAN_KEYS) or raw.get("format") != PLAN_FORMAT:
        _fail(PLAN_UNTRUSTED)
    rows = raw["rows"]
    if not isinstance(rows, list):
        _fail(PLAN_UNTRUSTED)
    parsed_rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != set(PLAN_ROW_KEYS):
            _fail(PLAN_UNTRUSTED)
        receipt_id = row["receipt_id"]
        adapter = row["adapter"]
        scope_state = row["scope_state"]
        origin_path = row["origin_path"]
        source_scope_id = row["source_scope_id"]
        assigned = row["assigned_source_scope_id"]
        if not isinstance(receipt_id, str) or sha_hex_prefixed(receipt_id) is None:
            _fail(PLAN_UNTRUSTED)
        if receipt_id in seen:
            _fail(SCOPE_PLAN_CONFLICT)
        seen.add(receipt_id)
        if not isinstance(adapter, str) or not valid_named_adapter(adapter):
            _fail(PLAN_UNTRUSTED)
        if scope_state not in {"missing", "operator_supplied_unverified"}:
            _fail(PLAN_UNTRUSTED)
        if not isinstance(origin_path, str) or not origin_path.startswith("/") or "\x00" in origin_path:
            _fail(PLAN_UNTRUSTED)
        if source_scope_id is not None and (not isinstance(source_scope_id, str) or not valid_scope(source_scope_id)):
            _fail(PLAN_UNTRUSTED)
        if assigned is not None and (not isinstance(assigned, str) or not valid_scope(assigned)):
            _fail(PLAN_UNTRUSTED)
        if scope_state == "missing" and source_scope_id is not None:
            _fail(PLAN_UNTRUSTED)
        if scope_state == "operator_supplied_unverified" and source_scope_id is None:
            _fail(PLAN_UNTRUSTED)
        parsed_rows.append({
            "receipt_id": receipt_id,
            "adapter": adapter,
            "scope_state": scope_state,
            "source_scope_id": source_scope_id,
            "origin_path": origin_path,
            "assigned_source_scope_id": assigned,
        })
    return {"format": PLAN_FORMAT, "rows": parsed_rows}


def parse_ledger(data: bytes) -> dict[str, Any]:
    raw = _parse_json_object(data, IMPORT_CONFLICT)
    if set(raw) != set(LEDGER_KEYS) or raw.get("format") != LEDGER_FORMAT:
        _fail(IMPORT_CONFLICT)
    entries = raw["entries"]
    if not isinstance(entries, list):
        _fail(IMPORT_CONFLICT)
    parsed: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in entries:
        if not isinstance(item, dict) or set(item) != set(LEDGER_ENTRY_KEYS):
            _fail(IMPORT_CONFLICT)
        receipt_id = item["receipt_id"]
        if not isinstance(receipt_id, str) or sha_hex_prefixed(receipt_id) is None:
            _fail(IMPORT_CONFLICT)
        if receipt_id in seen:
            _fail(IMPORT_CONFLICT)
        seen.add(receipt_id)
        adapter = item["adapter"]
        scope = item["source_scope_id"]
        archive_id = item["archive_id"]
        batch_id = item["batch_id"]
        records_sha = item["records_sha256"]
        source_sha = item["source_sha256"]
        if not isinstance(adapter, str) or adapter not in IMPORTABLE_ADAPTERS:
            _fail(IMPORT_CONFLICT)
        if not isinstance(scope, str) or not valid_scope(scope):
            _fail(IMPORT_CONFLICT)
        if not isinstance(archive_id, str) or not is_hex64(archive_id):
            _fail(IMPORT_CONFLICT)
        if not isinstance(batch_id, str) or not is_hex64(batch_id):
            _fail(IMPORT_CONFLICT)
        if sha_hex_prefixed(records_sha) is None or sha_hex_prefixed(source_sha) is None:
            _fail(IMPORT_CONFLICT)
        parsed.append({
            "receipt_id": receipt_id,
            "adapter": adapter,
            "source_scope_id": scope,
            "archive_id": archive_id,
            "batch_id": batch_id,
            "records_sha256": records_sha,
            "source_sha256": source_sha,
        })
    body = {"format": LEDGER_FORMAT, "entries": parsed}
    if canonical_object_bytes(body) != data:
        _fail(IMPORT_CONFLICT)
    return body


def plan_document(receipts: tuple[Receipt, ...]) -> dict[str, Any]:
    rows = []
    for receipt in receipts:
        assigned = receipt.source_scope_id
        rows.append({
            "receipt_id": receipt.receipt_id,
            "adapter": receipt.adapter,
            "scope_state": receipt.scope_state,
            "source_scope_id": receipt.source_scope_id,
            "origin_path": receipt.origin_path,
            "assigned_source_scope_id": assigned,
        })
    rows.sort(key=lambda item: item["receipt_id"])
    return {"format": PLAN_FORMAT, "rows": rows}


def _read_owner_file(path: str, *, max_bytes: int, untrusted: str) -> bytes:
    directory, name = split_leaf(path)
    try:
        parent, _chain = open_dir_abs(directory)
    except GatherStoreError as error:
        _wrap_store(error)
    try:
        fd, info = open_child_file(parent, name)
        try:
            if not owner_only_file(info, os.geteuid()) or info.st_size > max_bytes:
                _fail(untrusted)
            return pread_all(fd, info.st_size, max_bytes)
        finally:
            os.close(fd)
    except GatherStoreError as error:
        if error.code in {STORE_UNTRUSTED, OUTPUT_UNTRUSTED}:
            _fail(untrusted)
        _wrap_store(error)
    finally:
        os.close(parent)


def _load_plan(path: str) -> dict[str, Any]:
    data = _read_owner_file(path, max_bytes=1 << 20, untrusted=PLAN_UNTRUSTED)
    return parse_plan(data)


def _load_ledger(path: str) -> dict[str, Any]:
    if not posixpath.exists(path):
        return {"format": LEDGER_FORMAT, "entries": []}
    data = _read_owner_file(path, max_bytes=4 << 20, untrusted=IMPORT_CONFLICT)
    return parse_ledger(data)


def _plan_row_matches(receipt: Receipt, plan_row: dict[str, Any] | None) -> bool:
    """Literal plan/store comparison. Missing rows and disagreeing fields do not match."""
    if plan_row is None:
        return False
    if plan_row.get("receipt_id") != receipt.receipt_id:
        return False
    if plan_row.get("adapter") != receipt.adapter:
        return False
    if plan_row.get("scope_state") != receipt.scope_state:
        return False
    if plan_row.get("origin_path") != receipt.origin_path:
        return False
    if plan_row.get("source_scope_id") != receipt.source_scope_id:
        return False
    assigned = plan_row.get("assigned_source_scope_id")
    if receipt.scope_state == "operator_supplied_unverified" and assigned != receipt.source_scope_id:
        return False
    return True


def resolve_scope(receipt: Receipt, plan_row: dict[str, Any] | None) -> str:
    if not _plan_row_matches(receipt, plan_row):
        _fail(SCOPE_PLAN_CONFLICT)
    assert plan_row is not None
    assigned = plan_row.get("assigned_source_scope_id")
    if receipt.scope_state == "operator_supplied_unverified":
        if receipt.source_scope_id is None:
            _fail(STORE_UNTRUSTED)
        return receipt.source_scope_id
    if assigned is None:
        _fail(SCOPE_MISSING, 8)
    return assigned


def _select_receipts(
    receipts: tuple[Receipt, ...],
    *,
    receipt_ids: set[str] | None,
    adapter: str | None,
) -> list[Receipt]:
    selected: list[Receipt] = []
    wanted = set(receipt_ids) if receipt_ids else None
    for receipt in receipts:
        if wanted is not None and receipt.receipt_id not in wanted and receipt.receipt_hex not in wanted:
            continue
        if adapter is not None and receipt.adapter != adapter:
            continue
        selected.append(receipt)
        if wanted is not None:
            wanted.discard(receipt.receipt_id)
            wanted.discard(receipt.receipt_hex)
    if wanted:
        _fail(ARGUMENT_INVALID, 2)
    return selected


def _aggregate(
    operation: str,
    *,
    complete: bool,
    code: str,
    counts: Mapping[str, int],
    by_adapter: Mapping[str, int],
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "operation": operation,
        "complete": complete,
        "code": code,
        "counts": {key: int(counts.get(key, 0)) for key in COUNT_KEYS},
        "by_adapter": dict(sorted((str(k), int(v)) for k, v in by_adapter.items())),
    }
    if extra:
        for key, value in extra.items():
            body[key] = value
    return body


def _emit(body: dict[str, Any]) -> None:
    sys.stdout.write(go_json(body).decode("utf-8"))


def _write_details(path: str, rows: list[dict[str, Any]]) -> None:
    document = {
        "format": "oca-gather-import-details/v0.1-experimental",
        "rows": rows,
    }
    exclusive_publish(path, canonical_object_bytes(document), exist_code=OUTPUT_EXISTS, exist_exit=2)


def _publish_archives_marker(marker_path: str) -> None:
    try:
        exclusive_publish(
            marker_path, ARCHIVES_MARKER_TEXT,
            exist_code=ARCHIVE_PARENT_UNTRUSTED, exist_exit=4)
    except GatherStoreError as error:
        if error.code != ARCHIVE_PARENT_UNTRUSTED:
            _wrap_store(error)
        data = _read_owner_file(marker_path, max_bytes=4096, untrusted=ARCHIVE_PARENT_UNTRUSTED)
        if data != ARCHIVES_MARKER_TEXT:
            _fail(ARCHIVE_PARENT_UNTRUSTED)


def _ensure_archives_parent(path: str, store_path: str) -> None:
    try:
        if paths_overlap(path, True, store_path, True):
            _fail(SOURCE_STORE_OVERLAP, 8)
    except GatherStoreError as error:
        if error.code == ARGUMENT_INVALID:
            _fail(ARGUMENT_INVALID, 2)
        if error.code == SOURCE_STORE_OVERLAP:
            _fail(SOURCE_STORE_OVERLAP, 8)
        _wrap_store(error)
    cleaned = posixpath.normpath(path)
    if posixpath.exists(cleaned):
        try:
            fd, chain = open_dir_abs(cleaned)
        except GatherStoreError as error:
            _wrap_store(error)
        try:
            info = os.fstat(fd)
            if not owner_only_dir(info, os.geteuid()):
                _fail(ARCHIVE_PARENT_UNTRUSTED)
            names = sorted(item for item in os.listdir(fd) if item not in {".", ".."})
            marker_path = posixpath.join(cleaned, ARCHIVES_MARKER_NAME)
            if ARCHIVES_MARKER_NAME not in names:
                if names:
                    _fail(ARCHIVE_PARENT_UNTRUSTED)
                _publish_archives_marker(marker_path)
            else:
                data = _read_owner_file(marker_path, max_bytes=4096, untrusted=ARCHIVE_PARENT_UNTRUSTED)
                if data != ARCHIVES_MARKER_TEXT:
                    _fail(ARCHIVE_PARENT_UNTRUSTED)
        finally:
            os.close(fd)
        return
    directory, name = split_leaf(cleaned)
    try:
        parent, _chain = open_dir_abs(directory)
    except GatherStoreError as error:
        _wrap_store(error)
    try:
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
        except FileExistsError:
            pass
        except OSError:
            _fail(ARCHIVE_PARENT_UNTRUSTED)
        child = None
        try:
            child = open_child_dir(parent, name)
            os.fchmod(child, 0o700)
            info = os.fstat(child)
            if not owner_only_dir(info, os.geteuid()):
                _fail(ARCHIVE_PARENT_UNTRUSTED)
        finally:
            if child is not None:
                os.close(child)
    finally:
        os.close(parent)
    _publish_archives_marker(posixpath.join(cleaned, ARCHIVES_MARKER_NAME))


def _ensure_archive_root(parent: str, archive_id: str) -> str:
    root = posixpath.join(parent, archive_id)
    if posixpath.lexists(root):
        try:
            fd, _chain = open_dir_abs(root)
        except GatherStoreError:
            _fail(ARCHIVE_PARENT_UNTRUSTED)
        try:
            info = os.fstat(fd)
            if not owner_only_dir(info, os.geteuid()):
                _fail(ARCHIVE_PARENT_UNTRUSTED)
        finally:
            os.close(fd)
        return root
    directory, name = split_leaf(root)
    try:
        pfd, _chain = open_dir_abs(directory)
    except GatherStoreError as error:
        _wrap_store(error)
    try:
        try:
            os.mkdir(name, 0o700, dir_fd=pfd)
        except OSError:
            _fail(ARCHIVE_PARENT_UNTRUSTED)
        child = open_child_dir(pfd, name)
        try:
            os.fchmod(child, 0o700)
            info = os.fstat(child)
            if not owner_only_dir(info, os.geteuid()):
                _fail(ARCHIVE_PARENT_UNTRUSTED)
        finally:
            os.close(child)
    finally:
        os.close(pfd)
    return root


def _read_existing_regular(path: str) -> bytes | None:
    """Descriptor no-follow owner-checked read. Returns None instead of following a symlink."""
    try:
        directory, name = split_leaf(path)
        parent, _chain = open_dir_abs(directory)
    except GatherStoreError:
        return None
    fd = None
    try:
        fd, info = open_child_file(parent, name)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            return None
        return pread_all(fd, info.st_size, ADAPTER_MAX_BYTES)
    except GatherStoreError:
        return None
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.close(parent)
        except OSError:
            pass


def _batch_digests(archive_root: str, batch_id: str) -> tuple[str, str, str] | None:
    batch_dir = posixpath.join(archive_root, "batches", batch_id)
    manifest_path = posixpath.join(batch_dir, "manifest.json")
    records_path = posixpath.join(batch_dir, "records.ndjson")
    records = _read_existing_regular(records_path)
    manifest_raw = _read_existing_regular(manifest_path)
    if records is None or manifest_raw is None:
        return None
    try:
        manifest = loads_bytes(manifest_raw)
    except StrictJSONError:
        return None
    if not isinstance(manifest, dict):
        return None
    records_digest = SHA256_PREFIX + hashlib.sha256(records).hexdigest()
    sources = manifest.get("sources")
    if not isinstance(sources, list) or not sources:
        return None
    source_item = sources[0]
    if not isinstance(source_item, dict):
        return None
    rel = source_item.get("path")
    source_digest = source_item.get("byte_digest")
    if not isinstance(rel, str) or not isinstance(source_digest, str):
        return None
    parts = rel.split("/")
    if ".." in parts or rel.startswith("/") or not parts:
        return None
    source_path = posixpath.join(archive_root, rel)
    source_bytes = _read_existing_regular(source_path)
    if source_bytes is None:
        return None
    if SHA256_PREFIX + hashlib.sha256(source_bytes).hexdigest() != source_digest:
        return None
    return records_digest, source_digest, rel


def _list_tree(root: str) -> set[str]:
    found: set[str] = set()
    if not posixpath.lexists(root) or posixpath.islink(root):
        return found
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        keep: list[str] = []
        for name in dirnames:
            path = posixpath.join(dirpath, name)
            rel = posixpath.relpath(path, root)
            found.add(rel)
            if not posixpath.islink(path):
                keep.append(name)
        dirnames[:] = keep
        for name in filenames:
            path = posixpath.join(dirpath, name)
            found.add(posixpath.relpath(path, root))
    return found


def _cleanup_created_paths(archive_root: str, created: set[str], created_root: bool) -> None:
    """Remove only paths created by the current import. Never delete prior shared refs."""
    for rel in sorted(created, key=lambda item: item.count("/"), reverse=True):
        path = posixpath.join(archive_root, rel)
        if posixpath.islink(path) or posixpath.isfile(path):
            try:
                os.unlink(path)
            except OSError:
                pass
            continue
        if posixpath.isdir(path) and not posixpath.islink(path):
            try:
                os.rmdir(path)
            except OSError:
                pass
    if created_root:
        try:
            os.rmdir(archive_root)
        except OSError:
            pass


@contextmanager
def _exclusive_archives_parent(path: str) -> Iterator[None]:
    """Exclusive marker-file flock for cooperating apply writers.

    The archives parent directory is not flocked here. replace_publish
    acquires a transient LOCK_EX on that directory during ledger exchange;
    a same-process directory lock on a second fd would yield STORE_BUSY.
    """
    parent = None
    fd = None
    try:
        parent, _chain = open_dir_abs(path)
        info = os.fstat(parent)
        if not owner_only_dir(info, os.geteuid()):
            _fail(ARCHIVE_PARENT_UNTRUSTED)
        fd, minfo = open_child_file(parent, ARCHIVES_MARKER_NAME)
        if not owner_only_file(minfo, os.geteuid()):
            _fail(ARCHIVE_PARENT_UNTRUSTED)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in {errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK}:
                _fail(STORE_BUSY, 6)
            _fail(ARCHIVE_PARENT_UNTRUSTED)
        yield
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if parent is not None:
            try:
                os.close(parent)
            except OSError:
                pass


def _unchanged_match(entry: Mapping[str, Any], archive_id: str, batch_id: str, scope: str,
                     source_sha: str, archive_root: str) -> bool:
    if entry.get("archive_id") != archive_id or entry.get("batch_id") != batch_id:
        return False
    if entry.get("source_scope_id") != scope:
        return False
    if entry.get("source_sha256") != source_sha:
        return False
    try:
        validate_archive(Path(archive_root))
    except ValidationError:
        return False
    found = _batch_digests(archive_root, batch_id)
    if found is None:
        return False
    records_digest, source_digest, _rel = found
    if records_digest != entry.get("records_sha256") or source_digest != source_sha:
        return False
    return True


def _import_one(
    store: GatherStore,
    receipt: Receipt,
    scope: str,
    archives_parent: str,
    staging_parent: str,
) -> tuple[str, dict[str, str] | None]:
    if receipt.adapter not in IMPORTABLE_ADAPTERS or not receipt.importer_available:
        return IMPORTER_UNAVAILABLE, None
    if receipt.byte_count > ADAPTER_MAX_BYTES:
        return ADAPTER_SIZE_LIMIT, None
    archive_id = archive_id_for(receipt.adapter, scope)
    batch_id = receipt.receipt_hex
    source_sha = receipt.object_sha256
    archive_root = posixpath.join(archives_parent, archive_id)
    created_root = not posixpath.lexists(archive_root)
    if not created_root:
        try:
            validate_archive(Path(archive_root))
        except ValidationError:
            return IMPORT_CONFLICT, None
    staging_dir = posixpath.join(staging_parent, secrets.token_hex(8))
    os.mkdir(staging_dir, 0o700)
    os.chmod(staging_dir, 0o700)
    before: set[str] = set()
    mutated = False
    try:
        object_path = store.materialize_object(receipt.object_digest_hex, staging_dir, name="object")
        archive_path = Path(_ensure_archive_root(archives_parent, archive_id))
        archive_root = str(archive_path)
        mutated = True
        before = _list_tree(archive_root)
        create_adapter(receipt.adapter, scope).conform(
            Path(object_path), archive_path, archive_id=archive_id, batch_id=batch_id)
        validate_archive(archive_path)
        found = _batch_digests(archive_root, batch_id)
        if found is None:
            _cleanup_created_paths(archive_root, _list_tree(archive_root) - before, created_root)
            return IMPORT_CONFLICT, None
        records_digest, source_digest, _rel = found
        if source_digest != source_sha:
            _cleanup_created_paths(archive_root, _list_tree(archive_root) - before, created_root)
            return IMPORT_CONFLICT, None
        return "ADDED", {
            "receipt_id": receipt.receipt_id,
            "adapter": receipt.adapter,
            "source_scope_id": scope,
            "archive_id": archive_id,
            "batch_id": batch_id,
            "records_sha256": records_digest,
            "source_sha256": source_digest,
        }
    except GatherStoreError as error:
        if mutated:
            _cleanup_created_paths(archive_root, _list_tree(archive_root) - before, created_root)
        if error.code == PARTIAL_OR_DEFERRED:
            return ADAPTER_SIZE_LIMIT, None
        raise
    except (AdapterInputError, ValidationError, ValueError) as error:
        if mutated:
            _cleanup_created_paths(archive_root, _list_tree(archive_root) - before, created_root)
        text = str(error)
        if text == "SOURCE_OBJECT_COLLISION" or text == "BATCH_EXISTS":
            return IMPORT_CONFLICT, None
        return ADAPTER_IMPORT_REJECTED, None
    except Exception:
        if mutated:
            _cleanup_created_paths(archive_root, _list_tree(archive_root) - before, created_root)
        raise
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)


def _apply(
    store: GatherStore,
    selected: list[Receipt],
    plan: dict[str, Any] | None,
    archives_parent: str,
    details_rows: list[dict[str, Any]],
) -> tuple[dict[str, int], dict[str, int], str, bool]:
    counts = _empty_counts()
    counts["selected"] = len(selected)
    by_adapter: dict[str, int] = {}
    plan_index: dict[str, dict[str, Any]] = {}
    if plan is not None:
        for row in plan["rows"]:
            plan_index[row["receipt_id"]] = row
    ledger_path = posixpath.join(archives_parent, LEDGER_NAME)
    ledger = _load_ledger(ledger_path)
    ledger_index = {item["receipt_id"]: item for item in ledger["entries"]}
    initial_entries = len(ledger["entries"])
    staging_parent = posixpath.join(posixpath.dirname(archives_parent), ".oca-bridge-stage-" + secrets.token_hex(8))
    os.mkdir(staging_parent, 0o700)
    os.chmod(staging_parent, 0o700)
    outcome = OK
    complete = True
    try:
        _overlap_or_fail(staging_parent, True, store.path, True)
        selected_ids = {receipt.receipt_id for receipt in selected}
        plan_ids = set(plan_index)
        plan_mismatch = plan_ids != selected_ids or any(
            not _plan_row_matches(receipt, plan_index.get(receipt.receipt_id))
            for receipt in selected
        )
        if plan_mismatch:
            complete = False
            outcome = SCOPE_PLAN_CONFLICT
            for receipt in selected:
                by_adapter[receipt.adapter] = by_adapter.get(receipt.adapter, 0) + 1
                counts["conflict"] += 1
                details_rows.append({
                    "adapter": receipt.adapter,
                    "scope_state": receipt.scope_state,
                    "code": SCOPE_PLAN_CONFLICT,
                })
            extra_plan = len(plan_ids - selected_ids)
            if extra_plan:
                counts["conflict"] += extra_plan
            return counts, by_adapter, outcome, complete
        for receipt in selected:
            by_adapter[receipt.adapter] = by_adapter.get(receipt.adapter, 0) + 1
            row = {
                "adapter": receipt.adapter,
                "scope_state": receipt.scope_state,
                "code": OK,
            }
            try:
                if receipt.adapter not in IMPORTABLE_ADAPTERS or not receipt.importer_available:
                    counts["importer_unavailable"] += 1
                    complete = False
                    outcome = IMPORTER_UNAVAILABLE if outcome == OK else outcome
                    row["code"] = IMPORTER_UNAVAILABLE
                    details_rows.append(row)
                    continue
                scope = resolve_scope(receipt, plan_index.get(receipt.receipt_id))
                archive_id = archive_id_for(receipt.adapter, scope)
                batch_id = receipt.receipt_hex
                existing = ledger_index.get(receipt.receipt_id)
                archive_root = posixpath.join(archives_parent, archive_id)
                if existing is not None:
                    if existing.get("archive_id") != archive_id or existing.get("source_scope_id") != scope:
                        counts["conflict"] += 1
                        complete = False
                        outcome = IMPORT_CONFLICT
                        row["code"] = IMPORT_CONFLICT
                        details_rows.append(row)
                        continue
                    if _unchanged_match(existing, archive_id, batch_id, scope, receipt.object_sha256, archive_root):
                        counts["unchanged"] += 1
                        row["code"] = "UNCHANGED"
                        details_rows.append(row)
                        continue
                    counts["conflict"] += 1
                    complete = False
                    outcome = IMPORT_CONFLICT
                    row["code"] = IMPORT_CONFLICT
                    details_rows.append(row)
                    continue
                if posixpath.lexists(posixpath.join(archive_root, "batches", batch_id)):
                    counts["conflict"] += 1
                    complete = False
                    outcome = IMPORT_CONFLICT
                    row["code"] = IMPORT_CONFLICT
                    details_rows.append(row)
                    continue
                code, entry = _import_one(store, receipt, scope, archives_parent, staging_parent)
                if code == "ADDED" and entry is not None:
                    counts["added"] += 1
                    ledger["entries"].append(entry)
                    ledger_index[entry["receipt_id"]] = entry
                    row["code"] = "ADDED"
                    details_rows.append(row)
                    continue
                if code == IMPORTER_UNAVAILABLE:
                    counts["importer_unavailable"] += 1
                    complete = False
                    outcome = IMPORTER_UNAVAILABLE if outcome == OK else outcome
                elif code == ADAPTER_SIZE_LIMIT:
                    counts["limit_hits"] += 1
                    counts["deferred"] += 1
                    complete = False
                    outcome = PARTIAL_OR_DEFERRED if outcome == OK else outcome
                elif code == IMPORT_CONFLICT:
                    counts["conflict"] += 1
                    complete = False
                    outcome = IMPORT_CONFLICT
                else:
                    counts["deferred"] += 1
                    complete = False
                    outcome = code if outcome == OK else outcome
                row["code"] = code
                details_rows.append(row)
            except GatherImportError as error:
                if error.code == SCOPE_MISSING:
                    counts["scope_missing"] += 1
                    complete = False
                    if outcome == OK:
                        outcome = SCOPE_MISSING
                    row["code"] = SCOPE_MISSING
                    details_rows.append(row)
                    continue
                if error.code == SCOPE_PLAN_CONFLICT:
                    counts["conflict"] += 1
                    complete = False
                    outcome = SCOPE_PLAN_CONFLICT
                    row["code"] = SCOPE_PLAN_CONFLICT
                    details_rows.append(row)
                    continue
                raise
        if len(ledger["entries"]) != initial_entries:
            ledger["entries"].sort(key=lambda item: item["receipt_id"])
            replace_publish(ledger_path, canonical_object_bytes(ledger))
        touched = {posixpath.join(archives_parent, item["archive_id"]) for item in ledger["entries"]}
        for archive_root in sorted(touched):
            if posixpath.isdir(archive_root):
                try:
                    validate_archive(Path(archive_root))
                except ValidationError:
                    counts["conflict"] += 1
                    complete = False
                    outcome = IMPORT_CONFLICT
    finally:
        shutil.rmtree(staging_parent, ignore_errors=True)
    return counts, by_adapter, outcome, complete


class _QuietParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        _fail(ARGUMENT_INVALID, 2)


def build_parser() -> argparse.ArgumentParser:
    parser = _QuietParser(prog="open-chat-archive-gather", add_help=True)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("verify", "plan", "apply"):
        item = sub.add_parser(name, add_help=True)
        item.add_argument("--store", required=True)
        item.add_argument("--plan")
        item.add_argument("--archives-parent")
        item.add_argument("--receipt-id", action="append", default=[])
        item.add_argument("--adapter", choices=sorted(IMPORTABLE_ADAPTERS))
        item.add_argument("--details-output")
    return parser


def _exit_for(code: str, complete: bool, counts: Mapping[str, int]) -> int:
    if code == ARGUMENT_INVALID:
        return 2
    if code == GATHER_PLATFORM_UNSUPPORTED:
        return 5
    if code == STORE_BUSY:
        return 6
    if code in {
        STORE_UNTRUSTED, STORE_OBJECT_CORRUPT, IMPORT_CONFLICT, SCOPE_PLAN_CONFLICT,
        PLAN_UNTRUSTED, ARCHIVE_PARENT_UNTRUSTED, OUTPUT_UNTRUSTED,
    }:
        return 4
    if not complete or counts.get("conflict", 0):
        if counts.get("conflict", 0) or code in {IMPORT_CONFLICT, SCOPE_PLAN_CONFLICT}:
            return 4
        return 8
    return 0


def run(argv: list[str] | None = None) -> tuple[dict[str, Any], int]:
    args = build_parser().parse_args(argv)
    store_path = _require_abs(args.store)
    details_path = _require_abs(args.details_output) if args.details_output else None
    receipt_ids = set(args.receipt_id or [])
    for item in receipt_ids:
        if sha_hex_prefixed(item) is None and not is_hex64(item):
            _fail(ARGUMENT_INVALID, 2)
    adapter = args.adapter
    details_rows: list[dict[str, Any]] = []
    extra: dict[str, Any] = {}
    try:
        with locked_store(store_path) as store:
            status: StoreStatus = store.status
            if not status.complete:
                body = _aggregate(
                    args.command, complete=False, code=PARTIAL_OR_DEFERRED,
                    counts=_empty_counts(), by_adapter=status.by_adapter,
                    extra={"store_state": status.store_state},
                )
                return body, 8
            selected = _select_receipts(store.receipts, receipt_ids=receipt_ids or None, adapter=adapter)
            by_adapter = {}
            for receipt in selected:
                by_adapter[receipt.adapter] = by_adapter.get(receipt.adapter, 0) + 1
            counts = _empty_counts()
            counts["selected"] = len(selected)
            if args.command == "verify":
                for receipt in selected:
                    if receipt.adapter not in IMPORTABLE_ADAPTERS or not receipt.importer_available:
                        counts["importer_unavailable"] += 1
                    elif receipt.scope_state == "missing":
                        counts["scope_missing"] += 1
                    details_rows.append({
                        "adapter": receipt.adapter,
                        "scope_state": receipt.scope_state,
                        "code": OK,
                    })
                extra = {
                    "store_state": status.store_state,
                    "objects": status.objects,
                    "receipts": status.receipts,
                }
                complete = True
                code = OK
                body = _aggregate("verify", complete=complete, code=code, counts=counts,
                                  by_adapter=dict(status.by_adapter), extra=extra)
            elif args.command == "plan":
                if not args.plan:
                    _fail(ARGUMENT_INVALID, 2)
                plan_path = _require_abs(args.plan)
                _overlap_or_fail(plan_path, False, store_path, True)
                if details_path:
                    _overlap_or_fail(details_path, False, store_path, True)
                    _overlap_or_fail(details_path, False, plan_path, False)
                document = plan_document(tuple(selected))
                try:
                    exclusive_publish(
                        plan_path, canonical_object_bytes(document),
                        exist_code=PLAN_EXISTS, exist_exit=2)
                except GatherStoreError as error:
                    if error.code == OUTPUT_EXISTS:
                        _fail(PLAN_EXISTS, 2)
                    _wrap_store(error)
                body = _aggregate("plan", complete=True, code=OK, counts=counts, by_adapter=by_adapter)
            else:
                if not args.plan or not args.archives_parent:
                    _fail(ARGUMENT_INVALID, 2)
                plan_path = _require_abs(args.plan)
                archives_parent = _require_abs(args.archives_parent)
                _overlap_or_fail(plan_path, False, store_path, True)
                _overlap_or_fail(plan_path, False, archives_parent, True)
                _overlap_or_fail(archives_parent, True, store_path, True)
                if details_path:
                    _overlap_or_fail(details_path, False, store_path, True)
                    _overlap_or_fail(details_path, False, archives_parent, True)
                    _overlap_or_fail(details_path, False, plan_path, False)
                plan = _load_plan(plan_path)
                _ensure_archives_parent(archives_parent, store_path)
                with _exclusive_archives_parent(archives_parent):
                    counts, by_adapter, outcome, complete = _apply(
                        store, selected, plan, archives_parent, details_rows)
                extra = {"store_state": status.store_state}
                body = _aggregate("apply", complete=complete, code=outcome, counts=counts,
                                  by_adapter=by_adapter, extra=extra)
            if details_path:
                if args.command == "verify":
                    _overlap_or_fail(details_path, False, store_path, True)
                try:
                    _write_details(details_path, details_rows)
                except GatherStoreError as error:
                    if error.code == OUTPUT_EXISTS:
                        _fail(OUTPUT_EXISTS, 2)
                    _wrap_store(error)
            return body, _exit_for(body["code"], body["complete"], body["counts"])
    except GatherStoreError as error:
        _wrap_store(error)


def main(argv: list[str] | None = None) -> int:
    try:
        body, code = run(list(sys.argv[1:] if argv is None else argv))
    except GatherImportError as error:
        body = _aggregate(
            "error", complete=False, code=error.code, counts=_empty_counts(), by_adapter={})
        # Preserve the attempted operation name when argv is available.
        args = list(sys.argv[1:] if argv is None else argv)
        if args and args[0] in {"verify", "plan", "apply"}:
            body["operation"] = args[0]
        _emit(body)
        return error.exit_code
    _emit(body)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
