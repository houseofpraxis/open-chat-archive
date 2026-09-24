# Local review server

`oca-inspect` reads an `oca-gather` store and serves a localhost-only HTML
review. It does not import, validate, upload, or modify the store.

```sh
oca-inspect serve --store "$HOME/oca-gather-store"
```

Open the printed `http://127.0.0.1:8777/` address. The page lists collected
sessions and shows extracted text. It is a review display, not a canonical
archive. Pi sessions are included as collection-only.

## Home page

The index supports server-side search, filters, and sorting without any
JavaScript:

- **Search** matches session titles and message text (case-insensitive).
- **Filters** narrow by adapter and by source scope.
- **Sort** by newest, oldest, title, most turns, most tokens, or largest.

## Session page

Each session shows literal metadata recorded by the source client:

- recorded start and end timestamps
- models observed in the session
- token usage — summed per-message usage, or the last cumulative count the
  client reported; absent when the source recorded none
- native session ID, working directory, and client version when present
- collected line/byte counts and the object digest
- origin path and scope

User and assistant text turns are shown by default. Tool calls, tool
results, and reasoning turns are hidden behind a `show N tool/reasoning
turns` toggle (`?tools=1`).

## Copy and export

- `plain-text transcript` serves `/s/<id>/transcript.txt`: labeled
  `[User]` / `[Assistant]` turns you can select-copy or `curl` into another
  chat as context. Add `?tools=1` to include tool and reasoning turns.
- `export Markdown` serves `/s/<id>/export.md` as a download with
  Obsidian-compatible YAML frontmatter (title, adapter, origin, scope,
  recorded times, models, token counts, receipt/object digests, tags).
  Frontmatter strings are JSON-quoted, so hostile titles cannot escape the
  block; tool output is fenced with fences longer than any backtick run.

Both exports state that they are review displays, not canonical imports.

## Boundaries

Non-loopback addresses are refused. Stdout is one JSON object with the listen
address and a session count. It does not print store paths. The HTML,
transcript, and Markdown all contain conversation text and origin paths; do
not publish or share them.

Stop the server with Ctrl+C. Uninstall is removing the `oca-inspect` binary.
The gather store is left untouched.
