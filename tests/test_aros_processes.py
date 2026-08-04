from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

import arbor.aros.processes as processes


def _noop() -> None:
    pass


def _wait_until_not_live(pid: int, timeout_seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        except OSError:
            return True
        state = raw.rsplit(")", 1)[1].split()[0]
        if state in {"Z", "X", "x"}:
            return True
        time.sleep(0.02)
    return False


def test_spawn_process_uses_exact_popen_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    process = type("FakeProcess", (), {"pid": 321})()

    def fake_popen(*args: object, **kwargs: object) -> Any:
        calls.append((args, kwargs))
        return process

    monkeypatch.setattr(processes._subprocess, "Popen", fake_popen)
    monkeypatch.setattr(processes._os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(
        processes,
        "_process_start_token",
        lambda pid: f"linux-proc-start:{pid}",
    )
    stdin = object()
    stdout = object()
    stderr = object()
    environment = {"PATH": "/bin"}

    handle = processes.spawn_process(
        ["python", "-c", "pass"],
        cwd=tmp_path,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        env=environment,
        pass_fds=(7, 3, 7),
        preexec_fn=_noop,
    )

    assert processes.__all__ == [
        "ProcessIdentity",
        "ProcessHandle",
        "ParentDeathSetup",
        "spawn_process",
        "identity_is_live",
        "signal_process_group",
        "reap_leader",
        "terminate_and_reap",
    ]
    assert {name for name in vars(processes) if not name.startswith("_")} == set(
        processes.__all__
    )
    assert handle == processes.ProcessHandle(
        process=process,
        identity=processes.ProcessIdentity(
            pid=321,
            pgid=321,
            start_token="linux-proc-start:321",
        ),
    )
    assert calls == [
        (
            (["python", "-c", "pass"],),
            {
                "shell": False,
                "cwd": tmp_path,
                "stdin": stdin,
                "stdout": stdout,
                "stderr": stderr,
                "env": environment,
                "close_fds": True,
                "pass_fds": (3, 7),
                "start_new_session": True,
                "preexec_fn": _noop,
            },
        )
    ]
    with pytest.raises(ValueError, match="mutually exclusive"):
        processes.spawn_process(
            ["python", "-c", "pass"],
            cwd=tmp_path,
            stdin=None,
            stdout=None,
            stderr=None,
            env=None,
            pass_fds=(),
            preexec_fn=_noop,
            parent_death=processes.ParentDeathSetup(
                expected_parent_pid=os.getpid(),
                before_install=_noop,
                after_install=_noop,
            ),
        )
    assert len(calls) == 1


def test_spawn_process_only_preserves_declared_file_descriptors(
    tmp_path: Path,
) -> None:
    kept_read, kept_write = os.pipe()
    dropped_read, dropped_write = os.pipe()
    try:
        os.set_inheritable(kept_read, True)
        os.set_inheritable(dropped_read, True)
        code = """\
import os
import sys

values = [int(value) for value in sys.argv[1:]]

def state(fd, device, inode):
    try:
        metadata = os.fstat(fd)
    except OSError:
        return "closed"
    return "open" if (metadata.st_dev, metadata.st_ino) == (device, inode) else "closed"

pairs = zip(values[::3], values[1::3], values[2::3])
print(" ".join(state(*pair) for pair in pairs), flush=True)
"""
        kept = os.fstat(kept_read)
        dropped = os.fstat(dropped_read)
        handle = processes.spawn_process(
            [
                sys.executable,
                "-c",
                code,
                str(kept_read),
                str(kept.st_dev),
                str(kept.st_ino),
                str(dropped_read),
                str(dropped.st_dev),
                str(dropped.st_ino),
            ],
            cwd=tmp_path,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=None,
            pass_fds=(kept_read,),
        )
        stdout, stderr = handle.process.communicate(timeout=5)

        assert handle.process.returncode == 0, stderr.decode()
        assert stdout == b"open closed\n"
    finally:
        for descriptor in (kept_read, kept_write, dropped_read, dropped_write):
            os.close(descriptor)


def test_spawn_process_identity_capture_failure_terminates_and_reaps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProcess:
        pid = 4321

        def __init__(self) -> None:
            self.wait_calls: list[float | None] = []
            self.kill_calls = 0

        def kill(self) -> None:
            self.kill_calls += 1

        def wait(self, timeout: float | None = None) -> int:
            self.wait_calls.append(timeout)
            return -signal.SIGKILL

    process = FakeProcess()
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(processes._subprocess, "Popen", lambda *_a, **_k: process)
    monkeypatch.setattr(processes._os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(processes, "_process_start_token", lambda _pid: None)
    monkeypatch.setattr(
        processes._os,
        "killpg",
        lambda pgid, signal_number: signals.append((pgid, signal_number)),
    )

    with pytest.raises(RuntimeError, match="capture process identity"):
        processes.spawn_process(
            ["python", "-c", "pass"],
            cwd=tmp_path,
            stdin=None,
            stdout=None,
            stderr=None,
            env=None,
            pass_fds=(),
        )

    assert signals == [(4321, signal.SIGKILL)]
    assert process.kill_calls == 0
    assert process.wait_calls == [None]

    fallback = FakeProcess()
    monkeypatch.setattr(processes._subprocess, "Popen", lambda *_a, **_k: fallback)
    monkeypatch.setattr(processes._os, "getpgid", lambda pid: pid + 1)

    def missing_group(_pgid: int, _signal_number: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(processes._os, "killpg", missing_group)

    with pytest.raises(RuntimeError, match="capture process identity"):
        processes.spawn_process(
            ["python", "-c", "pass"],
            cwd=tmp_path,
            stdin=None,
            stdout=None,
            stderr=None,
            env=None,
            pass_fds=(),
        )

    assert fallback.kill_calls == 1
    assert fallback.wait_calls == [None]


def test_signal_process_group_refuses_reused_pid_or_start_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handle = processes.spawn_process(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=None,
        pass_fds=(),
    )
    reused = processes.ProcessIdentity(
        pid=handle.identity.pid,
        pgid=handle.identity.pgid,
        start_token=f"{handle.identity.start_token}-reused",
    )
    delivered: list[tuple[int, int]] = []
    try:
        with monkeypatch.context() as context:
            context.setattr(
                processes._os,
                "killpg",
                lambda pgid, signal_number: delivered.append((pgid, signal_number)),
            )
            assert processes.identity_is_live(reused) is False
            assert processes.signal_process_group(reused, signal.SIGTERM) is False
        assert delivered == []
    finally:
        processes.terminate_and_reap(handle, grace_seconds=0.1)


def test_terminate_and_reap_escalates_term_to_kill_and_reaps_leader(
    tmp_path: Path,
) -> None:
    handle = processes.spawn_process(
        [
            sys.executable,
            "-c",
            (
                "import signal,time;"
                "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                "print('ready',flush=True);time.sleep(30)"
            ),
        ],
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=None,
        pass_fds=(),
    )
    assert handle.process.stdout is not None
    assert handle.process.stdout.readline() == b"ready\n"

    sequence = processes.terminate_and_reap(handle, grace_seconds=0.05)

    assert sequence == ["TERM", "KILL"]
    assert handle.process.returncode == -signal.SIGKILL


def test_reap_leader_has_bounded_timeout(tmp_path: Path) -> None:
    handle = processes.spawn_process(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=None,
        pass_fds=(),
    )
    try:
        with pytest.raises(TimeoutError, match="reap process leader"):
            processes.reap_leader(handle, timeout_seconds=0.01)
    finally:
        processes.terminate_and_reap(handle, grace_seconds=0.1)


def test_parent_death_setup_order_and_kills_child_with_real_broker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order_read, order_write = os.pipe()
    os.set_inheritable(order_write, True)
    original_getppid = os.getppid
    try:
        with monkeypatch.context() as context:
            context.setattr(
                processes,
                "_set_parent_death_signal",
                lambda: os.write(order_write, b"P"),
            )

            def checked_parent() -> int:
                os.write(order_write, b"C")
                return original_getppid()

            context.setattr(processes._os, "getppid", checked_parent)
            handle = processes.spawn_process(
                [
                    sys.executable,
                    "-c",
                    f"import os;os.write({order_write},b'E')",
                ],
                cwd=tmp_path,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=None,
                pass_fds=(order_write,),
                parent_death=processes.ParentDeathSetup(
                    expected_parent_pid=os.getpid(),
                    before_install=lambda: os.write(order_write, b"B"),
                    after_install=lambda: os.write(order_write, b"A"),
                ),
            )
            assert processes.reap_leader(handle, timeout_seconds=5) == 0
        os.close(order_write)
        order_write = -1
        assert os.read(order_read, 5) == b"BPCAE"
    finally:
        os.close(order_read)
        if order_write >= 0:
            os.close(order_write)

    broker_read, broker_write = os.pipe()
    broker_pid = os.fork()
    if broker_pid == 0:
        os.close(broker_read)
        try:
            child = processes.spawn_process(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                cwd=tmp_path,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=None,
                pass_fds=(),
                parent_death=processes.ParentDeathSetup(
                    expected_parent_pid=os.getpid(),
                    before_install=_noop,
                    after_install=_noop,
                ),
            )
            os.write(broker_write, f"{child.identity.pid}\n".encode())
            time.sleep(30)
        except BaseException:
            os._exit(2)
        os._exit(0)

    os.close(broker_write)
    with os.fdopen(broker_read, "r", encoding="utf-8") as stream:
        line = stream.readline().strip()
    assert line
    child_pid = int(line)
    try:
        os.kill(broker_pid, signal.SIGKILL)
        waited, wait_status = os.waitpid(broker_pid, 0)
        assert waited == broker_pid
        assert os.waitstatus_to_exitcode(wait_status) == -signal.SIGKILL
        assert _wait_until_not_live(child_pid)
    finally:
        try:
            os.killpg(child_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
