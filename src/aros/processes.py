"""Narrow synchronous process operations shared by AROS run control."""

import ctypes as _ctypes
import os as _os
import signal as _signal
import subprocess as _subprocess
from collections.abc import Callable as _Callable
from collections.abc import Iterable as _Iterable
from collections.abc import Mapping as _Mapping
from collections.abc import Sequence as _Sequence
from dataclasses import dataclass as _dataclass
from os import PathLike as _PathLike
from typing import IO as _IO
from typing import Any as _Any

from .store import process_start_token as _process_start_token


__all__ = [
    "ProcessIdentity",
    "ProcessHandle",
    "ParentDeathSetup",
    "spawn_process",
    "identity_is_live",
    "signal_process_group",
    "reap_leader",
    "terminate_and_reap",
]

_PR_SET_PDEATHSIG = 1


@_dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    pgid: int
    start_token: str


@_dataclass(frozen=True)
class ProcessHandle:
    process: _subprocess.Popen[bytes]
    identity: ProcessIdentity


@_dataclass(frozen=True)
class ParentDeathSetup:
    expected_parent_pid: int
    before_install: _Callable[[], None]
    after_install: _Callable[[], None]


def _set_parent_death_signal() -> None:
    try:
        libc = _ctypes.CDLL(None, use_errno=True)
        prctl = libc.prctl
        prctl.argtypes = [
            _ctypes.c_int,
            _ctypes.c_ulong,
            _ctypes.c_ulong,
            _ctypes.c_ulong,
            _ctypes.c_ulong,
        ]
        prctl.restype = _ctypes.c_int
        _ctypes.set_errno(0)
        result = prctl(_PR_SET_PDEATHSIG, _signal.SIGKILL, 0, 0, 0)
    except (AttributeError, OSError, TypeError) as error:
        raise OSError("unable to install parent-death signal") from error
    if result != 0:
        error_number = _ctypes.get_errno()
        detail = _os.strerror(error_number) if error_number else "prctl failed"
        raise OSError(
            error_number,
            f"unable to install parent-death signal: {detail}",
        )


def _parent_death_preexec(setup: ParentDeathSetup) -> _Callable[[], None]:
    def install() -> None:
        setup.before_install()
        _set_parent_death_signal()
        if _os.getppid() != setup.expected_parent_pid:
            _os.kill(_os.getpid(), _signal.SIGKILL)
        setup.after_install()

    return install


def _capture_identity(process: _subprocess.Popen[bytes]) -> ProcessIdentity:
    pid = process.pid
    try:
        pgid = _os.getpgid(pid)
    except OSError as error:
        raise RuntimeError(f"unable to capture process identity: {pid}") from error
    start_token = _process_start_token(pid)
    if pgid != pid or start_token is None:
        raise RuntimeError(f"unable to capture process identity: {pid}")
    return ProcessIdentity(pid=pid, pgid=pgid, start_token=start_token)


def _terminate_unidentified(process: _subprocess.Popen[bytes]) -> None:
    try:
        _os.killpg(process.pid, _signal.SIGKILL)
    except ProcessLookupError:
        process.kill()
    finally:
        process.wait()


def spawn_process(
    argv: _Sequence[str],
    *,
    cwd: str | _PathLike[str] | None,
    stdin: int | _IO[_Any] | None,
    stdout: int | _IO[_Any] | None,
    stderr: int | _IO[_Any] | None,
    env: _Mapping[str, str] | None,
    pass_fds: _Iterable[int],
    preexec_fn: _Callable[[], None] | None = None,
    parent_death: ParentDeathSetup | None = None,
) -> ProcessHandle:
    if preexec_fn is not None and parent_death is not None:
        raise ValueError("preexec_fn and parent_death are mutually exclusive")
    child_setup = (
        _parent_death_preexec(parent_death) if parent_death is not None else preexec_fn
    )
    process = _subprocess.Popen(
        list(argv),
        shell=False,
        cwd=cwd,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        env=env,
        close_fds=True,
        pass_fds=tuple(sorted(set(pass_fds))),
        start_new_session=True,
        preexec_fn=child_setup,
    )
    try:
        identity = _capture_identity(process)
    except RuntimeError:
        _terminate_unidentified(process)
        raise
    return ProcessHandle(process=process, identity=identity)


def identity_is_live(identity: ProcessIdentity) -> bool:
    if (
        type(identity.pid) is not int
        or identity.pid <= 1
        or type(identity.pgid) is not int
        or identity.pgid != identity.pid
        or not isinstance(identity.start_token, str)
        or not identity.start_token
    ):
        return False
    return _process_start_token(identity.pid) == identity.start_token


def signal_process_group(
    identity: ProcessIdentity,
    signal_number: int,
) -> bool:
    if not identity_is_live(identity):
        return False
    try:
        _os.killpg(identity.pgid, signal_number)
    except ProcessLookupError:
        return False
    return True


def reap_leader(
    handle: ProcessHandle,
    *,
    timeout_seconds: float | None = None,
) -> int:
    try:
        return handle.process.wait(timeout=timeout_seconds)
    except _subprocess.TimeoutExpired as error:
        raise TimeoutError(
            f"unable to reap process leader: {handle.identity.pid}"
        ) from error


def terminate_and_reap(
    handle: ProcessHandle,
    *,
    grace_seconds: float = 1.0,
    reap_timeout_seconds: float = 2.0,
) -> list[str]:
    if handle.process.poll() is not None:
        reap_leader(handle, timeout_seconds=reap_timeout_seconds)
        return []
    sequence: list[str] = []
    if signal_process_group(handle.identity, _signal.SIGTERM):
        sequence.append("TERM")
    try:
        reap_leader(handle, timeout_seconds=grace_seconds)
    except TimeoutError:
        if signal_process_group(handle.identity, _signal.SIGKILL):
            sequence.append("KILL")
        reap_leader(handle, timeout_seconds=reap_timeout_seconds)
    return sequence
