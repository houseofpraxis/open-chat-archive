"""Synthetic topology regressions for OpenWebUI adapter 0.1.3."""
import json
import tempfile
import unittest
from pathlib import Path

from open_chat_archive.batch_writer import AdapterInputError
from open_chat_archive.source_adapters import OpenWebUIAdapter
from open_chat_archive.validation import validate_archive


def _node(node_id, *, parent=None, children=(), done=None):
    node = {
        "id": node_id,
        "role": "assistant",
        "content": "synthetic " + node_id,
        "parentId": parent,
        "childrenIds": list(children),
    }
    if done is not None:
        node["done"] = done
    return node


class OpenWebUITopologyTests(unittest.TestCase):
    def _conform(self, messages, current, *, version=None):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        source = Path(directory.name) / "synthetic-openwebui.json"
        source.write_text(json.dumps([{
            "id": "chat",
            "history": {"currentId": current, "messages": messages},
        }], separators=(",", ":")), encoding="utf-8")
        root = Path(directory.name) / "archive"
        adapter = OpenWebUIAdapter("scope")
        if version is not None:
            adapter.version = version
        result = adapter.conform(source, root, archive_id="archive", batch_id="batch")
        records = [json.loads(line) for line in (result.batch_dir / "records.ndjson").read_text(encoding="utf-8").splitlines()]
        validate_archive(root)
        return records

    @staticmethod
    def _semantic(records, record_types):
        return {
            (record["record_type"], record["identity"]["logical_key"]): (record["entity_id"], record["revision_id"])
            for record in records if record["record_type"] in record_types
        }

    @staticmethod
    def _session(records):
        return next(record for record in records if record["record_type"] == "session")

    def test_validated_graphs_classify_forest_isolated_singleton_linear_and_tree(self):
        cases = {
            "forest": (
                {"root-a": _node("root-a", children=("leaf-a",)), "leaf-a": _node("leaf-a", parent="root-a"),
                 "root-b": _node("root-b", children=("leaf-b",)), "leaf-b": _node("leaf-b", parent="root-b")},
                "leaf-a", "unknown", "forest",
            ),
            "isolated": (
                {"one": _node("one"), "two": _node("two")}, "one", "unknown", "forest",
            ),
            "singleton": ({"only": _node("only")}, "only", "linear", None),
            "linear": (
                {"one": _node("one", children=("two",)), "two": _node("two", parent="one", children=("three",)),
                 "three": _node("three", parent="two")},
                "three", "linear", None,
            ),
            "tree": (
                {"root": _node("root", children=("left", "right")), "left": _node("left", parent="root"),
                 "right": _node("right", parent="root")},
                "left", "tree", None,
            ),
        }
        for name, (messages, current, expected, extension) in cases.items():
            with self.subTest(name=name):
                session = self._session(self._conform(messages, current))
                self.assertEqual(session["body"]["thread_shape"], expected)
                metadata = session.get("extensions", {}).get(OpenWebUIAdapter.namespace, {})
                self.assertEqual(metadata.get("thread_shape"), extension)
                self.assertNotIn("forest", session["body"].values())

    def test_active_nonleaf_and_message_relationship_stability_across_version_bump(self):
        messages = {
            "root": _node("root", children=("active",)),
            "active": _node("active", parent="root", children=("child",), done=True),
            "child": _node("child", parent="active"),
        }
        old_records = self._conform(messages, "active", version="0.1.2")
        records = self._conform(messages, "active")
        session = self._session(records)
        messages_by_key = {record["identity"]["logical_key"]: record for record in records if record["record_type"] == "message"}
        lifecycle = next(record for record in records if record["record_type"] == "event" and record["body"]["kind"] == "lifecycle")
        self.assertEqual(OpenWebUIAdapter.version, "0.1.3")
        self.assertEqual(session["body"]["active_message_id"], messages_by_key["chat:active"]["entity_id"])
        self.assertEqual(lifecycle["identity"]["logical_key"], "chat:active-leaf")
        self.assertEqual(lifecycle["body"]["message_id"], messages_by_key["chat:active"]["entity_id"])
        self.assertEqual(lifecycle["body"]["payload"], {"status": "completed", "detail": "active_node"})
        self.assertEqual(
            self._semantic(old_records, {"message", "relationship"}),
            self._semantic(records, {"message", "relationship"}),
        )

    def test_map_and_child_order_do_not_change_topology_or_drop_edges(self):
        ordered = {
            "root": _node("root", children=("left", "right")),
            "left": _node("left", parent="root"),
            "right": _node("right", parent="root"),
            "isolated": _node("isolated"),
        }
        shuffled_map = {
            "right": _node("right", parent="root"),
            "isolated": _node("isolated"),
            "root": _node("root", children=("left", "right")),
            "left": _node("left", parent="root"),
        }
        first = self._conform(ordered, "root")
        second = self._conform(shuffled_map, "root")
        self.assertEqual(self._semantic(first, {"message", "relationship"}), self._semantic(second, {"message", "relationship"}))
        for records in (first, second):
            session = self._session(records)
            self.assertEqual(session["body"]["thread_shape"], "unknown")
            self.assertEqual(session["extensions"][OpenWebUIAdapter.namespace]["thread_shape"], "forest")

        child_shuffled = dict(ordered)
        child_shuffled["root"] = _node("root", children=("right", "left"))
        records = self._conform(child_shuffled, "root")
        relationships = {record["identity"]["logical_key"]: record["body"]["sibling_index"]
                         for record in records if record["record_type"] == "relationship"}
        self.assertEqual(relationships, {"chat:root>right": 0, "chat:root>left": 1})
        self.assertEqual(len([record for record in records if record["record_type"] == "message"]), 4)
        self.assertEqual(self._session(records)["body"]["thread_shape"], "unknown")

    def test_cycles_and_inconsistent_edges_remain_rejected(self):
        cases = {
            "cycle": {
                "one": _node("one", parent="two", children=("two",)),
                "two": _node("two", parent="one", children=("one",)),
            },
            "inconsistent": {
                "parent": _node("parent", children=()),
                "child": _node("child", parent="parent"),
            },
        }
        for name, messages in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / "synthetic-openwebui.json"
                source.write_text(json.dumps([{"id": "chat", "history": {"currentId": next(iter(messages)), "messages": messages}}]), encoding="utf-8")
                expected = "OPENWEBUI_CYCLE" if name == "cycle" else "OPENWEBUI_TREE_INCONSISTENT"
                with self.assertRaisesRegex(AdapterInputError, expected):
                    OpenWebUIAdapter("scope").conform(source, Path(directory) / "archive", archive_id="archive", batch_id="batch")
