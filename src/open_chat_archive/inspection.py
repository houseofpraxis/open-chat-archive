"""Read-only, non-normative inspection model for canonical archives.

This module loads only manifests and canonical NDJSON records after
``validate_archive`` succeeds. It never opens source or blob payloads.
"""
from __future__ import annotations

import time
from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from .strictjson import load_file, load_ndjson
from .topology import TopologyProjection, project_openwebui_topology
from .validation import ValidationError, validate_archive
from .source_summaries import source_summaries
from .source_registry import SourceRegistry


RecordKey = tuple[str, str]
TIMELINE_TYPES = {"message", "event", "span", "turn"}
COLLAPSED_DIAGNOSTICS = {"COARSE_EVIDENCE_LOCATION", "EXTENSION_ONLY_MODEL"}
CONTENT_TYPES = {"text", "reasoning", "code", "json", "image", "audio", "file", "resource_ref"}
ACTIVE_HINT_KEYS = {"active_node_id", "active_message_id", "current_node_id", "currentId"}


@dataclass(frozen=True)
class Occurrence:
    """One canonical-record occurrence in an immutable batch."""

    batch_id: str
    ordinal: int


@dataclass(frozen=True)
class Diagnostic:
    code: str
    severity: str
    scope: str
    category: str
    related_opaque_ids: tuple[str, ...] = ()
    count: int = 1
    associated_session_ids: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "severity": self.severity, "scope": self.scope,
                "category": self.category, "related_opaque_ids": list(self.related_opaque_ids),
                "count": self.count, "associated_session_ids": list(self.associated_session_ids)}


@dataclass(frozen=True)
class SessionProjection:
    key: RecordKey
    page_name: str
    record_keys: tuple[RecordKey, ...]
    message_keys: tuple[RecordKey, ...]
    relationship_keys: tuple[RecordKey, ...]
    timeline_keys: tuple[RecordKey, ...]
    components: tuple[tuple[RecordKey, ...], ...]
    roots: tuple[RecordKey, ...]
    leaves: tuple[RecordKey, ...]
    depth: int
    active_node: str | None
    topology: TopologyProjection | None = None
    literal_relationship_keys: tuple[RecordKey, ...] = ()


