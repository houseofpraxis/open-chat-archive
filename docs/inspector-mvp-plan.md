# Experimental archive inspector MVP plan

## Status and boundary

This document proposes a non-normative, local, read-only inspector for Open Chat Archive data. It is an experimental consumer of the Tier 1 format, not part of the v0.1 specification and not evidence that indexing, retrieval, or UI behavior is standardized.

The inspector exists to answer two questions before a larger archive product is designed:

1. Can a person browse useful transcripts and metadata using only conformed archive records?
2. Does attempting that projection expose omissions, ambiguity, or unnecessary complexity in the format and adapters?

The implementation should optimize for traceability and diagnosis rather than presenting a polished replacement chat application.

## Principles

- Read conformed archives rather than parsing provider exports again.
- Validate before projecting and keep normative failures distinct from exploratory warnings.
- Never modify an input archive.
- Distinguish canonical fields, namespaced extensions, and inspector-derived interpretations.
- Never silently discard an unknown record, content part, branch, revision, or extension.
- Make every rendered interpretation traceable to canonical record IDs and field paths.
- Preserve ambiguity instead of inventing ordering, timestamps, model identity, or lineage.
- Keep the implementation dependency-light and usable without a service or frontend build chain.
- Treat every generated page and report as `restricted-unclassified` plaintext.

## Scope

The MVP will provide:

- Discovery of multiple archives and immutable batches under an explicit archive parent.
- Normative archive validation using the existing validator.
- An archive and batch overview.
- A filterable chat catalog.
- Transcript and branch-graph browsing.
- Canonical, extension, identity, revision, relationship, and evidence inspection.
- Archive-wide record and coverage-ledger explorers.
- Exploratory structural diagnostics and a machine-readable inspection report.
- Fully local, pre-rendered static HTML.

The MVP will not provide:

- Importing or repairing source exports.
- Archive mutation, annotation, approval, or proposal workflows.
- Authentication or network deployment.
- Full-text indexing, embeddings, semantic search, reranking, or context packs.
- Markdown or HTML rendering of message text.
- Media playback or arbitrary source/blob previews.
- A stable public UI or plugin API.
- Specification changes based on a single adapter or sample archive.

## Proposed architecture

```text
explicit archives parent
        |
        +-- normative archive validation
        |
        +-- canonical manifest and NDJSON loader
                |
                +-- immutable record index
                +-- reference and provenance joins
                +-- transcript projection
                +-- exploratory analyzers
                        |
                        +-- inspection-report.json
                        +-- pre-rendered HTML pages
```

The projection layer will read manifests and canonical records. The normative validator may read source and blob bytes to verify integrity and evidence ranges, but the site generator will not interpret provider source files or copy them into its output.

### Proposed code organization

```text
src/open_chat_archive/inspection.py       # indexes, joins, projections, diagnostics
src/open_chat_archive/inspect_site.py     # CLI and static rendering
src/open_chat_archive/site_assets/        # tracked CSS and progressive JavaScript
tests/test_inspection.py
tests/test_inspect_site.py
docs/inspector.md                         # operator documentation after implementation
```

The proposed command shape is:

```sh
open-chat-archive-inspect build <archives-parent> --output <site-directory>
```

The default development output should remain under an ignored, owner-only directory such as `gitignored/inspector-site/`.

## Static output design

The recommended MVP is pre-rendered and multipage:

```text
inspector-site/
  index.html
  archives/<opaque-archive-id>/index.html
  archives/<opaque-archive-id>/records.html
  archives/<opaque-archive-id>/coverage.html
  chats/<opaque-session-id>.html
  assets/site.css
  assets/site.js
  inspection-report.json
```

This is preferred over a single-page application for the MVP because it:

- Works directly from `file://` without a server.
- Requires no Node or frontend dependency chain.
- Keeps large transcripts split into bounded pages.
- Allows all untrusted content to be escaped during generation.
- Leaves JavaScript as progressive enhancement for filtering, collapsing, and navigation.

Generated filenames must use opaque archive or record identifiers, never titles, user names, or transcript text.

## Loading and record accounting

The loader will:

1. Discover archive directories under the explicit parent.
2. Run normative archive validation for each archive.
3. Load all manifests and records from valid archives.
4. Index records by `(entity_id, revision_id)`.
5. Retain every batch occurrence of an identical record.
6. Group multiple revisions without silently selecting one as current.
7. Join observations to their exact subject entity and revision.
8. Join relationships to their endpoints.
9. Associate records with sessions when the standard provides a direct or unambiguous transitive reference.
10. Account for records that cannot be associated with a session at archive scope.

A projection must not use lexicographic revision IDs as chronology. When revision records do not establish an order, the UI will display an explicitly unordered set.

## Normative validation versus exploratory analysis

