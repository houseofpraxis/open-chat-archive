# Experimental source registry

The source registry is an optional local sidecar for operator-maintained
metadata. It is version `0.1-experimental`, not part of normative v0.1, not
parsed evidence, and not account authentication. It never changes canonical
records, entity IDs, revision IDs, batches, or imports.

Safe registry reads and writes require POSIX descriptor-relative directory
operations with no-follow flags. On platforms that do not provide those
primitives, registry `init` and `validate` fail closed with
`REGISTRY_PLATFORM_UNSUPPORTED`; ordinary validation and inspector operation
without `--source-registry` remain available. There is no pathname-based
fallback.

Create a neutral placeholder file only after validating every immediate archive:

```sh
open-chat-archive-sources init ARCHIVES_PARENT --output source-registry.json
open-chat-archive-sources validate source-registry.json
```

The initializer refuses existing files (including empty or symlinked curated
files), input overlap, and symlinked archive inputs. It writes a new owner-only
file atomically. If initial directory durability confirmation fails after a
successful no-replace publication, it reports
`REGISTRY_PUBLISHED_DURABILITY_UNCONFIRMED`: do not rerun blindly; inspect the
published path and recover or retain it deliberately. Earlier write failures
clean their temporary file and leave the requested output absent. It creates a
separate opaque source ID for every observed
`(archive_id, adapter_namespace, source_scope_id)` tuple and marks it
`unverified`; it does not infer accounts, aliases, or confirmations.

For MVP, edit the registry manually, then run `validate`. A source definition
has a safe opaque ID, namespace, literal display label, and status
`unverified`, `ambiguous`, or `operator_confirmed`. Confirmation requires both
an account and instance identifier, but remains an operator assertion rather
than authenticated proof. Bindings are unique full triples; a deliberate alias
may point multiple triples at one definition but never merges records. Keep
this sensitive sidecar outside archive roots and inspector outputs.

Text fields are protected literal display text and are safely escaped by the
inspector. Do not add credentials or secret notes: the fixed schema rejects
unknown fields. Registry completeness is manual: `unverified` means no
operator confirmation, `ambiguous` means the operator cannot make a unique
assertion, and `operator_confirmed` is still only manually supplied metadata.
The sidecar is an immutable boundary for import data: editing or renaming a
registry entry does not rewrite canonical archives, identities, records, or
batches. The registry is never a fetch list: neither the initializer nor the
inspector opens source URLs or contacts a provider. See the synthetic example
for the exact JSON shape.
