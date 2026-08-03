"""Durable execution tests for AROS child tasks."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import time
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
    original_run = tasks_module.subprocess.run

    def normalize_before_tmux(
        command: list[str],
        *args: object,
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        if "new-session" in command:
            for name in ("launch.json", "stdout.log", "stderr.log"):
                (runtime / name).chmod(0o666)
        return original_run(command, *args, **kwargs)  # type: ignore[return-value]

    monkeypatch.setattr(tasks_module.subprocess, "run", normalize_before_tmux)

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
    original_run = tasks_module.subprocess.run
    carrier_calls: list[list[str]] = []

    def carrier_without_runner(*args: object, **kwargs: object) -> object:
        command = args[0]
        if isinstance(command, list) and "new-session" in command:
            carrier_calls.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")
        return original_run(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(tasks_module, "_LAUNCH_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(tasks_module.subprocess, "run", carrier_without_runner)

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

    def carrier_without_runner(*args: object, **kwargs: object) -> object:
        command = args[0]
        if isinstance(command, list) and "new-session" in command:
            return subprocess.CompletedProcess(command, 0, "", "")
        return original_run(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(tasks_module, "_LAUNCH_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(tasks_module.subprocess, "run", carrier_without_runner)
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

    def carrier_without_runner(*args: object, **kwargs: object) -> object:
        command = args[0]
        if isinstance(command, list) and "new-session" in command:
            return subprocess.CompletedProcess(command, 0, "", "")
        return original_run(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(tasks_module, "_LAUNCH_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(tasks_module.subprocess, "run", carrier_without_runner)
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


def test_tmux_client_timeout_remains_uncertain_and_never_invents_final(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, brief = _create_committed_task(
        tmp_path,
        [sys.executable, "-c", "print('uncertain carrier')"],
        key="tmux-client-timeout",
    )
    task_id = str(brief["task_id"])
    original_run = tasks_module.subprocess.run

    def timeout_tmux_client(*args: object, **kwargs: object) -> object:
        command = args[0]
        if isinstance(command, list) and "new-session" in command:
            raise subprocess.TimeoutExpired(command, 10)
        return original_run(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(tasks_module, "_LAUNCH_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(tasks_module.subprocess, "run", timeout_tmux_client)

    status = service.start(task_id)

    runtime = tmp_path / ".aros" / "tasks" / task_id
    assert status["state"] == "lost"
    assert (runtime / "launch.json").is_file()
    assert not (runtime / "final.json").exists()


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

    def carrier_without_runner(*args: object, **kwargs: object) -> object:
        command = args[0]
        if isinstance(command, list) and "new-session" in command:
            return subprocess.CompletedProcess(command, 0, "", "")
        return original_run(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(tasks_module, "_LAUNCH_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(tasks_module.subprocess, "run", carrier_without_runner)
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

    def carrier_without_runner(*args: object, **kwargs: object) -> object:
        command = args[0]
        if isinstance(command, list) and "new-session" in command:
            return subprocess.CompletedProcess(command, 0, "", "")
        return original_run(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(tasks_module, "_LAUNCH_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(tasks_module.subprocess, "run", carrier_without_runner)
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

    def carrier_without_runner(*args: object, **kwargs: object) -> object:
        command = args[0]
        if isinstance(command, list) and "new-session" in command:
            return subprocess.CompletedProcess(command, 0, "", "")
        return original_run(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(tasks_module, "_LAUNCH_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(tasks_module.subprocess, "run", carrier_without_runner)
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

    def carrier_without_runner(*args: object, **kwargs: object) -> object:
        command = args[0]
        if isinstance(command, list) and "new-session" in command:
            return subprocess.CompletedProcess(command, 0, "", "")
        return original_run(*args, **kwargs)  # type: ignore[arg-type]

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
    monkeypatch.setattr(tasks_module.subprocess, "run", carrier_without_runner)
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

    def carrier_without_runner(*args: object, **kwargs: object) -> object:
        command = args[0]
        if isinstance(command, list) and "new-session" in command:
            return subprocess.CompletedProcess(command, 0, "", "")
        return original_run(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(tasks_module, "_LAUNCH_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(tasks_module.subprocess, "run", carrier_without_runner)
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

    def carrier_without_runner(*args: object, **kwargs: object) -> object:
        command = args[0]
        if isinstance(command, list) and "new-session" in command:
            return subprocess.CompletedProcess(command, 0, "", "")
        return original_run(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(tasks_module, "_LAUNCH_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(tasks_module.subprocess, "run", carrier_without_runner)
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
    carrier_calls = 0

    def lose_first_carrier(*args: object, **kwargs: object) -> object:
        nonlocal carrier_calls
        command = args[0]
        if isinstance(command, list) and "new-session" in command:
            carrier_calls += 1
            return subprocess.CompletedProcess(command, 0, "", "")
        return original_run(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(tasks_module, "_LAUNCH_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(tasks_module.subprocess, "run", lose_first_carrier)
    assert service.start(task_id)["state"] == "lost"
    monkeypatch.setattr(tasks_module.subprocess, "run", original_run)

    assert run_task(tmp_path, task_id) == 0
    repaired = service.status(task_id)

    assert repaired["state"] == "completed"
    assert carrier_calls == 1
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

    def carrier_without_runner(*args: object, **kwargs: object) -> object:
        command = args[0]
        if isinstance(command, list) and "new-session" in command:
            return subprocess.CompletedProcess(command, 0, "", "")
        return original_run(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(tasks_module, "_LAUNCH_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(tasks_module.subprocess, "run", carrier_without_runner)
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

    def carrier_without_runner(*args: object, **kwargs: object) -> object:
        command = args[0]
        if isinstance(command, list) and "new-session" in command:
            return subprocess.CompletedProcess(command, 0, "", "")
        return original_run(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(tasks_module, "_LAUNCH_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(tasks_module.subprocess, "run", carrier_without_runner)
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

    def carrier_without_runner(*args: object, **kwargs: object) -> object:
        command = args[0]
        if isinstance(command, list) and "new-session" in command:
            return subprocess.CompletedProcess(command, 0, "", "")
        return original_run(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(tasks_module, "_LAUNCH_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(tasks_module.subprocess, "run", carrier_without_runner)
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

    def carrier_without_runner(*args: object, **kwargs: object) -> object:
        command = args[0]
        if isinstance(command, list) and "new-session" in command:
            return subprocess.CompletedProcess(command, 0, "", "")
        return original_run(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(tasks_module, "_LAUNCH_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(tasks_module.subprocess, "run", carrier_without_runner)
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

    def carrier_without_runner(*args: object, **kwargs: object) -> object:
        command = args[0]
        if isinstance(command, list) and "new-session" in command:
            return subprocess.CompletedProcess(command, 0, "", "")
        return original_run(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(tasks_module, "_LAUNCH_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(tasks_module.subprocess, "run", carrier_without_runner)
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

    def carrier_without_runner(*args: object, **kwargs: object) -> object:
        command = args[0]
        if isinstance(command, list) and "new-session" in command:
            return subprocess.CompletedProcess(command, 0, "", "")
        return original_run(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(tasks_module, "_LAUNCH_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(tasks_module.subprocess, "run", carrier_without_runner)
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

    def carrier_without_runner(*args: object, **kwargs: object) -> object:
        command = args[0]
        if isinstance(command, list) and "new-session" in command:
            return subprocess.CompletedProcess(command, 0, "", "")
        return original_run(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(tasks_module, "_LAUNCH_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(tasks_module.subprocess, "run", carrier_without_runner)
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
