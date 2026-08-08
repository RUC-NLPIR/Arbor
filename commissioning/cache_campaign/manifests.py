from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import secrets
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import SCHEMA_VERSION
from .oracle import scan_oracle_general
from .records import (
    ContractError,
    Portfolio,
    TraceWindow,
    load_candidate_object,
    record_sha256,
)


class ManifestError(ContractError):
    pass


@dataclass(frozen=True)
class _OwnedManifest:
    name: str
    identity: tuple[int, int]
    sha256: str | None


@dataclass
class _OwnedDirectory:
    path: Path | None
    descriptor: int
    identity: tuple[int, int]
    files: dict[str, _OwnedManifest] = field(default_factory=dict)


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _output_path(path: Path) -> Path:
    absolute = path.absolute()
    if os.path.lexists(absolute):
        raise ManifestError(f"output must not preexist: {absolute}")
    return absolute.resolve(strict=False)


def _interval(trace: TraceWindow) -> tuple[int, int]:
    return trace.start_request, trace.start_request + trace.max_requests


def _intervals_disjoint(left: TraceWindow, right: TraceWindow) -> bool:
    left_start, left_end = _interval(left)
    right_start, right_end = _interval(right)
    return left_end <= right_start or right_end <= left_start


def _differs_from_every_dev_window(item: TraceWindow, dev: list[TraceWindow]) -> bool:
    for dev_item in dev:
        if item.application != dev_item.application:
            continue
        if item.origin_sha256 != dev_item.origin_sha256:
            return False
        if not _intervals_disjoint(item, dev_item):
            return False
    return True


def _validate_splits(portfolio: Portfolio) -> None:
    traces = list(portfolio.traces)
    trace_ids = [item.trace_id for item in traces]
    if len(trace_ids) != len(set(trace_ids)):
        raise ManifestError("duplicate trace ID")
    file_hashes = [item.sha256 for item in traces]
    if len(file_hashes) != len(set(file_hashes)):
        raise ManifestError("duplicate physical trace SHA-256")

    by_origin: dict[str, list[TraceWindow]] = {}
    for trace in traces:
        by_origin.setdefault(trace.origin_sha256, []).append(trace)
    for origin_sha256, windows in by_origin.items():
        ordered = sorted(windows, key=lambda item: _interval(item))
        for previous, current in zip(ordered, ordered[1:]):
            if not _intervals_disjoint(previous, current):
                raise ManifestError(f"overlapping origin interval: {origin_sha256}")

    dev = [item for item in traces if item.split == "dev"]
    visible = [item for item in traces if item.split == "visible"]
    r3 = [item for item in traces if item.split == "r3"]
    if len(dev) < 3 or len({(item.organization, item.application) for item in dev}) < 2:
        raise ManifestError("dev requires three windows and two sources")
    if not visible:
        raise ManifestError("at least one visible window is required")
    if any(not _differs_from_every_dev_window(item, dev) for item in visible):
        raise ManifestError("visible must differ by application or disjoint time")
    if not r3:
        raise ManifestError("at least one R3 window is required")
    seen_orgs = {item.organization for item in [*dev, *visible]}
    if any(item.organization in seen_orgs for item in r3):
        raise ManifestError("R3 organization must be unseen")


def _trace_record(trace: TraceWindow, diagnostic: dict[str, object]) -> dict[str, object]:
    diagnostic_hash = diagnostic.get("diagnostic_sha256")
    if not isinstance(diagnostic_hash, str):
        raise ManifestError("trace diagnostic is missing its SHA-256")
    return {
        "trace_id": trace.trace_id,
        "split": trace.split,
        "organization": trace.organization,
        "application": trace.application,
        "dataset": trace.dataset,
        "provenance_url": trace.provenance_url,
        "license_ref": trace.license_ref,
        "path": str(trace.path),
        "trace_type": trace.trace_type,
        "origin_sha256": trace.origin_sha256,
        "start_request": trace.start_request,
        "warmup_seconds": trace.warmup_seconds,
        "max_requests": trace.max_requests,
        "working_set_bytes": trace.working_set_bytes,
        "sha256": trace.sha256,
        "size_bytes": trace.size_bytes,
        "diagnostic_sha256": diagnostic_hash,
        "diagnostics": diagnostic,
    }


