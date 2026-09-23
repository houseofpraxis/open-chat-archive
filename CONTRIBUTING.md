# Contributing

Contributions must remain original and Apache-2.0 compatible. Do not submit real exports, transcripts, identifiers, secrets, derived scalar hashes, or fixtures copied from a product. Use conspicuously synthetic data only. Record design sources and independently derived reasoning when changing contracts.

Keep Tier 1 changes separate from downstream indexing or retrieval. Schema changes require matching prose, examples, tests, and a recorded decision. Never weaken the default `restricted-unclassified` policy or fail-closed adapter behavior silently.

Run:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 scripts/validate_repository.py
PYTHONPATH=src python3 -m open_chat_archive.shape_profile --help
PYTHONPATH=src python3 -m open_chat_archive.scaffold_adapter --help
git diff --check
```

Security reports should avoid including source values or raw failing inputs. See [SECURITY.md](docs/SECURITY.md).
