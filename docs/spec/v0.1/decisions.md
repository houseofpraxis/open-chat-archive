# Recorded decisions

1. Tier 1 is exact preservation, conformation, validation, and adapter/Markdown authoring only.
2. Logical identity uses protected adapter-scoped keys; physical batches and evidence observations are separate.
3. Semantic revisions exclude clocks, paths, runs, batches, and newly observed evidence.
4. Canonical model inference requires an approved append-only proposal.
5. Rich telemetry remains sparse when sparse and lossless when supplied.
6. `shape-only-v1` discloses opaque topology, types, bounded counts, and coordinates—not raw keys/values/hashes.
7. Git-crypt plus an operator staged-ciphertext gate protects committed sensitive blobs; no worktree or sandbox claim is made.
8. When a source supplies canonical-safe time or model identity, adapters project it onto session/message bodies; `source_ordinal` is unique per record type per session; events may name their issuing message; ordered children use `sibling_index`; parsed JSON/JSONL evidence must include a pointer or line coordinate, and projected fields appear as `mapped` coverage-ledger rows.
9. OpenWebUI adapter 0.1.3 keeps v0.1 wire/schema compatibility: a validated graph with multiple explicit roots is canonically `thread_shape=unknown` and records its `forest` interpretation only in `extensions.org.open-chat-archive.open-webui.thread_shape`. `forest` is not a v0.1 canonical enum. The adapter version changes batch metadata; message and relationship identities and semantics remain stable, while changed session or lifecycle facts append revisions rather than changing entities.
