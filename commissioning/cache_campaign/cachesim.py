from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import BinaryIO


RESULT = re.compile(
    r"^[^\x00-\x1f\x7f,]+ cache size\s+[^,\x00-\x1f\x7f]+,\s+"
    r"(?P<requests>[0-9]+) req, miss ratio (?P<object>[0-9]+\.[0-9]+), "
    r"byte miss ratio (?P<byte>[0-9]+\.[0-9]+), throughput "
    r"(?P<throughput>[0-9]+\.[0-9]+) MQPS$"
)
_LOGGER = re.compile(
    r"^(?:\x1b\[[0-9;]*m)*\[(?:VERB|DEBUG|INFO|WARN)\]\s+"
    r"[^\x00-\x08\x0b\x0c\x0e-\x1a\x1c-\x1f\x7f]*"
    r"(?:\x1b\[0m)?$"
)
_ANSI_RESET = re.compile(r"^(?:\x1b\[0m)+$")


class CacheSimOutputError(ValueError):
    pass


class ChildRunError(RuntimeError):
    pass


@dataclass(frozen=True)
class ParsedResult:
    request_count: int
    object_miss_ratio: Decimal
    byte_miss_ratio: Decimal
    simulator_throughput_mqps: Decimal


@dataclass(frozen=True)
class ChildResult:
    argv: tuple[str, ...]
    returncode: int
    wall_ns: int
    cpu_ns: int
    stdout_path: Path
    stdout_bytes: int
    stdout_sha256: str
    stderr_path: Path
    stderr_bytes: int
    stderr_sha256: str


def _excerpt(line: str) -> str:
    value = repr(line[:160])
    return value + ("..." if len(line) > 160 else "")


def parse_cachesim_output(output: str) -> ParsedResult:
    if type(output) is not str:
        raise CacheSimOutputError("libCacheSim output must be text")
    if "\r" in output:
        raise CacheSimOutputError("libCacheSim output must use LF line endings")

    matches: list[re.Match[str]] = []
    for line_number, line in enumerate(output.split("\n"), start=1):
        match = RESULT.fullmatch(line)
        if match is not None:
            matches.append(match)
            continue
        logger_result = _LOGGER.fullmatch(line)
        if logger_result is not None and " cache size " not in line:
            continue
        if line == "" or _ANSI_RESET.fullmatch(line) is not None:
            continue
        raise CacheSimOutputError(
            f"unrecognized libCacheSim output on line {line_number}: {_excerpt(line)}"
        )

    if len(matches) != 1:
        raise CacheSimOutputError(
            f"expected exactly one libCacheSim result line, found {len(matches)}"
        )
    fields = matches[0].groupdict()
    request_count = int(fields["requests"])
    object_miss_ratio = Decimal(fields["object"])
    byte_miss_ratio = Decimal(fields["byte"])
    throughput = Decimal(fields["throughput"])
    if request_count <= 0:
        raise CacheSimOutputError("libCacheSim request count must be positive")
    if not object_miss_ratio.is_finite() or not 0 <= object_miss_ratio <= 1:
        raise CacheSimOutputError("libCacheSim object miss ratio must be in [0, 1]")
    if not byte_miss_ratio.is_finite() or not 0 <= byte_miss_ratio <= 1:
        raise CacheSimOutputError("libCacheSim byte miss ratio must be in [0, 1]")
    if not throughput.is_finite() or throughput <= 0:
        raise CacheSimOutputError("libCacheSim throughput must be positive")
    return ParsedResult(
        request_count=request_count,
        object_miss_ratio=object_miss_ratio,
        byte_miss_ratio=byte_miss_ratio,
        simulator_throughput_mqps=throughput,
    )


def _validated_argv(argv: Sequence[str]) -> tuple[str, ...]:
    if isinstance(argv, (str, bytes, bytearray)) or not isinstance(argv, Sequence):
        raise ChildRunError("argv must be a nonempty sequence of strings")
    values = tuple(argv)
    if not values:
        raise ChildRunError("argv must be a nonempty sequence of strings")
    if any(type(value) is not str or not value or "\0" in value for value in values):
        raise ChildRunError("argv entries must be nonempty strings without NUL bytes")
    return values


def _validated_cwd(cwd: Path | None) -> Path | None:
    if cwd is None:
        return None
    try:
        path = Path(cwd).absolute()
        metadata = path.lstat()
        if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
            raise ChildRunError("cwd must be a real directory")
        return path.resolve(strict=True)
    except ChildRunError:
        raise
    except (OSError, TypeError, ValueError) as error:
        raise ChildRunError(f"cwd must be a real directory: {_bounded_error(error)}") from error


def _bounded_error(error: BaseException) -> str:
    message = " ".join(str(error).split()) or error.__class__.__name__
    return message[:300] + ("..." if len(message) > 300 else "")


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _create_raw_file(
    directory_descriptor: int,
    name: str,
    files: dict[str, tuple[int, int]],
) -> BinaryIO:
    descriptor = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
        dir_fd=directory_descriptor,
    )
    try:
        metadata = os.fstat(descriptor)
        files[name] = _identity(metadata)
        if not stat.S_ISREG(metadata.st_mode):
            raise ChildRunError(f"raw output is not a regular file: {name}")
        stream = os.fdopen(descriptor, "wb")
        descriptor = -1
        return stream
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _hash_raw_file(
    directory_descriptor: int,
    name: str,
    expected_identity: tuple[int, int],
) -> tuple[str, int]:
    descriptor = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=directory_descriptor,
    )
    digest = hashlib.sha256()
    size = 0
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or _identity(metadata) != expected_identity:
            raise ChildRunError(f"raw output changed before hashing: {name}")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
                size += len(block)
        observed = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        if _identity(observed) != expected_identity or observed.st_size != size:
            raise ChildRunError(f"raw output changed while hashing: {name}")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return digest.hexdigest(), size


