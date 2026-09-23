"""Typed Tier 1 adapter contracts. Execution isolation is an operator control."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Sequence


@dataclass(frozen=True)
class Diagnostic:
    code: str
    coordinate: str | None = None


@dataclass(frozen=True)
class FingerprintResult:
    matched: bool
    fingerprint: str | None = None


@dataclass(frozen=True)
class BatchResult:
    batch_dir: Path
    coverage_ledger: Sequence[dict[str, Any]]
    diagnostics: Sequence[Diagnostic] = ()


class Adapter(Protocol):
    namespace: str
    version: str
    def recognize(self, profile: dict[str, Any]) -> FingerprintResult: ...
    def conform(self, source: Path, archive_root: Path, *, archive_id: str, batch_id: str) -> BatchResult: ...


def select_adapter(adapters: Sequence[Adapter], profile: dict[str, Any]) -> Adapter:
    matches = [adapter for adapter in adapters if adapter.recognize(profile).matched]
    if len(matches) != 1: raise ValueError("unknown-fingerprint" if not matches else "ambiguous-fingerprint")
    return matches[0]
