"""Optional, explicit experimental operator metadata for archive source scopes."""
from __future__ import annotations

import argparse
import errno
import json
import os
import stat
import sys
import uuid
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, NoReturn

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from .strictjson import StrictJSONError, load_ndjson, loads_bytes
from .validation import ValidationError, validate_archive


MAX_BYTES = 1024 * 1024


class SourceRegistryError(ValueError):
    """A stable, value-safe source-registry failure code."""


def _fail(code: str) -> NoReturn:
    raise SourceRegistryError(code)


def _schema() -> Draft202012Validator:
    try:
        raw = resources.files("open_chat_archive.schema_assets").joinpath(
            "source-registry-0.1-experimental.schema.json").read_bytes()
        value = loads_bytes(raw)
        Draft202012Validator.check_schema(value)
    except (OSError, StrictJSONError, SchemaError):
        _fail("REGISTRY_SCHEMA")
    return Draft202012Validator(value)


REGISTRY_PLATFORM_UNSUPPORTED = "REGISTRY_PLATFORM_UNSUPPORTED"


def _directory_flags() -> int:
    """Return the mandatory safe directory flags, or a stable failure code.

    Registry input and output are intentionally unavailable where descriptor
    relative, no-follow traversal cannot be expressed.  Keeping this lookup
    here (rather than at module import) leaves the ordinary inspector usable
    on platforms without these POSIX flags.
    """
    try:
        return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    except AttributeError:
        _fail(REGISTRY_PLATFORM_UNSUPPORTED)


def _require_safe_registry_io() -> None:
    """Reject registry operations if their required POSIX primitives are absent."""
    _directory_flags()


def _input_flags() -> int:
    """Return nonblocking no-follow input flags without a portability fallback."""
    try:
        return os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    except AttributeError:
        _fail(REGISTRY_PLATFORM_UNSUPPORTED)


def _open_directory_chain(path: Path, *, missing: str, symlink: str,
                          not_directory: str) -> int:
    """Open an existing directory without traversing an untrusted symlink."""
    text = os.fspath(path)
    absolute = os.path.isabs(text)
    parts = Path(text).parts[1:] if absolute else Path(text).parts
    try:
        descriptor = os.open("/" if absolute else ".", _directory_flags())
    except OSError:
        _fail(missing)
    try:
        for part in parts:
            try:
                info = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    _fail(symlink)
                if not stat.S_ISDIR(info.st_mode):
                    _fail(not_directory)
                next_descriptor = os.open(part, _directory_flags(), dir_fd=descriptor)
            except FileNotFoundError:
                _fail(missing)
            except OSError as error:
                if error.errno == errno.ELOOP:
                    _fail(symlink)
                _fail(not_directory)
            os.close(descriptor)
            descriptor = next_descriptor
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _open_registry_input(path: Path) -> int:
    """Acquire the registry through no-follow descriptors and a bounded reader."""
    text = os.fspath(path)
    absolute = os.path.isabs(text)
    parts = Path(text).parts
    name = parts[-1] if parts else ""
    if not name or name in (".", ".."):
        _fail("REGISTRY_NOT_REGULAR")
    parent_parts = parts[1:-1] if absolute else parts[:-1]
    parent_text = os.path.join("/" if absolute else ".", *parent_parts)
    parent = _open_directory_chain(
        Path(parent_text), missing="REGISTRY_MISSING", symlink="REGISTRY_SYMLINK",
        not_directory="REGISTRY_MISSING")
    try:
        flags = _input_flags()
        try:
            descriptor = os.open(name, flags, dir_fd=parent)
        except FileNotFoundError:
            _fail("REGISTRY_MISSING")
        except OSError as error:
            if error.errno == errno.ELOOP:
                _fail("REGISTRY_SYMLINK")
            _fail("REGISTRY_READ")
    finally:
        os.close(parent)
    try:
        try:
            info = os.fstat(descriptor)
        except OSError:
            _fail("REGISTRY_READ")
        if not stat.S_ISREG(info.st_mode):
            _fail("REGISTRY_NOT_REGULAR")
        if info.st_size > MAX_BYTES:
            _fail("REGISTRY_TOO_LARGE")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _read_registry(path: Path) -> bytes:
    descriptor = _open_registry_input(path)
    try:
        raw = bytearray()
        while len(raw) <= MAX_BYTES:
            try:
                chunk = os.read(descriptor, MAX_BYTES + 1 - len(raw))
            except OSError:
                _fail("REGISTRY_READ")
            if not chunk:
                break
            raw.extend(chunk)
        if len(raw) > MAX_BYTES:
            _fail("REGISTRY_TOO_LARGE")
        return bytes(raw)
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class SourceRegistry:
    """Frozen source definitions indexed only by their full explicit binding."""

    _bindings: Mapping[tuple[str, str, str], Mapping[str, Any]]

    def resolve(self, archive_id: str, adapter_namespace: str,
                source_scope_id: str) -> Mapping[str, Any] | None:
        return self._bindings.get((archive_id, adapter_namespace, source_scope_id))


