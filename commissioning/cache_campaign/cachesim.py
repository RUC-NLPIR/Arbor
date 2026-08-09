from __future__ import annotations

import ctypes
import errno
import math
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

from .linux_subreaper import SubreaperError, SubreaperScope
from .records import quarantine_unlink, sha256_file


_DEFAULT_TIMEOUT_SECONDS = 3600.0
_DEFAULT_MAX_OUTPUT_BYTES = 64 * 1024 * 1024
_MAX_TIMEOUT_SECONDS = 24 * 60 * 60
_MAX_OUTPUT_BYTES = 1024 * 1024 * 1024


RESULT = re.compile(
    r"^/[^,\x00-\x1f\x7f]{1,1792} "
    r"[A-Za-z0-9][A-Za-z0-9_.:+-]{0,255} cache size {1,16}"
    r"[^, ]{1,64}, {1,32}"
    r"(?P<requests>[0-9]{1,20}) req, miss ratio "
    r"(?P<object>[0-9]{1,3}\.[0-9]{1,12}), byte miss ratio "
    r"(?P<byte>[0-9]{1,3}\.[0-9]{1,12}), throughput "
    r"(?P<throughput>[0-9]{1,12}\.[0-9]{1,12}) MQPS$"
)


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


def parse_cachesim_output(output: str) -> ParsedResult:
    """Parse libCacheSim stdout; stderr logger text is never accepted here."""
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


def _validated_limits(
    timeout_seconds: float,
    max_output_bytes: int,
) -> tuple[int, int]:
    if (
        type(timeout_seconds) not in {int, float}
        or not math.isfinite(timeout_seconds)
        or not 0 < timeout_seconds <= _MAX_TIMEOUT_SECONDS
    ):
        raise ChildRunError(
            f"timeout_seconds must be in (0, {_MAX_TIMEOUT_SECONDS}]"
        )
    if (
        type(max_output_bytes) is not int
        or not 2 <= max_output_bytes <= _MAX_OUTPUT_BYTES
    ):
        raise ChildRunError(
            f"max_output_bytes must be in [2, {_MAX_OUTPUT_BYTES}]"
        )
    return max(1, round(timeout_seconds * 1_000_000_000)), max_output_bytes


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
        try:
            metadata = os.fstat(descriptor)
        except BaseException:
            retained = os.stat(f"/proc/self/fd/{descriptor}")
            if stat.S_ISREG(retained.st_mode):
                files[name] = _raw_file_receipt(retained)
            raise
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


