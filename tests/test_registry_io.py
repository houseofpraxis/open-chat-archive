import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from open_chat_archive import source_registry
from open_chat_archive.source_registry import SourceRegistryError, init_source_registry, load_source_registry


class RegistryInitializationTests(unittest.TestCase):
    def _archives(self, root: Path, count: int = 1) -> Path:
        parent = root / "archives"
        parent.mkdir()
        for number in range(count):
            (parent / f"archive-{number}").mkdir()
        return parent

    def _init(self, parent: Path, output: Path, scopes: set[tuple[str, str, str]]) -> None:
        with mock.patch.object(source_registry, "_archive_scopes", return_value=scopes):
            init_source_registry(parent, output)

    def test_initializes_loadable_owner_only_registry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent = self._archives(root)
            output = root / "created" / "registry.json"
            self._init(parent, output, {("archive", "example.adapter", "scope")})
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(output.parent.stat().st_mode), 0o700)
            self.assertIsNotNone(load_source_registry(output).resolve("archive", "example.adapter", "scope"))

    def test_refuses_overlap_existing_and_symlinked_ancestors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent = self._archives(root)
            scopes = {("archive", "example.adapter", "scope")}
            with self.assertRaisesRegex(SourceRegistryError, "REFUSE_INPUT_OUTPUT_OVERLAP"):
                self._init(parent, parent / "registry.json", scopes)
            existing = root / "exists.json"
            existing.write_text("curated", encoding="utf-8")
            with self.assertRaisesRegex(SourceRegistryError, "REFUSE_EXISTING_OUTPUT"):
                self._init(parent, existing, scopes)
            actual = root / "actual"
            actual.mkdir()
            linked = root / "linked"
            os.symlink(actual, linked)
            with self.assertRaisesRegex(SourceRegistryError, "REGISTRY_OUTPUT_SYMLINK"):
                self._init(parent, linked / "registry.json", scopes)
            linked_archives = root / "linked-archives"
            os.symlink(parent, linked_archives)
            with self.assertRaisesRegex(SourceRegistryError, "ARCHIVES_PARENT_SYMLINK"):
                self._init(linked_archives, root / "other.json", scopes)

    def test_preflight_limits_before_creating_output_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent = self._archives(root)
            output = root / "not-created" / "registry.json"
            too_many = {("archive" + str(number), "example.adapter", "scope")
                        for number in range(10_001)}
            with self.assertRaisesRegex(SourceRegistryError, "REGISTRY_TOO_MANY_ENTRIES"):
                self._init(parent, output, too_many)
            self.assertFalse(output.parent.exists())
            too_large = {("archive" + str(number), "example.adapter", "scope" + str(number))
                         for number in range(10_000)}
            with self.assertRaisesRegex(SourceRegistryError, "REGISTRY_TOO_LARGE"):
                self._init(parent, output, too_large)
            self.assertFalse(output.parent.exists())

    def test_every_archive_is_validated_before_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent = self._archives(root, count=2)
            output = root / "out.json"
            calls: list[Path] = []

            def collect(archive: Path, schema_root: Path | None) -> set[tuple[str, str, str]]:
                calls.append(archive)
                self.assertFalse(output.exists())
                return {(archive.name, "example.adapter", "scope")}

            with mock.patch.object(source_registry, "_archive_scopes", side_effect=collect):
                init_source_registry(parent, output)
            self.assertEqual({path.name for path in calls}, {"archive-0", "archive-1"})
            self.assertTrue(output.exists())

    def test_failures_before_publish_leave_no_output_or_temp(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent = self._archives(root)
            output = root / "output" / "registry.json"
            with mock.patch.object(source_registry.os, "fsync", side_effect=OSError("disk")):
                with self.assertRaisesRegex(SourceRegistryError, "REGISTRY_OUTPUT_WRITE"):
                    self._init(parent, output, {("archive", "example.adapter", "scope")})
            self.assertFalse(output.exists())
            self.assertEqual(list(output.parent.glob(".*.registry-*")), [])

    def test_post_publish_fsync_failure_retains_file_without_temp_or_chmod(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent = self._archives(root)
            output = root / "output.json"
            with mock.patch.object(source_registry.os, "fsync", side_effect=[None, OSError("directory")]), \
                 mock.patch.object(source_registry.os, "chmod") as chmod:
                with self.assertRaisesRegex(SourceRegistryError, "REGISTRY_PUBLISHED_DURABILITY_UNCONFIRMED"):
                    self._init(parent, output, {("archive", "example.adapter", "scope")})
            self.assertTrue(output.exists())
            self.assertEqual(list(root.glob(".*.registry-*")), [])
            chmod.assert_not_called()

    def test_link_race_cleans_temp_and_does_not_publish(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent = self._archives(root)
            output = root / "output.json"
            with mock.patch.object(source_registry.os, "link", side_effect=FileExistsError):
                with self.assertRaisesRegex(SourceRegistryError, "REFUSE_EXISTING_OUTPUT"):
                    self._init(parent, output, {("archive", "example.adapter", "scope")})
            self.assertFalse(output.exists())
            self.assertEqual(list(root.glob(".*.registry-*")), [])

    def test_missing_posix_descriptor_flags_do_not_break_imports_or_no_registry_inspector(self) -> None:
        """Registry use fails closed, while the optional feature remains unimported."""
        simulated = """
import os
from pathlib import Path
del os.O_DIRECTORY
del os.O_NOFOLLOW
from open_chat_archive.inspect_site import _preflight_registry
from open_chat_archive.source_registry import SourceRegistryError, init_source_registry, load_source_registry
assert _preflight_registry(None, Path('unused')) is None
for operation in (lambda: load_source_registry(Path('unused')), lambda: init_source_registry(Path('unused'), Path('output.json'))):
    try:
        operation()
    except SourceRegistryError as error:
        assert str(error) == 'REGISTRY_PLATFORM_UNSUPPORTED'
    else:
        raise AssertionError('registry operation did not fail closed')
"""
        environment = dict(os.environ, PYTHONPATH=str(Path.cwd() / "src"))
        subprocess.run([sys.executable, "-c", simulated], env=environment, check=True)
