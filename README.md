# Open Chat Archive

An Apache-2.0 clean-room Tier 1 format and Python toolkit for preserving exact sensitive chat exports, conforming them into immutable evidence-rich NDJSON batches, validating archive integrity, and safely authoring adapters and Markdown proposals. Both exact sources and conformed raw are `restricted-unclassified` and git-crypt protected in real archives; checked-in examples are synthetic plaintext.

Version 0.1 is an unstable draft. It intentionally excludes dbt, indexing, embeddings, search, retrieval MCP, and UI design.

```sh
python -m pip install -e .
PYTHONPATH=src python -m open_chat_archive.shape_profile opaque-input.json
PYTHONPATH=src python -m open_chat_archive.scaffold_adapter profile.json generated-adapter
PYTHONPATH=src python -m open_chat_archive.import_source --help
PYTHONPATH=src python -m open_chat_archive.inspect_site build-all ARCHIVES_PARENT --output INSPECTOR_OUTPUT
PYTHONPATH=src python -m open_chat_archive.source_registry init ARCHIVES_PARENT --output source-registry.json
PYTHONPATH=src python -m open_chat_archive.inspect_site build-all ARCHIVES_PARENT --output INSPECTOR_OUTPUT --source-registry source-registry.json
PYTHONPATH=src python scripts/validate_repository.py
PYTHONPATH=src python -m unittest discover -s tests -v
```

“Batch validation” checks one batch under an explicit archive root. “Archive validation” also checks the closed layout and all batches. “Repository self-tests” exercise schemas, examples, tooling, and hostile mutations; none is a synonym for another.

See the [draft specification](docs/spec/v0.1/README.md), [clean-room record](docs/clean-room.md), [security controls](docs/spec/v0.1/security.md), and two skills. Deferred: ZIP profiling, a complete Markdown parser, advanced cross-source equivalence, and Tier 2 consumers.

## Experimental evidence projection

`open_chat_archive.evidence_projection.project_archive(path)` validates an OCA
archive and returns the experimental `oca-evidence-v1` consumer projection.
Every canonical envelope is retained with its exact batch occurrences,
inspection-associated session IDs, user-visible text/code, and exact
observation bindings. This Tier 2 view does not change Tier 1 schemas or IDs.

The full projection contains restricted archive content. Write it only to an
explicit local path; the CLI creates a private mode-0600 file and never prints
projection content to stdout:

```sh
PYTHONPATH=src python -m open_chat_archive.evidence_projection ARCHIVE_ROOT --output /private/path/projection.json
```

Archive validation checks integrity and conformance. Projection runs only
after that validation succeeds, then derives search text and citations while
preserving the full canonical records. Neither operation modifies the source
archive. See [evidence projection details](docs/evidence-projection.md).

The initial explicit-format local adapters are [Claude.ai](docs/adapters/claude-ai.md), [Claude Code](docs/adapters/claude-code.md), [OpenWebUI](docs/adapters/open-webui.md), [Codex rollout JSONL](docs/adapters/codex.md), and [Grok Build updates.jsonl](docs/adapters/grok-build.md). Their documented mappings are synthetic baselines and are not claims of authorized real-export compatibility.

The non-normative [experimental archive inspector](docs/inspector.md) builds local, owner-only static artifacts from canonical archives so the format can be reviewed as a consumer would use it. The [MVP plan](docs/inspector-mvp-plan.md) is the design record and does not expand the Tier 1 contract.

An optional, explicit [experimental source registry](docs/source-registry.md) supplies local operator labels for full archive/adapter/scope triples. It is not a canonical schema change or evidence source.

After `oca-gather collect`, use [gathered-store inspection](docs/gather-import.md) to verify the store, import supported objects, and build a local HTML site plus an Obsidian-style Markdown vault. The LLM skill is [inspect-gathered-sessions](skills/inspect-gathered-sessions/SKILL.md). Pi remains collection-only.

## Review a gathered store

`oca-gather` collects sessions. `oca-inspect` is the one-command local review
server. Install the collector from
[houseofpraxis/oca-gather](https://github.com/houseofpraxis/oca-gather).
Download `oca-inspect` for your OS and architecture from
[inspect-v0.1.0-experimental.2](https://github.com/houseofpraxis/open-chat-archive/releases/tag/inspect-v0.1.0-experimental.2),
check it against `checksums.txt` in that release, and put it on `PATH`.

```sh
oca-inspect serve --store "$HOME/oca-gather-store"
```

Open the printed `http://127.0.0.1:8777/` address. The server binds to loopback
only, does not modify the store, and does not claim a canonical import. The
page contains conversation text; do not publish it. Pi sessions are shown as
collection-only. See [inspect-server.md](docs/inspect-server.md).

Checksums in the release detect download mismatch. They are not an independent
publisher signature. The review binary is built from the private collector
source because it reuses the store verifier; this repository publishes the
binary, docs, and the separate Python canonical toolkit.

Canonical import, static HTML, and the Markdown vault remain available. See
[gathered-store inspection](docs/gather-import.md).

See [gathered-store inspection](docs/gather-import.md) and the
[inspect-gathered-sessions](skills/inspect-gathered-sessions/SKILL.md) skill.
Pi remains collection-only. Generated sites and vaults contain conversation
content and must stay local.

```sh
python3 -m pip install -e .
cd toolkit-go && go build -o oca-vault ./cmd/oca-vault
open-chat-archive-gather verify --store "$STORE"
open-chat-archive-gather plan --store "$STORE" --plan "$PLAN"
open-chat-archive-from-gather run \
  --store "$STORE" --plan "$PLAN" --work-root "$WORK" \
  --vault-command "$PWD/oca-vault"
```
