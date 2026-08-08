from __future__ import annotations

import ctypes
import errno
import os
import re
import secrets
import signal
import stat
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, DecimalException
from pathlib import Path
from typing import BinaryIO

from .records import quarantine_unlink, sha256_file


RESULT = re.compile(
    r"^[^,]{1,2048} cache size {1,16}[^, ]{1,64}, {1,32}"
    r"(?P<requests>[0-9]{1,20}) req, miss ratio "
    r"(?P<object>[0-9]{1,3}\.[0-9]{1,12}), byte miss ratio "
    r"(?P<byte>[0-9]{1,3}\.[0-9]{1,12}), throughput "
    r"(?P<throughput>[0-9]{1,12}\.[0-9]{1,12}) MQPS$"
)
_LOGGER_HEADER = (
    r"^\[INFO\] {2}[0-9]{2}-[0-9]{2}-[0-9]{4} "
    r"[0-9]{2}:[0-9]{2}:[0-9]{2} {0,7}[A-Za-z0-9_-]{1,60}\.c:"
    r"[0-9]{1,6} {2,5}\(tid=[0-9]{1,20}\): "
)
_CONFIG_LOG = re.compile(
    _LOGGER_HEADER
    + r"trace path: [^,]{1,2048}, trace_type [A-Za-z0-9_-]{1,32}, "
    + r"ofilepath [^,]{1,2048}, [0-9]{1,3} threads, warmup [0-9]{1,10} sec, "
    + r"total [0-9]{1,3} algo x [0-9]{1,3} size = [0-9]{1,5} caches"
    + r"(?:, [A-Za-z][A-Za-z0-9_-]{0,63}){1,64}"
    + r"(?:, trace-type-params: [^,]{1,256})?"
    + r"(?:, admission: [^,]{1,128})?"
    + r"(?:, admission-params: [^,]{1,256})?"
    + r"(?:, prefetch: [^,]{1,128})?"
    + r"(?:, prefetch-params: [^,]{1,256})?"
    + r"(?:, eviction-params: [^,]{1,256})?"
    + r"(?:, use ttl)?(?:, ignore object size)?(?:, consider object metadata)?$"
)
_PROGRESS_LOG = re.compile(
    _LOGGER_HEADER
    + r"[^ ,]{1,255} [A-Za-z0-9_.:+-]{1,128} [0-9]{1,12}\.[0-9]{2} hour: "
    + r"[0-9]{1,20} requests, miss ratio [0-9]{1,3}\.[0-9]{1,12}, "
    + r"interval miss ratio [0-9]{1,3}\.[0-9]{1,12}$"
)
_DISABLE_METADATA_LOG = re.compile(_LOGGER_HEADER + r"disable object metadata$")


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


@dataclass(frozen=True)
class _RawFileReceipt:
    identity: tuple[int, int]
    size: int
    mtime_ns: int
    ctime_ns: int


def _excerpt(line: str) -> str:
    value = repr(line[:120])
    return value + ("..." if len(line) > 120 else "")


def _known_log_line(line: str) -> bool:
    return any(
        expression.fullmatch(line) is not None
        for expression in (_CONFIG_LOG, _PROGRESS_LOG, _DISABLE_METADATA_LOG)
    )


def parse_cachesim_output(output: str) -> ParsedResult:
    if type(output) is not str:
        raise CacheSimOutputError("libCacheSim output must be text")
    if any(
        character != "\n" and not 0x20 <= ord(character) <= 0x7E
        for character in output
    ):
        raise CacheSimOutputError(
            "libCacheSim output must contain printable ASCII and LF only"
        )

    matches: list[re.Match[str]] = []
    for line_number, line in enumerate(output.split("\n"), start=1):
        match = RESULT.fullmatch(line)
        if match is not None:
            matches.append(match)
            continue
        if _known_log_line(line):
            continue
        if line == "":
            continue
        raise CacheSimOutputError(
            f"unrecognized libCacheSim output on line {line_number}: {_excerpt(line)}"
        )

    if len(matches) != 1:
        raise CacheSimOutputError(
            f"expected exactly one libCacheSim result line, found {len(matches)}"
        )
    fields = matches[0].groupdict()
    try:
        request_count = int(fields["requests"])
        object_miss_ratio = Decimal(fields["object"])
        byte_miss_ratio = Decimal(fields["byte"])
        throughput = Decimal(fields["throughput"])
    except (ValueError, DecimalException) as error:
        raise CacheSimOutputError("invalid bounded decimal in libCacheSim result") from error
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


