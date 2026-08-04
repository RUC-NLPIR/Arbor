"""Durable execution tests for AROS child tasks."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import shlex
import signal
import shutil
import socket
import stat
import subprocess
import sys
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Event, get_ident

import pytest

import arbor.aros.task_runner as task_runner_module
import arbor.aros.tasks as tasks_module
from arbor.aros.store import atomic_write_json, json_sha256, process_start_token
from arbor.aros.task_runner import launched_status, lost_status, run as run_task
from arbor.aros.tasks import TaskError, TaskService
from arbor.aros.workspace import init_workspace


TERMINAL_STATES = {
    "completed",
    "failed_process",
    "timed_out",
    "cancelled",
    "lost",
}


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _init_workspace(root: Path) -> None:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    _git(root, "config", "user.email", "aros@example.invalid")
    _git(root, "config", "user.name", "AROS test")
    (root / "README.md").write_text("# test workspace\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-qm", "initial state")
    init_workspace(root, "Test child task execution")
    _git(root, "add", ".gitignore", "AGENTS.md", "AROS.md", "memory/NOW.md")
    _git(root, "commit", "-qm", "initialize AROS")


def _create_committed_task(
    root: Path,
    argv: list[str],
    *,
    timeout_seconds: float = 10,
    key: str = "runner-task",
) -> tuple[TaskService, dict[str, object]]:
    _init_workspace(root)
    service = TaskService(root)
    brief = service.create(
        "execute exact child adapter",
        actor="principal",
        mode="write",
        adapter_argv=argv,
        capabilities={"network": True, "shell": True},
        deliverables=["observation.json"],
        acceptance=["inspect immutable final receipt"],
        timeout_seconds=timeout_seconds,
        idempotency_key=key,
    )
    task_id = str(brief["task_id"])
    _git(root, "add", f"tasks/{task_id}/brief.json")
    _git(root, "commit", "-qm", f"record {task_id}")
    return service, brief


def _normalized_permission_probe(runtime: Path) -> dict[str, object]:
    return {
        "requested_mode": 0o600,
        "observed_mode": 0o666,
        "mode_request_supported": False,
        "device": runtime.stat().st_dev,
        "enforced": False,
    }


def _wait_terminal(
    service: TaskService,
    task_id: str,
    *,
    timeout: float = 10,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = service.status(task_id)
        if status["state"] in TERMINAL_STATES:
            return status
        time.sleep(0.02)
    pytest.fail(f"task did not reach a terminal state: {service.status(task_id)}")


def _wait_state(
    service: TaskService,
    task_id: str,
    expected: str,
    *,
    timeout: float = 10,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = service.status(task_id)
        if status["state"] == expected:
            return status
        if status["state"] in TERMINAL_STATES:
            pytest.fail(f"task reached {status['state']} before {expected}: {status}")
        time.sleep(0.02)
    pytest.fail(f"task did not reach {expected}: {service.status(task_id)}")


def _fake_absent_tmux_carrier(
    monkeypatch: pytest.MonkeyPatch,
) -> list[list[str]]:
    original_run = tasks_module.subprocess.run
    carrier_calls: list[list[str]] = []

    def run_carrier(
        _service: TaskService,
        _lock_descriptor: int,
        command: list[str],
        _environment: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        carrier_calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    def absent_carrier(
        command: list[str],
        *args: object,
        **kwargs: object,
    ) -> object:
        if "has-session" in command:
            return subprocess.CompletedProcess(command, 1, "", "")
        return original_run(command, *args, **kwargs)

    monkeypatch.setattr(TaskService, "_run_carrier_guardian", run_carrier)
    monkeypatch.setattr(tasks_module.subprocess, "run", absent_carrier)
    return carrier_calls


def _term_ignoring_process_tree_code() -> str:
    descendant = (
        "import signal,time;"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        "signal.signal(signal.SIGINT,signal.SIG_IGN);"
        "time.sleep(30)"
    )
    return (
        "import signal,subprocess,sys,time;from pathlib import Path;"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        "signal.signal(signal.SIGINT,signal.SIG_IGN);"
        f"child=subprocess.Popen([sys.executable,'-c',{descendant!r}]);"
        "Path('descendant.pid').write_text(str(child.pid),encoding='utf-8');"
        "time.sleep(30)"
    )


def _exiting_leader_process_tree_code(descendant: str) -> str:
    return (
        "import subprocess,sys,time\n"
        "from pathlib import Path\n"
        f"child=subprocess.Popen([sys.executable,'-c',{descendant!r}])\n"
        "Path('descendant.pid').write_text(str(child.pid),encoding='utf-8')\n"
        "deadline=time.monotonic()+5\n"
        "while not Path('descendant.ready').exists() and time.monotonic()<deadline:\n"
        "    time.sleep(0.01)\n"
        "if not Path('descendant.ready').exists():\n"
        "    raise SystemExit(2)\n"
        "print('leader stdout',flush=True)\n"
    )


def _persistent_escaped_session_process_tree_code(
    *,
    keep_leader_alive: bool = False,
    ignore_leader_stop_signals: bool = False,
    wait_for_leader_release: bool = False,
) -> str:
    descendant = (
        "import time\n"
        "from pathlib import Path\n"
        "Path('escaped.ready').touch()\n"
        "time.sleep(30)\n"
    )
    code = (
        "import signal,subprocess,sys,time\n"
        "from pathlib import Path\n"
        + (
            "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
            "signal.signal(signal.SIGINT,signal.SIG_IGN)\n"
            if ignore_leader_stop_signals
            else ""
        )
        + f"child=subprocess.Popen([sys.executable,'-c',{descendant!r}],"
        "start_new_session=True)\n"
        "Path('escaped.pid').write_text(str(child.pid),encoding='utf-8')\n"
        "deadline=time.monotonic()+5\n"
        "while not Path('escaped.ready').exists() and time.monotonic()<deadline:\n"
        "    time.sleep(0.01)\n"
        "if not Path('escaped.ready').exists():\n"
        "    raise SystemExit(2)\n"
    )
    if wait_for_leader_release:
        code += (
            "while not Path('leader.release').exists():\n"
            "    time.sleep(0.01)\n"
        )
    elif keep_leader_alive:
        code += "time.sleep(30)\n"
    return code


def _process_state(pid: int) -> str | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        return raw.rsplit(")", 1)[1].split()[0]
    except (IndexError, ValueError):
        return None


def _process_is_running(pid: int) -> bool:
    state = _process_state(pid)
    return state is not None and state not in {"Z", "X", "x"}


def _assert_processes_stop(*pids: int) -> None:
    deadline = time.monotonic() + 5
    while any(_process_is_running(pid) for pid in pids) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert all(not _process_is_running(pid) for pid in pids)


def _best_effort_test_pid(path: Path, field: str | None = None) -> int | None:
    try:
        raw = path.read_text(encoding="utf-8")
        value = int(raw) if field is None else json.loads(raw).get(field)
    except (AttributeError, OSError, UnicodeError, ValueError):
        return None
    return value if type(value) is int and value > 1 else None


def _kill_test_process(pid: int | None, *, group: bool) -> None:
    if pid is None or not _process_is_running(pid):
        return
    try:
        if group:
            os.killpg(pid, signal.SIGKILL)
        else:
            os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


@contextmanager
def _persistent_escaped_session_task(
    root: Path,
    *,
    timeout_seconds: float,
    key: str,
    keep_leader_alive: bool = False,
    ignore_leader_stop_signals: bool = False,
    wait_for_leader_release: bool = False,
) -> Iterator[tuple[TaskService, str, Path, Path, int, int, int]]:
    service, brief = _create_committed_task(
        root,
        [
            sys.executable,
            "-c",
            _persistent_escaped_session_process_tree_code(
                keep_leader_alive=keep_leader_alive,
                ignore_leader_stop_signals=ignore_leader_stop_signals,
                wait_for_leader_release=wait_for_leader_release,
            ),
        ],
        timeout_seconds=timeout_seconds,
        key=key,
    )
    task_id = str(brief["task_id"])
    runtime = root / ".aros" / "tasks" / task_id
    worktree = root / ".worktree" / "tasks" / task_id
    runner_pid: int | None = None
    adapter_pid: int | None = None
    descendant_pid: int | None = None
    try:
        service.start(task_id)
        ready_path = worktree / "escaped.ready"
        deadline = time.monotonic() + 5
        while not ready_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready_path.exists()
        descendant_pid = _best_effort_test_pid(worktree / "escaped.pid")
        runner_pid = _best_effort_test_pid(runtime / "execution.json", "runner_pid")
        adapter_pid = _best_effort_test_pid(runtime / "adapter.json", "adapter_pid")
        assert runner_pid is not None
        assert adapter_pid is not None
        assert descendant_pid is not None
        yield (
            service,
            task_id,
            runtime,
            worktree,
            runner_pid,
            adapter_pid,
            descendant_pid,
        )
    finally:
        if wait_for_leader_release:
            try:
                (worktree / "leader.release").touch(exist_ok=True)
            except OSError:
                pass
        runner_pid = runner_pid or _best_effort_test_pid(
            runtime / "execution.json",
            "runner_pid",
        )
        adapter_pid = adapter_pid or _best_effort_test_pid(
            runtime / "adapter.json",
            "adapter_pid",
        )
        descendant_pid = descendant_pid or _best_effort_test_pid(
            worktree / "escaped.pid"
        )
        _kill_test_process(adapter_pid, group=True)
        _kill_test_process(runner_pid, group=False)
        _kill_test_process(descendant_pid, group=True)
        recorded_pids = tuple(
            pid for pid in (adapter_pid, runner_pid, descendant_pid) if pid is not None
        )
        _assert_processes_stop(*recorded_pids)


def _spawn_unreaped_zombie_session_leader() -> tuple[int, str]:
    ready_read, ready_write = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(ready_read)
        os.setsid()
        os.write(ready_write, b"1")
        os.close(ready_write)
        os._exit(0)
    os.close(ready_write)
    assert os.read(ready_read, 1) == b"1"
    os.close(ready_read)
    deadline = time.monotonic() + 3
    state = ""
    while time.monotonic() < deadline:
        try:
            raw = Path(f"/proc/{child}/stat").read_text(encoding="utf-8")
            state = raw.rsplit(")", 1)[1].split()[0]
        except (OSError, IndexError):
            state = ""
        if state == "Z":
            break
        time.sleep(0.01)
    assert state == "Z"
    token = process_start_token(child)
    assert token is not None
    return child, token


def _spawn_zombie_leader_with_live_descendant() -> tuple[int, int, str]:
    ready_read, ready_write = os.pipe()
    leader = os.fork()
    if leader == 0:
        os.close(ready_read)
        os.setsid()
        descendant = os.fork()
        if descendant == 0:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            os.write(ready_write, str(os.getpid()).encode("ascii"))
            os.close(ready_write)
            time.sleep(30)
            os._exit(0)
        os.close(ready_write)
        os._exit(0)
    os.close(ready_write)
    descendant = int(os.read(ready_read, 32).decode("ascii"))
    os.close(ready_read)
    deadline = time.monotonic() + 3
    while _process_state(leader) != "Z" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert _process_state(leader) == "Z"
    token = process_start_token(leader)
    assert token is not None
    assert _process_is_running(descendant)
    return leader, descendant, token


def test_process_liveness_rejects_unreaped_zombie() -> None:
    child, token = _spawn_unreaped_zombie_session_leader()
    try:
        assert os.getpgid(child) == child
        assert not task_runner_module.process_status_is_live(
            {
                "host": socket.gethostname(),
                "adapter_pid": child,
                "adapter_pgid": child,
                "adapter_start_token": token,
            }
        )
    finally:
        os.waitpid(child, 0)


def test_exact_live_claimed_group_skips_process_group_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pid = 4242
    token = "linux-proc-start:123"
    monkeypatch.setattr(
        task_runner_module,
        "_process_state_group_and_token",
        lambda _pid: ("S", pid, token),
    )

    def reject_process_group_scan(_pgid: int) -> bool:
        raise AssertionError("exact live leader must not scan /proc")

    monkeypatch.setattr(
        task_runner_module,
        "_process_group_has_live_member",
        reject_process_group_scan,
    )

    assert task_runner_module._claimed_group_is_live(pid, pid, token)


@pytest.mark.parametrize("initial_state", (None, "Z"), ids=("gone", "zombie"))
def test_claimed_group_scan_rechecks_and_rejects_reused_leader_identity(
    monkeypatch: pytest.MonkeyPatch,
    initial_state: str | None,
) -> None:
    pid = 4242
    token = "linux-proc-start:123"
    initial_identity = (
        None if initial_state is None else (initial_state, pid, token)
    )
    identities = iter(
        (initial_identity, ("S", pid, "linux-proc-start:124"))
    )
    identity_reads: list[int] = []
    group_scans: list[int] = []

    def sequenced_identity(observed_pid: int) -> tuple[str, int, str] | None:
        identity_reads.append(observed_pid)
        return next(identities)

    def live_group(observed_pgid: int) -> bool:
        group_scans.append(observed_pgid)
        return True

    monkeypatch.setattr(
        task_runner_module,
        "_process_state_group_and_token",
        sequenced_identity,
    )
    monkeypatch.setattr(
        task_runner_module,
        "_process_group_has_live_member",
        live_group,
    )

    assert not task_runner_module._claimed_group_is_live(pid, pid, token)
    assert identity_reads == [pid, pid]
    assert group_scans == [pid]


def test_zombie_only_group_termination_delivers_no_signal() -> None:
    leader, token = _spawn_unreaped_zombie_session_leader()
    request = {
        "host": socket.gethostname(),
        "adapter_pid": leader,
        "adapter_pgid": leader,
        "adapter_start_token": token,
        "signal": "TERM",
    }
    try:
        assert task_runner_module._terminate_recorded_group(
            request,
            grace_seconds=0.05,
        ) == []
    finally:
        os.waitpid(leader, 0)


def test_zombie_leader_with_live_descendant_is_live_and_stoppable() -> None:
    leader, descendant, token = _spawn_zombie_leader_with_live_descendant()
    request = {
        "host": socket.gethostname(),
        "adapter_pid": leader,
        "adapter_pgid": leader,
        "adapter_start_token": token,
        "signal": "TERM",
    }
    try:
        assert task_runner_module.process_status_is_live(request)
        assert task_runner_module._terminate_recorded_group(
            request,
            grace_seconds=0.05,
        ) == ["TERM", "KILL"]
        _assert_processes_stop(descendant)
    finally:
        if _process_is_running(descendant):
            os.killpg(leader, signal.SIGKILL)
        _assert_processes_stop(descendant)
        os.waitpid(leader, 0)


@pytest.mark.parametrize("mismatch", ("token", "pgid"))
def test_group_termination_refuses_reused_leader_identity(mismatch: str) -> None:
    process = subprocess.Popen(
        [sys.executable, "-c", "import time;time.sleep(30)"],
        start_new_session=True,
    )
    token = process_start_token(process.pid)
    assert token is not None
    adapter_identity = (
        process.pid,
        process.pid + 1 if mismatch == "pgid" else process.pid,
        "linux-proc-start:0" if mismatch == "token" else token,
    )
    try:
        assert task_runner_module._terminate_group(
            process,
            adapter_identity=adapter_identity,
            grace_seconds=0.05,
        ) == []
        assert _process_is_running(process.pid)
    finally:
        if _process_is_running(process.pid):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


def test_public_start_launches_exact_argv_in_owned_worktree_with_scrubbed_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    injected = {
        "AWS_SECRET_ACCESS_KEY": "aws-secret",
        "AZURE_OPENAI_API_KEY": "azure-secret",
        "BASH_ENV": str(tmp_path / "bash-startup"),
        "GIT_CONFIG_COUNT": "1",
        "LD_PRELOAD": str(tmp_path / "loader.so"),
        "OPENAI_API_KEY": "provider-secret",
        "PYTHONPATH": str(tmp_path / "python-startup"),
        "SSH_AUTH_SOCK": str(tmp_path / "agent.sock"),
    }
    marker = tmp_path / "shell-expanded"
    code = (
        "import json,os,sys;from pathlib import Path;"
        "Path('observation.json').write_text(json.dumps("
        "{'argv':sys.argv[1:],'cwd':os.getcwd(),'env':dict(os.environ)},"
        "sort_keys=True),encoding='utf-8');"
        "print('adapter stdout');"
        "print('adapter stderr',file=sys.stderr)"
    )
    exact_arguments = [
        f"; touch {marker}",
        f"$(touch {marker})",
        "space preserved",
        "*?[brackets]",
    ]
    service, brief = _create_committed_task(
        tmp_path,
        [sys.executable, "-c", code, *exact_arguments],
    )
    task_id = str(brief["task_id"])
    worktree = tmp_path / ".worktree" / "tasks" / task_id
    expected_task_environment = {
        "AROS_TASK_ID": task_id,
        "AROS_TASK_BRIEF": str(tmp_path / "tasks" / task_id / "brief.json"),
        "AROS_TASK_WORKTREE": str(worktree),
        "AROS_TASK_BASE_COMMIT": str(brief["base_commit"]),
        "AROS_TASK_BRIEF_SHA256": str(brief["brief_sha256"]),
    }
    ambient_task_environment = {
        "AROS_TASK_ID": "wrong-task-id",
        "AROS_TASK_BRIEF": "/wrong/brief.json",
        "AROS_TASK_WORKTREE": "/wrong/worktree",
        "AROS_TASK_BASE_COMMIT": "wrong-base-commit",
        "AROS_TASK_BRIEF_SHA256": "wrong-brief-sha256",
    }
    for key, value in injected.items():
        monkeypatch.setenv(key, value)
    for key, value in ambient_task_environment.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("OMP_NUM_THREADS", "7")
    monkeypatch.setenv("TZ", "Etc/UTC")

    service.start(task_id, actor="launch-principal")
    status = _wait_terminal(service, task_id)

    runtime = tmp_path / ".aros" / "tasks" / task_id
    observation = json.loads(
        (worktree / "observation.json").read_text(encoding="utf-8")
    )
    assert observation["argv"] == exact_arguments
    assert observation["cwd"] == str(worktree)
    assert not marker.exists()
    environment = observation["env"]
    assert environment["HOME"] == str(runtime / "home")
    assert environment["TMPDIR"] == str(runtime / "tmp")
    assert environment["OMP_NUM_THREADS"] == "7"
    assert environment["TZ"] == "Etc/UTC"
    assert environment.get("PATH") == os.environ.get("PATH")
    assert {
        key: environment[key] for key in expected_task_environment
    } == expected_task_environment
    assert not set(injected).intersection(environment)
    assert not any(
        key.startswith(("GIT_", "PYTHON", "LD_", "DYLD_")) for key in environment
    )

    launch = json.loads((runtime / "launch.json").read_text(encoding="utf-8"))
    assert launch == {
        **launch,
        "schema_version": 1,
        "task_id": task_id,
        "actor": "launch-principal",
        "brief_sha256": brief["brief_sha256"],
        "security_profile": "trusted-local",
        "isolation_scope": "application",
        "capabilities_enforced": False,
        "filesystem_permissions_enforced": True,
        "filesystem_permission_probe": {
            "requested_mode": 0o600,
            "observed_mode": 0o600,
            "mode_request_supported": True,
            "device": runtime.stat().st_dev,
            "enforced": True,
        },
        "carrier": "tmux",
        "launch_sha256": json_sha256(
            {key: value for key, value in launch.items() if key != "launch_sha256"}
        ),
    }
    invocation = launch["runner_invocation"]
    assert launch["runner_cwd"] == str(runtime / "home")
    assert invocation == [
        sys.executable,
        "-I",
        "-c",
        tasks_module._TASK_RUNNER_BOOTSTRAP,
        str(runtime / "runner-import"),
        "--workspace",
        str(tmp_path),
        "--task-id",
        task_id,
    ]
    assert not any(argument in invocation for argument in exact_arguments)

    assert status["state"] == "completed"
    for field in (
        "runner_pid",
        "runner_pgid",
        "runner_start_token",
        "adapter_pid",
        "adapter_pgid",
        "adapter_start_token",
        "host",
        "started_at",
        "heartbeat_at",
    ):
        assert status[field]
    final = json.loads((runtime / "final.json").read_text(encoding="utf-8"))
    assert final["brief_sha256"] == brief["brief_sha256"]
    assert final["ownership_sha256"] == launch["ownership_sha256"]
    assert final["launch_sha256"] == launch["launch_sha256"]
    assert final["exit_code"] == 0
    assert final["state"] == "completed"
    assert final["filesystem_permissions_enforced"] is True
    assert final["filesystem_permission_probe"] == launch[
        "filesystem_permission_probe"
    ]
    assert final["final_sha256"] == json_sha256(
        {key: value for key, value in final.items() if key != "final_sha256"}
    )
    assert final["stdout"] == {
        "path": f".aros/tasks/{task_id}/stdout.log",
        "bytes": len(b"adapter stdout\n"),
        "sha256": hashlib.sha256(b"adapter stdout\n").hexdigest(),
    }
    assert final["stderr"] == {
        "path": f".aros/tasks/{task_id}/stderr.log",
        "bytes": len(b"adapter stderr\n"),
        "sha256": hashlib.sha256(b"adapter stderr\n").hexdigest(),
    }
    assert "primary_metric" not in final
    for name in ("stdout.log", "stderr.log", "final.json", "launch.json"):
        metadata = (runtime / name).lstat()
        assert stat.S_ISREG(metadata.st_mode)
        assert stat.S_IMODE(metadata.st_mode) == 0o600


def test_normalized_permissions_preserve_fresh_single_link_log_flow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        tasks_module,
        "_probe_filesystem_permissions",
        _normalized_permission_probe,
    )
    service, brief = _create_committed_task(
        tmp_path,
        [sys.executable, "-c", "print('normalized output')"],
        key="normalized-permission-flow",
    )
    task_id = str(brief["task_id"])
    runtime = tmp_path / ".aros" / "tasks" / task_id
    original_guardian = TaskService._run_carrier_guardian

    def normalize_before_tmux(
        task_service: TaskService,
        lock_descriptor: int,
        command: list[str],
        environment: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        for name in ("launch.json", "stdout.log", "stderr.log"):
            (runtime / name).chmod(0o666)
        return original_guardian(
            task_service,
            lock_descriptor,
            command,
            environment,
        )

    monkeypatch.setattr(TaskService, "_run_carrier_guardian", normalize_before_tmux)

    service.start(task_id)
    terminal = _wait_terminal(service, task_id)
    assert terminal["state"] == "completed"
    launch = json.loads((runtime / "launch.json").read_text(encoding="utf-8"))
    final_path = runtime / "final.json"
    final = json.loads(final_path.read_text(encoding="utf-8"))

    assert launch["filesystem_permissions_enforced"] is False
    assert launch["filesystem_permission_probe"] == _normalized_permission_probe(
        tmp_path / ".aros"
    )
    assert final["filesystem_permissions_enforced"] is False
    assert final["filesystem_permission_probe"] == launch[
        "filesystem_permission_probe"
    ]
    for name in ("launch.json", "stdout.log", "stderr.log"):
        metadata = (runtime / name).lstat()
        assert stat.S_ISREG(metadata.st_mode)
        assert metadata.st_nlink == 1
        assert stat.S_IMODE(metadata.st_mode) == 0o666

    final_path.chmod(0o666)
    assert TaskService(tmp_path).status(task_id)["state"] == "completed"


@pytest.mark.parametrize("name", ("launch.json", "final.json", "stdout.log"))
def test_enforced_permissions_reject_runtime_mode_drift(
    tmp_path: Path,
    name: str,
) -> None:
    service, brief = _create_committed_task(
        tmp_path,
        [sys.executable, "-c", "print('strict output')"],
        key=f"strict-runtime-mode-{name}",
    )
    task_id = str(brief["task_id"])
    service.start(task_id)
    _wait_terminal(service, task_id)
    path = tmp_path / ".aros" / "tasks" / task_id / name
    path.chmod(0o666)

    with pytest.raises(TaskError, match="permission|restrictive|mode"):
        TaskService(tmp_path).status(task_id)


def test_tmux_runner_cannot_be_shadowed_by_ambient_cwd_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, brief = _create_committed_task(
        tmp_path,
        [
            sys.executable,
            "-c",
            "from pathlib import Path;Path('real-runner.txt').write_text('real\\n')",
        ],
        key="cwd-shadow-runner",
    )
    task_id = str(brief["task_id"])
    malicious_cwd = tmp_path / ".git" / "malicious-cwd"
    fake_runner = malicious_cwd / "arbor" / "aros" / "task_runner.py"
    fake_runner.parent.mkdir(parents=True)
    (malicious_cwd / "arbor" / "__init__.py").write_text("", encoding="utf-8")
    (malicious_cwd / "arbor" / "aros" / "__init__.py").write_text("", encoding="utf-8")
    shadow_marker = tmp_path / ".git" / "cwd-shadow-loaded"
    fake_runner.write_text(
        "from pathlib import Path\n"
        f"Path({str(shadow_marker)!r}).write_text('shadowed\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(malicious_cwd)

    service.start(task_id)
    terminal = _wait_terminal(service, task_id)

    real_marker = tmp_path / ".worktree" / "tasks" / task_id / "real-runner.txt"
    assert terminal["state"] == "completed"
    assert real_marker.read_text(encoding="utf-8") == "real\n"
    assert not shadow_marker.exists()


def test_launch_without_process_or_final_becomes_lost_and_never_relaunches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "must-not-run"
    service, brief = _create_committed_task(
        tmp_path,
        [
            sys.executable,
            "-c",
            f"from pathlib import Path;Path({str(marker)!r}).touch()",
        ],
        key="lost-once",
    )
    task_id = str(brief["task_id"])
    monkeypatch.setattr(tasks_module, "_LAUNCH_GRACE_SECONDS", 0.0)
    carrier_calls = _fake_absent_tmux_carrier(monkeypatch)

    first = service.start(task_id)
    status_path = tmp_path / ".aros" / "tasks" / task_id / "status.json"
    status_path.unlink()
    second = TaskService(tmp_path).start(task_id)

    assert first["state"] == "lost"
    assert second["state"] == "lost"
    assert second["reason"] == "process_absent_without_final_receipt"
    assert len(carrier_calls) == 1
    assert not marker.exists()
    runtime = tmp_path / ".aros" / "tasks" / task_id
    assert (runtime / "launch.json").is_file()
    assert not (runtime / "final.json").exists()
    assert json.loads(status_path.read_text(encoding="utf-8"))["state"] == "lost"


def test_live_tmux_carrier_stays_launched_until_runner_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, brief = _create_committed_task(
        tmp_path,
        [sys.executable, "-c", "print('carrier released')"],
        key="live-carrier-before-runner-claim",
    )
    task_id = str(brief["task_id"])
    runtime = tmp_path / ".aros" / "tasks" / task_id
    runner_release = tmp_path / ".git" / "release-task-runner"
    real_tmux = shutil.which("tmux")
    assert real_tmux is not None
    fake_tmux = tmp_path / ".git" / "delayed-runner-tmux"
    carrier_launches = tmp_path / ".git" / "delayed-runner-launches"
    delay_prefix = (
        f"while [ ! -e {shlex.quote(str(runner_release))} ]; "
        "do sleep 0.01; done; exec "
    )
    fake_tmux.write_text(
        f"#!{sys.executable}\n"
        "import os\n"
        "import sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "if 'new-session' in args:\n"
        f"    marker = Path({str(carrier_launches)!r})\n"
        "    marker.write_text((marker.read_text() if marker.exists() else '') + '1')\n"
        f"    args[-1] = {delay_prefix!r} + args[-1]\n"
        f"os.execv({real_tmux!r}, [{real_tmux!r}, *args])\n",
        encoding="utf-8",
    )
    fake_tmux.chmod(0o700)

    monkeypatch.setattr(tasks_module, "_LAUNCH_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(tasks_module.shutil, "which", lambda _name: str(fake_tmux))
    launch: dict[str, object] | None = None
    try:
        first = service.start(task_id)
        launch = json.loads((runtime / "launch.json").read_text(encoding="utf-8"))
        carrier = subprocess.run(
            [
                real_tmux,
                "-L",
                str(launch["tmux_socket"]),
                "has-session",
                "-t",
                f"={launch['tmux_session']}",
            ],
            capture_output=True,
            check=False,
            env=task_runner_module.runner_environment(runtime),
        )
        carrier_was_live = carrier.returncode == 0
        before_claim = [service.status(task_id) for _ in range(3)]

        ownership = json.loads(
            (runtime / "ownership.json").read_text(encoding="utf-8")
        )
        atomic_write_json(
            runtime / "status.json",
            lost_status(brief, ownership, launch, before_claim[-1]),
        )
        repaired = service.status(task_id)
        replay = service.start(task_id)

        runner_release.touch()
        deadline = time.monotonic() + 5
        while not (runtime / "execution.json").is_file() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert (runtime / "execution.json").is_file()
        terminal = _wait_terminal(service, task_id)
    finally:
        runner_release.touch(exist_ok=True)
        if launch is not None:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                carrier = subprocess.run(
                    [
                        real_tmux,
                        "-L",
                        str(launch["tmux_socket"]),
                        "has-session",
                        "-t",
                        f"={launch['tmux_session']}",
                    ],
                    capture_output=True,
                    check=False,
                )
                if carrier.returncode != 0:
                    break
                time.sleep(0.02)

    assert carrier_was_live
    assert first["state"] == "launched"
    assert [status["state"] for status in before_claim] == ["launched"] * 3
    assert repaired["state"] == "launched"
    assert replay["state"] == "launched"
    assert carrier_launches.read_text(encoding="utf-8") == "1"
    assert terminal["state"] == "completed"
    assert (runtime / "execution.json").is_file()
    assert (runtime / "final.json").is_file()


def test_carrier_launch_lock_keeps_pre_session_start_nonterminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, brief = _create_committed_task(
        tmp_path,
        [sys.executable, "-c", "print('pre-carrier release')"],
        key="carrier-launch-before-session",
    )
    task_id = str(brief["task_id"])
    runtime = tmp_path / ".aros" / "tasks" / task_id
    real_tmux = shutil.which("tmux")
    assert real_tmux is not None
    fake_tmux = tmp_path / ".git" / "pre-carrier-tmux"
    launch_entered = tmp_path / ".git" / "pre-carrier-entered"
    release_launch = tmp_path / ".git" / "release-pre-carrier"
    carrier_launches = tmp_path / ".git" / "pre-carrier-launches"
    fake_tmux.write_text(
        f"#!{sys.executable}\n"
        "import os\n"
        "import sys\n"
        "import time\n"
        "from pathlib import Path\n"
        "if 'new-session' in sys.argv:\n"
        f"    marker = Path({str(carrier_launches)!r})\n"
        "    marker.write_text((marker.read_text() if marker.exists() else '') + '1')\n"
        f"    Path({str(launch_entered)!r}).touch()\n"
        f"    while not Path({str(release_launch)!r}).exists():\n"
        "        time.sleep(0.01)\n"
        f"os.execv({real_tmux!r}, [{real_tmux!r}, *sys.argv[1:]])\n",
        encoding="utf-8",
    )
    fake_tmux.chmod(0o700)

    monkeypatch.setattr(tasks_module, "_LAUNCH_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(tasks_module.shutil, "which", lambda _name: str(fake_tmux))
    launch: dict[str, object] | None = None
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            starter = pool.submit(service.start, task_id)
            deadline = time.monotonic() + 5
            while not launch_entered.is_file() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert launch_entered.is_file()
            launch = json.loads(
                (runtime / "launch.json").read_text(encoding="utf-8")
            )
            carrier_before_release = subprocess.run(
                [
                    real_tmux,
                    "-L",
                    str(launch["tmux_socket"]),
                    "has-session",
                    "-t",
                    f"={launch['tmux_session']}",
                ],
                capture_output=True,
                check=False,
                env=task_runner_module.runner_environment(runtime),
            )
            before_carrier = [service.status(task_id) for _ in range(3)]
            replay = service.start(task_id)
            release_launch.touch()
            first = starter.result(timeout=10)
        terminal = _wait_terminal(service, task_id)
    finally:
        release_launch.touch(exist_ok=True)

    assert carrier_before_release.returncode == 1
    assert [status["state"] for status in before_carrier] == ["launched"] * 3
    assert replay["state"] == "launched"
    assert first["state"] != "lost"
    assert carrier_launches.read_text(encoding="utf-8") == "1"
    assert terminal["state"] == "completed"
    assert (runtime / "execution.json").is_file()
    assert (runtime / "final.json").is_file()


def test_guardian_keeps_launch_live_after_starter_sigkill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code = (
        "from pathlib import Path\n"
        "path = Path('adapter-count')\n"
        "path.write_text((path.read_text() if path.exists() else '') + '1')\n"
    )
    service, brief = _create_committed_task(
        tmp_path,
        [sys.executable, "-c", code],
        key="guardian-survives-starter-sigkill",
    )
    task_id = str(brief["task_id"])
    runtime = tmp_path / ".aros" / "tasks" / task_id
    worktree = tmp_path / ".worktree" / "tasks" / task_id
    real_tmux = shutil.which("tmux")
    assert real_tmux is not None
    fake_tmux = tmp_path / ".git" / "barrier-tmux"
    client_started = tmp_path / ".git" / "tmux-client-started"
    release_client = tmp_path / ".git" / "release-tmux-client"
    fake_tmux.write_text(
        f"#!{sys.executable}\n"
        "import os\n"
        "import sys\n"
        "import time\n"
        "from pathlib import Path\n"
        "if 'new-session' in sys.argv:\n"
        f"    Path({str(client_started)!r}).write_text(str(os.getpid()))\n"
        f"    while not Path({str(release_client)!r}).exists():\n"
        "        time.sleep(0.01)\n"
        f"os.execv({real_tmux!r}, [{real_tmux!r}, *sys.argv[1:]])\n",
        encoding="utf-8",
    )
    fake_tmux.chmod(0o700)
    monkeypatch.setattr(tasks_module.shutil, "which", lambda _name: str(fake_tmux))
    monkeypatch.setattr(tasks_module, "_LAUNCH_GRACE_SECONDS", 0.0)

    starter_pid = os.fork()
    if starter_pid == 0:
        try:
            service.start(task_id)
        except BaseException:
            os._exit(2)
        os._exit(0)

    starter_reaped = False
    launch: dict[str, object] | None = None
    try:
        deadline = time.monotonic() + 5
        while not client_started.is_file() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert client_started.is_file()
        launch = json.loads((runtime / "launch.json").read_text(encoding="utf-8"))
        before_kill = subprocess.run(
            [
                real_tmux,
                "-L",
                str(launch["tmux_socket"]),
                "has-session",
                "-t",
                f"={launch['tmux_session']}",
            ],
            capture_output=True,
            check=False,
        )
        os.kill(starter_pid, signal.SIGKILL)
        waited, wait_status = os.waitpid(starter_pid, 0)
        starter_reaped = True
        after_kill = [TaskService(tmp_path).status(task_id) for _ in range(3)]

        release_client.touch()
        deadline = time.monotonic() + 5
        while not (runtime / "execution.json").is_file() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert (runtime / "execution.json").is_file()
        terminal = _wait_terminal(TaskService(tmp_path), task_id)
    finally:
        release_client.touch(exist_ok=True)
        if not starter_reaped:
            try:
                os.kill(starter_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(starter_pid, 0)
        if launch is not None:
            subprocess.run(
                [
                    real_tmux,
                    "-L",
                    str(launch["tmux_socket"]),
                    "kill-session",
                    "-t",
                    f"={launch['tmux_session']}",
                ],
                capture_output=True,
                check=False,
            )

    assert before_kill.returncode == 1
    assert waited == starter_pid
    assert os.waitstatus_to_exitcode(wait_status) == -signal.SIGKILL
    assert [status["state"] for status in after_kill] == ["launched"] * 3
    assert terminal["state"] == "completed"
    assert (worktree / "adapter-count").read_text(encoding="utf-8") == "1"


def test_guardian_does_not_leak_launch_lock_to_tmux_server(tmp_path: Path) -> None:
    service, brief = _create_committed_task(
        tmp_path,
        [sys.executable, "-c", "raise AssertionError('must not run')"],
        key="guardian-lock-fd-nonleak",
    )
    task_id = str(brief["task_id"])
    runtime = tmp_path / ".aros" / "tasks" / task_id
    lock_path = service._carrier_launch_lock_path(task_id)
    fake_tmux = tmp_path / ".git" / "forking-tmux-client"
    server_report = tmp_path / ".git" / "fake-tmux-server-report"
    server_pid_path = tmp_path / ".git" / "fake-tmux-server-pid"
    release_server = tmp_path / ".git" / "release-fake-tmux-server"
    fake_tmux.write_text(
        f"#!{sys.executable}\n"
        "import os\n"
        "import time\n"
        "from pathlib import Path\n"
        f"lock_path = {str(lock_path)!r}\n"
        f"report = Path({str(server_report)!r})\n"
        f"pid_path = Path({str(server_pid_path)!r})\n"
        f"release = Path({str(release_server)!r})\n"
        "server = os.fork()\n"
        "if server == 0:\n"
        "    pid_path.write_text(str(os.getpid()))\n"
        "    targets = []\n"
        "    for entry in Path('/proc/self/fd').iterdir():\n"
        "        try:\n"
        "            targets.append(os.readlink(entry))\n"
        "        except OSError:\n"
        "            pass\n"
        "    report.write_text('leaked' if lock_path in targets else 'clean')\n"
        "    while not release.exists():\n"
        "        time.sleep(0.01)\n"
        "    os._exit(0)\n"
        "deadline = time.monotonic() + 5\n"
        "while not report.exists() and time.monotonic() < deadline:\n"
        "    time.sleep(0.01)\n"
        "raise SystemExit(0 if report.exists() else 2)\n",
        encoding="utf-8",
    )
    fake_tmux.chmod(0o700)
    server_pid: int | None = None
    try:
        with service._carrier_launch_guard(task_id) as lock_descriptor:
            result = service._run_carrier_guardian(
                lock_descriptor,
                [str(fake_tmux)],
                task_runner_module.runner_environment(runtime),
            )
        server_pid = int(server_pid_path.read_text(encoding="utf-8"))

        assert result.returncode == 0
        assert server_report.read_text(encoding="utf-8") == "clean"
        assert _process_is_running(server_pid)
        assert service._carrier_launch_is_active(task_id) is False
    finally:
        release_server.touch(exist_ok=True)
        if server_pid is not None:
            deadline = time.monotonic() + 5
            while _process_is_running(server_pid) and time.monotonic() < deadline:
                time.sleep(0.02)
            if _process_is_running(server_pid):
                os.kill(server_pid, signal.SIGKILL)


def test_guardian_does_not_kill_client_at_removed_ten_second_timeout(
    tmp_path: Path,
) -> None:
    service, brief = _create_committed_task(
        tmp_path,
        [sys.executable, "-c", "raise AssertionError('must not run')"],
        key="guardian-no-client-timeout",
    )
    task_id = str(brief["task_id"])
    runtime = tmp_path / ".aros" / "tasks" / task_id
    fake_tmux = tmp_path / ".git" / "blocked-tmux-client"
    client_started = tmp_path / ".git" / "blocked-client-started"
    release_client = tmp_path / ".git" / "release-blocked-client"
    fake_tmux.write_text(
        f"#!{sys.executable}\n"
        "import time\n"
        "from pathlib import Path\n"
        f"Path({str(client_started)!r}).touch()\n"
        f"while not Path({str(release_client)!r}).exists():\n"
        "    time.sleep(0.01)\n",
        encoding="utf-8",
    )
    fake_tmux.chmod(0o700)

    try:
        with service._carrier_launch_guard(task_id) as lock_descriptor:
            with ThreadPoolExecutor(max_workers=1) as pool:
                guardian = pool.submit(
                    service._run_carrier_guardian,
                    lock_descriptor,
                    [str(fake_tmux)],
                    task_runner_module.runner_environment(runtime),
                )
                deadline = time.monotonic() + 5
                while not client_started.is_file() and time.monotonic() < deadline:
                    time.sleep(0.02)
                assert client_started.is_file()
                old_timeout = time.monotonic() + 10.2
                while time.monotonic() < old_timeout:
                    time.sleep(0.05)
                still_running = not guardian.done()
                launch_still_active = service._carrier_launch_is_active(task_id)
                release_client.touch()
                result = guardian.result(timeout=5)
        released_after_result = not service._carrier_launch_is_active(task_id)
    finally:
        release_client.touch(exist_ok=True)

    assert still_running
    assert launch_still_active
    assert result.returncode == 0
    assert released_after_result


def test_guardian_forwards_tmux_exit_and_exec_results(tmp_path: Path) -> None:
    service, brief = _create_committed_task(
        tmp_path,
        [sys.executable, "-c", "raise AssertionError('must not run')"],
        key="guardian-result-forwarding",
    )
    task_id = str(brief["task_id"])
    runtime = tmp_path / ".aros" / "tasks" / task_id
    environment = task_runner_module.runner_environment(runtime)
    commands = (
        (
            [
                sys.executable,
                "-c",
                "import sys;print('ok-out');sys.stderr.write('ok-err')",
            ],
            0,
            "ok-out\n",
            "ok-err",
        ),
        (
            [
                sys.executable,
                "-c",
                "import sys;print('bad-out');sys.stderr.write('bad-err');sys.exit(7)",
            ],
            7,
            "bad-out\n",
            "bad-err",
        ),
        (["/definitely/missing/aros-tmux"], 127, "", "tmux exec failed:"),
    )

    observed: list[tuple[int, str, str]] = []
    for command, _returncode, _stdout, _stderr in commands:
        with service._carrier_launch_guard(task_id) as lock_descriptor:
            result = service._run_carrier_guardian(
                lock_descriptor,
                command,
                environment,
            )
        observed.append(
            (result.returncode, result.stdout, result.stderr)
        )

    for actual, expected in zip(observed, commands, strict=True):
        returncode, stdout, stderr = actual
        _command, expected_returncode, expected_stdout, expected_stderr = expected
        assert returncode == expected_returncode
        assert stdout == expected_stdout
        assert expected_stderr in stderr
    assert service._carrier_launch_is_active(task_id) is False


def test_invalid_utf8_guardian_error_records_failed_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, brief = _create_committed_task(
        tmp_path,
        [sys.executable, "-c", "raise AssertionError('must not run')"],
        key="guardian-invalid-utf8",
    )
    task_id = str(brief["task_id"])
    runtime = tmp_path / ".aros" / "tasks" / task_id
    fake_tmux = tmp_path / ".git" / "invalid-utf8-tmux"
    fake_tmux.write_text(
        f"#!{sys.executable}\n"
        "import os\n"
        "import sys\n"
        "if 'new-session' not in sys.argv:\n"
        "    raise SystemExit(1)\n"
        "os.write(2, b'\\xffbad guardian error')\n"
        "raise SystemExit(7)\n",
        encoding="utf-8",
    )
    fake_tmux.chmod(0o700)
    monkeypatch.setattr(tasks_module, "_LAUNCH_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(tasks_module.shutil, "which", lambda _name: str(fake_tmux))
    start_error: TaskError | None = None
    decode_error: UnicodeDecodeError | None = None

    try:
        service.start(task_id)
    except TaskError as error:
        start_error = error
    except UnicodeDecodeError as error:
        decode_error = error

    status = service.status(task_id)
    final_path = runtime / "final.json"
    final = json.loads(final_path.read_text(encoding="utf-8")) if final_path.exists() else {}
    assert decode_error is None
    assert start_error is not None
    assert status["state"] == "failed_process"
    assert "\ufffdbad guardian error" in str(final["error"])


@pytest.mark.parametrize("failure", ("nonzero", "oserror"))
def test_carrier_failure_is_recorded_before_launch_lock_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    service, brief = _create_committed_task(
        tmp_path,
        [sys.executable, "-c", "raise AssertionError('must not run')"],
        key=f"carrier-failure-lock-{failure}",
    )
    task_id = str(brief["task_id"])
    record_entered = Event()
    release_record = Event()
    guardian_calls: list[list[str]] = []
    original_run = tasks_module.subprocess.run
    original_record = TaskService._record_carrier_failure

    def fail_guardian(
        _service: TaskService,
        _lock_descriptor: int,
        command: list[str],
        _environment: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        guardian_calls.append(command)
        if failure == "oserror":
            raise OSError("injected guardian spawn failure")
        return subprocess.CompletedProcess(command, 7, "", "tmux failed")

    def pause_failure_record(
        task_service: TaskService,
        failed_task_id: str,
        detail: str,
        *,
        preserve_execution: bool = False,
    ) -> bool:
        record_entered.set()
        assert release_record.wait(timeout=5)
        return original_record(
            task_service,
            failed_task_id,
            detail,
            preserve_execution=preserve_execution,
        )

    def absent_carrier(
        command: list[str],
        *args: object,
        **kwargs: object,
    ) -> object:
        if "has-session" in command:
            return subprocess.CompletedProcess(command, 1, "", "")
        return original_run(command, *args, **kwargs)

    monkeypatch.setattr(tasks_module, "_LAUNCH_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(tasks_module.shutil, "which", lambda _name: "/fake/tmux")
    monkeypatch.setattr(TaskService, "_run_carrier_guardian", fail_guardian)
    monkeypatch.setattr(TaskService, "_record_carrier_failure", pause_failure_record)
    monkeypatch.setattr(tasks_module.subprocess, "run", absent_carrier)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            starter = pool.submit(service.start, task_id)
            assert record_entered.wait(timeout=5)
            during_record = [service.status(task_id) for _ in range(3)]
            replay = service.start(task_id)
            release_record.set()
            with pytest.raises(TaskError, match="tmux launch failed"):
                starter.result(timeout=5)
    finally:
        release_record.set()

    terminal = service.status(task_id)
    assert [status["state"] for status in during_record] == ["launched"] * 3
    assert replay["state"] == "launched"
    assert terminal["state"] == "failed_process"
    assert len(guardian_calls) == 1


def test_signaled_tmux_client_does_not_overwrite_live_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code = (
        "from pathlib import Path\n"
        "path = Path('adapter-count')\n"
        "path.write_text((path.read_text() if path.exists() else '') + '1')\n"
    )
    service, brief = _create_committed_task(
        tmp_path,
        [sys.executable, "-c", code],
        key="signaled-client-live-session",
    )
    task_id = str(brief["task_id"])
    runtime = tmp_path / ".aros" / "tasks" / task_id
    worktree = tmp_path / ".worktree" / "tasks" / task_id
    release_runner = tmp_path / ".git" / "release-signaled-client-runner"
    real_tmux = shutil.which("tmux")
    assert real_tmux is not None
    fake_tmux = tmp_path / ".git" / "signaled-client-tmux"
    runner_gate = (
        f"while [ ! -e {shlex.quote(str(release_runner))} ]; "
        "do sleep 0.01; done; exec "
    )
    fake_tmux.write_text(
        f"#!{sys.executable}\n"
        "import os\n"
        "import signal\n"
        "import subprocess\n"
        "import sys\n"
        "args = sys.argv[1:]\n"
        "if 'new-session' not in args:\n"
        f"    os.execv({real_tmux!r}, [{real_tmux!r}, *args])\n"
        f"args[-1] = {runner_gate!r} + args[-1]\n"
        f"result = subprocess.run([{real_tmux!r}, *args], check=False)\n"
        "if result.returncode != 0:\n"
        "    raise SystemExit(result.returncode)\n"
        "os.kill(os.getpid(), signal.SIGKILL)\n",
        encoding="utf-8",
    )
    fake_tmux.chmod(0o700)
    monkeypatch.setattr(tasks_module, "_LAUNCH_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(tasks_module.shutil, "which", lambda _name: str(fake_tmux))
    launch: dict[str, object] | None = None
    start_error: TaskError | None = None
    try:
        try:
            started = service.start(task_id)
        except TaskError as error:
            start_error = error
            started = service.status(task_id)
        launch = json.loads((runtime / "launch.json").read_text(encoding="utf-8"))
        release_runner.touch()
        terminal = _wait_terminal(service, task_id)
    finally:
        release_runner.touch(exist_ok=True)
        if launch is not None:
            subprocess.run(
                [
                    real_tmux,
                    "-L",
                    str(launch["tmux_socket"]),
                    "kill-session",
                    "-t",
                    f"={launch['tmux_session']}",
                ],
                capture_output=True,
                check=False,
            )

    assert start_error is None
    assert started["state"] == "launched"
    assert terminal["state"] == "completed"
    assert (worktree / "adapter-count").read_text(encoding="utf-8") == "1"


@pytest.mark.parametrize("returncode", (7, -signal.SIGKILL))
def test_failed_guardian_result_preserves_valid_final(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
) -> None:
    service, brief = _create_committed_task(
        tmp_path,
        [sys.executable, "-c", "raise AssertionError('must not run')"],
        key=f"guardian-valid-final-{returncode}",
    )
    task_id = str(brief["task_id"])
    runtime = tmp_path / ".aros" / "tasks" / task_id
    guardian_calls: list[list[str]] = []
    prior_final_bytes: list[bytes] = []

    def publish_final_then_fail(
        task_service: TaskService,
        _lock_descriptor: int,
        command: list[str],
        _environment: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        guardian_calls.append(command)
        assert task_runner_module.record_carrier_failure(
            task_service,
            task_id,
            "prior valid carrier final",
        )
        prior_final_bytes.append((runtime / "final.json").read_bytes())
        assert not (runtime / "execution.json").exists()
        return subprocess.CompletedProcess(command, returncode, "", "tmux failed")

    monkeypatch.setattr(tasks_module, "_LAUNCH_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(tasks_module.shutil, "which", lambda _name: "/fake/tmux")
    monkeypatch.setattr(TaskService, "_run_carrier_guardian", publish_final_then_fail)

    terminal = service.start(task_id)
    final_bytes = (runtime / "final.json").read_bytes()
    final = json.loads(final_bytes)
    replay = service.start(task_id)

    assert terminal["state"] == final["state"] == "failed_process"
    assert replay == terminal
    assert final["error"] == "carrier launch failed: prior valid carrier final"
    assert prior_final_bytes == [final_bytes]
    assert not (runtime / "execution.json").exists()
    assert len(guardian_calls) == 1


@pytest.mark.parametrize("returncode", (7, -signal.SIGKILL))
def test_failed_guardian_result_preserves_valid_execution_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
) -> None:
    service, brief = _create_committed_task(
        tmp_path,
        [sys.executable, "-c", "raise AssertionError('must not run')"],
        key=f"guardian-valid-execution-{returncode}",
    )
    task_id = str(brief["task_id"])
    runtime = tmp_path / ".aros" / "tasks" / task_id
    guardian_calls: list[list[str]] = []

    def claim_then_fail(
        task_service: TaskService,
        _lock_descriptor: int,
        command: list[str],
        _environment: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        guardian_calls.append(command)
        recorded_brief = task_service._load_brief(task_id)
        ownership = task_service._load_ownership(recorded_brief)
        launch = task_service._load_launch(recorded_brief, ownership)
        token = process_start_token(os.getpid())
        assert token is not None
        execution = task_runner_module.create_execution_claim(
            task_service,
            recorded_brief,
            ownership,
            launch,
            (os.getpid(), os.getpgid(0), token),
        )
        assert execution is not None
        return subprocess.CompletedProcess(command, returncode, "", "tmux failed")

    monkeypatch.setattr(tasks_module, "_LAUNCH_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(tasks_module.shutil, "which", lambda _name: "/fake/tmux")
    monkeypatch.setattr(TaskService, "_run_carrier_guardian", claim_then_fail)

    launched = service.start(task_id)
    replay = service.start(task_id)

    assert launched["state"] == replay["state"] == "launched"
    assert (runtime / "execution.json").is_file()
    assert not (runtime / "final.json").exists()
    assert len(guardian_calls) == 1


def test_pending_sigint_after_guardian_spawn_waits_and_records_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, brief = _create_committed_task(
        tmp_path,
        [sys.executable, "-c", "raise AssertionError('must not run')"],
        key="guardian-pending-sigint",
    )
    task_id = str(brief["task_id"])
    fake_tmux = tmp_path / ".git" / "sigint-tmux-client"
    client_started = tmp_path / ".git" / "sigint-client-started"
    client_pid_path = tmp_path / ".git" / "sigint-client-pid"
    client_mask = tmp_path / ".git" / "sigint-client-mask"
    release_client = tmp_path / ".git" / "release-sigint-client"
    fake_tmux.write_text(
        f"#!{sys.executable}\n"
        "import os\n"
        "import signal\n"
        "import sys\n"
        "import time\n"
        "from pathlib import Path\n"
        "if 'new-session' not in sys.argv:\n"
        "    raise SystemExit(1)\n"
        "blocked = signal.pthread_sigmask(signal.SIG_BLOCK, set())\n"
        f"Path({str(client_mask)!r}).write_text("
        "'blocked' if signal.SIGINT in blocked else 'unblocked')\n"
        f"Path({str(client_pid_path)!r}).write_text(str(os.getpid()))\n"
        f"Path({str(client_started)!r}).touch()\n"
        f"while not Path({str(release_client)!r}).exists():\n"
        "    time.sleep(0.01)\n"
        "raise SystemExit(7)\n",
        encoding="utf-8",
    )
    fake_tmux.chmod(0o700)
    original_pthread_sigmask = signal.pthread_sigmask
    starter_thread: list[int] = []
    mask_calls: list[int] = []
    restore_attempted = Event()

    def deliver_pending_sigint(how: int, mask: object) -> set[signal.Signals]:
        result = original_pthread_sigmask(how, mask)  # type: ignore[arg-type]
        if starter_thread and get_ident() == starter_thread[0]:
            mask_calls.append(how)
            if how == signal.SIG_SETMASK:
                restore_attempted.set()
                raise KeyboardInterrupt
        return result

    def start_task() -> dict[str, object]:
        starter_thread.append(get_ident())
        return service.start(task_id)

    monkeypatch.setattr(tasks_module, "_LAUNCH_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(tasks_module.shutil, "which", lambda _name: str(fake_tmux))
    monkeypatch.setattr(signal, "pthread_sigmask", deliver_pending_sigint)
    client_pid: int | None = None
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            starter = pool.submit(start_task)
            deadline = time.monotonic() + 5
            while not client_started.is_file() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert client_started.is_file()
            client_pid = int(client_pid_path.read_text(encoding="utf-8"))
            during_interrupt = [service.status(task_id) for _ in range(3)]
            release_client.touch()
            with pytest.raises(KeyboardInterrupt):
                starter.result(timeout=5)
    finally:
        release_client.touch(exist_ok=True)

    terminal = service.status(task_id)
    assert restore_attempted.is_set()
    assert mask_calls == [signal.SIG_BLOCK, signal.SIG_SETMASK]
    assert client_mask.read_text(encoding="utf-8") == "unblocked"
    assert [status["state"] for status in during_interrupt] == ["launched"] * 3
    assert terminal["state"] == "failed_process"
    assert client_pid is not None
    assert not _process_is_running(client_pid)


def test_real_sigint_waits_for_carrier_failure_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, brief = _create_committed_task(
        tmp_path,
        [sys.executable, "-c", "raise AssertionError('must not run')"],
        key="real-sigint-carrier-failure",
    )
    task_id = str(brief["task_id"])
    runtime = tmp_path / ".aros" / "tasks" / task_id
    fake_tmux = tmp_path / ".git" / "real-sigint-tmux-client"
    client_started = tmp_path / ".git" / "real-sigint-client-started"
    release_client = tmp_path / ".git" / "release-real-sigint-client"
    fake_tmux.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "import time\n"
        "from pathlib import Path\n"
        "if 'new-session' not in sys.argv:\n"
        "    raise SystemExit(1)\n"
        f"Path({str(client_started)!r}).touch()\n"
        f"while not Path({str(release_client)!r}).exists():\n"
        "    time.sleep(0.01)\n"
        "raise SystemExit(7)\n",
        encoding="utf-8",
    )
    fake_tmux.chmod(0o700)
    monkeypatch.setattr(tasks_module, "_LAUNCH_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(tasks_module.shutil, "which", lambda _name: str(fake_tmux))

    starter_pid = os.fork()
    if starter_pid == 0:
        signal.pthread_sigmask(signal.SIG_SETMASK, set())
        signal.signal(signal.SIGINT, signal.default_int_handler)
        try:
            service.start(task_id)
        except KeyboardInterrupt:
            try:
                status = TaskService(tmp_path).status(task_id)
                final = json.loads(
                    (runtime / "final.json").read_text(encoding="utf-8")
                )
            except BaseException:
                os._exit(3)
            os._exit(
                0
                if status["state"] == final["state"] == "failed_process"
                else 4
            )
        except BaseException:
            os._exit(5)
        os._exit(6)

    starter_reaped = False
    try:
        deadline = time.monotonic() + 5
        while not client_started.is_file() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert client_started.is_file()
        os.kill(starter_pid, signal.SIGINT)

        pending_mask = 1 << (signal.SIGINT - 1)
        pending_observed = False
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                process_status = Path(f"/proc/{starter_pid}/status").read_text(
                    encoding="ascii"
                )
            except OSError:
                break
            masks = {
                name: int(value, 16)
                for name, value in (
                    line.split(":", 1)
                    for line in process_status.splitlines()
                    if line.startswith(("SigPnd:", "ShdPnd:"))
                )
            }
            if any(mask & pending_mask for mask in masks.values()):
                pending_observed = True
                break
            time.sleep(0.01)

        during_interrupt = [TaskService(tmp_path).status(task_id) for _ in range(3)]
        release_client.touch()
        waited, wait_status = os.waitpid(starter_pid, 0)
        starter_reaped = True
    finally:
        release_client.touch(exist_ok=True)
        if not starter_reaped:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                waited, _wait_status = os.waitpid(starter_pid, os.WNOHANG)
                if waited == starter_pid:
                    starter_reaped = True
                    break
                time.sleep(0.02)
            if not starter_reaped:
                try:
                    os.kill(starter_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                os.waitpid(starter_pid, 0)

    terminal = TaskService(tmp_path).status(task_id)
    assert pending_observed
    assert [status["state"] for status in during_interrupt] == ["launched"] * 3
    assert waited == starter_pid
    assert os.waitstatus_to_exitcode(wait_status) == 0
    assert terminal["state"] == "failed_process"


def test_pending_sigint_during_guardian_spawn_error_records_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, brief = _create_committed_task(
        tmp_path,
        [sys.executable, "-c", "raise AssertionError('must not run')"],
        key="guardian-spawn-error-pending-sigint",
    )
    task_id = str(brief["task_id"])
    original_popen = tasks_module.subprocess.Popen
    original_pthread_sigmask = signal.pthread_sigmask
    mask_calls: list[int] = []
    guardian_spawn_attempted = False

    def fail_guardian_spawn(
        command: object,
        *args: object,
        **kwargs: object,
    ) -> subprocess.Popen[bytes]:
        nonlocal guardian_spawn_attempted
        if kwargs.get("pass_fds"):
            guardian_spawn_attempted = True
            raise OSError("injected guardian Popen failure")
        return original_popen(command, *args, **kwargs)  # type: ignore[arg-type]

    def deliver_pending_sigint(how: int, mask: object) -> set[signal.Signals]:
        result = original_pthread_sigmask(how, mask)  # type: ignore[arg-type]
        mask_calls.append(how)
        if how == signal.SIG_SETMASK:
            raise KeyboardInterrupt
        return result

    monkeypatch.setattr(tasks_module, "_LAUNCH_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(tasks_module.subprocess, "Popen", fail_guardian_spawn)
    monkeypatch.setattr(signal, "pthread_sigmask", deliver_pending_sigint)

    with pytest.raises(KeyboardInterrupt):
        service.start(task_id)

    terminal = service.status(task_id)
    assert guardian_spawn_attempted
    assert mask_calls == [signal.SIG_BLOCK, signal.SIG_SETMASK]
    assert terminal["state"] == "failed_process"
    assert (tmp_path / ".aros" / "tasks" / task_id / "final.json").is_file()


def test_pending_sigint_after_guardian_result_waits_for_failure_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, brief = _create_committed_task(
        tmp_path,
        [sys.executable, "-c", "raise AssertionError('must not run')"],
        key="guardian-result-pending-sigint-failure",
    )
    task_id = str(brief["task_id"])
    original_record = TaskService._record_carrier_failure
    original_pthread_sigmask = signal.pthread_sigmask
    record_entered = Event()
    release_record = Event()
    pending_sigint = False
    record_mask_blocked: list[bool] = []
    mask_calls: list[int] = []

    def completed_guardian(
        _service: TaskService,
        _lock_descriptor: int,
        command: list[str],
        _environment: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 7, "", "tmux failed")

    def arm_sigint_before_record(
        task_service: TaskService,
        failed_task_id: str,
        detail: str,
        *,
        preserve_execution: bool = False,
    ) -> bool:
        nonlocal pending_sigint
        blocked = original_pthread_sigmask(signal.SIG_BLOCK, set())
        record_mask_blocked.append(signal.SIGINT in blocked)
        pending_sigint = True
        record_entered.set()
        assert release_record.wait(timeout=5)
        return original_record(
            task_service,
            failed_task_id,
            detail,
            preserve_execution=preserve_execution,
        )

    def deliver_pending_sigint(how: int, mask: object) -> set[signal.Signals]:
        result = original_pthread_sigmask(how, mask)  # type: ignore[arg-type]
        mask_calls.append(how)
        if how == signal.SIG_SETMASK and pending_sigint:
            raise KeyboardInterrupt
        return result

    monkeypatch.setattr(tasks_module, "_LAUNCH_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(TaskService, "_run_carrier_guardian", completed_guardian)
    monkeypatch.setattr(TaskService, "_record_carrier_failure", arm_sigint_before_record)
    monkeypatch.setattr(signal, "pthread_sigmask", deliver_pending_sigint)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            starter = pool.submit(service.start, task_id)
            assert record_entered.wait(timeout=5)
            during_publication = [service.status(task_id) for _ in range(3)]
            release_record.set()
            with pytest.raises(KeyboardInterrupt):
                starter.result(timeout=5)
    finally:
        release_record.set()

    terminal = service.status(task_id)
    assert record_mask_blocked == [True]
    assert mask_calls == [signal.SIG_BLOCK, signal.SIG_SETMASK]
    assert [status["state"] for status in during_publication] == ["launched"] * 3
    assert terminal["state"] == "failed_process"


def test_pending_sigint_after_successful_guardian_result_keeps_carrier_live(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, brief = _create_committed_task(
        tmp_path,
        [sys.executable, "-c", "import time;time.sleep(30)"],
        timeout_seconds=30,
        key="guardian-result-pending-sigint-success",
    )
    task_id = str(brief["task_id"])
    original_guardian = TaskService._run_carrier_guardian
    original_pthread_sigmask = signal.pthread_sigmask
    pending_sigint = False
    result_mask_blocked: list[bool] = []

    def arm_sigint_after_result(
        task_service: TaskService,
        lock_descriptor: int,
        command: list[str],
        environment: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        nonlocal pending_sigint
        result = original_guardian(
            task_service,
            lock_descriptor,
            command,
            environment,
        )
        blocked = original_pthread_sigmask(signal.SIG_BLOCK, set())
        result_mask_blocked.append(signal.SIGINT in blocked)
        pending_sigint = True
        return result

    def deliver_pending_sigint(how: int, mask: object) -> set[signal.Signals]:
        result = original_pthread_sigmask(how, mask)  # type: ignore[arg-type]
        if how == signal.SIG_SETMASK and pending_sigint:
            raise KeyboardInterrupt
        return result

    monkeypatch.setattr(TaskService, "_run_carrier_guardian", arm_sigint_after_result)
    monkeypatch.setattr(signal, "pthread_sigmask", deliver_pending_sigint)
    terminal: dict[str, object] | None = None
    try:
        with pytest.raises(KeyboardInterrupt):
            service.start(task_id)
        active = service.status(task_id)
    finally:
        observed = service.status(task_id)
        if observed["state"] == "launched":
            observed = _wait_state(service, task_id, "running")
        if observed["state"] == "running":
            service.stop(task_id, actor="cleanup", reason="test cleanup")
            terminal = _wait_terminal(service, task_id)

    assert result_mask_blocked == [True]
    assert active["state"] in {"launched", "running"}
    assert terminal is not None
    assert terminal["state"] == "cancelled"


def test_pending_sigint_after_launch_guard_precedes_launch_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, brief = _create_committed_task(
        tmp_path,
        [sys.executable, "-c", "raise AssertionError('must not run')"],
        key="launch-guard-pending-sigint",
    )
    task_id = str(brief["task_id"])
    original_guard = TaskService._carrier_launch_guard
    original_publication_path = TaskService._publication_lock_path
    original_pthread_sigmask = signal.pthread_sigmask
    guard_entered = False
    pending_sigint = False
    publication_mask_blocked: list[bool] = []
    mask_calls: list[int] = []
    guardian_calls: list[list[str]] = []

    @contextmanager
    def observe_guard(
        task_service: TaskService,
        guarded_task_id: str,
    ) -> Iterator[int]:
        nonlocal guard_entered
        with original_guard(task_service, guarded_task_id) as descriptor:
            guard_entered = True
            try:
                yield descriptor
            finally:
                guard_entered = False

    def arm_sigint_at_publication(task_service: TaskService) -> Path:
        nonlocal pending_sigint
        if guard_entered and not publication_mask_blocked:
            blocked = original_pthread_sigmask(signal.SIG_BLOCK, set())
            is_blocked = signal.SIGINT in blocked
            publication_mask_blocked.append(is_blocked)
            if not is_blocked:
                raise RuntimeError("SIGINT delivered before launch publication")
            pending_sigint = True
        return original_publication_path(task_service)

    def failed_guardian(
        _service: TaskService,
        _lock_descriptor: int,
        command: list[str],
        _environment: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        guardian_calls.append(command)
        return subprocess.CompletedProcess(command, 7, "", "tmux failed")

    def deliver_pending_sigint(how: int, mask: object) -> set[signal.Signals]:
        result = original_pthread_sigmask(how, mask)  # type: ignore[arg-type]
        mask_calls.append(how)
        if how == signal.SIG_SETMASK and pending_sigint:
            raise KeyboardInterrupt
        return result

    monkeypatch.setattr(tasks_module, "_LAUNCH_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(TaskService, "_carrier_launch_guard", observe_guard)
    monkeypatch.setattr(TaskService, "_publication_lock_path", arm_sigint_at_publication)
    monkeypatch.setattr(TaskService, "_run_carrier_guardian", failed_guardian)
    monkeypatch.setattr(signal, "pthread_sigmask", deliver_pending_sigint)

    with pytest.raises((KeyboardInterrupt, RuntimeError)) as interrupt:
        service.start(task_id)

    status = service.status(task_id)
    replay_error: TaskError | None = None
    try:
        replay = service.start(task_id)
    except TaskError as error:
        replay_error = error
        replay = service.status(task_id)
    assert publication_mask_blocked == [True]
    assert isinstance(interrupt.value, KeyboardInterrupt)
    assert mask_calls == [signal.SIG_BLOCK, signal.SIG_SETMASK]
    assert status["state"] == "failed_process"
    assert replay_error is None
    assert replay == status
    assert len(guardian_calls) == 1


def test_carrier_launch_lock_probe_does_not_create_or_accept_stale_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, brief = _create_committed_task(
        tmp_path,
        [sys.executable, "-c", "raise AssertionError('must not run')"],
        key="absent-carrier-launch-lock",
    )
    task_id = str(brief["task_id"])
    lock_path = service._carrier_launch_lock_path(task_id)

    assert not lock_path.exists()
    assert service._carrier_launch_is_active(task_id) is False
    assert not lock_path.exists()

    tasks_module._ensure_durable_lock_file(lock_path, "test carrier launch lock")
    assert service._carrier_launch_is_active(task_id) is False

    original_open = tasks_module.os.open

    def fail_lock_open(path: object, *args: object, **kwargs: object) -> int:
        if Path(path) == lock_path:  # type: ignore[arg-type]
            raise OSError("injected carrier launch lock observation failure")
        return original_open(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(tasks_module.os, "open", fail_lock_open)
    with pytest.raises(TaskError, match="carrier launch lock"):
        service._carrier_launch_is_active(task_id)


@pytest.mark.parametrize("wrong_identity", ("socket", "session"))
def test_carrier_probe_ignores_live_wrong_tmux_identity(
    tmp_path: Path,
    wrong_identity: str,
) -> None:
    service, brief = _create_committed_task(
        tmp_path,
        [sys.executable, "-c", "raise AssertionError('must not run')"],
        key=f"wrong-carrier-{wrong_identity}",
    )
    task_id = str(brief["task_id"])
    expected_socket = tasks_module._tmux_socket_name(tmp_path, task_id)
    expected_session = f"aros-task-{task_id.lower()}"
    live_socket = (
        f"{expected_socket}-wrong" if wrong_identity == "socket" else expected_socket
    )
    live_session = (
        f"{expected_session}-wrong"
        if wrong_identity == "session"
        else expected_session
    )
    tmux = shutil.which("tmux")
    assert tmux is not None
    created = subprocess.run(
        [
            tmux,
            "-L",
            live_socket,
            "new-session",
            "-d",
            "-s",
            live_session,
            "sleep 30",
        ],
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )
    assert created.returncode == 0, created.stderr
    launch = {
        "task_id": task_id,
        "host": socket.gethostname(),
        "tmux_socket": expected_socket,
        "tmux_session": expected_session,
    }

    try:
        assert service._carrier_is_live(launch) is False
    finally:
        subprocess.run(
            [
                tmux,
                "-L",
                live_socket,
                "kill-session",
                "-t",
                f"={live_session}",
            ],
            capture_output=True,
            check=False,
        )


@pytest.mark.parametrize(
    "failure",
    ("different-host", "oserror", "timeout", "unknown-status"),
)
def test_carrier_probe_observation_failure_is_not_absence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    service, brief = _create_committed_task(
        tmp_path,
        [sys.executable, "-c", "raise AssertionError('must not run')"],
        key=f"carrier-observation-{failure}",
    )
    task_id = str(brief["task_id"])
    launch = {
        "task_id": task_id,
        "host": (
            "definitely-not-the-local-host"
            if failure == "different-host"
            else socket.gethostname()
        ),
        "tmux_socket": tasks_module._tmux_socket_name(tmp_path, task_id),
        "tmux_session": f"aros-task-{task_id.lower()}",
    }

    def fail_carrier_observation(
        command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        if failure == "oserror":
            raise OSError("injected carrier observation failure")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 3)
        return subprocess.CompletedProcess(command, 2, "", "unexpected status")

    monkeypatch.setattr(tasks_module.subprocess, "run", fail_carrier_observation)

    with pytest.raises(TaskError, match="carrier|tmux|inspect"):
        service._carrier_is_live(launch)


def test_reconciliation_marks_claimed_unreaped_zombie_lost(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, brief = _create_committed_task(
        tmp_path,
        [sys.executable, "-c", "raise SystemExit(0)"],
        key="zombie-reconciliation",
    )
    task_id = str(brief["task_id"])
    original_run = tasks_module.subprocess.run

    monkeypatch.setattr(tasks_module, "_LAUNCH_GRACE_SECONDS", 0.0)
    _fake_absent_tmux_carrier(monkeypatch)
    assert service.start(task_id)["state"] == "lost"
    monkeypatch.setattr(tasks_module.subprocess, "run", original_run)
    runtime = tmp_path / ".aros" / "tasks" / task_id
    ownership = json.loads((runtime / "ownership.json").read_text(encoding="utf-8"))
    launch = json.loads((runtime / "launch.json").read_text(encoding="utf-8"))
    runner, runner_token = _spawn_unreaped_zombie_session_leader()
    adapter_process, adapter_token = _spawn_unreaped_zombie_session_leader()
    try:
        execution = task_runner_module.create_execution_claim(
            service,
            brief,
            ownership,
            launch,
            (runner, runner, runner_token),
        )
        assert execution is not None
        adapter = task_runner_module.create_adapter_claim(
            service,
            brief,
            ownership,
            launch,
            execution,
            (adapter_process, adapter_process, adapter_token),
            str(launch["launched_at"]),
        )
        running = task_runner_module.running_status_from_claims(
            brief,
            ownership,
            launch,
            execution,
            adapter,
        )
        atomic_write_json(runtime / "status.json", running)

        reconciled = service.status(task_id)

        assert reconciled["state"] == "lost"
        assert reconciled["reason"] == "process_absent_without_final_receipt"
        assert service.start(task_id)["state"] == "lost"
        assert not (runtime / "final.json").exists()
    finally:
        os.waitpid(runner, 0)
        os.waitpid(adapter_process, 0)


def test_reconciliation_keeps_dead_adapter_running_while_runner_finalizes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code = (
        "import time\n"
        "from pathlib import Path\n"
        "Path('adapter.ready').touch()\n"
        "while not Path('adapter.exit').exists():\n"
        "    time.sleep(0.01)\n"
    )
    service, brief = _create_committed_task(
        tmp_path,
        [sys.executable, "-c", code],
        key="runner-finalization-pending",
    )
    task_id = str(brief["task_id"])
    original_run = tasks_module.subprocess.run

    monkeypatch.setattr(tasks_module, "_LAUNCH_GRACE_SECONDS", 0.0)
    _fake_absent_tmux_carrier(monkeypatch)
    assert service.start(task_id)["state"] == "lost"
    monkeypatch.setattr(tasks_module.subprocess, "run", original_run)
    monkeypatch.setattr(tasks_module, "_LAUNCH_GRACE_SECONDS", 2.0)
    runtime = tmp_path / ".aros" / "tasks" / task_id
    worktree = tmp_path / ".worktree" / "tasks" / task_id
    adapter_pid: list[int] = []
    runner_reaped_adapter = Event()
    release_finalization = Event()
    original_popen_wait = task_runner_module.subprocess.Popen.wait

    def pause_after_adapter_reap(
        process: subprocess.Popen[bytes],
        *args: object,
        **kwargs: object,
    ) -> int:
        result = original_popen_wait(
            process,
            *args,  # type: ignore[arg-type]
            **kwargs,  # type: ignore[arg-type]
        )
        if adapter_pid and process.pid == adapter_pid[0]:
            runner_reaped_adapter.set()
            assert release_finalization.wait(timeout=5)
        return result

    monkeypatch.setattr(
        task_runner_module.subprocess.Popen,
        "wait",
        pause_after_adapter_reap,
    )

    with ThreadPoolExecutor(max_workers=1) as pool:
        runner = pool.submit(run_task, tmp_path, task_id)
        running = _wait_state(service, task_id, "running")
        monkeypatch.setattr(tasks_module, "_LAUNCH_GRACE_SECONDS", 0.0)
        adapter_pid.append(int(running["adapter_pid"]))
        ready_path = worktree / "adapter.ready"
        deadline = time.monotonic() + 5
        while not ready_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready_path.exists()
        (worktree / "adapter.exit").touch()
        try:
            assert runner_reaped_adapter.wait(timeout=5)
            assert not (runtime / "final.json").exists()
            pending = service.status(task_id)
            assert pending["state"] == "running"
        finally:
            release_finalization.set()
        assert runner.result(timeout=5) == 0

    terminal = service.status(task_id)
    assert terminal["state"] == "completed"
    assert terminal["exit_code"] == 0


def test_adapter_launch_failure_does_not_deadlock_finalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, brief = _create_committed_task(
        tmp_path,
        ["/definitely/missing/aros-adapter"],
        key="adapter-launch-failure",
    )
    task_id = str(brief["task_id"])
    original_run = tasks_module.subprocess.run

    monkeypatch.setattr(tasks_module, "_LAUNCH_GRACE_SECONDS", 0.0)
    _fake_absent_tmux_carrier(monkeypatch)
    assert service.start(task_id)["state"] == "lost"
    monkeypatch.setattr(tasks_module.subprocess, "run", original_run)

    child = os.fork()
    if child == 0:
        try:
            result = run_task(tmp_path, task_id)
        except BaseException:
            os._exit(2)
        os._exit(result)
    deadline = time.monotonic() + 3
    waited = 0
    wait_status = 0
    while time.monotonic() < deadline:
        waited, wait_status = os.waitpid(child, os.WNOHANG)
        if waited == child:
            break
        time.sleep(0.02)
    if waited != child:
        os.kill(child, signal.SIGKILL)
        os.waitpid(child, 0)
    assert waited == child, "task runner deadlocked finalizing Popen failure"
    assert os.waitstatus_to_exitcode(wait_status) == 1
    terminal = service.status(task_id)
    assert terminal["state"] == "failed_process"


def test_adapter_claim_failure_closes_gate_and_reaps_unclaimed_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code = (
        "import time;from pathlib import Path;"
        "Path('claim-failure-marker').touch();time.sleep(30)"
    )
    service, brief = _create_committed_task(
        tmp_path,
        [sys.executable, "-c", code],
        key="adapter-claim-failure",
    )
    task_id = str(brief["task_id"])
    original_run = tasks_module.subprocess.run

    monkeypatch.setattr(tasks_module, "_LAUNCH_GRACE_SECONDS", 0.0)
    _fake_absent_tmux_carrier(monkeypatch)
    assert service.start(task_id)["state"] == "lost"
    monkeypatch.setattr(tasks_module.subprocess, "run", original_run)
    original_popen = task_runner_module.subprocess.Popen
    adapter_pids: list[int] = []

    def capture_adapter(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        process = original_popen(*args, **kwargs)  # type: ignore[arg-type]
        if kwargs.get("start_new_session") is True:
            adapter_pids.append(process.pid)
        return process

    def fail_claim(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise OSError("injected adapter claim failure")

    monkeypatch.setattr(task_runner_module.subprocess, "Popen", capture_adapter)
    monkeypatch.setattr(task_runner_module, "create_adapter_claim", fail_claim)
    marker = tmp_path / ".worktree" / "tasks" / task_id / "claim-failure-marker"

    try:
        assert run_task(tmp_path, task_id) == 1
        assert adapter_pids
        _assert_processes_stop(adapter_pids[0])
        assert not marker.exists()
        terminal = service.status(task_id)
        assert terminal["state"] == "failed_process"
        final = json.loads(
            (tmp_path / ".aros" / "tasks" / task_id / "final.json").read_text(
                encoding="utf-8"
            )
        )
        assert "adapter claim failure" in str(final["error"])
    finally:
        for pid in adapter_pids:
            if _process_is_running(pid):
                os.killpg(pid, signal.SIGKILL)


def test_runner_hard_death_before_gate_never_executes_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code = (
        "import time;from pathlib import Path;"
        "Path('hard-crash-marker').touch();time.sleep(30)"
    )
    service, brief = _create_committed_task(
        tmp_path,
        [sys.executable, "-c", code],
        key="runner-hard-death-gate",
    )
    task_id = str(brief["task_id"])
    original_run = tasks_module.subprocess.run

    monkeypatch.setattr(tasks_module, "_LAUNCH_GRACE_SECONDS", 0.0)
    _fake_absent_tmux_carrier(monkeypatch)
    assert service.start(task_id)["state"] == "lost"
    monkeypatch.setattr(tasks_module.subprocess, "run", original_run)
    entered_read, entered_write = os.pipe()
    pid_read, pid_write = os.pipe()
    original_popen = task_runner_module.subprocess.Popen

    def capture_adapter(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        process = original_popen(*args, **kwargs)  # type: ignore[arg-type]
        if kwargs.get("start_new_session") is True:
            os.write(pid_write, f"{process.pid}\n".encode())
        return process

    def block_claim(*_args: object, **_kwargs: object) -> dict[str, object]:
        os.write(entered_write, b"1")
        time.sleep(30)
        raise AssertionError("runner claim block unexpectedly returned")

    monkeypatch.setattr(task_runner_module.subprocess, "Popen", capture_adapter)
    monkeypatch.setattr(task_runner_module, "create_adapter_claim", block_claim)
    runner = os.fork()
    if runner == 0:
        os.close(entered_read)
        os.close(pid_read)
        try:
            run_task(tmp_path, task_id)
        except BaseException:
            os._exit(2)
        os._exit(0)
    os.close(entered_write)
    os.close(pid_write)
    assert os.read(entered_read, 1) == b"1"
    os.close(entered_read)
    with os.fdopen(pid_read, "r", encoding="utf-8") as stream:
        adapter_pid = int(stream.readline().strip())
    marker = tmp_path / ".worktree" / "tasks" / task_id / "hard-crash-marker"

    try:
        os.kill(runner, signal.SIGKILL)
        os.waitpid(runner, 0)
        _assert_processes_stop(adapter_pid)
        assert not marker.exists()
        assert service.status(task_id)["state"] == "lost"
    finally:
        if _process_is_running(adapter_pid):
            os.killpg(adapter_pid, signal.SIGKILL)


def test_subreaper_unavailable_fails_closed_before_adapter_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, brief = _create_committed_task(
        tmp_path,
        [
            sys.executable,
            "-c",
            "from pathlib import Path;Path('subreaper-failure-marker').touch()",
        ],
        key="subreaper-unavailable",
    )
    task_id = str(brief["task_id"])
    original_run = tasks_module.subprocess.run

    class FailingPrctl:
        argtypes: object = None
        restype: object = None

        def __call__(self, *_args: object) -> int:
            ctypes.set_errno(errno.EPERM)
            return -1

    class LibcWithoutSubreaper:
        prctl = FailingPrctl()

    def libc_without_subreaper(*_args: object, **_kwargs: object) -> object:
        return LibcWithoutSubreaper()

    monkeypatch.setattr(tasks_module, "_LAUNCH_GRACE_SECONDS", 0.0)
    _fake_absent_tmux_carrier(monkeypatch)
    assert service.start(task_id)["state"] == "lost"
    monkeypatch.setattr(tasks_module.subprocess, "run", original_run)
    monkeypatch.setattr(ctypes, "CDLL", libc_without_subreaper)

    runner = os.fork()
    if runner == 0:
        try:
            run_task(tmp_path, task_id)
        except TaskError:
            os._exit(0)
        os._exit(0)
    waited, wait_status = os.waitpid(runner, 0)

    runtime = tmp_path / ".aros" / "tasks" / task_id
    marker = tmp_path / ".worktree" / "tasks" / task_id / "subreaper-failure-marker"
    assert waited == runner
    assert os.waitstatus_to_exitcode(wait_status) == 0
    assert not marker.exists()
    assert not (runtime / "adapter.json").exists()
    assert not (runtime / "final.json").exists()
    assert service.status(task_id)["state"] in {"failed_process", "lost"}


def test_duplicate_runner_invocation_never_spawns_a_second_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code = (
        "import os,time;"
        "f=open('runner-attempts.log','a',encoding='utf-8');"
        "f.write(str(os.getpid())+'\\n');f.close();time.sleep(0.3)"
    )
    service, brief = _create_committed_task(
        tmp_path,
        [sys.executable, "-c", code],
        key="duplicate-runner",
    )
    task_id = str(brief["task_id"])
    original_run = tasks_module.subprocess.run

    monkeypatch.setattr(tasks_module, "_LAUNCH_GRACE_SECONDS", 0.0)
    _fake_absent_tmux_carrier(monkeypatch)
    assert service.start(task_id)["state"] == "lost"
    monkeypatch.setattr(tasks_module.subprocess, "run", original_run)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run_task(tmp_path, task_id), range(2)))

    attempts = tmp_path / ".worktree" / "tasks" / task_id / "runner-attempts.log"
    assert results == [0, 0]
    assert len(attempts.read_text(encoding="utf-8").splitlines()) == 1
    assert service.status(task_id)["state"] == "completed"


def test_preexisting_adapter_claim_prevents_popen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "adapter-must-not-start"
    code = (
        "import time;from pathlib import Path;"
        f"time.sleep(0.1);Path({str(marker)!r}).touch()"
    )
    service, brief = _create_committed_task(
        tmp_path,
        [sys.executable, "-c", code],
        key="preexisting-adapter-claim",
    )
    task_id = str(brief["task_id"])
    original_run = tasks_module.subprocess.run

    monkeypatch.setattr(tasks_module, "_LAUNCH_GRACE_SECONDS", 0.0)
    _fake_absent_tmux_carrier(monkeypatch)
    assert service.start(task_id)["state"] == "lost"
    monkeypatch.setattr(tasks_module.subprocess, "run", original_run)
    runtime = tmp_path / ".aros" / "tasks" / task_id
    atomic_write_json(runtime / "adapter.json", {"forged": True})

    with pytest.raises(TaskError, match="adapter|absent|exist|claim"):
        run_task(tmp_path, task_id)
    time.sleep(0.2)

    assert not marker.exists()
    assert not (runtime / "final.json").exists()


def test_concurrent_starts_create_one_launch_and_one_adapter_attempt(
    tmp_path: Path,
) -> None:
    code = (
        "import time;from pathlib import Path;"
        "p=Path('attempts.log');"
        "p.write_text((p.read_text() if p.exists() else '')+'attempt\\n');"
        "time.sleep(0.3)"
    )
    service, brief = _create_committed_task(
        tmp_path,
        [sys.executable, "-c", code],
        key="concurrent-launch",
    )
    task_id = str(brief["task_id"])

    with ThreadPoolExecutor(max_workers=4) as pool:
        starts = list(pool.map(lambda _: service.start(task_id), range(4)))
    terminal = _wait_terminal(service, task_id)

    assert terminal["state"] == "completed"
    assert all(
        status["launch_sha256"] == terminal["launch_sha256"] for status in starts
    )
    attempts = tmp_path / ".worktree" / "tasks" / task_id / "attempts.log"
    assert attempts.read_text(encoding="utf-8") == "attempt\n"
    runtime = tmp_path / ".aros" / "tasks" / task_id
    assert len(list(runtime.glob("launch.json"))) == 1
    assert len(list(runtime.glob("final.json"))) == 1


def test_start_race_reconciles_launch_published_before_ensure_worktree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, brief = _create_committed_task(
        tmp_path,
        [sys.executable, "-c", "print('one attempt')"],
        key="launch-before-ensure-race",
    )
    task_id = str(brief["task_id"])
    competing = TaskService(tmp_path)
    original_ensure = service._ensure_worktree
    raced = False

    def publish_before_ensure(
        selected_task_id: str,
        *,
        actor: str | None = None,
    ) -> dict[str, object]:
        nonlocal raced
        if not raced:
            raced = True
            competing.start(selected_task_id, actor=actor)
        return original_ensure(selected_task_id, actor=actor)

    monkeypatch.setattr(service, "_ensure_worktree", publish_before_ensure)

    result = service.start(task_id)

    assert raced
    assert result["state"] == "completed"
    runtime = tmp_path / ".aros" / "tasks" / task_id
    assert len(list(runtime.glob("launch.json"))) == 1
    assert len(list(runtime.glob("final.json"))) == 1


@pytest.mark.parametrize("stale_state", ("missing", "launched", "lost"))
def test_valid_final_repairs_missing_or_stale_status(
    tmp_path: Path,
    stale_state: str,
) -> None:
    service, brief = _create_committed_task(
        tmp_path,
        [sys.executable, "-c", "print('complete')"],
        key=f"final-repair-{stale_state}",
    )
    task_id = str(brief["task_id"])
    service.start(task_id)
    terminal = _wait_terminal(service, task_id)
    runtime = tmp_path / ".aros" / "tasks" / task_id
    status_path = runtime / "status.json"
    ownership = json.loads((runtime / "ownership.json").read_text(encoding="utf-8"))
    launch = json.loads((runtime / "launch.json").read_text(encoding="utf-8"))
    if stale_state == "missing":
        status_path.unlink()
    elif stale_state == "launched":
        atomic_write_json(status_path, launched_status(brief, ownership, launch))
    else:
        atomic_write_json(status_path, lost_status(brief, ownership, launch, terminal))

    repaired = TaskService(tmp_path).status(task_id)

    assert repaired["state"] == "completed"
    assert repaired == terminal
    assert json.loads(status_path.read_text(encoding="utf-8")) == terminal


def test_late_runner_final_repairs_lost_without_a_second_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, brief = _create_committed_task(
        tmp_path,
        [sys.executable, "-c", "print('late final')"],
        key="late-final",
    )
    task_id = str(brief["task_id"])
    original_run = tasks_module.subprocess.run

    monkeypatch.setattr(tasks_module, "_LAUNCH_GRACE_SECONDS", 0.0)
    carrier_calls = _fake_absent_tmux_carrier(monkeypatch)
    assert service.start(task_id)["state"] == "lost"
    monkeypatch.setattr(tasks_module.subprocess, "run", original_run)

    assert run_task(tmp_path, task_id) == 0
    repaired = service.status(task_id)

    assert repaired["state"] == "completed"
    assert len(carrier_calls) == 1
    final = json.loads(
        (tmp_path / ".aros" / "tasks" / task_id / "final.json").read_text(
            encoding="utf-8"
        )
    )
    assert final["state"] == "completed"


@pytest.mark.parametrize("target", ("final", "stdout"))
def test_execution_paths_reject_symlinks_before_launch(
    tmp_path: Path,
    target: str,
) -> None:
    service, brief = _create_committed_task(
        tmp_path,
        [sys.executable, "-c", "print('must not run')"],
        key=f"symlink-{target}",
    )
    task_id = str(brief["task_id"])
    service._ensure_worktree(task_id)
    runtime = tmp_path / ".aros" / "tasks" / task_id
    outside = tmp_path / ".git" / f"outside-{target}"
    outside.write_text("preserve\n", encoding="utf-8")
    name = "final.json" if target == "final" else "stdout.log"
    (runtime / name).symlink_to(outside)

    with pytest.raises(TaskError, match="symlink|plain|conflict|must not exist"):
        service.start(task_id)

    assert outside.read_text(encoding="utf-8") == "preserve\n"
    assert not (runtime / "launch.json").exists()


def test_preseeded_nonempty_log_is_rejected_before_launch(
    tmp_path: Path,
) -> None:
    service, brief = _create_committed_task(
        tmp_path,
        [sys.executable, "-c", "print('adapter bytes')"],
        key="preseeded-log",
    )
    task_id = str(brief["task_id"])
    runtime = tmp_path / ".aros" / "tasks" / task_id
    stdout = runtime / "stdout.log"
    stdout.write_bytes(b"forged pre-launch bytes\n")
    stdout.chmod(0o600)

    with pytest.raises(TaskError, match="empty|log|output"):
        service.start(task_id)

    assert stdout.read_bytes() == b"forged pre-launch bytes\n"
    assert not (runtime / "launch.json").exists()


@pytest.mark.parametrize("problem", ("symlink", "hardlink", "nonempty"))
def test_normalized_permissions_still_reject_preexisting_log_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    problem: str,
) -> None:
    monkeypatch.setattr(
        tasks_module,
        "_probe_filesystem_permissions",
        _normalized_permission_probe,
    )
    service, brief = _create_committed_task(
        tmp_path,
        [sys.executable, "-c", "print('must not run')"],
        key=f"normalized-preexisting-log-{problem}",
    )
    task_id = str(brief["task_id"])
    runtime = tmp_path / ".aros" / "tasks" / task_id
    stdout = runtime / "stdout.log"
    outside = tmp_path / ".git" / f"outside-{problem}"
    outside.write_bytes(b"preserve\n" if problem != "hardlink" else b"")
    outside.chmod(0o666)
    if problem == "symlink":
        stdout.symlink_to(outside)
    elif problem == "hardlink":
        os.link(outside, stdout)
    else:
        stdout.write_bytes(b"forged pre-launch bytes\n")
        stdout.chmod(0o666)

    with pytest.raises(TaskError, match="exist|log|output|link|plain"):
        service.start(task_id)

    assert not (runtime / "launch.json").exists()
    assert outside.read_bytes() == (b"preserve\n" if problem != "hardlink" else b"")
    if problem == "hardlink":
        assert stdout.stat().st_nlink == outside.stat().st_nlink == 2


def test_preseeded_runner_home_is_rejected_before_tmux_launch(
    tmp_path: Path,
) -> None:
    service, brief = _create_committed_task(
        tmp_path,
        [sys.executable, "-c", "print('must not launch')"],
        key="preseeded-runner-home",
    )
    task_id = str(brief["task_id"])
    runtime = tmp_path / ".aros" / "tasks" / task_id
    malicious = (
        runtime
        / "home"
        / ".local"
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    malicious.mkdir(parents=True)
    (malicious / "sitecustomize.py").write_text(
        "raise RuntimeError('preseeded runner HOME')\n",
        encoding="utf-8",
    )

    with pytest.raises(TaskError, match="HOME|exist|empty|runner"):
        service.start(task_id)

    assert not (runtime / "launch.json").exists()


@pytest.mark.parametrize("tamper", ("final", "stdout"))
def test_final_receipt_and_log_tamper_fail_closed(
    tmp_path: Path,
    tamper: str,
) -> None:
    service, brief = _create_committed_task(
        tmp_path,
        [sys.executable, "-c", "print('immutable output')"],
        key=f"tamper-{tamper}",
    )
    task_id = str(brief["task_id"])
    service.start(task_id)
    _wait_terminal(service, task_id)
    runtime = tmp_path / ".aros" / "tasks" / task_id
    if tamper == "final":
        final_path = runtime / "final.json"
        final = json.loads(final_path.read_text(encoding="utf-8"))
        final["exit_code"] = 99
        atomic_write_json(final_path, final)
    else:
        with (runtime / "stdout.log").open("ab") as stream:
            stream.write(b"tamper\n")

    with pytest.raises(TaskError, match="hash|size|receipt"):
        TaskService(tmp_path).status(task_id)


@pytest.mark.parametrize(
    "tamper",
    ("exit_state", "process_group", "process_token", "timing"),
)
def test_rehashed_final_receipt_semantic_tamper_fails_closed(
    tmp_path: Path,
    tamper: str,
) -> None:
    service, brief = _create_committed_task(
        tmp_path,
        [sys.executable, "-c", "print('strict final')"],
        key=f"semantic-final-tamper-{tamper}",
    )
    task_id = str(brief["task_id"])
    service.start(task_id)
    _wait_terminal(service, task_id)
    final_path = tmp_path / ".aros" / "tasks" / task_id / "final.json"
    final = json.loads(final_path.read_text(encoding="utf-8"))
    if tamper == "exit_state":
        final["exit_code"] = 9
    elif tamper == "process_group":
        final["adapter_pgid"] = int(final["adapter_pid"]) + 1
    elif tamper == "process_token":
        final["adapter_start_token"] = "forged-token"
    else:
        final["finished_at"] = "2000-01-01T00:00:00.000Z"
    final["final_sha256"] = json_sha256(
        {key: value for key, value in final.items() if key != "final_sha256"}
    )
    atomic_write_json(final_path, final)

    with pytest.raises(TaskError, match="exit|process|tim|receipt|state"):
        TaskService(tmp_path).status(task_id)


@pytest.mark.parametrize(
    "tamper",
    (
        "launch_boolean",
        "launch_probe_relation",
        "launch_support_relation",
        "final_probe_copy",
    ),
)
def test_rehashed_filesystem_permission_probe_tamper_fails_closed(
    tmp_path: Path,
    tamper: str,
) -> None:
    service, brief = _create_committed_task(
        tmp_path,
        [sys.executable, "-c", "print('permission-bound final')"],
        key=f"permission-probe-tamper-{tamper}",
    )
    task_id = str(brief["task_id"])
    service.start(task_id)
    _wait_terminal(service, task_id)
    runtime = tmp_path / ".aros" / "tasks" / task_id
    if tamper.startswith("launch"):
        path = runtime / "launch.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        if tamper == "launch_boolean":
            record["filesystem_permissions_enforced"] = False
        elif tamper == "launch_probe_relation":
            record["filesystem_permission_probe"]["observed_mode"] = 0o666
        else:
            record["filesystem_permission_probe"]["mode_request_supported"] = False
        record["launch_sha256"] = json_sha256(
            {key: value for key, value in record.items() if key != "launch_sha256"}
        )
    else:
        path = runtime / "final.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        record["filesystem_permission_probe"]["device"] += 1
        record["final_sha256"] = json_sha256(
            {key: value for key, value in record.items() if key != "final_sha256"}
        )
    atomic_write_json(path, record)

    with pytest.raises(TaskError, match="filesystem|permission|probe|lineage"):
        TaskService(tmp_path).status(task_id)


def test_real_launcher_exit_does_not_terminate_child(
    tmp_path: Path,
) -> None:
    code = (
        "import time;from pathlib import Path;"
        "time.sleep(1.0);Path('survived-launcher.txt').write_text('alive\\n')"
    )
    service, brief = _create_committed_task(
        tmp_path,
        [sys.executable, "-c", code],
        key="launcher-exit",
    )
    task_id = str(brief["task_id"])
    child = os.fork()
    if child == 0:
        try:
            service.start(task_id)
        except BaseException:
            os._exit(1)
        os._exit(0)

    waited, wait_status = os.waitpid(child, 0)
    marker = tmp_path / ".worktree" / "tasks" / task_id / "survived-launcher.txt"
    assert waited == child
    assert os.waitstatus_to_exitcode(wait_status) == 0
    assert not marker.exists()

    terminal = _wait_terminal(TaskService(tmp_path), task_id)

    assert terminal["state"] == "completed"
    assert marker.read_text(encoding="utf-8") == "alive\n"


def test_tmux_loss_does_not_override_matching_live_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code = (
        "import time;from pathlib import Path;"
        "time.sleep(0.8);Path('survived-tmux.txt').write_text('alive\\n')"
    )
    service, brief = _create_committed_task(
        tmp_path,
        [sys.executable, "-c", code],
        key="tmux-loss-live-process",
    )
    task_id = str(brief["task_id"])
    running = service.start(task_id)
    if running["state"] != "running":
        running = _wait_state(service, task_id, "running")
    runtime = tmp_path / ".aros" / "tasks" / task_id
    launch = json.loads((runtime / "launch.json").read_text(encoding="utf-8"))

    killed = subprocess.run(
        [
            "tmux",
            "-L",
            str(launch["tmux_socket"]),
            "kill-session",
            "-t",
            f"={launch['tmux_session']}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert killed.returncode == 0
    ownership = json.loads((runtime / "ownership.json").read_text(encoding="utf-8"))
    atomic_write_json(
        runtime / "status.json",
        launched_status(brief, ownership, launch),
    )
    assert service.status(task_id)["state"] == "running"

    marker = tmp_path / ".worktree" / "tasks" / task_id / "survived-tmux.txt"
    deadline = time.monotonic() + 5
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert marker.read_text(encoding="utf-8") == "alive\n"
    monkeypatch.setattr(tasks_module, "_LAUNCH_GRACE_SECONDS", 0.0)
    assert service.status(task_id)["state"] == "lost"
    assert not (runtime / "final.json").exists()


def test_stop_delivers_to_live_process_group_after_runner_and_tmux_loss(
    tmp_path: Path,
) -> None:
    service, brief = _create_committed_task(
        tmp_path,
        [sys.executable, "-c", _term_ignoring_process_tree_code()],
        timeout_seconds=30,
        key="orphan-stop-delivery",
    )
    task_id = str(brief["task_id"])
    running = service.start(task_id)
    if running["state"] != "running":
        running = _wait_state(service, task_id, "running")
    runtime = tmp_path / ".aros" / "tasks" / task_id
    worktree = tmp_path / ".worktree" / "tasks" / task_id
    descendant_path = worktree / "descendant.pid"
    deadline = time.monotonic() + 5
    while not descendant_path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    descendant_pid = int(descendant_path.read_text(encoding="utf-8"))
    launch = json.loads((runtime / "launch.json").read_text(encoding="utf-8"))
    subprocess.run(
        [
            "tmux",
            "-L",
            str(launch["tmux_socket"]),
            "kill-session",
            "-t",
            f"={launch['tmux_session']}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    deadline = time.monotonic() + 3
    while (
        _process_is_running(int(running["runner_pid"])) and time.monotonic() < deadline
    ):
        time.sleep(0.02)
    assert not _process_is_running(int(running["runner_pid"]))
    assert _process_is_running(int(running["adapter_pid"]))

    try:
        service.stop(task_id, actor="human", reason="stop orphaned process group")
        deadline = time.monotonic() + 4
        while (
            not (runtime / "stop-result.json").exists() and time.monotonic() < deadline
        ):
            time.sleep(0.02)
        result = json.loads((runtime / "stop-result.json").read_text(encoding="utf-8"))
        assert result["delivered"] is True
        assert result["signal_sequence"] == ["TERM", "KILL"]
        _assert_processes_stop(int(running["adapter_pid"]), descendant_pid)
    finally:
        if _process_is_running(int(running["adapter_pid"])):
            os.killpg(int(running["adapter_pgid"]), signal.SIGKILL)


def test_stop_delivers_to_live_descendant_after_leader_is_reaped(
    tmp_path: Path,
) -> None:
    descendant = (
        "import signal,time;from pathlib import Path;"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        "signal.signal(signal.SIGINT,signal.SIG_IGN);"
        "Path('descendant.ready').touch();"
        "time.sleep(30)"
    )
    code = (
        "import subprocess,sys,time\n"
        "from pathlib import Path\n"
        f"child=subprocess.Popen([sys.executable,'-c',{descendant!r}])\n"
        "Path('descendant.pid').write_text(str(child.pid),encoding='utf-8')\n"
        "deadline=time.monotonic()+5\n"
        "while not Path('descendant.ready').exists() and time.monotonic()<deadline:\n"
        "    time.sleep(0.01)\n"
        "if not Path('descendant.ready').exists():\n"
        "    raise SystemExit(2)\n"
        "Path('leader.ready').touch()\n"
        "while not Path('leader.exit').exists():\n"
        "    time.sleep(0.01)\n"
    )
    service, brief = _create_committed_task(
        tmp_path,
        [sys.executable, "-c", code],
        timeout_seconds=30,
        key="stop-after-reaped-leader",
    )
    task_id = str(brief["task_id"])
    running = service.start(task_id)
    if running["state"] != "running":
        running = _wait_state(service, task_id, "running")
    runtime = tmp_path / ".aros" / "tasks" / task_id
    worktree = tmp_path / ".worktree" / "tasks" / task_id
    ready_path = worktree / "leader.ready"
    deadline = time.monotonic() + 5
    while not ready_path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert ready_path.exists()
    adapter_pid = int(running["adapter_pid"])
    adapter_pgid = int(running["adapter_pgid"])
    descendant_pid = int(
        (worktree / "descendant.pid").read_text(encoding="utf-8")
    )

    try:
        (worktree / "leader.exit").touch()
        deadline = time.monotonic() + 5
        while _process_state(adapter_pid) is not None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert _process_state(adapter_pid) is None
        assert _process_is_running(descendant_pid)
        assert service.status(task_id)["state"] == "running"

        service.stop(task_id, actor="human", reason="stop reaped leader")
        final_path = runtime / "final.json"
        deadline = time.monotonic() + 5
        while not final_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert final_path.exists()
        terminal = service.status(task_id)
        final = json.loads(final_path.read_text(encoding="utf-8"))
        assert terminal["state"] == "cancelled"
        assert final["stop"]["delivered"] is True
        assert final["stop"]["signal_sequence"] == ["TERM", "KILL"]
        assert final["signal_sequence"] == ["TERM", "KILL"]
        _assert_processes_stop(adapter_pid, descendant_pid)
    finally:
        if _process_is_running(descendant_pid):
            os.killpg(adapter_pgid, signal.SIGKILL)
        _assert_processes_stop(descendant_pid)


def test_timeout_terminates_term_ignoring_process_group_and_reaps_leader(
    tmp_path: Path,
) -> None:
    service, brief = _create_committed_task(
        tmp_path,
        [sys.executable, "-c", _term_ignoring_process_tree_code()],
        timeout_seconds=0.3,
        key="timeout-process-group",
    )
    task_id = str(brief["task_id"])

    service.start(task_id)
    terminal = _wait_terminal(service, task_id)

    runtime = tmp_path / ".aros" / "tasks" / task_id
    worktree = tmp_path / ".worktree" / "tasks" / task_id
    descendant_pid = int((worktree / "descendant.pid").read_text(encoding="utf-8"))
    final = json.loads((runtime / "final.json").read_text(encoding="utf-8"))
    assert terminal["state"] == "timed_out"
    assert final["timeout"] == {"timeout_seconds": 0.3, "triggered": True}
    assert final["signal_sequence"] == ["TERM", "KILL"]
    _assert_processes_stop(int(final["adapter_pid"]), descendant_pid)


def test_runner_waits_for_escaped_session_descendant_before_finalizing(
    tmp_path: Path,
) -> None:
    descendant = (
        "import time\n"
        "from pathlib import Path\n"
        "Path('escaped.ready').write_text('ready',encoding='utf-8')\n"
        "while not Path('escaped.release').exists():\n"
        "    time.sleep(0.01)\n"
        "Path('escaped.marker').write_text('finished\\n',encoding='utf-8')\n"
        "print('escaped stdout',flush=True)\n"
    )
    leader = (
        "import subprocess,sys,time\n"
        "from pathlib import Path\n"
        f"child=subprocess.Popen([sys.executable,'-c',{descendant!r}],"
        "start_new_session=True)\n"
        "Path('escaped.pid').write_text(str(child.pid),encoding='utf-8')\n"
        "deadline=time.monotonic()+5\n"
        "while not Path('escaped.ready').exists() and time.monotonic()<deadline:\n"
        "    time.sleep(0.01)\n"
        "if not Path('escaped.ready').exists():\n"
        "    raise SystemExit(2)\n"
        "print('leader stdout',flush=True)\n"
    )
    service, brief = _create_committed_task(
        tmp_path,
        [sys.executable, "-c", leader],
        key="escaped-session-descendant",
    )
    task_id = str(brief["task_id"])

    service.start(task_id)
    runtime = tmp_path / ".aros" / "tasks" / task_id
    worktree = tmp_path / ".worktree" / "tasks" / task_id
    ready_path = worktree / "escaped.ready"
    deadline = time.monotonic() + 5
    while not ready_path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert ready_path.exists()
    descendant_pid = int((worktree / "escaped.pid").read_text(encoding="utf-8"))
    execution = json.loads((runtime / "execution.json").read_text(encoding="utf-8"))
    adapter = json.loads((runtime / "adapter.json").read_text(encoding="utf-8"))
    runner_pid = int(execution["runner_pid"])
    adapter_pid = int(adapter["adapter_pid"])
    deadline = time.monotonic() + 5
    while _process_is_running(adapter_pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    deadline = time.monotonic() + 1
    while (
        _process_is_running(runner_pid)
        and not (runtime / "final.json").exists()
        and time.monotonic() < deadline
    ):
        time.sleep(0.02)

    release_path = worktree / "escaped.release"
    marker_path = worktree / "escaped.marker"
    try:
        assert not _process_is_running(adapter_pid)
        assert _process_is_running(descendant_pid)
        assert _process_is_running(runner_pid)
        assert service.status(task_id)["state"] == "running"
        assert not marker_path.exists()
        assert not (runtime / "final.json").exists()
        release_path.touch()

        terminal = _wait_terminal(service, task_id)
        final_path = runtime / "final.json"
        final_bytes = final_path.read_bytes()
        stdout = (runtime / "stdout.log").read_bytes()
        marker = marker_path.read_bytes()
        final = json.loads(final_bytes)
        assert terminal["state"] == "completed"
        assert final["exit_code"] == 0
        assert stdout == b"leader stdout\nescaped stdout\n"
        assert marker == b"finished\n"
        deadline = time.monotonic() + 5
        while _process_state(descendant_pid) is not None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert _process_state(descendant_pid) is None
        time.sleep(0.2)
        assert final_path.read_bytes() == final_bytes
        assert (runtime / "stdout.log").read_bytes() == stdout
        assert marker_path.read_bytes() == marker
    finally:
        release_path.touch(exist_ok=True)
        deadline = time.monotonic() + 3
        while _process_is_running(descendant_pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        if _process_is_running(descendant_pid):
            os.killpg(descendant_pid, signal.SIGKILL)
        _assert_processes_stop(descendant_pid)


def test_persistent_escaped_session_after_normal_exit_fails_closed_as_lost(
    tmp_path: Path,
) -> None:
    with _persistent_escaped_session_task(
        tmp_path,
        timeout_seconds=30,
        key="persistent-escaped-session-normal-exit",
    ) as task:
        service, task_id, runtime, worktree, runner_pid, adapter_pid, descendant_pid = task

        assert _wait_state(service, task_id, "lost", timeout=5)["state"] == "lost"
        assert not (runtime / "final.json").exists()
        assert service.start(task_id)["state"] == "lost"
        with pytest.raises(TaskError, match="not terminal"):
            service.collect(task_id)
        with pytest.raises(TaskError, match="strict collection"):
            service.prune(task_id)
        assert worktree.is_dir()
        assert not _process_is_running(runner_pid)
        assert not _process_is_running(adapter_pid)
        assert _process_is_running(descendant_pid)
        assert os.getpgid(descendant_pid) == descendant_pid


def test_timeout_with_persistent_escaped_session_fails_closed_as_lost(
    tmp_path: Path,
) -> None:
    with _persistent_escaped_session_task(
        tmp_path,
        timeout_seconds=0.3,
        key="persistent-escaped-session-timeout",
        keep_leader_alive=True,
    ) as task:
        service, task_id, runtime, worktree, runner_pid, adapter_pid, descendant_pid = task

        assert _wait_state(service, task_id, "lost", timeout=5)["state"] == "lost"
        assert not (runtime / "final.json").exists()
        assert service.start(task_id)["state"] == "lost"
        with pytest.raises(TaskError, match="not terminal"):
            service.collect(task_id)
        with pytest.raises(TaskError, match="strict collection"):
            service.prune(task_id)
        assert worktree.is_dir()
        assert not _process_is_running(runner_pid)
        assert not _process_is_running(adapter_pid)
        assert _process_is_running(descendant_pid)
        assert os.getpgid(descendant_pid) == descendant_pid


def test_attributed_stop_with_persistent_escaped_session_fails_closed_as_lost(
    tmp_path: Path,
) -> None:
    with _persistent_escaped_session_task(
        tmp_path,
        timeout_seconds=30,
        key="persistent-escaped-session-stop",
        keep_leader_alive=True,
        ignore_leader_stop_signals=True,
    ) as task:
        service, task_id, runtime, worktree, runner_pid, adapter_pid, descendant_pid = task
        request = service.stop(
            task_id,
            actor="human",
            reason="stop uncontained descendant",
        )

        assert _wait_state(service, task_id, "lost", timeout=5)["state"] == "lost"
        assert not (runtime / "final.json").exists()
        assert service.start(task_id)["state"] == "lost"
        with pytest.raises(TaskError, match="not terminal"):
            service.collect(task_id)
        with pytest.raises(TaskError, match="strict collection"):
            service.prune(task_id)
        assert worktree.is_dir()
        result = json.loads(
            (runtime / "stop-result.json").read_text(encoding="utf-8")
        )
        assert request["actor"] == "human"
        assert request["reason"] == "stop uncontained descendant"
        assert result["delivered"] is True
        assert result["signal_sequence"] == ["TERM", "KILL"]
        assert not _process_is_running(runner_pid)
        assert not _process_is_running(adapter_pid)
        assert _process_is_running(descendant_pid)
        assert os.getpgid(descendant_pid) == descendant_pid


def test_runner_crash_with_adopted_escaped_session_never_creates_final(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _persistent_escaped_session_task(
        tmp_path,
        timeout_seconds=30,
        key="adopted-escaped-session-runner-crash",
        wait_for_leader_release=True,
    ) as task:
        service, task_id, runtime, worktree, runner_pid, adapter_pid, descendant_pid = task
        leader_release = worktree / "leader.release"
        leader_release.touch()
        children_path = Path(f"/proc/{runner_pid}/task/{runner_pid}/children")
        deadline = time.monotonic() + 1
        runner_children: set[int] = set()
        while time.monotonic() < deadline:
            runner_children = {
                int(value) for value in children_path.read_text(encoding="utf-8").split()
            }
            if descendant_pid in runner_children and adapter_pid not in runner_children:
                break
            time.sleep(0.01)

        assert _process_is_running(runner_pid)
        assert _process_state(adapter_pid) is None
        assert descendant_pid in runner_children
        assert adapter_pid not in runner_children
        os.kill(runner_pid, signal.SIGKILL)
        _assert_processes_stop(runner_pid)
        monkeypatch.setattr(tasks_module, "_LAUNCH_GRACE_SECONDS", 0.0)

        assert _wait_state(service, task_id, "lost", timeout=5)["state"] == "lost"
        assert not (runtime / "final.json").exists()
        assert service.start(task_id)["state"] == "lost"
        with pytest.raises(TaskError, match="not terminal"):
            service.collect(task_id)
        with pytest.raises(TaskError, match="strict collection"):
            service.prune(task_id)
        assert worktree.is_dir()
        assert _process_is_running(descendant_pid)
        assert os.getpgid(descendant_pid) == descendant_pid


def test_runner_reaps_adopted_zombies_while_adapter_leader_is_live(
    tmp_path: Path,
) -> None:
    grandchild_count = 8
    leader = (
        "import os,time\n"
        "from pathlib import Path\n"
        "intermediates=[]\n"
        f"for index in range({grandchild_count}):\n"
        "    pid=os.fork()\n"
        "    if pid==0:\n"
        "        grandchild=os.fork()\n"
        "        if grandchild==0:\n"
        "            Path(f'adopted-{index}.pid').write_text("
        "str(os.getpid()),encoding='utf-8')\n"
        "            while not Path('adopted.release').exists():\n"
        "                time.sleep(0.01)\n"
        "            os._exit(0)\n"
        "        os._exit(0)\n"
        "    intermediates.append(pid)\n"
        "for pid in intermediates:\n"
        "    os.waitpid(pid,0)\n"
        "deadline=time.monotonic()+5\n"
        f"while len(list(Path('.').glob('adopted-*.pid')))<{grandchild_count} "
        "and time.monotonic()<deadline:\n"
        "    time.sleep(0.01)\n"
        f"if len(list(Path('.').glob('adopted-*.pid')))!={grandchild_count}:\n"
        "    raise SystemExit(2)\n"
        "Path('leader.ready').touch()\n"
        "while not Path('leader.release').exists():\n"
        "    time.sleep(0.01)\n"
        "raise SystemExit(23)\n"
    )
    service, brief = _create_committed_task(
        tmp_path,
        [sys.executable, "-c", leader],
        key="reap-adopted-zombies-live-leader",
    )
    task_id = str(brief["task_id"])
    runtime = tmp_path / ".aros" / "tasks" / task_id
    worktree = tmp_path / ".worktree" / "tasks" / task_id
    adopted_release = worktree / "adopted.release"
    leader_release = worktree / "leader.release"
    final_path = runtime / "final.json"
    runner_pid: int | None = None
    adapter_pid: int | None = None
    adapter_pgid: int | None = None
    descendant_pids: set[int] = set()
    runner_children: set[int] = set()
    try:
        service.start(task_id)
        ready_path = worktree / "leader.ready"
        deadline = time.monotonic() + 5
        while not ready_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready_path.exists()
        descendant_pids = {
            int(path.read_text(encoding="utf-8"))
            for path in worktree.glob("adopted-*.pid")
        }
        assert len(descendant_pids) == grandchild_count
        runner_pid = _best_effort_test_pid(
            runtime / "execution.json",
            "runner_pid",
        )
        adapter_pid = _best_effort_test_pid(runtime / "adapter.json", "adapter_pid")
        adapter_pgid = _best_effort_test_pid(
            runtime / "adapter.json",
            "adapter_pgid",
        )
        assert runner_pid is not None
        assert adapter_pid is not None
        assert adapter_pgid is not None
        children_path = Path(f"/proc/{runner_pid}/task/{runner_pid}/children")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            runner_children = {
                int(value)
                for value in children_path.read_text(encoding="utf-8").split()
            }
            if descendant_pids <= runner_children:
                break
            time.sleep(0.02)

        assert descendant_pids <= runner_children
        assert _process_is_running(adapter_pid)
        adopted_release.touch()
        deadline = time.monotonic() + 3
        while (
            any(_process_state(pid) is not None for pid in descendant_pids)
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)

        assert _process_is_running(adapter_pid)
        assert all(_process_state(pid) is None for pid in descendant_pids)
        assert service.status(task_id)["state"] == "running"
        assert not final_path.exists()
        leader_release.touch()

        terminal = _wait_terminal(service, task_id)
        final = json.loads(final_path.read_text(encoding="utf-8"))
        assert terminal["state"] == "failed_process"
        assert terminal["exit_code"] == 23
        assert final["exit_code"] == 23
    finally:
        if worktree.is_dir():
            for release_path in (adopted_release, leader_release):
                try:
                    release_path.touch(exist_ok=True)
                except OSError:
                    pass
            for path in worktree.glob("adopted-*.pid"):
                child_pid = _best_effort_test_pid(path)
                if child_pid is not None:
                    descendant_pids.add(child_pid)
        runner_pid = runner_pid or _best_effort_test_pid(
            runtime / "execution.json",
            "runner_pid",
        )
        adapter_pid = adapter_pid or _best_effort_test_pid(
            runtime / "adapter.json",
            "adapter_pid",
        )
        adapter_pgid = adapter_pgid or _best_effort_test_pid(
            runtime / "adapter.json",
            "adapter_pgid",
        )
        deadline = time.monotonic() + 5
        while not final_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        if adapter_pid is not None and adapter_pgid == adapter_pid:
            _kill_test_process(adapter_pid, group=True)
        else:
            _kill_test_process(adapter_pid, group=False)
        _kill_test_process(runner_pid, group=False)
        for child_pid in descendant_pids:
            _kill_test_process(child_pid, group=False)
        recorded_pids = tuple(
            pid
            for pid in (adapter_pid, runner_pid, *sorted(descendant_pids))
            if pid is not None
        )
        _assert_processes_stop(*recorded_pids)


def test_runner_waits_for_process_group_drain_before_finalizing_late_stdout(
    tmp_path: Path,
) -> None:
    descendant = (
        "import time\n"
        "from pathlib import Path\n"
        "Path('descendant.ready').write_text('ready',encoding='utf-8')\n"
        "while not Path('descendant.release').exists():\n"
        "    time.sleep(0.01)\n"
        "print('late descendant stdout',flush=True)\n"
    )
    service, brief = _create_committed_task(
        tmp_path,
        [sys.executable, "-c", _exiting_leader_process_tree_code(descendant)],
        timeout_seconds=5,
        key="late-descendant-stdout",
    )
    task_id = str(brief["task_id"])
    running = service.start(task_id)
    if running["state"] != "running":
        running = _wait_state(service, task_id, "running")
    runtime = tmp_path / ".aros" / "tasks" / task_id
    worktree = tmp_path / ".worktree" / "tasks" / task_id
    descendant_path = worktree / "descendant.pid"
    deadline = time.monotonic() + 5
    while not descendant_path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    descendant_pid = int(descendant_path.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 5
    while (
        _process_is_running(int(running["adapter_pid"]))
        and time.monotonic() < deadline
    ):
        time.sleep(0.02)

    assert not _process_is_running(int(running["adapter_pid"]))
    assert _process_is_running(descendant_pid)
    assert service.status(task_id)["state"] == "running"
    assert not (runtime / "final.json").exists()
    (worktree / "descendant.release").touch()

    final_path = runtime / "final.json"
    deadline = time.monotonic() + 5
    while not final_path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert final_path.exists()
    terminal = service.status(task_id)
    stdout = (runtime / "stdout.log").read_bytes()
    final_bytes = final_path.read_bytes()
    final = json.loads(final_bytes)
    assert terminal["state"] == "completed"
    assert final["exit_code"] == 0
    assert stdout == b"leader stdout\nlate descendant stdout\n"
    assert final["stdout"] == {
        "path": f".aros/tasks/{task_id}/stdout.log",
        "bytes": len(stdout),
        "sha256": hashlib.sha256(stdout).hexdigest(),
    }
    _assert_processes_stop(descendant_pid)
    time.sleep(0.2)
    assert (runtime / "stdout.log").read_bytes() == stdout
    assert final_path.read_bytes() == final_bytes


def test_timeout_terminates_persistent_descendant_after_leader_exits(
    tmp_path: Path,
) -> None:
    descendant = (
        "import signal,time\n"
        "from pathlib import Path\n"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
        "signal.signal(signal.SIGINT,signal.SIG_IGN)\n"
        "Path('descendant.ready').write_text('ready',encoding='utf-8')\n"
        "while not Path('descendant.release').exists():\n"
        "    time.sleep(0.01)\n"
        "time.sleep(30)\n"
    )
    service, brief = _create_committed_task(
        tmp_path,
        [sys.executable, "-c", _exiting_leader_process_tree_code(descendant)],
        timeout_seconds=2,
        key="timeout-after-leader-exit",
    )
    task_id = str(brief["task_id"])

    running = service.start(task_id)
    if running["state"] != "running":
        running = _wait_state(service, task_id, "running")
    runtime = tmp_path / ".aros" / "tasks" / task_id
    worktree = tmp_path / ".worktree" / "tasks" / task_id
    descendant_path = worktree / "descendant.pid"
    deadline = time.monotonic() + 5
    while not descendant_path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    descendant_pid = int(descendant_path.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 5
    while (
        _process_is_running(int(running["adapter_pid"]))
        and time.monotonic() < deadline
    ):
        time.sleep(0.02)
    assert not _process_is_running(int(running["adapter_pid"]))
    assert _process_is_running(descendant_pid)
    assert service.status(task_id)["state"] == "running"
    assert not (runtime / "final.json").exists()
    (worktree / "descendant.release").touch()

    final_path = runtime / "final.json"
    deadline = time.monotonic() + 5
    while not final_path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert final_path.exists()
    terminal = service.status(task_id)
    final = json.loads(final_path.read_text(encoding="utf-8"))
    try:
        assert terminal["state"] == "timed_out"
        assert final["exit_code"] == 0
        assert final["timeout"] == {"timeout_seconds": 2, "triggered": True}
        assert final["signal_sequence"] == ["TERM", "KILL"]
        _assert_processes_stop(int(final["adapter_pid"]), descendant_pid)
    finally:
        if _process_is_running(descendant_pid):
            os.killpg(int(final["adapter_pgid"]), signal.SIGKILL)
        _assert_processes_stop(descendant_pid)


@pytest.mark.parametrize("release_group", (True, False), ids=("drains", "stuck"))
def test_timeout_waits_for_claimed_group_drain_after_kill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    release_group: bool,
) -> None:
    code = (
        "import signal,time;"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        "time.sleep(30)"
    )
    service, brief = _create_committed_task(
        tmp_path,
        [sys.executable, "-c", code],
        timeout_seconds=0.2,
        key=f"post-kill-drain-{release_group}",
    )
    task_id = str(brief["task_id"])
    original_run = tasks_module.subprocess.run

    monkeypatch.setattr(tasks_module, "_LAUNCH_GRACE_SECONDS", 0.0)
    _fake_absent_tmux_carrier(monkeypatch)
    assert service.start(task_id)["state"] == "lost"
    monkeypatch.setattr(tasks_module.subprocess, "run", original_run)
    monkeypatch.setattr(tasks_module, "_LAUNCH_GRACE_SECONDS", 2.0)
    if not release_group:
        monkeypatch.setattr(
            task_runner_module,
            "_GROUP_DRAIN_TIMEOUT_SECONDS",
            0.1,
            raising=False,
        )

    kill_sent = Event()
    post_kill_check = Event()
    release_drain = Event()
    original_claimed_group_is_live = task_runner_module._claimed_group_is_live
    original_signal_group = task_runner_module._signal_group

    def gated_claimed_group_is_live(pid: int, pgid: int, token: str) -> bool:
        if kill_sent.is_set():
            post_kill_check.set()
            if not release_drain.is_set():
                return True
        return original_claimed_group_is_live(pid, pgid, token)

    def observe_group_signal(
        pgid: int,
        signal_number: int,
        **kwargs: object,
    ) -> bool:
        delivered = original_signal_group(
            pgid,
            signal_number,
            **kwargs,  # type: ignore[arg-type]
        )
        if delivered and signal_number == signal.SIGKILL:
            kill_sent.set()
        return delivered

    monkeypatch.setattr(
        task_runner_module,
        "_claimed_group_is_live",
        gated_claimed_group_is_live,
    )
    monkeypatch.setattr(task_runner_module, "_signal_group", observe_group_signal)
    final_path = tmp_path / ".aros" / "tasks" / task_id / "final.json"

    with ThreadPoolExecutor(max_workers=1) as pool:
        runner = pool.submit(run_task, tmp_path, task_id)
        try:
            assert kill_sent.wait(timeout=5)
            assert post_kill_check.wait(timeout=1)
            assert not final_path.exists()
            if release_group:
                assert not runner.done()
                release_drain.set()
                assert runner.result(timeout=5) == 0
                final = json.loads(final_path.read_text(encoding="utf-8"))
                assert final["state"] == "timed_out"
            else:
                with pytest.raises(TaskError, match="group.*drain"):
                    runner.result(timeout=5)
                assert not final_path.exists()
        finally:
            release_drain.set()


def test_timeout_deadline_natural_group_drain_preserves_leader_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code = (
        "import time\n"
        "from pathlib import Path\n"
        "Path('adapter.ready').touch()\n"
        "while not Path('adapter.exit').exists():\n"
        "    time.sleep(0.01)\n"
    )
    service, brief = _create_committed_task(
        tmp_path,
        [sys.executable, "-c", code],
        timeout_seconds=0.2,
        key="natural-drain-at-timeout",
    )
    task_id = str(brief["task_id"])
    original_run = tasks_module.subprocess.run

    monkeypatch.setattr(tasks_module, "_LAUNCH_GRACE_SECONDS", 0.0)
    _fake_absent_tmux_carrier(monkeypatch)
    assert service.start(task_id)["state"] == "lost"
    monkeypatch.setattr(tasks_module.subprocess, "run", original_run)
    monkeypatch.setattr(tasks_module, "_LAUNCH_GRACE_SECONDS", 2.0)
    worktree = tmp_path / ".worktree" / "tasks" / task_id
    timeout_entered = Event()
    original_terminate_group = task_runner_module._terminate_group

    def drain_before_timeout_signal(
        process: subprocess.Popen[bytes],
        **kwargs: object,
    ) -> list[str]:
        timeout_entered.set()
        (worktree / "adapter.exit").touch()
        deadline = time.monotonic() + 5
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert process.returncode == 0
        return original_terminate_group(
            process,
            **kwargs,  # type: ignore[arg-type]
        )

    monkeypatch.setattr(
        task_runner_module,
        "_terminate_group",
        drain_before_timeout_signal,
    )

    with ThreadPoolExecutor(max_workers=1) as pool:
        runner = pool.submit(run_task, tmp_path, task_id)
        assert timeout_entered.wait(timeout=5)
        assert runner.result(timeout=5) == 0

    final = json.loads(
        (tmp_path / ".aros" / "tasks" / task_id / "final.json").read_text(
            encoding="utf-8"
        )
    )
    assert final["state"] == "completed"
    assert final["exit_code"] == 0
    assert final["timeout"] == {"timeout_seconds": 0.2, "triggered": False}
    assert final["signal_sequence"] == []


def test_concurrent_stop_delivery_signals_process_group_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code = (
        "import signal,time;"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        "time.sleep(30)"
    )
    service, brief = _create_committed_task(
        tmp_path,
        [sys.executable, "-c", code],
        timeout_seconds=30,
        key="single-stop-delivery-owner",
    )
    task_id = str(brief["task_id"])
    original_run = tasks_module.subprocess.run

    monkeypatch.setattr(tasks_module, "_LAUNCH_GRACE_SECONDS", 0.0)
    _fake_absent_tmux_carrier(monkeypatch)
    assert service.start(task_id)["state"] == "lost"
    monkeypatch.setattr(tasks_module.subprocess, "run", original_run)
    monkeypatch.setattr(tasks_module, "_LAUNCH_GRACE_SECONDS", 2.0)
    monkeypatch.setattr(task_runner_module, "_timestamp_age", lambda _value: 2.0)

    signals: list[int] = []
    first_kill = Event()
    release_drain = Event()
    original_claimed_group_is_live = task_runner_module._claimed_group_is_live

    def gated_claimed_group_is_live(pid: int, pgid: int, token: str) -> bool:
        if first_kill.is_set() and not release_drain.is_set():
            return True
        return original_claimed_group_is_live(pid, pgid, token)

    def record_group_signal(
        _pgid: int,
        signal_number: int,
        **_kwargs: object,
    ) -> bool:
        signals.append(signal_number)
        if signal_number == signal.SIGKILL:
            first_kill.set()
        return True

    monkeypatch.setattr(
        task_runner_module,
        "_claimed_group_is_live",
        gated_claimed_group_is_live,
    )
    monkeypatch.setattr(task_runner_module, "_signal_group", record_group_signal)
    runtime = tmp_path / ".aros" / "tasks" / task_id
    final_path = runtime / "final.json"

    with ThreadPoolExecutor(max_workers=2) as pool:
        runner = pool.submit(run_task, tmp_path, task_id)
        running = _wait_state(service, task_id, "running")
        stopper = pool.submit(
            service.stop,
            task_id,
            actor="human",
            reason="single delivery owner",
        )
        try:
            assert first_kill.wait(timeout=5)
            assert signals == [signal.SIGTERM, signal.SIGKILL]
            assert not final_path.exists()
        finally:
            if _process_is_running(int(running["adapter_pid"])):
                os.killpg(int(running["adapter_pgid"]), signal.SIGKILL)
            release_drain.set()
        assert stopper.result(timeout=5)["reason"] == "single delivery owner"
        assert runner.result(timeout=5) == 0

    final = json.loads(final_path.read_text(encoding="utf-8"))
    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert final["state"] == "cancelled"
    assert final["stop"]["signal_sequence"] == ["TERM", "KILL"]


def test_runner_waits_for_paused_external_stop_delivery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, brief = _create_committed_task(
        tmp_path,
        [sys.executable, "-c", "import time;time.sleep(30)"],
        timeout_seconds=30,
        key="paused-external-stop-delivery",
    )
    task_id = str(brief["task_id"])
    original_run = tasks_module.subprocess.run

    monkeypatch.setattr(tasks_module, "_LAUNCH_GRACE_SECONDS", 0.0)
    _fake_absent_tmux_carrier(monkeypatch)
    assert service.start(task_id)["state"] == "lost"
    monkeypatch.setattr(tasks_module.subprocess, "run", original_run)
    monkeypatch.setattr(tasks_module, "_LAUNCH_GRACE_SECONDS", 2.0)

    term_delivered = Event()
    release_delivery = Event()
    runner_delivery_attempt = Event()
    signals: list[int] = []
    deliver_calls = 0
    original_signal_group = task_runner_module._signal_group
    original_deliver_stop = task_runner_module.deliver_stop

    def pause_after_term(
        pgid: int,
        signal_number: int,
        **kwargs: object,
    ) -> bool:
        delivered = original_signal_group(
            pgid,
            signal_number,
            **kwargs,  # type: ignore[arg-type]
        )
        if delivered:
            signals.append(signal_number)
        if delivered and signal_number == signal.SIGTERM:
            term_delivered.set()
            assert release_delivery.wait(timeout=5)
        return delivered

    def observe_deliver_stop(
        delivery_service: TaskService,
        delivery_task_id: str,
    ) -> dict[str, object]:
        nonlocal deliver_calls
        deliver_calls += 1
        if deliver_calls == 2:
            runner_delivery_attempt.set()
        return original_deliver_stop(delivery_service, delivery_task_id)

    monkeypatch.setattr(task_runner_module, "_signal_group", pause_after_term)
    monkeypatch.setattr(task_runner_module, "deliver_stop", observe_deliver_stop)
    final_path = tmp_path / ".aros" / "tasks" / task_id / "final.json"

    with ThreadPoolExecutor(max_workers=2) as pool:
        runner = pool.submit(run_task, tmp_path, task_id)
        _wait_state(service, task_id, "running")
        stopper = pool.submit(
            service.stop,
            task_id,
            actor="human",
            reason="paused external delivery",
        )
        try:
            assert term_delivered.wait(timeout=5)
            assert runner_delivery_attempt.wait(timeout=5)
            assert not final_path.exists()
        finally:
            release_delivery.set()
        assert stopper.result(timeout=5)["reason"] == "paused external delivery"
        assert runner.result(timeout=5) == 0

    final = json.loads(final_path.read_text(encoding="utf-8"))
    assert deliver_calls == 2
    assert signals == [signal.SIGTERM]
    assert final["state"] == "cancelled"
    assert final["stop"]["delivered"] is True
    assert final["stop"]["signal_sequence"] == ["TERM"]


def test_stop_publication_arbitrates_with_runner_finalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code = (
        "import time\n"
        "from pathlib import Path\n"
        "Path('adapter.ready').touch()\n"
        "while not Path('adapter.exit').exists():\n"
        "    time.sleep(0.01)\n"
    )
    service, brief = _create_committed_task(
        tmp_path,
        [sys.executable, "-c", code],
        timeout_seconds=30,
        key="stop-publication-final-race",
    )
    task_id = str(brief["task_id"])
    original_run = tasks_module.subprocess.run

    monkeypatch.setattr(tasks_module, "_LAUNCH_GRACE_SECONDS", 0.0)
    _fake_absent_tmux_carrier(monkeypatch)
    assert service.start(task_id)["state"] == "lost"
    monkeypatch.setattr(tasks_module.subprocess, "run", original_run)
    monkeypatch.setattr(tasks_module, "_LAUNCH_GRACE_SECONDS", 2.0)

    runtime = tmp_path / ".aros" / "tasks" / task_id
    worktree = tmp_path / ".worktree" / "tasks" / task_id
    stop_publication_blocked = Event()
    release_stop_publication = Event()
    arm_runner_lifecycle_pause = Event()
    runner_after_lifecycle_check = Event()
    release_runner_after_check = Event()
    runner_reaped_adapter = Event()
    delivery_paused = Event()
    release_delivery = Event()
    runner_delivery_attempt = Event()
    deliver_calls = 0
    termination_calls = 0
    adapter_pid: list[int] = []
    runner_thread: list[int] = []
    original_create_json = task_runner_module.create_json
    original_file_lock = task_runner_module.file_lock
    original_popen_wait = task_runner_module.subprocess.Popen.wait
    original_deliver_stop = task_runner_module.deliver_stop
    original_monotonic = time.monotonic

    def controlled_monotonic() -> float:
        if runner_thread and get_ident() == runner_thread[0]:
            return 0.0
        return original_monotonic()

    @contextmanager
    def pause_runner_after_lifecycle_check(path: Path) -> object:
        with original_file_lock(path):
            yield
        if (
            arm_runner_lifecycle_pause.is_set()
            and not runner_after_lifecycle_check.is_set()
            and runner_thread
            and get_ident() == runner_thread[0]
            and Path(path) == service._lifecycle_lock_path(task_id)
        ):
            runner_after_lifecycle_check.set()
            assert release_runner_after_check.wait(timeout=5)

    def pause_stop_publication(
        path: Path,
        value: dict[str, object],
        *args: object,
        **kwargs: object,
    ) -> bool:
        if Path(path) == runtime / "stop.json":
            stop_publication_blocked.set()
            assert release_stop_publication.wait(timeout=5)
        return original_create_json(
            path,
            value,
            *args,  # type: ignore[arg-type]
            **kwargs,  # type: ignore[arg-type]
        )

    def observe_adapter_wait(
        process: subprocess.Popen[bytes],
        *args: object,
        **kwargs: object,
    ) -> int:
        result = original_popen_wait(
            process,
            *args,  # type: ignore[arg-type]
            **kwargs,  # type: ignore[arg-type]
        )
        if adapter_pid and process.pid == adapter_pid[0]:
            runner_reaped_adapter.set()
        return result

    def observe_deliver_stop(
        delivery_service: TaskService,
        delivery_task_id: str,
    ) -> dict[str, object]:
        nonlocal deliver_calls
        deliver_calls += 1
        if deliver_calls == 2:
            runner_delivery_attempt.set()
        return original_deliver_stop(delivery_service, delivery_task_id)

    def pause_recorded_delivery(
        _request: dict[str, object],
        *,
        record_signal_sequence: object = None,
        **_kwargs: object,
    ) -> list[str]:
        nonlocal termination_calls
        termination_calls += 1
        delivery_paused.set()
        assert release_delivery.wait(timeout=5)
        sequence = ["TERM"]
        if callable(record_signal_sequence):
            record_signal_sequence(sequence)
        return sequence

    monkeypatch.setattr(task_runner_module, "create_json", pause_stop_publication)
    monkeypatch.setattr(task_runner_module, "file_lock", pause_runner_after_lifecycle_check)
    monkeypatch.setattr(task_runner_module.subprocess.Popen, "wait", observe_adapter_wait)
    monkeypatch.setattr(task_runner_module, "deliver_stop", observe_deliver_stop)
    monkeypatch.setattr(task_runner_module.time, "monotonic", controlled_monotonic)
    monkeypatch.setattr(
        task_runner_module,
        "_terminate_recorded_group",
        pause_recorded_delivery,
    )
    final_path = runtime / "final.json"

    def run_with_controlled_clock() -> int:
        runner_thread.append(get_ident())
        return run_task(tmp_path, task_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        runner = pool.submit(run_with_controlled_clock)
        running = _wait_state(service, task_id, "running")
        adapter_pid.append(int(running["adapter_pid"]))
        ready_path = worktree / "adapter.ready"
        deadline = time.monotonic() + 5
        while not ready_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready_path.exists()
        arm_runner_lifecycle_pause.set()
        assert runner_after_lifecycle_check.wait(timeout=5)
        stopper = pool.submit(
            service.stop,
            task_id,
            actor="human",
            reason="publication arbitration",
        )
        try:
            assert stop_publication_blocked.wait(timeout=5)
            (worktree / "adapter.exit").touch()
            release_runner_after_check.set()
            assert runner_reaped_adapter.wait(timeout=5)
            release_stop_publication.set()
            assert delivery_paused.wait(timeout=5)
            assert runner_delivery_attempt.wait(timeout=5)
            assert not final_path.exists()
        finally:
            release_runner_after_check.set()
            release_stop_publication.set()
            release_delivery.set()
        assert stopper.result(timeout=5)["reason"] == "publication arbitration"
        assert runner.result(timeout=5) == 0

    final = json.loads(final_path.read_text(encoding="utf-8"))
    result = json.loads((runtime / "stop-result.json").read_text(encoding="utf-8"))
    assert deliver_calls == 2
    assert termination_calls == 1
    assert result["delivered"] is True
    assert result["signal_sequence"] == ["TERM"]
    assert final["state"] == "cancelled"
    assert final["stop"] == result


def test_stop_arriving_during_timeout_keeps_timeout_signal_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code = (
        "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(30)"
    )
    service, brief = _create_committed_task(
        tmp_path,
        [sys.executable, "-c", code],
        timeout_seconds=0.2,
        key="stop-during-timeout",
    )
    task_id = str(brief["task_id"])
    original_run = tasks_module.subprocess.run

    monkeypatch.setattr(tasks_module, "_LAUNCH_GRACE_SECONDS", 0.0)
    _fake_absent_tmux_carrier(monkeypatch)
    assert service.start(task_id)["state"] == "lost"
    monkeypatch.setattr(tasks_module.subprocess, "run", original_run)
    monkeypatch.setattr(tasks_module, "_LAUNCH_GRACE_SECONDS", 2.0)
    timeout_entered = Event()
    release_timeout = Event()
    original_terminate = task_runner_module._terminate_group

    def pause_timeout_termination(
        process: subprocess.Popen[bytes],
        **kwargs: object,
    ) -> list[str]:
        timeout_entered.set()
        assert release_timeout.wait(timeout=5)
        return original_terminate(process, **kwargs)  # type: ignore[arg-type]

    def deliver_only_requested_term(
        request: dict[str, object],
        **_kwargs: object,
    ) -> list[str]:
        os.killpg(int(request["adapter_pgid"]), signal.SIGTERM)
        return ["TERM"]

    monkeypatch.setattr(
        task_runner_module,
        "_terminate_group",
        pause_timeout_termination,
    )
    monkeypatch.setattr(
        task_runner_module,
        "_terminate_recorded_group",
        deliver_only_requested_term,
    )

    with ThreadPoolExecutor(max_workers=1) as pool:
        runner = pool.submit(run_task, tmp_path, task_id)
        assert timeout_entered.wait(timeout=5)
        service.stop(task_id, actor="human", reason="late timeout race")
        release_timeout.set()
        assert runner.result(timeout=5) == 0

    final = json.loads(
        (tmp_path / ".aros" / "tasks" / task_id / "final.json").read_text(
            encoding="utf-8"
        )
    )
    assert final["state"] == "timed_out"
    assert final["signal_sequence"] == ["TERM", "KILL"]
    assert final["stop"]["signal_sequence"] == ["TERM"]


def test_attributed_stop_is_idempotent_and_terminates_the_full_process_group(
    tmp_path: Path,
) -> None:
    service, brief = _create_committed_task(
        tmp_path,
        [sys.executable, "-c", _term_ignoring_process_tree_code()],
        timeout_seconds=30,
        key="stop-process-group",
    )
    task_id = str(brief["task_id"])
    running = service.start(task_id)
    if running["state"] != "running":
        running = _wait_state(service, task_id, "running")
    worktree = tmp_path / ".worktree" / "tasks" / task_id
    descendant_path = worktree / "descendant.pid"
    deadline = time.monotonic() + 5
    while not descendant_path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    descendant_pid = int(descendant_path.read_text(encoding="utf-8"))

    with ThreadPoolExecutor(max_workers=2) as pool:
        stops = list(
            pool.map(
                lambda _: service.stop(
                    task_id,
                    actor="human",
                    reason="bounded manual stop",
                    signal_name="TERM",
                ),
                range(2),
            )
        )
    assert stops[0] == stops[1]
    assert stops[0]["actor"] == "human"
    assert stops[0]["reason"] == "bounded manual stop"
    with pytest.raises(TaskError, match="different|conflict"):
        service.stop(
            task_id,
            actor="principal",
            reason="conflicting attribution",
        )

    terminal = _wait_terminal(service, task_id)
    runtime = tmp_path / ".aros" / "tasks" / task_id
    final = json.loads((runtime / "final.json").read_text(encoding="utf-8"))
    assert terminal["state"] == "cancelled"
    assert final["stop"]["actor"] == "human"
    assert final["stop"]["reason"] == "bounded manual stop"
    assert final["stop"]["delivered"] is True
    assert final["signal_sequence"] == ["TERM", "KILL"]
    assert (runtime / "stop.json").is_file()
    _assert_processes_stop(int(final["adapter_pid"]), descendant_pid)


@pytest.mark.parametrize("mismatch", ("host", "token", "pgid"))
def test_stop_refuses_mismatched_process_identity_without_requesting_signal(
    tmp_path: Path,
    mismatch: str,
) -> None:
    service, brief = _create_committed_task(
        tmp_path,
        [sys.executable, "-c", "import time;time.sleep(30)"],
        timeout_seconds=30,
        key=f"stop-mismatch-{mismatch}",
    )
    task_id = str(brief["task_id"])
    running = service.start(task_id)
    if running["state"] != "running":
        running = _wait_state(service, task_id, "running")
    runtime = tmp_path / ".aros" / "tasks" / task_id
    status_path = runtime / "status.json"
    tampered = dict(running)
    if mismatch == "host":
        tampered["host"] = "different-host"
    elif mismatch == "token":
        tampered["adapter_start_token"] = "linux-proc-start:0"
    else:
        tampered["adapter_pgid"] = int(running["adapter_pgid"]) + 1
    atomic_write_json(status_path, tampered)

    with pytest.raises(
        TaskError,
        match="binding|identity|unavailable|refusing|claim|mismatch",
    ):
        service.stop(task_id, actor="human", reason="must refuse")

    assert not (runtime / "stop.json").exists()
    assert _process_is_running(int(running["adapter_pid"]))
    atomic_write_json(status_path, running)
    service.stop(task_id, actor="cleanup", reason="test cleanup")
    assert _wait_terminal(service, task_id)["state"] == "cancelled"


def test_stop_rejects_coherently_forged_live_adapter_identity(
    tmp_path: Path,
) -> None:
    service, brief = _create_committed_task(
        tmp_path,
        [sys.executable, "-c", "import time;time.sleep(30)"],
        timeout_seconds=30,
        key="forged-live-adapter",
    )
    task_id = str(brief["task_id"])
    running = service.start(task_id)
    if running["state"] != "running":
        running = _wait_state(service, task_id, "running")
    victim = subprocess.Popen(
        [sys.executable, "-c", "import time;time.sleep(30)"],
        start_new_session=True,
    )
    victim_token = process_start_token(victim.pid)
    assert victim_token is not None
    runtime = tmp_path / ".aros" / "tasks" / task_id
    status_path = runtime / "status.json"
    forged = {
        **running,
        "adapter_pid": victim.pid,
        "adapter_pgid": victim.pid,
        "adapter_start_token": victim_token,
    }
    atomic_write_json(status_path, forged)

    try:
        with pytest.raises(TaskError, match="adapter|binding|claim|identity"):
            service.stop(task_id, actor="human", reason="must not kill victim")
        assert _process_is_running(victim.pid)
        assert not (runtime / "stop.json").exists()
        atomic_write_json(status_path, running)
        service.stop(task_id, actor="cleanup", reason="test cleanup")
        assert _wait_terminal(service, task_id)["state"] == "cancelled"
    finally:
        if _process_is_running(victim.pid):
            os.killpg(victim.pid, signal.SIGKILL)
        victim.wait(timeout=5)
        if _process_is_running(int(running["adapter_pid"])):
            os.killpg(int(running["adapter_pgid"]), signal.SIGKILL)


def test_public_stop_rejects_non_term_signal(
    tmp_path: Path,
) -> None:
    service, brief = _create_committed_task(
        tmp_path,
        [sys.executable, "-c", "import time;time.sleep(30)"],
        timeout_seconds=30,
        key="reject-kill-stop",
    )
    task_id = str(brief["task_id"])
    running = service.start(task_id)
    if running["state"] != "running":
        running = _wait_state(service, task_id, "running")
    runtime = tmp_path / ".aros" / "tasks" / task_id

    try:
        with pytest.raises(TaskError, match="TERM"):
            service.stop(
                task_id,
                actor="human",
                reason="must use TERM grace",
                signal_name="KILL",
            )
        assert _process_is_running(int(running["adapter_pid"]))
        assert not (runtime / "stop.json").exists()
        service.stop(task_id, actor="cleanup", reason="test cleanup")
        assert _wait_terminal(service, task_id)["state"] == "cancelled"
    finally:
        if _process_is_running(int(running["adapter_pid"])):
            os.killpg(int(running["adapter_pgid"]), signal.SIGKILL)


def test_rehashed_stop_sequence_outside_term_then_kill_fails_closed(
    tmp_path: Path,
) -> None:
    service, brief = _create_committed_task(
        tmp_path,
        [sys.executable, "-c", "import time;time.sleep(30)"],
        timeout_seconds=30,
        key="tampered-stop-sequence",
    )
    task_id = str(brief["task_id"])
    running = service.start(task_id)
    if running["state"] != "running":
        _wait_state(service, task_id, "running")
    service.stop(task_id, actor="human", reason="create stop receipt")
    _wait_terminal(service, task_id)
    runtime = tmp_path / ".aros" / "tasks" / task_id
    result_path = runtime / "stop-result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["signal_sequence"] = ["KILL"]
    result["stop_result_sha256"] = json_sha256(
        {key: value for key, value in result.items() if key != "stop_result_sha256"}
    )
    atomic_write_json(result_path, result)
    final_path = runtime / "final.json"
    final = json.loads(final_path.read_text(encoding="utf-8"))
    final["stop"] = result
    final["signal_sequence"] = ["KILL"]
    final["final_sha256"] = json_sha256(
        {key: value for key, value in final.items() if key != "final_sha256"}
    )
    atomic_write_json(final_path, final)

    with pytest.raises(TaskError, match="signal|sequence|TERM"):
        TaskService(tmp_path).status(task_id)


def test_stop_race_is_not_cancelled_when_no_signal_was_delivered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code = (
        "import time\n"
        "from pathlib import Path\n"
        "Path('adapter.ready').touch()\n"
        "while not Path('adapter.release').exists():\n"
        "    time.sleep(0.01)\n"
    )
    service, brief = _create_committed_task(
        tmp_path,
        [sys.executable, "-c", code],
        timeout_seconds=10,
        key="undelivered-stop-race",
    )
    task_id = str(brief["task_id"])
    runtime = tmp_path / ".aros" / "tasks" / task_id
    worktree = tmp_path / ".worktree" / "tasks" / task_id
    ready_path = worktree / "adapter.ready"
    release_path = worktree / "adapter.release"
    original_run = tasks_module.subprocess.run

    monkeypatch.setattr(tasks_module, "_LAUNCH_GRACE_SECONDS", 0.0)
    _fake_absent_tmux_carrier(monkeypatch)
    assert service.start(task_id)["state"] == "lost"
    monkeypatch.setattr(tasks_module.subprocess, "run", original_run)
    monkeypatch.setattr(tasks_module, "_LAUNCH_GRACE_SECONDS", 2.0)
    monkeypatch.setattr(
        task_runner_module,
        "_terminate_recorded_group",
        lambda _request, **_kwargs: [],
    )

    with ThreadPoolExecutor(max_workers=1) as pool:
        runner = pool.submit(run_task, tmp_path, task_id)
        try:
            running = _wait_state(service, task_id, "running")
            deadline = time.monotonic() + 5
            while not ready_path.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert ready_path.exists()
            service.stop(task_id, actor="human", reason="natural-exit race")
            stop_result = json.loads(
                (runtime / "stop-result.json").read_text(encoding="utf-8")
            )
            assert stop_result["delivered"] is False
            assert stop_result["signal_sequence"] == []
            assert _process_is_running(int(running["adapter_pid"]))
        finally:
            release_path.touch()
        assert runner.result(timeout=5) == 0

    terminal = service.status(task_id)
    final = json.loads(
        (runtime / "final.json").read_text(encoding="utf-8")
    )
    assert terminal["state"] == "completed"
    assert terminal["exit_code"] == 0
    assert final["stop"]["delivered"] is False
    assert final["signal_sequence"] == []
