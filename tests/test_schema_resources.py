import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from importlib import resources
from pathlib import Path


class SchemaResourceTests(unittest.TestCase):
    def test_packaged_canonical_schema_bytes_match_authoritative_files(self) -> None:
        package = resources.files("open_chat_archive.schema_assets")
        for name in ("archive-record.schema.json", "import-manifest.schema.json"):
            authoritative = Path("schemas") / "v0.1" / name
            self.assertEqual(hashlib.sha256(authoritative.read_bytes()).digest(),
                             hashlib.sha256(package.joinpath("v0.1", name).read_bytes()).digest())

    def test_offline_wheel_works_outside_checkout(self) -> None:
        """Exercise packaged schemas, CLI, registry, and inspector from a wheel."""
        candidates = [sys.executable, shutil.which("python3"), shutil.which("python"),
                      "/usr/bin/python3", "/usr/bin/python"]
        backend = None
        checked: list[str] = []
        probe = ("from importlib.metadata import version; import wheel; "
                 "raise SystemExit(0 if int(version('setuptools').split('.', 1)[0]) >= 68 else 1)")
        for candidate in candidates:
            if not candidate or candidate in checked or not Path(candidate).is_file():
                continue
            checked.append(candidate)
            if subprocess.run([candidate, "-c", probe], capture_output=True).returncode == 0:
                backend = candidate
                break
        if backend is None:
            self.skipTest("no local wheel backend with setuptools>=68 and wheel: " + ", ".join(checked))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "public-source"
            source.mkdir()
            shutil.copytree(Path.cwd() / "src", source / "src")
            for name in ("pyproject.toml", "README.md"):
                shutil.copy2(Path.cwd() / name, source / name)
            self.assertEqual({"README.md", "pyproject.toml", "src"},
                             {item.name for item in source.iterdir()})
            wheelhouse = root / "wheelhouse"
            install_target = root / "installed"
            offline_environment = dict(os.environ, PIP_NO_CACHE_DIR="1")
            subprocess.run([backend, "-m", "pip", "wheel", ".", "--wheel-dir", str(wheelhouse),
                            "--no-index", "--no-deps", "--no-build-isolation"], cwd=source,
                           env=offline_environment, check=True)
            wheel = next(wheelhouse.glob("open_chat_archive-*.whl"))
            subprocess.run([sys.executable, "-m", "pip", "install", str(wheel), "--target", str(install_target),
                            "--no-index", "--no-deps", "--no-build-isolation"], env=offline_environment, check=True)
            smoke = """
import os
import subprocess
import sys
from pathlib import Path
import open_chat_archive
from open_chat_archive.inspect_site import build_all
from open_chat_archive.source_registry import load_source_registry
from open_chat_archive.synthetic import create_synthetic_batch
from open_chat_archive.validation import validate_archive

target = Path(os.environ[\"WHEEL_INSTALL_TARGET\"])
root = Path(os.environ[\"WHEEL_SMOKE_ROOT\"])
assert Path(open_chat_archive.__file__).resolve().is_relative_to(target.resolve())
parent = root / \"archives\"; archive = parent / \"synthetic\"
create_synthetic_batch(archive, archive_id=\"synthetic\", batch_id=\"batch\")
validate_archive(archive)
registry = root / \"registry.json\"
sources = target / \"bin\" / \"open-chat-archive-sources\"
subprocess.run([str(sources), \"init\", str(parent), \"--output\", str(registry)], check=True)
assert load_source_registry(registry).resolve(\"synthetic\", \"org.open-chat-archive.synthetic\", \"synthetic-scope\")
subprocess.run([str(sources), \"validate\", str(registry)], check=True)
assert build_all(parent, root / \"site\", source_registry=registry)
assert (root / \"site\" / \"synthetic\" / \"inspection-report.json\").is_file()
"""
            environment = dict(os.environ, PYTHONPATH=str(install_target), WHEEL_INSTALL_TARGET=str(install_target),
                               WHEEL_SMOKE_ROOT=str(root / "smoke"))
            subprocess.run([sys.executable, "-c", smoke], cwd=root, env=environment, check=True)