def _bounded_message(message: str, limit: int = 512) -> str:
    if len(message) <= limit:
        return message
    return message[: limit - 3] + "..."


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _raw_file_receipt(metadata: os.stat_result) -> _RawFileReceipt:
    return _RawFileReceipt(
        identity=_identity(metadata),
        size=metadata.st_size,
        mtime_ns=metadata.st_mtime_ns,
        ctime_ns=metadata.st_ctime_ns,
    )


def _create_raw_file(
    directory_descriptor: int,
    name: str,
    files: dict[str, _RawFileReceipt],
) -> BinaryIO:
    descriptor = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
        dir_fd=directory_descriptor,
    )
    try:
        metadata = os.fstat(descriptor)
        files[name] = _raw_file_receipt(metadata)
        if not stat.S_ISREG(metadata.st_mode):
            raise ChildRunError(f"raw output is not a regular file: {name}")
        stream = os.fdopen(descriptor, "wb")
        descriptor = -1
        return stream
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _regular_file_metadata(
    path: Path, receipt: _RawFileReceipt, name: str
) -> os.stat_result:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or _identity(metadata) != receipt.identity
    ):
        raise ChildRunError(f"raw output changed: {name}")
    return metadata


def _seal_and_hash_raw_file(
    path: Path, opened: _RawFileReceipt, name: str
) -> tuple[str, _RawFileReceipt]:
    before = _regular_file_metadata(path, opened, name)
    sealed = _raw_file_receipt(before)
    digest = sha256_file(path)
    after = _regular_file_metadata(path, sealed, name)
    if _raw_file_receipt(after) != sealed:
        raise ChildRunError(f"raw output changed: {name}")
    return digest, sealed


def _revalidate_raw_hash(
    path: Path,
    receipt: _RawFileReceipt,
    expected_sha256: str,
    name: str,
) -> None:
    before = _regular_file_metadata(path, receipt, name)
    observed_sha256 = sha256_file(path)
    after = _regular_file_metadata(path, receipt, name)
    if (
        _raw_file_receipt(before) != receipt
        or _raw_file_receipt(after) != receipt
        or observed_sha256 != expected_sha256
    ):
        raise ChildRunError(f"raw output changed: {name}")


def _path_identity(path: Path) -> tuple[int, int] | None:
    try:
        return _identity(path.lstat())
    except FileNotFoundError:
        return None


def _rename_noreplace(
    directory_descriptor: int, source: str, target: str
) -> None:
    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError as error:
        raise ChildRunError("atomic no-replace rename is unavailable") from error
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        directory_descriptor,
        os.fsencode(source),
        directory_descriptor,
        os.fsencode(target),
        1,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number == errno.ENOENT:
        raise FileNotFoundError(error_number, os.strerror(error_number), source)
    if error_number == errno.EEXIST:
        raise FileExistsError(error_number, os.strerror(error_number), target)
    raise OSError(error_number, os.strerror(error_number), source)