The existing validator remains authoritative for format validity. The inspector must not decompose a single validator success into invented per-rule pass results.

The UI and report will maintain three separate classes:

- **Normative validation:** pass or a stable validator failure code.
- **Exploratory finding:** a quality or shape signal that is not a format violation.
- **Projection limitation:** the inspector could not confidently interpret a valid construct.

If an archive fails normative validation, the recommended default is to include a blocked archive card with the stable failure code, omit transcript projection for that archive, continue processing other valid archives, and return a nonzero build status. Best-effort rendering of invalid records is deferred.

## Archive overview

The landing page will summarize every discovered archive. Each archive page will show:

- Archive, batch, adapter, profile, and specification versions.
- Normative validation status and elapsed time.
- Manifest-declared source, blob, and record integrity metadata.
- Record counts and record-type distribution.
- Roles, visibility values, content types, event kinds, and extension namespaces.
- Optional-field presence and missingness.
- Session size, graph depth, roots, leaves, and branch distributions.
- Lifecycle and projection-status distributions.
- Evidence-coordinate and coverage-ledger summaries.
- Exploratory findings, grouped by diagnostic code and severity.

Aggregates must link to the underlying record or session views whenever practical.

## Chat catalog

The catalog will have one entry per session entity and revision combination, with an explicit revision indicator when more than one revision exists.

Recommended columns and filters:

- Title, with untitled sessions represented explicitly.
- Archive, batch occurrence, adapter, and spec version.
- Entity and revision ID.
- Canonical and extension-derived dates, labeled by origin.
- Models and tags, labeled by origin.
- Message, root, leaf, branch, component, and maximum-depth counts.
- Lifecycle or active-node state.
- Diagnostic count and highest severity.

Client-side filtering may cover title, ID, adapter, model, tag, lifecycle state, branch state, and diagnostics. Global transcript-content search is intentionally deferred.

## Transcript and graph view

### Graph projection

The transcript view will treat explicit relationships as primary. It will:

- Render every root and connected component.
- Retain every branch and inactive path.
- Highlight an adapter-declared active path when it can be mapped unambiguously.
- Offer an active-path-only toggle without making hidden branches the default representation of the archive.
- Mark disconnected components, multiple roots, multiple leaves, and uncertain sibling order.
- Use `source_ordinal` only according to its documented semantics and never silently equate it with time.

Adapter-aware enhancements are allowed only when clearly labeled. For example, an adapter extension may identify an active source node or preserve sibling order that the canonical relationship record does not express. A generic fallback must continue to render the canonical graph when no enhancement exists.

### Message presentation

Each message card will show:

- Role and visibility.
- Source ordinal.
- Literal content parts.
- Canonical and extension-derived model or timestamp metadata.
- Parent and child links.
- Entity and revision IDs.
- Batch occurrences.
- Incoming and outgoing relationships.
- Attached observations and evidence locations.

Message text will be rendered literally with preserved whitespace. Markdown, embedded HTML, and links will not be interpreted in the MVP.

### Metadata inspector

Each chat and record view will separate:

1. Canonical body.
2. Protected identity block.
3. Namespaced extensions.
4. Inspector-derived values and the derivation rule.
5. Evidence bindings and source coordinates.
6. Raw canonical JSON, safely escaped.

This separation is central to evaluating whether frequently needed extension fields should eventually become canonical.

## Record and coverage explorers

The record explorer will support filtering by record type, entity ID, revision ID, archive, and batch occurrence. It will provide links to associated chat pages or archive-level control records.

The coverage explorer will show:

- Profile-path counts.
- Coverage-ledger disposition counts.
- Mapped destinations, preservation targets, and ignored reasons.
- Evidence binding counts by claim basis.
- Coordinate precision, such as whole-artifact ranges versus JSON Pointer or line-level locations.
- Semantic records with multiple observations or no directly displayable evidence.

Large ledgers should have aggregate summaries first and expandable or separately paged details.

## Exploratory diagnostics

Initial diagnostic codes should include:

- `MULTIPLE_SESSION_ROOTS`
- `DISCONNECTED_SESSION_GRAPH`
- `ACTIVE_NODE_NOT_LEAF`
- `ACTIVE_NODE_UNRESOLVED`
- `AMBIGUOUS_SIBLING_ORDER`
- `SOURCE_ORDINAL_GAP`
- `SOURCE_ORDINAL_DUPLICATE`
- `MISSING_CANONICAL_TIMESTAMP`
- `EXTENSION_ONLY_MODEL`
- `COARSE_EVIDENCE_LOCATION`
- `COVERAGE_PRESERVED_ONLY`
- `AMBIGUOUS_REVISION`
- `UNASSOCIATED_RECORD`
- `UNRENDERED_CONTENT_TYPE`
- `UNKNOWN_EXTENSION_SHAPE`