def _publish_directory(staging: Path, output: Path) -> None:
    if os.path.lexists(output):
        raise ManifestError(f"refusing to replace output directory: {output}")
    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError as error:
        raise ManifestError("atomic no-replace directory publication is unavailable") from error
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    result = renameat2(-100, os.fsencode(staging), -100, os.fsencode(output), 1)
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        raise ManifestError(f"refusing to replace output directory: {output}")
    raise OSError(error_number, os.strerror(error_number), output)


def _directory_identity_from_stat(metadata: os.stat_result) -> tuple[int, int]:
    if not stat.S_ISDIR(metadata.st_mode):
        raise ManifestError("owned path is not a directory")
    return metadata.st_dev, metadata.st_ino


def _open_owned_directory(path: Path) -> _OwnedDirectory:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path, flags)
    try:
        identity = _directory_identity_from_stat(os.fstat(descriptor))
    except BaseException:
        os.close(descriptor)
        raise
    return _OwnedDirectory(path=path, descriptor=descriptor, identity=identity)


def _create_owned_child(parent: _OwnedDirectory, name: str) -> _OwnedDirectory:
    if parent.path is None:
        raise ManifestError("owned parent directory is unavailable")
    os.mkdir(name, mode=0o700, dir_fd=parent.descriptor)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(name, flags, dir_fd=parent.descriptor)
    try:
        identity = _directory_identity_from_stat(os.fstat(descriptor))
    except BaseException:
        os.close(descriptor)
        raise
    return _OwnedDirectory(
        path=parent.path / name,
        descriptor=descriptor,
        identity=identity,
    )


def _directory_state(path: Path, identity: tuple[int, int] | None) -> str:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return "missing"
    if identity is not None and stat.S_ISDIR(metadata.st_mode):
        if (metadata.st_dev, metadata.st_ino) == identity:
            return "owned"
    return "replaced"


def _read_owned_manifest(directory: _OwnedDirectory, name: str) -> _OwnedManifest:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, dir_fd=directory.descriptor)
    digest = hashlib.sha256()
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ManifestError(f"owned manifest is not a regular file: {name}")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return _OwnedManifest(
        name=name,
        identity=(metadata.st_dev, metadata.st_ino),
        sha256=digest.hexdigest(),
    )


def _capture_owned_manifest(directory: _OwnedDirectory, name: str) -> None:
    entries = os.listdir(directory.descriptor)
    if entries != [name]:
        raise ManifestError(f"owned staging entries mismatch: {entries}")
    if name in directory.files:
        _refresh_owned_file(directory, name)
    else:
        directory.files[name] = _read_owned_manifest(directory, name)


def _refresh_owned_file(directory: _OwnedDirectory, name: str) -> _OwnedManifest:
    expected = directory.files[name]
    observed = _read_owned_manifest(directory, name)
    if observed.identity != expected.identity:
        raise ManifestError(f"owned apparatus file changed: {name}")
    if expected.sha256 is not None and observed.sha256 != expected.sha256:
        raise ManifestError(f"owned apparatus file changed: {name}")
    directory.files[name] = observed
    return observed


