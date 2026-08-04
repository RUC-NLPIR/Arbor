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
from pathlib import Path as _Path
from typing import IO as _IO
from typing import Any as _Any

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


@_dataclass
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


def _read_process_stat(pid: int) -> tuple[str, int, str] | None:
    if type(pid) is not int or pid <= 1:
        return None
    try:
        raw = _Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        fields_after_name = raw.rsplit(")", 1)[1].split()
        return (
            fields_after_name[0],
            int(fields_after_name[2]),
            f"linux-proc-start:{fields_after_name[19]}",
        )
    except (OSError, IndexError, ValueError):
        return None


def _capture_identity(process: _subprocess.Popen[bytes]) -> ProcessIdentity:
    pid = process.pid
    observed = _read_process_stat(pid)
    if observed is None or observed[1] != pid:
        raise RuntimeError(f"unable to capture process identity: {pid}")
    return ProcessIdentity(pid=pid, pgid=observed[1], start_token=observed[2])


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
    cwd: _Path,
    stdin: int | _IO[_Any] | None = None,
    stdout: int | _IO[_Any] | None = None,
    stderr: int | _IO[_Any] | None = None,
    env: _Mapping[str, str] | None = None,
    pass_fds: _Iterable[int] = (),
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
        or identity.pgid <= 1
        or not isinstance(identity.start_token, str)
        or not identity.start_token
    ):
        return False
    observed = _read_process_stat(identity.pid)
    return bool(
        observed is not None
        and observed[0] not in {"Z", "X", "x"}
        and observed[1] == identity.pgid
        and observed[2] == identity.start_token
    )


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
    grace_seconds: float = 1.0,
) -> tuple[int, tuple[str, ...]]:
    if handle.process.poll() is not None:
        return reap_leader(handle), ()
    sequence: list[str] = []
    if signal_process_group(handle.identity, _signal.SIGTERM):
        sequence.append("TERM")
    try:
        exit_code = reap_leader(handle, grace_seconds)
    except TimeoutError:
        if signal_process_group(handle.identity, _signal.SIGKILL):
            sequence.append("KILL")
        exit_code = reap_leader(handle)
    return exit_code, tuple(sequence)
