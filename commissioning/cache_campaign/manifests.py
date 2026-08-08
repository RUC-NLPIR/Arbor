from __future__ import annotations

import ctypes
import errno
import os
import shutil
import stat
import tempfile
from pathlib import Path

from . import SCHEMA_VERSION
from .oracle import scan_oracle_general
from .records import (
    ContractError,
    Portfolio,
    TraceWindow,
    load_object,
    record_sha256,
    write_new_record,
)


class ManifestError(ContractError):
    pass


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
        source = (item.organization, item.application)
        dev_source = (dev_item.organization, dev_item.application)
        if source != dev_source:
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


def _directory_identity(path: Path) -> tuple[int, int]:
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise ManifestError(f"staging path is not a directory: {path}")
    return metadata.st_dev, metadata.st_ino


def _same_directory(path: Path, identity: tuple[int, int] | None) -> bool:
    if identity is None:
        return False
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISDIR(metadata.st_mode) and (metadata.st_dev, metadata.st_ino) == identity


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
        raw_candidate = load_object(candidate)
        portfolio = Portfolio.from_candidate(raw_candidate)
        for trace in portfolio.traces:
            if _paths_overlap(trace.path, task) or _paths_overlap(trace.path, host):
                raise ManifestError("trace input and outputs must not overlap")
        _validate_splits(portfolio)
    except ManifestError:
        raise
    except (OSError, ValueError) as error:
        raise ManifestError(str(error)) from error

    task_stage: Path | None = None
    host_stage: Path | None = None
    task_identity: tuple[int, int] | None = None
    host_identity: tuple[int, int] | None = None
    try:
        task.parent.mkdir(parents=True, exist_ok=True)
        host.parent.mkdir(parents=True, exist_ok=True)
        task_stage = Path(tempfile.mkdtemp(prefix=f".{task.name}.", dir=task.parent))
        host_stage = Path(tempfile.mkdtemp(prefix=f".{host.name}.", dir=host.parent))
        public_scan_directory = task_stage / ".oracle-scan"
        private_scan_directory = host_stage / ".oracle-scan"
        public_scan_directory.mkdir(mode=0o700)
        private_scan_directory.mkdir(mode=0o700)

        frozen_traces: list[dict[str, object]] = []
        for trace in portfolio.traces:
            scan_directory = (
                private_scan_directory if trace.split == "r3" else public_scan_directory
            )
            diagnostic = scan_oracle_general(trace, scan_directory)
            frozen_traces.append(_trace_record(trace, diagnostic))
        public_scan_directory.rmdir()
        private_scan_directory.rmdir()

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
        write_new_record(host_stage / "r3.json", host_manifest, "manifest_sha256")
        write_new_record(task_stage / "task.json", task_manifest, "manifest_sha256")

        task_identity = _directory_identity(task_stage)
        host_identity = _directory_identity(host_stage)
        _publish_directory(task_stage, task)
        _publish_directory(host_stage, host)
        for parent in {task.parent, host.parent}:
            _fsync_directory(parent)
        return task_manifest, host_manifest
    except BaseException as error:
        rollback_errors: list[OSError] = []
        for output, identity in ((host, host_identity), (task, task_identity)):
            if not _same_directory(output, identity):
                continue
            try:
                shutil.rmtree(output)
            except OSError as cleanup_error:
                rollback_errors.append(cleanup_error)
        if isinstance(error, (OSError, ValueError)):
            message = f"manifest publication failed: {error}"
            if rollback_errors:
                message += "; rollback failed: " + "; ".join(
                    str(item) for item in rollback_errors
                )
            raise ManifestError(message) from error
        raise
    finally:
        cleanup_errors: list[OSError] = []
        for staging in (task_stage, host_stage):
            if staging is None or not os.path.lexists(staging):
                continue
            try:
                shutil.rmtree(staging)
            except OSError as cleanup_error:
                cleanup_errors.append(cleanup_error)
        if cleanup_errors:
            detail = "; ".join(str(item) for item in cleanup_errors)
            raise ManifestError(f"temporary directory cleanup failed: {detail}") from cleanup_errors[0]
