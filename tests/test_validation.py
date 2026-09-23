import hashlib, json, shutil, tempfile, unittest
from pathlib import Path
from open_chat_archive.canonical import finalize_record
from open_chat_archive.strictjson import StrictJSONError, loads_bytes
from open_chat_archive.synthetic import create_synthetic_batch
from open_chat_archive.validation import ValidationError, validate_archive, validate_batch

ROOT=Path(__file__).resolve().parents[1]

def rewrite(batch, records=None, raw=None):
    manifest=json.loads((batch/"manifest.json").read_text())
    if records is not None:
        records.sort(key=lambda r:(r["record_type"],r["entity_id"],r["revision_id"]))
        raw=b''.join(json.dumps(r,sort_keys=True,separators=(',',':')).encode()+b'\n' for r in records)
        manifest["records"]["count"]=len(records); from collections import Counter
        manifest["records"]["type_counts"]=dict(sorted(Counter(r["record_type"] for r in records).items()))
    (batch/"records.ndjson").write_bytes(raw); manifest["records"]["size"]=len(raw); manifest["records"]["byte_digest"]='sha256:'+hashlib.sha256(raw).hexdigest()
    (batch/"manifest.json").write_text(json.dumps(manifest,sort_keys=True,indent=2)+'\n')

class ValidationTests(unittest.TestCase):
    def make(self, **kw):
        temp=tempfile.TemporaryDirectory(); root=Path(temp.name)/"archives"/"arc"; batch=create_synthetic_batch(root,archive_id="arc",batch_id="batch-1",**kw); return temp,root,batch

    def test_reordered_expanded_exports_converge(self):
        with tempfile.TemporaryDirectory() as d:
            a=create_synthetic_batch(Path(d)/"a"/"archives"/"arc",archive_id="arc",batch_id="a")
            b=create_synthetic_batch(Path(d)/"b"/"archives"/"arc",archive_id="arc",batch_id="b",expanded=True)
            c=create_synthetic_batch(Path(d)/"c"/"archives"/"arc",archive_id="arc",batch_id="c",expanded=True,changed=True)
            semantic=lambda p:{(r["record_type"],r["identity"]["logical_key"]):(r["entity_id"],r["revision_id"]) for r in validate_batch(p,archive_root=p.parents[1]) if r["record_type"] in {"session","message"}}
            aa,bb,cc=semantic(a),semantic(b),semantic(c)
            self.assertEqual(aa[("session","chat-alpha")],bb[("session","chat-alpha")]); self.assertIn(("session","chat-beta"),bb)
            self.assertEqual(bb[("session","chat-alpha")][0],cc[("session","chat-alpha")][0]); self.assertNotEqual(bb[("session","chat-alpha")][1],cc[("session","chat-alpha")][1])
            self.assertEqual(bb[("message","m1")],cc[("message","m1")])

    def test_tamper_delete_symlink_and_alternate_path(self):
        for mode in ("tamper","delete","symlink"):
            temp,root,batch=self.make(); self.addCleanup(temp.cleanup); manifest=json.loads((batch/"manifest.json").read_text()); source=root/manifest["sources"][0]["path"]
            if mode=="tamper": source.write_bytes(source.read_bytes()+b'x')
            elif mode=="delete": source.unlink()
            else:
                target=root/"outside"; target.write_text('x'); source.unlink(); source.symlink_to(target)
            with self.assertRaises(ValidationError): validate_batch(batch,archive_root=root)
        temp,root,batch=self.make(); self.addCleanup(temp.cleanup); (root/"plaintext-backup.json").write_text('{}')
        with self.assertRaisesRegex(ValidationError,"ARCHIVE_PATH_ALLOWLIST"): validate_archive(root)

    def test_duplicate_key_float_duplicate_record_and_bad_date(self):
        for bad in (b'{"a":1,"a":2}',b'{"a":1.0}',b'{"a":NaN}',b'\xef\xbb\xbf{}',b'{"a":"\\ud800"}'):
            with self.assertRaises(StrictJSONError): loads_bytes(bad)
        temp,root,batch=self.make(); self.addCleanup(temp.cleanup); raw=(batch/"records.ndjson").read_bytes(); rewrite(batch,raw=raw+raw.splitlines(keepends=True)[0])
        with self.assertRaises(ValidationError): validate_batch(batch,archive_root=root)
        temp,root,batch=self.make(); self.addCleanup(temp.cleanup); manifest=json.loads((batch/"manifest.json").read_text()); manifest["created_at"]="not-a-date"; (batch/"manifest.json").write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValidationError,"MANIFEST_SCHEMA"): validate_batch(batch,archive_root=root)

    def test_mismatch_range_coverage_and_proposal_gate(self):
        temp,root,batch=self.make(); self.addCleanup(temp.cleanup); records=[json.loads(x) for x in (batch/"records.ndjson").read_text().splitlines()]
        batch_record=next(r for r in records if r["record_type"]=="import_batch"); batch_record["body"]["coverage_ledger"].pop(); batch_record.update(finalize_record(batch_record)); rewrite(batch,records)
        with self.assertRaisesRegex(ValidationError,"COVERAGE_INCOMPLETE"): validate_batch(batch,archive_root=root)
        temp,root,batch=self.make(); self.addCleanup(temp.cleanup); records=[json.loads(x) for x in (batch/"records.ndjson").read_text().splitlines()]
        obs=next(r for r in records if r["record_type"]=="observation"); bind=obs["body"]["bindings"][0]; bind["claim_basis"]="model_inferred"; bind.pop("sources"); bind["proposal_id"]='sha256:'+'0'*64; obs.update(finalize_record(obs)); rewrite(batch,records)
        with self.assertRaisesRegex(ValidationError,"PROPOSAL_GATE"): validate_batch(batch,archive_root=root)

    def test_rich_payload_round_trip(self):
        temp,root,batch=self.make(rich=True); self.addCleanup(temp.cleanup); records=validate_batch(batch,archive_root=root); events={r["body"]["kind"]:r["body"]["payload"] for r in records if r["record_type"]=="event"}
        self.assertEqual(events["tool_invocation"]["arguments"]["json"]["units"],["metric"]); self.assertEqual(events["tool_result"]["output"]["json"]["temperature"],21)
        self.assertEqual(events["model_invocation"]["model_revision"],"2026-01"); self.assertEqual(events["usage"]["unknown_metrics"]["org.example.provider"]["audio_tokens"],0)

    def test_blob_artifact_reference_and_adapter_metadata_reconcile(self):
        temp,root,batch=self.make(rich=True); self.addCleanup(temp.cleanup)
        records=[json.loads(x) for x in (batch/"records.ndjson").read_text().splitlines()]
        artifact=next(r for r in records if r["record_type"]=="artifact")
        old_revision=artifact["revision_id"]; artifact["body"]["size"] += 1; artifact.update(finalize_record(artifact))
        observation=next(r for r in records if r["record_type"]=="observation" and r["body"]["subject"]["revision_id"]==old_revision)
        observation["body"]["subject"]["revision_id"]=artifact["revision_id"]; observation.update(finalize_record(observation)); rewrite(batch,records)
        with self.assertRaisesRegex(ValidationError,"BLOB_METADATA"): validate_batch(batch,archive_root=root)
        temp,root,batch=self.make(); self.addCleanup(temp.cleanup)
        records=[json.loads(x) for x in (batch/"records.ndjson").read_text().splitlines()]
        session=next(r for r in records if r["record_type"]=="session")
        session["identity"]["adapter_namespace"]="org.example.other"
        from open_chat_archive.canonical import entity_identity
        session["entity_id"]=entity_identity(session["identity"]); session.update(finalize_record(session)); rewrite(batch,records)
        with self.assertRaisesRegex(ValidationError,"IDENTITY_METADATA"): validate_batch(batch,archive_root=root)

    def test_tool_result_requires_output_or_error(self):
        temp,root,batch=self.make(rich=True); self.addCleanup(temp.cleanup)
        records=[json.loads(x) for x in (batch/"records.ndjson").read_text().splitlines()]
        event=next(r for r in records if r["record_type"]=="event" and r["body"]["kind"]=="tool_result")
        event["body"]["payload"].pop("output"); event.update(finalize_record(event)); rewrite(batch,records)
        with self.assertRaisesRegex(ValidationError,"RECORD_SCHEMA"): validate_batch(batch,archive_root=root)

if __name__ == "__main__": unittest.main()
