from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import math
import os
import secrets
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path, PurePosixPath

from .cachesim import ChildResult
from .records import record_sha256, sha256_file

_MAX_SNAPSHOT_BYTES = 512 * 1024 * 1024
_GIT_CONFIG = [
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.untrackedCache=false",
    "-c",
    "core.fileMode=true",
    "-c",
    "core.trustctime=true",
    "-c",
    "core.checkStat=default",
]
Run = Callable[..., ChildResult]


class EvidenceError(ValueError):
    pass


@dataclass(frozen=True)
class BoundJSONObject:
    value: dict[str, object]
    raw: bytes
    path: Path
    identity: tuple[int, int]
    mode: int
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class OutputParentBinding:
    path: Path
    descriptor: int
    identity: tuple[int, int]
    output_name: str

    @property
    def descriptor_path(self) -> Path:
        return Path(f"/proc/self/fd/{self.descriptor}")


def _strict_json_pairs(items: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in items:
        if key in value:
            raise EvidenceError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _invalid_json_constant(value: str) -> object:
    raise EvidenceError(f"non-finite JSON constant is forbidden: {value}")


def _finite_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise EvidenceError(f"non-finite JSON number is forbidden: {value}")
    return parsed


def _finite_json_decimal(value: str) -> Decimal:
    parsed = Decimal(value)
    if not parsed.is_finite():
        raise EvidenceError(f"non-finite JSON number is forbidden: {value}")
    return parsed


def _strict_parse_json_bytes(
    raw: bytes, *, decimal_numbers: bool
) -> dict[str, object]:
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_strict_json_pairs,
            parse_constant=_invalid_json_constant,
            parse_float=(
                _finite_json_decimal if decimal_numbers else _finite_json_float
            ),
        )
    except (UnicodeError, json.JSONDecodeError) as error:
        raise EvidenceError("bound JSON is not strict UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise EvidenceError("bound JSON must contain an object")
    return value


def read_bound_json_object(
    path: Path,
    *,
    max_bytes: int,
    decimal_numbers: bool = False,
) -> BoundJSONObject:
    if type(max_bytes) is not int or not 1 <= max_bytes <= 64 * 1024 * 1024:
        raise EvidenceError("bound JSON byte limit is invalid")
    candidate = Path(path).absolute()
    descriptor = os.open(candidate, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    digest = hashlib.sha256()
    raw = bytearray()
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise EvidenceError("bound JSON path is not a regular file")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            while True:
                block = stream.read(min(1024 * 1024, max_bytes + 1 - len(raw)))
                if not block:
                    break
                raw.extend(block)
                digest.update(block)
                if len(raw) > max_bytes:
                    raise EvidenceError("bound JSON exceeds its byte limit")
        value = _strict_parse_json_bytes(
            bytes(raw), decimal_numbers=decimal_numbers
        )
        after = candidate.stat(follow_symlinks=False)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if (
        stat.S_ISLNK(after.st_mode)
        or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or before.st_ctime_ns != after.st_ctime_ns
        or len(raw) != before.st_size
    ):
        raise EvidenceError("bound JSON path binding changed while reading")
    return BoundJSONObject(
        value=value,
        raw=bytes(raw),
        path=candidate.resolve(strict=True),
        identity=(before.st_dev, before.st_ino),
        mode=stat.S_IMODE(before.st_mode),
        size_bytes=len(raw),
        sha256=digest.hexdigest(),
    )


@dataclass(frozen=True)
class ArtifactSnapshot:
    name: str
    source_path: Path
    source_identity: tuple[int, int]
    size_bytes: int
    sha256: str
    snapshot_path: Path
    snapshot_identity: tuple[int, int]


def _open_regular(path: Path) -> tuple[int, os.stat_result]:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise EvidenceError(f"artifact must be a regular file: {path}")
        return descriptor, metadata
    except BaseException:
        os.close(descriptor)
        raise


def _hash_regular(path: Path) -> tuple[tuple[int, int], int, str]:
    try:
        descriptor, metadata = _open_regular(path)
    except OSError as error:
        raise EvidenceError(f"artifact is unavailable: {path}") from error
    digest = hashlib.sha256()
    size = 0
    try:
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
                size += len(block)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if size != metadata.st_size:
        raise EvidenceError(f"artifact changed while hashing: {path}")
    return (metadata.st_dev, metadata.st_ino), size, digest.hexdigest()


def discover_static_archive(build_root: Path) -> Path:
    root = Path(build_root).resolve(strict=True)
    candidates: list[Path] = []
    for path in root.rglob("liblibCacheSim.a"):
        try:
            metadata = path.lstat()
            resolved = path.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError):
            continue
        if not path.is_symlink() and stat.S_ISREG(metadata.st_mode):
            candidates.append(resolved)
    if len(candidates) != 1:
        raise EvidenceError(
            f"expected one liblibCacheSim.a under {root}, found {len(candidates)}"
        )
    return candidates[0]


class ArtifactRegistry:
    def __init__(self, stage: Path) -> None:
        self._stage = Path(stage)
        self._snapshot_root = self._stage / "artifact_snapshots"
        self._snapshots: dict[str, ArtifactSnapshot] = {}
        self._mutated: set[str] = set()

    @property
    def valid(self) -> bool:
        return not self._mutated

    def capture(self, name: str, source_path: Path) -> ArtifactSnapshot:
        if name in self._snapshots:
            raise EvidenceError(f"duplicate artifact snapshot: {name}")
        source = Path(source_path).absolute()
        if source.is_symlink():
            raise EvidenceError(f"artifact must not be a symlink: {name}")
        descriptor, before = _open_regular(source)
        if before.st_size > _MAX_SNAPSHOT_BYTES:
            os.close(descriptor)
            raise EvidenceError(f"artifact exceeds snapshot limit: {name}")
        self._snapshot_root.mkdir(mode=0o700, exist_ok=True)
        destination = self._snapshot_root / name
        output = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o400,
        )
        digest = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(descriptor, "rb") as input_stream, os.fdopen(
                output, "wb"
            ) as output_stream:
                descriptor = -1
                output = -1
                for block in iter(lambda: input_stream.read(1024 * 1024), b""):
                    digest.update(block)
                    size += len(block)
                    output_stream.write(block)
                output_stream.flush()
                os.fsync(output_stream.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if output >= 0:
                os.close(output)
        after = source.stat()
        if (
            (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_ctime_ns != after.st_ctime_ns
            or size != before.st_size
        ):
            raise EvidenceError(f"artifact changed during snapshot: {name}")
        destination.chmod(0o400)
        snapshot_metadata = destination.lstat()
        snapshot = ArtifactSnapshot(
            name=name,
            source_path=source,
            source_identity=(before.st_dev, before.st_ino),
            size_bytes=size,
            sha256=digest.hexdigest(),
            snapshot_path=destination,
            snapshot_identity=(snapshot_metadata.st_dev, snapshot_metadata.st_ino),
        )
        self._snapshots[name] = snapshot
        self._revalidate_one(snapshot)
        return snapshot

    def _revalidate_one(self, snapshot: ArtifactSnapshot) -> bool:
        try:
            source_identity, source_size, source_sha256 = _hash_regular(
                snapshot.source_path
            )
            snapshot_identity, snapshot_size, snapshot_sha256 = _hash_regular(
                snapshot.snapshot_path
            )
        except (OSError, ValueError):
            self._mutated.add(snapshot.name)
            return False
        intact = (
            source_identity == snapshot.source_identity
            and source_size == snapshot.size_bytes
            and source_sha256 == snapshot.sha256
            and snapshot_identity == snapshot.snapshot_identity
            and snapshot_size == snapshot.size_bytes
            and snapshot_sha256 == snapshot.sha256
        )
        if not intact:
            self._mutated.add(snapshot.name)
        return intact

    def revalidate(self) -> tuple[str, ...]:
        for snapshot in self._snapshots.values():
            self._revalidate_one(snapshot)
        return tuple(sorted(self._mutated))

    def snapshot_paths(self) -> tuple[Path, ...]:
        return tuple(
            snapshot.snapshot_path
            for snapshot in sorted(self._snapshots.values(), key=lambda item: item.name)
        )

    def receipt(self) -> dict[str, dict[str, object]]:
        self.revalidate()
        return {
            name: {
                "source_path": str(snapshot.source_path),
                "source_identity": {
                    "device": snapshot.source_identity[0],
                    "inode": snapshot.source_identity[1],
                },
                "size_bytes": snapshot.size_bytes,
                "sha256": snapshot.sha256,
                "snapshot_path": str(snapshot.snapshot_path.relative_to(self._stage)),
                "snapshot_identity": {
                    "device": snapshot.snapshot_identity[0],
                    "inode": snapshot.snapshot_identity[1],
                },
                "binding_intact": name not in self._mutated,
            }
            for name, snapshot in sorted(self._snapshots.items())
        }

def _bounded_error(value: object, limit: int = 512) -> str:
    message = " ".join(str(value).split()) or value.__class__.__name__
    return message if len(message) <= limit else message[: limit - 3] + "..."


@dataclass(frozen=True)
class Binding:
    head: str
    tree: str
    origin: bytes
    push_urls: bytes
    index: bytes
    index_flags: bytes
    tracked: tuple[tuple[str, str, str], ...]


@dataclass
class Invocation:
    record: dict[str, object]
    result: ChildResult | None
    stdout: bytes
    stderr: bytes
    stdout_identity: tuple[int, int] | None
    stderr_identity: tuple[int, int] | None

    @property
    def ok(self) -> bool:
        return self.result is not None and self.result.returncode == 0


@dataclass(frozen=True)
class EvidenceExpectation:
    path: str
    identity: tuple[int, int] | None
    size_bytes: int | None
    sha256: str | None


def _git_result(checkout: Path, *argv: str) -> subprocess.CompletedProcess[bytes]:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    environment["GIT_NO_LAZY_FETCH"] = "1"
    return subprocess.run(
        ["git", *_GIT_CONFIG, *argv],
        cwd=checkout,
        capture_output=True,
        check=False,
        env=environment,
    )


def _git_bytes(checkout: Path, *argv: str) -> bytes:
    result = _git_result(checkout, *argv)
    if result.returncode != 0:
        raise EvidenceError(
            f"Git command failed ({' '.join(argv)}): "
            f"{_bounded_error(result.stderr.decode('utf-8', errors='replace'), 300)}"
        )
    return result.stdout


def _git(checkout: Path, *argv: str) -> str:
    try:
        return _git_bytes(checkout, *argv).decode("utf-8").strip()
    except UnicodeError as error:
        raise EvidenceError("Git binding output is not UTF-8") from error


def regular_bytes(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise EvidenceError(f"expected a regular file: {path}")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            return stream.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def regular_identity(path: Path) -> tuple[int, int]:
    metadata = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise EvidenceError(f"expected a regular non-symlink file: {path}")
    return metadata.st_dev, metadata.st_ino


def refresh_file_record(
    path: Path,
    record: dict[str, object],
    identity: tuple[int, int],
) -> bool:
    initial_size = record.get("size_bytes")
    initial_sha256 = record.get("sha256")
    try:
        raw = regular_bytes(path)
        observed_identity = regular_identity(path)
        observed_size: int | None = len(raw)
        observed_sha256: str | None = hashlib.sha256(raw).hexdigest()
    except (OSError, ValueError):
        observed_identity = None
        observed_size = None
        observed_sha256 = None
    intact = (
        observed_identity == identity
        and observed_size == initial_size
        and observed_sha256 == initial_sha256
    )
    record["binding_intact"] = intact
    if not intact:
        record["initial_size_bytes"] = initial_size
        record["initial_sha256"] = initial_sha256
        record["size_bytes"] = observed_size
        record["sha256"] = observed_sha256
    return intact


def capture_expected_evidence(path: Path, stage: Path) -> EvidenceExpectation:
    relative = str(path.relative_to(stage))
    try:
        raw = regular_bytes(path)
        identity = regular_identity(path)
    except (OSError, ValueError):
        return EvidenceExpectation(relative, None, None, None)
    return EvidenceExpectation(
        relative,
        identity,
        len(raw),
        hashlib.sha256(raw).hexdigest(),
    )


def checkout_path(path: Path) -> Path:
    candidate = Path(path).absolute()
    try:
        metadata = candidate.lstat()
        if candidate.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
            raise EvidenceError("checkout must be a real directory")
        return candidate.resolve(strict=True)
    except EvidenceError:
        raise
    except OSError as error:
        raise EvidenceError("checkout must exist") from error


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def output_path(path: Path, checkout: Path) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        raise EvidenceError("output must be absolute")
    if candidate.name in {"", ".", ".."}:
        raise EvidenceError("output must name a new directory")
    parent = candidate.parent
    try:
        metadata = parent.lstat()
        if parent.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
            raise EvidenceError("output parent must be a real directory")
        resolved_parent = parent.resolve(strict=True)
    except EvidenceError:
        raise
    except OSError as error:
        raise EvidenceError("output parent must exist") from error
    resolved = (resolved_parent / candidate.name).resolve(strict=False)
    if _paths_overlap(checkout, resolved):
        raise EvidenceError("checkout and output paths must not overlap")
    if any(character.isspace() or character == ":" for character in str(resolved)):
        raise EvidenceError("output path is unsafe for LD_PRELOAD")
    if os.path.lexists(candidate) or os.path.lexists(resolved):
        raise EvidenceError(f"output must not exist: {resolved}")
    return resolved


def stage_directory(output: Path) -> tuple[Path, tuple[int, int]]:
    stage = Path(
        tempfile.mkdtemp(prefix=f".{output.name}-stage-", dir=output.parent)
    )
    stage.chmod(0o700)
    metadata = stage.lstat()
    return stage, (metadata.st_dev, metadata.st_ino)


def retain_output_parent(output: Path) -> OutputParentBinding:
    parent = Path(output).parent
    descriptor = os.open(
        parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        metadata = os.fstat(descriptor)
        path_metadata = parent.lstat()
        identity = (metadata.st_dev, metadata.st_ino)
        if (
            parent.is_symlink()
            or not stat.S_ISDIR(metadata.st_mode)
            or (path_metadata.st_dev, path_metadata.st_ino) != identity
        ):
            raise EvidenceError("output parent binding is invalid")
        return OutputParentBinding(parent.resolve(strict=True), descriptor, identity, output.name)
    except BaseException:
        os.close(descriptor)
        raise


def stage_directory_in_parent(
    parent: OutputParentBinding,
) -> tuple[Path, tuple[int, int]]:
    for _attempt in range(16):
        name = f".{parent.output_name}-stage-{secrets.token_hex(16)}"
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent.descriptor)
        except FileExistsError:
            continue
        path = parent.path / name
        metadata = path.lstat()
        return path, (metadata.st_dev, metadata.st_ino)
    raise EvidenceError("cannot allocate descriptor-relative stage directory")


def publish_stage_in_parent(
    parent: OutputParentBinding,
    stage: Path,
    identity: tuple[int, int],
) -> Path:
    stage_name = stage.name
    metadata = os.stat(stage_name, dir_fd=parent.descriptor, follow_symlinks=False)
    if not stat.S_ISDIR(metadata.st_mode) or (metadata.st_dev, metadata.st_ino) != identity:
        raise EvidenceError("evaluation stage changed before publication")
    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError as error:
        raise EvidenceError("atomic no-replace publication is unavailable") from error
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        parent.descriptor,
        os.fsencode(stage_name),
        parent.descriptor,
        os.fsencode(parent.output_name),
        1,
    )
    if result != 0:
        number = ctypes.get_errno()
        if number == errno.EEXIST:
            raise EvidenceError("refusing to replace output directory")
        raise OSError(number, os.strerror(number), parent.output_name)
    os.fsync(parent.descriptor)
    held = parent.descriptor_path / parent.output_name
    held_metadata = held.lstat()
    try:
        path_parent = parent.path.lstat()
        public = parent.path / parent.output_name
        public_metadata = public.lstat()
    except OSError as error:
        raise EvidenceError("output parent path changed during publication") from error
    if (
        (os.fstat(parent.descriptor).st_dev, os.fstat(parent.descriptor).st_ino)
        != parent.identity
        or (path_parent.st_dev, path_parent.st_ino) != parent.identity
        or (held_metadata.st_dev, held_metadata.st_ino) != identity
        or (public_metadata.st_dev, public_metadata.st_ino) != identity
    ):
        raise EvidenceError("output parent path binding changed during publication")
    return held


def directory_identity(path: Path) -> tuple[int, int]:
    metadata = path.lstat()
    if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise EvidenceError(f"apparatus directory is not real: {path}")
    return metadata.st_dev, metadata.st_ino


def cleanup_owned(path: Path, identity: tuple[int, int]) -> None:
    try:
        observed = directory_identity(path)
    except FileNotFoundError:
        return
    if observed != identity:
        raise EvidenceError(f"refusing to remove replaced apparatus directory: {path}")
    shutil.rmtree(path)


def publish_stage(stage: Path, identity: tuple[int, int], output: Path) -> None:
    if directory_identity(stage) != identity:
        raise EvidenceError("evaluation stage changed before publication")
    if os.path.lexists(output):
        raise EvidenceError(f"refusing to replace output directory: {output}")
    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError as error:
        raise EvidenceError("atomic no-replace publication is unavailable") from error
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(-100, os.fsencode(stage), -100, os.fsencode(output), 1)
    if result != 0:
        number = ctypes.get_errno()
        if number == errno.EEXIST:
            raise EvidenceError(f"refusing to replace output directory: {output}")
        raise OSError(number, os.strerror(number), output)
    descriptor = os.open(output.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _tracked_snapshot(checkout: Path) -> tuple[tuple[str, str, str], ...]:
    raw_paths = _git_bytes(checkout, "ls-files", "-z")
    entries: list[tuple[str, str, str]] = []
    for raw_path in raw_paths.split(b"\0"):
        if not raw_path:
            continue
        try:
            relative = os.fsdecode(raw_path)
            path = checkout.joinpath(*PurePosixPath(relative).parts)
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                mode = "symlink"
                value = os.fsencode(os.readlink(path))
            elif stat.S_ISREG(metadata.st_mode):
                mode = "executable" if metadata.st_mode & stat.S_IXUSR else "regular"
                value = regular_bytes(path)
            else:
                raise EvidenceError(f"unsupported tracked source entry: {relative}")
        except (OSError, ValueError) as error:
            raise EvidenceError("cannot snapshot tracked source bytes") from error
        entries.append((relative, mode, hashlib.sha256(value).hexdigest()))
    return tuple(sorted(entries))


def capture_binding(checkout: Path) -> Binding:
    push_result = _git_result(
        checkout, "config", "--get-regexp", r"^remote\.origin\.pushurl$"
    )
    if push_result.returncode not in {0, 1}:
        raise EvidenceError("cannot audit candidate origin push URLs")
    return Binding(
        head=_git(checkout, "rev-parse", "HEAD"),
        tree=_git(checkout, "rev-parse", "HEAD^{tree}"),
        origin=_git_bytes(checkout, "remote", "get-url", "--all", "origin"),
        push_urls=push_result.stdout,
        index=_git_bytes(checkout, "ls-files", "--stage", "-z"),
        index_flags=_git_bytes(checkout, "ls-files", "-v", "-z"),
        tracked=_tracked_snapshot(checkout),
    )


def _audit_filesystem(
    checkout: Path,
    tracked: tuple[tuple[str, str, str], ...],
    allowed_roots: Sequence[Path],
) -> None:
    tracked_files = {path for path, _mode, _digest in tracked}
    tracked_directories: set[str] = set()
    for relative in tracked_files:
        parent = PurePosixPath(relative).parent
        while parent.as_posix() != ".":
            tracked_directories.add(parent.as_posix())
            parent = parent.parent
    allowed: set[str] = set()
    for root in allowed_roots:
        try:
            relative = Path(root).absolute().relative_to(checkout).as_posix()
        except ValueError:
            continue
        if relative != ".":
            allowed.add(relative)

    directories: list[tuple[Path, str]] = [(checkout, "")]
    while directories:
        directory, prefix = directories.pop()
        with os.scandir(directory) as scanner:
            for entry in scanner:
                if not prefix and entry.name == ".git":
                    continue
                relative = f"{prefix}/{entry.name}" if prefix else entry.name
                if relative in allowed:
                    continue
                metadata = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(metadata.st_mode):
                    if relative not in tracked_directories:
                        raise EvidenceError(
                            f"candidate source gained an untracked directory: {relative}"
                        )
                    directories.append((Path(entry.path), relative))
                elif relative not in tracked_files:
                    raise EvidenceError(
                        f"candidate source gained an untracked entry: {relative}"
                    )


def revalidate_checkout(
    checkout: Path,
    expected: Binding,
    *,
    allowed_roots: Sequence[Path] = (),
) -> None:
    observed = capture_binding(checkout)
    if observed != expected:
        raise EvidenceError("candidate source binding mutated during evaluation")
    _audit_filesystem(checkout, expected.tracked, allowed_roots)


def command_record(
    stage: Path,
    index: int,
    label: str,
    argv: Sequence[str],
    cwd: Path,
    run: Run,
    timeout_seconds: float,
    max_output_bytes: int,
) -> Invocation:
    output = stage / "commands" / f"{index:02d}-{label}"
    output.parent.mkdir(exist_ok=True)
    command = list(argv)
    try:
        result = run(
            command,
            output,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
        )
        if not isinstance(result, ChildResult):
            raise EvidenceError("command runner returned an invalid process receipt")
        if result.argv != tuple(command):
            raise EvidenceError("command runner argv receipt mismatch")
        expected_stdout = output / "stdout.raw"
        expected_stderr = output / "stderr.raw"
        if result.stdout_path != expected_stdout or result.stderr_path != expected_stderr:
            raise EvidenceError("command runner raw output path mismatch")
        stdout = regular_bytes(expected_stdout)
        stderr = regular_bytes(expected_stderr)
        stdout_identity = regular_identity(expected_stdout)
        stderr_identity = regular_identity(expected_stderr)
        if (
            len(stdout) != result.stdout_bytes
            or len(stderr) != result.stderr_bytes
            or hashlib.sha256(stdout).hexdigest() != result.stdout_sha256
            or hashlib.sha256(stderr).hexdigest() != result.stderr_sha256
        ):
            raise EvidenceError("command runner raw output receipt mismatch")
        record: dict[str, object] = {
            "index": index,
            "label": label,
            "argv": command,
            "cwd": str(cwd),
            "timeout_seconds": timeout_seconds,
            "max_output_bytes": max_output_bytes,
            "returncode": result.returncode,
            "wall_ns": result.wall_ns,
            "cpu_ns": result.cpu_ns,
            "stdout": {
                "path": str(expected_stdout.relative_to(stage)),
                "size_bytes": result.stdout_bytes,
                "sha256": result.stdout_sha256,
            },
            "stderr": {
                "path": str(expected_stderr.relative_to(stage)),
                "size_bytes": result.stderr_bytes,
                "sha256": result.stderr_sha256,
            },
        }
        return Invocation(
            record,
            result,
            stdout,
            stderr,
            stdout_identity,
            stderr_identity,
        )
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        if isinstance(error, subprocess.TimeoutExpired):
            error_message = f"command timed out after {error.timeout} seconds"
        else:
            error_message = _bounded_error(error)
        record = {
            "index": index,
            "label": label,
            "argv": command,
            "cwd": str(cwd),
            "timeout_seconds": timeout_seconds,
            "max_output_bytes": max_output_bytes,
            "returncode": None,
            "error": error_message,
            "stdout": {
                "path": str((output / "stdout.raw").relative_to(stage)),
                "size_bytes": None,
                "sha256": None,
                "binding_intact": False,
            },
            "stderr": {
                "path": str((output / "stderr.raw").relative_to(stage)),
                "size_bytes": None,
                "sha256": None,
                "binding_intact": False,
            },
        }
        return Invocation(record, None, b"", b"", None, None)


def skipped_command_record(
    stage: Path,
    index: int,
    label: str,
    argv: Sequence[str],
    cwd: Path,
    reason: str,
    timeout_seconds: float,
    max_output_bytes: int,
) -> Invocation:
    output = stage / "commands" / f"{index:02d}-{label}"
    record: dict[str, object] = {
        "index": index,
        "label": label,
        "argv": list(argv),
        "cwd": str(cwd),
        "timeout_seconds": timeout_seconds,
        "max_output_bytes": max_output_bytes,
        "returncode": None,
        "error": reason,
        "stdout": {
            "path": str((output / "stdout.raw").relative_to(stage)),
            "size_bytes": None,
            "sha256": None,
            "binding_intact": False,
        },
        "stderr": {
            "path": str((output / "stderr.raw").relative_to(stage)),
            "size_bytes": None,
            "sha256": None,
            "binding_intact": False,
        },
    }
    return Invocation(record, None, b"", b"", None, None)


def revalidate_command_evidence(stage: Path, commands: list[Invocation]) -> bool:
    intact = True
    for invocation in commands:
        if invocation.result is None:
            continue
        for name, identity in (
            ("stdout", invocation.stdout_identity),
            ("stderr", invocation.stderr_identity),
        ):
            raw_record = invocation.record.get(name)
            if not isinstance(raw_record, dict) or identity is None:
                intact = False
                continue
            raw_path = raw_record.get("path")
            if type(raw_path) is not str:
                intact = False
                continue
            if not refresh_file_record(stage / raw_path, raw_record, identity):
                intact = False
    return intact


def command_evidence_expectations(
    commands: list[Invocation],
) -> list[EvidenceExpectation]:
    expected: list[EvidenceExpectation] = []
    for invocation in commands:
        for name, raw, identity in (
            (
                "stdout",
                invocation.stdout if invocation.result is not None else None,
                invocation.stdout_identity,
            ),
            (
                "stderr",
                invocation.stderr if invocation.result is not None else None,
                invocation.stderr_identity,
            ),
        ):
            record = invocation.record.get(name)
            raw_path = record.get("path") if isinstance(record, dict) else None
            if type(raw_path) is not str:
                raise EvidenceError("command evidence path is unavailable")
            expected.append(
                EvidenceExpectation(
                    raw_path,
                    identity,
                    len(raw) if raw is not None else None,
                    hashlib.sha256(raw).hexdigest() if raw is not None else None,
                )
            )
    return expected


def _expected_directories(expected: list[EvidenceExpectation]) -> set[str]:
    directories: set[str] = set()
    for item in expected:
        parent = PurePosixPath(item.path).parent
        while parent.as_posix() != ".":
            directories.add(parent.as_posix())
            parent = parent.parent
    return directories


def _present_inventory_directories(
    inventory: list[dict[str, object]],
) -> set[str]:
    paths = [
        EvidenceExpectation(str(item["path"]), None, None, None)
        for item in inventory
        if item["present"] is True
    ]
    directories = _expected_directories(paths)
    if any(str(item["path"]).startswith("commands/") for item in inventory):
        directories.add("commands")
    return directories


def _stage_paths(stage: Path) -> tuple[set[str], set[str]]:
    files: set[str] = set()
    directories: set[str] = set()
    pending: list[tuple[Path, str]] = [(stage, "")]
    while pending:
        directory, prefix = pending.pop()
        with os.scandir(directory) as scanner:
            for entry in scanner:
                relative = f"{prefix}/{entry.name}" if prefix else entry.name
                metadata = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(metadata.st_mode):
                    directories.add(relative)
                    pending.append((Path(entry.path), relative))
                else:
                    files.add(relative)
    return files, directories


def evidence_inventory(
    stage: Path,
    expected: list[EvidenceExpectation],
) -> tuple[list[dict[str, object]], bool]:
    if len({item.path for item in expected}) != len(expected):
        raise EvidenceError("duplicate expected evidence path")
    inventory: list[dict[str, object]] = []
    intact = True
    for item in sorted(expected, key=lambda value: value.path):
        path = stage / item.path
        try:
            raw = regular_bytes(path)
            identity = regular_identity(path)
            present = True
            size_bytes: int | None = len(raw)
            sha256: str | None = hashlib.sha256(raw).hexdigest()
        except (OSError, ValueError):
            identity = None
            present = False
            size_bytes = None
            sha256 = None
        binding_intact = (
            present
            and item.identity is not None
            and identity == item.identity
            and size_bytes == item.size_bytes
            and sha256 == item.sha256
        )
        intact = binding_intact and intact
        inventory.append(
            {
                "path": item.path,
                "identity": (
                    {"device": item.identity[0], "inode": item.identity[1]}
                    if item.identity is not None
                    else None
                ),
                "size_bytes": item.size_bytes,
                "sha256": item.sha256,
                "present": present,
                "observed_identity": (
                    {"device": identity[0], "inode": identity[1]}
                    if identity is not None
                    else None
                ),
                "observed_size_bytes": size_bytes,
                "observed_sha256": sha256,
                "binding_intact": binding_intact,
            }
        )
    files, directories = _stage_paths(stage)
    expected_files = {item.path for item in expected}
    evidence_files = files - {"receipt.json"}
    intact = (
        evidence_files
        == {item["path"] for item in inventory if item["present"]}
        and intact
    )
    intact = directories == _present_inventory_directories(inventory) and intact
    if evidence_files - expected_files:
        intact = False
    return inventory, intact


def _canonical_record_bytes(value: dict[str, object]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def verify_final_stage(
    stage: Path,
    expected: list[EvidenceExpectation],
    inventory: list[dict[str, object]],
    receipt: dict[str, object],
) -> None:
    observed_inventory, _inventory_intact = evidence_inventory(stage, expected)
    if observed_inventory != inventory:
        raise EvidenceError("evidence inventory changed after final receipt write")
    receipt_path = stage / "receipt.json"
    receipt_identity = regular_identity(receipt_path)
    raw = regular_bytes(receipt_path)
    if raw != _canonical_record_bytes(receipt):
        raise EvidenceError("final receipt canonical bytes changed")
    if receipt.get("receipt_sha256") != record_sha256(receipt, "receipt_sha256"):
        raise EvidenceError("final receipt self-hash mismatch")
    if regular_identity(receipt_path) != receipt_identity:
        raise EvidenceError("final receipt identity changed")
    files, directories = _stage_paths(stage)
    present_evidence = {
        item["path"] for item in inventory if item["present"] is True
    }
    if files != present_evidence | {"receipt.json"}:
        raise EvidenceError("final publication file inventory mismatch")
    if directories != _present_inventory_directories(inventory):
        raise EvidenceError("final publication directory inventory mismatch")


def unexpected_stage_entries(
    stage: Path,
    commands: list[Invocation],
    extra_expected_files: Sequence[Path] = (),
) -> list[dict[str, object]]:
    expected_files = {
        "synthetic.oracleGeneral.bin",
        "simulator-results.cachesim",
        "capacity_probe.c",
        "capacity-probe",
        "allocator_interposer.c",
        "allocator-interposer.so",
        "metadata_probe.c",
        "metadata-probe",
        "fixed_time_interposer.c",
        "fixed-time-interposer.so",
    }
    expected_directories = {"commands"}
    for path in extra_expected_files:
        relative = str(path.relative_to(stage))
        expected_files.add(relative)
        parent = PurePosixPath(relative).parent
        while parent.as_posix() != ".":
            expected_directories.add(parent.as_posix())
            parent = parent.parent
    for invocation in commands:
        for name in ("stdout", "stderr"):
            raw_record = invocation.record.get(name)
            if not isinstance(raw_record, dict):
                continue
            raw_path = raw_record.get("path")
            if type(raw_path) is str:
                expected_files.add(raw_path)
                expected_directories.add(str(PurePosixPath(raw_path).parent))

    unexpected: list[
        tuple[Path, tuple[int, int], dict[str, object], bool]
    ] = []
    directories: list[tuple[Path, str]] = [(stage, "")]
    while directories:
        directory, prefix = directories.pop()
        with os.scandir(directory) as scanner:
            for entry in scanner:
                relative = f"{prefix}/{entry.name}" if prefix else entry.name
                path = Path(entry.path)
                metadata = entry.stat(follow_symlinks=False)
                identity = (metadata.st_dev, metadata.st_ino)
                if stat.S_ISDIR(metadata.st_mode):
                    if relative in expected_directories:
                        directories.append((path, relative))
                        continue
                    record = {
                        "path": relative,
                        "type": "directory",
                        "size_bytes": None,
                        "sha256": None,
                    }
                    unexpected.append((path, identity, record, True))
                    continue
                if relative in expected_files and stat.S_ISREG(metadata.st_mode):
                    continue
                if stat.S_ISREG(metadata.st_mode):
                    raw = regular_bytes(path)
                    entry_type = "regular"
                elif stat.S_ISLNK(metadata.st_mode):
                    raw = os.fsencode(os.readlink(path))
                    entry_type = "symlink"
                else:
                    raw = b""
                    entry_type = "other"
                record = {
                    "path": relative,
                    "type": entry_type,
                    "size_bytes": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                }
                unexpected.append((path, identity, record, False))

    for path, identity, _record, is_directory in unexpected:
        metadata = path.lstat()
        if (metadata.st_dev, metadata.st_ino) != identity:
            raise EvidenceError(f"unexpected stage entry changed: {path.name}")
        if is_directory:
            cleanup_owned(path, identity)
        else:
            os.unlink(path)
    if unexpected:
        descriptor = os.open(stage, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return [record for _path, _identity, record, _is_directory in unexpected]


def executable_hash(path: Path) -> str | None:
    try:
        metadata = path.lstat()
    except OSError:
        return None
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode) or not os.access(path, os.X_OK):
        return None
    return sha256_file(path)


def capture_executable(
    path: Path, stage: Path
) -> tuple[dict[str, object], tuple[int, int] | None]:
    digest = executable_hash(path)
    record: dict[str, object] = {
        "path": str(path.relative_to(stage)),
        "size_bytes": None,
        "sha256": digest,
        "binding_intact": digest is not None,
    }
    if digest is None:
        return record, None
    metadata = path.lstat()
    record["size_bytes"] = metadata.st_size
    return record, (metadata.st_dev, metadata.st_ino)
