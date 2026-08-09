from __future__ import annotations

import ctypes
import os
import signal
import threading
import time
from dataclasses import dataclass
from pathlib import Path


PR_SET_CHILD_SUBREAPER = 36
PR_GET_CHILD_SUBREAPER = 37
_REAP_TIMEOUT_NS = 2_000_000_000
_RUN_CHILD_LOCK = threading.Lock()


class SubreaperError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    start_time: int
    parent_pid: int

    @property
    def key(self) -> tuple[int, int]:
        return self.pid, self.start_time


def _prctl(option: int, argument: object) -> int:
    function = ctypes.CDLL(None, use_errno=True).prctl
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
    ]
    function.restype = ctypes.c_int
    if isinstance(argument, ctypes.c_void_p):
        raw_argument = argument.value or 0
    else:
        raw_argument = int(argument)
    result = function(option, raw_argument, 0, 0, 0)
    if result != 0:
        number = ctypes.get_errno()
        raise SubreaperError(f"prctl failed: {os.strerror(number)}")
    return result


def _get_subreaper() -> bool:
    value = ctypes.c_int()
    pointer = ctypes.cast(ctypes.pointer(value), ctypes.c_void_p)
    _prctl(PR_GET_CHILD_SUBREAPER, pointer)
    return bool(value.value)


def _set_subreaper(enabled: bool) -> None:
    _prctl(PR_SET_CHILD_SUBREAPER, int(enabled))


def _process_identity(pid: int) -> ProcessIdentity | None:
    try:
        raw = (Path("/proc") / str(pid) / "stat").read_text(encoding="ascii")
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
        return None
    closing = raw.rfind(")")
    if closing < 0:
        return None
    fields = raw[closing + 2 :].split()
    try:
        parent_pid = int(fields[1])
        start_time = int(fields[19])
    except (IndexError, ValueError):
        return None
    return ProcessIdentity(pid=pid, start_time=start_time, parent_pid=parent_pid)


def _all_processes() -> dict[int, ProcessIdentity]:
    processes: dict[int, ProcessIdentity] = {}
    try:
        entries = os.scandir("/proc")
    except OSError as error:
        raise SubreaperError("cannot enumerate /proc") from error
    with entries:
        for entry in entries:
            if not entry.name.isdigit():
                continue
            identity = _process_identity(int(entry.name))
            if identity is not None:
                processes[identity.pid] = identity
    return processes


def _descendants(parent_pid: int) -> set[tuple[int, int]]:
    processes = _all_processes()
    children: dict[int, list[int]] = {}
    for item in processes.values():
        children.setdefault(item.parent_pid, []).append(item.pid)
    pending = list(children.get(parent_pid, ()))
    descendants: set[tuple[int, int]] = set()
    while pending:
        pid = pending.pop()
        identity = processes.get(pid)
        if identity is None or identity.key in descendants:
            continue
        descendants.add(identity.key)
        pending.extend(children.get(pid, ()))
    return descendants


def _direct_new_children(
    parent_pid: int,
    baseline: set[tuple[int, int]],
) -> list[ProcessIdentity]:
    return [
        item
        for item in _all_processes().values()
        if item.parent_pid == parent_pid and item.key not in baseline
    ]


def _kill_identity(identity: ProcessIdentity) -> None:
    observed = _process_identity(identity.pid)
    if observed is None or observed.key != identity.key:
        return
    try:
        os.kill(identity.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except PermissionError as error:
        raise SubreaperError("permission denied killing adopted descendant") from error


def _reap_identity(identity: ProcessIdentity, deadline_ns: int) -> None:
    while True:
        try:
            waited, _status = os.waitpid(identity.pid, os.WNOHANG)
        except ChildProcessError:
            return
        except InterruptedError:
            continue
        if waited == identity.pid:
            return
        if time.monotonic_ns() >= deadline_ns:
            raise SubreaperError("adopted descendant did not reap before deadline")
        time.sleep(0.005)


class SubreaperScope:
    def __init__(self) -> None:
        self._previous = False
        self._baseline: set[tuple[int, int]] = set()
        self._entered = False

    def __enter__(self) -> SubreaperScope:
        _RUN_CHILD_LOCK.acquire()
        try:
            self._previous = _get_subreaper()
            self._baseline = _descendants(os.getpid())
            _set_subreaper(True)
            self._entered = True
            return self
        except BaseException:
            _RUN_CHILD_LOCK.release()
            raise

    def contain(self) -> bool:
        if not self._entered:
            raise SubreaperError("subreaper scope is not active")
        deadline_ns = time.monotonic_ns() + _REAP_TIMEOUT_NS
        found = False
        empty_scans = 0
        while True:
            children = _direct_new_children(os.getpid(), self._baseline)
            if children:
                found = True
                empty_scans = 0
                for child in children:
                    _kill_identity(child)
                for child in children:
                    _reap_identity(child, deadline_ns)
            else:
                empty_scans += 1
                if empty_scans >= 2:
                    return found
                time.sleep(0.005)
            if time.monotonic_ns() >= deadline_ns:
                raise SubreaperError("descendant containment deadline exceeded")

    def __exit__(self, error_type: object, error: object, traceback: object) -> None:
        try:
            if self._entered:
                try:
                    self.contain()
                finally:
                    _set_subreaper(self._previous)
        finally:
            self._entered = False
            _RUN_CHILD_LOCK.release()
