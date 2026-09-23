"""Offline strict reader for oca-gather-store/v0.1-experimental.

Store I/O walks absolute paths from ``/`` with descriptor-relative, no-follow
opens. Linux amd64/arm64 uses libc ``syscall(SYS_openat2)`` with
``RESOLVE_BENEATH|NO_SYMLINKS|NO_MAGICLINKS``. Darwin uses ``openat`` plus
``O_NOFOLLOW``. Missing primitives fail closed as ``GATHER_PLATFORM_UNSUPPORTED``.
Origin paths from receipts are never opened.
"""
from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import os
import posixpath
import re
import stat
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Iterator, Mapping, NoReturn

from .strictjson import StrictJSONError, loads_bytes


ARGUMENT_INVALID = "ARGUMENT_INVALID"
STORE_UNTRUSTED = "STORE_UNTRUSTED"
STORE_OBJECT_CORRUPT = "STORE_OBJECT_CORRUPT"
GATHER_PLATFORM_UNSUPPORTED = "GATHER_PLATFORM_UNSUPPORTED"
STORE_BUSY = "STORE_BUSY"
PARTIAL_OR_DEFERRED = "PARTIAL_OR_DEFERRED"
SOURCE_STORE_OVERLAP = "SOURCE_STORE_OVERLAP"
OUTPUT_EXISTS = "OUTPUT_EXISTS"
OUTPUT_UNTRUSTED = "OUTPUT_UNTRUSTED"

MARKER_NAME = ".oca-gather-store"
MARKER_TEXT = b"oca-gather-store/v0.1-experimental\n"
RECEIPT_FORMAT = "oca-gather-receipt/v0.1-experimental"
RECEIPT_KEY_PREFIX = "oca-gather-receipt-key/v1"
ADAPTERS = ("claude-code", "codex", "grok-build", "pi")
IMPORTABLE_ADAPTERS = frozenset({"claude-code", "codex", "grok-build"})
SCOPE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SHA256_PREFIX = "sha256:"
OPAQUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

DEFAULT_MAX_FILES = 10_000
HARD_MAX_FILES = 50_000
DEFAULT_MAX_ENTRIES = 20_000
HARD_MAX_ENTRIES = 100_000
DEFAULT_MAX_DEPTH = 8
HARD_MAX_DEPTH = 32
DEFAULT_MAX_FILE_BYTES = 64 << 20
HARD_MAX_FILE_BYTES = 256 << 20
DEFAULT_MAX_TOTAL_BYTES = 512 << 20
HARD_MAX_TOTAL_BYTES = 2 << 30
DEFAULT_PROBE_BYTES = 256 << 10
HARD_PROBE_BYTES = 1 << 20
DEFAULT_PROBE_LINES = 32
HARD_PROBE_LINES = 128
DEFAULT_MAX_STATUS_RECEIPTS = 10_000
HARD_MAX_STATUS_RECEIPTS = 100_000
DEFAULT_RECEIPT_BYTES = 64 << 10
HARD_RECEIPT_BYTES = 256 << 10
DEFAULT_MAX_STATUS_BYTES = 512 << 20
HARD_MAX_STATUS_BYTES = 2 << 30
MAX_COMPONENT_BYTES = 255
ADAPTER_MAX_BYTES = 64 << 20
SYS_OPENAT2 = 437
RESOLVE_NO_MAGICLINKS = 0x02
RESOLVE_NO_SYMLINKS = 0x04
RESOLVE_BENEATH = 0x08
RESOLVE_FLAGS = RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS
RENAME_NOREPLACE = 1
RENAME_EXCHANGE = 2
DARWIN_RENAME_SWAP = 0x2
DARWIN_RENAME_EXCL = 0x4
F_FULLFSYNC = 51
_DAYS_IN_MONTH = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)

RECEIPT_KEYS = (
    "format", "receipt_id", "collector_version", "acquired_at", "adapter",
    "detection_mode", "validation_state", "origin_path", "source_stat",
    "byte_count", "object_sha256", "object_ref", "scope_state",
    "source_scope_id", "import_status", "importer_available",
)
SOURCE_STAT_KEYS = ("ctime_ns", "dev", "ino", "mtime_ns", "size")
UINT64_MAX = (1 << 64) - 1
INT64_MIN = -(1 << 63)
INT64_MAX = (1 << 63) - 1

_LINUX = sys.platform.startswith("linux")
_DARWIN = sys.platform == "darwin"
_platform_ready = False
_libc_syscall = None
_linux_libc = None
_darwin_lib = None
_OpenHow = None
_darwin_dir_fsync_unsupported = False


class GatherStoreError(Exception):
    """Fixed-code store failure. ``str(error)`` is only the code."""

    def __init__(self, code: str, exit_code: int = 4) -> None:
        self.code = code
        self.exit_code = exit_code
        super().__init__(code)

    def __str__(self) -> str:
        return self.code

    def __repr__(self) -> str:
        return f"GatherStoreError({self.code!r}, {self.exit_code})"


def _fail(code: str, exit_code: int = 4) -> NoReturn:
    raise GatherStoreError(code, exit_code)


def _utf8_valid(text: str) -> bool:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def component_ok(name: str) -> bool:
    if not name or name in {".", ".."}:
        return False
    if len(name.encode("utf-8", "surrogateescape")) > MAX_COMPONENT_BYTES:
        return False
    if "/" in name or "\x00" in name:
        return False
    return _utf8_valid(name)


def is_hex64(value: str) -> bool:
    if len(value) != 64:
        return False
    return all(c in "0123456789abcdef" for c in value)


def sha_hex_prefixed(value: str) -> str | None:
    if not isinstance(value, str) or not value.startswith(SHA256_PREFIX):
        return None
    digest = value[len(SHA256_PREFIX):]
    return digest if is_hex64(digest) else None


def valid_scope(value: str) -> bool:
    return bool(isinstance(value, str) and SCOPE_PATTERN.fullmatch(value))


def valid_named_adapter(value: str) -> bool:
    return value in ADAPTERS


def importer_available(adapter: str) -> bool:
    return adapter != "pi"


def scope_state_for(scope: str) -> str:
    return "missing" if scope == "" else "operator_supplied_unverified"


def receipt_key(adapter: str, state: str, scope: str, origin: str, digest_hex: str) -> str:
    payload = "\x00".join((RECEIPT_KEY_PREFIX, adapter, state, scope, origin, digest_hex))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def archive_id_for(adapter: str, source_scope_id: str) -> str:
    payload = "\x00".join(("oca-bridge-archive-id/v1", adapter, source_scope_id))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _mapped_exit(code: str) -> int:
    if code == ARGUMENT_INVALID:
        return 2
    if code == GATHER_PLATFORM_UNSUPPORTED:
        return 5
    if code == STORE_BUSY:
        return 6
    if code == PARTIAL_OR_DEFERRED:
        return 8
    return 4


def go_json(value: Any, *, invalid: str = STORE_UNTRUSTED) -> bytes:
    """Go ``encoding/json`` Encoder with ``SetEscapeHTML(false)`` plus one LF.

    U+007F and ``<>&`` stay raw; U+2028/U+2029 and C0 controls use ``\\u`` escapes.
    Encode/surrogate failures raise ``invalid`` (ARGUMENT_INVALID or STORE_UNTRUSTED).
    """
    chunks: list[str] = []
    try:
        _encode_go(value, chunks, invalid)
        return ("".join(chunks) + "\n").encode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError, UnicodeError):
        _fail(invalid, _mapped_exit(invalid))