@dataclass(frozen=True)
class InspectionModel:
    archive_root: Path
    archive_id: str
    manifests: tuple[Mapping[str, Any], ...]
    records: Mapping[RecordKey, Mapping[str, Any]]
    occurrences: Mapping[RecordKey, tuple[Occurrence, ...]]
    sessions: tuple[SessionProjection, ...]
    record_sessions: Mapping[RecordKey, tuple[str, ...]]
    diagnostics: tuple[Diagnostic, ...]
    adapters: tuple[str, ...]
    validation_elapsed_ms: int
    source_registry: SourceRegistry | None = None

    def sessions_by_id(self, session_id: str) -> tuple[RecordKey, ...]:
        """Canonical records uniquely associated with this session entity."""
        return tuple(key for key, values in self.record_sessions.items() if values == (session_id,))

    @property
    def input_records(self) -> int:
        return sum(len(items) for items in self.occurrences.values())

    def report(self) -> dict[str, Any]:
        type_counts = Counter(record["record_type"] for record in self.records.values())
        occurrence_type_counts: Counter[str] = Counter()
        for key, occurrences in self.occurrences.items():
            occurrence_type_counts[self.records[key]["record_type"]] += len(occurrences)
        graph = {"roots": 0, "leaves": 0, "branches": 0, "components": 0, "max_depth": 0}
        for session in self.sessions:
            graph["roots"] += len(session.roots)
            graph["leaves"] += len(session.leaves)
            graph["components"] += len(session.components)
            graph["max_depth"] = max(graph["max_depth"], session.depth)
            outgoing = Counter(self.records[key]["body"].get("from_id") for key in session.relationship_keys)
            graph["branches"] += sum(1 for count in outgoing.values() if count > 1)
        coverage: Counter[str] = Counter()
        profile_count = 0
        for record in self.records.values():
            if record["record_type"] == "import_batch":
                profile_count += len(record["body"].get("profile_paths", []))
                coverage.update(item.get("disposition", "unknown") for item in record["body"].get("coverage_ledger", []))
        evidence: Counter[str] = Counter()
        roles: Counter[str] = Counter(); visibility: Counter[str] = Counter()
        content_types: Counter[str] = Counter(); event_kinds: Counter[str] = Counter(); namespaces: Counter[str] = Counter()
        for record in self.records.values():
            extensions = record.get("extensions", {})
            if isinstance(extensions, dict): namespaces.update(extensions.keys())
            if record["record_type"] == "message":
                roles.update([record["body"].get("role", "unknown")])
                visibility.update([record["body"].get("visibility", "unknown")])
                content_types.update(part.get("type", "unknown") for part in record["body"].get("content", []))
            if record["record_type"] == "event": event_kinds.update([record["body"].get("kind", "unknown")])
            if record["record_type"] == "observation":
                for binding in record["body"].get("bindings", []):
                    for source in binding.get("sources", []):
                        location = source.get("location", {})
                        evidence["coarse" if _coarse_location(location) else "precise"] += 1
        associated = sum(1 for key in self.records if self.record_sessions.get(key))
        diagnostic_counts = Counter()
        unassigned_diagnostic_counts = Counter()
        for item in self.diagnostics:
            diagnostic_counts[item.code] += item.count
            if not item.associated_session_ids:
                unassigned_diagnostic_counts[item.code] += item.count
        adapter_summaries = self._adapter_summaries()
        return {
            "inspector_version": "0.1-experimental",
            "archive_identifier": self.archive_id,
            "archive_path_label": self.archive_root.name,
            "validation": {"status": "passed", "elapsed_ms": self.validation_elapsed_ms},
            "adapters": list(self.adapters),
            "batch_metadata": [{"batch_id": manifest["batch_id"], "spec_version": manifest["spec_version"],
                                "profile": manifest["profile"], "adapter": manifest["adapter"]}
                               for manifest in self.manifests],
            "session_count": len(self.sessions),
            "record_accounting": {"input_records": self.input_records, "unique_records": len(self.records),
                                  "associated_records": associated,
                                  "unassociated_records": len(self.records) - associated,
                                  "projected_session_records": sum(len(session.record_keys) for session in self.sessions),
                                  "duplicate_occurrences": self.input_records - len(self.records)},
            "record_counts_by_type": dict(sorted(type_counts.items())),
            "record_occurrence_counts_by_type": dict(sorted(occurrence_type_counts.items())),
            "aggregate_metadata": {"roles": dict(sorted(roles.items())), "visibility": dict(sorted(visibility.items())),
                                   "content_types": dict(sorted(content_types.items())), "event_kinds": dict(sorted(event_kinds.items())),
                                   "extension_namespaces": dict(sorted(namespaces.items()))},
            "projection_accounting": {"chat_pages": len(self.sessions), "rendered_records": len(self.records),
                                       "unsupported_records": 0},
            "graph_metrics": graph,
            "evidence_metrics": dict(sorted(evidence.items())),
            "coverage_metrics": {"profile_paths": profile_count, "dispositions": dict(sorted(coverage.items()))},
            "adapter_summaries": adapter_summaries,
            "source_summaries": source_summaries(self, self.source_registry),
            "diagnostic_counts": dict(sorted(diagnostic_counts.items())),
            "unassigned_archive_diagnostic_counts": dict(sorted(unassigned_diagnostic_counts.items())),
            "diagnostics": [item.as_dict() for item in self.diagnostics],
            "session_topologies": [_topology_report(session) for session in self.sessions],
            "diagnostic_classes": {"normative_validation": "passed", "exploratory_findings": "diagnostics category exploratory",
                                   "projection_limitations": "diagnostics category projection_limitation"},
        }

    def _adapter_summaries(self) -> dict[str, Any]:
        buckets: dict[str, dict[str, Any]] = {}
        sessions_by_adapter: dict[str, set[str]] = defaultdict(set)
        for session in self.sessions:
            record = self.records[session.key]
            namespace = record.get("identity", {}).get("adapter_namespace", "unknown")
            bucket = buckets.setdefault(namespace, {"session_count": 0, "dates": [], "diagnostic_counts": Counter()})
            bucket["session_count"] += 1
            sessions_by_adapter[namespace].add(session.key[0])
            body = record["body"]
            for field in ("created_at", "updated_at"):
                value = body.get(field)
                if isinstance(value, str): bucket["dates"].append(value)
            for key in session.message_keys:
                value = self.records[key]["body"].get("created_at")
                if isinstance(value, str): bucket["dates"].append(value)
        for item in self.diagnostics:
            for namespace, session_ids in sessions_by_adapter.items():
                affected = set(item.associated_session_ids) & session_ids
                if affected:
                    # Each collapsed item has one exact attribution tuple, so
                    # its count cannot be a union-derived global total here.
                    buckets[namespace]["diagnostic_counts"][item.code] += item.count
                    buckets[namespace].setdefault("affected", set()).update(affected)
        summaries: dict[str, Any] = {}
        for namespace, bucket in sorted(buckets.items()):
            dates = sorted(bucket["dates"])
            counts = dict(sorted(bucket["diagnostic_counts"].items()))
            summaries[namespace] = {"session_count": bucket["session_count"],
                                    "earliest_created_at": dates[0] if dates else None,
                                    "latest_created_at": dates[-1] if dates else None,
                                    "diagnostic_total": sum(counts.values()),
                                    "diagnostic_counts": counts,
                                    "affected_chat_count": len(bucket.get("affected", set()))}
        return summaries


