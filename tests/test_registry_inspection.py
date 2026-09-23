import json
import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType

from open_chat_archive.inspect_site import MARKER, MARKER_TEXT, build_all, build_one, main
from open_chat_archive.inspection import Diagnostic, InspectionModel
from open_chat_archive.source_registry import SourceRegistry, load_source_registry
from open_chat_archive.source_summaries import source_summaries
from open_chat_archive.source_adapters.claude_ai import ClaudeAIAdapter


class RegistryInspectionTests(unittest.TestCase):
    namespace = "org.open-chat-archive.claude-ai"

    def _archive(self, root: Path, name: str, scope: str) -> Path:
        archive = root / "archives" / name
        source = root / (name + ".json")
        source.write_text(json.dumps([{"uuid": name + "-chat", "name": "Synthetic", "created_at": "2026-01-01T00:00:00Z",
                                        "chat_messages": [{"uuid": name + "-message", "sender": "human", "text": "x", "created_at": "2026-01-01T00:00:00Z"}]}]))
        ClaudeAIAdapter(scope).conform(source, archive, archive_id=name, batch_id="batch")
        return archive

    @staticmethod
    def _record(record_type: str, archive: str, namespace: str, scope: str, entity: str, revision: str,
                **body: object) -> tuple[tuple[str, str], dict[str, object]]:
        return ((entity, revision), {"record_type": record_type,
                                     "identity": {"archive_id": archive, "adapter_namespace": namespace,
                                                  "source_scope_id": scope}, "body": body})

    @staticmethod
    def _model(records: dict[tuple[str, str], dict[str, object]], diagnostics: tuple[Diagnostic, ...] = (),
               record_sessions: dict[tuple[str, str], tuple[str, ...]] | None = None) -> InspectionModel:
        associations = record_sessions or {key: () for key in records}
        return InspectionModel(Path("synthetic"), "synthetic", (), MappingProxyType(records),
                               MappingProxyType({key: () for key in records}), (),
                               MappingProxyType(associations), diagnostics, (), 0)

    @staticmethod
    def _registry(root: Path, sources: list[dict[str, str]], bindings: list[dict[str, str]]) -> SourceRegistry:
        path = root / "registry.json"
        path.write_text(json.dumps({"registry_version": "0.1-experimental", "sources": sources,
                                    "bindings": bindings}))
        return load_source_registry(path)

    def test_registry_metadata_is_escaped_and_unused_values_do_not_leak(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); parent = root / "archives"; archive = parent / "archive"
            source = root / "source.json"
            source.write_text(json.dumps([{"uuid": "chat", "name": "Synthetic", "created_at": "2026-01-01T00:00:00Z",
                                            "chat_messages": [{"uuid": "message", "sender": "human", "text": "x", "created_at": "2026-01-01T00:00:00Z"}]}]))
            ClaudeAIAdapter("scope").conform(source, archive, archive_id="archive", batch_id="batch")
            namespace = self.namespace
            registry = root / "registry.json"
            registry.write_text(json.dumps({"registry_version": "0.1-experimental",
                "sources": [{"source_id": "used", "adapter_namespace": namespace, "display_label": "<img src=x>", "category": "<script>canary</script>", "status": "unverified"},
                            {"source_id": "unused", "adapter_namespace": namespace, "display_label": "PRIVATE-CANARY", "status": "unverified"}],
                "bindings": [{"archive_id": "archive", "adapter_namespace": namespace, "source_scope_id": "scope", "source_id": "used"}]}))
            output = root / "site"
            self.assertTrue(build_all(parent, output, source_registry=registry))
            report = json.loads((output / "archive" / "inspection-report.json").read_text())
            self.assertEqual(1, len(report["source_summaries"]))
            self.assertEqual("unverified", report["source_summaries"][0]["registry_status"])
            self.assertEqual("used", report["source_summaries"][0]["source_id"])
            self.assertEqual("<script>canary</script>", report["source_summaries"][0]["category"])
            chat_page = next((output / "archive" / "chats").glob("*.html")).read_text()
            sources_page = (output / "archive" / "sources.html").read_text()
            self.assertIn("source ID used", chat_page)
            self.assertIn("source ID: used", sources_page)
            self.assertIn("category: &lt;script&gt;canary&lt;/script&gt;", sources_page)
            for path in output.rglob("*"):
                if path.is_file() and path.suffix in {".html", ".json"}:
                    text = path.read_text()
                    self.assertNotIn("PRIVATE-CANARY", text)
                    if path.suffix == ".html":
                        self.assertNotIn("<img src=x>", text)
                        self.assertNotIn("<script>canary</script>", text)

    def test_source_rows_do_not_merge_aliases_or_same_personal_token(self) -> None:
        namespace_a = self.namespace; namespace_b = "org.example.other"
        records = dict([
            self._record("session", "archive-a", namespace_a, "scope-a", "same", "r1", created_at="2026-01-01T00:00:00Z"),
            self._record("session", "archive-a", namespace_a, "scope-a", "same", "r2", updated_at="2026-01-01T00:00:00+00:00"),
            self._record("session", "archive-a", namespace_b, "scope-b", "other", "r1"),
            self._record("session", "archive-b", namespace_a, "scope-c", "third", "r1"),
            self._record("session", "archive-b", namespace_b, "scope-d", "fourth", "r1"),
        ])
        with tempfile.TemporaryDirectory() as temporary:
            registry = self._registry(Path(temporary), [
                {"source_id": "alias", "adapter_namespace": namespace_a, "display_label": "A",
                 "account_identifier": "personal-token", "status": "unverified"},
                {"source_id": "other-a", "adapter_namespace": namespace_b, "display_label": "B",
                 "account_identifier": "personal-token", "status": "unverified"},
                {"source_id": "other-b", "adapter_namespace": namespace_b, "display_label": "D",
                 "account_identifier": "personal-token", "status": "unverified"},
            ], [
                {"archive_id": "archive-a", "adapter_namespace": namespace_a, "source_scope_id": "scope-a", "source_id": "alias"},
                {"archive_id": "archive-b", "adapter_namespace": namespace_a, "source_scope_id": "scope-c", "source_id": "alias"},
                {"archive_id": "archive-a", "adapter_namespace": namespace_b, "source_scope_id": "scope-b", "source_id": "other-a"},
                {"archive_id": "archive-b", "adapter_namespace": namespace_b, "source_scope_id": "scope-d", "source_id": "other-b"},
            ])
            rows = source_summaries(self._model(records), registry)
        self.assertEqual(4, len(rows))
        alias_a = next(row for row in rows if row["source_scope_id"] == "scope-a")
        self.assertEqual(1, alias_a["chat_count"])
        self.assertEqual(2, alias_a["session_revision_count"])
        self.assertEqual(2, sum(row["source_id"] == "alias" for row in rows))
        self.assertEqual({"archive-a", "archive-b"}, {row["archive_id"] for row in rows})
        self.assertEqual({namespace_a, namespace_b}, {row["adapter_namespace"] for row in rows})

    def test_source_summary_date_ties_unknown_dates_and_shared_diagnostics(self) -> None:
        namespace = self.namespace
        records = dict([
            self._record("session", "archive", namespace, "dated", "chat", "r1", created_at="2026-01-01T00:00:00Z"),
            self._record("session", "archive", namespace, "dated", "chat", "r2", updated_at="2026-01-01T00:00:00+00:00"),
            self._record("message", "archive", namespace, "dated", "message", "r1", created_at="2026-01-01T01:00:00+01:00"),
            self._record("session", "archive", namespace, "unknown", "other", "r1"),
        ])
        model = self._model(
            records, (Diagnostic("SHARED", "warning", "archive", "exploratory", count=3, associated_session_ids=("chat", "other")),
                      Diagnostic("ONE", "warning", "archive", "exploratory", count=2, associated_session_ids=("chat",))),
            {key: ("chat",) if key == ("message", "r1") else () for key in records},
        )
        rows = {row["source_scope_id"]: row for row in source_summaries(model)}
        dated = rows["dated"]; unknown = rows["unknown"]
        self.assertEqual(1, dated["chat_count"])
        self.assertEqual(2, dated["session_revision_count"])
        self.assertEqual("2026-01-01T00:00:00+00:00", dated["date_range"]["earliest"])
        self.assertEqual("2026-01-01T01:00:00+01:00", dated["date_range"]["latest"])
        self.assertEqual({"ONE": 2, "SHARED": 3}, dated["diagnostic_counts"])
        self.assertEqual(3, dated["shared_diagnostic_total"])
        self.assertEqual(1, dated["affected_chat_count"])
        self.assertEqual(0, unknown["with_date_count"])
        self.assertEqual(1, unknown["without_date_count"])
        self.assertEqual({"SHARED": 3}, unknown["diagnostic_counts"])

    def test_source_summary_dates_are_isolated_to_the_full_scope_triple(self) -> None:
        namespace = self.namespace
        records = dict([
            self._record("session", "archive", namespace, "scope-a", "dated", "r1",
                         created_at="2026-01-01T00:00:00Z"),
            self._record("session", "archive", namespace, "scope-a", "undated", "r1"),
            self._record("message", "archive", namespace, "scope-b", "foreign", "r1",
                         created_at="2030-01-01T00:00:00Z"),
        ])
        model = self._model(
            records, record_sessions={key: ("undated",) if key == ("foreign", "r1") else () for key in records},
        )

        row = next(row for row in source_summaries(model) if row["source_scope_id"] == "scope-a")
        self.assertEqual({"earliest": "2026-01-01T00:00:00Z", "latest": "2026-01-01T00:00:00Z",
                          "basis": "canonical session created_at/updated_at and message created_at"},
                         row["date_range"])
        self.assertEqual(1, row["with_date_count"])
        self.assertEqual(1, row["without_date_count"])

    def test_absent_and_partial_registry_are_unregistered_without_guessing(self) -> None:
        records = dict([
            self._record("session", "archive", self.namespace, "bound", "one", "r1"),
            self._record("session", "archive", self.namespace, "missing", "two", "r1"),
        ])
        model = self._model(records)
        absent = source_summaries(model)
        self.assertEqual(["unregistered", "unregistered"], [row["registry_status"] for row in absent])
        with tempfile.TemporaryDirectory() as temporary:
            registry = self._registry(Path(temporary), [
                {"source_id": "bound-id", "adapter_namespace": self.namespace, "display_label": "literal",
                 "status": "ambiguous"},
            ], [{"archive_id": "archive", "adapter_namespace": self.namespace,
                 "source_scope_id": "bound", "source_id": "bound-id"}])
            partial = source_summaries(model, registry)
        self.assertEqual("ambiguous", partial[0]["registry_status"])
        self.assertEqual("unregistered", partial[1]["registry_status"])

    def test_registry_preflight_preserves_existing_output_for_direct_and_cli_calls(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); archive = self._archive(root, "archive", "scope")
            malformed = root / "malformed.json"; malformed.write_text("{}")
            for operation in (lambda output: build_one(archive, output, source_registry=malformed),
                              lambda output: build_all(root / "archives", output, source_registry=malformed)):
                output = root / ("site-" + str(id(operation))); output.mkdir()
                (output / MARKER).write_text(MARKER_TEXT); sentinel = output / "sentinel"; sentinel.write_text("keep")
                with self.assertRaises(ValueError): operation(output)
                self.assertEqual("keep", sentinel.read_text())
            for operation in (lambda output, registry: build_one(archive, output, source_registry=registry),
                              lambda output, registry: build_all(root / "archives", output, source_registry=registry)):
                output = root / ("overlap-" + str(id(operation))); output.mkdir()
                (output / MARKER).write_text(MARKER_TEXT); sentinel = output / "sentinel"; sentinel.write_text("keep")
                overlap = output / "registry.json"; overlap.write_text("{}")
                with self.assertRaisesRegex(ValueError, "REFUSE_REGISTRY_OUTPUT_OVERLAP"):
                    operation(output, overlap)
                self.assertEqual("keep", sentinel.read_text())
            output = root / "site-cli"; output.mkdir(); (output / MARKER).write_text(MARKER_TEXT)
            sentinel = output / "sentinel"; sentinel.write_text("keep")
            self.assertEqual(2, main(["build", str(archive), "--output", str(output), "--source-registry", str(malformed)]))
            self.assertEqual("keep", sentinel.read_text())
            overlap = output / "registry.json"; overlap.write_text("{}")
            self.assertEqual(2, main(["build-all", str(root / "archives"), "--output", str(output), "--source-registry", str(overlap)]))
            self.assertEqual("keep", sentinel.read_text())

    def test_build_all_keeps_other_archive_registry_metadata_out_of_each_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); self._archive(root, "first", "first-scope"); self._archive(root, "second", "second-scope")
            registry = root / "registry.json"
            registry.write_text(json.dumps({"registry_version": "0.1-experimental", "sources": [
                {"source_id": "first", "adapter_namespace": self.namespace, "display_label": "FIRST-CANARY", "status": "unverified"},
                {"source_id": "second", "adapter_namespace": self.namespace, "display_label": "SECOND-CANARY", "status": "unverified"},
                {"source_id": "unused", "adapter_namespace": self.namespace, "display_label": "UNUSED-CANARY", "status": "unverified"}], "bindings": [
                {"archive_id": "first", "adapter_namespace": self.namespace, "source_scope_id": "first-scope", "source_id": "first"},
                {"archive_id": "second", "adapter_namespace": self.namespace, "source_scope_id": "second-scope", "source_id": "second"}]}))
            output = root / "site"; self.assertTrue(build_all(root / "archives", output, source_registry=registry))
            first = "\n".join(path.read_text() for path in (output / "first").rglob("*") if path.is_file())
            second = "\n".join(path.read_text() for path in (output / "second").rglob("*") if path.is_file())
            self.assertIn("FIRST-CANARY", first); self.assertNotIn("SECOND-CANARY", first); self.assertNotIn("UNUSED-CANARY", first)
            self.assertIn("SECOND-CANARY", second); self.assertNotIn("FIRST-CANARY", second); self.assertNotIn("UNUSED-CANARY", second)

    def test_registry_rename_changes_only_rendered_operator_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); archive = self._archive(root, "archive", "scope")
            before = {path.relative_to(archive): path.read_bytes() for path in archive.rglob("*") if path.is_file()}
            def write_registry(path: Path, label: str) -> None:
                path.write_text(json.dumps({"registry_version": "0.1-experimental", "sources": [
                    {"source_id": "stable-id", "adapter_namespace": self.namespace, "display_label": label, "status": "unverified"}],
                    "bindings": [{"archive_id": "archive", "adapter_namespace": self.namespace,
                                  "source_scope_id": "scope", "source_id": "stable-id"}]}))
            first = root / "first.json"; second = root / "second.json"
            write_registry(first, "before rename"); write_registry(second, "after rename")
            self.assertTrue(build_one(archive, root / "site-one", source_registry=first))
            self.assertTrue(build_one(archive, root / "site-two", source_registry=second))
            after = {path.relative_to(archive): path.read_bytes() for path in archive.rglob("*") if path.is_file()}
            self.assertEqual(before, after)
            one = json.loads((root / "site-one" / "inspection-report.json").read_text())["source_summaries"][0]
            two = json.loads((root / "site-two" / "inspection-report.json").read_text())["source_summaries"][0]
            self.assertEqual("stable-id", one["source_id"])
            self.assertEqual("stable-id", two["source_id"])
            self.assertNotEqual(one["display_label"], two["display_label"])
