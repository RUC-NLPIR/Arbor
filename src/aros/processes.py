"""Synchronous operations for a single ProcessHandle owner.

The owner must not reap the leader concurrently with signalling or termination.
Identity validation and process-group signalling are not atomic.
"""

import ctypes as _ctypes
import os as _os
import signal as _signal
import subprocess as _subprocess
import sys as _sys
from collections.abc import Callable as _Callable
from collections.abc import Mapping as _Mapping
from collections.abc import Sequence as _Sequence
from dataclasses import dataclass as _dataclass
from pathlib import Path as _Path
from typing import IO as _IO

__all__ = [
    "ProcessIdentity",
    "ProcessHandle",
    "ParentDeathSetup",
    "ProcessObservationError",
    "enable_child_subreaper",
    "spawn_process",
    "identity_is_live",
    "leader_identity_is_dead",
    "process_group_is_live",
    "process_tree_is_live",
    "signal_process_tree",
    "signal_process_group",
    "reap_leader",
]

_PR_SET_PDEATHSIG = 1
_PR_SET_CHILD_SUBREAPER = 36
_REAP_TIMEOUT_SECONDS = 2.0


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


class ProcessObservationError(RuntimeError):
    """Raised when Linux process truth cannot be observed safely."""


def enable_child_subreaper() -> None:
    if not _sys.platform.startswith("linux"):
        raise OSError("Run child subreaper requires Linux")
    try:
        libc = _ctypes.CDLL(None, use_errno=True)
        result = libc.prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0)
    except (AttributeError, OSError, TypeError) as error:
        raise OSError("unable to enable Run child subreaper") from error
    if result != 0:
        error_number = _ctypes.get_errno()
        raise OSError(
            error_number,
            f"unable to enable Run child subreaper: {_os.strerror(error_number)}",
        )


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
    except (FileNotFoundError, ProcessLookupError):
        return None
    except (OSError, IndexError, ValueError) as error:
        raise ProcessObservationError(f"unable to observe process: {pid}") from error


def _direct_child_pids(runner_pid: int) -> tuple[int, ...]:
    try:
        raw = _Path(
            f"/proc/{runner_pid}/task/{runner_pid}/children"
        ).read_text(encoding="utf-8")
        children = tuple(int(value) for value in raw.split())
    except (OSError, ValueError) as error:
        raise ProcessObservationError(
            f"unable to observe Run descendants: {runner_pid}"
        ) from error
    if any(pid <= 1 for pid in children):
        raise ProcessObservationError(f"invalid Run descendant: {runner_pid}")
    return children


def _adopted_identities(
    runner_pid: int,
    leader_pid: int,
) -> tuple[ProcessIdentity, ...]:
    adopted: list[ProcessIdentity] = []
    for pid in _direct_child_pids(runner_pid):
        if pid == leader_pid:
            continue
        observed = _read_process_stat(pid)
        if observed is not None and observed[0] not in {"Z", "X", "x"}:
            adopted.append(ProcessIdentity(pid, observed[1], observed[2]))
    return tuple(adopted)


def _reap_adopted_children(runner_pid: int, leader_pid: int) -> None:
    for pid in _direct_child_pids(runner_pid):
        if pid == leader_pid:
            continue
        try:
            _os.waitpid(pid, _os.WNOHANG)
        except ChildProcessError:
            continue
        except OSError as error:
            raise ProcessObservationError(
                f"unable to reap Run descendant: {pid}"
            ) from error


def _capture_identity(process: _subprocess.Popen[bytes]) -> ProcessIdentity:
    pid = process.pid
    observed = _read_process_stat(pid)
    if observed is None or observed[1] != pid:
        raise RuntimeError(f"unable to capture process identity: {pid}")
    return ProcessIdentity(pid=pid, pgid=observed[1], start_token=observed[2])


def _terminate_unidentified(process: _subprocess.Popen[bytes]) -> None:
    try:
        _os.killpg(process.pid, _signal.SIGKILL)
    except OSError:
        process.kill()
    try:
        process.wait(timeout=_REAP_TIMEOUT_SECONDS)
    except _subprocess.TimeoutExpired as error:
        raise TimeoutError(
            f"unable to reap unidentified process leader: {process.pid}"
        ) from error


def spawn_process(
    argv: _Sequence[str],
    *,
    cwd: _Path,
    stdin: int | _IO[bytes] | None = None,
    stdout: int | _IO[bytes] | None = None,
    stderr: int | _IO[bytes] | None = None,
    env: _Mapping[str, str] | None = None,
    pass_fds: _Sequence[int] = (),
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


def leader_identity_is_dead(identity: ProcessIdentity) -> bool:
    observed = _read_process_stat(identity.pid)
    return observed is None or (
        observed[0] in {"Z", "X", "x"}
        and observed[1:] == (identity.pgid, identity.start_token)
    )


def process_group_is_live(identity: ProcessIdentity) -> bool:
    observed = _read_process_stat(identity.pid)
    if observed is not None:
        if observed[1:] != (identity.pgid, identity.start_token):
            return False
        if observed[0] not in {"Z", "X", "x"}:
            return identity_is_live(identity)
    try:
        entries = _os.scandir("/proc")
    except OSError as error:
        raise ProcessObservationError("unable to observe Run process group") from error
    with entries:
        live = any(
            (process := _read_process_stat(int(entry.name))) is not None
            and process[0] not in {"Z", "X", "x"}
            and process[1] == identity.pgid
            for entry in entries
            if entry.name.isdecimal()
        )
    confirmed = _read_process_stat(identity.pid)
    return live and (
        confirmed is None
        or confirmed[1:] == (identity.pgid, identity.start_token)
    )


def process_tree_is_live(handle: ProcessHandle, runner_pid: int) -> bool:
    _reap_adopted_children(runner_pid, handle.identity.pid)
    if process_group_is_live(handle.identity):
        return True
    if _adopted_identities(runner_pid, handle.identity.pid):
        return True
    _reap_adopted_children(runner_pid, handle.identity.pid)
    return bool(_adopted_identities(runner_pid, handle.identity.pid))


def signal_process_tree(
    handle: ProcessHandle,
    runner_pid: int,
    signal_number: int,
) -> bool:
    delivered = signal_process_group(handle.identity, signal_number)
    for identity in _adopted_identities(runner_pid, handle.identity.pid):
        if identity.pgid == handle.identity.pgid or not identity_is_live(identity):
            continue
        try:
            _os.kill(identity.pid, signal_number)
        except ProcessLookupError:
            continue
        delivered = True
    return delivered


def signal_process_group(
    identity: ProcessIdentity,
    signal_number: int,
) -> bool:
    if not process_group_is_live(identity):
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
