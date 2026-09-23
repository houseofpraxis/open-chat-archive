---
name: conform-markdown-transcript
description: Preserve exact Markdown bytes and produce evidence-bound parsed facts and approval-gated proposals.
---

# Conform a Markdown transcript

Preserve exact Markdown bytes as the authoritative source artifact, including newline style and final-newline state. Parsing creates field-level `parsed` observations with byte/line evidence; it never replaces the source. Any normalized display Markdown is a separate artifact with algorithm/version and input evidence.

Ambiguous speakers, metadata, dates, frontmatter, and model suggestions become proposal records with alternatives/status/evidence. Pending, rejected, or unresolved model inference must not enter canonical bodies. Only individually approved proposals may support canonical `model_inferred` bindings, with approval basis/time and append-only semantic revisions. See [evidence contract](references/evidence-contract.md).
