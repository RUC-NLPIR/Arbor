"""Small durable-file primitives used by the AROS kernel."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import stat
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
    identity = {field: manifest[field] for field in FINAL_IDENTITY_FIELDS}
    if "execution_bundle" in manifest:
        identity["execution_bundle"] = manifest["execution_bundle"]
    return identity


def read_json(path: str | Path) -> Any:
    """Read one securely bound JSON file with standard decoder compatibility."""
    return _read_json(path, strict=False, repair_aliases=True)


def read_json_strict(path: str | Path) -> Any:
    """Read one securely bound JSON file, rejecting ambiguous JSON values."""
    return _read_json(path, strict=True, repair_aliases=True)


def read_json_strict_no_repair(path: str | Path) -> Any:
    """Strictly read one JSON authority without repairing crash aliases."""
    return _read_json(path, strict=True, repair_aliases=False)


def _read_json(
    path: str | Path,
    *,
    strict: bool,
    repair_aliases: bool,
) -> Any:
    target = Path(path)
    metadata = target.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"JSON path must be a regular file: {target}")
    identity = (metadata.st_dev, metadata.st_ino)
    if repair_aliases and metadata.st_nlink > 1:
        _remove_json_temp_aliases(target, identity)
        metadata = target.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or (metadata.st_dev, metadata.st_ino) != identity
        ):
            raise ValueError(f"JSON path changed during recovery: {target}")
    if metadata.st_nlink != 1:
        raise ValueError(
            f"JSON path must be a create-once single-link regular file: {target}"
        )

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(target, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError(f"JSON path must be a regular file: {target}")
        if (opened.st_dev, opened.st_ino) != identity:
            raise ValueError(f"JSON path changed while opening: {target}")
        if opened.st_nlink != 1:
            raise ValueError(
                f"JSON path must be a create-once single-link regular file: {target}"
            )
        with os.fdopen(descriptor, "r", encoding="utf-8", closefd=False) as handle:
            if strict:
                value = json.load(
                    handle,
                    object_pairs_hook=_unique_json_object,
                    parse_constant=_reject_json_constant,
                    parse_float=_strict_json_float,
                )
            else:
                value = json.load(handle)
    finally:
        os.close(descriptor)

    observed = target.lstat()
    if (
        not stat.S_ISREG(observed.st_mode)
        or (observed.st_dev, observed.st_ino) != identity
    ):
        raise ValueError(f"JSON path changed while reading: {target}")
    if observed.st_nlink != 1:
        raise ValueError(
            f"JSON path must be a create-once single-link regular file: {target}"
        )
    return value


def _strict_json_loads(raw: str | bytes | bytearray) -> Any:
    if isinstance(raw, (bytes, bytearray)):
        raw = bytes(raw).decode("utf-8")
    return json.loads(
        raw,
        object_pairs_hook=_unique_json_object,
        parse_constant=_reject_json_constant,
        parse_float=_strict_json_float,
    )


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"JSON contains non-finite number: {value}")


def _strict_json_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"JSON contains non-finite number: {value}")
    return number


def atomic_write_json(path: str | Path, value: Any) -> None:
    """Durably replace *path* with one complete JSON document."""
    target = Path(path)
    _durable_mkdir(target.parent)
    payload = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    fd, temporary = tempfile.mkstemp(
        prefix=_json_temp_prefix(target),
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
    try:
        target.lstat()
    except FileNotFoundError:
        pass
    else:
        return False
    _durable_mkdir(target.parent)
    payload = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    fd, temporary = tempfile.mkstemp(
        prefix=_json_temp_prefix(target),
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


def _json_temp_prefix(target: Path) -> str:
    digest = hashlib.sha256(os.fsencode(target.name)).hexdigest()
    return f".aros-json-{digest}."


def _remove_json_temp_aliases(
    target: Path,
    identity: tuple[int, int],
) -> None:
    prefix = _json_temp_prefix(target)
    removed = False
    for candidate in target.parent.iterdir():
        if (
            not candidate.name.startswith(prefix)
            or not candidate.name.endswith(".tmp")
            or len(candidate.name) <= len(prefix) + len(".tmp")
        ):
            continue
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        if (
            not stat.S_ISREG(metadata.st_mode)
            or (metadata.st_dev, metadata.st_ino) != identity
        ):
            continue
        try:
            candidate.unlink()
        except FileNotFoundError:
            continue
        removed = True
    if removed:
        _fsync_directory(target.parent)


def _durable_mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _fsync_directory_chain(path)


def _fsync_directory_chain(path: Path) -> None:
    directory = path.absolute()
    device = directory.stat().st_dev
    while True:
        _fsync_directory(directory)
        parent = directory.parent
        if parent == directory or parent.stat().st_dev != device:
            return
        directory = parent
