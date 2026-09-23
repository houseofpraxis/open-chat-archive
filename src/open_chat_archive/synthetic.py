"""Wholly synthetic batch builder used by examples and scaffold verification."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .canonical import digest, entity_identity, finalize_record, identity_input

NS="org.open-chat-archive.synthetic"


def _identity(archive_id: str, record_type: str, key: str, *, scope: str="synthetic-scope") -> dict[str,str]:
    return identity_input(archive_id=archive_id,record_type=record_type,adapter_namespace=NS,identity_scheme="native-id",identity_version="1",source_scope_id=scope,logical_key=key)


def _record(archive_id: str, record_type: str, key: str, body: dict[str,Any]) -> dict[str,Any]:
    identity=_identity(archive_id,record_type,key)
    return finalize_record({"spec_version":"0.1","record_type":record_type,"entity_id":entity_identity(identity),"revision_id":"sha256:"+"0"*64,"sensitivity":"restricted-unclassified","identity":identity,"body":body})


def _observation(archive_id: str, subject: dict[str,Any], source_id: str, pointer: str="") -> dict[str,Any]:
    body={"subject":{"entity_id":subject["entity_id"],"revision_id":subject["revision_id"]},"bindings":[{"field_path":"/body","claim_basis":"parsed","sources":[{"artifact_id":source_id,"location":{"json_pointer":pointer}}]}]}
    return _record(archive_id,"observation",digest(body),body)


def create_synthetic_batch(archive_root: Path, *, archive_id: str, batch_id: str, rich: bool=False,
                           expanded: bool=False, changed: bool=False) -> Path:
    archive_root.mkdir(parents=True,exist_ok=True); (archive_root/"sources").mkdir(exist_ok=True); (archive_root/"blobs").mkdir(exist_ok=True)
    source_obj={"conversations":[{"id":"chat-alpha","title":"Changed synthetic title" if changed else "Synthetic hello","messages":[{"id":"m1","role":"user","text":"Hello synthetic archive"},{"id":"m2","role":"assistant","text":"Hello, synthetic user"}]}]}
    if expanded: source_obj["conversations"].insert(0,{"id":"chat-beta","title":"New synthetic conversation","messages":[]})
    if rich: source_obj["events"]=[{"kind":"tool","arguments":{"city":"Exampleville","units":["metric"]},"result":{"temperature":21}},{"kind":"model","provider":"ExampleAI","model":"example-1"},{"kind":"log","message":"synthetic log line"}]
    source_raw=(json.dumps(source_obj,ensure_ascii=False,sort_keys=True,separators=(",",":"))+"\n").encode(); source_digest="sha256:"+hashlib.sha256(source_raw).hexdigest(); source_name="object-"+source_digest[7:19]
    (archive_root/"sources"/source_name).write_bytes(source_raw)
    source=_record(archive_id,"source_artifact",source_digest,{"archive_id":archive_id,"byte_digest":source_digest,"media_type":"application/json","size":len(source_raw),"storage_path":"sources/"+source_name,"acquisition_method":"synthetic-generation"})
    records=[source]
    session=_record(archive_id,"session","chat-alpha",{"archive_id":archive_id,"title":"Changed synthetic title" if changed else "Synthetic hello"}); records.append(session)
    session_observation=_observation(archive_id,session,source["entity_id"],"/conversations/1" if expanded else "/conversations/0"); records.append(session_observation)
    if rich:
        proposal=_record(archive_id,"proposal","proposal-title",{"subject":{"entity_id":session["entity_id"],"revision_id":session["revision_id"]},"field_path":"/body/title","proposed_value":"Synthetic hello","basis":"model_inferred","status":"approved","evidence":[{"artifact_id":source["entity_id"],"location":{"json_pointer":"/conversations/0/title"}}],"approval":{"status":"approved","at":"2026-09-02T00:00:00Z","approver_basis":"user"}})
        session_observation["body"]["bindings"].append({"field_path":"/body/title","claim_basis":"model_inferred","proposal_id":proposal["entity_id"]})
        session_observation.update(finalize_record(session_observation)); records.append(proposal)
    for n,(mid,role,text) in enumerate((("m1","user","Hello synthetic archive"),("m2","assistant","Hello, synthetic user"))):
        message=_record(archive_id,"message",mid,{"session_id":session["entity_id"],"source_ordinal":n,"role":role,"visibility":"user_visible","content":[{"type":"text","text":text}]}); records += [message,_observation(archive_id,message,source["entity_id"],f"/conversations/{1 if expanded else 0}/messages/{n}")]
    if expanded:
        beta=_record(archive_id,"session","chat-beta",{"archive_id":archive_id,"title":"New synthetic conversation"}); records += [beta,_observation(archive_id,beta,source["entity_id"],"/conversations/0")]
    blob_items=[]
    if rich:
        log_raw=b"synthetic log line\n"; log_digest="sha256:"+hashlib.sha256(log_raw).hexdigest(); log_name="object-"+log_digest[7:19]; (archive_root/"blobs"/log_name).write_bytes(log_raw)
        blob_items=[{"path":"blobs/"+log_name,"byte_digest":log_digest,"media_type":"text/plain","size":len(log_raw)}]
        artifact=_record(archive_id,"artifact","log-1",{"kind":"log","byte_digest":log_digest,"media_type":"text/plain","size":len(log_raw),"storage_path":"blobs/"+log_name,"source_reference":{"artifact_id":source["entity_id"],"location":{"json_pointer":"/events/2/message"}}}); records += [artifact,_observation(archive_id,artifact,source["entity_id"],"/events/2")]
        span=_record(archive_id,"span","span-root",{"session_id":session["entity_id"],"source_ordinal":2,"name":"synthetic operation","status":"ok"}); child=_record(archive_id,"span","span-child",{"session_id":session["entity_id"],"source_ordinal":3,"name":"nested synthetic operation","status":"ok","parent_span_id":span["entity_id"]})
        records += [span,_observation(archive_id,span,source["entity_id"],"/events/0"),child,_observation(archive_id,child,source["entity_id"],"/events/0")]
        event_bodies=[
          {"session_id":session["entity_id"],"source_ordinal":4,"kind":"tool_invocation","correlation_id":"corr-1","span_id":child["entity_id"],"payload":{"tool_namespace":"org.example.tools","tool_name":"weather","call_id":"call-1","arguments":{"json":{"city":"Exampleville","units":["metric"]}}}},
          {"session_id":session["entity_id"],"source_ordinal":5,"kind":"tool_result","correlation_id":"corr-1","span_id":child["entity_id"],"payload":{"call_id":"call-1","status":"ok","output":{"json":{"temperature":21}}}},
          {"session_id":session["entity_id"],"source_ordinal":6,"kind":"model_invocation","payload":{"provider":"ExampleAI","model_name":"example-1","model_revision":"2026-01","status":"started","parameters":{"json":{"temperature_milli":200}},"request_refs":[records[2]["entity_id"]]}},
          {"session_id":session["entity_id"],"source_ordinal":7,"kind":"model_response","payload":{"provider":"ExampleAI","model_name":"example-1","model_revision":"2026-01","status":"completed","response_refs":[records[4]["entity_id"]],"finish_reason":"stop"}},
          {"session_id":session["entity_id"],"source_ordinal":8,"kind":"log","payload":{"artifact_id":artifact["entity_id"],"level":"info","logger":"synthetic","attributes":{"sequence":1}}},
          {"session_id":session["entity_id"],"source_ordinal":9,"kind":"usage","payload":{"input_tokens":12,"output_tokens":7,"total_tokens":19,"cache_read_tokens":2,"reasoning_tokens":1,"cost":"0.0019","currency":"USD","unknown_metrics":{"org.example.provider":{"audio_tokens":0}}}}
        ]
        for i,body in enumerate(event_bodies):
            event=_record(archive_id,"event",f"event-{i}",body); records += [event,_observation(archive_id,event,source["entity_id"],f"/events/{min(i,2)}")]
    profile_paths=["/k0001","/k0001/*"] + (["/k0002","/k0002/*"] if rich else [])
    ledger=[{"path":p,"disposition":"preserved","preservation_target":"source-artifact"} for p in profile_paths]
    batch=_record(archive_id,"import_batch",batch_id,{"archive_id":archive_id,"batch_id":batch_id,"adapter":{"namespace":NS,"version":"0.1.0"},"format_fingerprint":"synthetic.json.v1","profile_paths":profile_paths,"coverage_ledger":ledger}); records.append(batch)
    records.sort(key=lambda r:(r["record_type"],r["entity_id"],r["revision_id"])); raw=b"".join(json.dumps(r,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False).encode()+b"\n" for r in records)
    batch_dir=archive_root/"batches"/batch_id; batch_dir.mkdir(parents=True,exist_ok=True); (batch_dir/"records.ndjson").write_bytes(raw)
    manifest={"spec_version":"0.1","profile":"batch-ndjson","sensitivity":"restricted-unclassified","archive_id":archive_id,"batch_id":batch_id,"created_at":"2026-09-02T00:00:00Z","adapter":{"namespace":NS,"version":"0.1.0"},"sources":[{"path":"sources/"+source_name,"byte_digest":source_digest,"media_type":"application/json","size":len(source_raw)}],"blobs":blob_items,"records":{"path":"records.ndjson","byte_digest":"sha256:"+hashlib.sha256(raw).hexdigest(),"size":len(raw),"count":len(records),"type_counts":dict(sorted(__import__('collections').Counter(r["record_type"] for r in records).items()))}}
    (batch_dir/"manifest.json").write_text(json.dumps(manifest,sort_keys=True,indent=2)+"\n",encoding="utf-8")
    return batch_dir