def _path_identity(path: Path) -> tuple[int, int] | None:
    try:
        return _identity(path.lstat())
    except FileNotFoundError:
        return None


def _remove_owned_directory(
    output: Path, directory_identity: tuple[int, int]
) -> str | None:
    try:
        observed_identity = _path_identity(output)
    except OSError as error:
        return f"cannot inspect output directory: {_bounded_error(error)}"
    if observed_identity != directory_identity:
        return "replacement output directory preserved"
    try:
        os.rmdir(output)
    except OSError as error:
        return f"cannot remove output directory: {_bounded_error(error)}"
    return None


def _cleanup_outputs(
    output: Path,
    directory_descriptor: int,
    directory_identity: tuple[int, int],
    files: dict[str, tuple[int, int]],
) -> str | None:
    issues = []
    for name, expected_identity in files.items():
        try:
            observed = os.stat(
                name, dir_fd=directory_descriptor, follow_symlinks=False
            )
        except FileNotFoundError:
            continue
        except OSError as error:
            issues.append(f"cannot inspect {name}: {_bounded_error(error)}")
            continue
        if _identity(observed) != expected_identity:
            issues.append(f"replacement preserved: {name}")
            continue
        try:
            os.unlink(name, dir_fd=directory_descriptor)
        except OSError as error:
            issues.append(f"cannot remove {name}: {_bounded_error(error)}")
    directory_issue = _remove_owned_directory(output, directory_identity)
    if directory_issue is not None:
        issues.append(directory_issue)
    return "; ".join(issues) or None


def _close_stream(stream: BinaryIO | None) -> None:
    if stream is not None and not stream.closed:
        stream.close()


def run_child(
    argv: Sequence[str],
    output_dir: Path,
    *,
    cwd: Path | None = None,
) -> ChildResult:
    """Run one child; by default it inherits the caller's current working directory."""
    wait4 = getattr(os, "wait4", None)
    if not callable(wait4):
        raise ChildRunError("os.wait4 is required for child CPU accounting")
    argv_tuple = _validated_argv(argv)
    child_cwd = _validated_cwd(cwd)
    try:
        output = Path(output_dir).absolute()
    except (TypeError, ValueError) as error:
        raise ChildRunError(f"invalid output directory: {_bounded_error(error)}") from error

    directory_descriptor = -1
    directory_identity: tuple[int, int] | None = None
    files: dict[str, tuple[int, int]] = {}
    stdout_stream: BinaryIO | None = None
    stderr_stream: BinaryIO | None = None
    try:
        try:
            output.mkdir(mode=0o700)
        except FileExistsError as error:
            raise ChildRunError(f"output directory must not exist: {output}") from error
        metadata = output.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or output.is_symlink():
            raise ChildRunError(f"created output is not a real directory: {output}")
        directory_identity = _identity(metadata)
        directory_descriptor = os.open(
            output,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        if _identity(os.fstat(directory_descriptor)) != directory_identity:
            raise ChildRunError("output directory changed before open")

        stdout_stream = _create_raw_file(
            directory_descriptor, "stdout.raw", files
        )
        stderr_stream = _create_raw_file(
            directory_descriptor, "stderr.raw", files
        )
        start = time.monotonic_ns()
        process = subprocess.Popen(
            argv_tuple,
            cwd=child_cwd,
            stdout=stdout_stream,
            stderr=stderr_stream,
            shell=False,
            start_new_session=True,
        )
        pid, status, usage = wait4(process.pid, 0)
        wall_ns = max(0, time.monotonic_ns() - start)
        if pid != process.pid:
            raise ChildRunError(
                f"os.wait4 returned unexpected pid: expected {process.pid}, observed {pid}"
            )
        returncode = os.waitstatus_to_exitcode(status)
        process.returncode = returncode
        cpu_ns = max(
            0,
            round((usage.ru_utime + usage.ru_stime) * 1_000_000_000),
        )

        _close_stream(stdout_stream)
        stdout_stream = None
        _close_stream(stderr_stream)
        stderr_stream = None
        stdout_sha256, stdout_bytes = _hash_raw_file(
            directory_descriptor, "stdout.raw", files["stdout.raw"]
        )
        stderr_sha256, stderr_bytes = _hash_raw_file(
            directory_descriptor, "stderr.raw", files["stderr.raw"]
        )
        if _path_identity(output) != directory_identity:
            raise ChildRunError("output directory changed before result publication")
        return ChildResult(
            argv=argv_tuple,
            returncode=returncode,
            wall_ns=wall_ns,
            cpu_ns=cpu_ns,
            stdout_path=output / "stdout.raw",
            stdout_bytes=stdout_bytes,
            stdout_sha256=stdout_sha256,
            stderr_path=output / "stderr.raw",
            stderr_bytes=stderr_bytes,
            stderr_sha256=stderr_sha256,
        )
    except BaseException as error:
        _close_stream(stdout_stream)
        _close_stream(stderr_stream)
        cleanup_issue = None
        if directory_descriptor >= 0 and directory_identity is not None:
            cleanup_issue = _cleanup_outputs(
                output,
                directory_descriptor,
                directory_identity,
                files,
            )
        elif directory_identity is not None:
            cleanup_issue = _remove_owned_directory(output, directory_identity)
        if isinstance(error, (OSError, ValueError, ChildRunError)):
            message = _bounded_error(error)
            if cleanup_issue is not None:
                message += f"; cleanup: {cleanup_issue}"
            if isinstance(error, ChildRunError) and cleanup_issue is None:
                raise
            raise ChildRunError(message) from error
        raise
    finally:
        if directory_descriptor >= 0:
            os.close(directory_descriptor)
