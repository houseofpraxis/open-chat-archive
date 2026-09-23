"""Synthetic-only static site tests; no ignored exports are read."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from open_chat_archive.inspect_site import _alpha_label, _chat_catalog, _message_card, build_all
from open_chat_archive.inspection import SessionProjection
from open_chat_archive.synthetic import create_synthetic_batch
from open_chat_archive.topology import TopologyProjection


class InspectSiteTests(unittest.TestCase):
    def test_build_all_creates_independent_owner_only_local_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary) / "archives"
            create_synthetic_batch(parent / "synthetic-one", archive_id="synthetic-one", batch_id="batch-0001")
            create_synthetic_batch(parent / "synthetic-two", archive_id="synthetic-two", batch_id="batch-0001", rich=True)
            output = Path(temporary) / "site"
            self.assertTrue(build_all(parent, output, schema_root=Path.cwd()))
            self.assertTrue((output / "synthetic-one" / "records.html").is_file())
            self.assertTrue((output / "synthetic-two" / "coverage.html").is_file())
            self.assertEqual(1, len(list((output / "synthetic-one" / "chats").glob("*.html"))))
            data = (output / "synthetic-two" / "index.html").read_text(encoding="utf-8")
            self.assertIn("restricted-unclassified", data)
            self.assertNotIn("https://", data)
            source_raw = next((parent / "synthetic-one" / "sources").iterdir()).read_text(encoding="utf-8")
            self.assertNotIn('{&quot;conversations&quot;', (output / "synthetic-one" / "records.html").read_text(encoding="utf-8"))
            self.assertTrue(source_raw.startswith('{"conversations"'))
            for path in [output, *output.rglob("*")]:
                self.assertEqual(0, os.stat(path).st_mode & 0o077)
            # The marker permits a safe repeat build of the generator's own output.
            self.assertTrue(build_all(parent, output, schema_root=Path.cwd()))

    def test_catalog_uses_session_adapter_not_archive_list(self) -> None:
        key = ("sha256:" + "a" * 64, "sha256:" + "b" * 64)
        session = SessionProjection(key, "chat-a.html", (), (), (), (), (), (), (), 0, None)
        model = SimpleNamespace(
            adapters=("org.example.one", "org.example.two"),
            records={key: {"body": {"title": "Only One", "created_at": "2026-01-01T00:00:00Z"},
                            "identity": {"adapter_namespace": "org.example.one"}}},
            diagnostics=(),
            sessions=(session,),
        )
        html = _chat_catalog(model)  # type: ignore[arg-type]
        self.assertIn("org.example.one", html)
        self.assertNotIn("org.example.two", html)

    def test_catalog_uses_friendly_confirmed_forest_diagnosis(self) -> None:
        key = ("sha256:" + "a" * 64, "sha256:" + "b" * 64)
        first = ("sha256:" + "1" * 64, "sha256:" + "3" * 64)
        second = ("sha256:" + "2" * 64, "sha256:" + "4" * 64)
        topology = TopologyProjection(None, "forest", "complete_parent_child", ((first,), (second,)),
                                     (first, second), (first, second), 1, None, (), ())
        session = SessionProjection(key, "chat-a.html", (), (), (), (), (), (), (), 0, None, topology)
        model = SimpleNamespace(
            records={key: {"body": {"title": "Forest"}, "identity": {"adapter_namespace": "org.example"}}},
            diagnostics=(
                SimpleNamespace(code="MULTIPLE_SESSION_ROOTS", severity="info", related_opaque_ids=(),
                                associated_session_ids=(key[0],)),
                SimpleNamespace(code="DISCONNECTED_SESSION_GRAPH", severity="info", related_opaque_ids=(),
                                associated_session_ids=(key[0],)),
            ),
            sessions=(session,),
        )
        html = _chat_catalog(model)  # type: ignore[arg-type]
        self.assertIn("confirmed forest topology", html)
        self.assertNotIn("MULTIPLE_SESSION_ROOTS", html)
        self.assertNotIn("DISCONNECTED_SESSION_GRAPH", html)

    def test_catalog_keeps_structural_warnings_and_matches_collapsed_session_ids(self) -> None:
        key = ("sha256:" + "c" * 64, "sha256:" + "d" * 64)
        topology = TopologyProjection(None, "unknown", "incomplete_or_unknown", (), (), (), 0, None, (), ())
        session = SessionProjection(key, "chat-a.html", (), (), (), (), (), (), (), 0, None, topology)
        model = SimpleNamespace(
            records={key: {"body": {"title": "Unknown"}, "identity": {"adapter_namespace": "org.example"}}},
            diagnostics=(
                SimpleNamespace(code="MULTIPLE_SESSION_ROOTS", severity="warning", related_opaque_ids=(),
                                associated_session_ids=(key[0],)),
                SimpleNamespace(code="DISCONNECTED_SESSION_GRAPH", severity="warning", related_opaque_ids=(),
                                associated_session_ids=(key[0],)),
            ),
            sessions=(session,),
        )
        html = _chat_catalog(model)  # type: ignore[arg-type]
        self.assertIn("MULTIPLE_SESSION_ROOTS", html)
        self.assertIn("DISCONNECTED_SESSION_GRAPH", html)

    def test_refuses_output_inside_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary) / "archives"
            create_synthetic_batch(parent / "synthetic-one", archive_id="synthetic-one", batch_id="batch-0001")
            with self.assertRaisesRegex(ValueError, "REFUSE_INPUT_OUTPUT_OVERLAP"):
                build_all(parent, parent / "synthetic-one" / "generated", schema_root=Path.cwd())

    def test_invalid_synthetic_archive_is_blocked_while_other_archive_builds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary) / "archives"
            create_synthetic_batch(parent / "synthetic-valid", archive_id="synthetic-valid", batch_id="batch-0001")
            invalid = parent / "synthetic-invalid"
            create_synthetic_batch(invalid, archive_id="synthetic-invalid", batch_id="batch-0001")
            (invalid / "batches" / "batch-0001" / "records.ndjson").write_bytes(b"not canonical\n")
            output = Path(temporary) / "site"
            self.assertFalse(build_all(parent, output, schema_root=Path.cwd()))
            self.assertTrue((output / "synthetic-valid" / "index.html").is_file())
            blocked = (output / "synthetic-invalid" / "index.html").read_text(encoding="utf-8")
            self.assertIn("RECORD_FILE_INTEGRITY", blocked)
            self.assertNotIn("not canonical", blocked)

    def test_message_markup_and_markdown_remain_literal(self) -> None:
        key = ("sha256:" + "1" * 64, "sha256:" + "2" * 64)
        hostile = "<img src=x onerror=alert(1)> **literal** </script>"
        record = {"record_type": "message", "body": {"role": "user", "visibility": "user_visible", "source_ordinal": 0,
                  "content": [{"type": "text", "text": hostile}]}, "identity": {}, "extensions": {}}
        model = SimpleNamespace(records={key: record}, occurrences={key: ()})
        page = _message_card(model, key)  # type: ignore[arg-type]
        self.assertIn("&lt;img src=x onerror=alert(1)&gt; **literal** &lt;/script&gt;", page)
        self.assertNotIn("<img src=x", page)

    def test_message_card_exposes_canonical_relationships(self) -> None:
        message_id = "sha256:" + "1" * 64
        other_id = "sha256:" + "3" * 64
        key = (message_id, "sha256:" + "2" * 64)
        relationship = ("sha256:" + "4" * 64, "sha256:" + "5" * 64)
        model = SimpleNamespace(
            records={
                key: {"record_type": "message", "body": {"role": "user", "visibility": "user_visible", "source_ordinal": 0, "content": []}, "identity": {}, "extensions": {}},
                relationship: {"record_type": "relationship", "body": {"from_id": message_id, "to_id": other_id, "kind": "reply_to"}, "identity": {}, "extensions": {}},
            },
            occurrences={key: ()},
        )
        self.assertIn("Explicit incoming and outgoing relationships", _message_card(model, key))  # type: ignore[arg-type]

    def test_component_labeling_continues_past_z(self) -> None:
        self.assertEqual("AA", _alpha_label(26))
        self.assertEqual("AZ", _alpha_label(51))


if __name__ == "__main__": unittest.main()