def _open_output_parent(
    output_dir: Path,
) -> tuple[Path, Path, int, tuple[int, int]]:
    try:
        requested = Path(output_dir).absolute()
    except (TypeError, ValueError) as error:
        raise ChildRunError(f"invalid output directory: {_bounded_error(error)}") from error
    if not requested.name or requested.name in {".", ".."}:
        raise ChildRunError("output directory must have a final name")
    supplied_parent = requested.parent
    try:
        metadata = supplied_parent.lstat()
        resolved_parent = supplied_parent.resolve(strict=True)
    except OSError as error:
        raise ChildRunError(
            f"output parent must be an existing real directory: {_bounded_error(error)}"
        ) from error
    if (
        supplied_parent.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or resolved_parent != supplied_parent
    ):
        raise ChildRunError("output parent must be an existing real directory")

    descriptor = os.open(
        supplied_parent,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        identity = _identity(os.fstat(descriptor))
        if identity != _identity(metadata):
            raise ChildRunError("output parent changed before open")
        try:
            os.stat(requested.name, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise ChildRunError("output directory must not exist")
    except BaseException:
        os.close(descriptor)
        raise
    return resolved_parent / requested.name, resolved_parent, descriptor, identity


def _parent_path_is_bound(parent: Path, identity: tuple[int, int]) -> bool:
    try:
        metadata = parent.lstat()
        return (
            stat.S_ISDIR(metadata.st_mode)
            and not parent.is_symlink()
            and _identity(metadata) == identity
            and parent.resolve(strict=True) == parent
        )
    except OSError:
        return False


def _create_stage_directory(
    parent_descriptor: int,
) -> tuple[str, int, tuple[int, int]]:
    stage_name = ""
    for _ in range(8):
        candidate = f".cachesim-stage-{secrets.token_hex(16)}"
        try:
            os.mkdir(candidate, 0o700, dir_fd=parent_descriptor)
        except FileExistsError:
            continue
        stage_name = candidate
        break
    if not stage_name:
        raise ChildRunError("cannot allocate private output stage")

    descriptor = -1
    identity: tuple[int, int] | None = None
    try:
        descriptor = os.open(
            stage_name,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_descriptor,
        )
        metadata = os.fstat(descriptor)
        identity = _identity(metadata)
        observed = os.stat(
            stage_name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or not stat.S_ISDIR(observed.st_mode)
            or _identity(observed) != identity
            or os.listdir(descriptor)
        ):
            raise ChildRunError("private output stage changed before adoption")
        return stage_name, descriptor, identity
    except BaseException:
        if identity is None:
            try:
                if descriptor >= 0:
                    retained = os.stat(f"/proc/self/fd/{descriptor}")
                else:
                    retained = os.stat(
                        stage_name,
                        dir_fd=parent_descriptor,
                        follow_symlinks=False,
                    )
                if stat.S_ISDIR(retained.st_mode):
                    identity = _identity(retained)
            except OSError:
                pass
        if descriptor >= 0:
            os.close(descriptor)
        if identity is not None:
            _remove_owned_directory(parent_descriptor, stage_name, identity)
        raise


def _descriptor_file_path(directory_descriptor: int, name: str) -> Path:
    return Path(f"/proc/self/fd/{directory_descriptor}") / name


def _revalidate_published_output(
    output: Path,
    parent: Path,
    parent_descriptor: int,
    parent_identity: tuple[int, int],
    directory_identity: tuple[int, int],
    files: dict[str, _RawFileReceipt],
    hashes: dict[str, str],
) -> None:
    if not _parent_path_is_bound(parent, parent_identity):
        raise ChildRunError("output parent changed before result publication")
    final_descriptor = os.open(
        output.name,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_descriptor,
    )
    try:
        if _identity(os.fstat(final_descriptor)) != directory_identity:
            raise ChildRunError("published output directory changed")
        if set(os.listdir(final_descriptor)) != set(files):
            raise ChildRunError("published output entries changed")
        for name, receipt in files.items():
            _revalidate_raw_hash(
                _descriptor_file_path(final_descriptor, name),
                receipt,
                hashes[name],
                name,
            )
        observed = os.stat(
            output.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        canonical = output.lstat()
        if (
            not stat.S_ISDIR(observed.st_mode)
            or _identity(observed) != directory_identity
            or _identity(canonical) != directory_identity
            or not _parent_path_is_bound(parent, parent_identity)
        ):
            raise ChildRunError("published output directory changed")
    finally:
        os.close(final_descriptor)


def _remove_owned_directory(
    parent_descriptor: int,
    name: str,
    directory_identity: tuple[int, int],
) -> str | None:
    quarantine = f".cachesim-quarantine-{secrets.token_hex(16)}"
    try:
        try:
            _rename_noreplace(parent_descriptor, name, quarantine)
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
                _rename_noreplace(parent_descriptor, quarantine, name)
            except OSError:
                pass
            return "replacement or nonempty output directory preserved"
        os.rmdir(quarantine, dir_fd=parent_descriptor)
    except (OSError, ChildRunError) as error:
        return f"cannot remove output directory: {_bounded_error(error)}"
    return None


def _cleanup_outputs(
    parent_descriptor: int,
    directory_name: str,
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
    directory_issue = _remove_owned_directory(
        parent_descriptor,
        directory_name,
        directory_identity,
    )
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


def _drain_pipe(
    descriptor: int,
    stream: BinaryIO,
    remaining: int,
) -> tuple[int, bool]:
    written = 0
    while True:
        try:
            block = os.read(descriptor, 64 * 1024)
        except BlockingIOError:
            return written, False
        if not block:
            return written, False
        allowed = min(len(block), remaining - written)
        if allowed:
            stream.write(block[:allowed])
            written += allowed
        if allowed != len(block):
            return written, True


def _wait4_bounded(
    process: subprocess.Popen[bytes],
    wait4: object,
    stdout_read: int,
    stderr_read: int,
    stdout_stream: BinaryIO,
    stderr_stream: BinaryIO,
    *,
    started_ns: int,
    timeout_ns: int,
    max_output_bytes: int,
) -> tuple[int, int, object, int]:
    deadline_ns = started_ns + timeout_ns
    output_bytes = 0
    while True:
        for descriptor, stream in (
            (stdout_read, stdout_stream),
            (stderr_read, stderr_stream),
        ):
            written, exceeded = _drain_pipe(
                descriptor, stream, max_output_bytes - output_bytes
            )
            output_bytes += written
            if exceeded:
                raise ChildRunError("child output limit exceeded")
        pid, status, usage = wait4(process.pid, os.WNOHANG)  # type: ignore[operator]
        if pid == process.pid:
            return pid, status, usage, output_bytes
        if pid != 0:
            raise ChildRunError(
                f"os.wait4 returned unexpected pid: expected 0 or {process.pid}, "
                f"observed {pid}"
            )
        now_ns = time.monotonic_ns()
        if now_ns >= deadline_ns:
            raise ChildRunError("child timeout exceeded")
        time.sleep(min(0.01, max(0.0, (deadline_ns - now_ns) / 1_000_000_000)))


def _reject_surviving_process_group(process_group: int) -> None:
    # Linux can reuse a PGID after its leader is reaped, so this check must stay
    # immediately after wait4 and before any hashing or other fallible work.
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return
    except PermissionError as error:
        raise ChildRunError(
            "permission denied while checking child process group"
        ) from error
    except OSError as error:
        raise ChildRunError("cannot check child process group") from error

    try:
        os.killpg(process_group, signal.SIGKILL)
    except ProcessLookupError as error:
        raise ChildRunError("child run retained a descendant process") from error
    except PermissionError as error:
        raise ChildRunError(
            "permission denied while killing child process group"
        ) from error
    except OSError as error:
        raise ChildRunError("cannot kill child process group") from error

    deadline = time.monotonic_ns() + 1_000_000_000
    while True:
        try:
            os.killpg(process_group, 0)
        except ProcessLookupError as error:
            raise ChildRunError("child run retained a descendant process") from error
        except PermissionError as error:
            raise ChildRunError(
                "permission denied while polling child process group"
            ) from error
        except OSError as error:
            raise ChildRunError("cannot poll child process group") from error
        if time.monotonic_ns() >= deadline:
            raise ChildRunError("descendant process group survived SIGKILL")
        time.sleep(0.01)


def _run_child_in_scope(
    argv: Sequence[str],
    output_dir: Path,
    *,
    descendant_scope: SubreaperScope,
    cwd: Path | None = None,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES,
) -> ChildResult:
    """Run one child; by default it inherits the caller's current working directory."""
    wait4 = getattr(os, "wait4", None)
    if not callable(wait4):
        raise ChildRunError("os.wait4 is required for child CPU accounting")
    argv_tuple = _validated_argv(argv)
    child_cwd = _validated_cwd(cwd)
    timeout_ns, output_limit = _validated_limits(
        timeout_seconds, max_output_bytes
    )

    output: Path | None = None
    parent: Path | None = None
    parent_descriptor = -1
    parent_identity: tuple[int, int] | None = None
    directory_descriptor = -1
    directory_identity: tuple[int, int] | None = None
    directory_name: str | None = None
    files: dict[str, _RawFileReceipt] = {}
    stdout_stream: BinaryIO | None = None
    stderr_stream: BinaryIO | None = None
    stdout_pipe_stream: BinaryIO | None = None
    stderr_pipe_stream: BinaryIO | None = None
    stdout_read = -1
    stderr_read = -1
    stdout_write = -1
    stderr_write = -1
    try:
        (
            output,
            parent,
            parent_descriptor,
            parent_identity,
        ) = _open_output_parent(output_dir)
        (
            directory_name,
            directory_descriptor,
            directory_identity,
        ) = _create_stage_directory(parent_descriptor)

        stdout_stream = _create_raw_file(
            directory_descriptor, "stdout.raw", files
        )
        stderr_stream = _create_raw_file(
            directory_descriptor, "stderr.raw", files
        )
        stdout_read, stdout_write = os.pipe2(getattr(os, "O_CLOEXEC", 0))
        stderr_read, stderr_write = os.pipe2(getattr(os, "O_CLOEXEC", 0))
        os.set_blocking(stdout_read, False)
        os.set_blocking(stderr_read, False)
        stdout_pipe_stream = os.fdopen(stdout_write, "wb", buffering=0)
        stdout_write = -1
        stderr_pipe_stream = os.fdopen(stderr_write, "wb", buffering=0)
        stderr_write = -1
        start = time.monotonic_ns()
        process = subprocess.Popen(
            argv_tuple,
            cwd=child_cwd,
            stdout=stdout_pipe_stream,
            stderr=stderr_pipe_stream,
            shell=False,
            start_new_session=True,
        )
        _close_stream(stdout_pipe_stream)
        stdout_pipe_stream = None
        _close_stream(stderr_pipe_stream)
        stderr_pipe_stream = None
        try:
            pid, status, usage, output_bytes = _wait4_bounded(
                process,
                wait4,
                stdout_read,
                stderr_read,
                stdout_stream,
                stderr_stream,
                started_ns=start,
                timeout_ns=timeout_ns,
                max_output_bytes=output_limit,
            )
        except BaseException:
            _kill_process_group_and_reap(process)
            raise
        if pid != process.pid:
            raise ChildRunError(
                f"os.wait4 returned unexpected pid: expected {process.pid}, observed {pid}"
            )
        returncode = os.waitstatus_to_exitcode(status)
        process.returncode = returncode
        wall_ns = max(0, time.monotonic_ns() - start)
        _reject_surviving_process_group(process.pid)
        if descendant_scope.contain():
            raise ChildRunError("child run retained an escaped descendant process")
        for descriptor, stream in (
            (stdout_read, stdout_stream),
            (stderr_read, stderr_stream),
        ):
            written, exceeded = _drain_pipe(
                descriptor, stream, output_limit - output_bytes
            )
            output_bytes += written
            if exceeded:
                raise ChildRunError("child output limit exceeded")
        cpu_ns = max(
            0,
            round((usage.ru_utime + usage.ru_stime) * 1_000_000_000),
        )

        _close_stream(stdout_stream)
        stdout_stream = None
        _close_stream(stderr_stream)
        stderr_stream = None
        stage_stdout_path = _descriptor_file_path(
            directory_descriptor, "stdout.raw"
        )
        stage_stderr_path = _descriptor_file_path(
            directory_descriptor, "stderr.raw"
        )
        stdout_sha256, files["stdout.raw"] = _seal_and_hash_raw_file(
            stage_stdout_path,
            files["stdout.raw"],
            "stdout.raw",
        )
        stderr_sha256, files["stderr.raw"] = _seal_and_hash_raw_file(
            stage_stderr_path,
            files["stderr.raw"],
            "stderr.raw",
        )
        _revalidate_raw_hash(
            stage_stdout_path,
            files["stdout.raw"],
            stdout_sha256,
            "stdout.raw",
        )
        _revalidate_raw_hash(
            stage_stderr_path,
            files["stderr.raw"],
            stderr_sha256,
            "stderr.raw",
        )
        if not _parent_path_is_bound(parent, parent_identity):
            raise ChildRunError("output parent changed before publication")
        _rename_noreplace(parent_descriptor, directory_name, output.name)
        directory_name = output.name
        hashes = {
            "stdout.raw": stdout_sha256,
            "stderr.raw": stderr_sha256,
        }
        _revalidate_published_output(
            output,
            parent,
            parent_descriptor,
            parent_identity,
            directory_identity,
            files,
            hashes,
        )
        stdout_path = output / "stdout.raw"
        stderr_path = output / "stderr.raw"
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
        containment_issue = None
        try:
            descendant_scope.contain()
        except (OSError, RuntimeError) as containment_error:
            containment_issue = _bounded_error(containment_error)
        _close_stream(stdout_pipe_stream)
        _close_stream(stderr_pipe_stream)
        _close_stream(stdout_stream)
        _close_stream(stderr_stream)
        cleanup_issue = None
        if (
            parent_descriptor >= 0
            and directory_name is not None
            and directory_descriptor >= 0
            and directory_identity is not None
        ):
            cleanup_issue = _cleanup_outputs(
                parent_descriptor,
                directory_name,
                directory_descriptor,
                directory_identity,
                files,
            )
        elif (
            parent_descriptor >= 0
            and directory_name is not None
            and directory_identity is not None
        ):
            cleanup_issue = _remove_owned_directory(
                parent_descriptor,
                directory_name,
                directory_identity,
            )
        if isinstance(error, (OSError, ValueError, ChildRunError)):
            message = _bounded_error(error)
            if cleanup_issue is not None:
                message += f"; cleanup: {cleanup_issue}"
            if containment_issue is not None:
                message += f"; containment: {containment_issue}"
            message = _bounded_message(message)
            if isinstance(error, ChildRunError) and cleanup_issue is None:
                raise
            raise ChildRunError(message) from error
        raise
    finally:
        if stdout_read >= 0:
            os.close(stdout_read)
        if stderr_read >= 0:
            os.close(stderr_read)
        if stdout_write >= 0:
            os.close(stdout_write)
        if stderr_write >= 0:
            os.close(stderr_write)
        if directory_descriptor >= 0:
            os.close(directory_descriptor)
        if parent_descriptor >= 0:
            os.close(parent_descriptor)


def run_child(
    argv: Sequence[str],
    output_dir: Path,
    *,
    cwd: Path | None = None,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES,
) -> ChildResult:
    """Run one bounded child; cwd inherits the caller's current working directory."""
    try:
        with SubreaperScope() as descendant_scope:
            return _run_child_in_scope(
                argv,
                output_dir,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
                max_output_bytes=max_output_bytes,
                descendant_scope=descendant_scope,
            )
    except SubreaperError as error:
        raise ChildRunError(_bounded_message(_bounded_error(error))) from error
