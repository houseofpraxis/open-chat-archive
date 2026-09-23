# Inspect a gathered store

`oca-gather` copies exact local session bytes into an owner-only store. This
toolkit is the later, separate step: verify that store, import supported
objects into validated canonical archives, and build local inspection
artifacts.

```text
oca-gather store
    → operator scope plan
    → canonical archive batches
    → validation attestation
    → local HTML inspector
    → Obsidian-style Markdown vault
```

Collection is not import. Every gather receipt keeps
`import_status: not_attempted`. Pi can be collected but has no canonical
importer.

## What you need

- Python 3.10 or newer
- an `oca-gather` store produced by v0.1-experimental
- `oca-vault` built for the same OS and architecture

The inspection tools do not use the network, execute a chat client, or modify
the gather store.

## Install

From a checkout:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
cd toolkit-go
go build -o "$HOME/.local/bin/oca-vault" ./cmd/oca-vault
```

Release archives, when published, are a separate supply-chain surface.
Checksums shipped beside those archives detect mismatch only; they do not
authenticate the publisher.

## Quick path

```sh
STORE="$HOME/oca-gather-store"
PLAN="$HOME/oca-plan.json"
WORK="$HOME/oca-inspect-work"

open-chat-archive-gather verify --store "$STORE"
open-chat-archive-gather plan --store "$STORE" --plan "$PLAN"
# Edit $PLAN locally. Assign source_scope_id for every missing-scope row.
open-chat-archive-from-gather run \
  --store "$STORE" \
  --plan "$PLAN" \
  --work-root "$WORK" \
  --vault-command "$HOME/.local/bin/oca-vault"
```

Open `$WORK/inspector/index.html` and `$WORK/vault/` locally. Both contain
conversation content. Do not commit, upload, or paste them into an issue or
model context.

`run` stops unless apply is complete. Pi receipts and missing scopes are
incomplete results, not success.

## Scope plan

A missing gather scope is not an account. The plan file is mode `0600` and
maps each receipt to an operator-chosen `source_scope_id`. Do not derive that
value from a path, username, native ID, or digest. If a receipt already
carries an operator-supplied scope, the plan must repeat it exactly.

The plan is an allowlist. Apply compares receipt ID, adapter, scope state,
literal origin text, and assigned scope. Disagreement is
`SCOPE_PLAN_CONFLICT` and imports nothing from that request.

## Identities and reruns

- Archive ID is SHA-256 over `oca-bridge-archive-id/v1`, the adapter, and the
  operator scope, separated by NUL bytes.
- Batch ID is the receipt ID without its `sha256:` prefix.
- The same adapter and scope share an archive. Each receipt is one batch.
- A repeat of the same receipt, scope, and bytes is unchanged.
- A changed scope or mutated batch is a conflict. Existing batches are not
  overwritten.

## Vault

`oca-vault` renders one validated archive. It does not parse vendor JSONL and
does not replace Python validation. Frontmatter is one JSON object between
`---` lines. Note names and wikilinks use canonical digests, so a second run
replaces the same generated notes instead of duplicating them.

The vault is a dedicated generated tree. A sibling note outside that tree is
left alone. Do not point `--output` at an existing Obsidian vault root.

## Privacy

Default stdout and stderr are aggregate JSON and fixed codes. They omit
paths, IDs, digests, snippets, and raw OS errors. `--details-output` is an
explicit local opt-in.

Stores, plans, archives, attestations, HTML, and vaults are sensitive
plaintext. Owner-only modes reduce accidental disclosure; they are not
encryption or authentication.
