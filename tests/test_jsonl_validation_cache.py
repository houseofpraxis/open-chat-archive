"""Synthetic cache regressions for strict NDJSON evidence validation."""
import hashlib
import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

from open_chat_archive.canonical import entity_identity, finalize_record, identity_input
from open_chat_archive.source_adapters import CodexAdapter
from open_chat_archive.strictjson import load_source_ndjson
from open_chat_archive.validation import ValidationError, validate_batch


class JsonlValidationCacheTests(unittest.TestCase):
    def _batch(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name) / "archive"
        source = Path(temp.name) / "synthetic.jsonl"
        entries = [
            {"timestamp": "synthetic", "type": "session_meta", "payload": {"id": "session"}},
            {"timestamp": "synthetic", "type": "response_item", "payload": {"type": "message", "id": "message", "role": "user", "content": [{"type": "input_text", "text": "synthetic"}]}}]
        source.write_bytes(b"".join(json.dumps(entry, separators=(",", ":")).encode() + b"\n" for entry in entries))
        result = CodexAdapter("scope").conform(source, root, archive_id="archive", batch_id="batch")
        return root, result.batch_dir

    @staticmethod
    def _rewrite(batch, records):
        records.sort(key=lambda record: (record["record_type"], record["entity_id"], record["revision_id"]))
        raw = b"".join(json.dumps(record, sort_keys=True, separators=(",", ":")).encode() + b"\n" for record in records)
        manifest = json.loads((batch / "manifest.json").read_text())
        manifest["records"].update({"byte_digest": "sha256:" + hashlib.sha256(raw).hexdigest(), "size": len(raw),
                                    "count": len(records), "type_counts": dict(sorted(Counter(record["record_type"] for record in records).items()))})
        (batch / "records.ndjson").write_bytes(raw)
        (batch / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")

    def test_ndjson_source_is_parsed_once_for_mixed_bindings_and_per_validation_call(self):
        root, batch = self._batch()
        records = [json.loads(line) for line in (batch / "records.ndjson").read_text().splitlines()]
        observation = next(record for record in records if record["record_type"] == "observation")
        source = observation["body"]["bindings"][0]["sources"][0]["artifact_id"]
        observation["body"]["bindings"].append({"field_path": "/body", "claim_basis": "parsed",
                                                   "sources": [{"artifact_id": source, "location": {"json_pointer": "/0", "jsonl_line": 1}}]})
        observation.update(finalize_record(observation))
        self._rewrite(batch, records)
        with mock.patch("open_chat_archive.validation.load_source_ndjson", wraps=load_source_ndjson) as parser:
            validate_batch(batch, archive_root=root)
            self.assertEqual(parser.call_count, 1)
            observation["body"]["bindings"].reverse()
            observation.update(finalize_record(observation))
            self._rewrite(batch, records)
            validate_batch(batch, archive_root=root)
            self.assertEqual(parser.call_count, 2)

    def test_cache_is_per_artifact(self):
        root, batch = self._batch()
        records = [json.loads(line) for line in (batch / "records.ndjson").read_text().splitlines()]
        raw = b'{"synthetic":true}\n'
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        path = "sources/object-" + digest[7:19]
        (root / path).write_bytes(raw)
        identity = identity_input(archive_id="archive", record_type="source_artifact", adapter_namespace="org.open-chat-archive.codex",
                                  identity_scheme="native-id", identity_version="1", source_scope_id="scope", logical_key=digest)
        source = finalize_record({"spec_version": "0.1", "record_type": "source_artifact", "entity_id": entity_identity(identity),
                                  "revision_id": "sha256:" + "0" * 64, "sensitivity": "restricted-unclassified", "identity": identity,
                                  "body": {"archive_id": "archive", "byte_digest": digest, "media_type": "application/x-ndjson", "size": len(raw),
                                           "storage_path": path, "acquisition_method": "local-file"}})
        records.append(source)
        observation = next(record for record in records if record["record_type"] == "observation")
        observation["body"]["bindings"][0]["sources"].append({"artifact_id": source["entity_id"], "location": {"jsonl_line": 1, "json_pointer": "/0"}})
        observation.update(finalize_record(observation))
        manifest = json.loads((batch / "manifest.json").read_text())
        manifest["sources"].append({"path": path, "byte_digest": digest, "media_type": "application/x-ndjson", "size": len(raw)})
        (batch / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
        self._rewrite(batch, records)
        with mock.patch("open_chat_archive.validation.load_source_ndjson", wraps=load_source_ndjson) as parser:
            validate_batch(batch, archive_root=root)
            self.assertEqual(parser.call_count, 2)

    def test_out_of_range_and_malformed_jsonl_remain_failures(self):
        root, batch = self._batch()
        records = [json.loads(line) for line in (batch / "records.ndjson").read_text().splitlines()]
        observation = next(record for record in records if record["record_type"] == "observation")
        observation["body"]["bindings"][0]["sources"][0]["location"]["jsonl_line"] = 3
        observation.update(finalize_record(observation))
        self._rewrite(batch, records)
        with self.assertRaisesRegex(ValidationError, "EVIDENCE_RANGE"):
            validate_batch(batch, archive_root=root)

        root, batch = self._batch()
        records = [json.loads(line) for line in (batch / "records.ndjson").read_text().splitlines()]
        raw = b'{"broken":}\n'
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        path = "sources/object-" + digest[7:19]
        (root / path).write_bytes(raw)
        identity = identity_input(archive_id="archive", record_type="source_artifact", adapter_namespace="org.open-chat-archive.codex",
                                  identity_scheme="native-id", identity_version="1", source_scope_id="scope", logical_key=digest)
        source = finalize_record({"spec_version": "0.1", "record_type": "source_artifact", "entity_id": entity_identity(identity),
                                  "revision_id": "sha256:" + "0" * 64, "sensitivity": "restricted-unclassified", "identity": identity,
                                  "body": {"archive_id": "archive", "byte_digest": digest, "media_type": "application/x-ndjson", "size": len(raw),
                                           "storage_path": path, "acquisition_method": "local-file"}})
        records.append(source)
        observation = next(record for record in records if record["record_type"] == "observation")
        observation["body"]["bindings"][0]["sources"].append({"artifact_id": source["entity_id"], "location": {"jsonl_line": 1}})
        observation.update(finalize_record(observation))
        manifest = json.loads((batch / "manifest.json").read_text())
        manifest["sources"].append({"path": path, "byte_digest": digest, "media_type": "application/x-ndjson", "size": len(raw)})
        (batch / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
        self._rewrite(batch, records)
        with self.assertRaisesRegex(ValidationError, "SOURCE_JSON"):
            validate_batch(batch, archive_root=root)


if __name__ == "__main__":
    unittest.main()
