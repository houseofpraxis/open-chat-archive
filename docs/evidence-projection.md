# Experimental evidence projection

`open_chat_archive.evidence_projection.project_archive(path: Path) -> dict`
is a local Tier 2 consumer interface. It loads the archive through OCA's
validated inspection model and returns `projection_version: "oca-evidence-v1"`,
the exact archive ID, and a deterministic array of records. It does not modify
Tier 1 schemas, canonical IDs, or source files.

Each array entry preserves the exact canonical record envelope, including its
origin identity, and adds:

- `occurrences`: every batch and exact 1-based NDJSON ordinal, sorted by batch
  ID and ordinal;
- `session_ids`: only session IDs associated by OCA inspection;
- `text`: concatenated text from `text` and `code` content parts of
  `user_visible` messages. Reasoning, non-visible messages, events, and
  arbitrary extensions are not included;
- `citations`: bindings from observations whose subject matches this exact
  entity and revision. Each citation retains the observation entity/revision,
  field path, claim basis, and source artifact ID, canonical byte digest, and
  a deep copy of the source location. A binding without source evidence stays
  present with an empty `sources` array.

All revisions and occurrences are retained. There are no generated timestamps,
native resume commands, or inferred native session identifiers. These are
projection fields only; the canonical OCA record schema and IDs remain
unchanged.

## Validation and privacy

Archive validation verifies layout, canonical records, batch manifests,
source/blob digests, and evidence references. Projection is a separate
consumer transformation that runs only after validation; it gathers text,
session associations, occurrences, and citations without replacing validation
or changing its result. Both are read-only with respect to the archive.

The projection contains complete restricted canonical records and can include
private conversation text. Treat the returned object as sensitive. The module
CLI requires an explicit `--output FILE`, writes atomically with mode `0600`,
refuses a destination inside the archive, and does not print record content to
stdout:

```sh
PYTHONPATH=src python -m open_chat_archive.evidence_projection ARCHIVE_ROOT --output /private/path/projection.json
```

The installed command is `open-chat-archive-evidence` with the same arguments.
Do not publish the output or add real archive content to source control.