Diagnostics must identify whether they concern the standard, an adapter-specific projection, or inspector support. They must not be described as normative errors unless the existing validator reports them as such.

## Machine-readable inspection report

`inspection-report.json` will contain:

- Inspector and supported-spec versions.
- Archives and batches examined.
- Normative validation outcomes and timing.
- Record counts and complete accounting totals.
- Field-presence and extension-path distributions.
- Session graph and transcript projection metrics.
- Evidence and coverage metrics.
- Structured diagnostics with severity, scope, and related opaque IDs.
- Counts of records rendered, associated, unsupported, or blocked.

The HTML dashboard should consume the same logical report model rather than maintaining independent calculations.

Runtime fields such as generation time and validation duration may vary. Tests should normalize those fields rather than weakening deterministic record projections.

## Security and privacy

The generated site is a sensitive plaintext derivative even though it omits original source bytes. The implementation will:

- Require an explicit input and output location.
- Refuse to write inside an archive root.
- Create output directories owner-only and files non-world-readable.
- Avoid CDNs, analytics, remote fonts, remote scripts, and all other network requests.
- Copy no source or blob bytes into the output.
- Escape transcript text, metadata, JSON, titles, attributes, and diagnostic detail.
- Treat strings containing HTML, `</script>`, bidirectional controls, malformed Unicode, and extreme lengths as hostile input.
- Use external tracked CSS and JavaScript with a restrictive Content Security Policy compatible with local static use.
- Show a persistent `restricted-unclassified` warning.
- Document that Git ignore and file permissions are not encryption.

A redacted or shareable report mode is not part of the MVP.

## Implementation stages

### Stage 1: inspection model

- Implement multi-archive and multi-batch loading.
- Build immutable indexes and reference joins.
- Preserve duplicate occurrences and unordered revisions.
- Add complete record-accounting assertions.

### Stage 2: analyzers

- Add graph topology analysis.
- Add field and extension utilization profiles.
- Add evidence precision and coverage analysis.
- Define structured diagnostic codes and severity.

### Stage 3: static rendering

- Render landing and archive dashboards.
- Render the chat catalog and transcript graph pages.
- Render record and coverage explorers.
- Emit the machine-readable report.

### Stage 4: progressive interaction

- Add local filtering and sorting.
- Add collapsible metadata and raw JSON.
- Add deep links, copyable IDs, keyboard navigation, and accessible status labels.

### Stage 5: verification

- Exercise checked-in synthetic minimal and rich archives.
- Add conspicuously synthetic branching, multiple-root, revision, and unsupported-extension fixtures.
- Run the inspector against authorized ignored archives locally.
- Record format findings separately before proposing schema changes.

Testing more than one adapter is an evaluation milestone, but importing additional private exports is separate from the inspector implementation.

## Test strategy

Tests will cover:

- Multiple archives and batches.
- Exact duplicate records across batches.
- Multiple revisions without an established chronology.
- Linear, branching, multiple-root, and disconnected graphs.
- Active-node mapping and unresolved adapter hints.
- Unknown record, extension, role, visibility, event, and content shapes.
- Observation and relationship joins.
- Complete record accounting.
- Stable diagnostics on synthetic fixtures.
- HTML and attribute injection attempts.
- Literal rendering of Markdown and HTML-like message text.
- Owner-only output permissions.
- Refusal to write into an archive root.
- Absence of source/blob payload copies and remote asset references.
- Static operation from `file://` with JavaScript disabled and enabled.

Real ignored archives may be used for local acceptance checks but must never become fixtures, snapshots, logs, or committed scalar fingerprints.

## MVP acceptance criteria

The MVP is complete when:

- Every valid discovered archive and batch is represented.
- Every canonical record is counted as rendered, associated, unsupported, or blocked; there are no silent drops.
- Every session can be opened from the catalog.
- Every message and explicit branch can be inspected.
- Canonical, extension, and derived values are visibly distinct.
- Multiple roots, disconnected components, uncertain ordering, and revision ambiguity remain visible.
- Normative validation status is not conflated with exploratory diagnostics.
- Evidence and coverage precision can be evaluated from the UI.
- The site works without a backend, build server, or network access.
- No original source or blob payload is copied into the site.
- No private archive data is added to Git.
- The resulting report supports a concrete follow-up list of format strengths, gaps, and ambiguities.

## Review defaults

Unless changed during review, implementation should proceed with these defaults:

1. Pre-rendered multipage HTML rather than an SPA.
2. Literal message text rather than Markdown rendering.
3. Full graph visibility with an emphasized active path.
4. Adapter-aware projection only when explicitly labeled and backed by canonical extensions.
5. Valid archives rendered; invalid archives shown as blocked; nonzero build status if any archive is invalid.
6. Raw canonical JSON available in collapsed local views.
7. Generated site and report treated as fully sensitive, with no redacted mode.