def stable_validation_code(error: ValidationError) -> str:
    """Return only the validator's stable code, never a record value."""
    return str(error).split(" ", 1)[0]


def page_name(key: RecordKey) -> str:
    """Opaque filename derived solely from canonical opaque digest identifiers."""
    return "chat-" + key[0][7:] + "-" + key[1][7:] + ".html"


def load_archive(archive_root: Path, *, schema_root: Path | None = None,
                 source_registry: SourceRegistry | None = None) -> InspectionModel:
    """Validate then load an archive into a frozen, provenance-retaining model."""
    started = time.perf_counter()
    batch_dirs = validate_archive(archive_root, schema_root=schema_root)
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    manifests: list[Mapping[str, Any]] = []
    records: dict[RecordKey, Mapping[str, Any]] = {}
    occurrences: dict[RecordKey, list[Occurrence]] = defaultdict(list)
    for batch_dir in sorted(batch_dirs):
        manifest = load_file(batch_dir / "manifest.json")
        manifests.append(MappingProxyType(deepcopy(manifest)))
        for ordinal, record in enumerate(load_ndjson((batch_dir / "records.ndjson").read_bytes()), 1):
            key = (record["entity_id"], record["revision_id"])
            records.setdefault(key, MappingProxyType(deepcopy(record)))
            occurrences[key].append(Occurrence(manifest["batch_id"], ordinal))
    archive_id = manifests[0]["archive_id"]
    adapters = tuple(sorted({manifest["adapter"]["namespace"] for manifest in manifests}))
    frozen_records = MappingProxyType(records)
    frozen_occurrences = MappingProxyType({key: tuple(value) for key, value in occurrences.items()})
    record_sessions, diagnostics = _associate_records(frozen_records)
    diagnostics.extend(_archive_diagnostics(frozen_records))
    sessions, graph_diagnostics = _project_sessions(frozen_records, record_sessions)
    attributed = _attribute_diagnostics(diagnostics + graph_diagnostics, record_sessions, frozen_records)
    collapsed = _collapse_diagnostics(attributed)
    return InspectionModel(archive_root=archive_root, archive_id=archive_id, manifests=tuple(manifests),
                           records=frozen_records, occurrences=frozen_occurrences, sessions=tuple(sessions),
                           record_sessions=MappingProxyType(record_sessions),
                           diagnostics=tuple(collapsed), adapters=adapters, validation_elapsed_ms=elapsed_ms,
                           source_registry=source_registry)


def _topology_report(session: SessionProjection) -> dict[str, Any]:
    topology = session.topology
    if topology is None:
        return {"session_key": list(session.key), "declared_shape": None, "derived_shape": "unknown",
                "basis": "incomplete_or_unknown", "components": [], "active_node": None,
                "active_children": []}
    labels = _component_labels(topology.components, topology.derived_shape)
    return {"session_key": list(session.key), "declared_shape": topology.declared_shape,
            "derived_shape": topology.derived_shape, "basis": topology.basis,
            "components": [{"label": label, "message_keys": [list(key) for key in component],
                            "roots": [list(key) for key in component if key in topology.roots],
                            "contains_active": any(key[0] == topology.active_node for key in component)}
                           for label, component in zip(labels, topology.components)],
            "active_node": topology.active_node,
            "active_children": [list(key) for key in topology.active_children]}


def _component_labels(components: tuple[tuple[RecordKey, ...], ...], shape: str) -> tuple[str, ...]:
    prefix = "Thread " if shape == "forest" else "Component "
    return tuple(prefix + _alpha_label(index) for index in range(len(components)))


def _alpha_label(index: int) -> str:
    result = ""
    value = index + 1
    while value:
        value, remainder = divmod(value - 1, 26)
        result = chr(ord("A") + remainder) + result
    return result


