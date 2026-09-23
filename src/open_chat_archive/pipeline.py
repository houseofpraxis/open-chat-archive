"""Console CLI from a verified gather store through import, HTML, and vault."""
from __future__ import annotations

import argparse
import os
import posixpath
import stat
import subprocess
import sys
from pathlib import Path
from typing import NoReturn

from .gather_import import (
    ARGUMENT_INVALID,
    COUNT_KEYS,
    GatherImportError,
    OK,
    SOURCE_STORE_OVERLAP,
    _aggregate,
    _emit,
    _fail,
    _overlap_or_fail,
    _require_abs,
    run as gather_run,
)
from .gather_store import (
    GatherStoreError,
    exclusive_publish,
    is_hex64,
    open_child_dir,
    open_child_file,
    open_dir_abs,
    owner_only_dir,
    paths_overlap,
    split_leaf,
)
from .inspect_site import build_all
from .validation import ValidationError, validate_archive, write_validation_attestation


PIPELINE_RENDER_UNAVAILABLE = "PIPELINE_RENDER_UNAVAILABLE"
PIPELINE_OUTPUT_UNTRUSTED = "PIPELINE_OUTPUT_UNTRUSTED"
WORK_MARKER_NAME = ".oca-bridge-pipeline"
WORK_MARKER_TEXT = b"oca-bridge-pipeline/v0.1-experimental\n"


class _QuietParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        _fail(ARGUMENT_INVALID, 2)


def build_parser() -> argparse.ArgumentParser:
    parser = _QuietParser(prog="open-chat-archive-from-gather", add_help=True)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("verify", "plan", "apply", "run"):
        item = sub.add_parser(name, add_help=True)
        item.add_argument("--store", required=True)
        item.add_argument("--plan")
        item.add_argument("--archives-parent")
        item.add_argument("--receipt-id", action="append", default=[])
        item.add_argument("--adapter", choices=("claude-code", "codex", "grok-build"))
        item.add_argument("--details-output")
        item.add_argument("--work-root")
        item.add_argument("--vault-command")
    return parser


def _ensure_work_root(path: str, store_path: str) -> None:
    try:
        if paths_overlap(path, True, store_path, True):
            _fail(SOURCE_STORE_OVERLAP, 8)
    except GatherStoreError as error:
        if error.code == ARGUMENT_INVALID:
            _fail(ARGUMENT_INVALID, 2)
        raise GatherImportError(error.code, error.exit_code) from None
    marker = posixpath.join(path, WORK_MARKER_NAME)
    if posixpath.lexists(path):
        try:
            fd, _chain = open_dir_abs(path)
        except GatherStoreError:
            _fail(PIPELINE_OUTPUT_UNTRUSTED)
        try:
            info = os.fstat(fd)
            if not owner_only_dir(info, os.geteuid()):
                _fail(PIPELINE_OUTPUT_UNTRUSTED)
            names = [name for name in os.listdir(fd) if name not in {".", ".."}]
        finally:
            os.close(fd)
        if WORK_MARKER_NAME not in names:
            _fail(PIPELINE_OUTPUT_UNTRUSTED)
        return
    directory, name = split_leaf(path)
    try:
        parent, _chain = open_dir_abs(directory)
    except GatherStoreError as error:
        raise GatherImportError(error.code, error.exit_code) from None
    try:
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
        except OSError:
            _fail(PIPELINE_OUTPUT_UNTRUSTED)
        child = open_child_dir(parent, name)
        try:
            os.fchmod(child, 0o700)
            info = os.fstat(child)
            if not owner_only_dir(info, os.geteuid()):
                _fail(PIPELINE_OUTPUT_UNTRUSTED)
        finally:
            os.close(child)
    finally:
        os.close(parent)
    exclusive_publish(marker, WORK_MARKER_TEXT, exist_code=PIPELINE_OUTPUT_UNTRUSTED, exist_exit=4)


def _ensure_subdir(parent: str, name: str) -> str:
    path = posixpath.join(parent, name)
    if posixpath.lexists(path):
        try:
            fd, _chain = open_dir_abs(path)
        except GatherStoreError:
            _fail(PIPELINE_OUTPUT_UNTRUSTED)
        try:
            info = os.fstat(fd)
            if not owner_only_dir(info, os.geteuid()):
                _fail(PIPELINE_OUTPUT_UNTRUSTED)
        finally:
            os.close(fd)
        return path
    directory, leaf = split_leaf(path)
    try:
        pfd, _chain = open_dir_abs(directory)
    except GatherStoreError:
        _fail(PIPELINE_OUTPUT_UNTRUSTED)
    try:
        try:
            os.mkdir(leaf, 0o700, dir_fd=pfd)
        except OSError:
            _fail(PIPELINE_OUTPUT_UNTRUSTED)
        child = open_child_dir(pfd, leaf)
        try:
            os.fchmod(child, 0o700)
        finally:
            os.close(child)
    finally:
        os.close(pfd)
    return path


def _vault_executable(path: str) -> str:
    cleaned = _require_abs(path)
    directory, name = split_leaf(cleaned)
    try:
        parent, _chain = open_dir_abs(directory)
    except GatherStoreError:
        _fail(ARGUMENT_INVALID, 2)
    try:
        fd, info = open_child_file(parent, name)
        os.close(fd)
    except GatherStoreError:
        _fail(ARGUMENT_INVALID, 2)
    finally:
        os.close(parent)
    if not stat.S_ISREG(info.st_mode) or not os.access(cleaned, os.X_OK):
        _fail(ARGUMENT_INVALID, 2)
    return cleaned