def _write_owned_record(
    directory: _OwnedDirectory,
    name: str,
    value: dict[str, object],
    hash_field: str,
) -> None:
    value[hash_field] = record_sha256(value, hash_field)
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    temporary_name = f".{name}.{secrets.token_hex(8)}.tmp"
    descriptor = os.open(
        temporary_name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
        dir_fd=directory.descriptor,
    )
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        os.close(descriptor)
        raise ManifestError(f"owned temporary is not a regular file: {temporary_name}")
    directory.files[temporary_name] = _OwnedManifest(
        name=temporary_name,
        identity=(metadata.st_dev, metadata.st_ino),
        sha256=None,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        temporary_receipt = _refresh_owned_file(directory, temporary_name)
        try:
            os.link(
                temporary_name,
                name,
                src_dir_fd=directory.descriptor,
                dst_dir_fd=directory.descriptor,
                follow_symlinks=False,
            )
        except FileExistsError as error:
            raise ManifestError(f"refusing to replace owned manifest: {name}") from error
        directory.files[name] = _OwnedManifest(
            name=name,
            identity=temporary_receipt.identity,
            sha256=temporary_receipt.sha256,
        )
        os.fsync(directory.descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            _refresh_owned_file(directory, temporary_name)
            os.unlink(temporary_name, dir_fd=directory.descriptor)
        except FileNotFoundError:
            directory.files.pop(temporary_name, None)
        else:
            directory.files.pop(temporary_name, None)


def _cleanup_owned_directory(directory: _OwnedDirectory) -> str | None:
    path = directory.path
    if path is None:
        return None
    state = _directory_state(path, directory.identity)
    if state == "missing":
        directory.path = None
        return None
    if state == "replaced":
        return f"cleanup conflict: replaced path: {path}"
    try:
        entries = os.listdir(directory.descriptor)
        if set(entries) != set(directory.files):
            return f"cleanup conflict: unexpected entries in {path}: {entries}"
        for name, expected in directory.files.items():
            observed = _read_owned_manifest(directory, name)
            if observed != expected:
                return f"cleanup conflict: apparatus file changed in {path}: {name}"
        for name in list(directory.files):
            os.unlink(name, dir_fd=directory.descriptor)
            directory.files.pop(name)
        state = _directory_state(path, directory.identity)
        if state == "missing":
            directory.path = None
            return None
        if state == "replaced":
            return f"cleanup conflict: replaced path: {path}"
        os.rmdir(path)
        directory.path = None
        return None
    except OSError as error:
        return f"cleanup failure for {path}: {error}"


def _cleanup_owned_directories(
    directories: list[_OwnedDirectory],
) -> list[str]:
    issues = []
    for directory in reversed(directories):
        try:
            issue = _cleanup_owned_directory(directory)
        except BaseException as error:
            location = directory.path or Path("<unlinked-owned-directory>")
            issue = f"cleanup failure for {location}: {error}"
        if issue is not None:
            issues.append(issue)
    return issues


def _publish_owned_directory(directory: _OwnedDirectory, output: Path) -> None:
    staging = directory.path
    if staging is None:
        raise ManifestError("owned staging directory is unavailable")
    if _directory_state(staging, directory.identity) != "owned":
        raise ManifestError(f"staging ownership conflict: {staging}")
    try:
        _publish_directory(staging, output)
    except BaseException:
        if _directory_state(output, directory.identity) == "owned":
            directory.path = output
        elif _directory_state(staging, directory.identity) == "missing":
            directory.path = output
        raise
    directory.path = output
    if _directory_state(output, directory.identity) != "owned":
        raise ManifestError(f"published output ownership conflict: {output}")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def freeze_manifests(
    candidate_path: Path,
    task_output: Path,
    host_output: Path,
) -> tuple[dict[str, object], dict[str, object]]:
    try:
        candidate = Path(candidate_path).resolve(strict=True)
        task = _output_path(Path(task_output))
        host = _output_path(Path(host_output))
        if _paths_overlap(task, host):
            raise ManifestError("task and host outputs must not overlap")
        if _paths_overlap(candidate, task) or _paths_overlap(candidate, host):
            raise ManifestError("candidate and outputs must not overlap")
        raw_candidate = load_candidate_object(candidate)
        portfolio = Portfolio.from_candidate(raw_candidate)
        for trace in portfolio.traces:
            if _paths_overlap(trace.path, task) or _paths_overlap(trace.path, host):
                raise ManifestError("trace input and outputs must not overlap")
        _validate_splits(portfolio)
    except ManifestError:
        raise
    except (OSError, ValueError) as error:
        raise ManifestError(str(error)) from error

    owned_directories: list[_OwnedDirectory] = []
    cleanup_performed = False
    completed = False
    try:
        task.parent.mkdir(parents=True, exist_ok=True)
        host.parent.mkdir(parents=True, exist_ok=True)
        task_stage_path = Path(
            tempfile.mkdtemp(prefix=f".{task.name}.", dir=task.parent)
        )
        task_stage = _open_owned_directory(task_stage_path)
        owned_directories.append(task_stage)
        host_stage_path = Path(
            tempfile.mkdtemp(prefix=f".{host.name}.", dir=host.parent)
        )
        host_stage = _open_owned_directory(host_stage_path)
        owned_directories.append(host_stage)
        public_scan = _create_owned_child(task_stage, ".oracle-scan")
        owned_directories.append(public_scan)
        private_scan = _create_owned_child(host_stage, ".oracle-scan")
        owned_directories.append(private_scan)

        frozen_traces: list[dict[str, object]] = []
        for trace in portfolio.traces:
            scan = private_scan if trace.split == "r3" else public_scan
            if scan.path is None:
                raise ManifestError("owned scan directory is unavailable")
            diagnostic = scan_oracle_general(
                trace,
                scan.path,
                temporary_descriptor=scan.descriptor,
            )
            frozen_traces.append(_trace_record(trace, diagnostic))
        for scan in (public_scan, private_scan):
            issue = _cleanup_owned_directory(scan)
            if issue is not None:
                raise ManifestError(issue)

        public_traces = [
            item for item in frozen_traces if item["split"] in {"dev", "visible"}
        ]
        private_traces = [item for item in frozen_traces if item["split"] == "r3"]
        host_manifest: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "source_commit": portfolio.source_commit,
            "cache_fractions": list(portfolio.cache_fractions),
            "traces": private_traces,
        }
        host_manifest["manifest_sha256"] = record_sha256(
            host_manifest, "manifest_sha256"
        )
        task_manifest: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "source_commit": portfolio.source_commit,
            "cache_fractions": list(portfolio.cache_fractions),
            "traces": public_traces,
            "r3_commitment_sha256": host_manifest["manifest_sha256"],
        }
        _write_owned_record(host_stage, "r3.json", host_manifest, "manifest_sha256")
        _capture_owned_manifest(host_stage, "r3.json")
        _write_owned_record(task_stage, "task.json", task_manifest, "manifest_sha256")
        _capture_owned_manifest(task_stage, "task.json")

        _publish_owned_directory(task_stage, task)
        _publish_owned_directory(host_stage, host)
        for parent in {task.parent, host.parent}:
            _fsync_directory(parent)
        completed = True
        return task_manifest, host_manifest
    except BaseException as error:
        publication_started = any(
            directory.path in {task, host} for directory in owned_directories
        )
        cleanup_issues = _cleanup_owned_directories(owned_directories)
        cleanup_performed = True
        if isinstance(error, (OSError, ValueError)):
            message = f"manifest publication failed: {error}"
            if cleanup_issues:
                label = (
                    "rollback conflict"
                    if publication_started
                    else "temporary directory cleanup failed"
                )
                message += f"; {label}: " + "; ".join(cleanup_issues)
            raise ManifestError(message) from error
        raise
    finally:
        try:
            if not completed and not cleanup_performed:
                _cleanup_owned_directories(owned_directories)
        finally:
            for directory in reversed(owned_directories):
                try:
                    os.close(directory.descriptor)
                except OSError:
                    pass
