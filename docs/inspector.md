# Experimental archive inspector

The inspector is a non-normative, experimental, read-only consumer of v0.1
canonical archives. It is not an importer, a repair tool, or a specification
change.

Run it against an explicit parent directory containing archive directories:

```sh
PYTHONPATH=src python -m open_chat_archive.inspect_site build-all ARCHIVES_PARENT --output OUTPUT_ROOT
open-chat-archive-inspect build ARCHIVE_ROOT --output OUTPUT_DIRECTORY
open-chat-archive-inspect build-all ARCHIVES_PARENT --output OUTPUT_ROOT --source-registry REGISTRY.json
```

`build-all` writes one independent artifact at
`OUTPUT_ROOT/<archive-directory-name>` and a root `index.html`. Local private
runs should use an ignored owner-only directory such as
`gitignored/inspector-sites/`. It validates
each archive with the existing normative validator before loading any canonical
records. A failing archive receives a blocked page containing only its stable
validator code; other valid archives still build, and the command exits
nonzero.

Each valid artifact contains an overview, `records.html`, `coverage.html`, an
`inspection-report.json`, and one opaque-ID chat page per session/revision.
It uses local CSS and JavaScript only, works via `file://`, and remains useful
with JavaScript disabled. Message content is literal text: Markdown, HTML, and
links are never interpreted.

The output is `restricted-unclassified` plaintext. Files are written owner-only
and output directories are owner-only. Those permissions and a git-ignore rule
are not encryption. The inspector refuses output that overlaps an input archive
or archive parent, and will replace an existing output only when it has its own
generation marker. It never copies or independently parses source or blob
payloads; the normative validator remains responsible for its necessary source
integrity checks.

`--source-registry` is optional explicit operator metadata. It is loaded and
validated before any output staging, and a missing, malformed, symlinked, or
output-overlapping registry fails closed. When present, `sources.html`, the
catalog, and chat pages show the raw source scope alongside its literal source
ID, label, account/instance, category, and status. This context is prominently marked **not
canonical evidence**: it neither changes record bytes/identities nor merges
chats. Each generated archive includes only metadata bound to scopes observed
in that archive; unused and other-archive registry entries never enter its
artifact. Source summaries count distinct session entities, session revisions,
unique canonical scope records, offset-aware canonical timestamps, and exact
diagnostic attribution. The source table also exposes distinct affected chats,
unknown-date chat counts, per-code diagnostic occurrence counts, and its
shared occurrence count. A multi-chat diagnostic is deliberately reported as
shared in each involved scope, so per-scope totals are nonadditive. Timestamp
ordering compares parsed offset-aware instants and uses the literal timestamp
as a deterministic tie-breaker.

`inspection-report.json` is a local sensitive report. Its contract includes an
archive identifier/path label, validation status, sorted adapters, batch
metadata, session and record accounting, type counts, projection accounting,
graph/evidence/coverage metrics, diagnostics, and `session_topologies`.
Each topology has its two-ID `session_key`, declared and derived shape, proof
basis, components (with opaque message/revision keys, roots, and active
membership), active node, and active continuation children. Diagnostics always
carry a code, severity, scope, class, related opaque IDs, and any exact
associated session IDs.

Topology is derived only from canonical `parent_child` relationships. All
other relationship kinds remain visible as literal records but cannot create a
root, branch, component, or continuation. Every parent-child relationship that
touches a session message is considered by that session's proof, even when the
other endpoint is foreign, missing, or non-message; such literal records are
not silently reassigned to a chat. A shape is confirmed only for the narrow
OpenWebUI proof: exactly one applicable session/message/parent-edge revision
set, every message's native `parentId`/`childrenIds` metadata, native
reciprocity, valid endpoints, acyclicity, and exact equality with canonical
parent-child edges. Namespace, a declared `thread_shape`, or missing edges by
themselves do not prove completeness. Failed proof is
`incomplete_or_unknown`; competing revisions are `revision_ambiguous`; cycles,
multi-parent edges, and invalid endpoints are `invalid_shape`. Revision
ambiguity deliberately exposes no joined components, roots, leaves, depth,
continuation children, or parent-edge keys; inspect the retained literal
timeline and records to compare revisions. An empty internal projection is
also unknown, never a confirmed linear chat. Observed multiple roots or
disconnected components remain warnings when parent-child evidence exists but
proof is incomplete or invalid. Confirmed multiple components are a `forest`;
a singleton and connected path are `linear`; connected branching is a `tree`.

For confirmed forests the catalog says `forest (N threads)` and the chat page
uses deterministic `Thread A`, `Thread B`, through `Thread AA` labels. Child
display follows complete, distinct `sibling_index` values; otherwise the UI
labels a deterministic source-ordinal/key order as display-only. The current
node is source-selected and can have children. Message cards retain unique
entity-and-revision anchors, and timelines, literal relationships, and evidence
remain available regardless of topology proof.

`diagnostic_counts` sums each diagnostic's occurrence `count`, not merely the
number of diagnostic objects. Collapsible diagnostics are grouped only when
their code, severity, category, and exact associated-session tuple match, so
adapter `diagnostic_total` remains occurrence-based without spreading a merged
global count to every affected adapter. `affected_chat_count` is the distinct
associated session entity count. Findings that cannot be attributed without
guessing appear in `unassigned_archive_diagnostic_counts` instead. A raw
multi-session occurrence remains shared evidence for each named chat/adapter;
the inspector does not invent per-chat fractional occurrences.

Three result classes stay separate:

- Normative validation is the validator's archive-level passed/blocked result.
- Exploratory findings describe useful shapes such as multiple roots or coarse
  evidence; they are not format errors.
- Projection limitations say that the inspector cannot confidently interpret a
  valid shape, such as unordered siblings or an unknown extension shape.

The MVP intentionally does not parse provider exports, render media, make
network requests, perform full-text retrieval, infer chronology from
`source_ordinal`, or choose a preferred revision when canonical revision links
do not establish one.
