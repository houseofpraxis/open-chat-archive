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

The initial explicit-format local adapters are [Claude.ai](docs/adapters/claude-ai.md), [Claude Code](docs/adapters/claude-code.md), [OpenWebUI](docs/adapters/open-webui.md), [Codex rollout JSONL](docs/adapters/codex.md), and [Grok Build updates.jsonl](docs/adapters/grok-build.md). Their documented mappings are synthetic baselines and are not claims of authorized real-export compatibility.

The non-normative [experimental archive inspector](docs/inspector.md) builds local, owner-only static artifacts from canonical archives so the format can be reviewed as a consumer would use it. The [MVP plan](docs/inspector-mvp-plan.md) is the design record and does not expand the Tier 1 contract.

An optional, explicit [experimental source registry](docs/source-registry.md) supplies local operator labels for full archive/adapter/scope triples. It is not a canonical schema change or evidence source.

After `oca-gather collect`, use [gathered-store inspection](docs/gather-import.md) to verify the store, import supported objects, and build a local HTML site plus an Obsidian-style Markdown vault. The LLM skill is [inspect-gathered-sessions](skills/inspect-gathered-sessions/SKILL.md). Pi remains collection-only.

## Gathered-store inspection

`oca-gather` is a separate collector. Install it from
[houseofpraxis/oca-gather](https://github.com/houseofpraxis/oca-gather).
This repository verifies a gather store, imports supported objects into
canonical archives, and builds a local HTML inspector plus an Obsidian-style
Markdown vault.

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
