#!/usr/bin/env python3
"""Operational pre-commit gate: inspect protected staged blobs without printing data."""
from __future__ import annotations
import re, subprocess, sys

LEGAL=re.compile(r"^archives/[A-Za-z0-9._-]+/(?:sources/[A-Za-z0-9._-]+|blobs/[A-Za-z0-9._-]+|batches/[A-Za-z0-9._-]+/(?:manifest\.json|records\.ndjson)|import-manifests/[A-Za-z0-9._-]+\.json)$")

def is_git_crypt_ciphertext(blob: bytes) -> bool:
    return blob.startswith(b"\x00GITCRYPT\x00")

def main() -> int:
    names=subprocess.run(["git","diff","--cached","--name-only","--diff-filter=ACMR"],capture_output=True,text=True,check=True).stdout.splitlines()
    for name in names:
        if not name.startswith("archives/"): continue
        if not LEGAL.fullmatch(name): print("staged ciphertext gate failed: ARCHIVE_PATH_ALLOWLIST"); return 1
        attrs=subprocess.run(["git","check-attr","filter","--",name],capture_output=True,text=True,check=True).stdout
        if not attrs.rstrip().endswith(": git-crypt"): print("staged ciphertext gate failed: GIT_ATTR_EFFECTIVE"); return 1
        blob=subprocess.run(["git","show",f":{name}"],capture_output=True,check=True).stdout
        if not is_git_crypt_ciphertext(blob):
            print("staged ciphertext gate failed: PLAINTEXT_STAGED"); return 1
    print("staged ciphertext gate passed"); return 0
if __name__ == "__main__": raise SystemExit(main())
