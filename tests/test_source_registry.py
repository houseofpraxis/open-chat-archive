import json
import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock
from pathlib import Path

from open_chat_archive import source_registry
from open_chat_archive.source_registry import MAX_BYTES, SourceRegistryError, load_source_registry


class SourceRegistryTests(unittest.TestCase):
    def _write(self, root: Path, value: object) -> Path:
        path = root / "registry.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_full_triple_isolation_and_frozen_definition(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry = load_source_registry(self._write(root, {
                "registry_version": "0.1-experimental",
                "sources": [{"source_id": "one", "adapter_namespace": "example.adapter",
                             "display_label": "literal", "status": "unverified"}],
                "bindings": [{"archive_id": "one", "adapter_namespace": "example.adapter",
                              "source_scope_id": "personal", "source_id": "one"}],
            }))
            matched = registry.resolve("one", "example.adapter", "personal")
            self.assertIsNotNone(matched)
            self.assertIsNone(registry.resolve("two", "example.adapter", "personal"))
            self.assertIsNone(registry.resolve("one", "other.adapter", "personal"))
            with self.assertRaises((TypeError, AttributeError)):
                assert matched is not None
                getattr(matched, "__setitem__")("display_label", "changed")

    def test_rejects_duplicate_dangling_and_symlinked_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = {"registry_version": "0.1-experimental",
                    "sources": [{"source_id": "one", "adapter_namespace": "example.adapter", "display_label": "x", "status": "unverified"}],
                    "bindings": [{"archive_id": "archive", "adapter_namespace": "example.adapter", "source_scope_id": "scope", "source_id": "one"}]}
            duplicate = dict(base); duplicate["bindings"] = base["bindings"] * 2
            with self.assertRaisesRegex(SourceRegistryError, "REGISTRY_BINDING_DUPLICATE"):
                load_source_registry(self._write(root, duplicate))
            dangling = dict(base); dangling["bindings"] = [dict(base["bindings"][0], source_id="missing")]
            with self.assertRaisesRegex(SourceRegistryError, "REGISTRY_BINDING_DANGLING"):
                load_source_registry(self._write(root, dangling))
            target = self._write(root, base); link = root / "link.json"; os.symlink(target, link)
            with self.assertRaisesRegex(SourceRegistryError, "REGISTRY_SYMLINK"):
                load_source_registry(link)

    def test_strict_schema_and_json_failures_are_fixed_codes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            valid = {"registry_version": "0.1-experimental",
                     "sources": [{"source_id": "one", "adapter_namespace": "example.adapter",
                                  "display_label": "x", "status": "unverified"}],
                     "bindings": [{"archive_id": "archive", "adapter_namespace": "example.adapter",
                                   "source_scope_id": "scope", "source_id": "one"}]}
            cases = [
                (b'{"registry_version":"0.1-experimental","registry_version":"0.1-experimental","sources":[],"bindings":[]}', "REGISTRY_JSON"),
                (json.dumps(dict(valid, unexpected=True)).encode(), "REGISTRY_SCHEMA"),
                (json.dumps(dict(valid, sources=[dict(valid["sources"][0], source_id="bad\n")])).encode(), "REGISTRY_SCHEMA"),
                (json.dumps(dict(valid, sources=[valid["sources"][0], valid["sources"][0]])).encode(), "REGISTRY_SOURCE_DUPLICATE"),
                (json.dumps(dict(valid, sources=[dict(valid["sources"][0], status="operator_confirmed")])).encode(), "REGISTRY_SCHEMA"),
                (json.dumps(dict(valid, bindings=[dict(valid["bindings"][0], archive_id=1)])).encode(), "REGISTRY_SCHEMA"),
                (json.dumps(dict(valid, bindings=[dict(valid["bindings"][0], adapter_namespace="other.adapter")])).encode(), "REGISTRY_NAMESPACE_MISMATCH"),
            ]
            for number, (raw, code) in enumerate(cases):
                path = root / str(number)
                path.write_bytes(raw)
                with self.subTest(code=code, number=number):
                    with self.assertRaisesRegex(SourceRegistryError, code):
                        load_source_registry(path)

    def test_namespace_pattern_matches_canonical_schema_and_rejects_bad_label_hyphens(self) -> None:
        root = Path(__file__).resolve().parents[1]
        registry_schema = json.loads((root / "src" / "open_chat_archive" / "schema_assets" /
                                      "source-registry-0.1-experimental.schema.json").read_text())
        canonical_schema = json.loads((root / "schemas" / "v0.1" / "archive-record.schema.json").read_text())
        self.assertEqual(canonical_schema["$defs"]["namespace"]["pattern"],
                         registry_schema["$defs"]["namespace"]["pattern"])
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for namespace in ("-bad.namespace", "bad-.namespace"):
                registry = {"registry_version": "0.1-experimental",
                            "sources": [{"source_id": "one", "adapter_namespace": namespace,
                                         "display_label": "x", "status": "unverified"}],
                            "bindings": [{"archive_id": "archive", "adapter_namespace": namespace,
                                          "source_scope_id": "scope", "source_id": "one"}]}
                with self.subTest(namespace=namespace), self.assertRaisesRegex(SourceRegistryError, "REGISTRY_SCHEMA"):
                    load_source_registry(self._write(directory, registry))

    def test_regular_descriptor_read_is_bounded_and_checks_fstat(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = self._write(root, {"registry_version": "0.1-experimental", "sources": [], "bindings": []})
            real_read = os.read
            real_fstat = os.fstat
            events: list[str] = []
            with mock.patch.object(source_registry.os, "fstat", side_effect=lambda fd: (events.append("fstat"), real_fstat(fd))[1]) as fstat, \
                 mock.patch.object(source_registry.os, "read", side_effect=lambda fd, count: (events.append("read"), real_read(fd, count))[1]) as read, \
                 mock.patch.object(source_registry.os, "open", wraps=os.open) as opened:
                load_source_registry(path)
            self.assertTrue(fstat.called)
            self.assertTrue(read.called)
            self.assertLess(events.index("fstat"), events.index("read"))
            self.assertTrue(all(call.args[1] <= MAX_BYTES + 1 for call in read.call_args_list))
            self.assertTrue(any(call.args[1] & os.O_NOFOLLOW and call.args[1] & os.O_NONBLOCK
                                for call in opened.call_args_list))

    def test_final_path_swap_to_symlink_is_rejected_at_open(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = self._write(root, {"registry_version": "0.1-experimental", "sources": [], "bindings": []})
            target = root / "target.json"
            target.write_text("{}", encoding="utf-8")
            real_open = os.open
            swapped = False

            def open_after_swap(name: str | bytes | os.PathLike[str] | os.PathLike[bytes], flags: int,
                                mode: int = 0o777, *, dir_fd: int | None = None) -> int:
                nonlocal swapped
                if name == "registry.json" and not swapped:
                    swapped = True
                    path.unlink()
                    os.symlink(target, path)
                if dir_fd is None:
                    return real_open(name, flags, mode)
                return real_open(name, flags, mode, dir_fd=dir_fd)

            with mock.patch.object(source_registry.os, "open", side_effect=open_after_swap):
                with self.assertRaisesRegex(SourceRegistryError, "REGISTRY_SYMLINK"):
                    load_source_registry(path)

    def test_oversize_fifo_and_ancestor_symlink_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            too_large = root / "large"
            too_large.write_bytes(b"x" * (MAX_BYTES + 1))
            with self.assertRaisesRegex(SourceRegistryError, "REGISTRY_TOO_LARGE"):
                load_source_registry(too_large)
            fifo = root / "fifo"
            os.mkfifo(fifo)
            with self.assertRaisesRegex(SourceRegistryError, "REGISTRY_NOT_REGULAR"):
                load_source_registry(fifo)
            actual = root / "actual"
            actual.mkdir()
            self._write(actual, {"registry_version": "0.1-experimental", "sources": [], "bindings": []})
            linked = root / "linked"
            os.symlink(actual, linked)
            with self.assertRaisesRegex(SourceRegistryError, "REGISTRY_SYMLINK"):
                load_source_registry(linked / "registry.json")

    def test_cli_errors_do_not_echo_a_registry_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            canary = Path(temporary) / "private-account-canary"
            stream = io.StringIO()
            with redirect_stderr(stream):
                self.assertEqual(source_registry.main(["validate", str(canary)]), 1)
            self.assertIn("REGISTRY_MISSING", stream.getvalue())
            self.assertNotIn("private-account-canary", stream.getvalue())
