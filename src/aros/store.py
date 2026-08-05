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


_DirectoryKey = tuple[str, ...]
_DirectoryIdentity = tuple[int, int, int]
_FileIdentity = tuple[int, int, int, int, int, int, int]


@dataclass(frozen=True)
class _AnchoredDirectory:
    parent: _DirectoryKey | None
    name: str
    identity: _DirectoryIdentity


@dataclass
class _AnchoredFile:
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


class AnchoredReadLimitError(AnchoredReadError):
    """Raised before capture when a bounded JSON authority is too large."""


class AnchoredReadStructureError(AnchoredReadError):
    """Raised when decoded JSON exceeds structural bounds."""


class JsonStructureError(ValueError):
    """Decoded JSON is too deep, too broad, or contains a non-JSON value."""


class AnchoredWorkspaceReader:
    """Hold one descriptor-anchored multi-file workspace transaction."""

    def __init__(
        self,
        root: str | Path,
        *,
        max_json_bytes: int | None = None,
        max_json_depth: int | None = None,
        max_json_nodes: int | None = None,
        max_capture_bytes: int | None = None,
    ):
        if max_json_bytes is not None and (
            type(max_json_bytes) is not int or max_json_bytes < 0
        ):
            raise AnchoredReadError(
                "max_json_bytes must be nonnegative or null"
            )
        for value, field in (
            (max_json_depth, "max_json_depth"),
            (max_json_nodes, "max_json_nodes"),
            (max_capture_bytes, "max_capture_bytes"),
        ):
            if value is not None and (type(value) is not int or value < 0):
                raise AnchoredReadError(f"{field} must be nonnegative or null")
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
        self._slash_descriptor: int | None = None
        self._root_descriptor: int | None = None
        self._closed = False
        self._max_json_bytes = max_json_bytes
        self._max_json_depth = max_json_depth
        self._max_json_nodes = max_json_nodes
        self._max_capture_bytes = max_capture_bytes
        self._captured_keys: set[object] = set()
        self._captured_bytes = 0
        flags = _anchored_directory_flags()
        try:
            self._slash_descriptor = os.open(os.sep, flags)
            metadata = os.fstat(self._slash_descriptor)
            self._directories[()] = _AnchoredDirectory(
                None,
                os.sep,
                _anchored_directory_identity(metadata),
            )
            parent = self._slash_descriptor
            current: _DirectoryKey = ()
            for index, name in enumerate(self._workspace_key):
                before = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if not stat.S_ISDIR(before.st_mode):
                    raise AnchoredReadError(
                        "workspace path component is not a directory"
                    )
                child = os.open(name, flags, dir_fd=parent)
                try:
                    identity = _anchored_directory_identity(os.fstat(child))
                    if identity != _anchored_directory_identity(before):
                        raise AnchoredReadError(
                            "workspace directory identity changed while opening"
                        )
                except BaseException:
                    os.close(child)
                    raise
                prior = current
                current = (*current, name)
                self._directories[current] = _AnchoredDirectory(
                    prior,
                    name,
                    identity,
                )
                if index and parent != self._slash_descriptor:
                    os.close(parent)
                parent = child
                self._root_descriptor = parent
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
        with self._open_file(key) as (descriptor, anchored):
            if (
                self._max_json_bytes is not None
                and anchored.identity[4] > self._max_json_bytes
            ):
                raise AnchoredReadLimitError(
                    "workspace JSON exceeds "
                    f"{self._max_json_bytes} bytes: {path}"
                )
            self._reserve_capture(key, anchored.identity[4])
            payload, _size, _digest = self._stream_file(
                descriptor,
                anchored,
                capture=True,
                capture_limit=None,
            )
        assert payload is not None
        value = _strict_json_loads(payload)
        if self._max_json_depth is not None or self._max_json_nodes is not None:
            try:
                validate_json_shape(
                    value,
                    max_depth=self._max_json_depth,
                    max_nodes=self._max_json_nodes,
                )
            except JsonStructureError as error:
                raise AnchoredReadStructureError(str(error)) from error
        self._json[key] = value
        return value

    def reserve_external_capture(self, key: str, size: int) -> None:
        """Reserve one externally pinned payload in this capture budget."""
        if not isinstance(key, str) or not key:
            raise AnchoredReadError("external capture key must be non-empty")
        self._reserve_capture(("external", key), size)

    def _reserve_capture(self, key: object, size: int) -> None:
        if type(size) is not int or size < 0:
            raise AnchoredReadError("capture size must be nonnegative")
        if self._max_capture_bytes is None or key in self._captured_keys:
            return
        if self._captured_bytes + size > self._max_capture_bytes:
            raise AnchoredReadLimitError(
                "workspace aggregate capture budget exceeds "
                f"{self._max_capture_bytes} bytes"
            )
        self._captured_keys.add(key)
        self._captured_bytes += size

    def require_file(self, path: str | Path) -> None:
        with self._open_file(self._workspace_file_key(path)) as (
            descriptor,
            anchored,
        ):
            self._stream_file(
                descriptor,
                anchored,
                capture=False,
                capture_limit=None,
            )

    def require_directory(self, path: str | Path) -> None:
        with self._open_directory(self._workspace_directory_key(path)):
            pass

    def require_repository(
        self,
        root: str | Path,
        git_dir: str | Path,
        common_dir: str | Path,
    ) -> None:
        if self._absolute_key(root) != self._workspace_key:
            raise AnchoredReadError("repository root differs from anchored workspace")
        self.require_git_marker()
        with self._open_directory(self._absolute_key(git_dir)):
            pass
        with self._open_directory(self._absolute_key(common_dir)):
            pass

    def require_git_marker(self) -> None:
        key = self._workspace_file_key(".git")
        with self._open_directory(key[:-1]) as descriptor:
            metadata = os.stat(
                key[-1],
                dir_fd=descriptor,
                follow_symlinks=False,
            )
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
        with self._open_directory(key) as descriptor:
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
        with self._open_directory(parent) as descriptor:
            metadata = os.stat(
                key[-1],
                dir_fd=descriptor,
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
        with self._open_file(self._workspace_file_key(path)) as (
            descriptor,
            anchored,
        ):
            actual_size = anchored.identity[4]
            if actual_size != expected_size:
                raise AnchoredReadError("workspace stream size differs from receipt")
            capture = capture_limit is not None and actual_size <= capture_limit
            payload, size, digest = self._stream_file(
                descriptor,
                anchored,
                capture=capture,
                capture_limit=capture_limit,
            )
        if size != expected_size or digest != expected_sha256:
            raise AnchoredReadError("workspace stream differs from receipt")
        return payload

    def revalidate(self) -> None:
        self._require_open()
        assert self._slash_descriptor is not None
        assert self._root_descriptor is not None
        if (
            _anchored_directory_identity(os.fstat(self._slash_descriptor))
            != self._directories[()].identity
            or _anchored_directory_identity(os.fstat(self._root_descriptor))
            != self._directories[self._workspace_key].identity
        ):
            raise AnchoredReadError("workspace retained directory identity changed")
        for key in tuple(self._directories):
            with self._open_directory(key, force_slash=True) as descriptor:
                directory = self._directories[key]
                if (
                    _anchored_directory_identity(os.fstat(descriptor))
                    != directory.identity
                ):
                    raise AnchoredReadError("workspace directory identity changed")
            listing = self._listings.get(key)
            if listing is not None:
                with self._open_directory(key, force_slash=True) as descriptor:
                    names = tuple(sorted(os.listdir(descriptor)))
                    entries = tuple(
                        (
                            name,
                            _anchored_file_identity(
                                os.stat(
                                    name,
                                    dir_fd=descriptor,
                                    follow_symlinks=False,
                                )
                            ),
                        )
                        for name in names
                    )
                if names != listing.names or entries != listing.entries:
                    raise AnchoredReadError("workspace directory listing changed")
        for key, (_metadata, identity) in self._stats.items():
            with self._open_directory(key[:-1], force_slash=True) as descriptor:
                observed = os.stat(
                    key[-1],
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
            if _anchored_file_identity(observed) != identity:
                raise AnchoredReadError("workspace path identity changed")
        for key, snapshot in self._files.items():
            with self._open_file(key, force_slash=True) as (descriptor, anchored):
                if anchored.identity != snapshot.identity:
                    raise AnchoredReadError("workspace file identity changed")
                if snapshot.sha256 is not None:
                    self._stream_file(
                        descriptor,
                        anchored,
                        capture=False,
                        capture_limit=None,
                    )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        descriptors = (self._root_descriptor, self._slash_descriptor)
        for descriptor in descriptors:
            if descriptor is None:
                continue
            try:
                os.close(descriptor)
            except OSError:
                pass

    @contextmanager
    def _open_file(
        self,
        key: tuple[str, ...],
        *,
        force_slash: bool = False,
    ) -> Iterator[tuple[int, _AnchoredFile]]:
        self._require_open()
        parent = key[:-1]
        name = key[-1]
        with self._open_directory(parent, force_slash=force_slash) as parent_descriptor:
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
                    raise AnchoredReadError(
                        "workspace file identity changed while opening"
                    )
                anchored = self._files.get(key)
                if anchored is None:
                    anchored = _AnchoredFile(parent, name, identity)
                    self._files[key] = anchored
                elif anchored.identity != identity:
                    raise AnchoredReadError("workspace file identity changed")
                yield descriptor, anchored
                observed = os.stat(
                    name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
                if (
                    _anchored_file_identity(os.fstat(descriptor)) != identity
                    or _anchored_file_identity(observed) != identity
                ):
                    raise AnchoredReadError("workspace file identity changed")
            finally:
                os.close(descriptor)

    def _stream_file(
        self,
        descriptor: int,
        anchored: _AnchoredFile,
        *,
        capture: bool,
        capture_limit: int | None,
    ) -> tuple[bytes | None, int, str]:
        if _anchored_file_identity(os.fstat(descriptor)) != anchored.identity:
            raise AnchoredReadError("workspace file identity changed")
        os.lseek(descriptor, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        payload = bytearray() if capture else None
        size = 0
        while True:
            chunk = os.read(descriptor, 65_536)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
            if payload is not None:
                if capture_limit is not None and size > capture_limit:
                    payload = None
                else:
                    payload.extend(chunk)
        os.lseek(descriptor, 0, os.SEEK_SET)
        if (
            _anchored_file_identity(os.fstat(descriptor)) != anchored.identity
            or size != anchored.identity[4]
        ):
            raise AnchoredReadError("workspace file identity changed while reading")
        observed = digest.hexdigest()
        if anchored.sha256 is None:
            anchored.sha256 = observed
        elif anchored.sha256 != observed:
            raise AnchoredReadError("workspace file bytes changed")
        return bytes(payload) if payload is not None else None, size, observed

    @contextmanager
    def _open_directory(
        self,
        key: _DirectoryKey,
        *,
        force_slash: bool = False,
    ) -> Iterator[int]:
        self._require_open()
        assert self._slash_descriptor is not None
        assert self._root_descriptor is not None
        if not force_slash and key[: len(self._workspace_key)] == self._workspace_key:
            current = self._workspace_key
            descriptor = self._root_descriptor
            names = key[len(self._workspace_key) :]
        else:
            current = ()
            descriptor = self._slash_descriptor
            names = key
        opened: list[int] = []
        try:
            for name in names:
                parent = current
                current = (*current, name)
                before = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if not stat.S_ISDIR(before.st_mode):
                    raise AnchoredReadError(
                        "workspace path component is not a directory"
                    )
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
                existing = self._directories.get(current)
                if existing is None:
                    self._directories[current] = _AnchoredDirectory(
                        parent,
                        name,
                        identity,
                    )
                elif existing.identity != identity:
                    os.close(child)
                    raise AnchoredReadError("workspace directory identity changed")
                opened.append(child)
                descriptor = child
            yield descriptor
        finally:
            for opened_descriptor in reversed(opened):
                os.close(opened_descriptor)

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
    try:
        return json.loads(
            raw,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
            parse_float=_strict_json_float,
        )
    except RecursionError as error:
        raise ValueError("JSON recursion or depth limit exceeded") from error


def validate_json_shape(
    value: object,
    *,
    max_depth: int | None,
    max_nodes: int | None,
) -> None:
    """Iteratively validate optional decoded-JSON depth and node bounds."""
    for limit, field in ((max_depth, "max_depth"), (max_nodes, "max_nodes")):
        if limit is not None and (type(limit) is not int or limit < 0):
            raise ValueError(f"{field} must be nonnegative or null")
    pending: list[tuple[object, int]] = [(value, 1)]
    nodes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if max_nodes is not None and nodes > max_nodes:
            raise JsonStructureError(
                f"JSON nodes exceed {max_nodes}"
            )
        if max_depth is not None and depth > max_depth:
            raise JsonStructureError(
                f"JSON depth exceeds {max_depth}"
            )
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise JsonStructureError("JSON object key is not a string")
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
        elif item is not None and not isinstance(
            item,
            (bool, int, float, str),
        ):
            raise JsonStructureError(
                f"decoded value is not JSON-compatible: {type(item).__name__}"
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
