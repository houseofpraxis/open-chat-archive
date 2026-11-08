"""Clean-room Pi Coding Agent v3 session JSONL adapter."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..adapter import BatchResult, FingerprintResult
from ..batch_writer import BatchWriter, read_regular, reject
from ..projection import canonical_datetime
from ..strictjson import SourceJSONNumber, StrictJSONError, load_source_ndjson


_KNOWN = frozenset({"message", "thinking_level_change", "model_change", "usage", "compaction",
    "branch_summary", "custom", "label", "session_info", "custom_message", "context_edit"})


def _string(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _safe(value: Any) -> bool:
    if isinstance(value, (SourceJSONNumber, float)):
        return False
    if value is None or isinstance(value, (str, bool, int)):
        return True
    if isinstance(value, list):
        return all(_safe(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _safe(item) for key, item in value.items())
    return False


def _safe_metadata(value: dict[str, Any]) -> dict[str, Any]:
    return {key: child for key, child in value.items() if _safe(child)}


class PiV3Adapter:
    namespace = "org.open-chat-archive.pi-v3"
    version = "0.1.0"
    fingerprint = "pi.session.v3.jsonl"

    def __init__(self, source_scope_id: str):
        self.source_scope_id = source_scope_id

    def recognize(self, profile: dict[str, Any]) -> FingerprintResult:
        paths = {item.get("path"): item.get("types", {}) for item in profile.get("paths", []) if isinstance(item, dict)}
        header = bool(paths.get("/*/k0001", {}).get("string"))
        version = bool(paths.get("/*/k0002", {}).get("integer"))
        return FingerprintResult(profile.get("profile_version") == "shape-only-v1"
            and bool(paths.get("/", {}).get("array")) and header and version,
            self.fingerprint if header and version else None)

    @staticmethod
    def _base(entry: Any, *, header: bool = False) -> dict[str, Any]:
        if not isinstance(entry, dict):
            reject("PI_ENTRY")
        if header:
            if entry.get("type") != "session" or entry.get("version") != 3 or not _string(entry.get("id")) \
                    or not _string(entry.get("timestamp")) or not isinstance(entry.get("cwd"), str):
                reject("PI_HEADER")
            return entry
        if not _string(entry.get("type")) or not _string(entry.get("id")) \
                or not isinstance(entry.get("parentId"), (str, type(None))) \
                or not _string(entry.get("timestamp")):
            reject("PI_ENTRY")
        return entry

    @staticmethod
    def _parts(content: Any) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        if isinstance(content, str):
            return ([{"type": "text", "text": content}] if content else []), []
        if not isinstance(content, list):
            reject("PI_CONTENT")
        text: list[dict[str, str]] = []
        reasoning: list[dict[str, str]] = []
        for part in content:
            if not isinstance(part, dict) or not _string(part.get("type")):
                reject("PI_CONTENT")
            kind = part["type"]
            if kind == "text":
                if not isinstance(part.get("text"), str):
                    reject("PI_CONTENT")
                if part["text"]:
                    text.append({"type": "text", "text": part["text"]})
            elif kind == "thinking":
                if not isinstance(part.get("thinking"), str):
                    reject("PI_CONTENT")
                if part["thinking"]:
                    reasoning.append({"type": "reasoning", "text": part["thinking"]})
            elif kind == "toolCall":
                if not _string(part.get("id")) or not _string(part.get("name")) or not isinstance(part.get("arguments"), dict):
                    reject("PI_TOOL_CALL")
            elif kind == "image":
                # Binary image data and future image encodings remain in the exact source.
                continue
            else:
                # Preserve unsupported content blocks without interpreting them.
                continue
        return text, reasoning

    def conform(self, source: Path, archive_root: Path, *, archive_id: str, batch_id: str) -> BatchResult:
        raw = read_regular(source)
        try:
            entries = load_source_ndjson(raw)
        except StrictJSONError:
            reject("PI_JSONL")
        if len(entries) < 1:
            reject("PI_EMPTY")
        header = self._base(entries[0], header=True)
        if any(not isinstance(item, dict) for item in entries[1:]):
            reject("PI_ENTRY")

        byte_ranges: list[tuple[int, int]] = []
        cursor = 0
        for line in raw.splitlines(keepends=True):
            byte_ranges.append((cursor, cursor + len(line)))
            cursor += len(line)
        if len(byte_ranges) != len(entries) or cursor != len(raw):
            reject("PI_JSONL")

        checked = [self._base(item) for item in entries[1:]]
        if any(item["type"] == "session" for item in checked):
            reject("PI_SESSION_UNIQUENESS")
        ids = [item["id"] for item in checked]
        if header["id"] in ids or len(ids) != len(set(ids)):
            reject("PI_DUPLICATE_ID")
        for item in checked:
            if item["type"] == "message":
                message = item.get("message")
                if not isinstance(message, dict) or not _string(message.get("role")):
                    reject("PI_MESSAGE")
                if "content" not in message and message["role"] in {"user", "assistant", "system", "toolResult"}:
                    reject("PI_MESSAGE")
                if "isError" in message and not isinstance(message["isError"], bool):
                    reject("PI_MESSAGE")
            if item["type"] == "session_info" and "name" in item and not isinstance(item["name"], str):
                reject("PI_SESSION_INFO")
        title = next((item.get("name") for item in reversed(checked)
                      if item["type"] == "session_info" and _string(item.get("name"))), None)
        tree = any(item["parentId"] is not None for item in checked)
        writer = BatchWriter(namespace=self.namespace, version=self.version, source_scope_id=self.source_scope_id,
            archive_id=archive_id, batch_id=batch_id, archive_root=archive_root, raw=raw,
            media_type="application/x-ndjson", fingerprint=self.fingerprint, parsed=entries)
        header_start, header_end = byte_ranges[0]
        header_meta = _safe_metadata(header)
        session_body: dict[str, Any] = {"archive_id": archive_id, "thread_shape": "tree" if tree else "linear"}
        if title:
            session_body["title"] = title
        created = canonical_datetime(header.get("timestamp"))
        if created:
            session_body["created_at"] = created
        session = writer.record("session", header["id"], session_body,
            extensions={self.namespace: {"header": header_meta}},
            location={"jsonl_line": 1, "byte_range": {"start": header_start, "end": header_end}})
        records: list[dict[str, Any]] = [session]
        emitted: dict[str, list[dict[str, Any]]] = {}
        events = 0
        for ordinal, item in enumerate(checked, start=1):
            start, end = byte_ranges[ordinal]
            loc = {"jsonl_line": ordinal + 1, "byte_range": {"start": start, "end": end}}
            entry_type, native_id = item["type"], item["id"]
            ext_value = _safe_metadata({key: value for key, value in item.items() if key not in {"type", "id", "parentId", "timestamp", "message"}})
            ext_value["timestamp"] = item["timestamp"]
            if item["parentId"] is not None:
                ext_value["parentId"] = item["parentId"]
            if entry_type == "message":
                message = item["message"]
                role = message["role"]
                message_meta = _safe_metadata({key: value for key, value in message.items() if key != "content"})
                created_at = canonical_datetime(item["timestamp"])
                made: list[dict[str, Any]] = []
                if role in {"user", "assistant", "system", "toolResult"}:
                    text_parts, reasoning_parts = self._parts(message["content"])
                    canonical_role = "tool" if role == "toolResult" else role
                    visibility = "tool_traffic" if canonical_role == "tool" else "user_visible" if canonical_role in {"user", "assistant"} else "control"
                    if canonical_role == "tool":
                        text_parts = []
                    for part_index, (part_kind, parts) in enumerate((("text", text_parts), ("reasoning", reasoning_parts))):
                        if not parts:
                            continue
                        body = {"session_id": session["entity_id"], "source_ordinal": ordinal * 2 + part_index,
                            "role": canonical_role, "visibility": visibility if part_kind == "text" else "exported_reasoning",
                            "content": parts}
                        if created_at:
                            body["created_at"] = created_at
                        key = f"{header['id']}:{native_id}:{part_kind}"
                        extensions = {**ext_value, "message_role": role, "entry_id": native_id,
                            "session_id": header["id"]}
                        if message_meta:
                            extensions["message_metadata"] = message_meta
                        emitted_record = writer.record("message", key, body,
                            extensions={self.namespace: extensions}, location=loc,
                            mapped_destinations=["/body"])
                        made.append(emitted_record); records.append(emitted_record)
                emitted[native_id] = made
                # Tool calls are preserved as typed events, never indexed as prose.
                for ci, part in enumerate(message.get("content", []) if isinstance(message.get("content"), list) else []):
                    if isinstance(part, dict) and part.get("type") == "toolCall":
                        event_body = {"session_id": session["entity_id"], "source_ordinal": events,
                            "kind": "tool_invocation", "correlation_id": part["id"],
                            "payload": {"tool_namespace": self.namespace + ".tools", "tool_name": part["name"],
                                "call_id": part["id"], "arguments": {"json": _safe_metadata(part["arguments"])}}}
                        records.append(writer.record("event", f"{header['id']}:{native_id}:tool:{ci}", event_body, location=loc)); events += 1
                if role == "toolResult":
                    tool_id = message.get("toolCallId")
                    if _string(tool_id):
                        result_content = message.get("content")
                        result_text, _ = self._parts(result_content)
                        is_error = message.get("isError") is True
                        payload = {"call_id": tool_id, "status": "error" if is_error else "unknown",
                            ("error" if is_error else "output"): {"json": {"text": "\n".join(p["text"] for p in result_text)}}}
                        records.append(writer.record("event", f"{header['id']}:{native_id}:tool-result", {
                            "session_id": session["entity_id"], "source_ordinal": events, "kind": "tool_result",
                            "correlation_id": tool_id, "payload": payload}, location=loc)); events += 1
            # Native control entries and unknown future entry types remain exact-source evidence.
            elif entry_type == "usage":
                usage = item.get("usage")
                if "usage" in item and not isinstance(usage, dict):
                    reject("PI_USAGE")
                if isinstance(usage, dict):
                    numeric = {key: value for key, value in usage.items() if key in {"input", "output", "cacheRead", "cacheWrite"}}
                    if any(not _integer(v) or v < 0 for v in numeric.values()):
                        reject("PI_USAGE")
                    mapped = {source_name: numeric[src] for src, source_name in (("input", "input_tokens"), ("output", "output_tokens"), ("cacheRead", "cache_read_tokens"), ("cacheWrite", "cache_write_tokens")) if src in numeric}
                    if mapped:
                        records.append(writer.record("event", f"{header['id']}:{native_id}:usage", {
                            "session_id": session["entity_id"], "source_ordinal": events, "kind": "usage", "payload": mapped},
                            extensions={self.namespace: ext_value}, location=loc)); events += 1
            elif entry_type == "session_info" and _string(item.get("name")):
                # Title is also evidenced at the exact session_info line.
                records.append(writer.record("event", f"{header['id']}:{native_id}:session-info", {
                    "session_id": session["entity_id"], "source_ordinal": events, "kind": "lifecycle",
                    "payload": {"status": "unknown", "detail": "session_info"}},
                    extensions={self.namespace: ext_value}, location=loc)); events += 1

        # The native entry tree is preserved above. Emit canonical links only
        # where a direct message-to-message edge has an unambiguous endpoint.
        by_id = {item["id"]: item for item in checked}
        for child in checked:
            parent_id = child["parentId"]
            if parent_id is None or parent_id not in by_id:
                continue
            parent = by_id[parent_id]
            if parent["type"] == child["type"] == "message" and len(emitted.get(parent_id, [])) == len(emitted.get(child["id"], [])) == 1:
                child_line = checked.index(child) + 2
                start, end = byte_ranges[child_line - 1]
                records.append(writer.record("relationship", f"{header['id']}:{parent_id}>{child['id']}", {
                    "from_id": emitted[parent_id][0]["entity_id"], "to_id": emitted[child["id"]][0]["entity_id"], "kind": "parent_child"},
                    location={"jsonl_line": child_line, "byte_range": {"start": start, "end": end}}))
        return writer.write(records)