def load_source_registry(path: Path) -> SourceRegistry:
    """Load an explicitly supplied registry without exposing input values in errors."""
    _require_safe_registry_io()
    raw = _read_registry(path)
    try:
        value = loads_bytes(raw)
    except StrictJSONError:
        _fail("REGISTRY_JSON")
    if not isinstance(value, dict) or list(_schema().iter_errors(value)):
        _fail("REGISTRY_SCHEMA")
    sources: dict[str, Mapping[str, Any]] = {}
    for item in value["sources"]:
        source_id = item["source_id"]
        if source_id in sources:
            _fail("REGISTRY_SOURCE_DUPLICATE")
        sources[source_id] = MappingProxyType(dict(item))
    bindings: dict[tuple[str, str, str], Mapping[str, Any]] = {}
    for item in value["bindings"]:
        key = (item["archive_id"], item["adapter_namespace"], item["source_scope_id"])
        if key in bindings:
            _fail("REGISTRY_BINDING_DUPLICATE")
        source = sources.get(item["source_id"])
        if source is None:
            _fail("REGISTRY_BINDING_DANGLING")
        if source["adapter_namespace"] != item["adapter_namespace"]:
            _fail("REGISTRY_NAMESPACE_MISMATCH")
        bindings[key] = source
    return SourceRegistry(MappingProxyType(bindings))


def _overlaps(left: Path, right: Path) -> bool:
    try:
        left.relative_to(right)
        return True
    except ValueError:
        try:
            right.relative_to(left)
            return True
        except ValueError:
            return False


def _lexical_absolute(path: Path) -> Path:
    """Normalize ``.``/``..`` without resolving a possible symlink."""
    return Path(os.path.abspath(os.fspath(path)))


def _open_output_parent(output: Path) -> tuple[int, str]:
    """Create missing output parents owner-only, without following any ancestor."""
    text = os.fspath(output)
    name = os.path.basename(text)
    if not name or name in (".", ".."):
        _fail("REGISTRY_OUTPUT_CHECK")
    absolute = os.path.isabs(text)
    parent_parts = Path(os.path.dirname(text) or ".").parts
    if absolute:
        parent_parts = parent_parts[1:]
    try:
        descriptor = os.open("/" if absolute else ".", _directory_flags())
    except OSError:
        _fail("REGISTRY_OUTPUT_CHECK")
    try:
        for part in parent_parts:
            try:
                info = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    _fail("REGISTRY_OUTPUT_SYMLINK")
                if not stat.S_ISDIR(info.st_mode):
                    _fail("REGISTRY_OUTPUT_CHECK")
                next_descriptor = os.open(part, _directory_flags(), dir_fd=descriptor)
            except FileNotFoundError:
                try:
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                    next_descriptor = os.open(part, _directory_flags(), dir_fd=descriptor)
                except FileExistsError:
                    try:
                        next_descriptor = os.open(part, _directory_flags(), dir_fd=descriptor)
                    except OSError as error:
                        if error.errno == errno.ELOOP:
                            _fail("REGISTRY_OUTPUT_SYMLINK")
                        _fail("REGISTRY_OUTPUT_CHECK")
                except OSError as error:
                    if error.errno == errno.ELOOP:
                        _fail("REGISTRY_OUTPUT_SYMLINK")
                    _fail("REGISTRY_OUTPUT_WRITE")
            except OSError as error:
                if error.errno == errno.ELOOP:
                    _fail("REGISTRY_OUTPUT_SYMLINK")
                _fail("REGISTRY_OUTPUT_CHECK")
            os.close(descriptor)
            descriptor = next_descriptor
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, name


