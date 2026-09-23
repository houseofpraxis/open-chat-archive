"""Local adapter verifier; does not claim OS sandbox or network enforcement."""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
from typing import Any

from .adapter import select_adapter
from .strictjson import load_file
from .validation import reconcile_coverage, validate_batch


def _module(path: Path):
    spec = importlib.util.spec_from_file_location("oca_candidate_adapter", path)
    if not spec or not spec.loader: raise ValueError("adapter-import")
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module); return module


def verify_scaffold(package: Path, work_root: Path, canaries: list[str] | None = None) -> None:
    canaries = canaries or []
    dossier = load_file(package / "dossier.json")
    positive = load_file(package / "fixtures" / "positive.profile.json")
    negative = load_file(package / "fixtures" / "negative.profile.json")
    module = _module(package / "adapter.py"); adapter = module.GeneratedAdapter()
    if select_adapter([adapter], positive) is not adapter: raise ValueError("positive-fingerprint")
    try: select_adapter([adapter], negative)
    except ValueError as exc:
        if str(exc) != "unknown-fingerprint": raise
    else: raise ValueError("negative-fingerprint")
    # Synthetic conformation only here. Raw local execution uses the same contract but stays outside model context.
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        first = adapter.conform(package / "fixtures" / "positive.source.json", work_root / "run-one", archive_id="verify", batch_id="batch-one")
        second = adapter.conform(package / "fixtures" / "positive.source.json", work_root / "run-two", archive_id="verify", batch_id="batch-two")
    validate_batch(first.batch_dir, archive_root=first.batch_dir.parents[1]); validate_batch(second.batch_dir, archive_root=second.batch_dir.parents[1])
    reconcile_coverage(dossier["profile_paths"], list(first.coverage_ledger))
    def semantic(records): return {(r["record_type"],r["entity_id"],r["revision_id"]) for r in records if r["record_type"] not in {"source_artifact","import_batch","observation"}}
    if semantic(validate_batch(first.batch_dir, archive_root=first.batch_dir.parents[1])) != semantic(validate_batch(second.batch_dir, archive_root=second.batch_dir.parents[1])): raise ValueError("repeatability")
    visible_names = ["adapter.py", "test_adapter.py", "dossier.json", "OWNED_FILES.txt",
                     "fixtures/positive.profile.json", "fixtures/negative.profile.json",
                     "fixtures/positive.source.json"]
    visible = stdout.getvalue() + stderr.getvalue()
    for relative in visible_names: visible += (package / relative).read_text(encoding="utf-8")
    forbidden = canaries + [hashlib.sha256(canary.encode("utf-8")).hexdigest() for canary in canaries]
    if any(item in visible for item in forbidden): raise ValueError("canary-disclosure")
    allowed_coordinates = set(dossier["profile_paths"])
    for result in (first, second):
        if any(diagnostic.coordinate not in allowed_coordinates | {None} for diagnostic in result.diagnostics): raise ValueError("diagnostic-coordinate")
