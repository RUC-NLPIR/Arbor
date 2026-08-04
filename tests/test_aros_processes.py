from __future__ import annotations

import inspect
import os
import select
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


def _proc_stat(
    pid: int,
    *,
    state: str = "R",
    pgid: int | None = None,
    starttime: int = 987654,
) -> str:
    fields = [state, "1", str(pid if pgid is None else pgid)]
    fields.extend("0" for _ in range(16))
    fields.append(str(starttime))
    return f"{pid} (test process) {' '.join(fields)}\n"


def _read_announced_pid(descriptor: int) -> int:
    readable, _, _ = select.select([descriptor], [], [], 5)
    assert readable
    raw = os.read(descriptor, 64)
    assert raw.endswith(b"\n")
    return int(raw.strip())


def _kill_process_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


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
    getpgid_calls: list[int] = []
    monkeypatch.setattr(
        processes._os,
        "getpgid",
        lambda pid: getpgid_calls.append(pid) or pid,
    )
    stat_path = Path("/proc/321/stat")
    stat_reads: list[Path] = []
    real_read_text = Path.read_text

    def read_process_stat(path: Path, *args: object, **kwargs: object) -> str:
        if path == stat_path:
            stat_reads.append(path)
            return _proc_stat(321, starttime=321)
        return real_read_text(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", read_process_stat)
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
    default_handle = processes.spawn_process(
        ["python", "-c", "pass"],
        cwd=tmp_path,
    )
    assert default_handle.identity == handle.identity
    assert calls[1] == (
        (["python", "-c", "pass"],),
        {
            "shell": False,
            "cwd": tmp_path,
            "stdin": None,
            "stdout": None,
            "stderr": None,
            "env": None,
            "close_fds": True,
            "pass_fds": (),
            "start_new_session": True,
            "preexec_fn": None,
        },
    )
    assert stat_reads == [stat_path, stat_path]
    assert getpgid_calls == []
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
    assert len(calls) == 2


def test_process_seam_has_exact_function_signatures() -> None:
    spawn = inspect.signature(processes.spawn_process).parameters
    assert list(spawn) == [
        "argv",
        "cwd",
        "stdin",
        "stdout",
        "stderr",
        "env",
        "pass_fds",
        "preexec_fn",
        "parent_death",
    ]
    assert spawn["cwd"].kind is inspect.Parameter.KEYWORD_ONLY
    assert spawn["cwd"].annotation is Path
    assert spawn["cwd"].default is inspect.Parameter.empty
    for name in ("stdin", "stdout", "stderr", "env"):
        assert spawn[name].default is None
    assert spawn["pass_fds"].default == ()
    assert spawn["preexec_fn"].default is None
    assert spawn["parent_death"].default is None

    reap = inspect.signature(processes.reap_leader).parameters
    assert list(reap) == ["handle", "timeout_seconds"]
    assert reap["timeout_seconds"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert reap["timeout_seconds"].default is None

    terminate_signature = inspect.signature(processes.terminate_and_reap)
    terminate = terminate_signature.parameters
    assert list(terminate) == ["handle", "grace_seconds"]
    assert terminate["grace_seconds"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert terminate["grace_seconds"].default == 1.0
    assert terminate_signature.return_annotation == tuple[int, tuple[str, ...]]


def test_process_handle_is_mutable() -> None:
    first = processes.ProcessIdentity(123, 123, "linux-proc-start:1")
    second = processes.ProcessIdentity(456, 456, "linux-proc-start:2")
    handle = processes.ProcessHandle(process=object(), identity=first)  # type: ignore[arg-type]

    handle.identity = second

    assert handle.identity == second


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
    real_read_text = Path.read_text

    def missing_process_stat(
        path: Path,
        *args: object,
        **kwargs: object,
    ) -> str:
        if path == Path("/proc/4321/stat"):
            raise FileNotFoundError(path)
        return real_read_text(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", missing_process_stat)
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

    def mismatched_process_stat(
        path: Path,
        *args: object,
        **kwargs: object,
    ) -> str:
        if path == Path("/proc/4321/stat"):
            return _proc_stat(4321, pgid=4322)
        return real_read_text(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", mismatched_process_stat)

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


@pytest.mark.parametrize(
    ("state", "actual_pgid"),
    (("R", 4322), ("Z", 4321)),
    ids=("actual-pgid-drift", "zombie-leader"),
)
def test_identity_liveness_rejects_actual_pgid_drift_and_zombie(
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    actual_pgid: int,
) -> None:
    identity = processes.ProcessIdentity(
        pid=4321,
        pgid=4321,
        start_token="linux-proc-start:987654",
    )
    reads = 0

    def changed_process_stat(
        path: Path,
        *_args: object,
        **_kwargs: object,
    ) -> str:
        nonlocal reads
        assert path == Path("/proc/4321/stat")
        reads += 1
        return _proc_stat(4321, state=state, pgid=actual_pgid)

    delivered: list[tuple[int, int]] = []
    monkeypatch.setattr(Path, "read_text", changed_process_stat)
    monkeypatch.setattr(
        processes._os,
        "killpg",
        lambda pgid, signal_number: delivered.append((pgid, signal_number)),
    )

    assert processes.identity_is_live(identity) is False
    assert processes.signal_process_group(identity, signal.SIGTERM) is False
    assert reads == 2
    assert delivered == []


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

    exit_code, sequence = processes.terminate_and_reap(handle, 0.05)

    assert exit_code == handle.process.returncode == -signal.SIGKILL
    assert sequence == ("TERM", "KILL")


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
            processes.reap_leader(handle, 0.01)
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
            assert processes.reap_leader(handle, 5) == 0
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


def test_parent_death_before_prctl_race_kills_child_without_exec(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "before-prctl-exec"
    announced_read, announced_write = os.pipe()
    release_read, release_write = os.pipe()
    broker_pid = os.fork()
    if broker_pid == 0:
        os.close(announced_read)
        os.close(release_write)

        def pause_before_install() -> None:
            os.write(announced_write, f"{os.getpid()}\n".encode())
            os.read(release_read, 1)

        try:
            processes.spawn_process(
                [
                    sys.executable,
                    "-c",
                    f"from pathlib import Path;Path({str(marker)!r}).touch()",
                ],
                cwd=tmp_path,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=None,
                pass_fds=(announced_write, release_read),
                parent_death=processes.ParentDeathSetup(
                    expected_parent_pid=os.getpid(),
                    before_install=pause_before_install,
                    after_install=_noop,
                ),
            )
        except BaseException:
            os._exit(2)
        os._exit(3)

    os.close(announced_write)
    os.close(release_read)
    child_pid = _read_announced_pid(announced_read)
    os.close(announced_read)
    try:
        os.kill(broker_pid, signal.SIGKILL)
        waited, wait_status = os.waitpid(broker_pid, 0)
        assert waited == broker_pid
        assert os.waitstatus_to_exitcode(wait_status) == -signal.SIGKILL
        os.write(release_write, b"1")
        os.close(release_write)
        release_write = -1

        assert _wait_until_not_live(child_pid)
        assert not marker.exists()
    finally:
        if release_write >= 0:
            os.close(release_write)
        _kill_process_group(child_pid)


def test_parent_death_after_prctl_before_parent_check_kills_child_without_exec(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "after-prctl-exec"
    announced_read, announced_write = os.pipe()
    release_read, release_write = os.pipe()
    broker_pid = os.fork()
    if broker_pid == 0:
        os.close(announced_read)
        os.close(release_write)
        expected_parent_pid = os.getpid()
        real_getppid = processes._os.getppid

        def pause_during_parent_check() -> int:
            os.write(announced_write, f"{os.getpid()}\n".encode())
            os.read(release_read, 1)
            return real_getppid()

        processes._os.getppid = pause_during_parent_check
        try:
            processes.spawn_process(
                [
                    sys.executable,
                    "-c",
                    f"from pathlib import Path;Path({str(marker)!r}).touch()",
                ],
                cwd=tmp_path,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=None,
                pass_fds=(announced_write, release_read),
                parent_death=processes.ParentDeathSetup(
                    expected_parent_pid=expected_parent_pid,
                    before_install=_noop,
                    after_install=_noop,
                ),
            )
        except BaseException:
            os._exit(2)
        os._exit(3)

    os.close(announced_write)
    os.close(release_read)
    child_pid = _read_announced_pid(announced_read)
    os.close(announced_read)
    try:
        os.kill(broker_pid, signal.SIGKILL)
        waited, wait_status = os.waitpid(broker_pid, 0)
        assert waited == broker_pid
        assert os.waitstatus_to_exitcode(wait_status) == -signal.SIGKILL

        assert _wait_until_not_live(child_pid, timeout_seconds=2)
        assert not marker.exists()
    finally:
        os.close(release_write)
        _kill_process_group(child_pid)


def test_parent_death_mismatch_branch_kills_child_without_after_or_exec(
    tmp_path: Path,
) -> None:
    after_marker = tmp_path / "mismatch-after"
    exec_marker = tmp_path / "mismatch-exec"
    handle = processes.spawn_process(
        [
            sys.executable,
            "-c",
            f"from pathlib import Path;Path({str(exec_marker)!r}).touch()",
        ],
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=None,
        pass_fds=(),
        parent_death=processes.ParentDeathSetup(
            expected_parent_pid=0,
            before_install=_noop,
            after_install=after_marker.touch,
        ),
    )

    exit_code = processes.reap_leader(handle, 5)

    assert exit_code == -signal.SIGKILL
    assert not after_marker.exists()
    assert not exec_marker.exists()
