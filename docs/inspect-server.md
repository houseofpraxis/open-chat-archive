# Local review server

`oca-inspect` reads an `oca-gather` store and serves a localhost-only HTML
review. It does not import, validate, upload, or modify the store.

```sh
oca-inspect serve --store "$HOME/oca-gather-store"
```

Open the printed `http://127.0.0.1:8777/` address. The page lists collected
sessions and shows extracted text. It is a review display, not a canonical
archive. Pi sessions are included as collection-only.

Non-loopback addresses are refused. Stdout is one JSON object with the listen
address and a session count. It does not print store paths. The HTML itself
contains conversation text and origin paths; do not publish or share it.

Stop the server with Ctrl+C. Uninstall is removing the `oca-inspect` binary.
The gather store is left untouched.

Release assets are `oca-inspect_linux_amd64.tar.gz`,
`oca-inspect_linux_arm64.tar.gz`, `oca-inspect_darwin_amd64.tar.gz`,
`oca-inspect_darwin_arm64.tar.gz`, and `checksums.txt`. Each archive contains
`oca-inspect`, `LICENSE`, and `NOTICE`.
