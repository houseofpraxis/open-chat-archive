"""Claude Code 2.1.251 JSONL synthetic-baseline adapter."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..adapter import BatchResult, FingerprintResult
from ..batch_writer import BatchWriter, read_regular, reject
from ..projection import canonical_datetime, canonical_model
from ..strictjson import StrictJSONError, load_ndjson


class ClaudeCodeAdapter:
    namespace = "org.open-chat-archive.claude-code"
    version = "0.1.2"

    def __init__(self, source_scope_id: str): self.source_scope_id = source_scope_id

    def recognize(self, profile: dict[str, Any]) -> FingerprintResult:
        paths = {item.get("path"): item.get("types", {}) for item in profile.get("paths", []) if isinstance(item, dict)}
        def string(path: str) -> bool: return bool(paths.get(path, {}).get("string"))
        def object_(path: str) -> bool: return bool(paths.get(path, {}).get("object"))

        # JSONL is profiled as an array of objects.  These are the bounded,
        # opaque message-like topologies observed for Claude Code: the local
        # k0003 object with two nested strings, plus the earlier k0001
        # message-object layouts with their direct scalar evidence.  Aliases
        # are structural coordinates only; no raw keys or values are read.
        message_like = (
            (object_("/*/k0003") and string("/*/k0003/k0001") and string("/*/k0003/k0002"))
            or (object_("/*/k0001") and string("/*/k0001/k0001") and string("/*/k0002") and string("/*/k0005"))
            or (object_("/*/k0001") and string("/*/k0001/k0001") and string("/*/k0002") and string("/*/k0003") and string("/*/k0004"))
        )
        matched = profile.get("profile_version") == "shape-only-v1" and bool(paths.get("/", {}).get("array")) and object_("/*") and message_like
        return FingerprintResult(bool(matched), "claude-code.2.1.251.jsonl" if matched else None)

    @staticmethod
    def _parts(content: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
        if isinstance(content, str): return [{"type": "text", "text": content}], [], False
        if not isinstance(content, list): reject("CLAUDE_CODE_CONTENT")
        parts: list[dict[str, Any]] = []; calls: list[dict[str, Any]] = []
        tool_result_only = bool(content)
        for block in content:
            if not isinstance(block, dict) or not isinstance(block.get("type"), str): reject("CLAUDE_CODE_CONTENT")
            kind = block["type"]
            tool_result_only = tool_result_only and kind == "tool_result"
            if kind in {"text", "thinking", "reasoning"}:
                text = block.get("text", block.get("thinking"))
                if not isinstance(text, str): reject("CLAUDE_CODE_CONTENT")
                parts.append({"type": "reasoning" if kind in {"thinking", "reasoning"} else "text", "text": text})
            elif kind == "tool_use":
                if not all(isinstance(block.get(key), str) and block[key] for key in ("id", "name")) or "input" not in block: reject("CLAUDE_CODE_TOOL")
                calls.append({"call_id": block["id"], "name": block["name"], "input": block["input"]})
            elif kind == "tool_result":
                call_id = block.get("tool_use_id")
                if not isinstance(call_id, str) or not call_id or "content" not in block: reject("CLAUDE_CODE_TOOL")
                calls.append({"result": True, "call_id": call_id, "content": block["content"], "error": block.get("is_error") is True})
        return parts, calls, tool_result_only

    @staticmethod
    def _visibility(parts: list[dict[str, Any]]) -> str:
        if not any(part["type"] == "reasoning" for part in parts): return "user_visible"
        if all(part["type"] == "reasoning" for part in parts): return "exported_reasoning"
        return "unknown"

    def conform(self, source: Path, archive_root: Path, *, archive_id: str, batch_id: str) -> BatchResult:
        raw = read_regular(source)
        try: entries = load_ndjson(raw)
        except StrictJSONError: reject("CLAUDE_CODE_JSONL")
        if not entries: reject("CLAUDE_CODE_EMPTY")
        session_ids: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("type"), str) or not entry["type"]:
                reject("CLAUDE_CODE_ENTRY")
            if "sessionId" in entry:
                candidate = entry["sessionId"]
                if not isinstance(candidate, str) or not candidate:
                    reject("CLAUDE_CODE_ENTRY")
                session_ids.add(candidate)
        if not session_ids:
            reject("CLAUDE_CODE_ENTRY")
        if len(session_ids) != 1:
            reject("CLAUDE_CODE_MULTI_SESSION")
        session_id = next(iter(session_ids))
        writer = BatchWriter(namespace=self.namespace, version=self.version, source_scope_id=self.source_scope_id,
            archive_id=archive_id, batch_id=batch_id, archive_root=archive_root, raw=raw,
            media_type="application/x-ndjson", fingerprint="claude-code.2.1.251.jsonl", parsed=entries)
        thread_shape = "tree" if any(isinstance(entry, dict) and entry.get("parentUuid") for entry in entries) else "linear"
        session = writer.record("session", session_id, {"archive_id": archive_id, "thread_shape": thread_shape},
            location={"jsonl_line": 1})
        records: list[dict[str, Any]] = [session]; emitted: dict[str, dict[str, Any]] = {}; parent_links: list[tuple[str, str, int]] = []
        event_ordinal = 0
        def take_event() -> int:
            nonlocal event_ordinal
            value = event_ordinal; event_ordinal += 1; return value
        for ordinal, entry in enumerate(entries):
            line = {"jsonl_line": ordinal + 1}
            entry_type = entry["type"]
            if entry_type in {"user", "assistant"}:
                if entry.get("sessionId") != session_id:
                    reject("CLAUDE_CODE_ENTRY")
                if not isinstance(entry.get("uuid"), str) or not entry["uuid"] or not isinstance(entry.get("message"), dict): reject("CLAUDE_CODE_MESSAGE")
                message_data = entry["message"]
                if "content" not in message_data: reject("CLAUDE_CODE_MESSAGE")
                parts, calls, tool_result_only = self._parts(message_data["content"])
                uid = entry["uuid"]
                if uid in emitted: reject("CLAUDE_CODE_DUPLICATE_ID")
                meta = {key: message_data[key] for key in ("model",) if key in message_data}
                if "timestamp" in entry: meta["timestamp"] = entry["timestamp"]
                role = "tool" if entry_type == "user" and tool_result_only else entry_type
                visibility = "tool_traffic" if role == "tool" else self._visibility(parts)
                message_body: dict[str, Any] = {"session_id": session["entity_id"], "source_ordinal": ordinal,
                    "role": role, "visibility": visibility, "content": parts}
                observed = canonical_datetime(entry.get("timestamp"))
                if observed: message_body["created_at"] = observed
                model = canonical_model(message_data.get("model"), provider="Anthropic")
                if model: message_body["model"] = model
                message = writer.record("message", uid, message_body, extensions={self.namespace: meta} if meta else None,
                    location=line, mapped_destinations=["/body"])
                emitted[uid] = message; records.append(message)
                parent = entry.get("parentUuid")
                if parent is not None:
                    if not isinstance(parent, str): reject("CLAUDE_CODE_PARENT")
                    parent_links.append((parent, uid, ordinal))
                for call_index, call in enumerate(calls):
                    if call.get("result"):
                        payload = {"call_id": call["call_id"], "status": "error" if call["error"] else "unknown", "output": {"json": call["content"]}}
                        event = {"session_id": session["entity_id"], "source_ordinal": take_event(), "kind": "tool_result",
                            "correlation_id": call["call_id"], "message_id": message["entity_id"], "payload": payload}
                    else:
                        payload = {"tool_namespace": "org.open-chat-archive.claude-code-tools", "tool_name": call["name"], "call_id": call["call_id"], "arguments": {"json": call["input"]}}
                        event = {"session_id": session["entity_id"], "source_ordinal": take_event(), "kind": "tool_invocation",
                            "correlation_id": call["call_id"], "message_id": message["entity_id"], "payload": payload}
                    event_ext = {self.namespace: {"toolUseResult": entry["toolUseResult"]}} if call.get("result") and "toolUseResult" in entry else None
                    records.append(writer.record("event", f"{uid}:tool:{call_index}", event, extensions=event_ext, location=line))
                usage = message_data.get("usage")
                if usage is not None:
                    if not isinstance(usage, dict): reject("CLAUDE_CODE_USAGE")
                    source_metrics = {
                        "input_tokens": "input_tokens",
                        "output_tokens": "output_tokens",
                        "cache_creation_input_tokens": "cache_write_tokens",
                        "cache_read_input_tokens": "cache_read_tokens",
                    }
                    mapped: dict[str, int] = {}
                    for source_name, destination in source_metrics.items():
                        if source_name not in usage:
                            continue
                        value = usage[source_name]
                        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                            reject("CLAUDE_CODE_USAGE")
                        mapped[destination] = value
                    if mapped:
                        records.append(writer.record("event", f"{uid}:usage", {"session_id": session["entity_id"], "source_ordinal": take_event(),
                            "kind": "usage", "message_id": message["entity_id"], "payload": mapped}, location=line))
                model_name = message_data.get("model")
                if entry_type == "assistant" and isinstance(model_name, str) and model_name:
                    records.append(writer.record("event", f"{uid}:model-response", {"session_id": session["entity_id"], "source_ordinal": take_event(),
                        "kind": "model_response", "message_id": message["entity_id"],
                        "payload": {"provider": "Anthropic", "model_name": model_name, "status": "unknown", "response_refs": [message["entity_id"]]}}, location=line))
            elif entry_type == "system" and entry.get("subtype") == "compact_boundary":
                if entry.get("sessionId") != session_id:
                    reject("CLAUDE_CODE_ENTRY")
                native_uuid = entry.get("uuid")
                if native_uuid is not None and (not isinstance(native_uuid, str) or not native_uuid):
                    reject("CLAUDE_CODE_COMPACT")
                fallback = native_uuid is None
                key = native_uuid if native_uuid is not None else f"compact:{ordinal}"
                ext = {name: entry[name] for name in ("logicalParentUuid", "compactMetadata", "timestamp") if name in entry}
                records.append(writer.record("event", f"{key}:compact", {"session_id": session["entity_id"], "source_ordinal": take_event(),
                    "kind": "lifecycle", "payload": {"status": "completed", "detail": "compact_boundary"}}, extensions={self.namespace: ext} if ext else None,
                    scheme="artifact-local" if fallback else "native-id", fallback=fallback, location=line))
            # Other local entry vocabularies are retained losslessly by source_artifact.
        for parent, child, ordinal in parent_links:
            if parent in emitted:
                records.append(writer.record("relationship", f"{parent}>{child}", {"from_id": emitted[parent]["entity_id"], "to_id": emitted[child]["entity_id"], "kind": "parent_child"},
                    location={"jsonl_line": ordinal + 1}))
        return writer.write(records)
