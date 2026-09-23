# OpenWebUI forest compatibility decision

## Reviewed decision

OpenWebUI adapter 0.1.3 recognizes a forest only after its complete source graph has passed the existing reciprocal parent/child, endpoint, and acyclicity checks. A graph with multiple explicit roots, including a set of isolated nodes, is emitted as canonical `thread_shape: unknown` plus `extensions[org.open-chat-archive.open-webui].thread_shape: forest`. This preserves the v0.1 wire and schema contract: `forest` is an adapter-specific interpretation, not a new canonical enum or a v0.2 feature.

Connected nonbranching graphs, including a singleton, remain canonical `linear`; one connected branching graph remains `tree`. The adapter version is 0.1.3. Its manifest metadata changes with that version, while message and relationship entity identities and semantics do not change. Session and lifecycle changes are semantic revisions of the same entities.

`history.currentId` selects the active source node. It may have children, and does not mean a leaf, final response, or latest timestamp. The lifecycle record intentionally retains its established `:active-leaf` logical-key suffix for entity stability while its detail is `active_node`.

## UI boundary

This decision makes no claim that a canonical v0.1 `forest` exists. Consumers may present the namespaced value as an OpenWebUI-specific source topology only when they understand that extension; generic v0.1 consumers must continue to treat the canonical shape as `unknown`. Presentation must retain every message and literal parent/child relationship, and must not discard branches because of the active node.
