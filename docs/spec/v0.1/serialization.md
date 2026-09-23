# Immutable batch NDJSON serialization

Normative archive-root layout:

```text
archives/<opaque-archive>/
  sources/<opaque-object>
  blobs/<opaque-object>
  batches/<opaque-batch>/manifest.json
  batches/<opaque-batch>/records.ndjson
  import-manifests/<opaque-manifest>.json
```

Only these data-bearing paths are legal. Components are opaque and must not contain user identifiers or titles. The manifest and records are restricted-unclassified. UTF-8 BOM, duplicate JSON keys, floats/non-finite numbers, unsafe decoding, CRLF, blank NDJSON lines, and a missing final LF are invalid. Records are compact JSON sorted by `(record_type, entity_id, revision_id)`.

Batch validation compiles and applies both Draft 2020-12 schemas, verifies a single batch's manifest/records, hashes, sizes, ordering, counts, identities, ranges, references, evidence and coverage. Archive validation additionally uses an explicit archive root, rejects traversal, symlinks, devices, non-regular or alternate files, verifies every source/blob and reconciles batches. Repository self-tests test implementations and mutation cases; these three results are not interchangeable. A source-format-specific parser may retain finite decimal token lexemes solely while interpreting exact retained source bytes; those wrappers are non-canonical and cannot appear in records. Canonical serialization continues to reject Python floats, non-finite values, and all non-JSON wrapper objects.
