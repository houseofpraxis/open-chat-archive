"""Conservative, read-only topology proof for OpenWebUI canonical records.

This module deliberately proves a graph only when the native OpenWebUI node
metadata and canonical ``parent_child`` relationships agree exactly.  It is an
inspector convenience, not a schema interpretation or repair mechanism.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Mapping


RecordKey = tuple[str, str]
OPENWEBUI_NAMESPACE = "org.open-chat-archive.open-webui"


@dataclass(frozen=True)
class TopologyProjection:
    """Safe topology values plus the basis that permits their interpretation."""

    declared_shape: str | None
    derived_shape: str
    basis: str
    components: tuple[tuple[RecordKey, ...], ...]
    roots: tuple[RecordKey, ...]
    leaves: tuple[RecordKey, ...]
    depth: int
    active_node: str | None
    active_children: tuple[RecordKey, ...]
    parent_child_keys: tuple[RecordKey, ...]
    diagnostic_codes: tuple[str, ...] = ()


def project_openwebui_topology(
    records: Mapping[RecordKey, Mapping[str, Any]], session_key: RecordKey,
    message_keys: tuple[RecordKey, ...], relationship_keys: tuple[RecordKey, ...],
    active_node: str | None,
) -> TopologyProjection:
    """Return an observed graph and a narrowly justified OpenWebUI proof basis."""
    session = records[session_key]
    declared = session.get("body", {}).get("thread_shape")
    declared_shape = declared if isinstance(declared, str) else None
    message_ids = {key[0] for key in message_keys}
    parent_keys = tuple(key for key in relationship_keys
                        if records[key].get("body", {}).get("kind") == "parent_child")
    empty = dict(declared_shape=declared_shape, derived_shape="unknown", basis="incomplete_or_unknown",
                 components=(), roots=(), leaves=(), depth=0,
                 active_node=active_node if active_node in message_ids else None,
                 active_children=(), parent_child_keys=())
    # Do this before entity-keyed graph construction.  A key lookup cannot
    # faithfully represent two revisions of the same topology participant.
    if _revision_ambiguous(records, session_key, message_keys, parent_keys):
        return TopologyProjection(**{**empty, "basis": "revision_ambiguous",
                                    "diagnostic_codes": ("REVISION_AMBIGUOUS",)})
    # The exporter rejects this case, but the projection itself must never
    # turn an empty internal selection into a confirmed linear graph.
    if not message_keys:
        return TopologyProjection(**{**empty, "diagnostic_codes": ("TOPOLOGY_PROOF_NO_MESSAGES",)})
    outgoing, incoming, endpoint_problem, duplicate_edges = _canonical_edges(records, parent_keys, message_ids)
    components = _components(message_keys, outgoing, incoming)
    roots = _ordered(message_keys, (key[0] for key in message_keys if not incoming.get(key[0])))
    leaves = _ordered(message_keys, (key[0] for key in message_keys if not outgoing.get(key[0])))
    cycle = _has_cycle(message_ids, outgoing)
    multi_parent = any(len(parents) > 1 for parents in incoming.values())
    active_children = _ordered(message_keys, outgoing.get(active_node or "", set()))
    base = dict(declared_shape=declared_shape, derived_shape="unknown", basis="incomplete_or_unknown",
                components=components, roots=roots, leaves=leaves, depth=_depth(roots, outgoing),
                active_node=active_node if active_node in message_ids else None,
                active_children=active_children, parent_child_keys=parent_keys)
    if endpoint_problem or duplicate_edges or cycle or multi_parent:
        codes: list[str] = []
        if endpoint_problem: codes.append("INVALID_PARENT_CHILD_ENDPOINT")
        if duplicate_edges: codes.append("DUPLICATE_PARENT_CHILD_EDGE")
        if cycle: codes.append("PARENT_CHILD_CYCLE")
        if multi_parent: codes.append("MULTIPLE_PARENT_CHILD_PARENTS")
        return TopologyProjection(**{**base, "basis": "invalid_shape", "diagnostic_codes": tuple(codes)})
    proof = _native_proof(records, session_key, message_keys, parent_keys)
    if proof is not None:
        return TopologyProjection(**{**base, "basis": "incomplete_or_unknown", "diagnostic_codes": (proof,)})
    if len(components) > 1:
        shape = "forest"
    elif any(len(children) > 1 for children in outgoing.values()):
        shape = "tree"
    else:
        shape = "linear"
    return TopologyProjection(**{**base, "derived_shape": shape, "basis": "complete_parent_child"})


def _native_proof(records: Mapping[RecordKey, Mapping[str, Any]], session_key: RecordKey,
                  message_keys: tuple[RecordKey, ...], parent_keys: tuple[RecordKey, ...]) -> str | None:
    session = records[session_key]
    identity = session.get("identity", {})
    prefix = identity.get("logical_key")
    if identity.get("adapter_namespace") != OPENWEBUI_NAMESPACE or not isinstance(prefix, str):
        return "TOPOLOGY_PROOF_UNAVAILABLE"
    prefix += ":"
    native: dict[str, RecordKey] = {}
    parents: dict[str, str | None] = {}
    children: dict[str, tuple[str, ...]] = {}
    for key in message_keys:
        record = records[key]
        logical = record.get("identity", {}).get("logical_key")
        # Prefix slicing preserves native IDs that themselves contain colons.
        if record.get("identity", {}).get("adapter_namespace") != OPENWEBUI_NAMESPACE or not isinstance(logical, str) or not logical.startswith(prefix):
            return "TOPOLOGY_PROOF_MESSAGE_IDENTITY"
        native_id = logical[len(prefix):]
        if not native_id or native_id in native:
            return "TOPOLOGY_PROOF_MESSAGE_IDENTITY"
        extension = record.get("extensions", {}).get(OPENWEBUI_NAMESPACE)
        if not isinstance(extension, dict) or "parentId" not in extension or "childrenIds" not in extension:
            return "TOPOLOGY_PROOF_NATIVE_METADATA"
        parent = extension["parentId"]
        child_ids = extension["childrenIds"]
        if parent is not None and not isinstance(parent, str):
            return "TOPOLOGY_PROOF_NATIVE_METADATA"
        if not isinstance(child_ids, list) or any(not isinstance(item, str) for item in child_ids) or len(child_ids) != len(set(child_ids)):
            return "TOPOLOGY_PROOF_NATIVE_METADATA"
        native[native_id] = key; parents[native_id] = parent; children[native_id] = tuple(child_ids)
    if set(native.values()) != set(message_keys):
        return "TOPOLOGY_PROOF_MESSAGE_IDENTITY"
    expected: set[tuple[str, str]] = set()
    for node, parent in parents.items():
        if parent is not None:
            if parent not in native or node not in children.get(parent, ()):
                return "TOPOLOGY_PROOF_NATIVE_RECIPROCITY"
            expected.add((parent, node))
        for child in children[node]:
            if child not in native or parents.get(child) != node:
                return "TOPOLOGY_PROOF_NATIVE_RECIPROCITY"
    actual: list[tuple[str, str]] = []
    entity_native = {key[0]: node for node, key in native.items()}
    for key in parent_keys:
        body = records[key]["body"]
        actual.append((entity_native.get(body.get("from_id"), ""), entity_native.get(body.get("to_id"), "")))
    if len(actual) != len(set(actual)) or set(actual) != expected:
        return "TOPOLOGY_PROOF_CANONICAL_EDGES"
    return None


def _canonical_edges(records: Mapping[RecordKey, Mapping[str, Any]], keys: tuple[RecordKey, ...], message_ids: set[str]):
    outgoing: dict[str, set[str]] = defaultdict(set); incoming: dict[str, set[str]] = defaultdict(set)
    endpoint_problem = False; pairs: list[tuple[str, str]] = []
    for key in keys:
        body = records[key].get("body", {})
        left, right = body.get("from_id"), body.get("to_id")
        if left not in message_ids or right not in message_ids:
            endpoint_problem = True; continue
        pairs.append((left, right)); outgoing[left].add(right); incoming[right].add(left)
    return outgoing, incoming, endpoint_problem, len(pairs) != len(set(pairs))


def _revision_ambiguous(records: Mapping[RecordKey, Mapping[str, Any]], session_key: RecordKey,
                        message_keys: tuple[RecordKey, ...], parent_keys: tuple[RecordKey, ...]) -> bool:
    selected = {session_key, *message_keys, *parent_keys}
    entities: dict[str, set[str]] = defaultdict(set)
    for key in records:
        entities[key[0]].add(key[1])
    return any(len(entities[key[0]]) > 1 for key in selected)


def _ordered(keys: tuple[RecordKey, ...], entity_ids: Any) -> tuple[RecordKey, ...]:
    lookup = {key[0]: key for key in keys}
    return tuple(sorted((lookup[item] for item in entity_ids if item in lookup), key=lambda key: key))


def _components(keys: tuple[RecordKey, ...], outgoing: Mapping[str, set[str]], incoming: Mapping[str, set[str]]) -> tuple[tuple[RecordKey, ...], ...]:
    lookup = {key[0]: key for key in keys}; unseen = set(lookup); result: list[tuple[RecordKey, ...]] = []
    while unseen:
        pending = [min(unseen)]; found: set[str] = set()
        while pending:
            node = pending.pop()
            if node in found: continue
            found.add(node); unseen.discard(node)
            pending.extend((outgoing.get(node, set()) | incoming.get(node, set())) - found)
        result.append(tuple(sorted((lookup[node] for node in found), key=lambda key: key)))
    return tuple(sorted(result, key=lambda component: component))


def _has_cycle(nodes: set[str], outgoing: Mapping[str, set[str]]) -> bool:
    visiting: set[str] = set(); visited: set[str] = set()
    def visit(node: str) -> bool:
        if node in visiting: return True
        if node in visited: return False
        visiting.add(node)
        found = any(visit(child) for child in outgoing.get(node, set()))
        visiting.remove(node); visited.add(node)
        return found
    return any(visit(node) for node in nodes)


def _depth(roots: tuple[RecordKey, ...], outgoing: Mapping[str, set[str]]) -> int:
    def walk(node: str, seen: frozenset[str]) -> int:
        return 1 + max((walk(child, seen | {node}) for child in outgoing.get(node, set()) - seen), default=0)
    return max((walk(key[0], frozenset()) for key in roots), default=0)
