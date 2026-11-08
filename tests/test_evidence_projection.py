"""Synthetic acceptance tests for the experimental evidence projection."""
from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from open_chat_archive.canonical import finalize_record
from open_chat_archive.evidence_projection import main, project_archive, _write_private_atomic
from open_chat_archive.synthetic import create_synthetic_batch
from open_chat_archive.validation import ValidationError


class EvidenceProjectionTests(unittest.TestCase):
    def test_deterministic_occurrences_exact_citations_and_no_source_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "archives" / "synthetic"
            batch = create_synthetic_batch(root, archive_id="synthetic", batch_id="batch-1")
            source_path = next((root / "sources").iterdir())
            before = hashlib.sha256(source_path.read_bytes()).digest()
            first = project_archive(root)
            second = project_archive(root)
            self.assertEqual(first, second)
            self.assertEqual("oca-evidence-v1", first["projection_version"])
            self.assertEqual("synthetic", first["archive_id"])
            entries = {entry["record"]["entity_id"]: entry for entry in first["records"]}
            message = next(entry for entry in first["records"]
                           if entry["record"]["record_type"] == "message"
                           and entry["record"]["body"]["role"] == "user")
            raw_records = (batch / "records.ndjson").read_text(encoding="utf-8").splitlines()
            expected_ordinal = next(i for i, raw in enumerate(raw_records, 1)
                                    if json.loads(raw)["entity_id"] == message["record"]["entity_id"])
            self.assertEqual([{"batch_id": "batch-1", "ordinal": expected_ordinal}], message["occurrences"])
            self.assertEqual([message["record"]["body"]["session_id"]], message["session_ids"])
            citation = message["citations"][0]
            source = next(r["record"] for r in first["records"]
                          if r["record"]["record_type"] == "source_artifact")
            self.assertEqual(source["body"]["byte_digest"], citation["sources"][0]["byte_digest"])
            self.assertEqual({"json_pointer": "/conversations/0/messages/0"}, citation["sources"][0]["location"])
            self.assertEqual(before, hashlib.sha256(source_path.read_bytes()).digest())
            self.assertTrue(entries)

    def test_changed_revision_and_expanded_snapshot_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "archives" / "synthetic"
            create_synthetic_batch(root, archive_id="synthetic", batch_id="batch-1")
            create_synthetic_batch(root, archive_id="synthetic", batch_id="batch-2", expanded=True, changed=True)
            projection = project_archive(root)
            alpha = [entry for entry in projection["records"]
                     if entry["record"]["record_type"] == "session"
                     and entry["record"]["identity"]["logical_key"] == "chat-alpha"]
            self.assertEqual(2, len(alpha))
            self.assertEqual(1, len({entry["record"]["entity_id"] for entry in alpha}))
            self.assertEqual(2, len({entry["record"]["revision_id"] for entry in alpha}))
            self.assertEqual({"batch-1", "batch-2"},
                             {item["batch_id"] for entry in alpha for item in entry["occurrences"]})
            self.assertTrue(any(entry["record"]["identity"]["logical_key"] == "chat-beta"
                                for entry in projection["records"] if entry["record"]["record_type"] == "session"))

    def test_hidden_and_reasoning_text_are_excluded_but_code_is_searchable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "archives" / "synthetic"
            batch = create_synthetic_batch(root, archive_id="synthetic", batch_id="batch-1")
            records = [json.loads(line) for line in (batch / "records.ndjson").read_text().splitlines()]
            target = next(r for r in records if r["record_type"] == "message" and r["body"]["role"] == "user")
            old_revision = target["revision_id"]
            target["body"]["content"] = [{"type": "text", "text": "visible"},
                                            {"type": "code", "text": "print(1)"},
                                            {"type": "reasoning", "text": "secret reasoning"}]
            target.update(finalize_record(target))
            observation = next(r for r in records if r["record_type"] == "observation"
                               and r["body"]["subject"] == {"entity_id": target["entity_id"], "revision_id": old_revision})
            observation["body"]["subject"]["revision_id"] = target["revision_id"]
            observation.update(finalize_record(observation))
            (batch / "records.ndjson").write_text("".join(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n"
                                                                       for r in sorted(records, key=lambda r: (r["record_type"], r["entity_id"], r["revision_id"]))),
                                                   encoding="utf-8")
            manifest = json.loads((batch / "manifest.json").read_text())
            raw = (batch / "records.ndjson").read_bytes()
            manifest["records"]["byte_digest"] = "sha256:" + hashlib.sha256(raw).hexdigest()
            manifest["records"]["size"] = len(raw)
            (batch / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
            projection = project_archive(root)
            projected = next(entry for entry in projection["records"]
                             if entry["record"]["entity_id"] == target["entity_id"]
                             and entry["record"]["revision_id"] == target["revision_id"])
            self.assertEqual("visible\nprint(1)", projected["text"])
            target["body"]["visibility"] = "exported_reasoning"
            target.update(finalize_record(target))
            observation["body"]["subject"]["revision_id"] = target["revision_id"]
            observation.update(finalize_record(observation))
            (batch / "records.ndjson").write_text("".join(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n"
                                                                       for r in sorted(records, key=lambda r: (r["record_type"], r["entity_id"], r["revision_id"]))),
                                                   encoding="utf-8")
            raw = (batch / "records.ndjson").read_bytes()
            manifest["records"]["byte_digest"] = "sha256:" + hashlib.sha256(raw).hexdigest()
            manifest["records"]["size"] = len(raw)
            (batch / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
            self.assertEqual("", next(entry["text"] for entry in project_archive(root)["records"]
                                       if entry["record"]["entity_id"] == target["entity_id"]
                                       and entry["record"]["revision_id"] == target["revision_id"]))

    def test_invalid_archive_rejects_before_output_and_output_mode_is_private(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "archives" / "synthetic"
            batch = create_synthetic_batch(root, archive_id="synthetic", batch_id="batch-1")
            source = next((root / "sources").iterdir())
            source.write_bytes(source.read_bytes() + b"tamper")
            with self.assertRaises(ValidationError):
                project_archive(root)
            output = Path(temporary) / "projection.json"
            self.assertFalse(output.exists())
            _write_private_atomic(output, b"synthetic projection\n")
            self.assertEqual(0o600, os.stat(output).st_mode & 0o777)

    def test_cli_requires_private_explicit_output_and_never_prints_projection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "archives" / "synthetic"
            create_synthetic_batch(root, archive_id="synthetic", batch_id="batch-1")
            output = Path(temporary) / "private" / "projection.json"
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(0, main([str(root), "--output", str(output)]))
            self.assertEqual("", stdout.getvalue())
            self.assertEqual(0o600, os.stat(output).st_mode & 0o777)
            self.assertEqual(project_archive(root), json.loads(output.read_text(encoding="utf-8")))
            with redirect_stdout(stdout):
                self.assertEqual(2, main([str(root), "--output", str(root / "projection.json")]))
            self.assertFalse((root / "projection.json").exists())


if __name__ == "__main__":
    unittest.main()
