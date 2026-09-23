#!/usr/bin/env python3
from pathlib import Path
from open_chat_archive.validation import validate_repository, ValidationError

try:
    batches = validate_repository(Path(__file__).resolve().parents[1])
except ValidationError as error:
    print(f"validation failed: {error}")
    raise SystemExit(1)
print(f"validation passed: {len(batches)} example batches")
