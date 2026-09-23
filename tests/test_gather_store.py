"""Synthetic gather-store reader tests. No real personal data."""
from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from open_chat_archive import gather_store as gs
from open_chat_archive.gather_store import (
    ARGUMENT_INVALID,
    GATHER_PLATFORM_UNSUPPORTED,
    MARKER_TEXT,
    OUTPUT_EXISTS,
    OUTPUT_UNTRUSTED,
    STORE_BUSY,
    STORE_OBJECT_CORRUPT,
    STORE_UNTRUSTED,
    GatherStoreError,
    build_receipt,
    exclusive_publish,
    go_json,
    locked_store,
    marshal_receipt_map,
    parse_receipt_strict,
    paths_overlap,
    pin_path,
    read_dir_names,
    receipt_key,
    replace_publish,
    verify_store,
)


def _tighten(root: str) -> None:
    os.chmod(root, 0o700)
    for dirpath, dirnames, filenames in os.walk(root):
        os.chmod(dirpath, 0o700)
        for name in filenames:
            os.chmod(os.path.join(dirpath, name), 0o600)


def write_file(path: str, data: bytes, mode: int = 0o600) -> None:
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    os.chmod(parent, 0o700)
    with open(path, "wb") as handle:
        handle.write(data)
    os.chmod(path, mode)


def claude_payload() -> bytes:
    entries = [
        {"type": "user", "uuid": "u", "sessionId": "s", "timestamp": "2026-01-01T00:00:00Z",
         "message": {"content": "synthetic-user"}},
        {"type": "assistant", "uuid": "a", "parentUuid": "u", "sessionId": "s",
         "timestamp": "2026-01-01T00:00:01Z",
         "message": {"content": [{"type": "text", "text": "synthetic-assistant"}]}},
    ]
    return b"".join(json.dumps(item, separators=(",", ":")).encode() + b"\n" for item in entries)


def codex_payload() -> bytes:
    entries = [
        {"timestamp": "2026-01-01T00:00:00Z", "type": "session_meta",
         "payload": {"id": "thread", "session_id": "thread"}},
        {"timestamp": "2026-01-01T00:00:00Z", "type": "response_item",
         "payload": {"type": "message", "id": "u", "role": "user",
                     "content": [{"type": "input_text", "text": "synthetic-input"}]}},
    ]
    return b"".join(json.dumps(item, separators=(",", ":")).encode() + b"\n" for item in entries)


def grok_payload() -> bytes:
    entries = [
        {"timestamp": 1, "method": "session/update",
         "params": {"sessionId": "s",
                    "update": {"sessionUpdate": "user_message_chunk",
                               "content": {"type": "text", "text": "synthetic-user"}},
                    "_meta": {"eventId": "u1"}}},
        {"timestamp": 2, "method": "session/update",
         "params": {"sessionId": "s",
                    "update": {"sessionUpdate": "agent_message_chunk",
                               "content": {"type": "text", "text": "synthetic-assistant"}},
                    "_meta": {"eventId": "a1"}}},
    ]
    return b"".join(json.dumps(item, separators=(",", ":")).encode() + b"\n" for item in entries)


def pi_payload() -> bytes:
    return b'{"synthetic":true,"kind":"pi-collection-only"}\n'


def make_store(root: str, items: list[dict]) -> list[dict]:
    os.makedirs(root, exist_ok=True)
    os.chmod(root, 0o700)
    write_file(os.path.join(root, ".oca-gather-store"), MARKER_TEXT)
    for name in ("objects", "receipts", "tmp"):
        os.makedirs(os.path.join(root, name), exist_ok=True)
        os.chmod(os.path.join(root, name), 0o700)
    os.makedirs(os.path.join(root, "objects", "sha256"), exist_ok=True)
    os.chmod(os.path.join(root, "objects", "sha256"), 0o700)
    built = []
    for item in items:
        data = item["data"]
        digest = hashlib.sha256(data).hexdigest()
        write_file(os.path.join(root, "objects", "sha256", digest), data)
        stat = {
            "dev": 1, "ino": 2, "size": len(data),
            "mtime_ns": 4, "ctime_ns": 5,
        }
        rid, body = build_receipt(
            adapter=item["adapter"], origin=item["origin"], digest_hex=digest,
            scope=item.get("scope", ""), source_stat=stat, byte_count=len(data),
            acquired_at=item.get("acquired_at", "2026-01-02T03:04:05Z"),
        )
        write_file(os.path.join(root, "receipts", rid + ".json"), body)
        built.append({"receipt_hex": rid, "digest": digest, "body": body, **item})
    _tighten(root)
    return built


class GatherStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = self.temp.name
        os.chmod(self.base, 0o700)

    def test_receipt_key_explicit_bytes(self) -> None:
        digest = "a" * 64
        payload = "\x00".join([
            "oca-gather-receipt-key/v1", "pi", "missing", "",
            "/synthetic/origin.jsonl", digest,
        ])
        want = hashlib.sha256(payload.encode()).hexdigest()
        got = receipt_key("pi", "missing", "", "/synthetic/origin.jsonl", digest)
        self.assertEqual(got, want)
        self.assertFalse(payload.endswith("\x00"))

    def test_canonical_receipt_roundtrip_and_rejects(self) -> None:
        digest = hashlib.sha256(b"abc").hexdigest()
        rid, body = build_receipt(
            adapter="pi", origin="/synthetic/a.jsonl", digest_hex=digest, scope="",
            source_stat={"dev": 1, "ino": 2, "size": 3, "mtime_ns": 4, "ctime_ns": 5},
            byte_count=3, acquired_at="2024-01-02T03:04:05Z",
        )
        parsed = parse_receipt_strict(body)
        self.assertEqual(parsed["receipt_id"], "sha256:" + rid)
        self.assertEqual(parsed["scope_state"], "missing")
        self.assertIsNone(parsed["source_scope_id"])
        self.assertFalse(parsed["importer_available"])
        self.assertTrue(body.endswith(b"\n") and body.count(b"\n") == 1)
        self.assertEqual(marshal_receipt_map(parsed), body)
        dup = b'{"adapter":"pi","adapter":"pi"}\n'
        with self.assertRaises(GatherStoreError) as raised:
            parse_receipt_strict(dup)
        self.assertEqual(str(raised.exception), STORE_UNTRUSTED)
        self.assertNotIn("/", str(raised.exception))
        obj = json.loads(body)
        rev = go_json({k: obj[k] for k in reversed(list(obj))})
        # go_json sorts keys, so reverse insertion still canonical; force unsorted via raw bytes.
        raw_rev = b'{"validation_state":"not_validated","adapter":"pi"}\n'
        with self.assertRaises(GatherStoreError):
            parse_receipt_strict(raw_rev)
        spaced = b"{ " + body.strip()[1:]
        with self.assertRaises(GatherStoreError):
            parse_receipt_strict(spaced)

    def test_valid_store_and_origin_never_opened(self) -> None:
        store = os.path.join(self.base, "store")
        origin = os.path.join(self.base, "never-open.jsonl")
        write_file(origin, b"should-not-be-read\n")
        sentinel = {"opened": False}

        def wrapped_open(path, *args, **kwargs):
            text = path if isinstance(path, str) else ""
            if text == origin or text.endswith("never-open.jsonl"):
                sentinel["opened"] = True
            return os_open(path, *args, **kwargs)

        os_open = os.open
        make_store(store, [{
            "adapter": "claude-code",
            "origin": origin,
            "scope": "synth-scope-1",
            "data": claude_payload(),
        }])
        with mock.patch("os.open", wraps=wrapped_open):
            status = verify_store(store)
            with locked_store(store) as handle:
                receipt = handle.receipts[0]
                data = handle.object_bytes(receipt.object_digest_hex)
                dest = os.path.join(self.base, "stage")
                os.mkdir(dest, 0o700)
                os.chmod(dest, 0o700)
                path = handle.materialize_object(receipt.object_digest_hex, dest)
        self.assertTrue(status.complete)
        self.assertEqual(status.store_state, "verified")
        self.assertEqual(status.receipts, 1)
        self.assertEqual(data, claude_payload())
        self.assertEqual(Path(path).read_bytes(), claude_payload())
        self.assertFalse(sentinel["opened"])
        self.assertEqual(receipt.origin_path, origin)

    def test_duplicate_key_corrupt_orphan_and_modes(self) -> None:
        store = os.path.join(self.base, "store")
        built = make_store(store, [{
            "adapter": "codex", "origin": "/synthetic/codex.jsonl",
            "scope": "synth-scope-2", "data": codex_payload(),
        }])
        rec = os.path.join(store, "receipts", built[0]["receipt_hex"] + ".json")
        write_file(rec, b'{"adapter":"codex","adapter":"codex"}\n')
        _tighten(store)
        with self.assertRaises(GatherStoreError) as raised:
            verify_store(store)
        self.assertEqual(raised.exception.code, STORE_UNTRUSTED)

        store2 = os.path.join(self.base, "store2")
        built2 = make_store(store2, [{
            "adapter": "grok-build", "origin": "/synthetic/grok.jsonl",
            "scope": "synth-scope-3", "data": grok_payload(),
        }])
        obj = os.path.join(store2, "objects", "sha256", built2[0]["digest"])
        write_file(obj, grok_payload() + b"x")
        _tighten(store2)
        with self.assertRaises(GatherStoreError) as raised:
            verify_store(store2)
        self.assertEqual(raised.exception.code, STORE_OBJECT_CORRUPT)

        store3 = os.path.join(self.base, "store3")
        make_store(store3, [{
            "adapter": "claude-code", "origin": "/synthetic/claude.jsonl",
            "scope": "synth-scope-4", "data": claude_payload(),
        }])
        orphan = b"synthetic-orphan-bytes\n"
        digest = hashlib.sha256(orphan).hexdigest()
        write_file(os.path.join(store3, "objects", "sha256", digest), orphan)
        _tighten(store3)
        status = verify_store(store3)
        self.assertEqual(status.objects, 2)
        self.assertEqual(status.receipts, 1)

        bad_orphan = os.path.join(self.base, "store4")
        make_store(bad_orphan, [{
            "adapter": "claude-code", "origin": "/synthetic/c.jsonl",
            "scope": "synth-scope-5", "data": claude_payload(),
        }])
        write_file(os.path.join(bad_orphan, "objects", "sha256", "ab" * 32), b"not-the-digest")
        _tighten(bad_orphan)
        with self.assertRaises(GatherStoreError) as raised:
            verify_store(bad_orphan)
        self.assertEqual(raised.exception.code, STORE_OBJECT_CORRUPT)

        extra = os.path.join(self.base, "store5")
        make_store(extra, [{
            "adapter": "pi", "origin": "/synthetic/pi.jsonl", "scope": "", "data": pi_payload(),
        }])
        write_file(os.path.join(extra, "unexpected"), b"x")
        _tighten(extra)
        with self.assertRaises(GatherStoreError) as raised:
            verify_store(extra)
        self.assertEqual(raised.exception.code, STORE_UNTRUSTED)

        mode = os.path.join(self.base, "store6")
        make_store(mode, [{
            "adapter": "pi", "origin": "/synthetic/pi2.jsonl", "scope": "", "data": pi_payload(),
        }])
        os.chmod(mode, 0o755)
        with self.assertRaises(GatherStoreError) as raised:
            verify_store(mode)
        self.assertEqual(raised.exception.code, STORE_UNTRUSTED)
        self.assertEqual(str(raised.exception), STORE_UNTRUSTED)

    def test_symlink_component_relative_path_and_tmp(self) -> None:
        real = os.path.join(self.base, "real")
        make_store(real, [{
            "adapter": "pi", "origin": "/synthetic/pi3.jsonl", "scope": "", "data": pi_payload(),
        }])
        link = os.path.join(self.base, "link")
        os.symlink(real, link)
        with self.assertRaises(GatherStoreError) as raised:
            verify_store(link)
        self.assertEqual(raised.exception.code, STORE_UNTRUSTED)
        with self.assertRaises(GatherStoreError) as raised:
            verify_store("relative-store")
        self.assertEqual(raised.exception.code, ARGUMENT_INVALID)
        self.assertEqual(raised.exception.exit_code, 2)

        store = os.path.join(self.base, "store-tmp")
        make_store(store, [{
            "adapter": "pi", "origin": "/synthetic/pi4.jsonl", "scope": "", "data": pi_payload(),
        }])
        write_file(os.path.join(store, "tmp", "t." + "ab" * 16), b"leftover")
        _tighten(store)
        status = verify_store(store)
        self.assertTrue(status.complete)
        write_file(os.path.join(store, "tmp", "not-a-temp"), b"bad")
        _tighten(store)
        with self.assertRaises(GatherStoreError):
            verify_store(store)

    def test_store_busy_shared_lock(self) -> None:
        store = os.path.join(self.base, "busy")
        make_store(store, [{
            "adapter": "pi", "origin": "/synthetic/pi5.jsonl", "scope": "", "data": pi_payload(),
        }])
        marker = os.path.join(store, ".oca-gather-store")
        script = (
            "import fcntl, os, sys, time\n"
            f"fd=os.open({marker!r}, os.O_RDWR)\n"
            "fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
            "sys.stdout.write('ready\\n'); sys.stdout.flush()\n"
            "time.sleep(20)\n"
        )
        proc = subprocess.Popen(
            [os.environ.get("PYTHON", sys.executable), "-c", script],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )

        def _stop() -> None:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
            if proc.stdout:
                proc.stdout.close()

        self.addCleanup(_stop)
        line = proc.stdout.readline()
        self.assertEqual(line.strip(), "ready")
        with self.assertRaises(GatherStoreError) as raised:
            verify_store(store)
        self.assertEqual(raised.exception.code, STORE_BUSY)
        self.assertEqual(raised.exception.exit_code, 6)
        _stop()

    def test_error_string_has_no_path_or_os_text(self) -> None:
        with self.assertRaises(GatherStoreError) as raised:
            verify_store("/synthetic/missing-store-path")
        text = str(raised.exception)
        self.assertEqual(text, STORE_UNTRUSTED)
        self.assertNotIn("synthetic", text)
        self.assertNotIn("No such file", text)
        self.assertNotIn("/", text)


class GatherStoreParityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = self.temp.name
        os.chmod(self.base, 0o700)

    def _restore_platform(self) -> None:
        gs._platform_ready = False
        gs._LINUX = sys.platform.startswith("linux")
        gs._DARWIN = sys.platform == "darwin"
        gs._darwin_dir_fsync_unsupported = False
        gs._libc_syscall = None
        if gs._LINUX:
            gs.ensure_platform()

    def test_rfc3339_matches_go_time_parse_vectors(self) -> None:
        accept = (
            "2024-01-02T03:04:05Z",
            "2024-01-02T03:04:05+00:00",
            "2024-01-02T03:04:05-07:00",
            "2024-01-02T03:04:05.1Z",
            "2024-01-02T03:04:05.123Z",
            "2024-01-02T03:04:05.123456789Z",
            "2024-01-02T03:04:05,123Z",
            "2024-02-29T00:00:00Z",
            "2024-04-30T00:00:00Z",
            "2024-01-02T03:04:05+24:00",
            "2024-01-02T03:04:05+00:60",
            "2024-01-02T3:04:05Z",
            "2024-01-02T03:04:05.123+01:00",
            "2000-02-29T00:00:00Z",
            "0000-01-01T00:00:00Z",
            "0000-02-29T00:00:00Z",
            "2024-01-02T03:04:05.1234567890Z",
            "2024-01-02T03:04:05+99:99",
        )
        reject = (
            "2024-02-31T00:00:00Z",
            "2024-04-31T00:00:00Z",
            "2024-02-31T00:00:00+00:00",
            "2024-04-31T12:00:00-05:00",
            "2023-02-29T00:00:00Z",
            "2024-06-31T00:00:00Z",
            "2024-11-31T00:00:00Z",
            "2024-01-02T24:00:00Z",
            "2024-01-02T23:60:00Z",
            "2024-01-02T23:59:60Z",
            "1900-02-29T00:00:00Z",
            "2024-01-02T03:04:05",
            "2024-01-02T03:04:05+0000",
            "2024-01-02t03:04:05Z",
            "2024-01-02T03:4:05Z",
            "2024-01-02T03:04:5Z",
            "2024-01-02T03:04:05.Z",
        )
        for item in accept:
            self.assertTrue(gs._rfc3339(item), item)
        for item in reject:
            self.assertFalse(gs._rfc3339(item), item)

        digest = hashlib.sha256(b"abc").hexdigest()
        _rid, body = build_receipt(
            adapter="pi", origin="/synthetic/a.jsonl", digest_hex=digest, scope="",
            source_stat={"dev": 1, "ino": 2, "size": 3, "mtime_ns": 4, "ctime_ns": 5},
            byte_count=3, acquired_at="2024-01-02T03:04:05Z",
        )
        parsed = parse_receipt_strict(
            body.replace(b"2024-01-02T03:04:05Z", b"2024-01-02T03:04:05.123Z")
        )
        self.assertEqual(parsed["acquired_at"], "2024-01-02T03:04:05.123Z")
        parsed = parse_receipt_strict(
            body.replace(b"2024-01-02T03:04:05Z", b"2024-01-02T03:04:05+00:00")
        )
        self.assertEqual(parsed["acquired_at"], "2024-01-02T03:04:05+00:00")
        with self.assertRaises(GatherStoreError) as raised:
            parse_receipt_strict(body.replace(b"2024-01-02T03:04:05Z", b"2024-02-31T03:04:05Z"))
        self.assertEqual(raised.exception.code, STORE_UNTRUSTED)
        with self.assertRaises(GatherStoreError) as raised:
            parse_receipt_strict(body.replace(b"2024-01-02T03:04:05Z", b"2024-04-31T03:04:05Z"))
        self.assertEqual(raised.exception.code, STORE_UNTRUSTED)

    def test_go_json_matches_encoding_json_vectors(self) -> None:
        # encoding/json Encoder with SetEscapeHTML(false); generated with Go 1.22.
        vectors = [
            ("a\x7fb", b'"a\x7fb"\n'),
            ("<>&", b'"<>&"\n'),
            ("a\u2028b\u2029c", b'"a\\u2028b\\u2029c"\n'),
            ("\"\\/\b\f\n\r\t", b'"\\"\\\\/\\b\\f\\n\\r\\t"\n'),
            ("\x00\x01\x1f", b'"\\u0000\\u0001\\u001f"\n'),
            ("<html>&amp;", b'"<html>&amp;"\n'),
            ({"b": 1, "a": "<x>"}, b'{"a":"<x>","b":1}\n'),
            (["<>", "\x7f"], b'["<>","\x7f"]\n'),
        ]
        for value, want in vectors:
            self.assertEqual(go_json(value), want)
        self.assertNotIn(b"\\u007f", go_json("a\x7fb"))
        self.assertNotIn(b"\\u003c", go_json("<>&"))
        with self.assertRaises(GatherStoreError) as raised:
            go_json("\ud800")
        self.assertEqual(raised.exception.code, STORE_UNTRUSTED)
        self.assertEqual(str(raised.exception), STORE_UNTRUSTED)
        with self.assertRaises(GatherStoreError) as raised:
            go_json("\ud800", invalid=ARGUMENT_INVALID)
        self.assertEqual(raised.exception.code, ARGUMENT_INVALID)
        self.assertEqual(raised.exception.exit_code, 2)
        self.assertNotIn("surrogate", str(raised.exception).lower())
        self.assertNotIn("codec", str(raised.exception).lower())

    def test_pin_path_fail_closed_and_overlap_without_origin_read(self) -> None:
        store = os.path.join(self.base, "store")
        origin = os.path.join(self.base, "origin.jsonl")
        write_file(origin, b"secret-origin-bytes\n")
        make_store(store, [{
            "adapter": "pi", "origin": origin, "scope": "", "data": pi_payload(),
        }])
        marker = os.path.join(store, ".oca-gather-store")
        sibling = os.path.join(self.base, "sibling")
        os.mkdir(sibling, 0o700)
        os.chmod(sibling, 0o700)

        with self.assertRaises(GatherStoreError) as raised:
            pin_path(store, False)
        self.assertEqual(raised.exception.code, STORE_UNTRUSTED)

        link = os.path.join(self.base, "store-link")
        os.symlink(store, link)
        with self.assertRaises(GatherStoreError) as raised:
            pin_path(link, True)
        self.assertEqual(raised.exception.code, STORE_UNTRUSTED)
        with self.assertRaises(GatherStoreError) as raised:
            pin_path(link, False)
        self.assertEqual(raised.exception.code, STORE_UNTRUSTED)

        with self.assertRaises(GatherStoreError) as raised:
            pin_path(marker, True)
        self.assertEqual(raised.exception.code, STORE_UNTRUSTED)

        missing = os.path.join(self.base, "missing-leaf")
        pinned = pin_path(missing, True)
        self.assertFalse(pinned.exists)

        with mock.patch.object(gs, "_open_child_dir_raw", side_effect=OSError(errno.EACCES, "Permission denied")):
            with self.assertRaises(GatherStoreError) as raised:
                pin_path(store, True)
            self.assertEqual(raised.exception.code, STORE_UNTRUSTED)
            self.assertNotIn("Permission", str(raised.exception))
            self.assertNotIn("/", str(raised.exception))

        with mock.patch.object(gs, "_open_child_file_raw", side_effect=OSError(errno.EACCES, "Permission denied")):
            with self.assertRaises(GatherStoreError) as raised:
                pin_path(origin, False)
            self.assertEqual(raised.exception.code, STORE_UNTRUSTED)
            self.assertNotIn("Permission", str(raised.exception))

        loop = os.path.join(self.base, "loop")
        os.symlink("loop", loop)
        with self.assertRaises(GatherStoreError) as raised:
            pin_path(loop, False)
        self.assertEqual(raised.exception.code, STORE_UNTRUSTED)
        with self.assertRaises(GatherStoreError) as raised:
            pin_path(loop, True)
        self.assertEqual(raised.exception.code, STORE_UNTRUSTED)

        notdir = os.path.join(origin, "child")
        with self.assertRaises(GatherStoreError) as raised:
            pin_path(notdir, False)
        self.assertEqual(raised.exception.code, STORE_UNTRUSTED)
        self.assertNotIn(origin, str(raised.exception))

        self.assertTrue(paths_overlap(marker, False, store, True))
        self.assertTrue(paths_overlap(store, True, marker, False))
        self.assertTrue(paths_overlap(marker, False, marker, False))
        self.assertTrue(paths_overlap(store, True, store, True))
        self.assertFalse(paths_overlap(sibling, True, store, True))
        with mock.patch("os.pread", side_effect=AssertionError("origin contents read")):
            self.assertTrue(paths_overlap(origin, False, self.base, True))
            self.assertFalse(paths_overlap(origin, False, store, True))

    def test_read_dir_names_bounds_while_iterating_and_closes(self) -> None:
        names = [f"n{i}" for i in range(10)]
        state = {"yielded": 0, "closed": False}

        class Ent:
            def __init__(self, name: str) -> None:
                self.name = name

        class Scanner:
            def __enter__(self) -> "Scanner":
                return self

            def __exit__(self, *exc: object) -> None:
                state["closed"] = True

            def __iter__(self) -> "Scanner":
                return self

            def __next__(self) -> Ent:
                if state["yielded"] >= len(names):
                    raise StopIteration
                name = names[state["yielded"]]
                state["yielded"] += 1
                return Ent(name)

        with mock.patch("os.scandir", return_value=Scanner()), mock.patch("os.lseek", return_value=0):
            got, hit = read_dir_names(3, 2)
        self.assertTrue(hit)
        self.assertEqual(len(got), 3)
        self.assertEqual(state["yielded"], 3)
        self.assertTrue(state["closed"])

    def test_materialize_object_maps_fsync_errors(self) -> None:
        store = os.path.join(self.base, "store")
        make_store(store, [{
            "adapter": "pi", "origin": "/synthetic/pi.jsonl", "scope": "", "data": pi_payload(),
        }])
        dest = os.path.join(self.base, "stage")
        os.mkdir(dest, 0o700)
        os.chmod(dest, 0o700)
        with locked_store(store) as handle:
            with mock.patch.object(gs, "_file_durable_sync", side_effect=OSError(errno.EIO, "Input/output error")):
                with self.assertRaises(GatherStoreError) as raised:
                    handle.materialize_object(handle.receipts[0].object_digest_hex, dest)
        self.assertEqual(raised.exception.code, OUTPUT_UNTRUSTED)
        self.assertEqual(str(raised.exception), OUTPUT_UNTRUSTED)
        self.assertNotIn("Input/output", str(raised.exception))
        self.assertNotIn("/", str(raised.exception))

        dest2 = os.path.join(self.base, "stage2")
        os.mkdir(dest2, 0o700)
        os.chmod(dest2, 0o700)
        with locked_store(store) as handle:
            with mock.patch("os.write", side_effect=OSError(errno.EIO, "Input/output error")):
                with self.assertRaises(GatherStoreError) as raised:
                    handle.materialize_object(handle.receipts[0].object_digest_hex, dest2)
        self.assertEqual(raised.exception.code, OUTPUT_UNTRUSTED)
        self.assertNotIn("Input/output", str(raised.exception))

    def test_replace_publish_lock_exchange_and_immutable_no_replace(self) -> None:
        parent = os.path.join(self.base, "out")
        os.mkdir(parent, 0o700)
        os.chmod(parent, 0o700)
        target = os.path.join(parent, "target")
        write_file(target, b"SECRET\n")
        dest = os.path.join(parent, "dest")
        os.symlink(target, dest)
        with self.assertRaises(GatherStoreError) as raised:
            replace_publish(dest, b"new\n")
        self.assertEqual(raised.exception.code, OUTPUT_UNTRUSTED)
        self.assertTrue(os.path.islink(dest))
        self.assertEqual(Path(target).read_bytes(), b"SECRET\n")

        live = os.path.join(parent, "live")
        write_file(live, b"old\n")
        os.chmod(live, 0o600)
        real_ex = gs._exchange_rename

        def swapped(src_dir: int, src_name: str, dst_dir: int, dst_name: str) -> None:
            os.unlink(live)
            os.symlink(target, live)
            return real_ex(src_dir, src_name, dst_dir, dst_name)

        with mock.patch.object(gs, "_exchange_rename", swapped):
            with self.assertRaises(GatherStoreError) as raised:
                replace_publish(live, b"new\n")
        self.assertEqual(raised.exception.code, OUTPUT_UNTRUSTED)
        self.assertTrue(os.path.islink(live))
        self.assertEqual(Path(target).read_bytes(), b"SECRET\n")

        immutable = os.path.join(parent, "immut")
        exclusive_publish(immutable, b"one\n")
        with self.assertRaises(GatherStoreError) as raised:
            exclusive_publish(immutable, b"two\n")
        self.assertEqual(raised.exception.code, OUTPUT_EXISTS)
        self.assertEqual(Path(immutable).read_bytes(), b"one\n")

        ledger = os.path.join(parent, "ledger")
        exclusive_publish(ledger, b"v1\n")
        replace_publish(ledger, b"v2\n")
        self.assertEqual(Path(ledger).read_bytes(), b"v2\n")
        self.assertEqual(stat.S_IMODE(os.stat(ledger).st_mode), 0o600)

        fresh = os.path.join(parent, "fresh")
        replace_publish(fresh, b"created\n")
        self.assertEqual(Path(fresh).read_bytes(), b"created\n")
        self.assertFalse(os.path.islink(fresh))

        as_dir = os.path.join(parent, "asdir")
        os.mkdir(as_dir, 0o700)
        os.chmod(as_dir, 0o700)
        with self.assertRaises(GatherStoreError) as raised:
            replace_publish(as_dir, b"nope\n")
        self.assertEqual(raised.exception.code, OUTPUT_UNTRUSTED)
        self.assertTrue(os.path.isdir(as_dir))

        parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(parent_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(GatherStoreError) as raised:
                replace_publish(ledger, b"v3-same-proc\n")
            self.assertEqual(raised.exception.code, STORE_BUSY)
            self.assertEqual(Path(ledger).read_bytes(), b"v2\n")
        finally:
            os.close(parent_fd)

        marker = os.path.join(parent, ".oca-bridge-archives")
        write_file(marker, b"oca-bridge-archives/v0.1-experimental\n")
        marker_fd = os.open(marker, os.O_RDONLY)
        try:
            fcntl.flock(marker_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            replace_publish(ledger, b"v2-under-marker\n")
            self.assertEqual(Path(ledger).read_bytes(), b"v2-under-marker\n")
        finally:
            os.close(marker_fd)

        script = (
            "import fcntl, os, sys, time\n"
            f"fd=os.open({parent!r}, os.O_RDONLY | os.O_DIRECTORY)\n"
            "fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
            "sys.stdout.write('ready\\n'); sys.stdout.flush()\n"
            "time.sleep(20)\n"
        )
        proc = subprocess.Popen(
            [os.environ.get("PYTHON", sys.executable), "-c", script],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )

        def _stop() -> None:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
            if proc.stdout:
                proc.stdout.close()

        self.addCleanup(_stop)
        self.assertEqual(proc.stdout.readline().strip(), "ready")
        with self.assertRaises(GatherStoreError) as raised:
            replace_publish(ledger, b"v3\n")
        self.assertEqual(raised.exception.code, STORE_BUSY)
        self.assertEqual(Path(ledger).read_bytes(), b"v2-under-marker\n")
        _stop()

    def test_ensure_platform_linux_tmp_probe_maps_fixed_codes(self) -> None:
        self.addCleanup(self._restore_platform)
        real = gs._openat2_raw

        def wrapped(dirfd: int, name: str, flags: int, mode: int = 0) -> int:
            if name == "tmp":
                raise OSError(errno.EINVAL, "openat2")
            return real(dirfd, name, flags, mode)

        gs._platform_ready = False
        with mock.patch.object(gs, "_openat2_raw", wrapped):
            with self.assertRaises(GatherStoreError) as raised:
                gs.ensure_platform()
        self.assertEqual(raised.exception.code, GATHER_PLATFORM_UNSUPPORTED)
        self.assertEqual(str(raised.exception), GATHER_PLATFORM_UNSUPPORTED)
        self.assertNotIn("openat2", str(raised.exception))
        self.assertNotIn("/", str(raised.exception))

        gs._platform_ready = False
        real = gs._openat2_raw

        def missing_tmp(dirfd: int, name: str, flags: int, mode: int = 0) -> int:
            if name == "tmp":
                raise OSError(errno.ENOENT, "No such file or directory")
            return real(dirfd, name, flags, mode)

        with mock.patch.object(gs, "_openat2_raw", missing_tmp):
            gs.ensure_platform()
        self.assertTrue(gs._platform_ready)

    def test_ensure_platform_darwin_branches_mocked(self) -> None:
        self.assertNotEqual(sys.platform, "darwin")
        self.addCleanup(self._restore_platform)
        gs._platform_ready = False
        gs._LINUX = False
        gs._DARWIN = True
        with mock.patch.object(gs, "_darwin_probe_rename_excl", return_value=errno.ENOSYS):
            with self.assertRaises(GatherStoreError) as raised:
                gs.ensure_platform()
        self.assertEqual(raised.exception.code, GATHER_PLATFORM_UNSUPPORTED)
        self.assertEqual(str(raised.exception), GATHER_PLATFORM_UNSUPPORTED)

        gs._platform_ready = False
        gs._LINUX = False
        gs._DARWIN = True
        with mock.patch.object(gs, "_darwin_probe_rename_excl", return_value=errno.ENOENT), \
             mock.patch.object(gs, "_darwin_probe_publication_capabilities") as probe:
            gs.ensure_platform()
            probe.assert_called_once()
        self.assertTrue(gs._platform_ready)

        gs._platform_ready = False
        gs._LINUX = False
        gs._DARWIN = True
        with mock.patch.object(gs, "_darwin_probe_rename_excl", return_value=0), \
             mock.patch.object(
                 gs, "_darwin_probe_publication_capabilities",
                 side_effect=GatherStoreError(OUTPUT_UNTRUSTED),
             ):
            with self.assertRaises(GatherStoreError) as raised:
                gs.ensure_platform()
        self.assertEqual(raised.exception.code, GATHER_PLATFORM_UNSUPPORTED)
        self.assertNotIn("untrusted", str(raised.exception).lower())

        def renameatx(fromfd: int, from_name: str, tofd: int, to_name: str, flags: int) -> None:
            if from_name == "t1" and to_name == "u":
                os.rename("t1", "u", src_dir_fd=fromfd, dst_dir_fd=tofd)
                return
            if from_name == "t2" and to_name == "u":
                raise OSError(errno.EEXIST, "")
            raise OSError(errno.ENOENT, "")

        gs._LINUX = False
        gs._DARWIN = True
        gs._darwin_dir_fsync_unsupported = False
        with mock.patch.object(gs, "_darwin_renameatx_np", renameatx), \
             mock.patch.object(gs, "_file_durable_sync", return_value=None):
            gs._darwin_probe_publication_capabilities()
        self.assertFalse(gs._darwin_dir_fsync_unsupported)

        gs._LINUX = False
        gs._DARWIN = True
        with mock.patch.object(gs, "_darwin_renameatx_np", renameatx), \
             mock.patch.object(gs, "_file_durable_sync", return_value=None), \
             mock.patch("os.fsync", side_effect=OSError(errno.EINVAL, "Invalid argument")):
            gs._darwin_probe_publication_capabilities()
        self.assertTrue(gs._darwin_dir_fsync_unsupported)

        gs._LINUX = False
        gs._DARWIN = True
        with mock.patch.object(gs, "_darwin_renameatx_np", renameatx), \
             mock.patch.object(gs, "_file_durable_sync", return_value=None), \
             mock.patch("fcntl.flock", side_effect=OSError(errno.ENOSYS, "Function not implemented")):
            with self.assertRaises(GatherStoreError) as raised:
                gs._darwin_probe_publication_capabilities()
        self.assertEqual(raised.exception.code, GATHER_PLATFORM_UNSUPPORTED)
        self.assertNotIn("Function", str(raised.exception))

        gs._LINUX = False
        gs._DARWIN = True
        with mock.patch.object(gs, "_darwin_renameatx_np", side_effect=OSError(errno.ENOSYS, "")):
            with self.assertRaises(GatherStoreError) as raised:
                gs._darwin_probe_rename_excl()
                gs._darwin_probe_publication_capabilities()
        self.assertEqual(raised.exception.code, GATHER_PLATFORM_UNSUPPORTED)

        gs._DARWIN = True
        gs._darwin_dir_fsync_unsupported = True
        with mock.patch("os.fsync", side_effect=OSError(errno.EINVAL, "Invalid argument")):
            gs._dir_durable_sync(0)
        gs._darwin_dir_fsync_unsupported = False
        with mock.patch("os.fsync", side_effect=OSError(errno.EINVAL, "Invalid argument")):
            with self.assertRaises(OSError):
                gs._dir_durable_sync(0)


if __name__ == "__main__":
    unittest.main()