def _remove_owned_directory(
    output: Path, directory_identity: tuple[int, int]
) -> str | None:
    parent_descriptor = -1
    quarantine = f".cachesim-quarantine-{secrets.token_hex(16)}"
    try:
        parent_descriptor = os.open(
            output.parent,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            _rename_noreplace(parent_descriptor, output.name, quarantine)
        except FileNotFoundError:
            return None
        quarantine_descriptor = os.open(
            quarantine,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_descriptor,
        )
        try:
            metadata = os.fstat(quarantine_descriptor)
            owned = (
                stat.S_ISDIR(metadata.st_mode)
                and _identity(metadata) == directory_identity
                and not os.listdir(quarantine_descriptor)
            )
        finally:
            os.close(quarantine_descriptor)
        if not owned:
            try:
                _rename_noreplace(parent_descriptor, quarantine, output.name)
            except OSError:
                pass
            return "replacement or nonempty output directory preserved"
        os.rmdir(quarantine, dir_fd=parent_descriptor)
    except (OSError, ChildRunError) as error:
        return f"cannot remove output directory: {_bounded_error(error)}"
    finally:
        if parent_descriptor >= 0:
            os.close(parent_descriptor)
    return None


def _cleanup_outputs(
    output: Path,
    directory_descriptor: int,
    directory_identity: tuple[int, int],
    files: dict[str, _RawFileReceipt],
) -> str | None:
    issues = []
    for name, receipt in files.items():
        try:
            quarantine_unlink(
                directory_descriptor,
                name,
                receipt.identity,
                fsync_directory=False,
            )
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as error:
            issues.append(f"replacement preserved for {name}: {_bounded_error(error)}")
    directory_issue = _remove_owned_directory(output, directory_identity)
    if directory_issue is not None:
        issues.append(directory_issue)
    return "; ".join(issues) or None


def _close_stream(stream: BinaryIO | None) -> None:
    if stream is not None and not stream.closed:
        stream.close()


def _kill_process_group_and_reap(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    returncode = process.wait()
    process.returncode = returncode


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
    files: dict[str, _RawFileReceipt] = {}
    stdout_stream: BinaryIO | None = None
    stderr_stream: BinaryIO | None = None
    try:
        try:
            output.mkdir(mode=0o700)
        except FileExistsError as error:
            raise ChildRunError("output directory must not exist") from error
        metadata = output.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or output.is_symlink():
            raise ChildRunError("created output is not a real directory")
        directory_identity = _identity(metadata)
        directory_descriptor = os.open(
            output,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        if _identity(os.fstat(directory_descriptor)) != directory_identity:
            raise ChildRunError("output directory changed before open")
        if os.listdir(directory_descriptor):
            raise ChildRunError("created output changed before adoption")

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
        try:
            pid, status, usage = wait4(process.pid, 0)
        except BaseException:
            _kill_process_group_and_reap(process)
            raise
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
        stdout_path = output / "stdout.raw"
        stderr_path = output / "stderr.raw"
        stdout_sha256, files["stdout.raw"] = _seal_and_hash_raw_file(
            stdout_path,
            files["stdout.raw"],
            "stdout.raw",
        )
        stderr_sha256, files["stderr.raw"] = _seal_and_hash_raw_file(
            stderr_path,
            files["stderr.raw"],
            "stderr.raw",
        )
        _revalidate_raw_hash(
            stdout_path,
            files["stdout.raw"],
            stdout_sha256,
            "stdout.raw",
        )
        _revalidate_raw_hash(
            stderr_path,
            files["stderr.raw"],
            stderr_sha256,
            "stderr.raw",
        )
        if _path_identity(output) != directory_identity:
            raise ChildRunError("output directory changed before result publication")
        return ChildResult(
            argv=argv_tuple,
            returncode=returncode,
            wall_ns=wall_ns,
            cpu_ns=cpu_ns,
            stdout_path=stdout_path,
            stdout_bytes=files["stdout.raw"].size,
            stdout_sha256=stdout_sha256,
            stderr_path=stderr_path,
            stderr_bytes=files["stderr.raw"].size,
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
            message = _bounded_message(message)
            if isinstance(error, ChildRunError) and cleanup_issue is None:
                raise
            raise ChildRunError(message) from error
        raise
    finally:
        if directory_descriptor >= 0:
            os.close(directory_descriptor)