def _encode_go(value: Any, chunks: list[str], invalid: str) -> None:
    if value is None:
        chunks.append("null")
        return
    if value is True:
        chunks.append("true")
        return
    if value is False:
        chunks.append("false")
        return
    if isinstance(value, int) and not isinstance(value, bool):
        chunks.append(str(value))
        return
    if isinstance(value, str):
        chunks.append(_go_string(value, invalid))
        return
    if isinstance(value, dict):
        chunks.append("{")
        first = True
        for key in sorted(value):
            if not isinstance(key, str):
                _fail(invalid, _mapped_exit(invalid))
            if not first:
                chunks.append(",")
            first = False
            chunks.append(_go_string(key, invalid))
            chunks.append(":")
            _encode_go(value[key], chunks, invalid)
        chunks.append("}")
        return
    if isinstance(value, (list, tuple)):
        chunks.append("[")
        for index, item in enumerate(value):
            if index:
                chunks.append(",")
            _encode_go(item, chunks, invalid)
        chunks.append("]")
        return
    _fail(invalid, _mapped_exit(invalid))


def _go_string(text: str, invalid: str) -> str:
    out = ['"']
    for char in text:
        code = ord(char)
        if 0xD800 <= code <= 0xDFFF:
            _fail(invalid, _mapped_exit(invalid))
        if char == '"':
            out.append('\\"')
        elif char == "\\":
            out.append("\\\\")
        elif char == "\b":
            out.append("\\b")
        elif char == "\f":
            out.append("\\f")
        elif char == "\n":
            out.append("\\n")
        elif char == "\r":
            out.append("\\r")
        elif char == "\t":
            out.append("\\t")
        elif code < 0x20:
            out.append(f"\\u{code:04x}")
        elif code == 0x2028:
            out.append("\\u2028")
        elif code == 0x2029:
            out.append("\\u2029")
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


def canonical_object_bytes(value: Any) -> bytes:
    return go_json(value)


