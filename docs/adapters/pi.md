# Pi Coding Agent v3 session JSONL

The explicit `pi-v3` adapter accepts one Pi session JSONL file. It requires a
version 3 session header, unique entry IDs, strict UTF-8 JSON on every line,
and a terminating newline. Symlinks, nonregular files, malformed JSON, and
partial final lines are rejected. Import the file with `open_chat_archive.import_source`;
the adapter does not scan session directories or resume a session.

The full byte-for-byte file is retained as the source artifact. The header is
evidenced at line 1, and each entry uses its physical JSONL line and exact byte
range. Canonical message identities combine the native session ID and entry ID
within the configured source scope. Header metadata, entry timestamps, explicit
`parentId` values, unsupported content blocks, and unknown future entry types
remain available in namespaced metadata or the exact source. Finite JSON decimal
numbers are parsed losslessly for source handling and are never copied into
canonical records as JSON numbers.

User and assistant text parts are mapped to user-visible canonical messages.
Thinking parts become separate `exported_reasoning` messages, so they are not
included in text projection. System messages are control traffic; tool results
and tool calls are tool traffic/events. Image data and unsupported message
roles/content are preserved in the source but not interpreted as prose. A
canonical `parent_child` relationship is emitted only for a direct parent edge
between two mapped message entries when each endpoint maps unambiguously to one
message. The explicit native tree remains preserved in source metadata in all
cases. A session title is set only from an explicit `session_info.name`.

The mapping is based on Pi Coding Agent 1.5.0 public type declarations. It is a
bounded interoperability implementation, not a claim to understand extension
payloads or every future Pi variant.
