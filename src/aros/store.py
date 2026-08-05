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
from dataclasses import dataclass
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


@dataclass(frozen=True)
class _AnchoredDirectory:
    descriptor: int
    parent: tuple[str, ...]
    name: str
    identity: tuple[int, int, int]


@dataclass(frozen=True)
class _AnchoredFile:
    descriptor: int
    parent: tuple[str, ...]
    name: str
    identity: tuple[int, int, int, int, int, int, int]
    payload: bytes


class AnchoredReadError(ValueError):
    """Raised when an anchored workspace snapshot changes or is unsafe."""


class AnchoredWorkspaceReader:
    """Read one immutable multi-file snapshot through anchored dirfds."""

    def __init__(self, root: str | Path):
        supplied = Path(root).expanduser().absolute()
        try:
            resolved = supplied.resolve(strict=True)
        except OSError as error:
            raise AnchoredReadError(
                f"workspace root does not exist: {supplied}"
            ) from error
        if supplied != resolved:
            raise AnchoredReadError(f"workspace root must be exact: {supplied}")
        self.root = resolved
        self._parent_path = resolved.parent
        self._parent_descriptor: int | None = None
        self._root_descriptor: int | None = None
        self._parent_identity: tuple[int, int, int] | None = None
        self._root_identity: tuple[int, int, int] | None = None
        self._directories: dict[tuple[str, ...], _AnchoredDirectory] = {}
        self._files: dict[tuple[str, ...], _AnchoredFile] = {}
        self._json: dict[tuple[str, ...], Any] = {}
        self._closed = False
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        try:
            self._parent_descriptor = os.open(self._parent_path, flags)
            parent = os.fstat(self._parent_descriptor)
            self._parent_identity = _anchored_directory_identity(parent)
            observed_parent = self._parent_path.lstat()
            if _anchored_directory_identity(observed_parent) != self._parent_identity:
                raise AnchoredReadError(
                    "workspace parent identity changed while opening"
                )
            self._root_descriptor = os.open(
                self.root.name,
                flags,
                dir_fd=self._parent_descriptor,
            )
            root_metadata = os.fstat(self._root_descriptor)
            self._root_identity = _anchored_directory_identity(root_metadata)
            observed_root = os.stat(
                self.root.name,
                dir_fd=self._parent_descriptor,
                follow_symlinks=False,
            )
            if _anchored_directory_identity(observed_root) != self._root_identity:
                raise AnchoredReadError("workspace root identity changed while opening")
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> AnchoredWorkspaceReader:
        if self._closed:
            raise AnchoredReadError("anchored workspace reader is closed")
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def __call__(self, path: str | Path) -> Any:
        return self.read_json(path)

    def read_json(self, path: str | Path) -> Any:
        parts = self._relative_parts(path)
        if parts not in self._json:
            self._json[parts] = _strict_json_loads(self._open_file(parts).payload)
        return self._json[parts]

    def read_bytes(self, path: str | Path) -> bytes:
        return self._open_file(self._relative_parts(path)).payload

    def require_file(self, path: str | Path) -> None:
        self._open_file(self._relative_parts(path))

    def require_directory(self, path: str | Path) -> None:
        self._directory_descriptor(self._relative_parts(path))

    def require_git_marker(self) -> None:
        self._require_open()
        assert self._root_descriptor is not None
        metadata = os.stat(
            ".git",
            dir_fd=self._root_descriptor,
            follow_symlinks=False,
        )
        if stat.S_ISDIR(metadata.st_mode):
            self.require_directory(".git")
        elif stat.S_ISREG(metadata.st_mode):
            self.require_file(".git")
        else:
            raise AnchoredReadError("workspace Git marker has invalid identity")

    def revalidate(self) -> None:
        self._require_open()
        assert self._parent_descriptor is not None
        assert self._root_descriptor is not None
        assert self._parent_identity is not None
        assert self._root_identity is not None
        if (
            _anchored_directory_identity(os.fstat(self._parent_descriptor))
            != self._parent_identity
            or _anchored_directory_identity(self._parent_path.lstat())
            != self._parent_identity
        ):
            raise AnchoredReadError("workspace parent identity changed")
        observed_root = os.stat(
            self.root.name,
            dir_fd=self._parent_descriptor,
            follow_symlinks=False,
        )
        if (
            _anchored_directory_identity(os.fstat(self._root_descriptor))
            != self._root_identity
            or _anchored_directory_identity(observed_root) != self._root_identity
        ):
            raise AnchoredReadError("workspace root identity changed")
        for directory in self._directories.values():
            parent_descriptor = self._directory_descriptor(directory.parent)
            observed = os.stat(
                directory.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if (
                _anchored_directory_identity(os.fstat(directory.descriptor))
                != directory.identity
                or _anchored_directory_identity(observed) != directory.identity
            ):
                raise AnchoredReadError("workspace directory identity changed")
        for anchored in self._files.values():
            parent_descriptor = self._directory_descriptor(anchored.parent)
            observed = os.stat(
                anchored.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if (
                _anchored_file_identity(os.fstat(anchored.descriptor))
                != anchored.identity
                or _anchored_file_identity(observed) != anchored.identity
                or _read_anchored_descriptor(anchored.descriptor) != anchored.payload
            ):
                raise AnchoredReadError("workspace file identity or bytes changed")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        descriptors = [item.descriptor for item in self._files.values()]
        descriptors.extend(
            item.descriptor
            for _path, item in sorted(
                self._directories.items(),
                key=lambda item: len(item[0]),
                reverse=True,
            )
        )
        if self._root_descriptor is not None:
            descriptors.append(self._root_descriptor)
        if self._parent_descriptor is not None:
            descriptors.append(self._parent_descriptor)
        for descriptor in descriptors:
            try:
                os.close(descriptor)
            except OSError:
                pass

    def _open_file(self, parts: tuple[str, ...]) -> _AnchoredFile:
        self._require_open()
        existing = self._files.get(parts)
        if existing is not None:
            return existing
        parent = parts[:-1]
        name = parts[-1]
        parent_descriptor = self._directory_descriptor(parent)
        before = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise AnchoredReadError(
                "workspace authority path must be a single-link plain file"
            )
        flags = (
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
        try:
            opened = os.fstat(descriptor)
            identity = _anchored_file_identity(opened)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or _anchored_file_identity(before) != identity
            ):
                raise AnchoredReadError(
                    "workspace authority must be a single-link plain file"
                )
            payload = _read_anchored_descriptor(descriptor)
            after = os.fstat(descriptor)
            observed = os.stat(
                name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if (
                _anchored_file_identity(after) != identity
                or _anchored_file_identity(observed) != identity
            ):
                raise AnchoredReadError(
                    "workspace file identity changed while reading"
                )
        except BaseException:
            os.close(descriptor)
            raise
        anchored = _AnchoredFile(descriptor, parent, name, identity, payload)
        self._files[parts] = anchored
        return anchored

    def _directory_descriptor(self, parts: tuple[str, ...]) -> int:
        self._require_open()
        assert self._root_descriptor is not None
        if not parts:
            return self._root_descriptor
        current: tuple[str, ...] = ()
        descriptor = self._root_descriptor
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        for name in parts:
            parent = current
            current = (*current, name)
            existing = self._directories.get(current)
            if existing is not None:
                descriptor = existing.descriptor
                continue
            before = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if not stat.S_ISDIR(before.st_mode):
                raise AnchoredReadError(
                    "workspace path parent has invalid identity"
                )
            child = os.open(name, flags, dir_fd=descriptor)
            try:
                opened = os.fstat(child)
                identity = _anchored_directory_identity(opened)
                if (
                    not stat.S_ISDIR(opened.st_mode)
                    or _anchored_directory_identity(before) != identity
                ):
                    raise AnchoredReadError(
                        "workspace path parent must be a plain directory"
                    )
            except BaseException:
                os.close(child)
                raise
            self._directories[current] = _AnchoredDirectory(
                child,
                parent,
                name,
                identity,
            )
            descriptor = child
        return descriptor

    def _relative_parts(self, path: str | Path) -> tuple[str, ...]:
        candidate = Path(path)
        if candidate.is_absolute():
            try:
                candidate = candidate.absolute().relative_to(self.root)
            except ValueError as error:
                raise AnchoredReadError(
                    f"workspace path escapes anchored root: {path}"
                ) from error
        parts = candidate.parts
        if not parts or any(part in {"", ".", ".."} for part in parts):
            raise AnchoredReadError(
                f"workspace path must be canonical and relative: {path}"
            )
        return parts

    def _require_open(self) -> None:
        if self._closed:
            raise AnchoredReadError("anchored workspace reader is closed")


def _anchored_directory_identity(metadata: os.stat_result) -> tuple[int, int, int]:
    return metadata.st_dev, metadata.st_ino, metadata.st_mode


def _anchored_file_identity(
    metadata: os.stat_result,
) -> tuple[int, int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _read_anchored_descriptor(descriptor: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while True:
        chunk = os.read(descriptor, 65_536)
        if not chunk:
            break
        chunks.append(chunk)
    os.lseek(descriptor, 0, os.SEEK_SET)
    return b"".join(chunks)


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
