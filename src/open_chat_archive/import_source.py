"""Explicit local source import command; diagnostics deliberately contain no source data."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .batch_writer import AdapterInputError
from .source_adapters import create_adapter
from .validation import ValidationError, validate_archive


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Import one local chat export into an immutable archive batch.")
    parser.add_argument("source", type=Path, metavar="SOURCE")
    parser.add_argument("archive_root", type=Path, metavar="ARCHIVE_ROOT")
    parser.add_argument("--format", required=True, choices=("claude-ai", "claude-code", "open-webui", "codex", "grok-build"))
    parser.add_argument("--source-scope-id", required=True)
    parser.add_argument("--archive-id", required=True)
    parser.add_argument("--batch-id", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = create_adapter(args.format, args.source_scope_id).conform(args.source, args.archive_root,
            archive_id=args.archive_id, batch_id=args.batch_id)
        validate_archive(args.archive_root)
    except (AdapterInputError, ValidationError, ValueError):
        print("import failed: ADAPTER_IMPORT_REJECTED", file=sys.stderr)
        return 2
    print("import completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
