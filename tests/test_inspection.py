"""Synthetic-only tests for the experimental inspection model."""
from __future__ import annotations

import tempfile
import unittest
import json
from pathlib import Path

from open_chat_archive.inspection import (
    Diagnostic,
    InspectionModel,
    SessionProjection,
    _associate_records,
    _collapse_diagnostics,
    _project_sessions,
    load_archive,
)
from open_chat_archive.topology import OPENWEBUI_NAMESPACE
from open_chat_archive.source_adapters.open_webui import OpenWebUIAdapter
from open_chat_archive.synthetic import create_synthetic_batch


class InspectionModelTests(unittest.TestCase):
    @staticmethod
    def _key(name: str) -> tuple[str, str]:
        return ("entity-" + name, "revision-" + name)

    @staticmethod
    def _record(kind: str, logical: str, body: dict, *, namespace: str = OPENWEBUI_NAMESPACE,
                extension: dict | None = None) -> dict:
        return {"record_type": kind, "identity": {"adapter_namespace": namespace, "logical_key": logical},
                "body": body, "extensions": {OPENWEBUI_NAMESPACE: extension} if extension is not None else {}}

    def test_duplicate_batch_occurrences_are_retained_without_duplicate_chats(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "archives" / "synthetic-alpha"
            create_synthetic_batch(archive, archive_id="synthetic-alpha", batch_id="batch-0001")
            create_synthetic_batch(archive, archive_id="synthetic-alpha", batch_id="batch-0002")
            model = load_archive(archive, schema_root=Path.cwd())
            report = model.report()
            self.assertEqual(1, report["session_count"])
            self.assertGreater(report["record_accounting"]["duplicate_occurrences"], 0)
            self.assertEqual(report["record_accounting"]["input_records"],
                             sum(len(value) for value in model.occurrences.values()))
            self.assertEqual(len(model.records), report["projection_accounting"]["rendered_records"])
            key = next(iter(model.records))
            with self.assertRaises(TypeError):
                model.records[key]["unexpected"] = "mutation"  # type: ignore[index]

    def test_report_has_separate_non_normative_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "archives" / "synthetic-beta"
            create_synthetic_batch(archive, archive_id="synthetic-beta", batch_id="batch-0001", rich=True)
            report = load_archive(archive, schema_root=Path.cwd()).report()
            self.assertEqual("passed", report["validation"]["status"])
            self.assertIn("exploratory_findings", report["diagnostic_classes"])
            self.assertIn("MISSING_CANONICAL_TIMESTAMP", {item["code"] for item in report["diagnostics"]})
            self.assertEqual(report["record_accounting"]["input_records"],
                             sum(report["record_occurrence_counts_by_type"].values()))
            summaries = report["adapter_summaries"]
            self.assertEqual(set(summaries), {"org.open-chat-archive.synthetic"})
            self.assertEqual(summaries["org.open-chat-archive.synthetic"]["session_count"], 1)

    def test_openwebui_proved_forest_has_safe_topology_and_counts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); archive = root / "synthetic"; source = root / "source.json"
            def node(identifier: str, parent: str | None, children: list[str]) -> dict[str, object]:
                return {"id": identifier, "parentId": parent, "childrenIds": children,
                        "role": "user", "content": "synthetic", "timestamp": 1760000000}
            source.write_text(json.dumps([{"id": "chat:colon", "history": {"currentId": "root:a", "messages": {
                "root:a": node("root:a", None, ["leaf:a"]), "leaf:a": node("leaf:a", "root:a", []),
                "isolated": node("isolated", None, [])}}}]), encoding="utf-8")
            OpenWebUIAdapter("synthetic-scope").conform(source, archive, archive_id="synthetic", batch_id="one")
            report = load_archive(archive, schema_root=Path.cwd()).report()
            topology = report["session_topologies"][0]
            self.assertEqual(("complete_parent_child", "forest"), (topology["basis"], topology["derived_shape"]))
            self.assertEqual(2, len(topology["components"]))
            self.assertEqual(1, sum(component["contains_active"] for component in topology["components"]))
            self.assertEqual(1, len(topology["active_children"]))
            summary = report["adapter_summaries"][OpenWebUIAdapter.namespace]
            self.assertEqual(1, summary["affected_chat_count"])
            self.assertEqual(1, summary["diagnostic_counts"]["MULTIPLE_SESSION_ROOTS"])
            self.assertEqual(1, summary["diagnostic_counts"]["DISCONNECTED_SESSION_GRAPH"])
            self.assertEqual(2, sum(summary["diagnostic_counts"][code] for code in
                                    ("MULTIPLE_SESSION_ROOTS", "DISCONNECTED_SESSION_GRAPH")))
            self.assertGreaterEqual(summary["diagnostic_total"], 3)

    def test_unknown_graph_without_edges_does_not_infer_disconnected_claude_chat(self) -> None:
        session, first, second = (self._key(name) for name in ("session", "first", "second"))
        records = {
            session: self._record("session", "session", {}, namespace="org.example.claude"),
            first: self._record("message", "session:first", {"session_id": session[0], "content": []},
                                namespace="org.example.claude"),
            second: self._record("message", "session:second", {"session_id": session[0], "content": []},
                                 namespace="org.example.claude"),
        }
        associations, _ = _associate_records(records)
        _, diagnostics = _project_sessions(records, associations)
        codes = {item.code for item in diagnostics}
        self.assertNotIn("MULTIPLE_SESSION_ROOTS", codes)
        self.assertNotIn("DISCONNECTED_SESSION_GRAPH", codes)

    def test_collapsed_diagnostics_keep_exact_session_attribution_and_unassigned_occurrences(self) -> None:
        first, second = self._key("first"), self._key("second")
        records = {
            first: self._record("session", "first", {}, namespace=OPENWEBUI_NAMESPACE),
            second: self._record("session", "second", {}, namespace="org.example.other"),
        }
        collapsed = _collapse_diagnostics([
            Diagnostic("COARSE_EVIDENCE_LOCATION", "info", "message", "exploratory", (), 2, (first[0],)),
            Diagnostic("COARSE_EVIDENCE_LOCATION", "info", "message", "exploratory", (), 1, (first[0],)),
            Diagnostic("COARSE_EVIDENCE_LOCATION", "info", "message", "exploratory", (), 3, (second[0],)),
            Diagnostic("COARSE_EVIDENCE_LOCATION", "info", "archive", "exploratory", (), 5, ()),
        ])
        by_sessions = {item.associated_session_ids: item.count for item in collapsed}
        self.assertEqual({(first[0],): 3, (second[0],): 3, (): 5}, by_sessions)
        session = lambda key: SessionProjection(key, "chat.html", (), (), (), (), (), (), (), 0, None)
        model = InspectionModel(Path("synthetic"), "synthetic", (), records, {}, (session(first), session(second)), {},
                                tuple(collapsed), (OPENWEBUI_NAMESPACE, "org.example.other"), 0)
        summaries = model._adapter_summaries()
        self.assertEqual(3, summaries[OPENWEBUI_NAMESPACE]["diagnostic_total"])
        self.assertEqual(3, summaries["org.example.other"]["diagnostic_total"])
        self.assertEqual(1, summaries[OPENWEBUI_NAMESPACE]["affected_chat_count"])
        self.assertEqual({"COARSE_EVIDENCE_LOCATION": 5}, model.report()["unassigned_archive_diagnostic_counts"])

    def test_cross_session_parent_edge_reaches_both_topology_proofs_without_session_association(self) -> None:
        first, first_message = self._key("first"), self._key("first-message")
        second, second_message = self._key("second"), self._key("second-message")
        cross = self._key("cross")
        records = {
            first: self._record("session", "first", {}),
            first_message: self._record("message", "first:a", {"session_id": first[0], "content": []},
                                        extension={"parentId": None, "childrenIds": []}),
            second: self._record("session", "second", {}),
            second_message: self._record("message", "second:b", {"session_id": second[0], "content": []},
                                         extension={"parentId": None, "childrenIds": []}),
            cross: self._record("relationship", "cross", {"from_id": first_message[0], "to_id": second_message[0],
                                                              "kind": "parent_child"}),
        }
        associations, _ = _associate_records(records)
        self.assertFalse(associations.get(cross))
        sessions, diagnostics = _project_sessions(records, associations)
        self.assertEqual(2, len(sessions))
        self.assertTrue(all(session.topology and session.topology.basis == "invalid_shape" for session in sessions))
        self.assertTrue(all(cross in session.topology.parent_child_keys for session in sessions if session.topology))
        self.assertEqual(2, sum(item.code == "INVALID_PARENT_CHILD_ENDPOINT" for item in diagnostics))

    def test_unknown_graph_with_edges_retains_observed_root_and_disconnect_warnings(self) -> None:
        session, root, child, isolated, edge = (self._key(name) for name in ("session", "root", "child", "isolated", "edge"))
        records = {
            session: self._record("session", "session", {}),
            root: self._record("message", "session:root", {"session_id": session[0], "content": []}),
            child: self._record("message", "session:child", {"session_id": session[0], "content": []}),
            isolated: self._record("message", "session:isolated", {"session_id": session[0], "content": []}),
            edge: self._record("relationship", "edge", {"from_id": root[0], "to_id": child[0], "kind": "parent_child"}),
        }
        associations, _ = _associate_records(records)
        sessions, diagnostics = _project_sessions(records, associations)
        topology = sessions[0].topology
        self.assertIsNotNone(topology)
        self.assertEqual(("incomplete_or_unknown", "unknown"), (topology.basis, topology.derived_shape))
        warning_codes = {item.code for item in diagnostics if item.severity == "warning"}
        self.assertTrue({"MULTIPLE_SESSION_ROOTS", "DISCONNECTED_SESSION_GRAPH"} <= warning_codes)


if __name__ == "__main__": unittest.main()
