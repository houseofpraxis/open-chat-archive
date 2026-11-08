"""Wholly synthetic Pi v3 adapter tests; no local session files are read."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from open_chat_archive.batch_writer import AdapterInputError
from open_chat_archive.evidence_projection import project_archive
from open_chat_archive.source_adapters import PiV3Adapter, create_adapter
from open_chat_archive.strictjson import SourceJSONNumber
from open_chat_archive.validation import validate_archive


class PiV3AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.source = self.directory / "synthetic-session.jsonl"
        self.root = self.directory / "archive"
        self.adapter = PiV3Adapter("synthetic-scope")

    @staticmethod
    def fixture():
        entries = [
            {"type": "session", "version": 3, "id": "session-native", "timestamp": "2026-11-01T00:00:00Z",
             "cwd": "/synthetic", "parentSession": "parent-session", "extension": {"score": 1.25}},
            {"type": "message", "id": "entry-user", "parentId": None, "timestamp": "2026-11-01T00:00:01Z",
             "message": {"role": "user", "content": "synthetic question"}},
            {"type": "message", "id": "entry-system", "parentId": "entry-user", "timestamp": "2026-11-01T00:00:01Z",
             "message": {"role": "system", "content": "synthetic control instruction"}},
            {"type": "message", "id": "entry-mixed", "parentId": "entry-user", "timestamp": "2026-11-01T00:00:02Z",
             "message": {"role": "assistant", "content": [{"type": "thinking", "thinking": "synthetic reasoning"},
                 {"type": "toolCall", "id": "tool-call", "name": "synthetic_tool", "arguments": {"count": 2}},
                 {"type": "text", "text": "synthetic answer"}]}},
            {"type": "message", "id": "entry-tool-result", "parentId": "entry-mixed", "timestamp": "2026-11-01T00:00:02Z",
             "message": {"role": "toolResult", "toolCallId": "tool-call", "toolName": "synthetic_tool", "isError": True,
                 "content": [{"type": "text", "text": "synthetic tool failure"}]}},
            {"type": "message", "id": "entry-branch", "parentId": "entry-user", "timestamp": "2026-11-01T00:00:03Z",
             "message": {"role": "assistant", "content": "synthetic alternate"}},
            {"type": "usage", "id": "entry-usage", "parentId": "entry-branch", "timestamp": "2026-11-01T00:00:04Z",
             "usage": {"input": 7, "output": 3, "providerMetric": 1.5}},
            {"type": "session_info", "id": "entry-info", "parentId": "entry-branch", "timestamp": "2026-11-01T00:00:05Z",
             "name": "Synthetic named session", "customMeta": {"ratio": 2.75}},
            {"type": "future_extension_entry", "id": "entry-future", "parentId": "entry-usage",
             "timestamp": "2026-11-01T00:00:06Z", "opaque": {"decimal": 5.75}},
        ]
        return b"".join(json.dumps(entry, separators=(",", ":"), ensure_ascii=False).encode() + b"\n" for entry in entries)

    def write_fixture(self, raw=None):
        raw = raw if raw is not None else self.fixture()
        self.source.write_bytes(raw)
        return raw

    def conform(self, raw=None, batch="batch-one"):
        raw = self.write_fixture(raw)
        result = self.adapter.conform(self.source, self.root, archive_id="archive", batch_id=batch)
        records = [json.loads(line) for line in (result.batch_dir / "records.ndjson").read_text().splitlines()]
        validate_archive(self.root)
        return raw, result, records

    def test_tree_native_identity_reasoning_title_source_hash_and_exact_citations(self):
        raw, result, records = self.conform()
        self.assertIs(create_adapter("pi-v3", "scope").__class__, PiV3Adapter)
        sessions = [record for record in records if record["record_type"] == "session"]
        self.assertEqual(sessions[0]["body"]["title"], "Synthetic named session")
        self.assertEqual(sessions[0]["extensions"][self.adapter.namespace]["header"]["id"], "session-native")
        self.assertEqual(sessions[0]["body"]["thread_shape"], "tree")
        messages = [record for record in records if record["record_type"] == "message"]
        self.assertTrue(all("session-native:" in record["identity"]["logical_key"] for record in messages))
        self.assertEqual({m["body"]["visibility"] for m in messages}, {"user_visible", "exported_reasoning", "control"})
        links = [record for record in records if record["record_type"] == "relationship"]
        self.assertEqual(len(links), 2)
        source_record = next(record for record in records if record["record_type"] == "source_artifact")
        self.assertEqual(source_record["body"]["byte_digest"], "sha256:" + hashlib.sha256(raw).hexdigest())
        manifest = json.loads((result.batch_dir / "manifest.json").read_text())
        saved = self.root / manifest["sources"][0]["path"]
        self.assertEqual(saved.read_bytes(), raw)
        physical = raw.splitlines(keepends=True)
        offsets, cursor = [], 0
        for line in physical:
            offsets.append((cursor, cursor + len(line)))
            cursor += len(line)
        projected = project_archive(self.root)
        projected_records = projected["records"]
        user = next(item for item in projected_records if item["record"].get("identity", {}).get("logical_key", "").endswith(":entry-user:text"))
        self.assertEqual(user["text"], "synthetic question")
        citation = user["citations"][0]["sources"][0]["location"]
        self.assertEqual(citation, {"byte_range": {"start": offsets[1][0], "end": offsets[1][1]}, "jsonl_line": 2})
        reasoning = next(item for item in projected_records if item["record"].get("identity", {}).get("logical_key", "").endswith(":entry-mixed:reasoning"))
        self.assertEqual(reasoning["text"], "")
        self.assertEqual(reasoning["record"]["body"]["content"][0]["type"], "reasoning")
        events = [record["body"] for record in records if record["record_type"] == "event"]
        self.assertEqual({event["kind"] for event in events if event.get("correlation_id") == "tool-call"},
            {"tool_invocation", "tool_result"})
        tool_result = next(event for event in events if event["kind"] == "tool_result")
        self.assertEqual(tool_result["payload"]["status"], "error")
        self.assertIn("error", tool_result["payload"])

    def test_new_batch_stability_metadata_and_finite_numbers_never_reach_canonical(self):
        raw, _, first = self.conform()
        _, second_result, second = self.conform(raw, batch="batch-two")
        identity = lambda records: {(record["record_type"], record["identity"]["logical_key"]): record["entity_id"]
                                    for record in records if record["record_type"] in {"session", "message"}}
        self.assertEqual(identity(first), identity(second))
        self.assertEqual(second_result.batch_dir.name, "batch-two")
        session = next(record for record in first if record["record_type"] == "session")
        header = session["extensions"][self.adapter.namespace]["header"]
        self.assertNotIn("extension", header)  # fractional metadata remains in the retained source bytes
        future_source = json.loads(self.source.read_text().splitlines()[-1])
        self.assertEqual(future_source["opaque"]["decimal"], 5.75)
        def walk(value):
            if isinstance(value, dict):
                for child in value.values():
                    yield from walk(child)
            elif isinstance(value, list):
                for child in value:
                    yield from walk(child)
            else:
                yield value
        self.assertFalse(any(isinstance(value, (float, SourceJSONNumber)) for record in first for value in walk(record)))
        usage = next(record for record in first if record["record_type"] == "event" and record["body"]["kind"] == "usage")
        self.assertEqual(usage["body"]["payload"], {"input_tokens": 7, "output_tokens": 3})

    def test_duplicate_native_ids_and_malformed_known_message_rejected(self):
        raw = self.fixture().decode().splitlines()
        duplicate = json.loads(raw[2]); duplicate["id"] = "entry-user"
        raw[2] = json.dumps(duplicate)
        self.source.write_text("\n".join(raw) + "\n")
        with self.assertRaises(AdapterInputError):
            self.adapter.conform(self.source, self.root, archive_id="archive", batch_id="duplicate")
        malformed = json.loads(self.fixture().decode().splitlines()[1]); malformed["id"] = ""
        self.source.write_text(json.dumps(json.loads(self.fixture().decode().splitlines()[0])) + "\n" + json.dumps(malformed) + "\n")
        with self.assertRaises(AdapterInputError):
            self.adapter.conform(self.source, self.root, archive_id="archive", batch_id="malformed")

    def test_unterminated_or_malformed_jsonl_rejected(self):
        raw = self.fixture()
        for invalid in (raw[:-1], raw + b"{broken}\n"):
            self.source.write_bytes(invalid)
            with self.assertRaises(AdapterInputError):
                self.adapter.conform(self.source, self.root, archive_id="archive", batch_id="bad")

    def test_unsupported_session_version_rejected(self):
        rows = self.fixture().splitlines()
        header = json.loads(rows[0]); header["version"] = 4
        self.source.write_bytes(json.dumps(header).encode() + b"\n")
        with self.assertRaises(AdapterInputError):
            self.adapter.conform(self.source, self.root, archive_id="archive", batch_id="wrong-version")


if __name__ == "__main__":
    unittest.main()
