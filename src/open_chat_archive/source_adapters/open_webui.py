"""OpenWebUI documented JSON export forms, preserving complete history graphs."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..adapter import BatchResult, FingerprintResult
from ..batch_writer import BatchWriter, read_regular, reject
from ..canonical import entity_identity
from ..projection import canonical_datetime, canonical_model, json_pointer
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


class OpenWebUIAdapter:
    namespace = "org.open-chat-archive.open-webui"
    version = "0.1.3"
    # Fixed adapter-specific ceilings for the documented complete export form.
    MAX_SOURCE_BYTES = 128 * 1024 * 1024
    PROFILE_MAX_ITEMS = 100_000

    def __init__(self, source_scope_id: str): self.source_scope_id = source_scope_id

    def recognize(self, profile: dict[str, Any]) -> FingerprintResult:
        paths = {item.get("path"): item.get("types", {}) for item in profile.get("paths", []) if isinstance(item, dict)}
        has = lambda path, kind: bool(paths.get(path, {}).get(kind))
        # Standard wrappers sort chat first; legacy entries sort history first.
        # Some documented wrappers place a boolean field before chat, shifting
        # its opaque alias while retaining the same nested history topology.
        standard = has("/*/k0001", "object") and has("/*/k0001/k0001", "object")
        legacy = has("/*/k0001", "object") and has("/*/k0001/k0002", "object")
        alternate_standard = (has("/*/k0001", "boolean") and has("/*/k0002", "object")
                              and has("/*/k0002/k0002", "object"))
        matched = (profile.get("profile_version") == "shape-only-v1" and has("/", "array")
                   and has("/*", "object") and (standard or legacy or alternate_standard))
        return FingerprintResult(bool(matched), "open-webui.export-array.v1" if matched else None)

    @staticmethod
    def _graph(history: Any) -> dict[str, dict[str, Any]]:
        if not isinstance(history, dict) or not isinstance(history.get("messages"), dict) or not isinstance(history.get("currentId"), str): reject("OPENWEBUI_HISTORY")
        nodes = history["messages"]
        if history["currentId"] not in nodes: reject("OPENWEBUI_CURRENT")
        for node_id, node in nodes.items():
            if not isinstance(node_id, str) or not node_id or not isinstance(node, dict): reject("OPENWEBUI_NODE")
            if node.get("id", node_id) != node_id or not isinstance(node.get("role"), str) or "content" not in node or "parentId" not in node or not isinstance(node.get("childrenIds"), list): reject("OPENWEBUI_NODE")
            parent = node["parentId"]
            if parent is not None and (not isinstance(parent, str) or parent not in nodes): reject("OPENWEBUI_PARENT")
            if len(node["childrenIds"]) != len(set(node["childrenIds"])) or any(not isinstance(child, str) or child not in nodes for child in node["childrenIds"]): reject("OPENWEBUI_CHILD")
        for node_id, node in nodes.items():
            if node["parentId"] is not None and node_id not in nodes[node["parentId"]]["childrenIds"]: reject("OPENWEBUI_TREE_INCONSISTENT")
            for child in node["childrenIds"]:
                if nodes[child]["parentId"] != node_id: reject("OPENWEBUI_TREE_INCONSISTENT")
        # Parent links form a functional graph; walk each chain to reject cycles.
        for node_id in nodes:
            seen: set[str] = set(); cursor: str | None = node_id
            while cursor is not None:
                if cursor in seen: reject("OPENWEBUI_CYCLE")
                seen.add(cursor); cursor = nodes[cursor]["parentId"]
        return nodes

    @staticmethod
    def _order(nodes: dict[str, dict[str, Any]]) -> list[str]:
        roots = sorted(node_id for node_id, node in nodes.items() if node["parentId"] is None)
        ordered: list[str] = []; seen: set[str] = set()
        def walk(node_id: str) -> None:
            if node_id in seen: return
            seen.add(node_id); ordered.append(node_id)
            for child in nodes[node_id]["childrenIds"]:
                walk(child)
        for root in roots: walk(root)
        for node_id in sorted(nodes): walk(node_id)
        return ordered

    @staticmethod
    def _thread_shape(nodes: dict[str, dict[str, Any]]) -> tuple[str, str | None]:
        """Classify the already-validated source graph without extending v0.1.

        A valid, acyclic functional parent graph has one root per connected
        component.  Multiple roots therefore describe a source forest, which
        v0.1 cannot express canonically.  The adapter preserves that more
        specific interpretation in its namespace while using canonical
        ``unknown``.
        """
        roots = sum(node["parentId"] is None for node in nodes.values())
        if roots > 1:
            return "unknown", "forest"
        if any(len(node["childrenIds"]) > 1 for node in nodes.values()):
            return "tree", None
        return "linear", None

    @staticmethod
    def _content(value: Any) -> list[dict[str, Any]]:
        if isinstance(value, str): return [{"type": "text", "text": value}]
        # Rich OpenWebUI content has no v0.1 canonical interpretation here, but
        # is retained as typed protected JSON only when it is canonical-safe.
        return [{"type": "json", "json": value}] if _safe(value) else []

    @staticmethod
    def _visibility(role: str) -> str:
        if role == "tool": return "tool_traffic"
        if role in {"system", "developer"}: return "control"
        if role == "unknown": return "unknown"
        return "user_visible"

    @staticmethod
    def _metadata(wrapper: dict[str, Any], chat: dict[str, Any]) -> dict[str, Any]:
        """Retain known fields from their documented wrapper or chat locations.

        Current exports keep most storage metadata on the wrapper while legacy
        exports place it directly in chat data.  If a future/hand-built input
        contains both locations, retain both rather than silently choosing one.
        """
        result: dict[str, Any] = {}
        for name in ("models", "options", "meta", "pinned", "folder_id", "created_at", "updated_at"):
            if name in wrapper and _safe(wrapper[name]):
                result[name] = wrapper[name]
            if chat is not wrapper and name in chat and _safe(chat[name]):
                destination = name if name not in result else "chat_" + name
                result[destination] = chat[name]
        return result

    def conform(self, source: Path, archive_root: Path, *, archive_id: str, batch_id: str) -> BatchResult:
        raw = read_regular(source, max_bytes=self.MAX_SOURCE_BYTES)
        try: exports = loads_source_bytes(raw)
        except StrictJSONError: reject("OPENWEBUI_JSON")
        if not isinstance(exports, list): reject("OPENWEBUI_ROOT")
        writer = BatchWriter(namespace=self.namespace, version=self.version, source_scope_id=self.source_scope_id,
            archive_id=archive_id, batch_id=batch_id, archive_root=archive_root, raw=raw,
            media_type="application/json", fingerprint="open-webui.export-array.v1", parsed=exports,
            profile_max_items=self.PROFILE_MAX_ITEMS)
        records: list[dict[str, Any]] = []; seen_sessions: set[str] = set(); seen_nodes: set[str] = set()
        for chat_ordinal, wrapper in enumerate(exports):
            if not isinstance(wrapper, dict): reject("OPENWEBUI_CHAT")
            # Standard export wraps the chat data; documented legacy entries are
            # themselves chat data. Metadata belongs to the outer wrapper.
            if "chat" in wrapper:
                if not isinstance(wrapper["chat"], dict): reject("OPENWEBUI_CHAT")
                chat, metadata = wrapper["chat"], wrapper
            else:
                chat, metadata = wrapper, wrapper
            nodes = self._graph(chat.get("history"))
            native = metadata.get("id", chat.get("id"))
            fallback = not (isinstance(native, str) and native)
            if fallback:
                native = "artifact-ordinal-" + str(chat_ordinal)
            assert isinstance(native, str)
            if native in seen_sessions: reject("OPENWEBUI_DUPLICATE_ID")
            seen_sessions.add(native)
            session_body: dict[str, Any] = {"archive_id": archive_id}
            title = metadata.get("title", chat.get("title"))
            if isinstance(title, str): session_body["title"] = title
            created = canonical_datetime(metadata.get("created_at", chat.get("created_at")))
            updated = canonical_datetime(metadata.get("updated_at", chat.get("updated_at")))
            if created: session_body["created_at"] = created
            if updated: session_body["updated_at"] = updated
            canonical_shape, extension_shape = self._thread_shape(nodes)
            session_body["thread_shape"] = canonical_shape
            models = metadata.get("models", chat.get("models"))
            if isinstance(models, list) and models and isinstance(models[0], str):
                model = canonical_model(models[0], provider="open-webui")
                if model: session_body["model"] = model
            session_meta = self._metadata(metadata, chat)
            if extension_shape:
                session_meta["thread_shape"] = extension_shape
            if chat is not metadata and "title" in metadata and "title" in chat:
                # The canonical session title uses wrapper precedence.  Preserve
                # a simultaneous nested title as source metadata instead of
                # overwriting it or claiming the two are equivalent.
                session_meta["chat_title"] = chat["title"]
            chat_prefix = (chat_ordinal, "chat") if chat is not metadata else (chat_ordinal,)
            session_pointer = json_pointer(chat_ordinal) if chat is metadata else json_pointer(chat_ordinal, "chat")
            node_records: dict[str, dict[str, Any]] = {}
            # Walk childrenIds so source_ordinal follows the source tree, not
            # lexicographic native IDs. Identity remains the native node key.
            order = self._order(nodes)
            # Session entity_id is needed on messages; record the session after
            # messages so active_message_id can name the source-selected node.
            session_identity = writer.identity("session", native, scheme="artifact-local" if fallback else "native-id", fallback=fallback)
            session_entity = entity_identity(session_identity)
            for ordinal, node_id in enumerate(order):
                node = nodes[node_id]
                global_key = native + ":" + node_id
                if global_key in seen_nodes: reject("OPENWEBUI_DUPLICATE_ID")
                seen_nodes.add(global_key)
                role = node["role"] if node["role"] in {"user", "assistant", "system", "developer", "tool"} else "unknown"
                meta = {key: node[key] for key in ("model", "done", "timestamp", "context", "parentId", "childrenIds")
                        if key in node and _safe(node[key])}
                if role == "unknown": meta["role"] = node["role"]
                message_body: dict[str, Any] = {"session_id": session_entity, "source_ordinal": ordinal,
                    "role": role, "visibility": self._visibility(role), "content": self._content(node["content"])}
                observed = canonical_datetime(node.get("timestamp"))
                if observed: message_body["created_at"] = observed
                model = canonical_model(node.get("model"), provider="open-webui")
                if model: message_body["model"] = model
                message_pointer = json_pointer(*chat_prefix, "history", "messages", node_id)
                message = writer.record("message", global_key, message_body,
                    extensions={self.namespace: meta} if meta else None,
                    scheme="artifact-local" if fallback else "native-id", fallback=fallback,
                    location={"json_pointer": message_pointer}, mapped_destinations=["/body"])
                node_records[node_id] = message; records.append(message)
            current = chat["history"]["currentId"]
            if current in node_records:
                session_body["active_message_id"] = node_records[current]["entity_id"]
            session = writer.record("session", native, session_body, extensions={self.namespace: session_meta} if session_meta else None,
                scheme="artifact-local" if fallback else "native-id", fallback=fallback,
                location={"json_pointer": session_pointer}, mapped_destinations=["/body"])
            records.append(session)
            for node_id in order:
                node = nodes[node_id]
                if node["parentId"] is not None:
                    parent = node["parentId"]
                    key = native + ":" + parent + ">" + node_id
                    sibling_index = list(nodes[parent]["childrenIds"]).index(node_id)
                    rel_pointer = json_pointer(*chat_prefix, "history", "messages", node_id, "parentId")
                    records.append(writer.record("relationship", key, {"from_id": node_records[parent]["entity_id"],
                        "to_id": node_records[node_id]["entity_id"], "kind": "parent_child", "sibling_index": sibling_index},
                        scheme="artifact-local" if fallback else "native-id", fallback=fallback,
                        location={"json_pointer": rel_pointer}, mapped_destinations=["/body"]))
            # currentId selects a source node, not necessarily a leaf or final
            # response. It is evidence metadata, not an instruction to discard
            # inactive branches.
            active = nodes[current]
            active_meta = {"currentId": current}
            if "done" in active and _safe(active["done"]):
                active_meta["active_done"] = active["done"]
            active_pointer = json_pointer(*chat_prefix, "history", "currentId")
            records.append(writer.record("event", native + ":active-leaf", {"session_id": session_entity, "source_ordinal": 0,
                "kind": "lifecycle", "message_id": node_records[current]["entity_id"],
                "payload": {"status": "completed" if active.get("done") is True else "unknown", "detail": "active_node"}},
                extensions={self.namespace: active_meta},
                scheme="artifact-local" if fallback else "native-id", fallback=fallback,
                location={"json_pointer": active_pointer}, mapped_destinations=["/body"]))
        return writer.write(records)