def _output_exists(parent: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError:
        _fail("REGISTRY_OUTPUT_CHECK")
    return True


def _publish_registry(parent: int, name: str, encoded: bytes) -> None:
    """Publish a 0600 file via a same-directory no-replace hard link.

    Once the link succeeds the registry is valid and retained.  A subsequent
    directory-fsync failure is reported as
    ``REGISTRY_PUBLISHED_DURABILITY_UNCONFIRMED``: callers must not interpret
    the operation as durable success, but must not delete a destination that
    another actor could already have replaced.
    """
    temporary = "." + name + ".registry-" + uuid.uuid4().hex
    temp_created = False
    published = False
    try:
        descriptor = os.open(
            temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
            0o600, dir_fd=parent)
        temp_created = True
        try:
            written = 0
            while written < len(encoded):
                wrote = os.write(descriptor, encoded[written:])
                if wrote <= 0:
                    raise OSError("short registry write")
                written += wrote
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            os.link(temporary, name, src_dir_fd=parent, dst_dir_fd=parent,
                    follow_symlinks=False)
        except FileExistsError:
            _fail("REFUSE_EXISTING_OUTPUT")
        published = True
        try:
            os.fsync(parent)
        except OSError:
            _fail("REGISTRY_PUBLISHED_DURABILITY_UNCONFIRMED")
    except SourceRegistryError:
        raise
    except OSError:
        _fail("REGISTRY_PUBLISHED_DURABILITY_UNCONFIRMED" if published
              else "REGISTRY_OUTPUT_WRITE")
    finally:
        if temp_created:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass
            except OSError:
                # The link is already the durable candidate; never attempt a
                # pathname-based cleanup of the published destination.
                if published:
                    _fail("REGISTRY_PUBLISHED_DURABILITY_UNCONFIRMED")
                _fail("REGISTRY_OUTPUT_WRITE")


def _archive_scopes(archive: Path, schema_root: Path | None) -> set[tuple[str, str, str]]:
    try:
        batches = validate_archive(archive, schema_root=schema_root)
    except ValidationError:
        _fail("REGISTRY_ARCHIVE_INVALID")
    scopes: set[tuple[str, str, str]] = set()
    for batch in batches:
        try:
            records = load_ndjson((batch / "records.ndjson").read_bytes())
        except (OSError, StrictJSONError):
            _fail("REGISTRY_ARCHIVE_READ")
        for record in records:
            identity = record.get("identity", {})
            archive_id = identity.get("archive_id")
            namespace = identity.get("adapter_namespace")
            scope = identity.get("source_scope_id")
            if isinstance(archive_id, str) and isinstance(namespace, str) and isinstance(scope, str):
                scopes.add((archive_id, namespace, scope))
    return scopes


def init_source_registry(archives_parent: Path, output: Path, *, schema_root: Path | None = None) -> None:
    """Create a new owner-only placeholder registry; never replaces an output."""
    _require_safe_registry_io()
    parent_descriptor = _open_directory_chain(
        archives_parent, missing="ARCHIVES_PARENT_MISSING",
        symlink="ARCHIVES_PARENT_SYMLINK", not_directory="ARCHIVES_PARENT_NOT_DIRECTORY")
    os.close(parent_descriptor)
    parent = _lexical_absolute(archives_parent)
    output_path = _lexical_absolute(output)
    if _overlaps(output_path, parent):
        _fail("REFUSE_INPUT_OUTPUT_OVERLAP")
    try:
        children = sorted(parent.iterdir())
    except OSError:
        _fail("ARCHIVES_PARENT_READ")
    archives: list[Path] = []
    for child in children:
        try:
            info = child.lstat()
        except OSError:
            _fail("ARCHIVE_CHILD_READ")
        if stat.S_ISLNK(info.st_mode):
            _fail("ARCHIVE_CHILD_SYMLINK")
        if stat.S_ISDIR(info.st_mode):
            archives.append(child)
    if not archives:
        _fail("ARCHIVE_NO_CHILD")
    # Validate every archive before creating any output directory or temp file.
    scopes: set[tuple[str, str, str]] = set()
    for archive in archives:
        scopes.update(_archive_scopes(archive, schema_root))
    try:
        output_info = output.lstat()
    except FileNotFoundError:
        output_info = None
    except OSError:
        _fail("REGISTRY_OUTPUT_CHECK")
    if output_info is not None:
        _fail("REFUSE_EXISTING_OUTPUT")
    if len(scopes) > 10_000:
        _fail("REGISTRY_TOO_MANY_ENTRIES")
    source_rows: list[dict[str, Any]] = []
    binding_rows: list[dict[str, str]] = []
    for archive_id, namespace, scope in sorted(scopes):
        source_id = "src-" + uuid.uuid4().hex
        source_rows.append({"source_id": source_id, "adapter_namespace": namespace,
                            "display_label": namespace + " / " + scope,
                            "status": "unverified"})
        binding_rows.append({"archive_id": archive_id, "adapter_namespace": namespace,
                             "source_scope_id": scope, "source_id": source_id})
    encoded = (json.dumps({"registry_version": "0.1-experimental", "sources": source_rows,
                           "bindings": binding_rows}, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    generated = {"registry_version": "0.1-experimental", "sources": source_rows,
                 "bindings": binding_rows}
    if len(encoded) > MAX_BYTES:
        _fail("REGISTRY_TOO_LARGE")
    if list(_schema().iter_errors(generated)):
        _fail("REGISTRY_GENERATED_SCHEMA")
    output_parent, output_name = _open_output_parent(output)
    try:
        if _output_exists(output_parent, output_name):
            _fail("REFUSE_EXISTING_OUTPUT")
        _publish_registry(output_parent, output_name, encoded)
    finally:
        os.close(output_parent)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage an experimental local source registry")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("archives_parent", type=Path)
    init.add_argument("--output", type=Path, required=True)
    validate = commands.add_parser("validate")
    validate.add_argument("registry", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            init_source_registry(args.archives_parent, args.output)
        else:
            load_source_registry(args.registry)
    except SourceRegistryError as error:
        print("source registry failed: " + str(error), file=sys.stderr)
        return 1
    print("source registry " + ("initialized" if args.command == "init" else "valid"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
