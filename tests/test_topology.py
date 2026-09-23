"""Synthetic unit tests for conservative OpenWebUI topology proof."""
from __future__ import annotations

import unittest

from open_chat_archive.topology import OPENWEBUI_NAMESPACE, project_openwebui_topology


def _key(letter: str, revision: str = "r") -> tuple[str, str]:
    return ("entity-" + letter, "revision-" + revision)


def _record(kind: str, logical: str, body: dict, extension: dict | None = None) -> dict:
    return {"record_type": kind, "identity": {"adapter_namespace": OPENWEBUI_NAMESPACE, "logical_key": logical},
            "body": body, "extensions": {OPENWEBUI_NAMESPACE: extension} if extension is not None else {}}


class TopologyProofTests(unittest.TestCase):
    def _forest(self, *, relation_kind: str = "parent_child", duplicate_revision: bool = False):
        session = _key("s")
        root, leaf, isolated = _key("root"), _key("leaf"), _key("solo")
        relation = _key("edge")
        records = {
            session: _record("session", "session:with:colon", {"thread_shape": "unknown", "active_message_id": leaf[0]}),
            root: _record("message", "session:with:colon:root:with:colon", {}, {"parentId": None, "childrenIds": ["leaf"]}),
            leaf: _record("message", "session:with:colon:leaf", {}, {"parentId": "root:with:colon", "childrenIds": []}),
            isolated: _record("message", "session:with:colon:solo", {}, {"parentId": None, "childrenIds": []}),
            relation: _record("relationship", "edge", {"from_id": root[0], "to_id": leaf[0], "kind": relation_kind, "sibling_index": 0}),
        }
        if duplicate_revision:
            records[(root[0], "revision-other")] = _record("message", "session:with:colon:root:with:colon", {}, {"parentId": None, "childrenIds": ["leaf"]})
        return records, session, (root, leaf, isolated), (relation,)

    def test_complete_forest_accepts_colons_and_isolated_nodes(self) -> None:
        records, session, messages, relations = self._forest()
        topology = project_openwebui_topology(records, session, messages, relations, messages[0][0])
        self.assertEqual(("complete_parent_child", "forest"), (topology.basis, topology.derived_shape))
        self.assertEqual(2, len(topology.components))
        self.assertEqual((messages[1],), topology.active_children)

    def test_non_parent_edge_cannot_create_a_forest(self) -> None:
        records, session, messages, relations = self._forest(relation_kind="reply_to")
        topology = project_openwebui_topology(records, session, messages, relations, None)
        self.assertEqual("incomplete_or_unknown", topology.basis)
        self.assertEqual("unknown", topology.derived_shape)
        self.assertEqual(0, len(topology.parent_child_keys))

    def test_message_revisions_are_conservatively_ambiguous(self) -> None:
        records, session, messages, relations = self._forest(duplicate_revision=True)
        topology = project_openwebui_topology(records, session, messages, relations, None)
        self.assertEqual("revision_ambiguous", topology.basis)
        self.assertIn("REVISION_AMBIGUOUS", topology.diagnostic_codes)

    def test_any_topology_participant_revision_hides_joined_graph_values_even_with_lineage(self) -> None:
        records, session, messages, relations = self._forest()
        lineage = _key("lineage")
        records[lineage] = _record("revision", "lineage", {"entity_id": session[0],
                                                               "from_revision_id": session[1],
                                                               "to_revision_id": "revision-other"})
        for selected in (session, messages[0], relations[0]):
            revised = (selected[0], "revision-other")
            records[revised] = dict(records[selected])
            topology = project_openwebui_topology(records, session, messages, relations, messages[0][0])
            self.assertEqual("revision_ambiguous", topology.basis)
            self.assertEqual((), topology.components)
            self.assertEqual((), topology.roots)
            self.assertEqual((), topology.leaves)
            self.assertEqual(0, topology.depth)
            self.assertEqual((), topology.active_children)
            self.assertEqual((), topology.parent_child_keys)
            del records[revised]

    def test_empty_projection_is_unknown_not_linear(self) -> None:
        records, session, _, _ = self._forest()
        topology = project_openwebui_topology(records, session, (), (), None)
        self.assertEqual(("incomplete_or_unknown", "unknown"), (topology.basis, topology.derived_shape))
        self.assertEqual((), topology.components)

    def test_cycle_is_visible_as_invalid_not_repaired(self) -> None:
        session, first, second, left, right = _key("s"), _key("a"), _key("b"), _key("ab"), _key("ba")
        records = {
            session: _record("session", "s", {}),
            first: _record("message", "s:a", {}, {"parentId": "b", "childrenIds": ["b"]}),
            second: _record("message", "s:b", {}, {"parentId": "a", "childrenIds": ["a"]}),
            left: _record("relationship", "a>b", {"from_id": first[0], "to_id": second[0], "kind": "parent_child"}),
            right: _record("relationship", "b>a", {"from_id": second[0], "to_id": first[0], "kind": "parent_child"}),
        }
        topology = project_openwebui_topology(records, session, (first, second), (left, right), None)
        self.assertEqual("invalid_shape", topology.basis)
        self.assertIn("PARENT_CHILD_CYCLE", topology.diagnostic_codes)

    def test_multi_parent_edge_is_visible_as_invalid_not_repaired(self) -> None:
        session, left, right, child, first_edge, second_edge = (_key(name) for name in ("s", "left", "right", "child", "left-child", "right-child"))
        records = {
            session: _record("session", "s", {}),
            left: _record("message", "s:left", {}, {"parentId": None, "childrenIds": ["child"]}),
            right: _record("message", "s:right", {}, {"parentId": None, "childrenIds": ["child"]}),
            child: _record("message", "s:child", {}, {"parentId": "left", "childrenIds": []}),
            first_edge: _record("relationship", "left>child", {"from_id": left[0], "to_id": child[0], "kind": "parent_child"}),
            second_edge: _record("relationship", "right>child", {"from_id": right[0], "to_id": child[0], "kind": "parent_child"}),
        }
        topology = project_openwebui_topology(records, session, (left, right, child), (first_edge, second_edge), None)
        self.assertEqual("invalid_shape", topology.basis)
        self.assertIn("MULTIPLE_PARENT_CHILD_PARENTS", topology.diagnostic_codes)


if __name__ == "__main__":
    unittest.main()
