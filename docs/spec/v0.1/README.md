# Open Chat Archive v0.1 draft

Tier 1 preserves exact source bytes, conforms protected immutable NDJSON, validates evidence and integrity, and supports safe adapter/Markdown authoring. Normative requirements are executable in JSON Schema, `validation.py`, the profiler/verifier, repository tests, or the named operational controls in the security profile.

Core invariants: artifact-independent stable logical identity when evidence supports it; semantic revisions exclude observations/import bookkeeping; complete field evidence with approved proposal gates; lossless sparse rich-event payloads; strict UTF-8 JSON; verified archive-root paths and bytes; model-safe `shape-only-v1`; and git-crypt coverage for all legal archive data paths.

Documents: [data model](data-model.md), [serialization](serialization.md), [adapter contract](adapter-contract.md), [security](security.md), and [decisions](decisions.md).

Draft deferrals are ZIP profiling, a complete Markdown parser, advanced cross-source equivalence, and all Tier 2 indexing/search/embedding/retrieval/UI work.
