"""Clean-room Codex CLI 0.147.0 rollout JSONL adapter.

The implementation deliberately retains the entire rollout as its evidence.
Only small, canonical-safe facts with direct archive meanings are projected.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..adapter import BatchResult, FingerprintResult
from ..batch_writer import BatchWriter, read_regular, reject
from ..strictjson import SourceJSONNumber, StrictJSONError, load_source_ndjson


_RESPONSE_TYPES = frozenset({
    "additional_tools", "message", "agent_message", "reasoning", "local_shell_call",
    "function_call", "tool_search_call", "function_call_output", "custom_tool_call",
    "custom_tool_call_output", "tool_search_output", "web_search_call", "image_generation_call",
    "compaction", "compaction_trigger", "context_compaction",
})
# The pinned EventMsg vocabulary.  Most are intentionally preserved only: this
# is an allowlist for compatibility detection, not a promise to interpret them.
_EVENT_TYPES = frozenset({
    "error", "warning", "guardian_warning", "realtime_conversation_started",
    "realtime_conversation_realtime", "realtime_conversation_closed", "realtime_conversation_sdp",
    "model_reroute", "model_verification", "turn_moderation_metadata", "safety_buffering",
    "context_compacted", "thread_rolled_back", "task_started", "turn_started",
    "thread_settings_applied", "task_complete", "turn_complete", "token_count", "agent_message",
    "user_message", "agent_reasoning", "agent_reasoning_raw_content", "agent_reasoning_section_break",
    "session_configured", "environment_connected", "environment_disconnected", "thread_goal_updated",
    "mcp_startup_update", "mcp_startup_complete", "mcp_tool_call_begin", "mcp_tool_call_end",
    "web_search_begin", "web_search_end", "image_generation_begin", "image_generation_end",
    "exec_command_begin", "exec_command_output_delta", "terminal_interaction", "exec_command_end",
    "view_image_tool_call", "exec_approval_request", "request_permissions", "request_user_input",
    "dynamic_tool_call_request", "dynamic_tool_call_response", "elicitation_request",
    "apply_patch_approval_request", "guardian_assessment", "deprecation_notice", "stream_error",
    "patch_apply_begin", "patch_apply_updated", "patch_apply_end", "turn_diff",
    "realtime_conversation_list_voices_response", "plan_update", "turn_aborted", "shutdown_complete",
    "entered_review_mode", "exited_review_mode", "raw_response_item", "raw_response_completed",
    "item_started", "item_completed", "hook_started", "hook_completed", "agent_message_content_delta",
    "plan_delta", "reasoning_content_delta", "reasoning_raw_content_delta", "collab_agent_spawn_begin",
    "collab_agent_spawn_end", "collab_agent_interaction_begin", "collab_agent_interaction_end",
    "collab_waiting_begin", "collab_waiting_end", "collab_close_begin", "collab_close_end",
    "collab_resume_begin", "collab_resume_end", "sub_agent_activity",
})
_TOP_TYPES = frozenset({"session_meta", "response_item", "inter_agent_communication",
    "inter_agent_communication_metadata", "compacted", "turn_context", "world_state", "event_msg",
    "token_usage_record"})
_TIMING = ("started_at", "completed_at", "duration_ms", "time_to_first_token_ms")


def _string(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _safe(value: Any) -> bool:
    if isinstance(value, SourceJSONNumber) or isinstance(value, float): return False
    if value is None or isinstance(value, (str, bool, int)): return True
    if isinstance(value, list): return all(_safe(child) for child in value)
    if isinstance(value, dict): return all(isinstance(key, str) and _safe(child) for key, child in value.items())
    return False


def _optional_scalars(payload: dict[str, Any], names: tuple[str, ...]) -> dict[str, Any]:
    return {name: payload[name] for name in names if name in payload and _safe(payload[name])}


def _call_id(payload: dict[str, Any]) -> Any:
    return payload.get("call_id", payload.get("callId"))


class CodexAdapter:
    namespace = "org.open-chat-archive.codex"
    version = "0.1.3"
    fingerprint = "codex.rollout.0.147.0.jsonl"
    # Fixed and bounded for complete rollout profiling; never source-derived.
    PROFILE_MAX_ITEMS = 100_000

    def __init__(self, source_scope_id: str): self.source_scope_id = source_scope_id

    def recognize(self, profile: dict[str, Any]) -> FingerprintResult:
        paths = {entry.get("path"): entry.get("types", {}) for entry in profile.get("paths", []) if isinstance(entry, dict)}
        has = lambda path, kind: bool(paths.get(path, {}).get(kind))
        # Flattened RolloutLine: the object payload can sort before timestamp/type
        # when no ordinal exists, while ordinal adds the integer direct sibling.
        direct = [f"/*/k{i:04d}" for i in range(1, 6)]
        payload_candidates = [path for path in direct if has(path, "object")]
        # A rollout payload is itself tagged/structured; requiring one nested
        # string distinguishes it from a generic object alongside two strings.
        payload = any(has(path + "/k0001", "string") or has(path + "/k0002", "string") for path in payload_candidates)
        strings = sum(has(path, "string") for path in direct)
        ordinal = any(has(path, "integer") for path in direct)
        topology = payload and strings >= 2 and (not ordinal or strings >= 2)
        matched = (profile.get("profile_version") == "shape-only-v1" and has("/", "array")
                   and has("/*", "object") and topology)
        return FingerprintResult(matched, self.fingerprint if matched else None)

    def _extensions(self, entry: dict[str, Any], extra: dict[str, Any] | None = None) -> dict[str, Any] | None:
        meta: dict[str, Any] = {}
        if _string(entry.get("timestamp")):
            meta["timestamp"] = entry["timestamp"]
        if extra:
            for name, value in extra.items():
                if value is not None and _safe(value):
                    meta[name] = value
        return {self.namespace: meta} if meta else None

    @staticmethod
    def _payload(entry: dict[str, Any], top: str) -> dict[str, Any]:
        raw_payload = entry.get("payload")
        if top == "token_usage_record" and "payload" not in entry:
            return {}
        if not isinstance(raw_payload, dict): reject("CODEX_PAYLOAD")
        payload: dict[str, Any] = dict(raw_payload)
        if top == "response_item":
            item_type = payload.get("type")
            if item_type == "compaction_summary":
                payload["type"] = "compaction"
            if not _string(payload.get("type")) or payload["type"] not in _RESPONSE_TYPES: reject("CODEX_RESPONSE_VARIANT")
        elif top == "event_msg":
            event_type = payload.get("type")
            if not _string(event_type) or event_type not in _EVENT_TYPES: reject("CODEX_EVENT_VARIANT")
        return payload

    @staticmethod
    def _usage(value: Any) -> dict[str, int]:
        if not isinstance(value, dict): reject("CODEX_USAGE")
        result: dict[str, int] = {}
        fields = {"input_tokens": "input_tokens", "output_tokens": "output_tokens",
                  "total_tokens": "total_tokens", "cached_input_tokens": "cache_read_tokens",
                  "cache_write_input_tokens": "cache_write_tokens", "reasoning_output_tokens": "reasoning_tokens"}
        for source, destination in fields.items():
            if source in value:
                amount = value[source]
                if not _integer(amount) or amount < 0: reject("CODEX_USAGE")
                result[destination] = amount
        return result

    def _event(self, writer: BatchWriter, session: dict[str, Any], ordinal: int, key: str,
               kind: str, payload: dict[str, Any], *, correlation: str | None = None,
               extensions: dict[str, Any] | None = None, fallback: bool = False) -> dict[str, Any]:
        seq = getattr(self, "_event_seq", 0); self._event_seq = seq + 1
        body: dict[str, Any] = {"session_id": session["entity_id"], "source_ordinal": seq,
                                "kind": kind, "payload": payload}
        if correlation: body["correlation_id"] = correlation
        return writer.record("event", key, body, extensions=extensions, scheme="artifact-local" if fallback else "native-id", fallback=fallback,
            location={"jsonl_line": ordinal + 1})

    def conform(self, source: Path, archive_root: Path, *, archive_id: str, batch_id: str) -> BatchResult:
        raw = read_regular(source)
        try: entries = load_source_ndjson(raw)
        except StrictJSONError: reject("CODEX_JSONL")
        if not entries: reject("CODEX_EMPTY")

        meta: dict[str, Any] | None = None
        meta_ordinal: int | None = None
        session_key: str | None = None
        checked: list[tuple[dict[str, Any], str, dict[str, Any]]] = []
        for ordinal, entry in enumerate(entries):
            if not isinstance(entry, dict) or not _string(entry.get("timestamp")):
                reject("CODEX_LINE")
            if "ordinal" in entry and (not _integer(entry["ordinal"]) or entry["ordinal"] < 0):
                reject("CODEX_LINE")
            top = entry.get("type")
            if not _string(top) or top not in _TOP_TYPES: reject("CODEX_ROLLOUT_VARIANT")
            payload = self._payload(entry, top)
            if top == "session_meta":
                if meta is not None: reject("CODEX_SESSION_META")
                if "id" in payload and payload["id"] is not None and not _string(payload["id"]):
                    reject("CODEX_SESSION_META")
                if "session_id" in payload and payload["session_id"] is not None and not _string(payload["session_id"]):
                    reject("CODEX_SESSION_META")
                if _string(payload.get("session_id")):
                    session_key = payload["session_id"]
                elif _string(payload.get("id")):
                    session_key = payload["id"]
                else:
                    reject("CODEX_SESSION_META")
                meta = payload
                meta_ordinal = ordinal
            checked.append((entry, top, payload))
        if meta is None or session_key is None: reject("CODEX_SESSION_META")

        writer = BatchWriter(namespace=self.namespace, version=self.version, source_scope_id=self.source_scope_id,
            archive_id=archive_id, batch_id=batch_id, archive_root=archive_root, raw=raw,
            media_type="application/x-ndjson", fingerprint=self.fingerprint, parsed=entries,
            profile_max_items=self.PROFILE_MAX_ITEMS)
        self._event_seq = 0
        session_meta = _optional_scalars(meta, ("id", "session_id", "timestamp", "cli_version", "originator", "source",
            "forked_from_id", "parent_thread_id", "model_provider", "thread_source", "agent_nickname", "agent_role",
            "history_mode", "memory_mode", "multi_agent_version", "subagent_history_start_ordinal"))
        session = writer.record("session", session_key, {"archive_id": archive_id},
            extensions={self.namespace: session_meta} if session_meta else None,
            location={"jsonl_line": meta_ordinal + 1})
        records: list[dict[str, Any]] = [session]
        provider = meta.get("model_provider") if _string(meta.get("model_provider")) else None
        context_by_turn: dict[str, str] = {}
        current_context: tuple[str | None, str] | None = None
        turns: set[str] = set(); mapped_items: set[str] = set(); response_ids: set[str] = set()
        tool_keys: set[str] = set()

        def item_key(payload: dict[str, Any], ordinal: int, prefix: str) -> tuple[str, bool]:
            item_id = payload.get("id")
            if item_id is not None and not _string(item_id): reject("CODEX_RESPONSE_ITEM")
            return (f"{prefix}:{item_id}" if item_id else f"{prefix}:{ordinal}", item_id is None)

        def remember_response_id(payload: dict[str, Any]) -> None:
            item_id = payload.get("id")
            if item_id is None:
                return
            if not _string(item_id): reject("CODEX_RESPONSE_ITEM")
            if item_id in response_ids: reject("CODEX_DUPLICATE_ITEM")
            response_ids.add(item_id)

        def add_tool(entry: dict[str, Any], ordinal: int, key: str, call_id: Any, name: Any, arguments: Any, *,
                     result: bool = False, status: str = "unknown", mcp: bool = False,
                     extra: dict[str, Any] | None = None) -> None:
            if not _string(call_id): reject("CODEX_TOOL")
            if key in tool_keys: reject("CODEX_TOOL")
            tool_keys.add(key)
            extensions = self._extensions(entry, extra)
            if result:
                if not _safe(arguments): return
                records.append(self._event(writer, session, ordinal, key, "tool_result", {"call_id": call_id,
                    "status": status, "output": {"json": arguments}}, correlation=call_id, extensions=extensions))
            else:
                if not _string(name) or not _safe(arguments): reject("CODEX_TOOL")
                records.append(self._event(writer, session, ordinal, key, "tool_invocation", {
                    "tool_namespace": self.namespace + (".mcp" if mcp else ".tools"),
                    "tool_name": name, "call_id": call_id, "arguments": {"json": arguments}},
                    correlation=call_id, extensions=extensions))

        for ordinal, (entry, top, payload) in enumerate(checked):
            if top == "turn_context":
                model = payload.get("model"); turn_id = payload.get("turn_id")
                if not _string(model): reject("CODEX_TURN_CONTEXT")
                if turn_id is not None and not _string(turn_id): reject("CODEX_TURN_CONTEXT")
                current_context = (turn_id if isinstance(turn_id, str) else None, str(model))
                if isinstance(turn_id, str): context_by_turn[turn_id] = str(model)
            elif top == "response_item":
                remember_response_id(payload)
                item_type = payload["type"]
                if item_type == "message":
                    if not isinstance(payload.get("content"), list) or not _string(payload.get("role")): reject("CODEX_RESPONSE_ITEM")
                    parts = []
                    for content in payload["content"]:
                        if not isinstance(content, dict) or not _string(content.get("type")): reject("CODEX_RESPONSE_ITEM")
                        if content["type"] in {"input_text", "output_text"} and isinstance(content.get("text"), str):
                            parts.append({"type": "text", "text": content["text"]})
                    if parts:
                        key, fallback = item_key(payload, ordinal, "message")
                        if key in mapped_items: reject("CODEX_DUPLICATE_ITEM")
                        mapped_items.add(key)
                        role = payload["role"] if payload["role"] in {"user", "assistant", "system", "developer", "tool"} else "unknown"
                        visibility = "control" if role in {"system", "developer"} else "tool_traffic" if role == "tool" else "user_visible" if role in {"user", "assistant"} else "unknown"
                        extra = _optional_scalars(payload, ("phase",))
                        passthrough = payload.get("internal_chat_message_metadata_passthrough")
                        turn_id = passthrough.get("turn_id") if isinstance(passthrough, dict) and _string(passthrough.get("turn_id")) else None
                        if turn_id: extra["turn_id"] = turn_id
                        message = writer.record("message", key, {"session_id": session["entity_id"], "source_ordinal": ordinal,
                            "role": role, "visibility": visibility, "content": parts}, extensions=self._extensions(entry, extra),
                            scheme="artifact-local" if fallback else "native-id", fallback=fallback,
                            location={"jsonl_line": ordinal + 1})
                        records.append(message)
                        model = context_by_turn.get(turn_id) if turn_id else (current_context[1] if current_context and current_context[0] is None else None)
                        if role == "assistant" and provider and model:
                            records.append(self._event(writer, session, ordinal, key + ":model-response", "model_response",
                                {"provider": provider, "model_name": model, "status": "unknown", "response_refs": [message["entity_id"]]},
                                extensions=self._extensions(entry)))
                elif item_type == "reasoning":
                    text_parts: list[dict[str, Any]] = []
                    for item in payload.get("summary", []):
                        if isinstance(item, dict) and isinstance(item.get("text"), str): text_parts.append({"type": "reasoning", "text": item["text"]})
                    content = payload.get("content")
                    if isinstance(content, list):
                        for item in content:
                            if isinstance(item, dict) and isinstance(item.get("text"), str): text_parts.append({"type": "reasoning", "text": item["text"]})
                    if text_parts:
                        key, fallback = item_key(payload, ordinal, "reasoning")
                        if key in mapped_items: reject("CODEX_DUPLICATE_ITEM")
                        mapped_items.add(key)
                        records.append(writer.record("message", key, {"session_id": session["entity_id"], "source_ordinal": ordinal,
                            "role": "assistant", "visibility": "exported_reasoning", "content": text_parts},
                            extensions=self._extensions(entry), scheme="artifact-local" if fallback else "native-id", fallback=fallback,
                            location={"jsonl_line": ordinal + 1}))
                elif item_type in {"function_call", "custom_tool_call"}:
                    field = "arguments" if item_type == "function_call" else "input"
                    if not isinstance(payload.get(field), str): reject("CODEX_TOOL")
                    prefix = "function-call" if item_type == "function_call" else "custom-tool-call"
                    extra = {"source_namespace": payload["namespace"]} if _string(payload.get("namespace")) else None
                    add_tool(entry, ordinal, f"{prefix}:{payload.get('call_id')}", payload.get("call_id"), payload.get("name"),
                             payload.get(field), extra=extra)
                elif item_type in {"function_call_output", "custom_tool_call_output"}:
                    output = payload.get("output")
                    if output is None: reject("CODEX_TOOL")
                    prefix = "function-call-output" if item_type == "function_call_output" else "custom-tool-call-output"
                    add_tool(entry, ordinal, f"{prefix}:{payload.get('call_id')}", payload.get("call_id"), None, output, result=True)
            elif top == "compacted":
                if not isinstance(payload.get("message"), str): reject("CODEX_COMPACTION")
                window_id = payload.get("window_id")
                extra = _optional_scalars(payload, ("message", "window_number", "first_window_id", "previous_window_id"))
                fallback = True
                key = f"compact:{ordinal}"
                if window_id is None:
                    pass
                elif _string(window_id):
                    extra["window_id"] = window_id
                    key, fallback = f"compact:{window_id}", False
                elif _integer(window_id) and window_id >= 0:
                    extra.setdefault("window_number", window_id)
                else:
                    reject("CODEX_COMPACTION")
                records.append(self._event(writer, session, ordinal, key, "lifecycle", {"status": "completed", "detail": "compacted"},
                    extensions=self._extensions(entry, extra), fallback=fallback))
            elif top == "event_msg":
                event_type = payload["type"]
                timing = _optional_scalars(payload, _TIMING)
                if event_type in {"task_started", "turn_started"}:
                    turn_id = payload.get("turn_id")
                    if not isinstance(turn_id, str) or not turn_id or turn_id in turns: reject("CODEX_TURN")
                    turns.add(turn_id)
                    records.append(writer.record("turn", turn_id, {"session_id": session["entity_id"], "source_ordinal": ordinal},
                        extensions=self._extensions(entry, timing), location={"jsonl_line": ordinal + 1}))
                    records.append(self._event(writer, session, ordinal, turn_id + ":started", "lifecycle",
                        {"status": "started", "detail": event_type}, extensions=self._extensions(entry, timing)))
                elif event_type in {"task_complete", "turn_complete"}:
                    turn_id = payload.get("turn_id")
                    if not isinstance(turn_id, str) or not turn_id: reject("CODEX_TURN")
                    status = "error" if isinstance(payload.get("error"), dict) else "completed"
                    records.append(self._event(writer, session, ordinal, turn_id + ":complete", "lifecycle",
                        {"status": status, "detail": event_type}, extensions=self._extensions(entry, timing)))
                elif event_type == "turn_aborted":
                    turn_id = payload.get("turn_id")
                    key = f"turn-aborted:{turn_id}" if _string(turn_id) else f"turn-aborted:{ordinal}"
                    records.append(self._event(writer, session, ordinal, key, "lifecycle", {"status": "cancelled", "detail": "turn_aborted"},
                        extensions=self._extensions(entry, timing), fallback=not _string(turn_id)))
                elif event_type == "error":
                    records.append(self._event(writer, session, ordinal, f"error:{ordinal}", "lifecycle",
                        {"status": "error", "detail": "error"}, extensions=self._extensions(entry), fallback=True))
                elif event_type == "context_compacted":
                    records.append(self._event(writer, session, ordinal, f"context-compacted:{ordinal}", "lifecycle",
                        {"status": "completed", "detail": "context_compacted"}, extensions=self._extensions(entry), fallback=True))
                elif event_type == "raw_response_completed":
                    usage = payload.get("token_usage")
                    response_id = payload.get("response_id")
                    if usage is not None:
                        mapped = self._usage(usage)
                        if mapped:
                            if _string(response_id):
                                records.append(self._event(writer, session, ordinal, f"raw-usage:{response_id}", "usage", mapped,
                                    correlation=response_id, extensions=self._extensions(entry, {"response_id": response_id})))
                            else:
                                records.append(self._event(writer, session, ordinal, f"raw-usage:{ordinal}", "usage", mapped,
                                    extensions=self._extensions(entry), fallback=True))
                elif event_type == "token_count":
                    info = payload.get("info")
                    if info is not None:
                        if not isinstance(info, dict): reject("CODEX_USAGE")
                        extra: dict[str, Any] = {"usage_kind": ""}
                        window = info.get("model_context_window")
                        if _integer(window): extra["model_context_window"] = window
                        for source_name, label in (("total_token_usage", "total"), ("last_token_usage", "last")):
                            if info.get(source_name) is not None:
                                mapped = self._usage(info[source_name])
                                if mapped:
                                    extra = dict(extra); extra["usage_kind"] = label
                                    records.append(self._event(writer, session, ordinal, f"token-count:{label}:{ordinal}", "usage", mapped,
                                        extensions=self._extensions(entry, extra), fallback=True))
                elif event_type == "exec_command_begin":
                    if not isinstance(payload.get("command"), list): reject("CODEX_TOOL")
                    add_tool(entry, ordinal, f"exec-begin:{payload.get('call_id')}", payload.get("call_id"), "exec_command", payload.get("command"))
                elif event_type == "exec_command_end":
                    output = payload.get("formatted_output")
                    if not isinstance(output, str): reject("CODEX_TOOL")
                    raw_status = payload.get("status")
                    status = {"completed": "ok", "failed": "error", "declined": "cancelled"}.get(raw_status, "unknown") if isinstance(raw_status, str) else "unknown"
                    add_tool(entry, ordinal, f"exec-end:{payload.get('call_id')}", payload.get("call_id"), None, output, result=True, status=status)
                elif event_type == "mcp_tool_call_begin":
                    invocation = payload.get("invocation")
                    if not isinstance(invocation, dict): reject("CODEX_TOOL")
                    extra = {"source_namespace": invocation["server"]} if _string(invocation.get("server")) else None
                    add_tool(entry, ordinal, f"mcp-begin:{payload.get('call_id')}", payload.get("call_id"), invocation.get("tool"),
                             invocation.get("arguments"), mcp=True, extra=extra)
                elif event_type == "mcp_tool_call_end":
                    result = payload.get("result")
                    status = "unknown"
                    if isinstance(result, dict) and "Ok" in result:
                        ok = result["Ok"]
                        status = "error" if isinstance(ok, dict) and (ok.get("is_error") is True or ok.get("isError") is True) else "ok"
                    elif isinstance(result, dict) and "Err" in result:
                        status = "error"
                    if "result" in payload:
                        add_tool(entry, ordinal, f"mcp-end:{payload.get('call_id')}", payload.get("call_id"), None, result, result=True, status=status, mcp=True)
                elif event_type == "web_search_begin":
                    add_tool(entry, ordinal, f"web-begin:{payload.get('call_id')}", payload.get("call_id"), "web_search", {})
                elif event_type == "web_search_end":
                    add_tool(entry, ordinal, f"web-end:{payload.get('call_id')}", payload.get("call_id"), None, payload.get("results", payload.get("query")), result=True)
                elif event_type == "dynamic_tool_call_response":
                    call_id = _call_id(payload)
                    content = payload.get("content_items", payload.get("contentItems"))
                    success = payload.get("success")
                    status = "ok" if success is True else "error" if success is False else "unknown"
                    if not isinstance(content, list): reject("CODEX_TOOL")
                    extra = {"source_namespace": payload["namespace"]} if _string(payload.get("namespace")) else None
                    add_tool(entry, ordinal, f"dynamic-response:{call_id}", call_id, None, content, result=True, status=status, extra=extra)
                elif event_type == "dynamic_tool_call_request":
                    call_id = _call_id(payload)
                    name = payload.get("tool")
                    arguments = payload.get("arguments")
                    if not _string(call_id) or not _string(name) or "arguments" not in payload: reject("CODEX_TOOL")
                    extra = {"source_namespace": payload["namespace"]} if _string(payload.get("namespace")) else None
                    add_tool(entry, ordinal, f"dynamic-request:{call_id}", call_id, name, arguments, extra=extra)
            elif top == "token_usage_record":
                usage = payload.get("usage")
                if usage is not None:
                    mapped = self._usage(usage)
                    if mapped:
                        response_id = payload.get("response_id")
                        if response_id is not None and not _string(response_id): reject("CODEX_USAGE")
                        key = f"token-usage:{response_id}" if response_id else f"token-usage:{ordinal}"
                        records.append(self._event(writer, session, ordinal, key, "usage", mapped,
                            correlation=response_id, extensions=self._extensions(entry, {"response_id": response_id} if response_id else None),
                            fallback=not bool(response_id)))
        return writer.write(records)
