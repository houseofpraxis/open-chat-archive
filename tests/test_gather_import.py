"""Synthetic gather import tests. No real personal data or origin I/O."""
from __future__ import annotations

import contextlib
import fcntl
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from open_chat_archive.gather_import import (
    ARCHIVES_MARKER_NAME,
    ARCHIVES_MARKER_TEXT,
    ARGUMENT_INVALID,
    IMPORTER_UNAVAILABLE,
    IMPORT_CONFLICT,
    PLAN_EXISTS,
    SCOPE_PLAN_CONFLICT,
    SOURCE_STORE_OVERLAP,
    STORE_BUSY,
    main,
    parse_plan,
)
from open_chat_archive.gather_store import archive_id_for, canonical_object_bytes
from open_chat_archive.pipeline import PIPELINE_RENDER_UNAVAILABLE, main as pipeline_main
from open_chat_archive.validation import ValidationError, validate_archive

PYTHON = os.environ.get("PYTHON", sys.executable)
SRC = str(Path(__file__).resolve().parents[1] / "src")

try:
    from test_gather_store import claude_payload, codex_payload, grok_payload, make_store, pi_payload
except ImportError:
    from tests.test_gather_store import claude_payload, codex_payload, grok_payload, make_store, pi_payload


def _run(argv: list[str]) -> tuple[int, dict]:
    buf = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
        code = main(argv)
    text = buf.getvalue()
    body = json.loads(text)
    return code, body, text, err.getvalue()


def _assign_plan(path: str, mapping: dict[str, str]) -> None:
    data = Path(path).read_bytes()
    plan = parse_plan(data)
    for row in plan["rows"]:
        assigned = mapping.get(row["receipt_id"])
        if assigned is not None:
            row["assigned_source_scope_id"] = assigned
    Path(path).write_bytes(canonical_object_bytes(plan))
    os.chmod(path, 0o600)


class GatherImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = self.temp.name
        os.chmod(self.base, 0o700)

    def _layout(self, items: list[dict]) -> tuple[str, str, str]:
        store = os.path.join(self.base, "store")
        make_store(store, items)
        plan = os.path.join(self.base, "plan.json")
        archives = os.path.join(self.base, "archives")
        os.mkdir(archives, 0o700)
        os.chmod(archives, 0o700)
        return store, plan, archives

    def test_claude_codex_grok_import_deterministic_ids_and_rerun(self) -> None:
        store, plan, archives = self._layout([
            {"adapter": "claude-code", "origin": "/synthetic/claude-a.jsonl",
             "scope": "synth-scope-a", "data": claude_payload()},
            {"adapter": "claude-code", "origin": "/synthetic/claude-b.jsonl",
             "scope": "synth-scope-a", "data": claude_payload()},
            {"adapter": "codex", "origin": "/synthetic/codex.jsonl",
             "scope": "synth-scope-b", "data": codex_payload()},
            {"adapter": "grok-build", "origin": "/synthetic/grok.jsonl",
             "scope": "synth-scope-c", "data": grok_payload()},
        ])
        code, body, text, err = _run(["plan", "--store", store, "--plan", plan])
        self.assertEqual(code, 0)
        self.assertEqual(body["operation"], "plan")
        self.assertNotIn("/synthetic/", text)
        self.assertNotIn("origin_path", text)
        self.assertNotIn("sha256:", text)
        self.assertNotIn("/synthetic/", err)
        self.assertNotIn("No such file", err)
        self.assertEqual(stat.S_IMODE(os.stat(plan).st_mode), 0o600)
        code, body, text, err = _run([
            "apply", "--store", store, "--plan", plan, "--archives-parent", archives,
        ])
        self.assertEqual(code, 0, text)
        self.assertNotEqual(body["code"], STORE_BUSY)
        self.assertTrue(body["complete"])
        self.assertEqual(body["counts"]["added"], 4)
        self.assertEqual(body["counts"]["unchanged"], 0)
        self.assertNotIn("/synthetic/", text)
        self.assertNotIn("No such file", text)
        claude_id = archive_id_for("claude-code", "synth-scope-a")
        other_a = archive_id_for("claude-code", "synth-scope-b")
        self.assertNotEqual(claude_id, other_a)
        claude_root = Path(archives) / claude_id
        self.assertTrue(claude_root.is_dir())
        batches = sorted(p.name for p in (claude_root / "batches").iterdir())
        self.assertEqual(len(batches), 2)
        validate_archive(claude_root)
        validate_archive(Path(archives) / archive_id_for("codex", "synth-scope-b"))
        validate_archive(Path(archives) / archive_id_for("grok-build", "synth-scope-c"))
        ledger = Path(archives) / ".oca-bridge-import-ledger.json"
        self.assertTrue(ledger.is_file())
        ledger_body = json.loads(ledger.read_bytes())
        ledger_ids = [item["receipt_id"] for item in ledger_body["entries"]]
        self.assertEqual(len(ledger_ids), 4)
        self.assertEqual(len(ledger_ids), len(set(ledger_ids)))
        code, body, text, _err = _run([
            "apply", "--store", store, "--plan", plan, "--archives-parent", archives,
        ])
        self.assertEqual(code, 0, text)
        self.assertNotEqual(body["code"], STORE_BUSY)
        self.assertEqual(body["counts"]["unchanged"], 4)
        self.assertEqual(body["counts"]["added"], 0)
        rerun_ids = [item["receipt_id"] for item in json.loads(ledger.read_bytes())["entries"]]
        self.assertEqual(rerun_ids, ledger_ids)
        batches_after = sorted(p.name for p in (claude_root / "batches").iterdir())
        self.assertEqual(batches, batches_after)

    def test_pi_never_calls_adapter_and_missing_scope_blocks(self) -> None:
        store, plan, archives = self._layout([
            {"adapter": "pi", "origin": "/synthetic/pi.jsonl", "scope": "", "data": pi_payload()},
            {"adapter": "claude-code", "origin": "/synthetic/claude.jsonl",
             "scope": "", "data": claude_payload()},
        ])
        code, body, text, _err = _run(["plan", "--store", store, "--plan", plan])
        self.assertEqual(code, 0)
        with mock.patch("open_chat_archive.gather_import.create_adapter") as mocked:
            code, body, text, _err = _run([
                "apply", "--store", store, "--plan", plan, "--archives-parent", archives,
            ])
            mocked.assert_not_called()
        self.assertNotEqual(code, 0)
        self.assertFalse(body["complete"])
        self.assertGreaterEqual(body["counts"]["importer_unavailable"], 1)
        self.assertGreaterEqual(body["counts"]["scope_missing"], 1)
        self.assertEqual(list(Path(archives).glob("*/sources/*")), [])
        self.assertNotIn("/synthetic/", text)
        parsed = parse_plan(Path(plan).read_bytes())
        mapping = {}
        for row in parsed["rows"]:
            if row["adapter"] == "claude-code":
                mapping[row["receipt_id"]] = "synth-assigned"
        _assign_plan(plan, mapping)
        with mock.patch("open_chat_archive.gather_import.create_adapter", wraps=__import__(
                "open_chat_archive.source_adapters", fromlist=["create_adapter"]).create_adapter) as mocked:
            code, body, text, _err = _run([
                "apply", "--store", store, "--plan", plan, "--archives-parent", archives,
            ])
            adapters = [call.args[0] for call in mocked.call_args_list]
            self.assertNotIn("pi", adapters)
        self.assertEqual(body["counts"]["added"], 1, text)
        self.assertEqual(body["counts"]["importer_unavailable"], 1)
        self.assertEqual(body["code"], IMPORTER_UNAVAILABLE)
        self.assertEqual(code, 8)

    def test_scope_conflict_overlap_plan_permissions_and_leakage(self) -> None:
        store, plan, archives = self._layout([
            {"adapter": "claude-code", "origin": "/synthetic/claude.jsonl",
             "scope": "synth-scope-a", "data": claude_payload()},
        ])
        _run(["plan", "--store", store, "--plan", plan])
        parsed = parse_plan(Path(plan).read_bytes())
        _assign_plan(plan, {parsed["rows"][0]["receipt_id"]: "synth-other"})
        code, body, text, _err = _run([
            "apply", "--store", store, "--plan", plan, "--archives-parent", archives,
        ])
        self.assertNotEqual(code, 0)
        self.assertEqual(body["code"], SCOPE_PLAN_CONFLICT)
        self.assertGreaterEqual(body["counts"]["conflict"], 1)
        self.assertNotIn("synth-scope", text)
        self.assertNotIn("/synthetic/", text)

        inside = os.path.join(store, "nested-plan.json")
        code, body, text, _err = _run(["plan", "--store", store, "--plan", inside])
        self.assertEqual(code, 8)
        self.assertEqual(body["code"], SOURCE_STORE_OVERLAP)

        plan2 = os.path.join(self.base, "plan2.json")
        _run(["plan", "--store", store, "--plan", plan2])
        os.chmod(plan2, 0o644)
        code, body, text, _err = _run([
            "apply", "--store", store, "--plan", plan2, "--archives-parent", archives,
        ])
        self.assertNotEqual(code, 0)
        self.assertNotIn("Permission", text)
        self.assertNotIn("plan2", text)

        code, body, text, _err = _run(["plan", "--store", store, "--plan", plan2])
        self.assertEqual(code, 2)
        self.assertEqual(body["code"], PLAN_EXISTS)

        link = os.path.join(self.base, "plan-link.json")
        os.symlink(plan2, link)
        code, body, text, _err = _run([
            "apply", "--store", store, "--plan", link, "--archives-parent", archives,
        ])
        self.assertNotEqual(code, 0)
        self.assertNotIn(link, text)

        code, body, text, _err = _run([
            "apply", "--store", store, "--plan", plan2, "--archives-parent", store,
        ])
        self.assertNotEqual(code, 0)
        self.assertEqual(body["code"], SOURCE_STORE_OVERLAP)

    def test_mutated_batch_conflict_and_details_file(self) -> None:
        store, plan, archives = self._layout([
            {"adapter": "codex", "origin": "/synthetic/codex.jsonl",
             "scope": "synth-scope-b", "data": codex_payload()},
        ])
        details = os.path.join(self.base, "details.json")
        _run(["plan", "--store", store, "--plan", plan])
        code, body, text, _err = _run([
            "apply", "--store", store, "--plan", plan, "--archives-parent", archives,
            "--details-output", details,
        ])
        self.assertEqual(code, 0, text)
        self.assertNotIn("sha256:", text)
        self.assertTrue(Path(details).is_file())
        self.assertEqual(stat.S_IMODE(os.stat(details).st_mode), 0o600)
        archive = Path(archives) / archive_id_for("codex", "synth-scope-b")
        records = next((archive / "batches").glob("*/records.ndjson"))
        records.write_bytes(records.read_bytes() + b"\n")
        code, body, text, _err = _run([
            "apply", "--store", store, "--plan", plan, "--archives-parent", archives,
        ])
        self.assertEqual(code, 4)
        self.assertEqual(body["code"], IMPORT_CONFLICT)
        self.assertGreaterEqual(body["counts"]["conflict"], 1)
        self.assertEqual(body["counts"]["added"], 0)

    def test_size_limit_and_relative_args(self) -> None:
        store, plan, archives = self._layout([
            {"adapter": "claude-code", "origin": "/synthetic/claude.jsonl",
             "scope": "synth-scope-a", "data": claude_payload()},
        ])
        _run(["plan", "--store", store, "--plan", plan])
        with mock.patch("open_chat_archive.gather_import.ADAPTER_MAX_BYTES", 8):
            code, body, text, _err = _run([
                "apply", "--store", store, "--plan", plan, "--archives-parent", archives,
            ])
        self.assertNotEqual(code, 0)
        self.assertGreaterEqual(body["counts"]["limit_hits"], 1)
        self.assertGreaterEqual(body["counts"]["deferred"], 1)
        self.assertNotIn("/synthetic/", text)
        code, body, text, _err = _run(["verify", "--store", "not-absolute"])
        self.assertEqual(code, 2)
        self.assertEqual(body["code"], ARGUMENT_INVALID)
        self.assertNotIn("not-absolute", text)

    def test_verify_aggregate_and_pipeline_aliases(self) -> None:
        store, plan, archives = self._layout([
            {"adapter": "claude-code", "origin": "/synthetic/claude.jsonl",
             "scope": "synth-scope-a", "data": claude_payload()},
            {"adapter": "pi", "origin": "/synthetic/pi.jsonl", "scope": "", "data": pi_payload()},
        ])
        code, body, text, _err = _run(["verify", "--store", store])
        self.assertEqual(code, 0)
        self.assertEqual(body["operation"], "verify")
        self.assertEqual(body["store_state"], "verified")
        self.assertEqual(body["receipts"], 2)
        self.assertIn("pi", body["by_adapter"])
        self.assertNotIn("/synthetic/", text)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = pipeline_main(["verify", "--store", store])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(buf.getvalue())["operation"], "verify")

    def test_pipeline_run_and_render_unavailable(self) -> None:
        store, plan, archives = self._layout([
            {"adapter": "grok-build", "origin": "/synthetic/grok.jsonl",
             "scope": "synth-scope-c", "data": grok_payload()},
        ])
        _run(["plan", "--store", store, "--plan", plan])
        work = os.path.join(self.base, "work")
        vault = os.path.join(self.base, "vault-cmd")
        Path(vault).write_text("#!/bin/sh\nmkdir -p \"$7\"\nexit 0\n", encoding="utf-8")
        os.chmod(vault, 0o700)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            code = pipeline_main([
                "run", "--store", store, "--plan", plan, "--work-root", work,
                "--vault-command", vault,
            ])
        text = buf.getvalue()
        body = json.loads(text)
        self.assertEqual(code, 0, text)
        self.assertEqual(body["operation"], "run")
        self.assertTrue(body["complete"])
        self.assertTrue((Path(work) / ".oca-bridge-pipeline").is_file())
        self.assertTrue((Path(work) / "inspector" / "index.html").is_file())
        self.assertNotIn("/synthetic/", text)
        fail_vault = os.path.join(self.base, "vault-fail")
        Path(fail_vault).write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        os.chmod(fail_vault, 0o700)
        work2 = os.path.join(self.base, "work2")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            code = pipeline_main([
                "run", "--store", store, "--plan", plan, "--work-root", work2,
                "--vault-command", fail_vault,
            ])
        body = json.loads(buf.getvalue())
        self.assertEqual(code, 8)
        self.assertEqual(body["code"], PIPELINE_RENDER_UNAVAILABLE)
        self.assertFalse(body["complete"])

    def test_plan_bounded_extra_missing_and_origin_mismatch(self) -> None:
        store, plan, archives = self._layout([
            {"adapter": "claude-code", "origin": "/synthetic/claude-a.jsonl",
             "scope": "synth-scope-a", "data": claude_payload()},
            {"adapter": "codex", "origin": "/synthetic/codex.jsonl",
             "scope": "synth-scope-b", "data": codex_payload()},
        ])
        _run(["plan", "--store", store, "--plan", plan])
        parsed = parse_plan(Path(plan).read_bytes())
        only = parsed["rows"][0]["receipt_id"]
        code, body, text, _err = _run([
            "apply", "--store", store, "--plan", plan, "--archives-parent", archives,
            "--receipt-id", only,
        ])
        self.assertNotEqual(code, 0)
        self.assertEqual(body["code"], SCOPE_PLAN_CONFLICT)
        self.assertGreaterEqual(body["counts"]["conflict"], 1)
        self.assertEqual(body["counts"]["added"], 0)
        self.assertNotIn("/synthetic/", text)

        subset = os.path.join(self.base, "subset-plan.json")
        subset_doc = {"format": parsed["format"], "rows": [parsed["rows"][0]]}
        Path(subset).write_bytes(canonical_object_bytes(subset_doc))
        os.chmod(subset, 0o600)
        other = parsed["rows"][1]["receipt_id"]
        code, body, text, _err = _run([
            "apply", "--store", store, "--plan", subset, "--archives-parent", archives,
            "--receipt-id", other,
        ])
        self.assertEqual(body["code"], SCOPE_PLAN_CONFLICT)
        self.assertEqual(body["counts"]["added"], 0)

        mutated = parse_plan(Path(plan).read_bytes())
        mutated["rows"] = [mutated["rows"][0]]
        mutated["rows"][0]["origin_path"] = "/synthetic/other-origin.jsonl"
        origin_plan = os.path.join(self.base, "origin-plan.json")
        Path(origin_plan).write_bytes(canonical_object_bytes(mutated))
        os.chmod(origin_plan, 0o600)
        code, body, text, _err = _run([
            "apply", "--store", store, "--plan", origin_plan, "--archives-parent", archives,
            "--receipt-id", mutated["rows"][0]["receipt_id"],
        ])
        self.assertEqual(body["code"], SCOPE_PLAN_CONFLICT)
        self.assertEqual(body["counts"]["added"], 0)
        self.assertNotIn("other-origin", text)
        self.assertNotIn("/synthetic/", text)

        both = parse_plan(Path(plan).read_bytes())
        both["rows"][1]["origin_path"] = "/synthetic/other-origin.jsonl"
        both_plan = os.path.join(self.base, "both-origin-plan.json")
        Path(both_plan).write_bytes(canonical_object_bytes(both))
        os.chmod(both_plan, 0o600)
        code, body, text, _err = _run([
            "apply", "--store", store, "--plan", both_plan, "--archives-parent", archives,
        ])
        self.assertEqual(body["code"], SCOPE_PLAN_CONFLICT)
        self.assertEqual(body["counts"]["added"], 0)
        self.assertGreaterEqual(body["counts"]["conflict"], 1)
        self.assertEqual(list(Path(archives).glob("*/sources/*")), [])
        self.assertNotIn("other-origin", text)

    def test_details_output_cannot_poison_store(self) -> None:
        store, plan, archives = self._layout([
            {"adapter": "codex", "origin": "/synthetic/codex.jsonl",
             "scope": "synth-scope-b", "data": codex_payload()},
        ])
        _run(["plan", "--store", store, "--plan", plan])
        poisoned = os.path.join(store, "details.json")
        code, body, text, _err = _run([
            "apply", "--store", store, "--plan", plan, "--archives-parent", archives,
            "--details-output", poisoned,
        ])
        self.assertNotEqual(code, 0)
        self.assertEqual(body["code"], SOURCE_STORE_OVERLAP)
        self.assertFalse(os.path.lexists(poisoned))
        self.assertNotIn("details.json", text)
        names = set(os.listdir(store))
        self.assertNotIn("details.json", names)

    def test_failed_import_cleanup_then_retry(self) -> None:
        store, plan, archives = self._layout([
            {"adapter": "grok-build", "origin": "/synthetic/grok.jsonl",
             "scope": "synth-scope-c", "data": grok_payload()},
        ])
        _run(["plan", "--store", store, "--plan", plan])
        with mock.patch(
            "open_chat_archive.validation.validate_archive",
            side_effect=ValidationError("ARCHIVE_VALIDATION"),
        ):
            code, body, text, _err = _run([
                "apply", "--store", store, "--plan", plan, "--archives-parent", archives,
            ])
        self.assertNotEqual(code, 0)
        self.assertEqual(list(Path(archives).glob("*/sources/*")), [])
        self.assertEqual(list(Path(archives).glob("*/blobs/*")), [])
        self.assertEqual(list(Path(archives).glob("*/batches/*")), [])
        leftover_dirs = [p for p in Path(archives).iterdir() if p.is_dir() and p.name != "."]
        self.assertEqual(leftover_dirs, [])
        archive = Path(archives) / archive_id_for("grok-build", "synth-scope-c")
        self.assertFalse(archive.exists())
        self.assertNotIn("/synthetic/", text)

        with mock.patch(
            "open_chat_archive.gather_import.validate_archive",
            side_effect=ValidationError("forced-post-source"),
        ):
            code, body, text, _err = _run([
                "apply", "--store", store, "--plan", plan, "--archives-parent", archives,
            ])
        self.assertNotEqual(code, 0)
        self.assertEqual(list(Path(archives).glob("*/sources/*")), [])
        self.assertEqual(list(Path(archives).glob("*/batches/*")), [])
        self.assertFalse(archive.exists())

        code, body, text, _err = _run([
            "apply", "--store", store, "--plan", plan, "--archives-parent", archives,
        ])
        self.assertEqual(code, 0, text)
        self.assertEqual(body["counts"]["added"], 1)
        validate_archive(archive)

    def test_symlink_in_archive_is_not_read(self) -> None:
        store, plan, archives = self._layout([
            {"adapter": "codex", "origin": "/synthetic/codex.jsonl",
             "scope": "synth-scope-b", "data": codex_payload()},
        ])
        _run(["plan", "--store", store, "--plan", plan])
        code, body, text, _err = _run([
            "apply", "--store", store, "--plan", plan, "--archives-parent", archives,
        ])
        self.assertEqual(code, 0, text)
        secret = os.path.join(self.base, "secret-outside.txt")
        Path(secret).write_bytes(b"SECRET-BYTES-MUST-NOT-BE-READ")
        os.chmod(secret, 0o600)
        archive = Path(archives) / archive_id_for("codex", "synth-scope-b")
        records = next((archive / "batches").glob("*/records.ndjson"))
        records.unlink()
        records.symlink_to(secret)
        code, body, text, _err = _run([
            "apply", "--store", store, "--plan", plan, "--archives-parent", archives,
        ])
        self.assertNotEqual(code, 0)
        self.assertEqual(body["code"], IMPORT_CONFLICT)
        self.assertNotIn("SECRET-BYTES-MUST-NOT-BE-READ", text)
        self.assertNotIn("secret-outside", text)

    def test_archives_parent_exclusive_lock_busy(self) -> None:
        store, plan, archives = self._layout([
            {"adapter": "claude-code", "origin": "/synthetic/claude.jsonl",
             "scope": "synth-scope-a", "data": claude_payload()},
        ])
        _run(["plan", "--store", store, "--plan", plan])
        marker = os.path.join(archives, ARCHIVES_MARKER_NAME)
        Path(marker).write_bytes(ARCHIVES_MARKER_TEXT)
        os.chmod(marker, 0o600)
        os.chmod(archives, 0o700)
        fd = os.open(marker, os.O_RDONLY)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        def _unlock() -> None:
            try:
                os.close(fd)
            except OSError:
                pass

        self.addCleanup(_unlock)
        code, body, text, _err = _run([
            "apply", "--store", store, "--plan", plan, "--archives-parent", archives,
        ])
        self.assertEqual(code, 6)
        self.assertEqual(body["code"], STORE_BUSY)
        self.assertEqual(body["counts"]["added"], 0)
        self.assertNotIn("/synthetic/", text)

    def test_two_apply_processes_serialize_on_parent(self) -> None:
        store, plan, archives = self._layout([
            {"adapter": "claude-code", "origin": "/synthetic/claude.jsonl",
             "scope": "synth-scope-a", "data": claude_payload()},
        ])
        _run(["plan", "--store", store, "--plan", plan])
        marker = os.path.join(archives, ARCHIVES_MARKER_NAME)
        Path(marker).write_bytes(ARCHIVES_MARKER_TEXT)
        os.chmod(marker, 0o600)
        os.chmod(archives, 0o700)
        env = os.environ.copy()
        env["PYTHONPATH"] = SRC
        argv = [
            PYTHON, "-c",
            "import json,sys; from open_chat_archive.gather_import import main; "
            "raise SystemExit(main(sys.argv[1:]))",
            "apply", "--store", store, "--plan", plan, "--archives-parent", archives,
        ]
        procs = [
            subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
            for _ in range(2)
        ]
        results = []
        for proc in procs:
            out, err = proc.communicate(timeout=60)
            results.append((proc.returncode, out, err))
        bodies = []
        for code, out, err in results:
            self.assertNotIn("/synthetic/", out)
            self.assertNotIn("/synthetic/", err)
            bodies.append(json.loads(out))
        added = [body.get("counts", {}).get("added", 0) for body in bodies]
        self.assertLessEqual(sum(added), 1)
        codes = {body.get("code") for body in bodies}
        self.assertTrue(codes <= {"OK", STORE_BUSY})
        for body, rc in zip(bodies, [item[0] for item in results]):
            if body.get("code") == STORE_BUSY:
                self.assertEqual(rc, 6)
                self.assertFalse(body.get("complete"))
                self.assertEqual(body.get("counts", {}).get("added", 0), 0)
            elif body.get("code") == "OK":
                self.assertIn(body.get("counts", {}).get("added", 0), (0, 1))
        archive = Path(archives) / archive_id_for("claude-code", "synth-scope-a")
        if archive.is_dir():
            validate_archive(archive)
        self.assertNotIn(IMPORT_CONFLICT, codes)
        retry_added = 0
        for _ in range(2):
            code, body, text, _err = _run([
                "apply", "--store", store, "--plan", plan, "--archives-parent", archives,
            ])
            self.assertEqual(body["code"], "OK", text)
            self.assertNotEqual(code, 6)
            self.assertNotEqual(body["code"], IMPORT_CONFLICT)
            self.assertEqual(code, 0, text)
            self.assertEqual(body["counts"]["added"] + body["counts"]["unchanged"], 1)
            retry_added += body["counts"]["added"]
        self.assertEqual(sum(added) + retry_added, 1)
        ledger = Path(archives) / ".oca-bridge-import-ledger.json"
        self.assertTrue(ledger.is_file())
        data = json.loads(ledger.read_bytes())
        ids = [item["receipt_id"] for item in data["entries"]]
        self.assertEqual(len(ids), 1)
        self.assertEqual(len(ids), len(set(ids)))
        validate_archive(Path(archives) / archive_id_for("claude-code", "synth-scope-a"))

    def test_pipeline_partial_skips_render_and_stub_vault_flags(self) -> None:
        store, plan, archives = self._layout([
            {"adapter": "pi", "origin": "/synthetic/pi.jsonl", "scope": "", "data": pi_payload()},
            {"adapter": "claude-code", "origin": "/synthetic/claude.jsonl",
             "scope": "synth-scope-a", "data": claude_payload()},
        ])
        _run(["plan", "--store", store, "--plan", plan])
        work = os.path.join(self.base, "work-partial")
        vault = os.path.join(self.base, "vault-partial")
        Path(vault).write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        os.chmod(vault, 0o700)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            code = pipeline_main([
                "run", "--store", store, "--plan", plan, "--work-root", work,
                "--vault-command", vault,
            ])
        body = json.loads(buf.getvalue())
        self.assertNotEqual(code, 0)
        self.assertFalse(body["complete"])
        self.assertFalse((Path(work) / "inspector" / "index.html").exists())
        self.assertEqual(list((Path(work) / "vault").glob("*") if (Path(work) / "vault").exists() else []), [])

        store2 = os.path.join(self.base, "store-multi")
        make_store(store2, [
            {"adapter": "claude-code", "origin": "/synthetic/claude-a.jsonl",
             "scope": "synth-scope-a", "data": claude_payload()},
            {"adapter": "codex", "origin": "/synthetic/codex.jsonl",
             "scope": "synth-scope-b", "data": codex_payload()},
        ])
        plan2 = os.path.join(self.base, "plan-multi.json")
        _run(["plan", "--store", store2, "--plan", plan2])
        log = os.path.join(self.base, "vault-calls.jsonl")
        stub = os.path.join(self.base, "vault-stub")
        Path(stub).write_text(
            f"#!{PYTHON}\n"
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "if sys.argv[1:][:1] != ['build'] or len(sys.argv) != 8:\n"
            "    raise SystemExit(2)\n"
            "if sys.argv[2] != '--archive' or sys.argv[4] != '--attestation' or sys.argv[6] != '--output':\n"
            "    raise SystemExit(2)\n"
            "archive, att, out = sys.argv[3], sys.argv[5], sys.argv[7]\n"
            "if not (os.path.isabs(archive) and os.path.isabs(att) and os.path.isabs(out)):\n"
            "    raise SystemExit(2)\n"
            "Path(out).mkdir(parents=True, exist_ok=True)\n"
            "Path(out, 'ok').write_text('ok\\n')\n"
            "log = os.environ['VAULT_LOG']\n"
            "with open(log, 'a', encoding='utf-8') as handle:\n"
            "    handle.write(json.dumps(sys.argv[1:]) + '\\n')\n",
            encoding="utf-8",
        )
        os.chmod(stub, 0o700)
        work2 = os.path.join(self.base, "work-multi")
        env_log = os.environ.get("VAULT_LOG")
        os.environ["VAULT_LOG"] = log
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
                code = pipeline_main([
                    "run", "--store", store2, "--plan", plan2, "--work-root", work2,
                    "--vault-command", stub,
                ])
            text = buf.getvalue()
            body = json.loads(text)
        finally:
            if env_log is None:
                os.environ.pop("VAULT_LOG", None)
            else:
                os.environ["VAULT_LOG"] = env_log
        self.assertEqual(code, 0, text)
        self.assertTrue(body["complete"])
        lines = Path(log).read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        for line in lines:
            argv = json.loads(line)
            self.assertEqual(argv[0], "build")
            self.assertEqual(argv[1], "--archive")
            self.assertEqual(argv[3], "--attestation")
            self.assertEqual(argv[5], "--output")
            self.assertTrue(os.path.isabs(argv[2]))
            self.assertTrue(os.path.isabs(argv[4]))
            self.assertTrue(os.path.isabs(argv[6]))
            self.assertEqual(len(argv), 7)
        vault_outs = list((Path(work2) / "vault").iterdir())
        self.assertEqual(len(vault_outs), 2)
        for item in Path(work2).joinpath("archives").iterdir():
            if item.is_dir() and len(item.name) == 64:
                self.assertEqual(stat.S_IMODE(item.stat().st_mode), 0o700)
                for dirpath, dirnames, filenames in os.walk(item, followlinks=False):
                    self.assertEqual(stat.S_IMODE(os.stat(dirpath).st_mode), 0o700)
                    for name in filenames:
                        path = os.path.join(dirpath, name)
                        if os.path.islink(path):
                            continue
                        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_preexisting_invalid_archive_fails_before_mutation(self) -> None:
        store, plan, archives = self._layout([
            {"adapter": "codex", "origin": "/synthetic/codex-a.jsonl",
             "scope": "synth-scope-b", "data": codex_payload()},
            {"adapter": "codex", "origin": "/synthetic/codex-b.jsonl",
             "scope": "synth-scope-b", "data": codex_payload()},
        ])
        _run(["plan", "--store", store, "--plan", plan])
        parsed = parse_plan(Path(plan).read_bytes())
        first = parsed["rows"][0]["receipt_id"]
        subset = {"format": parsed["format"], "rows": [parsed["rows"][0]]}
        subset_path = os.path.join(self.base, "preexist-subset.json")
        Path(subset_path).write_bytes(canonical_object_bytes(subset))
        os.chmod(subset_path, 0o600)
        code, body, text, _err = _run([
            "apply", "--store", store, "--plan", subset_path, "--archives-parent", archives,
            "--receipt-id", first,
        ])
        self.assertEqual(code, 0, text)
        archive = Path(archives) / archive_id_for("codex", "synth-scope-b")
        before_sources = sorted(p.name for p in (archive / "sources").iterdir())
        before_batches = sorted(p.name for p in (archive / "batches").iterdir())
        junk = archive / "plaintext-backup.json"
        junk.write_text("{}\n")
        os.chmod(junk, 0o600)
        second = parsed["rows"][1]["receipt_id"]
        rest = {"format": parsed["format"], "rows": [parsed["rows"][1]]}
        rest_path = os.path.join(self.base, "preexist-rest.json")
        Path(rest_path).write_bytes(canonical_object_bytes(rest))
        os.chmod(rest_path, 0o600)
        code, body, text, _err = _run([
            "apply", "--store", store, "--plan", rest_path, "--archives-parent", archives,
            "--receipt-id", second,
        ])
        self.assertNotEqual(code, 0)
        self.assertEqual(body["code"], IMPORT_CONFLICT)
        self.assertEqual(body["counts"]["added"], 0)
        self.assertTrue(junk.is_file())
        self.assertEqual(sorted(p.name for p in (archive / "sources").iterdir()), before_sources)
        self.assertEqual(sorted(p.name for p in (archive / "batches").iterdir()), before_batches)
        self.assertNotIn("/synthetic/", text)

    def test_two_stores_serialize_on_same_parent(self) -> None:
        store1 = os.path.join(self.base, "store-a")
        store2 = os.path.join(self.base, "store-b")
        make_store(store1, [
            {"adapter": "claude-code", "origin": "/synthetic/claude-a.jsonl",
             "scope": "synth-scope-a", "data": claude_payload()},
        ])
        make_store(store2, [
            {"adapter": "codex", "origin": "/synthetic/codex.jsonl",
             "scope": "synth-scope-b", "data": codex_payload()},
        ])
        plan1 = os.path.join(self.base, "plan-a.json")
        plan2 = os.path.join(self.base, "plan-b.json")
        archives = os.path.join(self.base, "archives-shared")
        os.mkdir(archives, 0o700)
        os.chmod(archives, 0o700)
        _run(["plan", "--store", store1, "--plan", plan1])
        _run(["plan", "--store", store2, "--plan", plan2])
        marker = os.path.join(archives, ARCHIVES_MARKER_NAME)
        Path(marker).write_bytes(ARCHIVES_MARKER_TEXT)
        os.chmod(marker, 0o600)
        env = os.environ.copy()
        env["PYTHONPATH"] = SRC

        def argv_for(store: str, plan: str) -> list[str]:
            return [
                PYTHON, "-c",
                "import json,sys; from open_chat_archive.gather_import import main; "
                "raise SystemExit(main(sys.argv[1:]))",
                "apply", "--store", store, "--plan", plan, "--archives-parent", archives,
            ]

        procs = [
            subprocess.Popen(argv_for(store1, plan1), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env),
            subprocess.Popen(argv_for(store2, plan2), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env),
        ]
        results = []
        for proc in procs:
            out, err = proc.communicate(timeout=60)
            results.append((proc.returncode, out, err))
        bodies = []
        for code, out, err in results:
            self.assertNotIn("/synthetic/", out)
            self.assertNotIn("/synthetic/", err)
            bodies.append(json.loads(out))
        codes = {body.get("code") for body in bodies}
        self.assertTrue(codes <= {"OK", STORE_BUSY})
        added = sum(body.get("counts", {}).get("added", 0) for body in bodies)
        self.assertLessEqual(added, 1)
        for body, rc in zip(bodies, [item[0] for item in results]):
            if body.get("code") == STORE_BUSY:
                self.assertEqual(rc, 6)
                self.assertFalse(body.get("complete"))
                self.assertEqual(body.get("counts", {}).get("added", 0), 0)
        self.assertNotIn(IMPORT_CONFLICT, codes)
        for store, plan in ((store1, plan1), (store2, plan2)):
            code, body, text, _err = _run([
                "apply", "--store", store, "--plan", plan, "--archives-parent", archives,
            ])
            self.assertIn(body["code"], {"OK"})
            self.assertNotEqual(body["code"], IMPORT_CONFLICT)
            self.assertNotEqual(code, 6)
            self.assertEqual(code, 0, text)
            self.assertIn(body["counts"]["added"] + body["counts"]["unchanged"], {1})
            self.assertNotIn("/synthetic/", text)
        ledger = Path(archives) / ".oca-bridge-import-ledger.json"
        self.assertTrue(ledger.is_file())
        data = json.loads(ledger.read_bytes())
        ids = [item["receipt_id"] for item in data["entries"]]
        self.assertEqual(len(ids), 2)
        self.assertEqual(len(ids), len(set(ids)))
        validate_archive(Path(archives) / archive_id_for("claude-code", "synth-scope-a"))
        validate_archive(Path(archives) / archive_id_for("codex", "synth-scope-b"))

    def test_apply_unrelated_parent_dir_lock_follows_replace_publish(self) -> None:
        store, plan, archives = self._layout([
            {"adapter": "claude-code", "origin": "/synthetic/claude.jsonl",
             "scope": "synth-scope-a", "data": claude_payload()},
        ])
        _run(["plan", "--store", store, "--plan", plan])
        marker = os.path.join(archives, ARCHIVES_MARKER_NAME)
        Path(marker).write_bytes(ARCHIVES_MARKER_TEXT)
        os.chmod(marker, 0o600)
        os.chmod(archives, 0o700)
        ledger = os.path.join(archives, ".oca-bridge-import-ledger.json")
        script = (
            "import fcntl, os, sys, time\n"
            f"fd=os.open({archives!r}, os.O_RDONLY | os.O_DIRECTORY)\n"
            "fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
            "sys.stdout.write('ready\\n'); sys.stdout.flush()\n"
            "time.sleep(20)\n"
        )
        env = os.environ.copy()
        env["PYTHONPATH"] = SRC
        proc = subprocess.Popen(
            [PYTHON, "-c", script],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )

        def _stop() -> None:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
            if proc.stdout:
                proc.stdout.close()

        self.addCleanup(_stop)
        self.assertEqual(proc.stdout.readline().strip(), "ready")
        argv = [
            PYTHON, "-c",
            "import sys; from open_chat_archive.gather_import import main; "
            "raise SystemExit(main(sys.argv[1:]))",
            "apply", "--store", store, "--plan", plan, "--archives-parent", archives,
        ]
        apply_proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
        )
        try:
            out, err = apply_proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            apply_proc.kill()
            apply_proc.wait(timeout=5)
            self.fail("apply deadlocked while an unrelated process held the parent directory lock")
        self.assertNotIn("/synthetic/", out)
        self.assertNotIn("/synthetic/", err)
        body = json.loads(out)
        self.assertEqual(apply_proc.returncode, 6)
        self.assertEqual(body["code"], STORE_BUSY)
        self.assertEqual(body["counts"]["added"], 0)
        self.assertFalse(os.path.lexists(ledger))
        _stop()


if __name__ == "__main__":
    unittest.main()
