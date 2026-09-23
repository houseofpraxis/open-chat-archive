"""Synthetic evidence-location regressions for the Codex adapter."""
import json
import tempfile
import unittest
from pathlib import Path

from open_chat_archive.source_adapters import CodexAdapter


class CodexEvidenceTests(unittest.TestCase):
    def _conform(self, entries, batch):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        source = Path(temp.name) / "synthetic.jsonl"
        source.write_bytes(b"".join(json.dumps(entry, separators=(",", ":")).encode() + b"\n" for entry in entries))
        root = Path(temp.name) / "archive"
        CodexAdapter("scope").conform(source, root, archive_id="archive", batch_id=batch)
        return [json.loads(line) for line in (root / "batches" / batch / "records.ndjson").read_text().splitlines()]

    @staticmethod
    def _lines(records):
        observations = {record["body"]["subject"]["entity_id"]: record for record in records if record["record_type"] == "observation"}
        return {record["identity"]["logical_key"]: observations[record["entity_id"]]["body"]["bindings"][0]["sources"][0]["location"]["jsonl_line"]
                for record in records if record["record_type"] in {"session", "turn", "message", "event"}}

    def test_direct_semantic_lines_follow_physical_jsonl_positions(self):
        def line(kind, payload):
            return {"timestamp": "synthetic", "type": kind, "payload": payload}

        entries = [
            line("event_msg", {"type": "warning"}),
            line("session_meta", {"id": "session"}),
            line("response_item", {"type": "message", "id": "message", "role": "user", "content": [{"type": "input_text", "text": "synthetic"}]}),
            line("response_item", {"type": "reasoning", "id": "reasoning", "summary": [{"type": "summary_text", "text": "synthetic"}]}),
            line("event_msg", {"type": "task_started", "turn_id": "turn"}),
        ]
        records = self._conform(entries, "batch-a")
        lines = self._lines(records)
        self.assertEqual(lines["session"], 2)
        self.assertEqual(lines["message:message"], 3)
        self.assertEqual(lines["reasoning:reasoning"], 4)
        self.assertEqual(lines["turn"], 5)
        self.assertEqual(lines["turn:started"], 5)

        shifted = self._conform([line("event_msg", {"type": "warning"}), *entries], "batch-b")
        native = {record["identity"]["logical_key"]: record["entity_id"]
                  for record in records if record["record_type"] in {"session", "turn", "message"}}
        shifted_native = {record["identity"]["logical_key"]: record["entity_id"]
                          for record in shifted if record["record_type"] in {"session", "turn", "message"}}
        self.assertEqual(native, shifted_native)


if __name__ == "__main__":
    unittest.main()