def _uint64(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > UINT64_MAX:
        _fail(STORE_UNTRUSTED)
    return value


def _int64(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < INT64_MIN or value > INT64_MAX:
        _fail(STORE_UNTRUSTED)
    return value


def _text(value: Any) -> str:
    if not isinstance(value, str):
        _fail(STORE_UNTRUSTED)
    return value


def _is_leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def _days_in(month: int, year: int) -> int:
    if month == 2 and _is_leap(year):
        return 29
    if month < 1 or month > 12:
        return 0
    return _DAYS_IN_MONTH[month - 1]


def _rfc3339(value: str) -> bool:
    """Accept the same calendar/offset/fraction strings as Go ``time.Parse(time.RFC3339)``."""
    if not isinstance(value, str):
        return False
    n = len(value)
    if n < 17:
        return False
    if not value[0:4].isdigit() or value[4] != "-" or not value[5:7].isdigit():
        return False
    if value[7] != "-" or not value[8:10].isdigit() or value[10] != "T":
        return False
    year = int(value[0:4])
    month = int(value[5:7])
    day = int(value[8:10])
    if month < 1 or month > 12 or day < 1 or day > _days_in(month, year):
        return False
    i = 11
    if i >= n or not value[i].isdigit():
        return False
    if i + 1 < n and value[i + 1].isdigit():
        hour = int(value[i:i + 2])
        i += 2
    else:
        hour = int(value[i])
        i += 1
    if hour > 23:
        return False
    if i >= n or value[i] != ":":
        return False
    i += 1
    if i + 2 > n or not value[i:i + 2].isdigit():
        return False
    minute = int(value[i:i + 2])
    i += 2
    if minute > 59:
        return False
    if i >= n or value[i] != ":":
        return False
    i += 1
    if i + 2 > n or not value[i:i + 2].isdigit():
        return False
    second = int(value[i:i + 2])
    i += 2
    if second > 59:
        return False
    if i < n and value[i] in ".,":
        i += 1
        start = i
        while i < n and value[i].isdigit():
            i += 1
        if i == start:
            return False
    if i >= n:
        return False
    if value[i] == "Z":
        return i + 1 == n
    if value[i] not in "+-":
        return False
    i += 1
    if i + 5 > n:
        return False
    if not value[i:i + 2].isdigit() or value[i + 2] != ":" or not value[i + 3:i + 5].isdigit():
        return False
    return i + 5 == n


def marshal_receipt_map(body: Mapping[str, Any], *, invalid: str = STORE_UNTRUSTED) -> bytes:
    scope = body.get("source_scope_id")
    source_stat = body["source_stat"]
    payload = {
        "format": body["format"],
        "receipt_id": body["receipt_id"],
        "collector_version": body["collector_version"],
        "acquired_at": body["acquired_at"],
        "adapter": body["adapter"],
        "detection_mode": body["detection_mode"],
        "validation_state": body["validation_state"],
        "origin_path": body["origin_path"],
        "source_stat": {
            "dev": source_stat["dev"],
            "ino": source_stat["ino"],
            "size": source_stat["size"],
            "mtime_ns": source_stat["mtime_ns"],
            "ctime_ns": source_stat["ctime_ns"],
        },
        "byte_count": body["byte_count"],
        "object_sha256": body["object_sha256"],
        "object_ref": body["object_ref"],
        "scope_state": body["scope_state"],
        "source_scope_id": None if scope is None else scope,
        "import_status": body["import_status"],
        "importer_available": body["importer_available"],
    }
    return go_json(payload, invalid=invalid)


def parse_receipt_strict(data: bytes) -> dict[str, Any]:
    if not data or data[-1:] != b"\n" or data.count(b"\n") != 1:
        _fail(STORE_UNTRUSTED)
    if len(data) > DEFAULT_RECEIPT_BYTES:
        _fail(STORE_UNTRUSTED)
    try:
        raw = loads_bytes(data)
    except StrictJSONError:
        _fail(STORE_UNTRUSTED)
    if not isinstance(raw, dict) or set(raw) != set(RECEIPT_KEYS):
        _fail(STORE_UNTRUSTED)
    source_stat = raw["source_stat"]
    if not isinstance(source_stat, dict) or set(source_stat) != set(SOURCE_STAT_KEYS):
        _fail(STORE_UNTRUSTED)
    adapter = _text(raw["adapter"])
    format_name = _text(raw["format"])
    receipt_id = _text(raw["receipt_id"])
    collector_version = _text(raw["collector_version"])
    acquired_at = _text(raw["acquired_at"])
    detection_mode = _text(raw["detection_mode"])
    validation_state = _text(raw["validation_state"])
    origin_path = _text(raw["origin_path"])
    object_sha256 = _text(raw["object_sha256"])
    object_ref = _text(raw["object_ref"])
    scope_state = _text(raw["scope_state"])
    import_status = _text(raw["import_status"])
    if not isinstance(raw["importer_available"], bool):
        _fail(STORE_UNTRUSTED)
    importer = raw["importer_available"]
    byte_count = _uint64(raw["byte_count"])
    parsed_stat = {
        "dev": _uint64(source_stat["dev"]),
        "ino": _uint64(source_stat["ino"]),
        "size": _uint64(source_stat["size"]),
        "mtime_ns": _int64(source_stat["mtime_ns"]),
        "ctime_ns": _int64(source_stat["ctime_ns"]),
    }
    scope = raw["source_scope_id"]
    if format_name != RECEIPT_FORMAT or detection_mode != "signature" or \
            validation_state != "not_validated" or import_status != "not_attempted" or \
            collector_version == "":
        _fail(STORE_UNTRUSTED)
    if not valid_named_adapter(adapter):
        _fail(STORE_UNTRUSTED)
    if importer != importer_available(adapter):
        _fail(STORE_UNTRUSTED)
    if scope_state not in {"missing", "operator_supplied_unverified"}:
        _fail(STORE_UNTRUSTED)
    if scope_state == "missing":
        if scope is not None:
            _fail(STORE_UNTRUSTED)
        scope_value = ""
    else:
        if not isinstance(scope, str) or not valid_scope(scope):
            _fail(STORE_UNTRUSTED)
        scope_value = scope
    if not _utf8_valid(origin_path) or not origin_path.startswith("/") or "\x00" in origin_path:
        _fail(STORE_UNTRUSTED)
    if not _rfc3339(acquired_at):
        _fail(STORE_UNTRUSTED)
    digest = sha_hex_prefixed(object_sha256)
    if digest is None or object_ref != "objects/sha256/" + digest:
        _fail(STORE_UNTRUSTED)
    rid = sha_hex_prefixed(receipt_id)
    if rid is None:
        _fail(STORE_UNTRUSTED)
    if receipt_key(adapter, scope_state, scope_value, origin_path, digest) != rid:
        _fail(STORE_UNTRUSTED)
    if parsed_stat["size"] != byte_count:
        _fail(STORE_UNTRUSTED)
    body = {
        "format": format_name,
        "receipt_id": receipt_id,
        "collector_version": collector_version,
        "acquired_at": acquired_at,
        "adapter": adapter,
        "detection_mode": detection_mode,
        "validation_state": validation_state,
        "origin_path": origin_path,
        "source_stat": parsed_stat,
        "byte_count": byte_count,
        "object_sha256": object_sha256,
        "object_ref": object_ref,
        "scope_state": scope_state,
        "source_scope_id": None if scope is None else scope,
        "import_status": import_status,
        "importer_available": importer,
    }
    canon = marshal_receipt_map(body)
    if canon != data:
        _fail(STORE_UNTRUSTED)
    return body


def build_receipt(
    *,
    adapter: str,
    origin: str,
    digest_hex: str,
    scope: str,
    source_stat: Mapping[str, int],
    byte_count: int,
    acquired_at: str,
    collector_version: str = "0.1-experimental",
) -> tuple[str, bytes]:
    if "\x00" in origin or "\x00" in scope or not _utf8_valid(origin):
        _fail(ARGUMENT_INVALID, 2)
    state = scope_state_for(scope)
    rid = receipt_key(adapter, state, scope, origin, digest_hex)
    body = {
        "format": RECEIPT_FORMAT,
        "receipt_id": SHA256_PREFIX + rid,
        "collector_version": collector_version,
        "acquired_at": acquired_at,
        "adapter": adapter,
        "detection_mode": "signature",
        "validation_state": "not_validated",
        "origin_path": origin,
        "source_stat": {
            "dev": int(source_stat["dev"]),
            "ino": int(source_stat["ino"]),
            "size": int(source_stat["size"]),
            "mtime_ns": int(source_stat["mtime_ns"]),
            "ctime_ns": int(source_stat["ctime_ns"]),
        },
        "byte_count": byte_count,
        "object_sha256": SHA256_PREFIX + digest_hex,
        "object_ref": "objects/sha256/" + digest_hex,
        "scope_state": state,
        "source_scope_id": None if scope == "" else scope,
        "import_status": "not_attempted",
        "importer_available": importer_available(adapter),
    }
    return rid, marshal_receipt_map(body, invalid=ARGUMENT_INVALID)


class _OpenHowStruct(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_uint64),
        ("mode", ctypes.c_uint64),
        ("resolve", ctypes.c_uint64),
    ]


def _dir_flags() -> int:
    try:
        return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    except AttributeError:
        _fail(GATHER_PLATFORM_UNSUPPORTED, 5)


def _file_flags() -> int:
    try:
        return os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    except AttributeError:
        _fail(GATHER_PLATFORM_UNSUPPORTED, 5)


def _create_flags() -> int:
    try:
        return os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    except AttributeError:
        _fail(GATHER_PLATFORM_UNSUPPORTED, 5)


def _load_linux_libc() -> ctypes.CDLL:
    global _linux_libc
    if _linux_libc is None:
        try:
            _linux_libc = ctypes.CDLL("libc.so.6", use_errno=True)
        except OSError:
            _fail(GATHER_PLATFORM_UNSUPPORTED, 5)
    return _linux_libc


def _load_darwin_libsystem() -> ctypes.CDLL:
    global _darwin_lib
    if _darwin_lib is None:
        try:
            _darwin_lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        except OSError:
            _fail(GATHER_PLATFORM_UNSUPPORTED, 5)
    return _darwin_lib


def _oserr(err: int) -> OSError:
    return OSError(err, "")


def ensure_platform() -> None:
    global _platform_ready
    if _platform_ready:
        return
    if _LINUX:
        _ensure_linux()
    elif _DARWIN:
        _ensure_darwin()
    else:
        _fail(GATHER_PLATFORM_UNSUPPORTED, 5)
    _platform_ready = True


def _ensure_linux() -> None:
    global _libc_syscall, _OpenHow
    _dir_flags(); _file_flags(); _create_flags()
    try:
        libc = _load_linux_libc()
        syscall = libc.syscall
        syscall.restype = ctypes.c_long
        how = _OpenHowStruct(_dir_flags(), 0, RESOLVE_FLAGS)
        ctypes.set_errno(0)
        fd = syscall(
            ctypes.c_long(SYS_OPENAT2),
            ctypes.c_int(-1),
            ctypes.c_char_p(b"."),
            ctypes.byref(how),
            ctypes.c_size_t(ctypes.sizeof(_OpenHowStruct)),
        )
        err = ctypes.get_errno()
        if fd >= 0:
            os.close(int(fd))
        elif err in {errno.ENOSYS, errno.ENOTSUP, errno.EINVAL}:
            _fail(GATHER_PLATFORM_UNSUPPORTED, 5)
        try:
            root = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError:
            _fail(GATHER_PLATFORM_UNSUPPORTED, 5)
        try:
            try:
                os.fstat(root)
            except OSError:
                _fail(GATHER_PLATFORM_UNSUPPORTED, 5)
            try:
                os.fsync(root)
            except OSError as error:
                if error.errno in {errno.ENOSYS, errno.ENOTSUP}:
                    _fail(GATHER_PLATFORM_UNSUPPORTED, 5)
            try:
                _linux_renameat2(-1, "a", -1, "b", RENAME_NOREPLACE)
            except OSError as error:
                if error.errno in {errno.ENOSYS, errno.ENOTSUP}:
                    _fail(GATHER_PLATFORM_UNSUPPORTED, 5)
            try:
                probe = _openat2_raw(root, "tmp", _dir_flags())
            except OSError as error:
                if error.errno in {errno.ENOSYS, errno.ENOTSUP, errno.EINVAL}:
                    _fail(GATHER_PLATFORM_UNSUPPORTED, 5)
                probe = -1
            if probe >= 0:
                try:
                    fcntl.flock(probe, fcntl.LOCK_SH | fcntl.LOCK_NB)
                except OSError:
                    pass
                os.close(probe)
        except GatherStoreError:
            raise
        except OSError:
            _fail(GATHER_PLATFORM_UNSUPPORTED, 5)
        finally:
            os.close(root)
        _libc_syscall = syscall
        _OpenHow = _OpenHowStruct
    except GatherStoreError:
        raise
    except OSError:
        _fail(GATHER_PLATFORM_UNSUPPORTED, 5)


def _ensure_darwin() -> None:
    """Darwin capability probe. Tests mock this path; it is not native Linux execution."""
    _dir_flags(); _file_flags(); _create_flags()
    try:
        err = _darwin_probe_rename_excl()
        if err in {errno.ENOSYS, errno.ENOTSUP}:
            _fail(GATHER_PLATFORM_UNSUPPORTED, 5)
        try:
            root = os.open("/", _dir_flags())
        except OSError:
            _fail(GATHER_PLATFORM_UNSUPPORTED, 5)
        os.close(root)
        _darwin_probe_publication_capabilities()
    except GatherStoreError as error:
        if error.code == GATHER_PLATFORM_UNSUPPORTED:
            raise
        _fail(GATHER_PLATFORM_UNSUPPORTED, 5)
    except OSError:
        _fail(GATHER_PLATFORM_UNSUPPORTED, 5)


def _darwin_probe_rename_excl() -> int:
    try:
        _darwin_renameatx_np(-1, "a", -1, "b", DARWIN_RENAME_EXCL)
        return 0
    except OSError as error:
        return int(error.errno or errno.ENOSYS)


def _darwin_probe_publication_capabilities() -> None:
    """Probe descriptor open, flock, exclusive rename, and dir-fsync classification."""
    global _darwin_dir_fsync_unsupported
    probe_root = ""
    try:
        probe_root = tempfile.mkdtemp(prefix="oca-gather-probe-")
        os.chmod(probe_root, 0o700)
        dfd = os.open(probe_root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.mkdir("p", 0o700, dir_fd=dfd)
            pfd = os.open("p", _dir_flags(), dir_fd=dfd)
            try:
                fd = create_exclusive_child(pfd, "t1", 0o600)
                try:
                    _file_durable_sync(fd)
                finally:
                    os.close(fd)
                fd2 = create_exclusive_child(pfd, "t2", 0o600)
                os.close(fd2)
                try:
                    os.fsync(dfd)
                    _darwin_dir_fsync_unsupported = False
                except OSError as error:
                    if error.errno == errno.EINVAL:
                        _darwin_dir_fsync_unsupported = True
                    elif error.errno in {errno.ENOSYS, errno.ENOTSUP}:
                        _fail(GATHER_PLATFORM_UNSUPPORTED, 5)
                try:
                    _darwin_renameatx_np(pfd, "t1", pfd, "u", DARWIN_RENAME_EXCL)
                except OSError as error:
                    if error.errno in {errno.ENOSYS, errno.ENOTSUP}:
                        _fail(GATHER_PLATFORM_UNSUPPORTED, 5)
                    _fail(GATHER_PLATFORM_UNSUPPORTED, 5)
                try:
                    _darwin_renameatx_np(pfd, "t2", pfd, "u", DARWIN_RENAME_EXCL)
                    _fail(GATHER_PLATFORM_UNSUPPORTED, 5)
                except OSError as error:
                    if error.errno != errno.EEXIST:
                        _fail(GATHER_PLATFORM_UNSUPPORTED, 5)
                nfd, _st = _open_child_file_raw(pfd, "u")
                try:
                    fcntl.flock(nfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    os.close(nfd)
                    _fail(GATHER_PLATFORM_UNSUPPORTED, 5)
                os.close(nfd)
            finally:
                os.close(pfd)
        finally:
            os.close(dfd)
    except GatherStoreError as error:
        if error.code == GATHER_PLATFORM_UNSUPPORTED:
            raise
        _fail(GATHER_PLATFORM_UNSUPPORTED, 5)
    except OSError:
        _fail(GATHER_PLATFORM_UNSUPPORTED, 5)
    finally:
        if probe_root:
            try:
                for dirpath, dirnames, filenames in os.walk(probe_root, topdown=False):
                    for name in filenames:
                        try:
                            os.unlink(os.path.join(dirpath, name))
                        except OSError:
                            pass
                    for name in dirnames:
                        try:
                            os.rmdir(os.path.join(dirpath, name))
                        except OSError:
                            pass
                os.rmdir(probe_root)
            except OSError:
                pass


def _openat2_raw(dirfd: int, name: str, flags: int, mode: int = 0) -> int:
    if _libc_syscall is None:
        libc = _load_linux_libc()
        syscall = libc.syscall
        syscall.restype = ctypes.c_long
    else:
        syscall = _libc_syscall
    how = _OpenHowStruct(flags, mode, RESOLVE_FLAGS)
    encoded = name.encode("utf-8")
    ctypes.set_errno(0)
    fd = syscall(
        ctypes.c_long(SYS_OPENAT2),
        ctypes.c_int(dirfd),
        ctypes.c_char_p(encoded),
        ctypes.byref(how),
        ctypes.c_size_t(ctypes.sizeof(_OpenHowStruct)),
    )
    if fd < 0:
        err = ctypes.get_errno()
        if err in {errno.ENOSYS, errno.ENOTSUP, errno.EINVAL} and dirfd < 0:
            _fail(GATHER_PLATFORM_UNSUPPORTED, 5)
        raise _oserr(err)
    return int(fd)


def _linux_renameat2(olddirfd: int, oldname: str, newdirfd: int, newname: str, flags: int) -> None:
    libc = _load_linux_libc()
    fn = libc.renameat2
    fn.restype = ctypes.c_int
    ctypes.set_errno(0)
    rc = fn(
        ctypes.c_int(olddirfd),
        ctypes.c_char_p(oldname.encode("utf-8")),
        ctypes.c_int(newdirfd),
        ctypes.c_char_p(newname.encode("utf-8")),
        ctypes.c_uint(flags),
    )
    if rc != 0:
        raise _oserr(ctypes.get_errno())


def _darwin_renameatx_np(fromfd: int, from_name: str, tofd: int, to_name: str, flags: int) -> None:
    lib = _load_darwin_libsystem()
    fn = lib.renameatx_np
    fn.restype = ctypes.c_int
    ctypes.set_errno(0)
    rc = fn(
        ctypes.c_int(fromfd),
        ctypes.c_char_p(from_name.encode("utf-8")),
        ctypes.c_int(tofd),
        ctypes.c_char_p(to_name.encode("utf-8")),
        ctypes.c_uint(flags),
    )
    if rc != 0:
        raise _oserr(ctypes.get_errno())


def _exclusive_rename(src_dir: int, src_name: str, dst_dir: int, dst_name: str) -> None:
    if _LINUX:
        _linux_renameat2(src_dir, src_name, dst_dir, dst_name, RENAME_NOREPLACE)
        return
    if _DARWIN:
        _darwin_renameatx_np(src_dir, src_name, dst_dir, dst_name, DARWIN_RENAME_EXCL)
        return
    _fail(GATHER_PLATFORM_UNSUPPORTED, 5)


def _exchange_rename(src_dir: int, src_name: str, dst_dir: int, dst_name: str) -> None:
    if _LINUX:
        _linux_renameat2(src_dir, src_name, dst_dir, dst_name, RENAME_EXCHANGE)
        return
    if _DARWIN:
        _darwin_renameatx_np(src_dir, src_name, dst_dir, dst_name, DARWIN_RENAME_SWAP)
        return
    _fail(GATHER_PLATFORM_UNSUPPORTED, 5)


def _file_durable_sync(fd: int) -> None:
    os.fsync(fd)
    if _DARWIN:
        fcntl.fcntl(fd, F_FULLFSYNC, 0)


def _dir_durable_sync(fd: int) -> None:
    try:
        os.fsync(fd)
    except OSError as error:
        if _DARWIN and error.errno == errno.EINVAL and _darwin_dir_fsync_unsupported:
            return
        raise


def _open_root() -> int:
    ensure_platform()
    try:
        return os.open("/", _dir_flags())
    except OSError:
        _fail(GATHER_PLATFORM_UNSUPPORTED, 5)


def _require_component(name: str) -> None:
    if not component_ok(name):
        _fail(ARGUMENT_INVALID, 2)


def _open_child_dir_raw(parent: int, name: str) -> int:
    _require_component(name)
    if _LINUX:
        fd = _openat2_raw(parent, name, _dir_flags())
    else:
        fd = os.open(name, _dir_flags(), dir_fd=parent)
    try:
        info = os.fstat(fd)
    except OSError:
        os.close(fd)
        raise
    if not stat.S_ISDIR(info.st_mode):
        os.close(fd)
        raise _oserr(errno.ENOTDIR)
    return fd


def open_child_dir(parent: int, name: str) -> int:
    try:
        return _open_child_dir_raw(parent, name)
    except OSError:
        _fail(STORE_UNTRUSTED)


def _open_child_file_raw(parent: int, name: str) -> tuple[int, os.stat_result]:
    _require_component(name)
    if _LINUX:
        fd = _openat2_raw(parent, name, _file_flags())
    else:
        fd = os.open(name, _file_flags(), dir_fd=parent)
    try:
        info = os.fstat(fd)
    except OSError:
        os.close(fd)
        raise
    if stat.S_ISDIR(info.st_mode):
        os.close(fd)
        raise _oserr(errno.EISDIR)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(fd)
        raise _oserr(errno.EINVAL)
    return fd, info


def open_child_file(parent: int, name: str) -> tuple[int, os.stat_result]:
    try:
        return _open_child_file_raw(parent, name)
    except OSError:
        _fail(STORE_UNTRUSTED)


def try_open_child_file(parent: int, name: str) -> tuple[int, os.stat_result] | None:
    try:
        return _open_child_file_raw(parent, name)
    except OSError as error:
        if error.errno == errno.ENOENT:
            return None
        _fail(OUTPUT_UNTRUSTED)


def create_exclusive_child(parent: int, name: str, mode: int = 0o600) -> int:
    _require_component(name)
    flags = _create_flags()
    try:
        if _LINUX:
            fd = _openat2_raw(parent, name, flags, mode)
        else:
            fd = os.open(name, flags, mode, dir_fd=parent)
    except OSError as error:
        if error.errno == errno.EEXIST:
            _fail(OUTPUT_EXISTS, 2)
        _fail(OUTPUT_UNTRUSTED)
    try:
        os.fchmod(fd, mode)
    except OSError:
        os.close(fd)
        _fail(OUTPUT_UNTRUSTED)
    return fd


@dataclass(frozen=True)
class Ident:
    dev: int
    ino: int


def ident_of(info: os.stat_result) -> Ident:
    return Ident(int(info.st_dev), int(info.st_ino))


def owner_only_file(info: os.stat_result, euid: int) -> bool:
    return (
        stat.S_ISREG(info.st_mode)
        and info.st_nlink == 1
        and info.st_uid == euid
        and (info.st_mode & 0o777) == 0o600
    )


def owner_only_dir(info: os.stat_result, euid: int) -> bool:
    return stat.S_ISDIR(info.st_mode) and info.st_uid == euid and (info.st_mode & 0o777) == 0o700


def abs_components(path: str) -> list[str]:
    if not path or not posixpath.isabs(path) or not _utf8_valid(path):
        _fail(ARGUMENT_INVALID, 2)
    cleaned = posixpath.normpath(path)
    if cleaned == "/":
        _fail(ARGUMENT_INVALID, 2)
    if not cleaned.startswith("/"):
        _fail(ARGUMENT_INVALID, 2)
    parts = cleaned[1:].split("/")
    for part in parts:
        if not component_ok(part):
            _fail(ARGUMENT_INVALID, 2)
    return parts


def split_leaf(path: str) -> tuple[str, str]:
    if not posixpath.isabs(path):
        _fail(ARGUMENT_INVALID, 2)
    cleaned = posixpath.normpath(path)
    if cleaned == "/":
        _fail(ARGUMENT_INVALID, 2)
    directory, name = posixpath.dirname(cleaned), posixpath.basename(cleaned)
    if not component_ok(name):
        _fail(ARGUMENT_INVALID, 2)
    return directory, name


def open_dir_abs(path: str) -> tuple[int, list[Ident]]:
    parts = abs_components(path) if path != "/" else []
    if path == "/":
        fd = _open_root()
        try:
            info = os.fstat(fd)
        except OSError:
            os.close(fd)
            _fail(STORE_UNTRUSTED)
        return fd, [ident_of(info)]
    fd = _open_root()
    try:
        info = os.fstat(fd)
    except OSError:
        os.close(fd)
        _fail(STORE_UNTRUSTED)
    chain = [ident_of(info)]
    for name in parts:
        try:
            nxt = open_child_dir(fd, name)
        except GatherStoreError:
            os.close(fd)
            raise
        os.close(fd)
        fd = nxt
        try:
            info = os.fstat(fd)
        except OSError:
            os.close(fd)
            _fail(STORE_UNTRUSTED)
        chain.append(ident_of(info))
    return fd, chain


def _close_quiet(fd: int) -> None:
    if fd >= 0:
        try:
            os.close(fd)
        except OSError:
            pass


def read_dir_names(fd: int, max_names: int) -> tuple[list[str], bool]:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
    except OSError:
        _fail(STORE_UNTRUSTED)
    names: list[str] = []
    try:
        with os.scandir(fd) as scanner:
            for entry in scanner:
                name = entry.name
                if name in {".", ".."}:
                    continue
                names.append(name)
                if max_names >= 0 and len(names) > max_names:
                    return names, True
    except OSError:
        _fail(STORE_UNTRUSTED)
    names.sort()
    return names, False


def pread_all(fd: int, size: int, max_bytes: int) -> bytes:
    if size < 0:
        _fail(PARTIAL_OR_DEFERRED, 8)
    if size > max_bytes:
        _fail(PARTIAL_OR_DEFERRED, 8)
    if size == 0:
        return b""
    buf = bytearray(size)
    off = 0
    while off < size:
        try:
            chunk = os.pread(fd, size - off, off)
        except OSError:
            _fail(STORE_UNTRUSTED)
        if not chunk:
            _fail(PARTIAL_OR_DEFERRED, 8)
        buf[off:off + len(chunk)] = chunk
        off += len(chunk)
    return bytes(buf)


def hash_fd(fd: int, max_bytes: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    off = 0
    while True:
        try:
            chunk = os.pread(fd, 64 * 1024, off)
        except OSError:
            _fail(STORE_UNTRUSTED)
        if not chunk:
            break
        size += len(chunk)
        if size > max_bytes:
            _fail(PARTIAL_OR_DEFERRED, 8)
        digest.update(chunk)
        off += len(chunk)
    return digest.hexdigest(), size


def temp_name_ok(name: str) -> bool:
    if not name.startswith("t.") or len(name) != 34:
        return False
    rest = name[2:]
    if any(c not in "0123456789abcdef" for c in rest):
        return False
    return component_ok(name)


def chain_contains(chain: list[Ident], item: Ident) -> bool:
    return item in chain


@dataclass(frozen=True)
class _Pinned:
    exists: bool
    is_dir: bool
    ident: Ident | None
    chain: tuple[Ident, ...]
    parent_id: Ident | None
    parent_chain: tuple[Ident, ...]
    name: str


def pin_path(path: str, is_dir: bool) -> _Pinned:
    """Pin a leaf by descriptor identity. Only ENOENT means missing.

    ``is_dir=True`` opens with ``O_DIRECTORY``. ``is_dir=False`` requires a
    regular singleton file and never treats a directory as a file. ELOOP,
    ENOTDIR, EACCES, and other open/stat errors fail closed.
    """
    cleaned = posixpath.normpath(path)
    directory, name = split_leaf(cleaned)
    parent, parent_chain = open_dir_abs(directory)
    try:
        try:
            parent_info = os.fstat(parent)
        except OSError:
            _fail(STORE_UNTRUSTED)
        parent_id = ident_of(parent_info)
        if is_dir:
            try:
                child = _open_child_dir_raw(parent, name)
            except OSError as error:
                if error.errno == errno.ENOENT:
                    return _Pinned(False, True, None, tuple(parent_chain), parent_id, tuple(parent_chain), name)
                _fail(STORE_UNTRUSTED)
            try:
                try:
                    info = os.fstat(child)
                except OSError:
                    _fail(STORE_UNTRUSTED)
                ident = ident_of(info)
            finally:
                _close_quiet(child)
            return _Pinned(True, True, ident, tuple(parent_chain) + (ident,), parent_id, tuple(parent_chain), name)
        try:
            fd, info = _open_child_file_raw(parent, name)
        except OSError as error:
            if error.errno == errno.ENOENT:
                return _Pinned(False, False, None, tuple(parent_chain), parent_id, tuple(parent_chain), name)
            _fail(STORE_UNTRUSTED)
        try:
            ident = ident_of(info)
        finally:
            _close_quiet(fd)
        return _Pinned(True, False, ident, tuple(parent_chain), parent_id, tuple(parent_chain), name)
    finally:
        _close_quiet(parent)


def _pinned_inside_dir(node: _Pinned, directory: Ident) -> bool:
    if node.ident is not None and node.ident == directory:
        return True
    if node.parent_id is not None and node.parent_id == directory:
        return True
    if chain_contains(list(node.chain), directory):
        return True
    if chain_contains(list(node.parent_chain), directory):
        return True
    return False


def paths_overlap(left: str, left_is_dir: bool, right: str, right_is_dir: bool) -> bool:
    """Descriptor-identity overlap, including a not-yet-created leaf.

    Equality and containment are decided from pinned identities and ancestor
    chains. Origin contents are never read.
    """
    src = pin_path(left, left_is_dir)
    ps = pin_path(right, right_is_dir)
    if src.exists and ps.exists and src.ident is not None and ps.ident is not None:
        if src.ident == ps.ident:
            return True
        if src.is_dir and _pinned_inside_dir(ps, src.ident):
            return True
        if ps.is_dir and _pinned_inside_dir(src, ps.ident):
            return True
        return False
    if ps.exists and ps.ident is not None:
        if src.is_dir and src.ident is not None:
            if src.ident == ps.ident:
                return True
            if chain_contains(list(ps.chain), src.ident):
                return True
            if chain_contains(list(src.chain), ps.ident):
                return True
            return False
        if src.parent_id == ps.ident or (
            ps.ident in ((src.parent_id,) + src.chain if src.parent_id else src.chain)
        ):
            return True
        if chain_contains(list(src.chain), ps.ident):
            return True
        return False
    if src.exists and src.ident is not None:
        if src.is_dir:
            if src.ident == ps.parent_id or chain_contains(list(ps.parent_chain), src.ident):
                return True
            return False
        if src.parent_id == ps.parent_id and src.name == ps.name:
            return True
        return False
    return src.parent_id == ps.parent_id and src.name == ps.name


def exclusive_publish(path: str, data: bytes, *, exist_code: str = OUTPUT_EXISTS, exist_exit: int = 2) -> None:
    """Create a new owner-only 0600 regular file. Refuse existing/symlink targets.

    Immutable no-replace publication: O_EXCL never overwrites an existing name.
    """
    directory, name = split_leaf(path)
    parent, _chain = open_dir_abs(directory)
    try:
        try:
            info = os.fstat(parent)
        except OSError:
            _fail(OUTPUT_UNTRUSTED)
        if not owner_only_dir(info, os.geteuid()):
            _fail(OUTPUT_UNTRUSTED)
        opened = try_open_child_file(parent, name)
        if opened is not None:
            existing, st = opened
            os.close(existing)
            _fail(exist_code, exist_exit)
        fd = create_exclusive_child(parent, name, 0o600)
        try:
            _write_all(fd, data)
            try:
                _file_durable_sync(fd)
                st = os.fstat(fd)
            except OSError:
                _fail(OUTPUT_UNTRUSTED)
            if not owner_only_file(st, os.geteuid()) or st.st_size != len(data):
                _fail(OUTPUT_UNTRUSTED)
        except GatherStoreError:
            os.close(fd)
            try:
                os.unlink(name, dir_fd=parent)
            except OSError:
                pass
            raise
        except OSError:
            os.close(fd)
            try:
                os.unlink(name, dir_fd=parent)
            except OSError:
                pass
            _fail(OUTPUT_UNTRUSTED)
        os.close(fd)
    finally:
        _close_quiet(parent)


def replace_publish(path: str, data: bytes) -> None:
    """Replace dest under an exclusive flock on the owner-only parent directory.

    Dest must be absent or an owner-only regular singleton. A dest that is a
    symlink or other non-regular inode is never overwritten: missing dest uses
    the platform exclusive rename (Linux ``RENAME_NOREPLACE``, Darwin
    ``RENAME_EXCL``); existing dest uses exchange (``RENAME_EXCHANGE`` /
    ``RENAME_SWAP``) and a dest-identity mismatch restores names and fails
    closed. Callers need not hold the parent lock; this function acquires it.
    Immutable publication remains ``exclusive_publish``. Callers must not
    already hold LOCK_EX on the parent via another file description: Linux
    flock is not recursive across open file descriptions and would yield
    STORE_BUSY.
    """
    directory, name = split_leaf(path)
    parent, _chain = open_dir_abs(directory)
    token = "t." + hashlib.sha256(os.urandom(16)).hexdigest()[:32]
    dest_fd = -1
    dest_id: Ident | None = None
    try:
        try:
            info = os.fstat(parent)
        except OSError:
            _fail(OUTPUT_UNTRUSTED)
        if not owner_only_dir(info, os.geteuid()):
            _fail(OUTPUT_UNTRUSTED)
        try:
            fcntl.flock(parent, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in {errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK}:
                _fail(STORE_BUSY, 6)
            _fail(OUTPUT_UNTRUSTED)
        try:
            dest_fd, dest_st = _open_child_file_raw(parent, name)
        except OSError as error:
            if error.errno != errno.ENOENT:
                _fail(OUTPUT_UNTRUSTED)
        else:
            if not owner_only_file(dest_st, os.geteuid()):
                _fail(OUTPUT_UNTRUSTED)
            dest_id = ident_of(dest_st)
        fd = create_exclusive_child(parent, token, 0o600)
        try:
            _write_all(fd, data)
            try:
                _file_durable_sync(fd)
                st = os.fstat(fd)
            except OSError:
                _fail(OUTPUT_UNTRUSTED)
            if not owner_only_file(st, os.geteuid()) or st.st_size != len(data):
                _fail(OUTPUT_UNTRUSTED)
        except GatherStoreError:
            os.close(fd)
            try:
                os.unlink(token, dir_fd=parent)
            except OSError:
                pass
            raise
        except OSError:
            os.close(fd)
            try:
                os.unlink(token, dir_fd=parent)
            except OSError:
                pass
            _fail(OUTPUT_UNTRUSTED)
        os.close(fd)
        if dest_id is None:
            try:
                _exclusive_rename(parent, token, parent, name)
            except OSError:
                try:
                    os.unlink(token, dir_fd=parent)
                except OSError:
                    pass
                _fail(OUTPUT_UNTRUSTED)
        else:
            try:
                _exchange_rename(parent, token, parent, name)
            except OSError:
                try:
                    os.unlink(token, dir_fd=parent)
                except OSError:
                    pass
                _fail(OUTPUT_UNTRUSTED)
            swapped_fd = -1
            try:
                swapped_fd, swapped_st = _open_child_file_raw(parent, token)
            except OSError:
                try:
                    _exchange_rename(parent, token, parent, name)
                except OSError:
                    pass
                _fail(OUTPUT_UNTRUSTED)
            os.close(swapped_fd)
            swapped_fd = -1
            if ident_of(swapped_st) != dest_id or not owner_only_file(swapped_st, os.geteuid()):
                try:
                    _exchange_rename(parent, token, parent, name)
                except OSError:
                    pass
                _fail(OUTPUT_UNTRUSTED)
            try:
                os.unlink(token, dir_fd=parent)
            except OSError:
                _fail(OUTPUT_UNTRUSTED)
        try:
            final_fd, final_st = _open_child_file_raw(parent, name)
        except OSError:
            _fail(OUTPUT_UNTRUSTED)
        try:
            if not owner_only_file(final_st, os.geteuid()) or final_st.st_size != len(data):
                _fail(OUTPUT_UNTRUSTED)
        finally:
            os.close(final_fd)
    finally:
        _close_quiet(dest_fd)
        _close_quiet(parent)


def _write_all(fd: int, data: bytes) -> None:
    off = 0
    while off < len(data):
        try:
            n = os.write(fd, data[off:])
        except OSError:
            _fail(OUTPUT_UNTRUSTED)
        if n == 0:
            _fail(OUTPUT_UNTRUSTED)
        off += n


@dataclass(frozen=True)
class Receipt:
    format: str
    receipt_id: str
    collector_version: str
    acquired_at: str
    adapter: str
    detection_mode: str
    validation_state: str
    origin_path: str
    source_stat: Mapping[str, int]
    byte_count: int
    object_sha256: str
    object_ref: str
    scope_state: str
    source_scope_id: str | None
    import_status: str
    importer_available: bool

    @property
    def receipt_hex(self) -> str:
        digest = sha_hex_prefixed(self.receipt_id)
        if digest is None:
            _fail(STORE_UNTRUSTED)
        return digest

    @property
    def object_digest_hex(self) -> str:
        digest = sha_hex_prefixed(self.object_sha256)
        if digest is None:
            _fail(STORE_UNTRUSTED)
        return digest


@dataclass(frozen=True)
class StoreStatus:
    complete: bool
    store_state: str
    objects: int
    receipts: int
    by_adapter: Mapping[str, int]
    by_importer_available: Mapping[str, int]
    by_scope_state: Mapping[str, int]


def _receipt_from_body(body: Mapping[str, Any]) -> Receipt:
    return Receipt(
        format=body["format"],
        receipt_id=body["receipt_id"],
        collector_version=body["collector_version"],
        acquired_at=body["acquired_at"],
        adapter=body["adapter"],
        detection_mode=body["detection_mode"],
        validation_state=body["validation_state"],
        origin_path=body["origin_path"],
        source_stat=MappingProxyType(dict(body["source_stat"])),
        byte_count=body["byte_count"],
        object_sha256=body["object_sha256"],
        object_ref=body["object_ref"],
        scope_state=body["scope_state"],
        source_scope_id=body["source_scope_id"],
        import_status=body["import_status"],
        importer_available=body["importer_available"],
    )


class GatherStore:
    """Pinned, shared-locked gather store. Does not open ``origin_path``."""

    def __init__(self, path: str) -> None:
        self.path = posixpath.normpath(path)
        self._fds: list[int] = []
        self._root = -1
        self._obj = -1
        self._rec = -1
        self._tmp = -1
        self._marker = -1
        self._id: Ident | None = None
        self._chain: list[Ident] = []
        self._obj_id: Ident | None = None
        self._rec_id: Ident | None = None
        self._tmp_id: Ident | None = None
        self._marker_id: Ident | None = None
        self._objects_dir_id: Ident | None = None
        self._receipts: tuple[Receipt, ...] = ()
        self._status = StoreStatus(
            complete=False, store_state="partial", objects=0, receipts=0,
            by_adapter=MappingProxyType({}),
            by_importer_available=MappingProxyType({"true": 0, "false": 0}),
            by_scope_state=MappingProxyType({}),
        )
        self._closed = False
        try:
            self._attach()
        except BaseException:
            self.close()
            raise

    def _track(self, fd: int) -> int:
        self._fds.append(fd)
        return fd

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for fd in reversed(self._fds):
            _close_quiet(fd)
        self._fds.clear()
        self._root = self._obj = self._rec = self._tmp = self._marker = -1

    def __enter__(self) -> "GatherStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _attach(self) -> None:
        ensure_platform()
        directory, name = split_leaf(self.path)
        parent, chain = open_dir_abs(directory)
        try:
            root = open_child_dir(parent, name)
        except GatherStoreError:
            os.close(parent)
            raise
        os.close(parent)
        self._track(root)
        self._root = root
        try:
            rst = os.fstat(root)
        except OSError:
            _fail(STORE_UNTRUSTED)
        if not owner_only_dir(rst, os.geteuid()):
            _fail(STORE_UNTRUSTED)
        self._id = ident_of(rst)
        self._chain = chain + [self._id]
        self._verify_root_listing(root)
        marker, mst = open_child_file(root, MARKER_NAME)
        self._track(marker)
        self._marker = marker
        if not owner_only_file(mst, os.geteuid()) or mst.st_size > HARD_RECEIPT_BYTES:
            _fail(STORE_UNTRUSTED)
        got = pread_all(marker, mst.st_size, HARD_RECEIPT_BYTES)
        if got != MARKER_TEXT:
            _fail(STORE_UNTRUSTED)
        try:
            fcntl.flock(marker, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in {errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK}:
                _fail(STORE_BUSY, 6)
            _fail(STORE_UNTRUSTED)
        objp = open_child_dir(root, "objects")
        try:
            try:
                pst = os.fstat(objp)
            except OSError:
                _fail(STORE_UNTRUSTED)
            self._objects_dir_id = ident_of(pst)
            names, hit = read_dir_names(objp, 64)
            if hit or names != ["sha256"]:
                _fail(STORE_UNTRUSTED)
            obj = open_child_dir(objp, "sha256")
        finally:
            os.close(objp)
        self._track(obj)
        self._obj = obj
        rec = open_child_dir(root, "receipts")
        self._track(rec)
        self._rec = rec
        tmp = open_child_dir(root, "tmp")
        self._track(tmp)
        self._tmp = tmp
        for fd, attr in ((obj, "_obj_id"), (rec, "_rec_id"), (tmp, "_tmp_id")):
            try:
                info = os.fstat(fd)
            except OSError:
                _fail(STORE_UNTRUSTED)
            if not owner_only_dir(info, os.geteuid()):
                _fail(STORE_UNTRUSTED)
            setattr(self, attr, ident_of(info))
        try:
            mst = os.fstat(marker)
        except OSError:
            _fail(STORE_UNTRUSTED)
        if not owner_only_file(mst, os.geteuid()):
            _fail(STORE_UNTRUSTED)
        self._marker_id = ident_of(mst)
        self._inspect()

    def _verify_root_listing(self, root: int) -> None:
        names, hit = read_dir_names(root, 16)
        if hit:
            _fail(STORE_UNTRUSTED)
        allowed = {MARKER_NAME, "objects", "receipts", "tmp"}
        if set(names) != allowed:
            _fail(STORE_UNTRUSTED)

    def _list_capped(self, fd: int, used: list[int], maximum: int) -> list[str]:
        if used[0] >= maximum:
            _fail(PARTIAL_OR_DEFERRED, 8)
        remain = maximum - used[0]
        names, hit = read_dir_names(fd, remain)
        used[0] += len(names)
        if hit or used[0] > maximum:
            _fail(PARTIAL_OR_DEFERRED, 8)
        return names

    def _inspect(self) -> None:
        euid = os.geteuid()
        used = [0]
        limits_entries = DEFAULT_MAX_ENTRIES
        try:
            root_names = self._list_capped(self._root, used, limits_entries)
        except GatherStoreError as error:
            if error.code == PARTIAL_OR_DEFERRED:
                self._status = StoreStatus(
                    complete=False, store_state="partial", objects=0, receipts=0,
                    by_adapter=MappingProxyType({}),
                    by_importer_available=MappingProxyType({"true": 0, "false": 0}),
                    by_scope_state=MappingProxyType({}),
                )
                raise
            raise
        allowed = {MARKER_NAME, "objects", "receipts", "tmp"}
        if set(root_names) != allowed:
            _fail(STORE_UNTRUSTED)
        objp = open_child_dir(self._root, "objects")
        try:
            objp_names = self._list_capped(objp, used, limits_entries)
            if objp_names != ["sha256"]:
                _fail(STORE_UNTRUSTED)
        finally:
            os.close(objp)
        tmp_names = self._list_capped(self._tmp, used, limits_entries)
        hashed_bytes = 0
        for name in tmp_names:
            if not temp_name_ok(name):
                _fail(STORE_UNTRUSTED)
            tf, st = open_child_file(self._tmp, name)
            os.close(tf)
            if not owner_only_file(st, euid):
                _fail(STORE_UNTRUSTED)
            if hashed_bytes + st.st_size > DEFAULT_MAX_STATUS_BYTES:
                _fail(PARTIAL_OR_DEFERRED, 8)
            hashed_bytes += st.st_size
        obj_names = self._list_capped(self._obj, used, limits_entries)
        obj_set: set[str] = set()
        for name in obj_names:
            if not is_hex64(name):
                _fail(STORE_UNTRUSTED)
            obj_set.add(name)
        rec_names = self._list_capped(self._rec, used, limits_entries)
        if len(rec_names) > DEFAULT_MAX_STATUS_RECEIPTS:
            _fail(PARTIAL_OR_DEFERRED, 8)
        hashed: dict[str, int] = {}

        def hash_object(digest: str, want_size: int, check_size: bool) -> None:
            nonlocal hashed_bytes
            if digest in hashed:
                if check_size and hashed[digest] != want_size:
                    _fail(STORE_UNTRUSTED)
                return
            if hashed_bytes >= DEFAULT_MAX_STATUS_BYTES:
                _fail(PARTIAL_OR_DEFERRED, 8)
            remain = DEFAULT_MAX_STATUS_BYTES - hashed_bytes
            fd, st = open_child_file(self._obj, digest)
            try:
                if not owner_only_file(st, euid):
                    _fail(STORE_UNTRUSTED)
                if st.st_size > remain:
                    _fail(PARTIAL_OR_DEFERRED, 8)
                got, size = hash_fd(fd, remain)
                if size != st.st_size or got != digest:
                    _fail(STORE_OBJECT_CORRUPT)
                if check_size and st.st_size != want_size:
                    _fail(STORE_UNTRUSTED)
            finally:
                os.close(fd)
            hashed[digest] = int(st.st_size)
            hashed_bytes += int(st.st_size)

        receipts: list[Receipt] = []
        by_adapter: dict[str, int] = {}
        by_importer = {"true": 0, "false": 0}
        by_scope: dict[str, int] = {}
        seen: set[str] = set()
        for name in rec_names:
            if len(name) != 69 or not name.endswith(".json") or not is_hex64(name[:64]):
                _fail(STORE_UNTRUSTED)
            if name in seen:
                _fail(STORE_UNTRUSTED)
            seen.add(name)
            fd, st = open_child_file(self._rec, name)
            try:
                if not owner_only_file(st, euid) or st.st_size > DEFAULT_RECEIPT_BYTES:
                    _fail(STORE_UNTRUSTED)
                body = pread_all(fd, st.st_size, DEFAULT_RECEIPT_BYTES)
            finally:
                os.close(fd)
            parsed = parse_receipt_strict(body)
            rid = sha_hex_prefixed(parsed["receipt_id"])
            if rid is None or rid + ".json" != name:
                _fail(STORE_UNTRUSTED)
            digest = sha_hex_prefixed(parsed["object_sha256"])
            if digest is None or digest not in obj_set:
                _fail(STORE_UNTRUSTED)
            if parsed["source_stat"]["size"] != parsed["byte_count"]:
                _fail(STORE_UNTRUSTED)
            hash_object(digest, parsed["byte_count"], True)
            receipt = _receipt_from_body(parsed)
            receipts.append(receipt)
            by_adapter[receipt.adapter] = by_adapter.get(receipt.adapter, 0) + 1
            by_importer["true" if receipt.importer_available else "false"] += 1
            by_scope[receipt.scope_state] = by_scope.get(receipt.scope_state, 0) + 1
        for name in obj_set:
            if name not in hashed:
                hash_object(name, 0, False)
        receipts.sort(key=lambda item: item.receipt_id)
        self._receipts = tuple(receipts)
        self._status = StoreStatus(
            complete=True,
            store_state="verified",
            objects=len(obj_set),
            receipts=len(rec_names),
            by_adapter=MappingProxyType(dict(sorted(by_adapter.items()))),
            by_importer_available=MappingProxyType(by_importer),
            by_scope_state=MappingProxyType(dict(sorted(by_scope.items()))),
        )

    @property
    def status(self) -> StoreStatus:
        return self._status

    @property
    def receipts(self) -> tuple[Receipt, ...]:
        return self._receipts

    @property
    def ident(self) -> Ident:
        if self._id is None:
            _fail(STORE_UNTRUSTED)
        return self._id

    @property
    def chain(self) -> tuple[Ident, ...]:
        return tuple(self._chain)

    def object_bytes(self, digest_hex: str, *, max_bytes: int = HARD_MAX_FILE_BYTES) -> bytes:
        if self._closed:
            _fail(STORE_UNTRUSTED)
        if not is_hex64(digest_hex):
            _fail(ARGUMENT_INVALID, 2)
        fd, st = open_child_file(self._obj, digest_hex)
        try:
            if not owner_only_file(st, os.geteuid()):
                _fail(STORE_UNTRUSTED)
            if st.st_size > max_bytes:
                _fail(PARTIAL_OR_DEFERRED, 8)
            data = pread_all(fd, st.st_size, max_bytes)
            if hashlib.sha256(data).hexdigest() != digest_hex or len(data) != st.st_size:
                _fail(STORE_OBJECT_CORRUPT)
            return data
        finally:
            os.close(fd)

    def materialize_object(self, digest_hex: str, dest_dir: str, *, name: str = "object",
                           max_bytes: int = ADAPTER_MAX_BYTES) -> str:
        """Write verified object bytes into an operator-owned private directory."""
        if not posixpath.isabs(dest_dir):
            _fail(ARGUMENT_INVALID, 2)
        _require_component(name)
        data = self.object_bytes(digest_hex, max_bytes=max_bytes)
        directory = posixpath.normpath(dest_dir)
        parent, _chain = open_dir_abs(directory)
        try:
            try:
                info = os.fstat(parent)
            except OSError:
                _fail(OUTPUT_UNTRUSTED)
            if not owner_only_dir(info, os.geteuid()):
                _fail(OUTPUT_UNTRUSTED)
            fd = create_exclusive_child(parent, name, 0o600)
            try:
                _write_all(fd, data)
                try:
                    _file_durable_sync(fd)
                    st = os.fstat(fd)
                except OSError:
                    _fail(OUTPUT_UNTRUSTED)
                if not owner_only_file(st, os.geteuid()) or st.st_size != len(data):
                    _fail(OUTPUT_UNTRUSTED)
            except GatherStoreError:
                os.close(fd)
                try:
                    os.unlink(name, dir_fd=parent)
                except OSError:
                    pass
                raise
            except OSError:
                os.close(fd)
                try:
                    os.unlink(name, dir_fd=parent)
                except OSError:
                    pass
                _fail(OUTPUT_UNTRUSTED)
            _close_quiet(fd)
        finally:
            _close_quiet(parent)
        return posixpath.join(directory, name)


def open_gather_store(path: str) -> GatherStore:
    if not isinstance(path, str) or not posixpath.isabs(path):
        _fail(ARGUMENT_INVALID, 2)
    return GatherStore(path)


def verify_store(path: str) -> StoreStatus:
    store = open_gather_store(path)
    try:
        return store.status
    finally:
        store.close()


@contextmanager
def locked_store(path: str) -> Iterator[GatherStore]:
    store = open_gather_store(path)
    try:
        yield store
    finally:
        store.close()
