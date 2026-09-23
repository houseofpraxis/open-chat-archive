---
name: analyze-chat-export
description: Safely profile a local JSON/JSONL chat export and scaffold a Tier 1 adapter without disclosing raw values or keys.
---

# Analyze a chat export

Keep raw input local/model-forbidden. Run `shape_profile` with positive limits and an opaque local filename. Share only its `shape-only-v1` output and synthetic fixtures. If a protected key map is explicitly requested, keep its mode 0600 file local and never attach it to model context.

Use `scaffold_adapter` on the safe profile. Models may use the dossier, synthetic fixtures, and locally approved semantic alias labels, never raw key names. Complete mappings locally, run the verifier under operator-provided no-network/resource isolation, validate output, reconcile every profile path, and canary-audit all model-visible artifacts and diagnostics. See [workflow](references/workflow.md) and [output contract](references/output-contract.md).