def _attribute_diagnostics(diagnostics: list[Diagnostic], associations: Mapping[RecordKey, tuple[str, ...]],
                           records: Mapping[RecordKey, Mapping[str, Any]]) -> list[Diagnostic]:
    by_entity: dict[str, set[str]] = defaultdict(set)
    session_ids = {key[0] for key, record in records.items() if record["record_type"] == "session"}
    for key, values in associations.items(): by_entity[key[0]].update(values)
    result: list[Diagnostic] = []
    for item in diagnostics:
        sessions: set[str] = set(item.associated_session_ids)
        for opaque_id in item.related_opaque_ids:
            if opaque_id in session_ids: sessions.add(opaque_id)
            sessions.update(by_entity.get(opaque_id, set()))
        result.append(Diagnostic(item.code, item.severity, item.scope, item.category,
                                 item.related_opaque_ids, item.count, tuple(sorted(sessions))))
    return result


def _associate_records(records: Mapping[RecordKey, Mapping[str, Any]]) -> tuple[dict[RecordKey, tuple[str, ...]], list[Diagnostic]]:
    by_entity: dict[str, list[RecordKey]] = defaultdict(list)
    for key in records: by_entity[key[0]].append(key)
    associations: dict[RecordKey, set[str]] = defaultdict(set)
    sessions = {key[0] for key, record in records.items() if record["record_type"] == "session"}
    for key, record in records.items():
        body = record["body"]
        if record["record_type"] == "session": associations[key].add(key[0])
        elif isinstance(body, dict) and body.get("session_id") in sessions: associations[key].add(body["session_id"])
    changed = True
    while changed:
        changed = False
        for key, record in records.items():
            body = record["body"]
            candidates: set[str] = set()
            if record["record_type"] == "observation":
                subject = body.get("subject", {})
                candidates.update(associations.get((subject.get("entity_id"), subject.get("revision_id")), set()))
            elif record["record_type"] == "revision":
                candidates.update(associations.get((body.get("entity_id"), body.get("from_revision_id")), set()))
                candidates.update(associations.get((body.get("entity_id"), body.get("to_revision_id")), set()))
            elif record["record_type"] == "relationship":
                for endpoint in (body.get("from_id"), body.get("to_id")):
                    for endpoint_key in by_entity.get(endpoint, []): candidates.update(associations.get(endpoint_key, set()))
            if len(candidates) == 1 and not candidates <= associations[key]:
                associations[key].update(candidates); changed = True
    diagnostics: list[Diagnostic] = []
    for key, record in records.items():
        if not associations.get(key):
            diagnostics.append(Diagnostic("UNASSOCIATED_RECORD", "info", "archive", "projection_limitation", (key[0], key[1])))
        extensions = record.get("extensions", {})
        if isinstance(extensions, dict):
            for value in extensions.values():
                if not isinstance(value, dict):
                    diagnostics.append(Diagnostic("UNKNOWN_EXTENSION_SHAPE", "info", "record", "projection_limitation", (key[0],)))
                if _contains_model(value) and "model" not in record.get("body", {}):
                    diagnostics.append(Diagnostic("EXTENSION_ONLY_MODEL", "info", "record", "exploratory", (key[0],)))
    return {key: tuple(sorted(value)) for key, value in associations.items()}, diagnostics


def _archive_diagnostics(records: Mapping[RecordKey, Mapping[str, Any]]) -> list[Diagnostic]:
    by_entity: dict[str, list[RecordKey]] = defaultdict(list)
    linked: set[RecordKey] = set()
    diagnostics: list[Diagnostic] = []
    for key, record in records.items():
        by_entity[key[0]].append(key)
        if record["record_type"] == "revision":
            body = record["body"]
            linked.add((body.get("entity_id"), body.get("from_revision_id")))
            linked.add((body.get("entity_id"), body.get("to_revision_id")))
    for entity_id, keys in by_entity.items():
        if len(keys) > 1 and any(item not in linked for item in keys):
            diagnostics.append(Diagnostic("AMBIGUOUS_REVISION", "info", "entity", "projection_limitation", (entity_id,)))
    for record in records.values():
        if record["record_type"] != "import_batch": continue
        ledger = record["body"].get("coverage_ledger", [])
        if ledger and all(item.get("disposition") == "preserved" for item in ledger):
            diagnostics.append(Diagnostic("COVERAGE_PRESERVED_ONLY", "info", "batch", "exploratory", (record["entity_id"],)))
    for key, record in records.items():
        if record["record_type"] != "observation": continue
        for binding in record["body"].get("bindings", []):
            for source in binding.get("sources", []):
                location = source.get("location", {})
                if _coarse_location(location):
                    diagnostics.append(Diagnostic("COARSE_EVIDENCE_LOCATION", "info", "observation", "exploratory", (key[0],)))
    return diagnostics


