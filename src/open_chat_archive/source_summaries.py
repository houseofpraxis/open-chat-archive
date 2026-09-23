"""Per-explicit-scope inspection summaries; never infer source equivalence."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from typing import Any, Mapping, TYPE_CHECKING

if TYPE_CHECKING:
    from .inspection import InspectionModel
    from .source_registry import SourceRegistry


def _identity(record: Mapping[str, Any]) -> tuple[str, str, str] | None:
    value = record.get("identity", {})
    items = (value.get("archive_id"), value.get("adapter_namespace"), value.get("source_scope_id"))
    return items if all(isinstance(item, str) for item in items) else None


def _instant(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def source_summaries(model: "InspectionModel", registry: "SourceRegistry | None" = None) -> list[dict[str, Any]]:
    """Return sorted rows for observed canonical scope triples only."""
    records_by_scope: dict[tuple[str, str, str], set[tuple[str, str]]] = defaultdict(set)
    sessions_by_scope: dict[tuple[str, str, str], set[tuple[str, str]]] = defaultdict(set)
    # Diagnostics name session entities rather than revisions. Retain every
    # observed scope for an entity so attribution cannot depend on iteration.
    session_scopes: dict[str, set[tuple[str, str, str]]] = defaultdict(set)
    for key, record in model.records.items():
        scope = _identity(record)
        if scope is None:
            continue
        records_by_scope[scope].add(key)
        if record["record_type"] == "session":
            sessions_by_scope[scope].add(key)
            session_scopes[key[0]].add(scope)
    rows: list[dict[str, Any]] = []
    for scope in sorted(records_by_scope):
        sessions = sessions_by_scope.get(scope, set())
        chat_ids = {key[0] for key in sessions}
        dates: list[tuple[datetime, str]] = []
        dated_chats: set[str] = set()
        for session in sessions:
            values: list[Any] = [model.records[session]["body"].get(field) for field in ("created_at", "updated_at")]
            values.extend(
                model.records[key]["body"].get("created_at")
                for key in model.sessions_by_id(session[0])
                if model.records[key]["record_type"] == "message"
                and _identity(model.records[key]) == scope
            )
            for value in values:
                parsed = _instant(value)
                if parsed is not None:
                    dates.append((parsed, value))
                    dated_chats.add(session[0])
        matching = registry.resolve(*scope) if registry is not None else None
        counts: Counter[str] = Counter()
        affected: set[str] = set()
        shared_total = 0
        for diagnostic in model.diagnostics:
            affected_here = set(diagnostic.associated_session_ids) & chat_ids
            if not affected_here:
                continue
            counts[diagnostic.code] += diagnostic.count
            affected.update(affected_here)
            diagnostic_scopes = {diagnostic_scope for session_id in diagnostic.associated_session_ids
                                 for diagnostic_scope in session_scopes.get(session_id, ())}
            if len(diagnostic_scopes) > 1:
                shared_total += diagnostic.count
        # Equal instants can have distinct spellings (Z and +00:00). Include
        # the original timestamp so set iteration cannot choose the result.
        earliest = min(dates, key=lambda item: (item[0], item[1]))[1] if dates else None
        latest = max(dates, key=lambda item: (item[0], item[1]))[1] if dates else None
        row: dict[str, Any] = {
            "archive_id": scope[0], "adapter_namespace": scope[1], "source_scope_id": scope[2],
            "source_id": matching.get("source_id") if matching else None,
            "registry_status": matching.get("status") if matching else "unregistered",
            "display_label": matching.get("display_label") if matching else None,
            "account_identifier": matching.get("account_identifier") if matching else None,
            "instance_identifier": matching.get("instance_identifier") if matching else None,
            "category": matching.get("category") if matching else None,
            "chat_count": len(chat_ids), "session_revision_count": len(sessions),
            "observed_scope_record_count": len(records_by_scope[scope]),
            "date_range": {"earliest": earliest, "latest": latest,
                           "basis": "canonical session created_at/updated_at and message created_at"},
            "with_date_count": len(dated_chats), "without_date_count": len(chat_ids - dated_chats),
            "diagnostic_total": sum(counts.values()), "diagnostic_counts": dict(sorted(counts.items())),
            "affected_chat_count": len(affected), "shared_diagnostic_total": shared_total,
        }
        rows.append(row)
    return rows
