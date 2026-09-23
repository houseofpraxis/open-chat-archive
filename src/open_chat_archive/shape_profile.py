"""Local shape-only-v1 profiling: no raw object keys, values, or value hashes."""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from .strictjson import SourceJSONNumber, StrictJSONError, load_ndjson, loads_bytes


class ProfileError(Exception): pass


def _kind(value: Any) -> str:
    if value is None: return "null"
    if isinstance(value, bool): return "boolean"
    if isinstance(value, str): return "string"
    if isinstance(value, SourceJSONNumber): return "number"
    if isinstance(value, int): return "integer"
    if isinstance(value, dict): return "object"
    if isinstance(value, list): return "array"
    raise ProfileError("unsupported scalar type")


def _bucket(count: int) -> str:
    if count == 0: return "0"
    if count == 1: return "1"
    if count <= 3: return "2-3"
    if count <= 7: return "4-7"
    if count <= 15: return "8-15"
    return "16+"


def profile_value(value: Any, *, max_depth: int = 20, max_items: int = 10000,
                  max_keys: int = 1000, key_map: dict[str, list[str]] | None = None) -> dict[str, Any]:
    for limit in (max_depth, max_items, max_keys):
        if not isinstance(limit, int) or limit < 1: raise ProfileError("invalid resource limit")
    nodes = 0
    paths: dict[str, Counter[str]] = {}
    cardinalities: dict[str, Counter[str]] = {}

    def visit(item: Any, path: str, raw_path: str, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > max_items: raise ProfileError("structural node limit exceeded")
        if depth > max_depth: raise ProfileError("recursion limit exceeded")
        kind = _kind(item); paths.setdefault(path, Counter())[kind] += 1
        if isinstance(item, dict):
            if len(item) > max_keys: raise ProfileError("object key limit exceeded")
            cardinalities.setdefault(path, Counter())[_bucket(len(item))] += 1
            # Alias is coordinate-local and ordinal; it is not derived from key bytes.
            for index, key in enumerate(sorted(item), 1):
                alias = f"k{index:04d}"
                alias_path = path + "/" + alias
                child_raw = raw_path + "/" + key.replace("~", "~0").replace("/", "~1")
                if key_map is not None: key_map.setdefault(alias_path, []).append(child_raw)
                visit(item[key], alias_path, child_raw, depth + 1)
        elif isinstance(item, list):
            cardinalities.setdefault(path, Counter())[_bucket(len(item))] += 1
            for child in item: visit(child, path + "/*", raw_path + "/*", depth + 1)

    visit(value, "", "", 0)
    return {"profile_version":"shape-only-v1","disclosure_policy":{
        "allowed":["opaque-alias-topology","json-types","bounded-cardinality-buckets","structural-coordinates"],
        "forbidden":["raw-values","raw-object-keys","value-or-key-hashes"]},
        "paths":[{"path": p or "/", "types":dict(sorted(c.items())), **({"cardinality_buckets":dict(sorted(cardinalities[p].items()))} if p in cardinalities else {})} for p,c in sorted(paths.items())],
        "node_count_bucket":_bucket(nodes)}


def load_profile(path: Path, input_format: str, *, max_bytes: int, max_depth: int,
                 max_items: int, max_keys: int, key_map_path: Path | None = None) -> dict[str, Any]:
    for limit in (max_bytes, max_depth, max_items, max_keys):
        if not isinstance(limit, int) or limit < 1: raise ProfileError("invalid resource limit")
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode): raise ProfileError("input kind rejected")
            if info.st_size > max_bytes: raise ProfileError("input byte limit exceeded")
            raw = handle.read(max_bytes + 1)
        if len(raw) > max_bytes: raise ProfileError("input byte limit exceeded")
        value = loads_bytes(raw) if input_format == "json" else load_ndjson(raw)
    except ProfileError: raise
    except (OSError, StrictJSONError): raise ProfileError("input structure rejected") from None
    mapping: dict[str, list[str]] | None = {} if key_map_path else None
    result = profile_value(value, max_depth=max_depth, max_items=max_items, max_keys=max_keys, key_map=mapping)
    if key_map_path:
        if key_map_path.exists() or key_map_path.is_symlink(): raise ProfileError("key map target must not exist")
        payload = {"classification":"restricted-model-forbidden","profile_version":"shape-only-v1","aliases":mapping}
        fd = os.open(key_map_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle: json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":")); handle.write("\n")
    return result


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Create a shape-only-v1 profile; input paths and values are never echoed.")
    p.add_argument("input", type=Path, metavar="OPAQUE_INPUT")
    p.add_argument("--format", choices=("json","jsonl"), default="json")
    p.add_argument("--max-bytes", type=int, default=64*1024*1024); p.add_argument("--max-depth", type=int, default=20)
    p.add_argument("--max-items", type=int, default=10000); p.add_argument("--max-keys", type=int, default=1000)
    p.add_argument("--max-output-bytes", type=int, default=1024*1024)
    p.add_argument("--protected-key-map", type=Path, metavar="NEW_FILE", help="optional restricted/model-forbidden local alias map (mode 0600)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.max_output_bytes < 1: raise ProfileError("invalid resource limit")
        result = load_profile(args.input,args.format,max_bytes=args.max_bytes,max_depth=args.max_depth,max_items=args.max_items,max_keys=args.max_keys,key_map_path=args.protected_key_map)
        rendered = json.dumps(result,ensure_ascii=False,sort_keys=True,indent=2)+"\n"
        if len(rendered.encode()) > args.max_output_bytes: raise ProfileError("output byte limit exceeded")
    except ProfileError as error:
        print(f"profile error: {error}",file=sys.stderr); return 2
    print(rendered,end=""); return 0


if __name__ == "__main__": raise SystemExit(main())
