"""Generate an owned, runnable synthetic adapter package from shape-only metadata."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from .strictjson import load_file

ADAPTER = '''from pathlib import Path
from open_chat_archive.adapter import BatchResult, FingerprintResult
from open_chat_archive.synthetic import create_synthetic_batch

class GeneratedAdapter:
    namespace = "org.example.generated"
    version = "0.1.0"
    fingerprint = "synthetic.shape-only-v1"
    def recognize(self, profile):
        return FingerprintResult(profile.get("fingerprint") == self.fingerprint, self.fingerprint if profile.get("fingerprint") == self.fingerprint else None)
    def conform(self, source: Path, archive_root: Path, *, archive_id: str, batch_id: str) -> BatchResult:
        # Runnable synthetic baseline. Replace only this mapping during local development.
        batch = create_synthetic_batch(archive_root / "archives" / archive_id, archive_id=archive_id, batch_id=batch_id)
        return BatchResult(batch, [{"path":p,"disposition":"preserved","preservation_target":"source-artifact"} for p in __import__('json').loads((Path(__file__).parent/'dossier.json').read_text())['profile_paths']])
'''
TEST = '''import unittest
from adapter import GeneratedAdapter
class AdapterTest(unittest.TestCase):
    def test_fingerprint(self):
        adapter=GeneratedAdapter()
        self.assertTrue(adapter.recognize({"fingerprint":"synthetic.shape-only-v1"}).matched)
        self.assertFalse(adapter.recognize({"fingerprint":"unknown"}).matched)
if __name__ == "__main__": unittest.main()
'''


PROFILE_PATH = re.compile(r"^/(?:k[0-9]{4}|\*)(?:/(?:k[0-9]{4}|\*))*$")


def _profile_paths(profile: dict) -> list[str]:
    if set(profile) != {"profile_version", "disclosure_policy", "paths", "node_count_bucket"}: raise ValueError("profile-shape")
    if profile.get("profile_version") != "shape-only-v1": raise ValueError("profile-policy")
    policy = profile.get("disclosure_policy")
    if policy != {"allowed":["opaque-alias-topology","json-types","bounded-cardinality-buckets","structural-coordinates"],"forbidden":["raw-values","raw-object-keys","value-or-key-hashes"]}: raise ValueError("profile-policy")
    entries = profile.get("paths")
    if not isinstance(entries, list): raise ValueError("profile-shape")
    paths = [entry.get("path") for entry in entries if isinstance(entry, dict)]
    if len(paths) != len(entries) or len(paths) != len(set(paths)) or any(path != "/" and not PROFILE_PATH.fullmatch(path) for path in paths): raise ValueError("profile-path")
    return paths


def scaffold(profile: dict, output: Path) -> list[Path]:
    paths = _profile_paths(profile)
    if output.is_symlink() or (output.exists() and (not output.is_dir() or any(output.iterdir()))): raise ValueError("output-not-empty")
    output.mkdir(parents=True,exist_ok=True); (output/"fixtures").mkdir()
    dossier={"classification":"model-visible-shape-only","policy":"shape-only-v1","profile_paths":paths,"optional_semantic_aliases":"locally-approved labels only; never raw keys"}
    files={"adapter.py":ADAPTER,"test_adapter.py":TEST,"dossier.json":json.dumps(dossier,sort_keys=True,indent=2)+"\n","fixtures/positive.profile.json":json.dumps({"fingerprint":"synthetic.shape-only-v1","paths":paths},sort_keys=True)+"\n","fixtures/negative.profile.json":json.dumps({"fingerprint":"unknown","paths":[]},sort_keys=True)+"\n","fixtures/positive.source.json":"{\"synthetic\":true}\n","OWNED_FILES.txt":"adapter.py\ntest_adapter.py\ndossier.json\nfixtures/positive.profile.json\nfixtures/negative.profile.json\nfixtures/positive.source.json\nOWNED_FILES.txt\n"}
    for relative,content in files.items(): (output/relative).write_text(content,encoding="utf-8",newline="\n")
    return [output/name for name in files]


def main(argv=None):
    p=argparse.ArgumentParser(description="Scaffold a runnable adapter package from model-safe shape-only-v1 metadata")
    p.add_argument("profile",type=Path); p.add_argument("output",type=Path)
    args=p.parse_args(argv)
    try:
        profile=load_file(args.profile)
        scaffold(profile,args.output)
    except Exception as exc:
        print(f"scaffold failed: {type(exc).__name__}"); return 2
    print("scaffold created"); return 0
if __name__ == "__main__": raise SystemExit(main())