def _contains_model(value: Any) -> bool:
    if isinstance(value, dict): return any("model" in str(key).lower() or _contains_model(item) for key, item in value.items())
    if isinstance(value, list): return any(_contains_model(item) for item in value)
    return False


def _project_sessions(records: Mapping[RecordKey, Mapping[str, Any]], associations: Mapping[RecordKey, tuple[str, ...]]) -> tuple[list[SessionProjection], list[Diagnostic]]:
    session_keys = sorted(key for key, record in records.items() if record["record_type"] == "session")
    projections: list[SessionProjection] = []
    diagnostics: list[Diagnostic] = []
    for session_key in session_keys:
        session_id = session_key[0]
        members = tuple(sorted(key for key, value in associations.items() if value == (session_id,)))
        messages = tuple(key for key in members if records[key]["record_type"] == "message")
        message_ids = {key[0] for key in messages}
        relationships = tuple(key for key in members if records[key]["record_type"] == "relationship")
        # Relationship association deliberately stays conservative: a
        # cross-session record is not assigned to either chat.  Topology proof
        # must nevertheless see every parent_child record that touches one of
        # this session's messages, including a foreign or non-message endpoint.
        topology_relationships = tuple(sorted(
            key for key, record in records.items()
            if record["record_type"] == "relationship"
            and record.get("body", {}).get("kind") == "parent_child"
            and ({record["body"].get("from_id"), record["body"].get("to_id")} & message_ids)
        ))
        timeline = tuple(sorted((key for key in members if records[key]["record_type"] in TIMELINE_TYPES),
                                key=lambda key: (records[key]["body"].get("source_ordinal", 10 ** 9), records[key]["record_type"], key)))
        _ordinal_diagnostics(records, members, session_id, diagnostics)
        active = records[session_key]["body"].get("active_message_id") if isinstance(records[session_key]["body"].get("active_message_id"), str) else None
        if active is None:
            active = _resolve_active(records, members, message_ids, records[session_key])
        if active:
            if active not in message_ids: diagnostics.append(Diagnostic("ACTIVE_NODE_UNRESOLVED", "warning", "session", "projection_limitation", (session_id,)))
        topology = project_openwebui_topology(records, session_key, messages, topology_relationships, active)
        for code in topology.diagnostic_codes:
            diagnostics.append(Diagnostic(code, "warning" if topology.basis == "invalid_shape" else "info",
                                          "session", "projection_limitation", (session_id,)))
        # A complete native proof can establish a forest even when every
        # component is an isolated source root.  Unknown graphs, on the other
        # hand, need an observed parent-child edge before these are warnings.
        confirmed_forest = (topology.basis == "complete_parent_child"
                             and topology.derived_shape == "forest")
        has_topology_evidence = confirmed_forest or bool(topology.parent_child_keys)
        if has_topology_evidence and len(topology.roots) > 1:
            diagnostics.append(Diagnostic("MULTIPLE_SESSION_ROOTS",
                                          "info" if confirmed_forest else "warning",
                                          "session", "exploratory", (session_id,)))
        if has_topology_evidence and len(topology.components) > 1:
            diagnostics.append(Diagnostic("DISCONNECTED_SESSION_GRAPH",
                                          "info" if confirmed_forest else "warning",
                                          "session", "exploratory", (session_id,)))
        if topology.active_node and topology.active_children:
            diagnostics.append(Diagnostic("ACTIVE_NODE_NOT_LEAF", "info", "session", "exploratory", (session_id, topology.active_node)))
        if _ambiguous_sibling_order(records, topology.parent_child_keys):
            diagnostics.append(Diagnostic("AMBIGUOUS_SIBLING_ORDER", "info", "session", "projection_limitation", (session_id,)))
        if messages and not _has_canonical_timestamp(records, messages): diagnostics.append(Diagnostic("MISSING_CANONICAL_TIMESTAMP", "info", "session", "exploratory", (session_id,)))
        for message_key in messages:
            for part in records[message_key]["body"].get("content", []):
                if part.get("type") not in CONTENT_TYPES:
                    diagnostics.append(Diagnostic("UNRENDERED_CONTENT_TYPE", "warning", "message", "projection_limitation", (message_key[0],)))
        projections.append(SessionProjection(session_key, page_name(session_key), members, messages,
                                             topology.parent_child_keys, timeline, topology.components,
                                             topology.roots, topology.leaves, topology.depth,
                                             topology.active_node, topology, relationships))
    return projections, diagnostics


