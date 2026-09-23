"""Synthetic-baseline Claude.ai account-export adapter (local files only)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..adapter import BatchResult, FingerprintResult
from ..batch_writer import BatchWriter, read_regular, reject
from ..projection import canonical_datetime, json_pointer
from ..strictjson import SourceJSONNumber, StrictJSONError, loads_source_bytes


def _safe(value: Any) -> bool:
    """Whether a source value is representable in canonical JSON."""
    if isinstance(value, SourceJSONNumber) or isinstance(value, float):
        return False
    if value is None or isinstance(value, (bool, int, str)):
        return True
    if isinstance(value, list):
        return all(_safe(child) for child in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _safe(child) for key, child in value.items())
    return False


class ClaudeAIAdapter:
    namespace = "org.open-chat-archive.claude-ai"
    version = "0.1.2"
    MAX_SOURCE_BYTES = 64 * 1024 * 1024
    # Fixed adapter-specific ceiling for the account-export wrapper profile.
    PROFILE_MAX_ITEMS = 500_000

    def __init__(self, source_scope_id: str): self.source_scope_id = source_scope_id

    def recognize(self, profile: dict[str, Any]) -> FingerprintResult:
        # Account objects conventionally sort chat_messages into the first
        # opaque alias.  A compatible account wrapper puts one nested object
        # before it, shifting that array to the second alias.
        paths = {item.get("path"): item.get("types", {}) for item in profile.get("paths", []) if isinstance(item, dict)}
        has = lambda path, kind: bool(paths.get(path, {}).get(kind))
        standard = has("/*/k0001", "array") and has("/*/k0001/*", "object")
        alternate = (has("/*/k0001", "object") and has("/*/k0001/k0001", "string")
                     and has("/*/k0002", "array") and has("/*/k0002/*", "object")
                     and all(has(f"/*/k{index:04d}", "string") for index in range(3, 8)))
        matched = (profile.get("profile_version") == "shape-only-v1" and has("/", "array")
                   and has("/*", "object") and (standard or alternate))
        return FingerprintResult(bool(matched), "claude-ai.account-export.v1" if matched else None)

    @staticmethod
    def _content(value: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        content: list[dict[str, Any]] = []; tools: list[dict[str, Any]] = []
        if not isinstance(value, list): reject("CLAUDE_AI_CONTENT")
        for block in value:
            if not isinstance(block, dict) or not isinstance(block.get("type"), str): reject("CLAUDE_AI_CONTENT")
            kind = block["type"]
            if kind in {"text", "thinking", "reasoning", "code"}:
                text = block.get("text", block.get("thinking"))
                if not isinstance(text, str): reject("CLAUDE_AI_CONTENT")
                content.append({"type": "reasoning" if kind in {"thinking", "reasoning"} else kind, "text": text})
            elif kind == "tool_use":
                if not all(isinstance(block.get(key), str) and block[key] for key in ("id", "name")) or "input" not in block:
                    reject("CLAUDE_AI_TOOL")
                if _safe(block["input"]):
                    tools.append({"kind": "tool_invocation", "call_id": block["id"], "tool_name": block["name"], "arguments": {"json": block["input"]}})
            elif kind == "tool_result":
                call_id = block.get("tool_use_id", block.get("id"))
                if not isinstance(call_id, str) or not call_id or "content" not in block: reject("CLAUDE_AI_TOOL")
                if _safe(block["content"]):
                    tools.append({"kind": "tool_result", "call_id": call_id,
                        "status": "error" if block.get("is_error") is True else "unknown",
                        "output": {"json": block["content"]}})
            # Unsupported block semantics remain exactly available in the source.
        return content, tools

    @staticmethod
    def _visibility(parts: list[dict[str, Any]]) -> str:
        if not any(part["type"] == "reasoning" for part in parts): return "user_visible"
        if all(part["type"] == "reasoning" for part in parts): return "exported_reasoning"
        return "unknown"

    def conform(self, source: Path, archive_root: Path, *, archive_id: str, batch_id: str) -> BatchResult:
        if source.suffix.lower() == ".zip": reject("CLAUDE_AI_ZIP_UNSUPPORTED")
        raw = read_regular(source, max_bytes=self.MAX_SOURCE_BYTES)
        try: data = loads_source_bytes(raw)
        except StrictJSONError:
            reject("CLAUDE_AI_JSON")
            raise AssertionError("unreachable")
        if not isinstance(data, list): reject("CLAUDE_AI_ROOT")
        writer = BatchWriter(namespace=self.namespace, version=self.version, source_scope_id=self.source_scope_id,
            archive_id=archive_id, batch_id=batch_id, archive_root=archive_root, raw=raw,
            media_type="application/json", fingerprint="claude-ai.account-export.v1", parsed=data,
            profile_max_items=self.PROFILE_MAX_ITEMS)
        records: list[dict[str, Any]] = []
        seen_sessions: set[str] = set(); seen_messages: set[str] = set()
        for ordinal, chat in enumerate(data):
            if not isinstance(chat, dict) or not isinstance(chat.get("uuid"), str) or not chat["uuid"] or not isinstance(chat.get("chat_messages"), list):
                reject("CLAUDE_AI_CONVERSATION")
            cid = chat["uuid"]
            if cid in seen_sessions: reject("CLAUDE_AI_DUPLICATE_ID")
            seen_sessions.add(cid)
            body: dict[str, Any] = {"archive_id": archive_id, "thread_shape": "linear"}
            if isinstance(chat.get("name"), str): body["title"] = chat["name"]
            created = canonical_datetime(chat.get("created_at")); updated = canonical_datetime(chat.get("updated_at"))
            if created: body["created_at"] = created
            if updated: body["updated_at"] = updated
            meta = {key: chat[key] for key in ("created_at", "updated_at") if key in chat and _safe(chat[key])}
            session_pointer = json_pointer(ordinal)
            session = writer.record("session", cid, body, extensions={self.namespace: meta} if meta else None,
                location={"json_pointer": session_pointer}, mapped_destinations=["/body"])
            records.append(session)
            event_ordinal = 0
            for message_ordinal, message in enumerate(chat["chat_messages"]):
                if not isinstance(message, dict) or not isinstance(message.get("uuid"), str) or not message["uuid"]:
                    reject("CLAUDE_AI_MESSAGE")
                mid = message["uuid"]
                if mid in seen_messages: reject("CLAUDE_AI_DUPLICATE_ID")
                seen_messages.add(mid)
                sender = message.get("sender")
                if sender not in {"human", "assistant"}: reject("CLAUDE_AI_SENDER")
                if "content" in message and message["content"] is not None:
                    parts, tools = self._content(message["content"])
                else:
                    if not isinstance(message.get("text"), str): reject("CLAUDE_AI_MESSAGE")
                    parts, tools = [{"type": "text", "text": message["text"]}], []
                if not parts and isinstance(message.get("text"), str): parts.append({"type": "text", "text": message["text"]})
                ext = {key: message[key] for key in ("created_at",) if key in message and _safe(message[key])}
                message_body: dict[str, Any] = {"session_id": session["entity_id"], "source_ordinal": message_ordinal,
                    "role": "user" if sender == "human" else "assistant", "visibility": self._visibility(parts), "content": parts}
                observed = canonical_datetime(message.get("created_at"))
                if observed: message_body["created_at"] = observed
                message_pointer = json_pointer(ordinal, "chat_messages", message_ordinal)
                msg = writer.record("message", mid, message_body, extensions={self.namespace: ext} if ext else None,
                    location={"json_pointer": message_pointer}, mapped_destinations=["/body"])
                records.append(msg)
                for tool_ordinal, tool in enumerate(tools):
                    key = f"{mid}:tool:{tool_ordinal}"
                    if tool["kind"] == "tool_invocation":
                        event_body = {"session_id": session["entity_id"], "source_ordinal": event_ordinal,
                            "kind": "tool_invocation", "correlation_id": tool["call_id"], "message_id": msg["entity_id"],
                            "payload": {"tool_namespace": "org.open-chat-archive.claude-ai-tools", "tool_name": tool["tool_name"],
                                "call_id": tool["call_id"], "arguments": tool["arguments"]}}
                    else:
                        event_body = {"session_id": session["entity_id"], "source_ordinal": event_ordinal,
                            "kind": "tool_result", "correlation_id": tool["call_id"], "message_id": msg["entity_id"],
                            "payload": {"call_id": tool["call_id"], "status": tool["status"], "output": tool["output"]}}
                    event_ordinal += 1
                    records.append(writer.record("event", key, event_body, location={"json_pointer": message_pointer},
                        mapped_destinations=["/body"]))
        return writer.write(records)
