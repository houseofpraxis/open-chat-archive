"""Archive attestation CLI tests. Synthetic archives only."""
from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

from open_chat_archive.gather_store import canonical_object_bytes
from open_chat_archive.synthetic import create_synthetic_batch
from open_chat_archive.validation import (
    ATTESTATION_BATCH_KEYS,
    ATTESTATION_FORMAT,
    ATTESTATION_KIND,
    ATTESTATION_KEYS,
    ATTESTATION_MANIFEST_KEYS,
    ATTESTATION_RECORDS_KEYS,
    ATTESTATION_SPEC_VERSION,
    ATTESTATION_STATUS,
    ATTESTATION_TARGET,
    ATTESTATION_VALIDATOR_KEYS,
    ATTESTATION_VALIDATOR_NAME,
    ValidationError,
    build_validation_attestation,
    main,
    validate_archive,
    write_validation_attestation,
)

# Exact Python/Go attestation contract. Go tests can mirror these key tuples
# and the canonical JSON object (sorted keys, one trailing LF) produced by
# canonical_object_bytes / Go CanonicalBytes+newline.
CONTRACT_ATTESTATION_KEYS = (
    "archive_id", "attestation_kind", "batches", "format", "spec_version", "status", "validator",
)
CONTRACT_VALIDATOR_KEYS = ("name", "package_version")
CONTRACT_BATCH_KEYS = ("batch_id", "manifest", "records")
CONTRACT_MANIFEST_KEYS = ("byte_digest", "path", "size")
CONTRACT_RECORDS_KEYS = ("byte_digest", "count", "path", "size")


class ValidationAttestationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        os.chmod(self.root, 0o700)
        self.archive = self.root / "archives" / "arc"
        self.batch = create_synthetic_batch(self.archive, archive_id="arc", batch_id="batch-1")

    def test_attestation_after_validate_and_tamper_binding(self) -> None:
        dest = self.root / "attestation.json"
        validate_archive(self.archive)
        write_validation_attestation(self.archive, dest)
        raw = dest.read_bytes()
        self.assertTrue(raw.endswith(b"\n") and raw.count(b"\n") == 1)
        self.assertEqual(stat.S_IMODE(dest.stat().st_mode), 0o600)
        body = json.loads(raw)
        self.assertEqual(body["format"], ATTESTATION_FORMAT)
        self.assertEqual(body["spec_version"], ATTESTATION_SPEC_VERSION)
        self.assertEqual(body["attestation_kind"], ATTESTATION_KIND)
        self.assertEqual(body["status"], ATTESTATION_STATUS)
        self.assertEqual(body["archive_id"], "arc")
        self.assertEqual(body["validator"]["name"], ATTESTATION_VALIDATOR_NAME)
        self.assertIn("package_version", body["validator"])
        self.assertEqual(len(body["batches"]), 1)
        row = body["batches"][0]
        self.assertEqual(row["batch_id"], "batch-1")
        self.assertEqual(row["manifest"]["path"], "batches/batch-1/manifest.json")
        self.assertEqual(row["records"]["path"], "batches/batch-1/records.ndjson")
        manifest = (self.archive / "batches" / "batch-1" / "manifest.json").read_bytes()
        records = (self.archive / "batches" / "batch-1" / "records.ndjson").read_bytes()
        self.assertEqual(row["manifest"]["byte_digest"], "sha256:" + hashlib.sha256(manifest).hexdigest())
        self.assertEqual(row["records"]["byte_digest"], "sha256:" + hashlib.sha256(records).hexdigest())
        self.assertEqual(row["manifest"]["size"], len(manifest))
        self.assertEqual(row["records"]["size"], len(records))
        dumped = json.dumps(body)
        self.assertNotIn(str(self.archive), dumped)
        self.assertNotIn("elapsed", dumped)
        records_path = self.archive / "batches" / "batch-1" / "records.ndjson"
        records_path.write_bytes(records + b" ")
        with self.assertRaises(ValidationError):
            validate_archive(self.archive)
        tampered = records_path.read_bytes()
        self.assertNotEqual("sha256:" + hashlib.sha256(tampered).hexdigest(), row["records"]["byte_digest"])
        with self.assertRaises(ValidationError):
            build_validation_attestation(self.archive)

    def test_cli_flag_archive_only_and_existing_behavior(self) -> None:
        dest = self.root / "cli-attestation.json"
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main([str(self.archive), "--attestation-output", str(dest)])
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue().strip(), "validation passed")
        self.assertTrue(dest.is_file())
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main([str(self.archive)])
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue().strip(), "validation passed")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main([str(self.batch), "--attestation-output", str(self.root / "batch.json")])
        self.assertEqual(code, 2)
        self.assertIn(ATTESTATION_TARGET, out.getvalue())
        self.assertFalse((self.root / "batch.json").exists())
        repo = Path(__file__).resolve().parents[1]
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main([str(repo), "--attestation-output", str(self.root / "repo.json")])
        self.assertEqual(code, 2)
        self.assertIn(ATTESTATION_TARGET, out.getvalue())
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main([str(self.archive), "--attestation-output", str(dest)])
        self.assertNotEqual(code, 0)
        self.assertIn("ATTESTATION_EXISTS", out.getvalue())

    def test_refuse_overlap_and_relative(self) -> None:
        inside = self.archive / "attestation.json"
        with self.assertRaises(ValidationError) as raised:
            write_validation_attestation(self.archive, inside)
        self.assertIn("ATTESTATION_OVERLAP", str(raised.exception))
        with self.assertRaises(ValidationError):
            write_validation_attestation(self.archive, Path("relative.json"))
        built = build_validation_attestation(self.archive)
        self.assertEqual(built["batches"][0]["batch_id"], "batch-1")
        self.assertEqual(built["attestation_kind"], ATTESTATION_KIND)
        self.assertEqual(built["spec_version"], ATTESTATION_SPEC_VERSION)

    def test_exact_keys_canonical_bytes_and_go_contract_fixture(self) -> None:
        dest = self.root / "contract.json"
        write_validation_attestation(self.archive, dest)
        raw = dest.read_bytes()
        body = json.loads(raw)
        self.assertEqual(tuple(sorted(body)), tuple(sorted(CONTRACT_ATTESTATION_KEYS)))
        self.assertEqual(set(body), set(ATTESTATION_KEYS))
        self.assertEqual(set(body["validator"]), set(ATTESTATION_VALIDATOR_KEYS))
        self.assertEqual(set(body["validator"]), set(CONTRACT_VALIDATOR_KEYS))
        self.assertEqual(set(body["batches"][0]), set(ATTESTATION_BATCH_KEYS))
        self.assertEqual(set(body["batches"][0]), set(CONTRACT_BATCH_KEYS))
        self.assertEqual(set(body["batches"][0]["manifest"]), set(ATTESTATION_MANIFEST_KEYS))
        self.assertEqual(set(body["batches"][0]["manifest"]), set(CONTRACT_MANIFEST_KEYS))
        self.assertEqual(set(body["batches"][0]["records"]), set(ATTESTATION_RECORDS_KEYS))
        self.assertEqual(set(body["batches"][0]["records"]), set(CONTRACT_RECORDS_KEYS))
        self.assertNotIn("sha256", body["batches"][0]["manifest"])
        self.assertNotIn("sha256", body["batches"][0]["records"])
        rebuilt = canonical_object_bytes(body)
        self.assertEqual(raw, rebuilt)
        self.assertEqual(raw, canonical_object_bytes(build_validation_attestation(self.archive)))
        # Sorted object keys in canonical bytes: archive_id, attestation_kind, batches, ...
        text = raw.decode("utf-8")
        self.assertTrue(text.startswith('{"archive_id":'))
        self.assertIn('"attestation_kind":"oca-archive-validation"', text)
        self.assertIn('"spec_version":"0.1"', text)
        self.assertIn('"format":"oca-validation-attestation/v0.1-experimental"', text)
        fixture = self.root / "go-mirror-attestation.json"
        fixture.write_bytes(raw)
        os.chmod(fixture, 0o600)
        mirrored = json.loads(fixture.read_bytes())
        self.assertEqual(set(mirrored), set(CONTRACT_ATTESTATION_KEYS))
        self.assertEqual(canonical_object_bytes(mirrored), raw)

    def test_builder_cannot_pass_without_validate_and_refuses_symlink(self) -> None:
        secret = self.root / "secret-outside.txt"
        secret.write_bytes(b"SECRET-BYTES-MUST-NOT-BE-READ")
        os.chmod(secret, 0o600)
        records = self.archive / "batches" / "batch-1" / "records.ndjson"
        records.unlink()
        records.symlink_to(secret)
        with self.assertRaises(ValidationError):
            build_validation_attestation(self.archive)
        dest = self.root / "from-symlink.json"
        with self.assertRaises(ValidationError):
            write_validation_attestation(self.archive, dest)
        self.assertFalse(dest.exists())


if __name__ == "__main__":
    unittest.main()