def _by_ordinal(records: Mapping[RecordKey, Mapping[str, Any]], keys: tuple[RecordKey, ...]) -> tuple[RecordKey, ...]:
    return tuple(sorted(keys, key=lambda key: (records[key]["body"].get("source_ordinal", 10 ** 9), key)))


def _ambiguous_sibling_order(records: Mapping[RecordKey, Mapping[str, Any]], relationship_keys: tuple[RecordKey, ...]) -> bool:
    by_parent: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for key in relationship_keys:
        body = records[key]["body"]
        by_parent[body.get("from_id")].append(body)
    for edges in by_parent.values():
        if len(edges) > 1:
            values = [edge.get("sibling_index") for edge in edges]
            if any(not isinstance(value, int) for value in values) or len(values) != len(set(values)):
                return True
    return False


def _coarse_location(location: Mapping[str, Any]) -> bool:
    if location.get("jsonl_line") or location.get("markdown_lines"):
        return False
    pointer = location.get("json_pointer")
    return not (isinstance(pointer, str) and pointer != "")


def _ordinal_diagnostics(records: Mapping[RecordKey, Mapping[str, Any]], members: tuple[RecordKey, ...], session_id: str, diagnostics: list[Diagnostic]) -> None:
    by_type: dict[str, list[int]] = defaultdict(list)
    for key in members:
        body = records[key]["body"]
        if isinstance(body, dict) and isinstance(body.get("source_ordinal"), int):
            by_type[records[key]["record_type"]].append(body["source_ordinal"])
    for ordinals in by_type.values():
        if len(ordinals) != len(set(ordinals)): diagnostics.append(Diagnostic("SOURCE_ORDINAL_DUPLICATE", "info", "session", "exploratory", (session_id,)))
        if ordinals and set(ordinals) != set(range(min(ordinals), max(ordinals) + 1)): diagnostics.append(Diagnostic("SOURCE_ORDINAL_GAP", "info", "session", "exploratory", (session_id,)))


def _hint_from(value: Any) -> str | None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ACTIVE_HINT_KEYS and isinstance(item, str) and item: return item
            found = _hint_from(item)
            if found: return found
    elif isinstance(value, list):
        for item in value:
            found = _hint_from(item)
            if found: return found
    return None


def _resolve_active(records: Mapping[RecordKey, Mapping[str, Any]], members: tuple[RecordKey, ...], message_ids: set[str], session: Mapping[str, Any]) -> str | None:
    hint = _hint_from(session.get("extensions", {}))
    if hint is None:
        for key in members:
            record = records[key]
            if record["record_type"] == "event" and record["body"].get("kind") == "lifecycle":
                hint = _hint_from(record.get("extensions", {}))
                if hint: break
    if hint is None: return None
    if hint in message_ids: return hint
    session_key = session.get("identity", {}).get("logical_key")
    prefix = session_key + ":" if isinstance(session_key, str) else None
    for key in members:
        record = records[key]
        if record["record_type"] != "message": continue
        logical = record.get("identity", {}).get("logical_key", "")
        if not isinstance(logical, str): continue
        if logical == hint or (prefix is not None and logical.startswith(prefix) and logical[len(prefix):] == hint):
            return record["entity_id"]
    return hint


def _has_canonical_timestamp(records: Mapping[RecordKey, Mapping[str, Any]], messages: tuple[RecordKey, ...]) -> bool:
    return any(records[key]["body"].get("created_at") or records[key]["body"].get("timestamp") for key in messages)


def _collapse_diagnostics(diagnostics: list[Diagnostic]) -> list[Diagnostic]:
    kept: list[Diagnostic] = []
    buckets: dict[tuple[str, str, str, tuple[str, ...]], list[Diagnostic]] = defaultdict(list)
    for item in diagnostics:
        if item.code in COLLAPSED_DIAGNOSTICS:
            buckets[(item.code, item.severity, item.category, item.associated_session_ids)].append(item)
        else: kept.append(item)
    for (code, severity, category, session_ids), items in buckets.items():
        kept.append(Diagnostic(code, severity, "archive", category, (),
                               sum(item.count for item in items), session_ids))
    return kept
