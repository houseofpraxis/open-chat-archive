# Markdown evidence contract

Exact bytes are authoritative. Line ranges are one-based inclusive; byte ranges are zero-based half-open and must be ordered/in bounds. Bind parsed facts to precise source artifact locations. Ancestor bindings cover descendants; mixed parsed/user/context evidence uses separate bindings.

Proposal records contain an exact subject entity/revision, field pointer, proposed protected JSON value, basis, status, and source evidence. Approved/rejected status requires matching approval status, approver basis, and time. Model-inferred canonical evidence must link an approved model-inference proposal for the same subject revision/field. Normalization is a derived artifact, never source evidence.

A complete parser is deferred. Implementations should test CRLF/LF, absent final newline, Unicode, code-fence delimiters, repeated headings, ambiguous labels, and frontmatter-like text conservatively.
