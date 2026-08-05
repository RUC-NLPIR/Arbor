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


_MAX_ANCHORED_JSON_BYTES = 1024 * 1024
_DirectoryKey = tuple[str, ...]
_DirectoryIdentity = tuple[int, int, int]
_FileIdentity = tuple[int, int, int, int, int, int, int]


@dataclass(frozen=True)
class _AnchoredDirectory:
    descriptor: int
    parent: _DirectoryKey | None
    name: str
    identity: _DirectoryIdentity


@dataclass
class _AnchoredFile:
    descriptor: int
    parent: _DirectoryKey
    name: str
    identity: _FileIdentity
    sha256: str | None = None


@dataclass(frozen=True)
class _AnchoredListing:
    names: tuple[str, ...]
    entries: tuple[tuple[str, _FileIdentity], ...]


class AnchoredReadError(ValueError):
    """Raised when an anchored workspace snapshot changes or is unsafe."""

    def __init__(
        self,
        message: str,
        *,
        original_error: BaseException | None = None,
        revalidation_error: BaseException | None = None,
    ):
        super().__init__(message)
        self.original_error = original_error
        self.revalidation_error = revalidation_error


class AnchoredWorkspaceReader:
    """Hold one descriptor-anchored multi-file workspace transaction."""

    def __init__(self, root: str | Path):
        supplied = Path(root).expanduser()
        if not supplied.is_absolute():
            supplied = Path.cwd() / supplied
        parts = tuple(part for part in supplied.parts if part != os.sep)
        if not parts or any(part in {"", ".", ".."} for part in parts):
            raise AnchoredReadError(f"workspace root must be canonical: {supplied}")
        self.root = Path(os.sep, *parts)
        self._workspace_key = parts
        self._directories: dict[_DirectoryKey, _AnchoredDirectory] = {}
        self._files: dict[tuple[str, ...], _AnchoredFile] = {}
        self._json: dict[tuple[str, ...], Any] = {}
        self._listings: dict[_DirectoryKey, _AnchoredListing] = {}
        self._stats: dict[tuple[str, ...], tuple[os.stat_result, _FileIdentity]] = {}
        self._closed = False
        flags = _anchored_directory_flags()
        try:
            descriptor = os.open(os.sep, flags)
            metadata = os.fstat(descriptor)
            self._directories[()] = _AnchoredDirectory(
                descriptor,
                None,
                os.sep,
                _anchored_directory_identity(metadata),
            )
            self._directory_descriptor(self._workspace_key)
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> AnchoredWorkspaceReader:
        self._require_open()
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        original_error: BaseException | None,
        _traceback: object,
    ) -> None:
        revalidation_error: BaseException | None = None
        try:
            self.revalidate()
        except BaseException as error:
            revalidation_error = error
        finally:
            self.close()
        if revalidation_error is not None:
            if original_error is None:
                raise revalidation_error
            raise AnchoredReadError(
                "workspace changed while handling another error",
                original_error=original_error,
                revalidation_error=revalidation_error,
            ) from revalidation_error

    def __call__(self, path: str | Path) -> Any:
        return self.read_json(path)

    def read_json(self, path: str | Path) -> Any:
        key = self._workspace_file_key(path)
        if key in self._json:
            return self._json[key]
        anchored = self._open_file(key)
        if anchored.identity[4] > _MAX_ANCHORED_JSON_BYTES:
            self._stream_file(anchored, capture=False)
            raise AnchoredReadError("workspace JSON authority exceeds 1 MiB")
        payload, _size, _digest = self._stream_file(anchored, capture=True)
        assert payload is not None
        value = _strict_json_loads(payload)
        self._json[key] = value
        return value

    def require_file(self, path: str | Path) -> None:
        self._stream_file(self._open_file(self._workspace_file_key(path)), capture=False)

    def require_directory(self, path: str | Path) -> None:
        self._directory_descriptor(self._workspace_directory_key(path))

    def require_repository(
        self,
        root: str | Path,
        git_dir: str | Path,
        common_dir: str | Path,
    ) -> None:
        if self._absolute_key(root) != self._workspace_key:
            raise AnchoredReadError("repository root differs from anchored workspace")
        self.require_git_marker()
        self._directory_descriptor(self._absolute_key(git_dir))
        self._directory_descriptor(self._absolute_key(common_dir))

    def require_git_marker(self) -> None:
        metadata = self.lstat(".git")
        if stat.S_ISDIR(metadata.st_mode):
            self.require_directory(".git")
        elif stat.S_ISREG(metadata.st_mode):
            self.require_file(".git")
        else:
            raise AnchoredReadError("workspace Git marker has invalid identity")

    def listdir(self, path: str | Path) -> tuple[str, ...]:
        key = self._workspace_directory_key(path)
        existing = self._listings.get(key)
        if existing is not None:
            return existing.names
        descriptor = self._directory_descriptor(key)
        names = tuple(sorted(os.listdir(descriptor)))
        entries = tuple(
            (
                name,
                _anchored_file_identity(
                    os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                ),
            )
            for name in names
        )
        self._listings[key] = _AnchoredListing(names, entries)
        return names

    def lstat(self, path: str | Path) -> os.stat_result:
        key = self._workspace_file_key(path)
        existing = self._stats.get(key)
        if existing is not None:
            return existing[0]
        parent = key[:-1]
        metadata = os.stat(
            key[-1],
            dir_fd=self._directory_descriptor(parent),
            follow_symlinks=False,
        )
        self._stats[key] = (metadata, _anchored_file_identity(metadata))
        return metadata

    def verify_stream(
        self,
        path: str | Path,
        *,
        expected_size: int,
        expected_sha256: str,
        capture_limit: int | None,
    ) -> bytes | None:
        if type(expected_size) is not int or expected_size < 0:
            raise AnchoredReadError("expected stream size must be nonnegative")
        if capture_limit is not None and (
            type(capture_limit) is not int or capture_limit < 0
        ):
            raise AnchoredReadError("capture limit must be nonnegative or null")
        anchored = self._open_file(self._workspace_file_key(path))
        capture = capture_limit is not None and expected_size <= capture_limit
        payload, size, digest = self._stream_file(anchored, capture=capture)
        if size != expected_size or digest != expected_sha256:
            raise AnchoredReadError("workspace stream differs from receipt")
        return payload

    def revalidate(self) -> None:
        self._require_open()
        for key, directory in tuple(self._directories.items()):
            current = _anchored_directory_identity(os.fstat(directory.descriptor))
            if directory.parent is None:
                observed = _anchored_directory_identity(os.stat(os.sep))
            else:
                observed = _anchored_directory_identity(
                    os.stat(
                        directory.name,
                        dir_fd=self._directory_descriptor(directory.parent),
                        follow_symlinks=False,
                    )
                )
            if current != directory.identity or observed != directory.identity:
                raise AnchoredReadError("workspace directory identity changed")
            listing = self._listings.get(key)
            if listing is not None:
                names = tuple(sorted(os.listdir(directory.descriptor)))
                entries = tuple(
                    (
                        name,
                        _anchored_file_identity(
                            os.stat(
                                name,
                                dir_fd=directory.descriptor,
                                follow_symlinks=False,
                            )
                        ),
                    )
                    for name in names
                )
                if names != listing.names or entries != listing.entries:
                    raise AnchoredReadError("workspace directory listing changed")
        for key, (metadata, identity) in self._stats.items():
            observed = os.stat(
                key[-1],
                dir_fd=self._directory_descriptor(key[:-1]),
                follow_symlinks=False,
            )
            if _anchored_file_identity(metadata) != identity or (
                _anchored_file_identity(observed) != identity
            ):
                raise AnchoredReadError("workspace path identity changed")
        for anchored in self._files.values():
            self._validate_file_path(anchored)
            self._stream_file(anchored, capture=False)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        descriptors = [item.descriptor for item in self._files.values()]
        descriptors.extend(
            item.descriptor
            for _key, item in sorted(
                self._directories.items(),
                key=lambda item: len(item[0]),
                reverse=True,
            )
        )
        for descriptor in descriptors:
            try:
                os.close(descriptor)
            except OSError:
                pass

    def _open_file(self, key: tuple[str, ...]) -> _AnchoredFile:
        self._require_open()
        existing = self._files.get(key)
        if existing is not None:
            return existing
        parent = key[:-1]
        name = key[-1]
        parent_descriptor = self._directory_descriptor(parent)
        before = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise AnchoredReadError(
                "workspace authority path must be a single-link plain file"
            )
        descriptor = os.open(
            name,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            dir_fd=parent_descriptor,
        )
        try:
            identity = _anchored_file_identity(os.fstat(descriptor))
            if identity != _anchored_file_identity(before):
                raise AnchoredReadError("workspace file identity changed while opening")
        except BaseException:
            os.close(descriptor)
            raise
        anchored = _AnchoredFile(descriptor, parent, name, identity)
        self._files[key] = anchored
        return anchored

    def _stream_file(
        self,
        anchored: _AnchoredFile,
        *,
        capture: bool,
    ) -> tuple[bytes | None, int, str]:
        if _anchored_file_identity(os.fstat(anchored.descriptor)) != anchored.identity:
            raise AnchoredReadError("workspace file identity changed")
        os.lseek(anchored.descriptor, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        payload = bytearray() if capture else None
        size = 0
        while True:
            chunk = os.read(anchored.descriptor, 65_536)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
            if payload is not None:
                payload.extend(chunk)
        os.lseek(anchored.descriptor, 0, os.SEEK_SET)
        if _anchored_file_identity(os.fstat(anchored.descriptor)) != anchored.identity:
            raise AnchoredReadError("workspace file identity changed while reading")
        observed = digest.hexdigest()
        if anchored.sha256 is None:
            anchored.sha256 = observed
        elif anchored.sha256 != observed:
            raise AnchoredReadError("workspace file bytes changed")
        return bytes(payload) if payload is not None else None, size, observed

    def _validate_file_path(self, anchored: _AnchoredFile) -> None:
        observed = os.stat(
            anchored.name,
            dir_fd=self._directory_descriptor(anchored.parent),
            follow_symlinks=False,
        )
        if _anchored_file_identity(observed) != anchored.identity:
            raise AnchoredReadError("workspace file identity changed")

    def _directory_descriptor(self, key: _DirectoryKey) -> int:
        self._require_open()
        if key in self._directories:
            return self._directories[key].descriptor
        current: _DirectoryKey = ()
        descriptor = self._directories[()].descriptor
        for name in key:
            parent = current
            current = (*current, name)
            existing = self._directories.get(current)
            if existing is not None:
                descriptor = existing.descriptor
                continue
            before = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if not stat.S_ISDIR(before.st_mode):
                raise AnchoredReadError("workspace path component is not a directory")
            child = os.open(name, _anchored_directory_flags(), dir_fd=descriptor)
            try:
                identity = _anchored_directory_identity(os.fstat(child))
                if identity != _anchored_directory_identity(before):
                    raise AnchoredReadError(
                        "workspace directory identity changed while opening"
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

    def _workspace_file_key(self, path: str | Path) -> tuple[str, ...]:
        candidate = Path(path)
        if candidate.is_absolute():
            key = self._absolute_key(candidate)
            if key[: len(self._workspace_key)] != self._workspace_key:
                raise AnchoredReadError(f"workspace path escapes root: {path}")
            if len(key) == len(self._workspace_key):
                raise AnchoredReadError("workspace file path is empty")
            return key
        parts = candidate.parts
        if not parts or any(part in {"", ".", ".."} for part in parts):
            raise AnchoredReadError(f"workspace path must be canonical: {path}")
        return (*self._workspace_key, *parts)

    def _workspace_directory_key(self, path: str | Path) -> _DirectoryKey:
        candidate = Path(path)
        if str(candidate) in {"", "."}:
            return self._workspace_key
        return self._workspace_file_key(candidate)

    @staticmethod
    def _absolute_key(path: str | Path) -> _DirectoryKey:
        candidate = Path(path)
        if not candidate.is_absolute():
            raise AnchoredReadError(f"anchored path must be absolute: {path}")
        parts = tuple(part for part in candidate.parts if part != os.sep)
        if any(part in {"", ".", ".."} for part in parts):
            raise AnchoredReadError(f"anchored path must be canonical: {path}")
        return parts

    def _require_open(self) -> None:
        if self._closed:
            raise AnchoredReadError("anchored workspace reader is closed")


def _anchored_directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )


def _anchored_directory_identity(metadata: os.stat_result) -> _DirectoryIdentity:
    return metadata.st_dev, metadata.st_ino, metadata.st_mode


def _anchored_file_identity(metadata: os.stat_result) -> _FileIdentity:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


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
