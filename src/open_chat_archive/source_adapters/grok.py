"""Clean-room Grok Build 1.0.13 updates.jsonl adapter.

The implementation retains the entire ACP session-update log as evidence.
Only small, canonical-safe facts with direct archive meanings are projected.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..adapter import BatchResult, FingerprintResult
from ..batch_writer import BatchWriter, read_regular, reject
from ..strictjson import SourceJSONNumber, StrictJSONError, load_source_ndjson


_ACP_METHODS = frozenset({"session/update"})
_XAI_METHODS = frozenset({"_x.ai/session/update"})
_ACP_MAPPED = frozenset({
    "user_message_chunk", "agent_message_chunk", "agent_thought_chunk", "tool_call", "tool_call_update",
})
_ACP_KNOWN = _ACP_MAPPED | frozenset({
    "plan", "available_commands_update", "current_mode_update", "config_option_update", "session_info_update",
})
_XAI_MAPPED = frozenset({
    "turn_completed", "auto_compact_started", "auto_compact_completed", "auto_compact_failed",
    "auto_compact_cancelled", "response_started", "response_completed", "rewind_marker",
})
_XAI_KNOWN = _XAI_MAPPED | frozenset({
    "diff_review", "retry_state", "memory_flush_started", "memory_flush_completed", "memory_dream_completed",
    "memory_session_saved", "auto_continue_completed", "feedback_request", "relay_sync_status",
    "auto_recovery_started", "auto_recovery_exhausted", "hook_annotation", "hook_execution",
    "hooks_changed", "plugins_changed", "plugin_updates_installed", "session_status",
    "session_summary_generated", "session_recap", "session_recap_unavailable", "last_turn_summary",
    "compaction_checkpoint", "task_completed", "subagent_spawned", "subagent_progress", "subagent_finished",
    "task_backgrounded", "scheduled_task_created", "scheduled_task_fired", "scheduled_task_deleted",
    "monitor_event", "model_auto_switched", "model_changed", "tool_call_delta_chunk", "image_compressed",
    "image_dropped", "memory_files", "workflow_updated", "goal_updated", "pending_interaction",
    "interaction_resolved", "reasoning_completed",
})


def _string(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _safe(value: Any) -> bool:
    if isinstance(value, SourceJSONNumber) or isinstance(value, float):
        return False
    if value is None or isinstance(value, (str, bool, int)):
        return True
    if isinstance(value, list):
        return all(_safe(child) for child in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _safe(child) for key, child in value.items())
    return False


def _pick(payload: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in payload:
            return payload[name]
    return None


class GrokBuildAdapter:
    namespace = "org.open-chat-archive.grok-build"
    version = "0.1.1"
    fingerprint = "grok-build.updates.1.0.13.jsonl"
    # Streamed ACP chunks are structurally dense; keep this bounded and adapter-specific.
    PROFILE_MAX_ITEMS = 3_000_000

    def __init__(self, source_scope_id: str):
        self.source_scope_id = source_scope_id

    def recognize(self, profile: dict[str, Any]) -> FingerprintResult:
        paths = {entry.get("path"): entry.get("types", {}) for entry in profile.get("paths", []) if isinstance(entry, dict)}
        has = lambda path, kind: bool(paths.get(path, {}).get(kind))
        direct = [f"/*/k{i:04d}" for i in range(1, 6)]
        strings = sum(has(path, "string") for path in direct)
        objects = sum(has(path, "object") for path in direct)
        nested = any(has(path + "/k0001", "string") or has(path + "/k0002", "object") or has(path + "/k0001", "object")
                     for path in direct if has(path, "object"))
        matched = (profile.get("profile_version") == "shape-only-v1" and has("/", "array") and has("/*", "object")
                   and objects >= 1 and strings >= 1 and nested)
        return FingerprintResult(matched, self.fingerprint if matched else None)

    def _extensions(self, extra: dict[str, Any] | None = None) -> dict[str, Any] | None:
        meta: dict[str, Any] = {}
        if extra:
            for name, value in extra.items():
                if value is not None and _safe(value):
                    meta[name] = value
        return {self.namespace: meta} if meta else None

    def _event(self, writer: BatchWriter, session: dict[str, Any], ordinal: int, key: str,
               kind: str, payload: dict[str, Any], *, correlation: str | None = None,
               extensions: dict[str, Any] | None = None, fallback: bool = False) -> dict[str, Any]:
        seq = getattr(self, "_event_seq", 0); self._event_seq = seq + 1
        body: dict[str, Any] = {"session_id": session["entity_id"], "source_ordinal": seq,
                                "kind": kind, "payload": payload}
        if correlation:
            body["correlation_id"] = correlation
        return writer.record("event", key, body, extensions=extensions,
                             scheme="artifact-local" if fallback else "native-id", fallback=fallback,
                             location={"jsonl_line": ordinal + 1})

    @staticmethod
    def _line(entry: Any) -> tuple[dict[str, Any], str | None, dict[str, Any], dict[str, Any]]:
        if not isinstance(entry, dict):
            reject("GROK_LINE")
        if "timestamp" in entry and not _integer(entry["timestamp"]):
            reject("GROK_LINE")
        if "method" in entry:
            method = entry.get("method")
            if not _string(method):
                reject("GROK_LINE")
            params = entry.get("params")
            if not isinstance(params, dict):
                reject("GROK_PAYLOAD")
            return entry, str(method), params, params
        # Legacy ACP notification without the disk envelope.
        if not _string(_pick(entry, "sessionId", "session_id")):
            reject("GROK_LINE")
        return entry, None, entry, entry

    @staticmethod
    def _session_id(params: dict[str, Any]) -> str:
        session_id = _pick(params, "sessionId", "session_id")
        if not _string(session_id):
            reject("GROK_SESSION")
        return session_id

    @staticmethod
    def _update(params: dict[str, Any]) -> dict[str, Any]:
        raw_update = params.get("update")
        if not isinstance(raw_update, dict):
            reject("GROK_UPDATE")
            raise AssertionError("unreachable")
        update: dict[str, Any] = dict(raw_update)
        tag = update.get("sessionUpdate")
        if not _string(tag):
            reject("GROK_UPDATE")
        return update

    @staticmethod
    def _text(update: dict[str, Any]) -> str | None:
        content = update.get("content")
        block = content.get("content") if isinstance(content, dict) and isinstance(content.get("content"), dict) else content
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            return block["text"]
        return None

    @staticmethod
    def _usage(value: Any) -> dict[str, int]:
        if not isinstance(value, dict):
            reject("GROK_USAGE")
        result: dict[str, int] = {}
        fields = {
            "input_tokens": "input_tokens", "inputTokens": "input_tokens",
            "output_tokens": "output_tokens", "outputTokens": "output_tokens",
            "total_tokens": "total_tokens", "totalTokens": "total_tokens",
            "cached_read_tokens": "cache_read_tokens", "cachedReadTokens": "cache_read_tokens",
            "cache_read_input_tokens": "cache_read_tokens", "cacheReadInputTokens": "cache_read_tokens",
            "cache_creation_tokens": "cache_write_tokens", "cacheCreationTokens": "cache_write_tokens",
            "cache_creation_input_tokens": "cache_write_tokens", "cacheCreationInputTokens": "cache_write_tokens",
            "reasoning_tokens": "reasoning_tokens", "reasoningTokens": "reasoning_tokens",
        }
        for source, destination in fields.items():
            if source in value:
                amount = value[source]
                if not _integer(amount) or amount < 0:
                    reject("GROK_USAGE")
                result[destination] = amount
        return result

    def _meta(self, params: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
        extra: dict[str, Any] = {}
        if _integer(entry.get("timestamp")):
            extra["timestamp"] = entry["timestamp"]
        meta = params.get("_meta")
        if isinstance(meta, dict):
            for name in ("eventId", "promptIndex", "agentTimestampMs", "turnStartMs"):
                value = meta.get(name)
                if _string(value) or _integer(value):
                    extra[name] = value
        return extra

    def conform(self, source: Path, archive_root: Path, *, archive_id: str, batch_id: str) -> BatchResult:
        raw = read_regular(source)
        try:
            entries = load_source_ndjson(raw)
        except StrictJSONError:
            reject("GROK_JSONL")
            raise AssertionError("unreachable")
        if not entries:
            reject("GROK_EMPTY")

        session_key: str | None = None
        checked: list[tuple[dict[str, Any], str | None, dict[str, Any], dict[str, Any]]] = []
        for entry in entries:
            wrapper, method, params, payload = self._line(entry)
            key = self._session_id(params)
            self._update(params)
            if session_key is None:
                session_key = key
            elif key != session_key:
                reject("GROK_SESSION")
            if method is not None and method not in _ACP_METHODS and method not in _XAI_METHODS:
                reject("GROK_METHOD")
            checked.append((wrapper, method, params, payload))
        if session_key is None:
            reject("GROK_SESSION")
            raise AssertionError("unreachable")

        writer = BatchWriter(namespace=self.namespace, version=self.version, source_scope_id=self.source_scope_id,
            archive_id=archive_id, batch_id=batch_id, archive_root=archive_root, raw=raw,
            media_type="application/x-ndjson", fingerprint=self.fingerprint, parsed=entries,
            profile_max_items=self.PROFILE_MAX_ITEMS)
        self._event_seq = 0
        session = writer.record("session", session_key, {"archive_id": archive_id})
        records: list[dict[str, Any]] = [session]
        tool_keys: set[str] = set()
        mapped_items: set[str] = set()
        turns: set[str] = set()

        def add_unique(kind: str, key: str) -> None:
            token = f"{kind}:{key}"
            if token in mapped_items:
                reject("GROK_DUPLICATE")
            mapped_items.add(token)

        for ordinal, (entry, method, params, _payload) in enumerate(checked):
            update = self._update(params)
            tag = update["sessionUpdate"]
            extra = self._meta(params, entry)
            extra["session_update"] = tag
            if method:
                extra["method"] = method
            rail = "xai" if method in _XAI_METHODS else "acp"
            if rail == "acp" and tag not in _ACP_KNOWN and tag not in _XAI_MAPPED:
                continue
            if rail == "xai" and tag not in _XAI_KNOWN and tag not in _ACP_MAPPED:
                continue

            if tag in {"user_message_chunk", "agent_message_chunk", "agent_thought_chunk"}:
                text = self._text(update)
                if text is None:
                    continue
                event_id = extra.get("eventId")
                fallback = not _string(event_id)
                prefix = "thought" if tag == "agent_thought_chunk" else "user" if tag == "user_message_chunk" else "agent"
                key = f"{prefix}:{event_id}" if not fallback else f"{prefix}:{ordinal}"
                add_unique("message", key)
                role = "user" if tag == "user_message_chunk" else "assistant"
                visibility = "exported_reasoning" if tag == "agent_thought_chunk" else "user_visible"
                part_type = "reasoning" if tag == "agent_thought_chunk" else "text"
                records.append(writer.record("message", key, {"session_id": session["entity_id"], "source_ordinal": ordinal,
                    "role": role, "visibility": visibility, "content": [{"type": part_type, "text": text}]},
                    extensions=self._extensions(extra), scheme="artifact-local" if fallback else "native-id",
                    fallback=fallback))
            elif tag == "tool_call":
                call_id = _pick(update, "toolCallId", "tool_call_id")
                title = update.get("title")
                if not _string(call_id) or not _string(title):
                    reject("GROK_TOOL")
                arguments = _pick(update, "rawInput", "raw_input")
                if arguments is None:
                    arguments = {}
                if not _safe(arguments):
                    continue
                key = f"tool:{call_id}"
                if key in tool_keys:
                    reject("GROK_TOOL")
                tool_keys.add(key)
                status = update.get("status")
                extra = dict(extra)
                if _string(update.get("kind")):
                    extra["kind"] = update["kind"]
                extra["title"] = title
                records.append(self._event(writer, session, ordinal, key, "tool_invocation", {
                    "tool_namespace": self.namespace + ".tools", "tool_name": title, "call_id": call_id,
                    "arguments": {"json": arguments}}, correlation=call_id, extensions=self._extensions(extra)))
                if status in {"completed", "failed"}:
                    result_key = f"tool-result:{call_id}"
                    if result_key not in tool_keys:
                        tool_keys.add(result_key)
                        output = _pick(update, "rawOutput", "raw_output")
                        if output is None or not _safe(output):
                            if status == "failed":
                                records.append(self._event(writer, session, ordinal, result_key, "tool_result",
                                    {"call_id": call_id, "status": "error", "error": {"code": "failed"}},
                                    correlation=call_id, extensions=self._extensions(extra)))
                        else:
                            records.append(self._event(writer, session, ordinal, result_key, "tool_result",
                                {"call_id": call_id, "status": "ok" if status == "completed" else "error",
                                 "output": {"json": output}}, correlation=call_id, extensions=self._extensions(extra)))
            elif tag == "tool_call_update":
                call_id = _pick(update, "toolCallId", "tool_call_id")
                if not _string(call_id):
                    reject("GROK_TOOL")
                status = update.get("status")
                if status not in {"completed", "failed"}:
                    continue
                result_key = f"tool-result:{call_id}"
                if result_key in tool_keys:
                    continue
                output = _pick(update, "rawOutput", "raw_output")
                if output is None or not _safe(output):
                    if status != "failed":
                        continue
                    tool_keys.add(result_key)
                    records.append(self._event(writer, session, ordinal, result_key, "tool_result",
                        {"call_id": call_id, "status": "error", "error": {"code": "failed"}},
                        correlation=call_id, extensions=self._extensions(extra)))
                else:
                    tool_keys.add(result_key)
                    records.append(self._event(writer, session, ordinal, result_key, "tool_result",
                        {"call_id": call_id, "status": "ok" if status == "completed" else "error",
                         "output": {"json": output}}, correlation=call_id, extensions=self._extensions(extra)))
            elif tag == "turn_completed":
                prompt_id = _pick(update, "prompt_id", "promptId")
                stop = _pick(update, "stop_reason", "stopReason")
                if not _string(prompt_id) or not _string(stop):
                    reject("GROK_TURN")
                if prompt_id not in turns:
                    turns.add(prompt_id)
                    records.append(writer.record("turn", prompt_id, {"session_id": session["entity_id"],
                        "source_ordinal": ordinal}, extensions=self._extensions(extra)))
                status = "cancelled" if stop in {"cancelled", "aborted"} else "error" if stop in {"error", "failed"} else "completed"
                extra = dict(extra)
                extra["stop_reason"] = stop
                records.append(self._event(writer, session, ordinal, f"turn-complete:{prompt_id}", "lifecycle",
                    {"status": status, "detail": "turn_completed"}, correlation=prompt_id,
                    extensions=self._extensions(extra)))
                usage = update.get("usage")
                if usage is not None:
                    mapped = self._usage(usage)
                    if mapped:
                        records.append(self._event(writer, session, ordinal, f"turn-usage:{prompt_id}", "usage", mapped,
                            correlation=prompt_id, extensions=self._extensions(extra)))
            elif tag in {"auto_compact_started", "auto_compact_completed", "auto_compact_failed", "auto_compact_cancelled"}:
                status = {"auto_compact_started": "started", "auto_compact_completed": "completed",
                          "auto_compact_failed": "error", "auto_compact_cancelled": "cancelled"}[tag]
                records.append(self._event(writer, session, ordinal, f"compact:{ordinal}", "lifecycle",
                    {"status": status, "detail": tag}, extensions=self._extensions(extra), fallback=True))
            elif tag == "rewind_marker":
                records.append(self._event(writer, session, ordinal, f"rewind:{ordinal}", "lifecycle",
                    {"status": "cancelled", "detail": "rewind_marker"}, extensions=self._extensions(extra), fallback=True))
            elif tag == "response_completed":
                usage = update.get("usage")
                message_id = _pick(update, "message_id", "messageId")
                if usage is not None:
                    mapped = self._usage(usage)
                    if mapped:
                        key = f"response-usage:{message_id}" if _string(message_id) else f"response-usage:{ordinal}"
                        records.append(self._event(writer, session, ordinal, key, "usage", mapped,
                            correlation=message_id if _string(message_id) else None,
                            extensions=self._extensions(extra), fallback=not _string(message_id)))
        return writer.write(records)
