"""Synthetic regressions for bounded native control and usage records."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from open_chat_archive.batch_writer import AdapterInputError
from open_chat_archive.evidence_projection import project_archive
from open_chat_archive.source_adapters import ClaudeCodeAdapter, CodexAdapter
from open_chat_archive.validation import validate_archive


def line(kind, payload, *, timestamp="synthetic"):
    item = {"timestamp": timestamp, "type": kind}
    if payload is not None:
        item["payload"] = payload
    return item


class NativeControlCompatTests(unittest.TestCase):
    def _conform(self, adapter, raw, name):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name) / "archive"
        source = Path(temp.name) / "source.jsonl"
        source.write_bytes(raw)
        root.mkdir()
        result = adapter.conform(source, root, archive_id="archive", batch_id=name)
        validate_archive(root)
        records = [json.loads(row) for row in (result.batch_dir / "records.ndjson").read_text().splitlines()]
        source_rec = next(rec for rec in records if rec["record_type"] == "source_artifact")
        self.assertEqual(source_rec["body"]["byte_digest"], "sha256:" + hashlib.sha256(raw).hexdigest())
        self.assertEqual((root / source_rec["body"]["storage_path"]).read_bytes(), raw)
        return root, records

    def test_codex_token_usage_record_and_thread_settings_are_source_safe(self):
        raw = b"".join([
            b'{"timestamp":"synthetic","type":"session_meta","payload":{"id":"s"}}\n',
            b'{"timestamp":"synthetic","type":"token_usage_record","payload":{"thread_id":"t","turn_id":"u","session_id":"s","root_turn_id":"r","response_id":"resp","usage":{"input_tokens":7,"output_tokens":3,"total_tokens":10},"turn_token_usage":{"input_tokens":1},"thread_token_usage":{"output_tokens":2}}}\n',
            b'{"timestamp":"synthetic","type":"event_msg","payload":{"type":"thread_settings_applied","thread_id":"t","thread_settings":{"mode":"synthetic"}}}\n',
            b'{"timestamp":"synthetic","type":"response_item","payload":{"type":"message","id":"m","role":"user","content":[{"type":"input_text","text":"synthetic searchable"}]}}\n',
        ])
        root, records = self._conform(CodexAdapter("scope"), raw, "codex")
        self.assertEqual({r["identity"]["logical_key"] for r in records if r["record_type"] == "session"}, {"s"})
        usage = next(r for r in records if r["record_type"] == "event" and r["body"]["kind"] == "usage")
        self.assertEqual(usage["body"]["payload"], {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10})
        self.assertEqual(usage["body"]["correlation_id"], "resp")
        projection = project_archive(root)
        visible = [item["text"] for item in projection["records"] if item["text"]]
        self.assertEqual(visible, ["synthetic searchable"])
        self.assertTrue(next(item for item in projection["records"] if item["text"])["citations"])

    def test_claude_control_cost_rows_preserve_source_and_hide_text(self):
        raw = (b'{"type":"mode","sessionId":"s","mode":"synthetic"}\n'
               b'{"type":"cost-state","sessionId":"s","totalCostUSD":1.2300,"modelUsage":{"dynamicModel":{"costUSD":2.5}}}\n'
               b'{"type":"attachment","sessionId":"s","content":"private synthetic control"}\n'
               b'{"type":"user","sessionId":"s","uuid":"u","message":{"content":"synthetic searchable"}}\n')
        root, records = self._conform(ClaudeCodeAdapter("scope"), raw, "claude")
        self.assertEqual({r["identity"]["logical_key"] for r in records if r["record_type"] == "session"}, {"s"})
        self.assertEqual({r["identity"]["logical_key"] for r in records if r["record_type"] == "message"}, {"u"})
        projected = project_archive(root)
        self.assertEqual([item["text"] for item in projected["records"] if item["text"]], ["synthetic searchable"])
        self.assertIn(b'1.2300', raw)
        self.assertTrue(next(item for item in projected["records"] if item["text"])["citations"])

    def test_bad_json_numbers_duplicate_keys_and_known_bad_variants_rejected(self):
        codex = CodexAdapter("scope")
        claude = ClaudeCodeAdapter("scope")
        cases = [
            (codex, b'{"timestamp":"x","type":"session_meta","payload":{"id":"s","x":NaN}}\n', "CODEX_JSONL"),
            (codex, b'{"timestamp":"x","type":"session_meta","payload":{"id":"s","id":"t"}}\n', "CODEX_JSONL"),
            (codex, b'{"timestamp":"x","type":"session_meta","payload":{"id":"s"}}\n{"timestamp":"x","type":"unlisted","payload":{}}\n', "CODEX_ROLLOUT_VARIANT"),
            (claude, b'{"type":"user","sessionId":"s","uuid":"u","message":{"content":"x","content":"y"}}\n', "CLAUDE_CODE_JSONL"),
            (claude, b'{"type":"user","sessionId":"s","uuid":"u","message":{"content":[{"type":"tool_use","id":"c","name":"t","input":{"n":1.25}}]}}\n', "CLAUDE_CODE_TOOL"),
        ]
        for index, (adapter, raw, code) in enumerate(cases):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as temp:
                source = Path(temp) / "source.jsonl"; source.write_bytes(raw)
                root = Path(temp) / "archive"; root.mkdir()
                with self.assertRaises(AdapterInputError) as caught:
                    adapter.conform(source, root, archive_id="archive", batch_id=f"b{index}")
                self.assertEqual(str(caught.exception), code)


if __name__ == "__main__":
    unittest.main()
