# Clean-room provenance

Before this repository was created, the orchestrator reviewed MyChatArchive's public README, advertised feature set, AGPL license, and repository file-level structure. It did not inspect or copy implementation bodies, tests, schemas, fixtures, or internal algorithms. This is the complete known review boundary; it does not claim that no public prose was inspected.

This repository's implementation, schemas, tests, and wholly synthetic fixtures were independently produced from the user-authored requirements, public standards, and general engineering techniques. No further inspection of an external chat-archive implementation is needed or authorized for this project.

For the Codex rollout adapter, the clean-room format boundary additionally permits review of the pinned read-only Codex CLI Rust type declarations at tag `rust-v0.147.0`, commit `be6e8eac029b183056b7e4402879f15d2c85f61b`, limited to interoperability facts in `codex-rs/protocol/src/protocol.rs`, `models.rs`, and narrowly relevant projection tests. No implementation code was copied. This establishes a bounded vocabulary only; it is not local-transcript validation or a claim of exhaustive compatibility.

For the Grok Build adapter, the clean-room format boundary additionally permits review of the pinned public `xai-org/grok-build` type declarations at commit `72a61251fcffb464bcc687aeb5a998e5a98ec0c9` and `agent-client-protocol-schema` `0.10.4`, limited to interoperability facts for `updates.jsonl` envelopes, ACP `SessionNotification`/`SessionUpdate`, and the xAI session-update rail. No implementation code was copied. This establishes a bounded vocabulary only; it is not local-transcript validation or a claim of exhaustive compatibility.
