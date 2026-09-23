# Gathered-session inspection workflow

## Boundary

Do not read, paste, summarize, or attach:

- gather-store receipts, objects, or `origin_path` values
- operator plan or ledger files
- canonical archives, source blobs, or validation attestations
- inspector HTML, `inspection-report.json`, or Markdown vault notes
- paths, native IDs, SHA-256 digests, account labels, or chat text

Default command output is one aggregate JSON object. If a command prints a path, ID, digest, snippet, or raw OS error, stop and treat that as a defect. `--details-output` writes a mode `0600` local file; never open it for the model.

Pi receipts are collection-only. Report `IMPORTER_UNAVAILABLE`. Do not relabel Pi bytes as another adapter and do not add a Pi importer.

## Preconditions

The operator already ran `oca-gather collect` into an owner-only store. Install the public toolkit from `houseofpraxis/open-chat-archive` and build or install `oca-vault` for the local OS/architecture. Both tools stay offline. Do not run the source client.

Use absolute paths chosen by the operator. Do not discover a home directory or guess a store location.

## 1. Verify

```sh
open-chat-archive-gather verify --store "$STORE"
```

Success is exit 0 with `complete: true` and `code: OK`. Exit 4 means the store is untrusted or corrupt; preserve it and stop. Exit 6 means another process holds the store. Exit 8 means the scan hit a bound.

## 2. Plan

```sh
open-chat-archive-gather plan --store "$STORE" --plan "$PLAN"
```

`$PLAN` must be outside the store. The command creates a mode `0600` file. The operator edits it locally to assign `source_scope_id` for every `scope_state: missing` row. Never infer a scope from a path, digest, username, native ID, or file contents. A receipt that already has an operator-supplied scope may only keep that scope.

## 3. Apply

```sh
open-chat-archive-gather apply \
  --store "$STORE" \
  --plan "$PLAN" \
  --archives-parent "$ARCHIVES"
```

Apply imports only verified object bytes for `claude-code`, `codex`, and `grok-build`. It does not open `origin_path`. The plan is an allowlist: every selected receipt must match the plan row, including literal origin text. Missing, extra, or disagreeing rows are `SCOPE_PLAN_CONFLICT`.

Rerun outcomes:

- unchanged: same receipt, scope, and bytes already validate
- added: a new receipt becomes one new batch
- conflict: scope, bytes, or archive state disagree; nothing is overwritten

Exit 0 is the only complete success. Pi or missing scope makes the result incomplete. Do not continue to rendering after a nonzero or `complete: false` result.

## 4. Render

After a complete apply, one pipeline command validates, writes attestations, builds the local HTML inspector, and runs `oca-vault`:

```sh
open-chat-archive-from-gather run \
  --store "$STORE" \
  --plan "$PLAN" \
  --work-root "$WORK" \
  --vault-command "$VAULT"
```

`$VAULT` is the absolute `oca-vault` executable. The pipeline passes `build --archive --attestation --output` itself. Do not add a shell string.

`$WORK` is a dedicated owner-only directory. It will contain `archives/`, `attestations/`, `inspector/`, and `vault/`. These are sensitive derivatives. Tell the operator the local paths only if they asked where to open the result, and do not read the generated files.

## Reading results

| Code | Meaning |
| --- | --- |
| `OK` | Completed within bounds |
| `IMPORTER_UNAVAILABLE` | Pi or another receipt has no canonical importer |
| `SCOPE_MISSING` | A receipt still needs an operator scope |
| `SCOPE_PLAN_CONFLICT` | Plan does not exactly match the selected receipts |
| `IMPORT_CONFLICT` | Existing archive/ledger disagrees; not overwritten |
| `STORE_UNTRUSTED` / `STORE_OBJECT_CORRUPT` | Preserve the store; do not repair it |
| `STORE_BUSY` | Retry after the other writer exits |
| `PIPELINE_RENDER_UNAVAILABLE` | Import did not reach a complete vault render |

HTML and Markdown are review aids. They are not evidence that every source byte was semantically interpreted, and they are not safe to publish.
