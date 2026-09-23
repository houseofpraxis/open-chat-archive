"""Synthetic-only regression tests for the initial built-in adapters."""
import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from open_chat_archive.batch_writer import AdapterInputError
from open_chat_archive.shape_profile import ProfileError, profile_value
from open_chat_archive.source_adapters import ClaudeAIAdapter, ClaudeCodeAdapter, CodexAdapter, GrokBuildAdapter, OpenWebUIAdapter
from open_chat_archive.strictjson import SourceJSONNumber, StrictJSONError, load_ndjson, load_source_ndjson, loads_bytes, loads_source_bytes
from open_chat_archive.validation import validate_archive


class SourceAdapterTests(unittest.TestCase):
    def _conform(self, adapter, raw, *, jsonl=False, batch="batch-a"):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        source = Path(temp.name) / "synthetic-source"
        source.write_bytes(raw if isinstance(raw, bytes) else json.dumps(raw, separators=(",", ":")).encode())
        root = Path(temp.name) / "archive"
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            result = adapter.conform(source, root, archive_id="archive", batch_id=batch)
        self.assertEqual(output.getvalue() + errors.getvalue(), "")
        self.assertEqual((root / "sources" / json.loads((result.batch_dir / "manifest.json").read_text())["sources"][0]["path"].split("/")[1]).read_bytes(), source.read_bytes())
        records = []
        for line in (result.batch_dir / "records.ndjson").read_text().splitlines(): records.append(json.loads(line))
        source_record = next(record for record in records if record["record_type"] == "source_artifact")
        for observation in (record for record in records if record["record_type"] == "observation"):
            for binding in observation["body"]["bindings"]:
                for evidence in binding["sources"]:
                    self.assertEqual(evidence["artifact_id"], source_record["entity_id"])
                    location = evidence["location"]
                    self.assertEqual(location.get("byte_range"), {"start": 0, "end": len(source.read_bytes())})
                    self.assertTrue("json_pointer" in location or "jsonl_line" in location)
        validate_archive(root)
        return root, source, records

    def test_claude_ai_messages_tools_and_stability(self):
        source = [{"uuid":"chat-a","name":"Synthetic","created_at":"2026-01-01T00:00:00Z","chat_messages":[
            {"uuid":"msg-u","sender":"human","text":"synthetic user"},
            {"uuid":"msg-r","sender":"assistant","content":[{"type":"thinking","thinking":"synthetic reasoning"}]},
            {"uuid":"msg-m","sender":"assistant","content":[{"type":"thinking","thinking":"synthetic reasoning"},{"type":"text","text":"synthetic visible"}]},
            {"uuid":"msg-a","sender":"assistant","content":[{"type":"thinking","thinking":"synthetic reasoning"},{"type":"tool_use","id":"call-a","name":"synthetic_tool","input":{"n":1}}]}]}]
        adapter = ClaudeAIAdapter("scope")
        self.assertTrue(adapter.recognize(profile_value(source)).matched)
        root, path, records = self._conform(adapter, source)
        session = next(record for record in records if record["record_type"] == "session")
        self.assertEqual(session["body"]["created_at"], "2026-01-01T00:00:00Z")
        self.assertEqual(session["body"]["thread_shape"], "linear")
        events = [record for record in records if record["record_type"] == "event"]
        self.assertTrue(all("message_id" in record["body"] for record in events))
        self.assertEqual(len({record["body"]["source_ordinal"] for record in events}), len(events))
        self.assertEqual({r["body"]["role"] for r in records if r["record_type"] == "message"}, {"user", "assistant"})
        messages = {record["identity"]["logical_key"]: record["body"] for record in records if record["record_type"] == "message"}
        self.assertEqual(messages["msg-r"]["visibility"], "exported_reasoning")
        self.assertEqual(messages["msg-m"]["visibility"], "unknown")
        self.assertEqual(messages["msg-a"]["visibility"], "exported_reasoning")
        self.assertIn("tool_invocation", {r["body"]["kind"] for r in records if r["record_type"] == "event"})
        before = {(r["record_type"], r["identity"]["logical_key"]): (r["entity_id"], r["revision_id"]) for r in records if r["record_type"] in {"session", "message"}}
        adapter.conform(path, root, archive_id="archive", batch_id="batch-b")
        all_records = [json.loads(line) for line in (root / "batches" / "batch-b" / "records.ndjson").read_text().splitlines()]
        after = {(r["record_type"], r["identity"]["logical_key"]): (r["entity_id"], r["revision_id"]) for r in all_records if r["record_type"] in {"session", "message"}}
        self.assertEqual(before, after); validate_archive(root)

    def test_claude_ai_alternate_recognition_source_numbers_and_profile_limit(self):
        adapter = ClaudeAIAdapter("scope")
        alternate = {"profile_version":"shape-only-v1", "paths":[
            {"path":"/", "types":{"array":1}}, {"path":"/*", "types":{"object":1}},
            {"path":"/*/k0001", "types":{"object":1}}, {"path":"/*/k0001/k0001", "types":{"string":1}},
            {"path":"/*/k0002", "types":{"array":1}}, {"path":"/*/k0002/*", "types":{"object":1}},
            *[{"path":f"/*/k{index:04d}", "types":{"string":1}} for index in range(3, 8)],
        ]}
        self.assertTrue(adapter.recognize(alternate).matched)
        self.assertFalse(adapter.recognize(profile_value([{"only_array": []}])).matched)

        raw = b'''[{"chat_messages":[{"content":[{"type":"text","text":"synthetic text"},{"type":"thinking","thinking":"synthetic reasoning"},{"type":"code","text":"synthetic code"},{"type":"tool_use","id":"safe-call","name":"synthetic_tool","input":{"safe":[true,1,"value"]}},{"type":"tool_result","tool_use_id":"safe-call","content":{"safe":"result"}},{"type":"tool_use","id":"unsafe-call","name":"synthetic_tool","input":{"decimal":1.25e+2}},{"type":"tool_result","tool_use_id":"unsafe-call","content":{"decimal":2.5}}],"created_at":3.75,"sender":"assistant","uuid":"message-a"},{"created_at":"synthetic-message-time","sender":"human","text":"synthetic user","uuid":"message-b"}],"created_at":4.5,"name":"Synthetic","updated_at":"synthetic-time","uuid":"chat-a"}]'''
        self.assertTrue(adapter.recognize(profile_value(loads_source_bytes(raw))).matched)
        with mock.patch("open_chat_archive.batch_writer.profile_value", wraps=profile_value) as profiler:
            root, source, records = self._conform(adapter, raw)
        self.assertEqual(adapter.MAX_SOURCE_BYTES, 64 * 1024 * 1024)
        self.assertEqual(adapter.PROFILE_MAX_ITEMS, 500_000)
        self.assertEqual(profiler.call_args.kwargs["max_items"], adapter.PROFILE_MAX_ITEMS)
        manifest = json.loads((root / "batches" / "batch-a" / "manifest.json").read_text())
        self.assertEqual((root / manifest["sources"][0]["path"]).read_bytes(), raw)
        session = next(record for record in records if record["record_type"] == "session")
        self.assertEqual(session["extensions"][adapter.namespace], {"updated_at":"synthetic-time"})
        message = next(record for record in records if record["record_type"] == "message" and record["identity"]["logical_key"] == "message-a")
        self.assertNotIn("extensions", message)
        self.assertEqual([part["type"] for part in message["body"]["content"]], ["text", "reasoning", "code"])
        safe_message = next(record for record in records if record["record_type"] == "message" and record["identity"]["logical_key"] == "message-b")
        self.assertEqual(safe_message["extensions"][adapter.namespace], {"created_at":"synthetic-message-time"})
        events = [record["body"] for record in records if record["record_type"] == "event"]
        self.assertEqual({event["correlation_id"] for event in events}, {"safe-call"})
        self.assertEqual({event["kind"] for event in events}, {"tool_invocation", "tool_result"})
        invocation = next(event for event in events if event["kind"] == "tool_invocation")
        result = next(event for event in events if event["kind"] == "tool_result")
        self.assertEqual(invocation["payload"]["arguments"], {"json":{"safe":[True,1,"value"]}})
        self.assertEqual(result["payload"]["output"], {"json":{"safe":"result"}})

        before = {(record["record_type"], record["identity"]["logical_key"]): (record["entity_id"], record["revision_id"])
                  for record in records if record["record_type"] in {"session", "message", "event"}}
        adapter.conform(source, root, archive_id="archive", batch_id="batch-b")
        repeat = [json.loads(line) for line in (root / "batches" / "batch-b" / "records.ndjson").read_text().splitlines()]
        after = {(record["record_type"], record["identity"]["logical_key"]): (record["entity_id"], record["revision_id"])
                 for record in repeat if record["record_type"] in {"session", "message", "event"}}
        self.assertEqual(before, after)
        validate_archive(root)

    def test_claude_code_parent_tools_usage_and_boundary(self):
        entries = [
            {"type":"user","uuid":"u","sessionId":"s","timestamp":"2026-01-01T00:00:00Z","message":{"content":"synthetic"}},
            {"type":"assistant","uuid":"a","parentUuid":"u","sessionId":"s","timestamp":"2026-01-01T00:00:01Z","message":{"content":[{"type":"text","text":"answer"},{"type":"tool_use","id":"call","name":"tool","input":{"x":1}}],"model":"synthetic-model","usage":{"input_tokens":2,"output_tokens":3,"cache_creation_input_tokens":4,"cache_read_input_tokens":5}}},
            {"type":"user","uuid":"r","parentUuid":"a","sessionId":"s","timestamp":"2026-01-01T00:00:02Z","toolUseResult":{"ok":True},"message":{"content":[{"type":"tool_result","tool_use_id":"call","content":"result"}]}},
            {"type":"assistant","uuid":"thinking","parentUuid":"r","sessionId":"s","message":{"content":[{"type":"thinking","thinking":"synthetic reasoning"}]}},
            {"type":"assistant","uuid":"mixed","parentUuid":"thinking","sessionId":"s","message":{"content":[{"type":"thinking","thinking":"synthetic reasoning"},{"type":"text","text":"synthetic visible"}]}},
            {"type":"system","uuid":"c","subtype":"compact_boundary","sessionId":"s","compactMetadata":{"kind":"synthetic"}}
        ]
        raw = b"".join(json.dumps(item, separators=(",", ":")).encode() + b"\n" for item in entries)
        adapter = ClaudeCodeAdapter("scope")
        self.assertTrue(adapter.recognize(profile_value(entries)).matched)
        _, _, records = self._conform(adapter, raw)
        kinds = {r["body"]["kind"] for r in records if r["record_type"] == "event"}
        self.assertTrue({"model_response", "tool_invocation", "tool_result", "usage", "lifecycle"}.issubset(kinds))
        usage = next(r for r in records if r["record_type"] == "event" and r["body"]["kind"] == "usage")["body"]["payload"]
        self.assertEqual((usage["cache_write_tokens"], usage["cache_read_tokens"]), (4, 5))
        messages = {record["identity"]["logical_key"]: record["body"] for record in records if record["record_type"] == "message"}
        self.assertEqual((messages["r"]["role"], messages["r"]["visibility"]), ("tool", "tool_traffic"))
        self.assertEqual(messages["thinking"]["visibility"], "exported_reasoning")
        self.assertEqual(messages["mixed"]["visibility"], "unknown")
        response = next(record for record in records if record["record_type"] == "event" and record["identity"]["logical_key"] == "a:model-response")
        self.assertEqual(response["body"]["payload"]["status"], "unknown")
        self.assertEqual(len([r for r in records if r["record_type"] == "relationship"]), 4)

    def test_claude_code_auxiliary_session_ids_and_recognition(self):
        entries = [
            {"type":"auxiliary-a","metadata":{}},
            {"type":"user","uuid":"u","sessionId":"s","message":{"content":"synthetic"}},
            {"type":"auxiliary-b","metadata":{}},
            {"type":"assistant","uuid":"a","sessionId":"s","message":{"content":"synthetic"}},
            {"type":"auxiliary-c","metadata":{}},
        ]
        raw = b"".join(json.dumps(item, separators=(",", ":")).encode() + b"\n" for item in entries)
        adapter = ClaudeCodeAdapter("scope")
        self.assertTrue(adapter.recognize(profile_value(load_source_ndjson(raw))).matched)
        _, _, records = self._conform(adapter, raw)
        self.assertEqual({record["identity"]["logical_key"] for record in records if record["record_type"] == "message"}, {"u", "a"})

        sparse = {"profile_version":"shape-only-v1", "paths":[
            {"path":"/", "types":{"array":1}},
            {"path":"/*", "types":{"object":1}},
            {"path":"/*/k0003", "types":{"object":1}},
            {"path":"/*/k0003/k0001", "types":{"string":1}},
            {"path":"/*/k0003/k0002", "types":{"string":1}},
        ]}
        dense = {"profile_version":"shape-only-v1", "paths":[
            {"path":"/", "types":{"array":3}},
            {"path":"/*", "types":{"object":3}},
            {"path":"/*/k0001", "types":{"string":3}},
            {"path":"/*/k0003", "types":{"object":3}},
            {"path":"/*/k0003/k0001", "types":{"string":3}},
            {"path":"/*/k0003/k0002", "types":{"string":3}},
            {"path":"/*/k0004", "types":{"array":2}},
        ]}
        mixed = {"profile_version":"shape-only-v1", "paths":[
            {"path":"/", "types":{"array":2}},
            {"path":"/*", "types":{"object":2}},
            {"path":"/*/k0002", "types":{"null":1, "string":1}},
            {"path":"/*/k0003", "types":{"object":2}},
            {"path":"/*/k0003/k0001", "types":{"string":2}},
            {"path":"/*/k0003/k0002", "types":{"string":2}},
            {"path":"/*/k0005", "types":{"boolean":1}},
        ]}
        adapter = ClaudeCodeAdapter("scope")
        self.assertTrue(adapter.recognize(sparse).matched)
        self.assertTrue(adapter.recognize(dense).matched)
        self.assertTrue(adapter.recognize(mixed).matched)
        self.assertFalse(adapter.recognize({"profile_version":"shape-only-v1", "paths":[
            {"path":"/", "types":{"array":1}},
            {"path":"/*", "types":{"object":1}},
            {"path":"/*/k0003", "types":{"object":1}},
            {"path":"/*/k0003/k0001", "types":{"string":1}},
        ]}).matched)
        self.assertFalse(adapter.recognize(profile_value({"unrelated": True})).matched)

    def test_claude_code_session_id_rejections(self):
        adapter = ClaudeCodeAdapter("scope")
        cases = [
            (b'{"type":"user","uuid":"u","message":{"content":"synthetic"}}\n', "CLAUDE_CODE_ENTRY"),
            (b'{"type":"user","uuid":"u","sessionId":"s","message":{"content":"synthetic"}}\n{"type":"assistant","uuid":"a","sessionId":"other","message":{"content":"synthetic"}}\n', "CLAUDE_CODE_MULTI_SESSION"),
            (b'{"type":"auxiliary","sessionId":""}\n', "CLAUDE_CODE_ENTRY"),
            (b'{"type":"auxiliary","sessionId":1}\n', "CLAUDE_CODE_ENTRY"),
            (b'{"type":"user","uuid":"u","sessionId":"s","message":{"content":"synthetic"}}\n{"type":"system","subtype":"compact_boundary"}\n', "CLAUDE_CODE_ENTRY"),
        ]
        for raw, code in cases:
            with self.subTest(code=code), tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / "source"; source.write_bytes(raw)
                with self.assertRaisesRegex(AdapterInputError, code):
                    adapter.conform(source, Path(directory) / "archive", archive_id="archive", batch_id="batch")

    def test_openwebui_standard_legacy_and_rejections(self):
        standard = [{"id":"chat","title":"Synthetic","models":["m"],"chat":{"history":{"currentId":"other","messages":{
            "root":{"id":"root","role":"user","content":"one","parentId":None,"childrenIds":["leaf","other","system","developer","tool","odd"]},
            "leaf":{"id":"leaf","role":"assistant","content":"two","parentId":"root","childrenIds":[],"done":True},
            "other":{"id":"other","role":"assistant","content":"branch","parentId":"root","childrenIds":[]},
            "system":{"id":"system","role":"system","content":"control","parentId":"root","childrenIds":[]},
            "developer":{"id":"developer","role":"developer","content":"control","parentId":"root","childrenIds":[]},
            "tool":{"id":"tool","role":"tool","content":"traffic","parentId":"root","childrenIds":[]},
            "odd":{"id":"odd","role":"synthetic-unknown","content":"unknown","parentId":"root","childrenIds":[]}}}}}]
        adapter = OpenWebUIAdapter("scope")
        self.assertTrue(adapter.recognize(profile_value(standard)).matched)
        _, _, records = self._conform(adapter, standard)
        self.assertEqual(len([r for r in records if r["record_type"] == "message"]), 7)
        self.assertEqual(len([r for r in records if r["record_type"] == "relationship"]), 6)
        self.assertTrue(all("sibling_index" in r["body"] for r in records if r["record_type"] == "relationship"))
        session = next(record for record in records if record["record_type"] == "session")
        self.assertEqual(session["body"]["thread_shape"], "tree")
        self.assertIn("active_message_id", session["body"])
        messages = {record["identity"]["logical_key"].split(":", 1)[1]: record["body"] for record in records if record["record_type"] == "message"}
        self.assertEqual(messages["system"]["visibility"], "control")
        self.assertEqual(messages["developer"]["visibility"], "control")
        self.assertEqual(messages["tool"]["visibility"], "tool_traffic")
        self.assertEqual(messages["odd"]["visibility"], "unknown")
        lifecycle = next(record for record in records if record["record_type"] == "event" and record["body"]["kind"] == "lifecycle")
        self.assertEqual(lifecycle["body"]["payload"]["status"], "unknown")
        legacy = [{"id":"legacy","history":{"currentId":"x","messages":{"x":{"id":"x","role":"user","content":"x","parentId":None,"childrenIds":[],"done":True}}}}]
        self.assertTrue(adapter.recognize(profile_value(legacy)).matched)
        _, _, legacy_records = self._conform(adapter, legacy)
        lifecycle = next(record for record in legacy_records if record["record_type"] == "event" and record["body"]["kind"] == "lifecycle")
        self.assertEqual(lifecycle["body"]["payload"]["status"], "completed")
        bad = [{"id":"bad","chat":{"history":{"currentId":"x","messages":{"x":{"id":"x","role":"user","content":"x","parentId":"x","childrenIds":["x"]}}}}}]
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"; source.write_text(json.dumps(bad))
            with self.assertRaisesRegex(AdapterInputError, "OPENWEBUI_CYCLE"):
                adapter.conform(source, Path(directory) / "archive", archive_id="archive", batch_id="batch")
        self.assertFalse(adapter.recognize(profile_value({"unrelated": True})).matched)

    def test_openwebui_alternate_wrapper_source_numbers_and_ordinal_fallback(self):
        adapter = OpenWebUIAdapter("scope")
        alternate = b'''[{"archived":true,"chat":{"alpha":"synthetic","history":{"currentId":"root","messages":{"root":{"id":"root","role":"user","content":{"safe":"rich","decimal":1.25e+2},"parentId":null,"childrenIds":[],"model":"synthetic-model","context":{"safe":"node","decimal":2.5}}}}},"meta":{"safe":"session","decimal":3e2},"options":{"safe":true}}]'''
        self.assertTrue(adapter.recognize(profile_value(loads_source_bytes(alternate))).matched)
        with mock.patch("open_chat_archive.batch_writer.profile_value", wraps=profile_value) as profiler:
            root, source, records = self._conform(adapter, alternate)
        self.assertEqual(source.read_bytes(), alternate)
        self.assertGreaterEqual(adapter.MAX_SOURCE_BYTES, 128 * 1024 * 1024)
        self.assertEqual(adapter.PROFILE_MAX_ITEMS, 100_000)
        self.assertEqual(profiler.call_args.kwargs["max_items"], adapter.PROFILE_MAX_ITEMS)
        session = next(record for record in records if record["record_type"] == "session")
        session_meta = session["extensions"][adapter.namespace]
        self.assertEqual(session_meta["options"], {"safe": True})
        self.assertNotIn("meta", session_meta)
        message = next(record for record in records if record["record_type"] == "message")
        self.assertEqual(message["body"]["content"], [])
        message_meta = message["extensions"][adapter.namespace]
        self.assertEqual(message_meta["model"], "synthetic-model")
        self.assertNotIn("context", message_meta)
        validate_archive(root)

        fallback = b'''[{"chat":{"history":{"currentId":"root","messages":{"root":{"id":"root","role":"user","content":"synthetic","parentId":null,"childrenIds":[]}}}}}]'''
        fallback_root, fallback_source, fallback_records = self._conform(adapter, fallback)
        first = next(record for record in fallback_records if record["record_type"] == "session")
        adapter.conform(fallback_source, fallback_root, archive_id="archive", batch_id="batch-b")
        later = [json.loads(line) for line in (fallback_root / "batches" / "batch-b" / "records.ndjson").read_text().splitlines()]
        second = next(record for record in later if record["record_type"] == "session")
        self.assertEqual(first["identity"]["logical_key"], "artifact-ordinal-0")
        self.assertEqual(first["identity"], second["identity"])
        self.assertEqual((first["entity_id"], first["revision_id"]), (second["entity_id"], second["revision_id"]))
        validate_archive(fallback_root)

    def test_raw_shape_rejections_are_fixed_code_and_quiet(self):
        cases = [
            (ClaudeAIAdapter("scope"), [{"uuid":"c","chat_messages":[{"uuid":"m","sender":"other","text":"synthetic"}]}], "CLAUDE_AI_SENDER"),
            (ClaudeAIAdapter("scope"), [{"uuid":"c","chat_messages":[{"uuid":"m","sender":"assistant","content":"synthetic"}]}], "CLAUDE_AI_CONTENT"),
            (ClaudeAIAdapter("scope"), [{"uuid":"c","chat_messages":[{"uuid":"m","sender":"assistant","content":[{"type":"tool_use","id":"","name":"synthetic"}]}]}], "CLAUDE_AI_TOOL"),
            (ClaudeCodeAdapter("scope"), b'{"type":"assistant"}\n', "CLAUDE_CODE_ENTRY"),
            (OpenWebUIAdapter("scope"), [{"id":"c","history":{"currentId":"missing","messages":{}}}], "OPENWEBUI_CURRENT"),
        ]
        for adapter, payload, code in cases:
            with self.subTest(code=code), tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / "source"
                source.write_bytes(payload if isinstance(payload, bytes) else json.dumps(payload).encode())
                output, errors = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                    with self.assertRaisesRegex(AdapterInputError, code):
                        adapter.conform(source, Path(directory) / "archive", archive_id="archive", batch_id="batch")
                self.assertEqual(output.getvalue() + errors.getvalue(), "")

    def test_codex_rollout_mapping_usage_compaction_and_stability(self):
        def line(type_, payload, ordinal=None):
            result = {"timestamp":"2026-01-01T00:00:00Z", "type":type_, "payload":payload}
            if ordinal is not None: result["ordinal"] = ordinal
            return result
        entries = [
            line("session_meta", {"id":"thread", "session_id":"thread", "timestamp":"2026-01-01T00:00:00Z", "cli_version":"0.147.0", "originator":"synthetic", "source":"cli", "model_provider":"SyntheticProvider"}),
            line("turn_context", {"turn_id":"turn", "model":"synthetic-model"}, 1),
            line("event_msg", {"type":"task_started", "turn_id":"turn"}),
            line("response_item", {"type":"message", "id":"u", "role":"user", "content":[{"type":"input_text", "text":"synthetic input"}]}),
            line("response_item", {"type":"reasoning", "id":"r", "summary":[{"type":"summary_text", "text":"synthetic reasoning"}], "content":[{"type":"reasoning_text", "text":"synthetic detail"}]}),
            line("response_item", {"type":"message", "id":"a", "role":"assistant", "phase":"final_answer", "internal_chat_message_metadata_passthrough":{"turn_id":"turn"}, "content":[{"type":"output_text", "text":"synthetic output"}, {"type":"input_image", "image_url":"synthetic"}]}),
            line("response_item", {"type":"function_call", "id":"fc", "call_id":"call", "name":"synthetic_tool", "arguments":"{\"x\":1}"}),
            line("response_item", {"type":"function_call_output", "id":"fo", "call_id":"call", "output":"synthetic result"}),
            line("event_msg", {"type":"exec_command_begin", "call_id":"exec", "command":["synthetic"]}),
            line("event_msg", {"type":"exec_command_end", "call_id":"exec", "formatted_output":"synthetic exec result", "status":"completed"}),
            line("event_msg", {"type":"raw_response_completed", "response_id":"response", "token_usage":{"input_tokens":2,"cached_input_tokens":1,"cache_write_input_tokens":3,"output_tokens":4,"reasoning_output_tokens":5,"total_tokens":6}}),
            line("event_msg", {"type":"token_count", "info":{"total_token_usage":{"input_tokens":7,"cached_input_tokens":2,"cache_write_input_tokens":3,"output_tokens":4,"reasoning_output_tokens":5,"total_tokens":9},"last_token_usage":{"input_tokens":1,"cached_input_tokens":0,"cache_write_input_tokens":0,"output_tokens":1,"reasoning_output_tokens":0,"total_tokens":2}}, "rate_limits":{"primary":{"used_percent":1.25}}}),
            line("compacted", {"message":"synthetic compact", "window_id":"window", "previous_window_id":"previous"}),
            line("event_msg", {"type":"context_compacted"}),
            line("event_msg", {"type":"task_complete", "turn_id":"turn", "error":None}),
        ]
        raw = b"".join(json.dumps(item, separators=(",", ":")).encode() + b"\n" for item in entries)
        adapter = CodexAdapter("scope")
        self.assertTrue(adapter.recognize(profile_value(load_source_ndjson(raw))).matched)
        root, path, records = self._conform(adapter, raw)
        self.assertEqual({r["identity"]["logical_key"] for r in records if r["record_type"] == "turn"}, {"turn"})
        messages = {r["identity"]["logical_key"]: r["body"] for r in records if r["record_type"] == "message"}
        self.assertEqual(messages["message:u"]["role"], "user")
        self.assertEqual(messages["reasoning:r"]["visibility"], "exported_reasoning")
        kinds = {r["body"]["kind"] for r in records if r["record_type"] == "event"}
        self.assertTrue({"model_response", "tool_invocation", "tool_result", "usage", "lifecycle"}.issubset(kinds))
        keys = {r["identity"]["logical_key"] for r in records if r["record_type"] == "event"}
        self.assertIn("function-call:call", keys); self.assertIn("function-call-output:call", keys)
        self.assertIn("exec-begin:exec", keys); self.assertIn("exec-end:exec", keys)
        self.assertIn("raw-usage:response", keys)
        assistant = next(r for r in records if r["identity"]["logical_key"] == "message:a")
        self.assertEqual(assistant["extensions"][adapter.namespace]["timestamp"], "2026-01-01T00:00:00Z")
        usage = [r["body"]["payload"] for r in records if r["record_type"] == "event" and r["body"]["kind"] == "usage"]
        self.assertTrue(any(item.get("cache_read_tokens") == 1 and item.get("reasoning_tokens") == 5 for item in usage))
        before = {(r["record_type"], r["identity"]["logical_key"]): (r["entity_id"], r["revision_id"]) for r in records if r["record_type"] in {"session", "turn", "message"}}
        adapter.conform(path, root, archive_id="archive", batch_id="batch-b")
        later = [json.loads(x) for x in (root / "batches" / "batch-b" / "records.ndjson").read_text().splitlines()]
        after = {(r["record_type"], r["identity"]["logical_key"]): (r["entity_id"], r["revision_id"]) for r in later if r["record_type"] in {"session", "turn", "message"}}
        self.assertEqual(before, after)
        warning = {"timestamp":"2026-01-01T00:00:00Z","type":"event_msg","payload":{"type":"warning"}}
        shifted = b"".join(json.dumps(item, separators=(",", ":")).encode() + b"\n" for item in [warning, *entries])
        shifted_records = self._conform(adapter, shifted, batch="batch-c")[2]
        native = {"session", "turn", "message"}
        left = {(r["record_type"], r["identity"]["logical_key"]): r["entity_id"] for r in records if r["record_type"] in native}
        right = {(r["record_type"], r["identity"]["logical_key"]): r["entity_id"] for r in shifted_records if r["record_type"] in native}
        self.assertTrue(set(left) <= set(right))
        self.assertEqual(left, {key: right[key] for key in left})
        for key in ("function-call:call", "exec-end:exec"):
            self.assertEqual(next(r["entity_id"] for r in records if r["identity"]["logical_key"] == key),
                             next(r["entity_id"] for r in shifted_records if r["identity"]["logical_key"] == key))

    def test_codex_recognition_fallback_and_quiet_rejections(self):
        adapter = CodexAdapter("scope")
        no_ordinal = {"profile_version":"shape-only-v1", "paths":[
            {"path":"/", "types":{"array":1}}, {"path":"/*", "types":{"object":1}},
            {"path":"/*/k0001", "types":{"object":1}}, {"path":"/*/k0001/k0001", "types":{"string":1}},
            {"path":"/*/k0002", "types":{"string":1}}, {"path":"/*/k0003", "types":{"string":1}}]}
        mixed = {"profile_version":"shape-only-v1", "paths":[
            {"path":"/", "types":{"array":2}}, {"path":"/*", "types":{"object":2}},
            {"path":"/*/k0001", "types":{"integer":1}}, {"path":"/*/k0002", "types":{"object":1}},
            {"path":"/*/k0002/k0001", "types":{"string":1}}, {"path":"/*/k0003", "types":{"string":2}}, {"path":"/*/k0004", "types":{"string":1}}]}
        self.assertTrue(adapter.recognize(no_ordinal).matched); self.assertTrue(adapter.recognize(mixed).matched)
        self.assertFalse(adapter.recognize(profile_value([{"unrelated":{}, "one":"x", "two":"y"}])).matched)
        base = b'{"timestamp":"synthetic","type":"session_meta","payload":{"id":"s"}}\n'
        cases = [
            (base + b'{"timestamp":"synthetic","type":"session_meta","payload":{"id":"s"}}\n', "CODEX_SESSION_META"),
            (b'{"timestamp":"synthetic","type":"session_meta","payload":{"id":"s"},"ordinal":true}\n', "CODEX_LINE"),
            (base + b'{"timestamp":"synthetic","type":"response_item","payload":{"type":"future"}}\n', "CODEX_RESPONSE_VARIANT"),
            (base + b'{"timestamp":"synthetic","type":"event_msg","payload":{"type":"future"}}\n', "CODEX_EVENT_VARIANT"),
        ]
        for raw, code in cases:
            with self.subTest(code=code), tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / "source"; source.write_bytes(raw)
                output, errors = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                    with self.assertRaisesRegex(AdapterInputError, code): adapter.conform(source, Path(directory) / "archive", archive_id="archive", batch_id="batch")
                self.assertEqual(output.getvalue() + errors.getvalue(), "")

    def test_codex_bounded_profile_above_generic_default_conforms_and_validates(self):
        def line(type_, payload):
            return {"timestamp":"2026-01-01T00:00:00Z", "type":type_, "payload":payload}

        entries = [line("session_meta", {"id":"thread", "session_id":"thread"})]
        entries.extend(line("event_msg", {"type":"warning"}) for _ in range(2_001))
        adapter = CodexAdapter("scope")
        with self.assertRaisesRegex(ProfileError, "structural node limit exceeded"):
            profile_value(entries)
        profile_value(entries, max_items=adapter.PROFILE_MAX_ITEMS)
        raw = b"".join(json.dumps(item, separators=(",", ":")).encode() + b"\n" for item in entries)
        _, _, records = self._conform(adapter, raw)
        self.assertEqual({record["record_type"] for record in records},
                         {"source_artifact", "session", "observation", "import_batch"})

    def test_codex_session_identity_aliases_and_statuses(self):
        adapter = CodexAdapter("scope")
        distinct = b'{"timestamp":"synthetic","type":"session_meta","payload":{"id":"thread","session_id":"session"}}\n'
        _, _, records = self._conform(adapter, distinct)
        self.assertEqual({r["identity"]["logical_key"] for r in records if r["record_type"] == "session"}, {"session"})
        legacy = b'{"timestamp":"synthetic","type":"session_meta","payload":{"id":"legacy"}}\n'
        _, _, legacy_records = self._conform(adapter, legacy)
        self.assertEqual({r["identity"]["logical_key"] for r in legacy_records if r["record_type"] == "session"}, {"legacy"})
        alias = b'{"timestamp":"synthetic","type":"session_meta","payload":{"id":"s"}}\n{"timestamp":"synthetic","type":"response_item","payload":{"type":"compaction_summary","encrypted_content":"synthetic"}}\n'
        self._conform(adapter, alias)
        compact = b'{"timestamp":"synthetic","type":"session_meta","payload":{"id":"s"}}\n{"timestamp":"synthetic","type":"compacted","payload":{"message":"summary","window_id":3}}\n'
        _, _, compact_records = self._conform(adapter, compact)
        compact_event = next(r for r in compact_records if r["record_type"] == "event")
        self.assertTrue(compact_event["identity"].get("fallback"))
        self.assertEqual(compact_event["extensions"][adapter.namespace]["window_number"], 3)
        self.assertNotIn("window_id", compact_event["extensions"][adapter.namespace])
        namespaced = b'{"timestamp":"synthetic","type":"session_meta","payload":{"id":"s"}}\n{"timestamp":"synthetic","type":"response_item","payload":{"type":"function_call","call_id":"c","name":"tool","namespace":"a.","arguments":"{}"}}\n'
        _, _, named = self._conform(adapter, namespaced)
        invocation = next(r for r in named if r["record_type"] == "event")
        self.assertEqual(invocation["body"]["payload"]["tool_namespace"], adapter.namespace + ".tools")
        self.assertEqual(invocation["extensions"][adapter.namespace]["source_namespace"], "a.")
        mcp = b'{"timestamp":"synthetic","type":"session_meta","payload":{"id":"s"}}\n{"timestamp":"synthetic","type":"event_msg","payload":{"type":"mcp_tool_call_end","call_id":"m","result":{"Ok":{"is_error":false}}}}\n'
        _, _, mcp_records = self._conform(adapter, mcp)
        self.assertEqual(next(r["body"]["payload"]["status"] for r in mcp_records if r["record_type"] == "event"), "ok")
        mcp_err = b'{"timestamp":"synthetic","type":"session_meta","payload":{"id":"s"}}\n{"timestamp":"synthetic","type":"event_msg","payload":{"type":"mcp_tool_call_end","call_id":"m","result":{"Err":"synthetic"}}}\n'
        _, _, mcp_err_records = self._conform(adapter, mcp_err)
        self.assertEqual(next(r["body"]["payload"]["status"] for r in mcp_err_records if r["record_type"] == "event"), "error")
        duplicate = b'{"timestamp":"synthetic","type":"session_meta","payload":{"id":"s"}}\n{"timestamp":"synthetic","type":"response_item","payload":{"type":"message","id":"dup","role":"user","content":[]}}\n{"timestamp":"synthetic","type":"response_item","payload":{"type":"reasoning","id":"dup","summary":[]}}\n'
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"; source.write_bytes(duplicate)
            with self.assertRaisesRegex(AdapterInputError, "CODEX_DUPLICATE_ITEM"):
                adapter.conform(source, Path(directory) / "archive", archive_id="archive", batch_id="batch")

    def test_grok_build_mapping_usage_compaction_and_stability(self):
        def envelope(method, session, tag, update=None, meta=None, timestamp=1):
            params = {"sessionId": session, "update": {"sessionUpdate": tag, **(update or {})}}
            if meta is not None:
                params["_meta"] = meta
            return {"timestamp": timestamp, "method": method, "params": params}
        entries = [
            envelope("session/update", "s", "user_message_chunk", {"content": {"type": "text", "text": "synthetic user"}}, {"eventId": "u1"}),
            envelope("session/update", "s", "agent_thought_chunk", {"content": {"type": "text", "text": "synthetic reasoning"}}, {"eventId": "t1"}),
            envelope("session/update", "s", "agent_message_chunk", {"content": {"type": "text", "text": "synthetic assistant"}}, {"eventId": "a1"}),
            envelope("session/update", "s", "tool_call", {"toolCallId": "call", "title": "Read", "kind": "read", "status": "pending", "rawInput": {"n": 1}}),
            envelope("session/update", "s", "tool_call_update", {"toolCallId": "call", "status": "completed", "rawOutput": {"ok": True}}),
            envelope("_x.ai/session/update", "s", "turn_completed", {"prompt_id": "p1", "stop_reason": "end_turn", "usage": {"inputTokens": 2, "outputTokens": 3, "cachedReadTokens": 1, "reasoningTokens": 4}}),
            envelope("_x.ai/session/update", "s", "auto_compact_completed", {"tokens_after": 1}),
            envelope("session/update", "s", "plan", {"entries": []}),
        ]
        raw = b"".join(json.dumps(item, separators=(",", ":")).encode() + b"\n" for item in entries)
        adapter = GrokBuildAdapter("scope")
        self.assertTrue(adapter.recognize(profile_value(load_source_ndjson(raw))).matched)
        root, path, records = self._conform(adapter, raw)
        self.assertEqual({r["identity"]["logical_key"] for r in records if r["record_type"] == "session"}, {"s"})
        messages = {r["identity"]["logical_key"]: r["body"] for r in records if r["record_type"] == "message"}
        self.assertEqual(messages["user:u1"]["role"], "user")
        self.assertEqual(messages["thought:t1"]["visibility"], "exported_reasoning")
        self.assertEqual(messages["agent:a1"]["visibility"], "user_visible")
        kinds = {r["body"]["kind"] for r in records if r["record_type"] == "event"}
        self.assertTrue({"tool_invocation", "tool_result", "usage", "lifecycle"}.issubset(kinds))
        usage = next(r["body"]["payload"] for r in records if r["record_type"] == "event" and r["body"]["kind"] == "usage")
        self.assertEqual((usage.get("cache_read_tokens"), usage.get("reasoning_tokens")), (1, 4))
        invocation = next(r for r in records if r["identity"]["logical_key"] == "tool:call")
        self.assertEqual(invocation["body"]["payload"]["tool_namespace"], adapter.namespace + ".tools")
        before = {(r["record_type"], r["identity"]["logical_key"]): (r["entity_id"], r["revision_id"]) for r in records if r["record_type"] in {"session", "turn", "message"}}
        adapter.conform(path, root, archive_id="archive", batch_id="batch-b")
        later = [json.loads(x) for x in (root / "batches" / "batch-b" / "records.ndjson").read_text().splitlines()]
        after = {(r["record_type"], r["identity"]["logical_key"]): (r["entity_id"], r["revision_id"]) for r in later if r["record_type"] in {"session", "turn", "message"}}
        self.assertEqual(before, after)
        warning = {"timestamp": 0, "method": "session/update", "params": {"sessionId": "s", "update": {"sessionUpdate": "available_commands_update", "availableCommands": []}}}
        shifted = b"".join(json.dumps(item, separators=(",", ":")).encode() + b"\n" for item in [warning, *entries])
        shifted_records = self._conform(adapter, shifted, batch="batch-c")[2]
        native = {"session", "turn", "message"}
        left = {(r["record_type"], r["identity"]["logical_key"]): r["entity_id"] for r in records if r["record_type"] in native}
        right = {(r["record_type"], r["identity"]["logical_key"]): r["entity_id"] for r in shifted_records if r["record_type"] in native}
        self.assertTrue(set(left) <= set(right))
        self.assertEqual(left, {key: right[key] for key in left})

    def test_grok_build_bounded_profile_above_generic_default_conforms_and_validates(self):
        def envelope(tag="plan"):
            return {"timestamp": 1, "method": "session/update",
                    "params": {"sessionId": "s", "update": {"sessionUpdate": tag}}}
        entries = [envelope() for _ in range(2_001)]
        adapter = GrokBuildAdapter("scope")
        with self.assertRaisesRegex(ProfileError, "structural node limit exceeded"):
            profile_value(entries)
        profile_value(entries, max_items=adapter.PROFILE_MAX_ITEMS)
        raw = b"".join(json.dumps(item, separators=(",", ":")).encode() + b"\n" for item in entries)
        _, _, records = self._conform(adapter, raw)
        self.assertEqual({record["record_type"] for record in records},
                         {"source_artifact", "session", "observation", "import_batch"})

    def test_grok_build_recognition_fallback_and_quiet_rejections(self):
        adapter = GrokBuildAdapter("scope")
        envelope = {"profile_version": "shape-only-v1", "paths": [
            {"path": "/", "types": {"array": 1}}, {"path": "/*", "types": {"object": 1}},
            {"path": "/*/k0001", "types": {"string": 1}}, {"path": "/*/k0002", "types": {"object": 1}},
            {"path": "/*/k0002/k0001", "types": {"string": 1}}, {"path": "/*/k0002/k0002", "types": {"object": 1}},
            {"path": "/*/k0003", "types": {"integer": 1}}]}
        legacy = {"profile_version": "shape-only-v1", "paths": [
            {"path": "/", "types": {"array": 1}}, {"path": "/*", "types": {"object": 1}},
            {"path": "/*/k0001", "types": {"string": 1}}, {"path": "/*/k0002", "types": {"object": 1}},
            {"path": "/*/k0002/k0001", "types": {"string": 1}}]}
        self.assertTrue(adapter.recognize(envelope).matched)
        self.assertTrue(adapter.recognize(legacy).matched)
        self.assertFalse(adapter.recognize(profile_value([{"unrelated": {}, "one": "x", "two": "y"}])).matched)
        ok = b'{"timestamp":1,"method":"session/update","params":{"sessionId":"s","update":{"sessionUpdate":"user_message_chunk","content":{"type":"text","text":"synthetic"}}}}\n'
        self._conform(adapter, ok)
        unknown = b'{"timestamp":1,"method":"session/update","params":{"sessionId":"s","update":{"sessionUpdate":"future_tag"}}}\n'
        self._conform(adapter, unknown)
        cases = [
            (b'{"timestamp":true,"method":"session/update","params":{"sessionId":"s","update":{"sessionUpdate":"plan"}}}\n', "GROK_LINE"),
            (ok + b'{"timestamp":1,"method":"session/update","params":{"sessionId":"other","update":{"sessionUpdate":"plan"}}}\n', "GROK_SESSION"),
            (b'{"timestamp":1,"method":"other/update","params":{"sessionId":"s","update":{"sessionUpdate":"plan"}}}\n', "GROK_METHOD"),
        ]
        for raw, code in cases:
            with self.subTest(code=code), tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / "source"; source.write_bytes(raw)
                output, errors = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                    with self.assertRaisesRegex(AdapterInputError, code):
                        adapter.conform(source, Path(directory) / "archive", archive_id="archive", batch_id="batch")
                self.assertEqual(output.getvalue() + errors.getvalue(), "")

    def test_source_decimal_parser_isolated_from_canonical_parser(self):
        parsed = load_source_ndjson(b'{"n":1.25e+2}\n')
        self.assertIsInstance(parsed[0]["n"], SourceJSONNumber)
        number_path = next(item for item in profile_value(parsed)["paths"] if item["path"] == "/*/k0001")
        self.assertEqual(number_path["types"], {"number": 1})
        with self.assertRaisesRegex(StrictJSONError, "float"): load_ndjson(b'{"n":1.25}\n')
        with self.assertRaisesRegex(StrictJSONError, "float"): loads_bytes(b'{"n":1.25}')
        with self.assertRaisesRegex(StrictJSONError, "nonfinite"): load_source_ndjson(b'{"n":NaN}\n')


if __name__ == "__main__": unittest.main()
