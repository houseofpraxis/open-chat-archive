# Grok Build updates.jsonl adapter

The explicit `grok-build` adapter imports one regular, non-symlink JSONL file in the Grok Build `updates.jsonl` ACP session-update container. It does not scan directories, sibling session files, or `chat_history.jsonl`. Official session restore treats `updates.jsonl` as the authoritative conversation log; `chat_history.jsonl` is the model-protocol view and is out of scope for this adapter.

The implementation vocabulary was reviewed clean-room from:

- the installed local CLI `grok 1.0.13 (5e9a58528b76)`
- the public `xai-org/grok-build` snapshot at commit `72a61251fcffb464bcc687aeb5a998e5a98ec0c9` (`SOURCE_REV` `a549186d9d39311f2d3ee4208db62af8c65aa476`), whose published `xai-grok-shell` crate version is `1.0.16`
- pinned `agent-client-protocol-schema` `0.10.4`

No upstream implementation code was copied, and this document does not imply xAI endorsement. The public GitHub tree does not publish the local `5e9a58528b76` monorepo revision as a tag; compatibility evidence is therefore bounded to the pinned public type declarations, wholly synthetic tests, and a later independent no-disclosure local check when one is reported.

Each line is a disk `SessionUpdateEnvelope` (`timestamp` as a nonnegative integer, `method`, `params`) or a legacy raw ACP `SessionNotification`. Supported methods are `session/update` and `_x.ai/session/update`. `params.sessionId` (or `session_id`) must be a nonempty string and must be identical across the file. `params.update.sessionUpdate` must be a nonempty string. Unknown methods, conflicting session IDs, malformed mapped records, and non-integer timestamps are rejected with fixed codes. Unknown or unmapped `sessionUpdate` tags remain exactly preserved in the source artifact; the xAI rail's catch-all `Unknown` variant is treated the same way.

Mapped ACP updates are `user_message_chunk`, `agent_message_chunk`, `agent_thought_chunk`, `tool_call`, and `tool_call_update`. Plain `text` content blocks become canonical messages. Thought chunks are assistant `exported_reasoning`. Image, audio, and other content remain source-only. Native identities:

- session: `sessionId`
- message/thought: `_meta.eventId` when a nonempty string
- tool call/result: `toolCallId`
- turn: `turn_completed.prompt_id`

Missing native IDs use explicitly marked artifact-local, same-artifact-only fallbacks. `source_ordinal` is the zero-based JSONL line index. Streamed chunks are not concatenated; each mapped chunk is its own message so identities do not invent utterance boundaries.

`tool_call` maps to `tool_invocation` using the required title as `tool_name` and `org.open-chat-archive.grok-build.tools` as `tool_namespace`. Begin/pending statuses never claim success. `tool_call_update` maps a `tool_result` only for `completed` or `failed` with canonical-safe output, or a failed result with `{code: failed}` when output is absent. xAI `turn_completed` supplies turns, lifecycle status from the stop reason, and integer usage when present. `response_completed` integer usage maps when present. Auto-compact and rewind markers map to lifecycle events. Model provider is not invented, so `model_response` is omitted unless a future source field names both provider and model.

The complete source bytes are retained as the source artifact, and every emitted semantic body/extension is observed against the whole byte range. Source JSON may contain finite decimals in cost metadata; those tokens stay in the source-only parser and never enter canonical records. Structural profiling uses a Grok-specific 3,000,000-node ceiling because streamed ACP chunks are dense; the generic writer default remains 10,000. Profiling runs before a new source object is stored. Sources larger than the shared 64 MiB regular-file bound fail closed with `INPUT_KIND_OR_SIZE`.

Recognition uses only shape-only-v1 aliases, types, and cardinalities, and is only a conservative aid to explicit format selection. This adapter claims only the pinned declarations, synthetic regression evidence, and a later independent no-disclosure local check when one is reported. It is not a claim of exhaustive historical or future Grok Build session support.
