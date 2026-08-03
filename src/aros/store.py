"""Small durable-file primitives used by the AROS kernel."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


_thread_locks_guard = threading.Lock()
_thread_locks: dict[str, threading.RLock] = {}
_link = os.link
ENVIRONMENT_ALLOWLIST = (
    "CUDA_VISIBLE_DEVICES",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "MKL_NUM_THREADS",
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "PATH",
    "PYTHONPATH",
    "TZ",
)
FINAL_IDENTITY_FIELDS = (
    "run_id",
    "repository_ref",
    "base_commit",
    "candidate_commit",
    "argv",
    "cwd",
    "timeout_seconds",
    "idempotency_key",
    "security_profile",
    "writable_paths",
    "network_policy",
    "process_policy",
    "environment_policy",
    "isolation_limits",
    "environment_ref",
    "environment_sha256",
    "manifest_sha256",
    "actor",
    "question_refs",
    "experiment_ref",
    "prediction_ref",
    "evaluator_ref",
    "evaluator_version",
    "seed",
    "dataset_ref",
    "resource_request",
    "budget",
    "output_paths",
    "success_exit_codes",
)


def canonical_json_bytes(value: Any) -> bytes:
    """Return the stable JSON representation used for hashes."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def json_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def manifest_sha256(manifest: dict[str, Any]) -> str:
    payload = dict(manifest)
    payload.pop("manifest_sha256", None)
    return json_sha256(payload)


def environment_fingerprint() -> tuple[dict[str, object], str]:
    values = {
        key: os.environ[key]
        for key in ENVIRONMENT_ALLOWLIST
        if key in os.environ
    }
    return (
        {"kind": "allowlist-fingerprint-v1", "keys": sorted(values)},
        json_sha256(values),
    )


def environment_sha256() -> str:
    return environment_fingerprint()[1]


def process_start_token(pid: int) -> str | None:
    if pid <= 1:
        return None
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        fields_after_name = raw.rsplit(")", 1)[1].split()
        return f"linux-proc-start:{fields_after_name[19]}"
    except (OSError, IndexError, ValueError):
        return None


def final_identity(manifest: dict[str, Any]) -> dict[str, Any]:
    return {field: manifest[field] for field in FINAL_IDENTITY_FIELDS}


def read_json(path: str | Path) -> Any:
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def atomic_write_json(path: str | Path, value: Any) -> None:
    """Durably replace *path* with one complete JSON document."""
    target = Path(path)
    _durable_mkdir(target.parent)
    payload = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    fd, temporary = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
    )
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, target)
        _fsync_directory(target.parent)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def create_json(path: str | Path, value: Any) -> bool:
    """Create an immutable JSON document, returning false if it exists."""
    target = Path(path)
    _durable_mkdir(target.parent)
    payload = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    fd, temporary = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
    )
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            _link(temporary_path, target, follow_symlinks=False)
        except FileExistsError:
            return False
        _fsync_directory(target.parent)
        return True
    finally:
        try:
            temporary_path.unlink(missing_ok=True)
        finally:
            _fsync_directory(target.parent)


@contextmanager
def file_lock(path: str | Path) -> Iterator[None]:
    """Hold a process- and thread-safe exclusive lock for *path*."""
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    key = str(lock_path.resolve())
    with _thread_locks_guard:
        thread_lock = _thread_locks.setdefault(key, threading.RLock())
    with thread_lock:
        with lock_path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _durable_mkdir(path: Path) -> None:
    missing: list[Path] = []
    current = path
    while not current.exists():
        missing.append(current)
        parent = current.parent
        if parent == current:
            break
        current = parent
    path.mkdir(parents=True, exist_ok=True)
    for directory in reversed(missing):
        _fsync_directory(directory.parent)
        _fsync_directory(directory)