def _chmod_owner_only_tree(root: str) -> None:
    """Dirs 0700, files 0600. Never follow symlinks."""
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        try:
            os.chmod(dirpath, 0o700, follow_symlinks=False)
        except OSError:
            _fail(PIPELINE_OUTPUT_UNTRUSTED)
        keep: list[str] = []
        for name in dirnames:
            path = posixpath.join(dirpath, name)
            if posixpath.islink(path):
                continue
            keep.append(name)
        dirnames[:] = keep
        for name in filenames:
            path = posixpath.join(dirpath, name)
            if posixpath.islink(path):
                continue
            try:
                os.chmod(path, 0o600, follow_symlinks=False)
            except OSError:
                _fail(PIPELINE_OUTPUT_UNTRUSTED)


def _list_archive_dirs(parent: str) -> list[str]:
    """Descriptor-verified regular archive directories (hex64 names, no symlink)."""
    fd, _chain = open_dir_abs(parent)
    try:
        try:
            names = sorted(name for name in os.listdir(fd) if name not in {".", ".."})
        except OSError:
            _fail(PIPELINE_OUTPUT_UNTRUSTED)
        found: list[str] = []
        for name in names:
            if not is_hex64(name):
                continue
            try:
                child = open_child_dir(fd, name)
            except GatherStoreError:
                continue
            try:
                info = os.fstat(child)
                if stat.S_ISDIR(info.st_mode):
                    found.append(posixpath.join(parent, name))
            finally:
                os.close(child)
        return found
    finally:
        os.close(fd)


def _run_pipeline(args: argparse.Namespace) -> tuple[dict, int]:
    store_path = _require_abs(args.store)
    plan_path = _require_abs(args.plan) if args.plan else ""
    work_root = _require_abs(args.work_root) if args.work_root else ""
    if not plan_path or not work_root or not args.vault_command:
        _fail(ARGUMENT_INVALID, 2)
    vault_command = _vault_executable(args.vault_command)
    details_path = _require_abs(args.details_output) if args.details_output else ""
    _overlap_or_fail(work_root, True, store_path, True)
    _overlap_or_fail(plan_path, False, store_path, True)
    _overlap_or_fail(plan_path, False, work_root, True)
    if details_path:
        _overlap_or_fail(details_path, False, store_path, True)
        _overlap_or_fail(details_path, False, work_root, True)
        _overlap_or_fail(details_path, False, plan_path, False)
    _ensure_work_root(work_root, store_path)
    archives_parent = _ensure_subdir(work_root, "archives")
    inspector_out = posixpath.join(work_root, "inspector")
    attest_dir = _ensure_subdir(work_root, "attestations")
    vault_parent = _ensure_subdir(work_root, "vault")
    apply_argv = [
        "apply", "--store", store_path, "--plan", plan_path,
        "--archives-parent", archives_parent,
    ]
    for receipt_id in args.receipt_id or []:
        apply_argv.extend(["--receipt-id", receipt_id])
    if args.adapter:
        apply_argv.extend(["--adapter", args.adapter])
    if details_path:
        apply_argv.extend(["--details-output", details_path])
    body, code = gather_run(apply_argv)
    body["operation"] = "run"
    if code != 0 or not body.get("complete"):
        body["complete"] = False
        return body, code if code != 0 else 8
    try:
        archives = _list_archive_dirs(archives_parent)
        if not archives:
            body["complete"] = False
            body["code"] = PIPELINE_RENDER_UNAVAILABLE
            return body, 8
        generated = 0
        for archive_path in archives:
            _chmod_owner_only_tree(archive_path)
            archive = Path(archive_path)
            name = posixpath.basename(archive_path)
            dest = posixpath.join(attest_dir, name + ".json")
            _overlap_or_fail(dest, False, store_path, True)
            _overlap_or_fail(dest, False, archive_path, True)
            if details_path:
                _overlap_or_fail(dest, False, details_path, False)
            validate_archive(archive)
            write_validation_attestation(archive, Path(dest))
        build_all(Path(archives_parent), Path(inspector_out))
        for archive_path in archives:
            name = posixpath.basename(archive_path)
            dest = posixpath.join(attest_dir, name + ".json")
            output = posixpath.join(vault_parent, name)
            _overlap_or_fail(output, True, store_path, True)
            _overlap_or_fail(output, True, archive_path, True)
            _overlap_or_fail(output, True, dest, False)
            result = subprocess.run(
                [
                    vault_command, "build",
                    "--archive", archive_path,
                    "--attestation", dest,
                    "--output", output,
                ],
                cwd=work_root,
                check=False,
                shell=False,
                capture_output=True,
            )
            if result.returncode != 0 or not posixpath.lexists(output):
                body["complete"] = False
                body["code"] = PIPELINE_RENDER_UNAVAILABLE
                return body, 8
            generated += 1
        if generated == 0:
            body["complete"] = False
            body["code"] = PIPELINE_RENDER_UNAVAILABLE
            return body, 8
    except (ValidationError, GatherStoreError, GatherImportError, OSError, RuntimeError, ValueError):
        body["complete"] = False
        body["code"] = PIPELINE_RENDER_UNAVAILABLE
        return body, 8
    body["complete"] = True
    body["code"] = OK
    return body, 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if argv and argv[0] in {"verify", "plan", "apply"}:
            from . import gather_import
            return gather_import.main(argv)
        args = build_parser().parse_args(argv)
        if args.command != "run":
            from . import gather_import
            return gather_import.main(argv)
        body, code = _run_pipeline(args)
        _emit(body)
        return code
    except GatherImportError as error:
        body = _aggregate("run", complete=False, code=error.code, counts={key: 0 for key in COUNT_KEYS}, by_adapter={})
        _emit(body)
        return error.exit_code
    except GatherStoreError as error:
        body = _aggregate("run", complete=False, code=error.code, counts={key: 0 for key in COUNT_KEYS}, by_adapter={})
        _emit(body)
        return error.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
