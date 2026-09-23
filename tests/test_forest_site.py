"""Parsed, synthetic no-JS regressions for forest inspector pages."""
from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from types import SimpleNamespace
import unittest

from open_chat_archive.batch_writer import BatchWriter
from open_chat_archive.canonical import entity_identity
from open_chat_archive.inspect_site import _chat_page, _message_anchor, _page, build_one
from open_chat_archive.inspection import SessionProjection
from open_chat_archive.topology import OPENWEBUI_NAMESPACE, TopologyProjection


class _ParsedHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.hrefs: list[str] = []
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if values.get("id"):
            self.ids.add(values["id"] or "")
        if tag == "a" and values.get("href"):
            self.hrefs.append(values["href"] or "")

    def handle_data(self, data: str) -> None:
        self.text.append(data)

    @property
    def visible_text(self) -> str:
        return " ".join(" ".join(self.text).split())


class ForestSiteTests(unittest.TestCase):
    def _forest_archive(self, root: Path) -> None:
        """Build a valid, wholly synthetic 27-component OpenWebUI-shaped archive."""
        writer = BatchWriter(namespace=OPENWEBUI_NAMESPACE, version="0.0.0", source_scope_id="scope",
                             archive_id="archive", batch_id="batch", archive_root=root,
                             raw=b'[{"synthetic":true}]', media_type="application/json", fingerprint="synthetic",
                             parsed=[{"synthetic": True}])
        session_id = entity_identity(writer.identity("session", "chat"))
        messages = []
        for index in range(27):
            node = "node-" + str(index)
            messages.append(writer.record(
                "message", "chat:" + node,
                {"session_id": session_id, "source_ordinal": index,
                 "role": "assistant", "visibility": "user_visible", "content": [{"type": "text", "text": node}]},
                extensions={OPENWEBUI_NAMESPACE: {"parentId": None, "childrenIds": []}},
            ))
        session = writer.record(
            "session", "chat",
            {"archive_id": "archive", "title": "Synthetic forest", "thread_shape": "unknown",
             "active_message_id": messages[13]["entity_id"]},
            extensions={OPENWEBUI_NAMESPACE: {"thread_shape": "forest"}},
        )
        other_session_id = entity_identity(writer.identity("session", "other"))
        other_message = writer.record(
            "message", "other:node",
            {"session_id": other_session_id, "source_ordinal": 0, "role": "assistant",
             "visibility": "user_visible", "content": [{"type": "text", "text": "other"}]},
            extensions={OPENWEBUI_NAMESPACE: {"parentId": None, "childrenIds": []}},
        )
        other_session = writer.record(
            "session", "other",
            {"archive_id": "archive", "title": "Other", "thread_shape": "unknown",
             "active_message_id": other_message["entity_id"]},
            extensions={OPENWEBUI_NAMESPACE: {}},
        )
        associated_literal = writer.record(
            "relationship", "chat:literal",
            {"from_id": messages[0]["entity_id"], "to_id": messages[1]["entity_id"], "kind": "correlates"},
            extensions={"org.example.relationship": {"extension-marker": "associated-extension"}},
        )
        unassociated_literal = writer.record(
            "relationship", "unassociated-link",
            {"from_id": messages[0]["entity_id"], "to_id": other_message["entity_id"],
             "kind": "correlates", "sibling_index": 99},
            extensions={"org.example.relationship": {"extension-marker": "unassociated-extension"}},
        )
        writer.write([*messages, session, other_message, other_session, associated_literal, unassociated_literal])

    def test_confirmed_forest_is_fully_navigable_and_all_literal_records_are_visible(self) -> None:
        with TemporaryDirectory() as temporary:
            archive = Path(temporary) / "archive"
            self._forest_archive(archive)
            output = Path(temporary) / "site"
            self.assertTrue(build_one(archive, output, schema_root=Path.cwd()))
            forest_page = next(page for page in (output / "chats").glob("*.html")
                               if "Thread A" in page.read_text(encoding="utf-8"))
            chat = _ParsedHTML(); chat.feed(forest_page.read_text(encoding="utf-8"))
            records_html = (output / "records.html").read_text(encoding="utf-8")
            records = _ParsedHTML(); records.feed(records_html)
            text = chat.visible_text
            self.assertIn("Thread A — 1 message. Root(s):", text)
            self.assertIn("Thread AA — 1 message. Root(s):", text)
            self.assertEqual(1, text.count("Contains active node."))
            self.assertIn("associated-extension", text)
            fragment_targets = [href[1:] for href in chat.hrefs if href.startswith("#message-")]
            self.assertEqual(27, len(set(fragment_targets)))
            self.assertTrue(set(fragment_targets) <= chat.ids)
            self.assertIn("Complete canonical record details", records_html)
            self.assertIn("unassociated-link", records_html)
            self.assertIn("sibling_index", records_html)
            self.assertIn("unassociated-extension", records_html)
            self.assertIn("bindings", records.visible_text)

    def test_revision_ambiguity_stops_graph_without_dropping_revision_cards(self) -> None:
        session_key = ("session", "revision-1")
        first = ("message-a", "revision-1")
        second = ("message-a", "revision-2")
        other = ("message-b", "revision-1")
        topology = TopologyProjection(None, "unknown", "revision_ambiguous", ((first, other),),
                                     (first,), (other,), 2, first[0], (other,), (), ("REVISION_AMBIGUOUS",))
        session = SessionProjection(session_key, "chat.html", (session_key, first, second, other),
                                    (first, second, other), (), (first, second, other), topology.components,
                                    topology.roots, topology.leaves, topology.depth, topology.active_node, topology, ())
        records: dict[tuple[str, str], dict[str, Any]] = {
            session_key: {"record_type": "session", "body": {"title": "Ambiguous"}, "identity": {}, "extensions": {}}
        }
        for key in (first, second, other):
            records[key] = {"record_type": "message", "body": {"role": "assistant", "visibility": "user_visible",
                            "source_ordinal": 0, "content": []}, "identity": {}, "extensions": {}}
        model = SimpleNamespace(records=records, occurrences={key: () for key in records})
        page = _page("synthetic", _chat_page(model, session), asset_prefix="assets/")  # type: ignore[arg-type]
        parsed = _ParsedHTML(); parsed.feed(page)
        self.assertIn("Graph rendering stopped", parsed.visible_text)
        self.assertIn("no arbitrary revision lookup was built", parsed.visible_text)
        self.assertNotIn('class="graph"', page)
        self.assertEqual({_message_anchor(first), _message_anchor(second), _message_anchor(other)} & parsed.ids,
                         {_message_anchor(first), _message_anchor(second), _message_anchor(other)})

    def test_incomplete_sibling_indexes_state_the_display_only_fallback(self) -> None:
        session_key, parent, child = ("session", "r"), ("parent", "r"), ("child", "r")
        relationship = ("relationship", "r")
        topology = TopologyProjection(None, "tree", "complete_parent_child", ((parent, child),),
                                     (parent,), (child,), 2, parent[0], (child,), (relationship,))
        session = SessionProjection(session_key, "chat.html", (session_key, parent, child, relationship),
                                    (parent, child), (relationship,), (parent, child), topology.components,
                                    topology.roots, topology.leaves, topology.depth, topology.active_node, topology,
                                    (relationship,))
        records = {
            session_key: {"record_type": "session", "body": {"title": "Fallback"}, "identity": {}, "extensions": {}},
            parent: {"record_type": "message", "body": {"role": "user", "visibility": "user_visible", "source_ordinal": 0, "content": []}, "identity": {}, "extensions": {}},
            child: {"record_type": "message", "body": {"role": "assistant", "visibility": "user_visible", "source_ordinal": 1, "content": []}, "identity": {}, "extensions": {}},
            relationship: {"record_type": "relationship", "body": {"from_id": parent[0], "to_id": child[0], "kind": "parent_child"}, "identity": {}, "extensions": {}},
        }
        model = SimpleNamespace(records=records, occurrences={key: () for key in records}, record_sessions={key: () for key in records})
        page = _page("synthetic", _chat_page(model, session), asset_prefix="assets/")  # type: ignore[arg-type]
        parsed = _ParsedHTML(); parsed.feed(page)
        self.assertIn("Child ordering fallback (display-only)", parsed.visible_text)


if __name__ == "__main__":
    unittest.main()
