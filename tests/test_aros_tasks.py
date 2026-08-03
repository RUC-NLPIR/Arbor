"""Durable child-task record tests for AROS."""

from __future__ import annotations

import errno
import hashlib
import inspect
import json
import math
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from typing import Callable

import pytest

import arbor.aros.tasks as tasks_module
import arbor.aros.worktrees as worktrees_module
from arbor.aros.store import atomic_write_json, create_json, json_sha256
from arbor.aros.tasks import TaskError, TaskService
from arbor.aros.workspace import init_workspace


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _git_ref_exists(root: Path, ref: str) -> bool:
    return subprocess.run(
        ["git", "-C", str(root), "show-ref", "--verify", "--quiet", ref],
        check=False,
    ).returncode == 0


def _init_workspace(root: Path) -> str:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    _git(root, "config", "user.email", "aros@example.invalid")
    _git(root, "config", "user.name", "AROS test")
    (root / "README.md").write_text("# test workspace\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-qm", "initial state")
    init_workspace(root, "Test child task records")
    _git(root, "add", ".gitignore", "AGENTS.md", "AROS.md", "memory/NOW.md")
    _git(root, "commit", "-qm", "initialize AROS")
    return _git(root, "rev-parse", "HEAD")


def _request(*, key: str = "task-key") -> dict[str, object]:
    return {
        "actor": "principal",
        "mode": "write",
        "adapter_argv": ["adapter", "--exact"],
        "capabilities": {"network": False, "shell": True},
        "deliverables": ["result.json"],
        "acceptance": ["python verify.py"],
        "timeout_seconds": 60,
        "idempotency_key": key,
    }


def test_task_git_environment_delegates_to_shared_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = {
        "PATH": "/controlled/bin",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    calls = 0

    def shared_environment() -> dict[str, str]:
        nonlocal calls
        calls += 1
        return dict(expected)

    monkeypatch.setattr(worktrees_module, "_git_environment", shared_environment)

    assert tasks_module._git_environment() == expected
    assert calls == 1


def test_task_worktree_registration_parser_delegates_to_shared_helper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registered = tmp_path / "registered-worktree"
    registered.mkdir()
    expected = {
        "worktree": str(registered),
        "HEAD": "a" * 40,
        "detached": True,
    }
    raw = b"opaque shared parser input"
    observed: list[bytes] = []

    def shared_parser(value: bytes) -> list[dict[str, object]]:
        observed.append(value)
        return [dict(expected)]

    service = TaskService.__new__(TaskService)
    monkeypatch.setattr(service, "_safe_git_bytes", lambda *_args, **_kwargs: raw)
    monkeypatch.setattr(
        worktrees_module,
        "_parse_worktree_registrations",
        shared_parser,
    )

    assert service._worktree_registrations() == [expected]
    assert observed == [raw]


def _normalized_permission_probe(*, device: int = 57) -> dict[str, object]:
    return {
        "requested_mode": 0o600,
        "observed_mode": 0o666,
        "mode_request_supported": False,
        "device": device,
        "enforced": False,
    }


def _create(
    service: TaskService,
    *,
    key: str = "task-key",
    objective: str = "bounded objective",
) -> dict[str, object]:
    return service.create(objective, **_request(key=key))  # type: ignore[arg-type]


def test_service_probes_the_actual_aros_filesystem_permissions(
    tmp_path: Path,
) -> None:
    _init_workspace(tmp_path)
    runtime = tmp_path / ".aros"
    entries_before = set(runtime.iterdir())

    service = TaskService(tmp_path)

    assert service._filesystem_permission_probe == {
        "requested_mode": 0o600,
        "observed_mode": 0o600,
        "mode_request_supported": True,
        "device": runtime.stat().st_dev,
        "enforced": True,
    }
    assert service._filesystem_permissions_enforced is True
    assert set(runtime.iterdir()) == entries_before


def test_service_observes_filesystem_permissions_once_per_instance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_workspace(tmp_path)
    observed_roots: list[Path] = []
    evidence = _normalized_permission_probe()

    def normalized(root: Path) -> dict[str, object]:
        observed_roots.append(root)
        return dict(evidence)

    monkeypatch.setattr(
        tasks_module,
        "_probe_filesystem_permissions",
        normalized,
        raising=False,
    )

    first = TaskService(tmp_path)
    second = TaskService(tmp_path)

    assert observed_roots == [tmp_path / ".aros", tmp_path / ".aros"]
    assert first._filesystem_permission_probe == evidence
    assert second._filesystem_permission_probe == evidence
    assert first._filesystem_permissions_enforced is False
    assert second._filesystem_permissions_enforced is False


@pytest.mark.parametrize(
    "unsupported_errno",
    sorted(
        {
            errno.EOPNOTSUPP,
            getattr(errno, "ENOTSUP", errno.EOPNOTSUPP),
            errno.ENOSYS,
        }
    ),
)
def test_permission_probe_observes_mode_when_fchmod_is_unsupported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unsupported_errno: int,
) -> None:
    _init_workspace(tmp_path)
    runtime = tmp_path / ".aros"
    entries_before = set(runtime.iterdir())
    original_fchmod = tasks_module.os.fchmod

    def unsupported(descriptor: int, _mode: int) -> None:
        original_fchmod(descriptor, 0o666)
        raise OSError(unsupported_errno, os.strerror(unsupported_errno))

    monkeypatch.setattr(tasks_module.os, "fchmod", unsupported)

    evidence = tasks_module._probe_filesystem_permissions(runtime)

    assert evidence == {
        "requested_mode": 0o600,
        "observed_mode": 0o666,
        "mode_request_supported": False,
        "device": runtime.stat().st_dev,
        "enforced": False,
    }
    assert set(runtime.iterdir()) == entries_before


def test_service_create_accepts_unsupported_fchmod_with_unchanged_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_workspace(tmp_path)
    runtime = tmp_path / ".aros"

    def unsupported(_descriptor: int, _mode: int) -> None:
        raise OSError(errno.EOPNOTSUPP, os.strerror(errno.EOPNOTSUPP))

    monkeypatch.setattr(tasks_module.os, "fchmod", unsupported)

    service = TaskService(tmp_path)
    brief = _create(service, key="unsupported-unchanged-mode")

    assert service._filesystem_permission_probe == {
        "requested_mode": 0o600,
        "observed_mode": 0o600,
        "mode_request_supported": False,
        "device": runtime.stat().st_dev,
        "enforced": False,
    }
    assert service._filesystem_permissions_enforced is False
    assert service.status(str(brief["task_id"]))["state"] == "prepared"
    assert not list(runtime.glob(".task-permission-probe-*"))


def test_permission_probe_rejects_other_fchmod_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_workspace(tmp_path)
    runtime = tmp_path / ".aros"
    entries_before = set(runtime.iterdir())

    def denied(_descriptor: int, _mode: int) -> None:
        raise OSError(errno.EPERM, os.strerror(errno.EPERM))

    monkeypatch.setattr(tasks_module.os, "fchmod", denied)

    with pytest.raises(TaskError, match="observe filesystem permissions"):
        tasks_module._probe_filesystem_permissions(runtime)

    assert set(runtime.iterdir()) == entries_before


def _request_from_brief(brief: dict[str, object]) -> dict[str, object]:
    return {
        field: brief[field]
        for field in (
            "objective",
            "actor",
            "mode",
            "adapter_argv",
            "capabilities",
            "deliverables",
            "acceptance",
            "timeout_seconds",
            "idempotency_key",
        )
    }


def _rehash_brief(brief: dict[str, object]) -> None:
    brief["brief_sha256"] = json_sha256(
        {key: value for key, value in brief.items() if key != "brief_sha256"}
    )


def _commit_brief(root: Path, brief: dict[str, object]) -> str:
    task_id = str(brief["task_id"])
    _git(root, "add", f"tasks/{task_id}/brief.json")
    _git(root, "commit", "-qm", f"record {task_id}")
    return _git(root, "rev-parse", "HEAD")


def _fake_tmux_carrier(
    monkeypatch: pytest.MonkeyPatch,
) -> list[list[str]]:
    original_run = tasks_module.subprocess.run
    carrier_calls: list[list[str]] = []

    def run(command: list[str], *args: object, **kwargs: object) -> object:
        if "new-session" in command:
            carrier_calls.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")
        return original_run(command, *args, **kwargs)

    monkeypatch.setattr(tasks_module.shutil, "which", lambda _name: "/fake/tmux")
    monkeypatch.setattr(tasks_module, "_LAUNCH_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(tasks_module.subprocess, "run", run)
    return carrier_calls


def _create_committed_task(
    root: Path,
    *,
    key: str,
) -> tuple[TaskService, dict[str, object], str, Path]:
    _init_workspace(root)
    service = TaskService(root)
    brief = _create(service, key=key)
    task_id = str(brief["task_id"])
    _commit_brief(root, brief)
    return service, brief, task_id, root / ".aros" / "tasks" / task_id


def _publish_preparation_intent(
    service: TaskService,
    task_id: str,
    runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service._ensure_worktree(task_id, actor="principal")

    def stop_after_intent(_runtime: Path, **_kwargs: object) -> None:
        raise RuntimeError("stop after preparation intent")

    with monkeypatch.context() as preparation_context:
        preparation_context.setattr(
            tasks_module.shutil,
            "which",
            lambda _name: "/fake/tmux",
        )
        preparation_context.setattr(
            service,
            "_prepare_execution_paths",
            stop_after_intent,
        )
        with pytest.raises(RuntimeError, match="after preparation"):
            service.start(task_id, actor="principal")
    assert (runtime / "preparation.json").is_file()
    assert not (runtime / "launch.json").exists()


def _create_terminal_task(
    root: Path,
    *,
    state: str = "completed",
    mode: str = "write",
) -> tuple[
    TaskService,
    dict[str, object],
    dict[str, object],
    dict[str, object],
]:
    _init_workspace(root)
    service = TaskService(root)
    request = _request()
    request["mode"] = mode
    brief = service.create("bounded objective", **request)  # type: ignore[arg-type]
    _commit_brief(root, brief)
    task_id = str(brief["task_id"])
    service._ensure_worktree(task_id)
    runtime = root / ".aros" / "tasks" / task_id
    ownership = json.loads((runtime / "ownership.json").read_text(encoding="utf-8"))
    timestamp = str(ownership["acquired_at"])
    launch: dict[str, object] = {
        "schema_version": 1,
        "task_id": task_id,
        "actor": ownership["actor"],
        "brief_sha256": brief["brief_sha256"],
        "ownership_sha256": ownership["ownership_sha256"],
        "base_commit": brief["base_commit"],
        "security_profile": "trusted-local",
        "isolation_scope": "application",
        "capabilities_enforced": False,
        "filesystem_permissions_enforced": (
            service._filesystem_permissions_enforced
        ),
        "filesystem_permission_probe": service._filesystem_permission_probe,
        "carrier": "tmux",
        "tmux_session": f"aros-task-{task_id.lower()}",
        "tmux_socket": tasks_module._tmux_socket_name(root, task_id),
        "host": tasks_module.socket.gethostname(),
        "runner_version": 1,
        "runner_cwd": str(runtime / "home"),
        "runner_invocation": [
            sys.executable,
            "-I",
            "-c",
            tasks_module._TASK_RUNNER_BOOTSTRAP,
            str(runtime / "runner-import"),
            "--workspace",
            str(root),
            "--task-id",
            task_id,
        ],
        "launched_at": timestamp,
    }
    launch["launch_sha256"] = json_sha256(launch)
    assert create_json(runtime / "launch.json", launch)
    for name in ("stdout.log", "stderr.log"):
        descriptor = os.open(
            runtime / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        os.close(descriptor)
    empty_sha256 = hashlib.sha256(b"").hexdigest()
    final: dict[str, object] = {
        "schema_version": 1,
        "task_id": task_id,
        "state": state,
        "brief_sha256": brief["brief_sha256"],
        "ownership_sha256": ownership["ownership_sha256"],
        "launch_sha256": launch["launch_sha256"],
        "security_profile": "trusted-local",
        "isolation_scope": "application",
        "capabilities_enforced": False,
        "filesystem_permissions_enforced": launch[
            "filesystem_permissions_enforced"
        ],
        "filesystem_permission_probe": launch["filesystem_permission_probe"],
        "host": launch["host"],
        "runner_pid": None,
        "runner_pgid": None,
        "runner_start_token": None,
        "adapter_pid": None,
        "adapter_pgid": None,
        "adapter_start_token": None,
        "started_at": timestamp,
        "finished_at": timestamp,
        "duration_seconds": 0,
        "exit_code": 0 if state in {"completed", "timed_out"} else 1,
        "timeout": {
            "timeout_seconds": brief["timeout_seconds"],
            "triggered": state == "timed_out",
        },
        "stop": None,
        "signal_sequence": ["TERM"] if state == "timed_out" else [],
        "stdout": {
            "path": f".aros/tasks/{task_id}/stdout.log",
            "bytes": 0,
            "sha256": empty_sha256,
        },
        "stderr": {
            "path": f".aros/tasks/{task_id}/stderr.log",
            "bytes": 0,
            "sha256": empty_sha256,
        },
        "error": None,
    }
    final["final_sha256"] = json_sha256(final)
    assert create_json(runtime / "final.json", final)
    assert service.status(task_id)["state"] == state
    return service, brief, ownership, final


def _commit_child_return(
    root: Path,
    brief: dict[str, object],
    ownership: dict[str, object],
    *,
    changed_files: list[str] | None = None,
) -> tuple[dict[str, object], str, str]:
    task_id = str(brief["task_id"])
    worktree = Path(str(ownership["worktree_path"]))
    changes = ["alpha.txt", "zeta.txt"] if changed_files is None else changed_files
    for relative in reversed(changes):
        path = worktree / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"artifact {relative}\n", encoding="utf-8")
    if changes:
        _git(worktree, "add", "--", *changes)
        _git(worktree, "commit", "-qm", "produce child artifacts")
    child_commit = _git(worktree, "rev-parse", "HEAD")
    returned: dict[str, object] = {
        "schema_version": 1,
        "task_id": task_id,
        "brief_sha256": brief["brief_sha256"],
        "base_commit": brief["base_commit"],
        "child_commit": child_commit,
        "summary": "Produced deterministic child artifacts.",
        "work_performed": ["wrote exact artifact files"],
        "changed_files": sorted(changes, key=lambda item: item.encode("utf-8")),
        "evidence": ["child commit records the artifact bytes"],
        "deviations": [],
        "uncertainty": [],
        "follow_up": ["principal should inspect before assimilation"],
    }
    returned["return_sha256"] = json_sha256(returned)
    return_path = worktree / "tasks" / task_id / "return.json"
    return_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(return_path, returned)
    _git(worktree, "add", "--", f"tasks/{task_id}/return.json")
    _git(worktree, "commit", "-qm", "record child return")
    return_commit = _git(worktree, "rev-parse", "HEAD")
    return returned, child_commit, return_commit


@pytest.mark.parametrize("mode", ("read_only", "write"))
def test_start_prepares_a_branch_attached_owned_worktree_without_execution(
    tmp_path: Path,
    mode: str,
) -> None:
    base_commit = _init_workspace(tmp_path)
    marker = tmp_path / "adapter-ran"
    service = TaskService(tmp_path)
    request = _request(key=f"start-{mode}")
    request["mode"] = mode
    request["adapter_argv"] = [
        sys.executable,
        "-c",
        f"from pathlib import Path; Path({str(marker)!r}).touch()",
    ]
    brief = service.create("prepare isolated child", **request)  # type: ignore[arg-type]
    task_id = str(brief["task_id"])
    parent_head = _commit_brief(tmp_path, brief)
    worktree = (tmp_path / ".worktree" / "tasks" / task_id).absolute()
    branch = f"aros/task/{task_id}"

    status = service._ensure_worktree(task_id, actor="delegate-principal")

    ownership_path = tmp_path / ".aros" / "tasks" / task_id / "ownership.json"
    ownership = json.loads(ownership_path.read_text(encoding="utf-8"))
    assert set(ownership) == {
        "schema_version",
        "task_id",
        "brief_sha256",
        "actor",
        "worktree_path",
        "branch",
        "base_commit",
        "parent_head",
        "acquired_at",
        "ownership_sha256",
    }
    assert ownership == {
        **ownership,
        "schema_version": 1,
        "task_id": task_id,
        "brief_sha256": brief["brief_sha256"],
        "actor": "delegate-principal",
        "worktree_path": str(worktree),
        "branch": branch,
        "base_commit": base_commit,
        "parent_head": parent_head,
        "ownership_sha256": json_sha256(
            {
                key: value
                for key, value in ownership.items()
                if key != "ownership_sha256"
            }
        ),
    }
    assert status == {
        "schema_version": 1,
        "task_id": task_id,
        "state": "worktree_ready",
        "brief_sha256": brief["brief_sha256"],
        "ownership_sha256": ownership["ownership_sha256"],
        "updated_at": ownership["acquired_at"],
    }
    assert service.status(task_id) == status
    assert service.list() == [status]
    assert worktree.is_dir()
    assert Path(_git(worktree, "rev-parse", "--show-toplevel")) == worktree
    assert _git(worktree, "branch", "--show-current") == branch
    assert _git(worktree, "rev-parse", "HEAD") == base_commit
    assert _git(tmp_path, "rev-parse", "HEAD") == parent_head
    assert not marker.exists()


@pytest.mark.parametrize(
    "checkpoint",
    ("home", "tmp", "import_alias", "stdout", "stderr"),
)
def test_start_resumes_owned_path_preparation_after_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    checkpoint: str,
) -> None:
    service, brief, task_id, runtime = _create_committed_task(
        tmp_path,
        key=f"preparation-crash-{checkpoint}",
    )
    service._ensure_worktree(task_id, actor="principal")
    ownership = json.loads(
        (runtime / "ownership.json").read_text(encoding="utf-8")
    )
    original_fsync_directory = tasks_module._fsync_directory
    crashed = False

    def crash_after_checkpoint(path: Path) -> None:
        nonlocal crashed
        original_fsync_directory(path)
        reached = (
            checkpoint == "home"
            and path == runtime
            and (runtime / "home").is_dir()
            and not (runtime / "tmp").exists()
        ) or (
            checkpoint == "tmp"
            and path == runtime
            and (runtime / "tmp").is_dir()
            and not (runtime / "runner-import").exists()
        ) or (
            checkpoint == "import_alias"
            and path == runtime / "runner-import"
            and (runtime / "runner-import" / "arbor").is_symlink()
        ) or (
            checkpoint == "stdout"
            and path == runtime
            and (runtime / "stdout.log").is_file()
            and not (runtime / "stderr.log").exists()
        ) or (
            checkpoint == "stderr"
            and path == runtime
            and (runtime / "stderr.log").is_file()
            and not (runtime / "launch.json").exists()
        )
        if reached and not crashed:
            crashed = True
            raise RuntimeError(f"injected crash after {checkpoint}")

    with monkeypatch.context() as crash_context:
        crash_context.setattr(
            tasks_module,
            "_fsync_directory",
            crash_after_checkpoint,
        )
        crash_context.setattr(
            tasks_module.shutil,
            "which",
            lambda _name: "/fake/tmux",
        )
        with pytest.raises(RuntimeError, match=f"after {checkpoint}"):
            service.start(task_id, actor="principal")

    assert crashed
    preparation_path = runtime / "preparation.json"
    preparation = json.loads(preparation_path.read_text(encoding="utf-8"))
    assert set(preparation) == {
        "schema_version",
        "task_id",
        "brief_sha256",
        "ownership_sha256",
        "actor",
        "filesystem_permissions_enforced",
        "filesystem_permission_probe",
        "paths",
        "prepared_at",
        "preparation_sha256",
    }
    assert preparation == {
        **preparation,
        "schema_version": 1,
        "task_id": task_id,
        "brief_sha256": brief["brief_sha256"],
        "ownership_sha256": ownership["ownership_sha256"],
        "actor": "principal",
        "filesystem_permissions_enforced": (
            service._filesystem_permissions_enforced
        ),
        "filesystem_permission_probe": service._filesystem_permission_probe,
        "paths": {
            "home": f".aros/tasks/{task_id}/home",
            "tmp": f".aros/tasks/{task_id}/tmp",
            "runner_import": f".aros/tasks/{task_id}/runner-import",
            "stdout": f".aros/tasks/{task_id}/stdout.log",
            "stderr": f".aros/tasks/{task_id}/stderr.log",
        },
        "preparation_sha256": json_sha256(
            {
                key: value
                for key, value in preparation.items()
                if key != "preparation_sha256"
            }
        ),
    }
    assert not (runtime / "launch.json").exists()

    carrier_calls = _fake_tmux_carrier(monkeypatch)
    restarted = TaskService(tmp_path)

    assert restarted.start(task_id, actor="principal")["state"] == "lost"
    assert len(carrier_calls) == 1
    assert json.loads(preparation_path.read_text(encoding="utf-8")) == preparation
    assert (runtime / "launch.json").is_file()
    assert not (runtime / "final.json").exists()


@pytest.mark.parametrize("preseed", ("path", "symlink", "nonempty"))
def test_start_rejects_preseeded_paths_without_publishing_preparation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    preseed: str,
) -> None:
    service, _brief, task_id, runtime = _create_committed_task(
        tmp_path,
        key=f"preparation-preseed-{preseed}",
    )
    outside = tmp_path / ".git" / "preparation-outside"
    if preseed == "path":
        seeded = runtime / "home"
        seeded.mkdir()
    elif preseed == "symlink":
        outside.mkdir()
        (outside / "preserve.txt").write_text("preserve\n", encoding="utf-8")
        seeded = runtime / "tmp"
        seeded.symlink_to(outside, target_is_directory=True)
    else:
        seeded = runtime / "stdout.log"
        seeded.write_bytes(b"preserve pre-launch bytes\n")
    carrier_calls = _fake_tmux_carrier(monkeypatch)

    with pytest.raises(TaskError, match="conflict|exist|symlink|preparation"):
        service.start(task_id, actor="principal")

    assert seeded.exists()
    if preseed == "nonempty":
        assert seeded.read_bytes() == b"preserve pre-launch bytes\n"
    elif preseed == "symlink":
        assert seeded.is_symlink()
        assert (outside / "preserve.txt").read_text(encoding="utf-8") == "preserve\n"
    assert not (runtime / "preparation.json").exists()
    assert not (runtime / "launch.json").exists()
    assert carrier_calls == []


@pytest.mark.parametrize(
    "problem",
    ("schema", "probe", "mode", "hardlink", "symlink"),
)
def test_start_rejects_tampered_or_linked_preparation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    problem: str,
) -> None:
    service, _brief, task_id, runtime = _create_committed_task(
        tmp_path,
        key=f"invalid-preparation-{problem}",
    )
    _publish_preparation_intent(service, task_id, runtime, monkeypatch)

    preparation_path = runtime / "preparation.json"
    outside = tmp_path / ".git" / f"preparation-{problem}"
    if problem in {"schema", "probe"}:
        preparation = json.loads(preparation_path.read_text(encoding="utf-8"))
        if problem == "schema":
            preparation["unexpected"] = True
        else:
            probe = preparation["filesystem_permission_probe"]
            assert isinstance(probe, dict)
            probe["device"] = int(probe["device"]) + 1
        preparation["preparation_sha256"] = json_sha256(
            {
                key: value
                for key, value in preparation.items()
                if key != "preparation_sha256"
            }
        )
        atomic_write_json(preparation_path, preparation)
    elif problem == "mode":
        preparation_path.chmod(0o666)
    elif problem == "hardlink":
        os.link(preparation_path, outside)
    else:
        preparation_path.replace(outside)
        preparation_path.symlink_to(outside)
    carrier_calls = _fake_tmux_carrier(monkeypatch)

    with pytest.raises(
        TaskError,
        match="preparation|restrictive|plain|link|permission|schema|binding",
    ):
        TaskService(tmp_path).start(task_id, actor="principal")

    assert not (runtime / "launch.json").exists()
    assert carrier_calls == []
    if problem == "hardlink":
        assert preparation_path.stat().st_nlink == outside.stat().st_nlink == 2
    elif problem == "symlink":
        assert preparation_path.is_symlink()
        assert outside.is_file()


def test_start_recovers_interrupted_preparation_temp_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _brief, task_id, runtime = _create_committed_task(
        tmp_path,
        key="recover-preparation-temp-alias",
    )
    _publish_preparation_intent(service, task_id, runtime, monkeypatch)
    preparation_path = runtime / "preparation.json"
    digest = hashlib.sha256(os.fsencode(preparation_path.name)).hexdigest()
    temporary = runtime / f".aros-json-{digest}.crash.tmp"
    os.link(preparation_path, temporary, follow_symlinks=False)
    identity = (preparation_path.stat().st_dev, preparation_path.stat().st_ino)
    assert preparation_path.stat().st_nlink == 2
    assert (temporary.stat().st_dev, temporary.stat().st_ino) == identity
    carrier_calls = _fake_tmux_carrier(monkeypatch)

    assert TaskService(tmp_path).start(task_id, actor="principal")["state"] == "lost"

    assert not temporary.exists()
    assert preparation_path.stat().st_nlink == 1
    assert (preparation_path.stat().st_dev, preparation_path.stat().st_ino) == identity
    assert (runtime / "launch.json").is_file()
    assert len(carrier_calls) == 1


@pytest.mark.parametrize("name", ("home", "tmp", "runner-import"))
def test_start_rejects_permissive_directory_during_preparation_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    service, _brief, task_id, runtime = _create_committed_task(
        tmp_path,
        key=f"permissive-preparation-{name}",
    )
    _publish_preparation_intent(service, task_id, runtime, monkeypatch)

    directory = runtime / name
    directory.mkdir(mode=0o700)
    directory.chmod(0o777)
    carrier_calls = _fake_tmux_carrier(monkeypatch)

    with pytest.raises(TaskError, match="directory|mode|restrictive|preparation"):
        TaskService(tmp_path).start(task_id, actor="principal")

    assert stat.S_IMODE(directory.lstat().st_mode) == 0o777
    assert not (runtime / "launch.json").exists()
    assert carrier_calls == []


def test_start_rejects_directory_replacement_during_preparation_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _brief, task_id, runtime = _create_committed_task(
        tmp_path,
        key="replaced-preparation-directory",
    )
    _publish_preparation_intent(service, task_id, runtime, monkeypatch)

    home = runtime / "home"
    displaced = runtime / "displaced-home"
    home.mkdir(mode=0o700)
    original_iterdir = Path.iterdir
    replaced = False

    def replace_before_iterdir(path: Path):  # type: ignore[no-untyped-def]
        nonlocal replaced
        if path == home and not replaced:
            home.rename(displaced)
            home.mkdir(mode=0o700)
            replaced = True
        return original_iterdir(path)

    carrier_calls = _fake_tmux_carrier(monkeypatch)
    monkeypatch.setattr(Path, "iterdir", replace_before_iterdir)

    with pytest.raises(TaskError, match="directory|identity|changed|preparation"):
        TaskService(tmp_path).start(task_id, actor="principal")

    assert replaced
    assert displaced.is_dir()
    assert not (runtime / "launch.json").exists()
    assert carrier_calls == []


def test_start_rejects_directory_entry_added_during_preparation_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _brief, task_id, runtime = _create_committed_task(
        tmp_path,
        key="changed-preparation-directory-entries",
    )
    _publish_preparation_intent(service, task_id, runtime, monkeypatch)

    home = runtime / "home"
    home.mkdir(mode=0o700)
    injected = home / "injected"
    original_fsync_directory = tasks_module._fsync_directory

    def inject_after_parent_sync(path: Path) -> None:
        original_fsync_directory(path)
        if path == runtime and not injected.exists():
            injected.write_text("hostile\n", encoding="utf-8")

    monkeypatch.setattr(
        tasks_module,
        "_fsync_directory",
        inject_after_parent_sync,
    )
    carrier_calls = _fake_tmux_carrier(monkeypatch)

    with pytest.raises(TaskError, match="directory|entries|empty|changed"):
        TaskService(tmp_path).start(task_id, actor="principal")

    assert injected.read_text(encoding="utf-8") == "hostile\n"
    assert not (runtime / "launch.json").exists()
    assert carrier_calls == []


def test_start_resyncs_import_alias_after_crash_before_first_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _brief, task_id, runtime = _create_committed_task(
        tmp_path,
        key="preparation-alias-presync-crash",
    )
    service._ensure_worktree(task_id, actor="principal")
    import_root = runtime / "runner-import"
    alias = import_root / "arbor"
    original_fsync_directory = tasks_module._fsync_directory
    crashed = False

    def crash_before_alias_sync(path: Path) -> None:
        nonlocal crashed
        if path == import_root and alias.is_symlink() and not crashed:
            crashed = True
            raise RuntimeError("injected crash before alias sync")
        original_fsync_directory(path)

    with monkeypatch.context() as crash_context:
        crash_context.setattr(
            tasks_module,
            "_fsync_directory",
            crash_before_alias_sync,
        )
        crash_context.setattr(
            tasks_module.shutil,
            "which",
            lambda _name: "/fake/tmux",
        )
        with pytest.raises(RuntimeError, match="before alias sync"):
            service.start(task_id, actor="principal")

    assert crashed
    assert alias.is_symlink()
    assert not (runtime / "launch.json").exists()
    synced: list[Path] = []

    def record_sync(path: Path) -> None:
        original_fsync_directory(path)
        synced.append(path)

    monkeypatch.setattr(tasks_module, "_fsync_directory", record_sync)
    carrier_calls = _fake_tmux_carrier(monkeypatch)

    assert TaskService(tmp_path).start(task_id, actor="principal")["state"] == "lost"
    assert import_root in synced
    assert len(carrier_calls) == 1


@pytest.mark.parametrize(
    ("log_name", "replacement_stage"),
    (
        ("stdout.log", "file_sync"),
        ("stdout.log", "parent_sync"),
        ("stderr.log", "file_sync"),
        ("stderr.log", "parent_sync"),
    ),
)
def test_start_rejects_log_replacement_during_preparation_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    log_name: str,
    replacement_stage: str,
) -> None:
    service, _brief, task_id, runtime = _create_committed_task(
        tmp_path,
        key=f"replaced-{log_name}-{replacement_stage}",
    )
    service._ensure_worktree(task_id, actor="principal")
    original_fsync_directory = tasks_module._fsync_directory
    crashed = False

    def crash_after_stderr(path: Path) -> None:
        nonlocal crashed
        original_fsync_directory(path)
        if (
            path == runtime
            and (runtime / "stderr.log").is_file()
            and not (runtime / "launch.json").exists()
            and not crashed
        ):
            crashed = True
            raise RuntimeError("injected crash after stderr")

    with monkeypatch.context() as crash_context:
        crash_context.setattr(
            tasks_module,
            "_fsync_directory",
            crash_after_stderr,
        )
        crash_context.setattr(
            tasks_module.shutil,
            "which",
            lambda _name: "/fake/tmux",
        )
        with pytest.raises(RuntimeError, match="after stderr"):
            service.start(task_id, actor="principal")

    assert crashed
    selected = runtime / log_name
    selected_metadata = selected.lstat()
    selected_identity = (selected_metadata.st_dev, selected_metadata.st_ino)
    displaced = runtime / f"{log_name}.{replacement_stage}.original"
    original_fsync = tasks_module.os.fsync
    selected_synced = False
    replaced = False

    def replace_selected() -> None:
        nonlocal replaced
        selected.rename(displaced)
        descriptor = os.open(
            selected,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        os.close(descriptor)
        replaced = True

    def sync_and_maybe_replace(descriptor: int) -> None:
        nonlocal selected_synced
        original_fsync(descriptor)
        metadata = os.fstat(descriptor)
        if (metadata.st_dev, metadata.st_ino) != selected_identity:
            return
        selected_synced = True
        if replacement_stage == "file_sync" and not replaced:
            replace_selected()

    def sync_parent_and_maybe_replace(path: Path) -> None:
        original_fsync_directory(path)
        if (
            replacement_stage == "parent_sync"
            and path == runtime
            and selected_synced
            and not replaced
        ):
            replace_selected()

    monkeypatch.setattr(tasks_module.os, "fsync", sync_and_maybe_replace)
    monkeypatch.setattr(
        tasks_module,
        "_fsync_directory",
        sync_parent_and_maybe_replace,
    )
    carrier_calls = _fake_tmux_carrier(monkeypatch)

    with pytest.raises(TaskError, match="log|identity|changed|restrictive"):
        TaskService(tmp_path).start(task_id, actor="principal")

    assert replaced
    assert displaced.is_file()
    assert not (runtime / "launch.json").exists()
    assert carrier_calls == []


def test_concurrent_starts_share_one_preparation_and_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _brief, task_id, runtime = _create_committed_task(
        tmp_path,
        key="concurrent-preparation",
    )
    carrier_calls = _fake_tmux_carrier(monkeypatch)

    with ThreadPoolExecutor(max_workers=4) as pool:
        starts = list(
            pool.map(
                lambda _number: service.start(task_id, actor="principal"),
                range(4),
            )
        )

    preparation_path = runtime / "preparation.json"
    launch_path = runtime / "launch.json"
    preparation = json.loads(preparation_path.read_text(encoding="utf-8"))
    launch = json.loads(launch_path.read_text(encoding="utf-8"))
    assert all(start["state"] == "lost" for start in starts)
    assert all(start["launch_sha256"] == launch["launch_sha256"] for start in starts)
    assert preparation_path.lstat().st_nlink == 1
    assert preparation["preparation_sha256"] == json_sha256(
        {
            key: value
            for key, value in preparation.items()
            if key != "preparation_sha256"
        }
    )
    assert len(list(runtime.glob("preparation.json"))) == 1
    assert len(list(runtime.glob("launch.json"))) == 1
    assert len(carrier_calls) == 1


def test_launch_evidence_prevents_automatic_carrier_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _brief, task_id, runtime = _create_committed_task(
        tmp_path,
        key="preparation-no-relaunch",
    )
    carrier_calls = _fake_tmux_carrier(monkeypatch)

    first = service.start(task_id, actor="principal")
    preparation_path = runtime / "preparation.json"
    preparation_identity = (
        preparation_path.lstat().st_dev,
        preparation_path.lstat().st_ino,
    )
    second = TaskService(tmp_path).start(task_id, actor="principal")

    assert first["state"] == second["state"] == "lost"
    assert first["launch_sha256"] == second["launch_sha256"]
    assert len(carrier_calls) == 1
    assert (
        preparation_path.lstat().st_dev,
        preparation_path.lstat().st_ino,
    ) == preparation_identity


@pytest.mark.parametrize("dirty_kind", ("unstaged", "staged", "untracked"))
def test_start_rejects_and_preserves_a_dirty_parent_without_allocating(
    tmp_path: Path,
    dirty_kind: str,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service, key=f"dirty-parent-{dirty_kind}")
    task_id = str(brief["task_id"])
    _commit_brief(tmp_path, brief)
    dirty = tmp_path / ("README.md" if dirty_kind != "untracked" else "untracked.txt")
    dirty.write_text(f"preserve {dirty_kind}\n", encoding="utf-8")
    if dirty_kind == "staged":
        _git(tmp_path, "add", "README.md")

    with pytest.raises(TaskError, match="clean|dirty"):
        service._ensure_worktree(task_id)

    assert dirty.read_text(encoding="utf-8") == f"preserve {dirty_kind}\n"
    assert not (tmp_path / ".worktree" / "tasks" / task_id).exists()
    assert not _git_ref_exists(tmp_path, f"refs/heads/aros/task/{task_id}")


def test_start_requires_the_brief_to_be_committed_at_current_head(
    tmp_path: Path,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service, key="uncommitted-brief")
    task_id = str(brief["task_id"])

    with pytest.raises(TaskError, match="committed|clean"):
        service._ensure_worktree(task_id)

    assert (tmp_path / "tasks" / task_id / "brief.json").is_file()
    assert not (tmp_path / ".worktree" / "tasks" / task_id).exists()


def test_start_compares_committed_and_working_brief_bytes_even_if_index_hides_change(
    tmp_path: Path,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service, key="hidden-brief-mismatch")
    task_id = str(brief["task_id"])
    _commit_brief(tmp_path, brief)
    brief_path = tmp_path / "tasks" / task_id / "brief.json"
    _git(tmp_path, "update-index", "--assume-unchanged", f"tasks/{task_id}/brief.json")
    original = brief_path.read_bytes()
    brief_path.write_bytes(original + b" ")
    assert _git(tmp_path, "status", "--porcelain") == ""

    with pytest.raises(TaskError, match="committed brief|bytes|mismatch"):
        service._ensure_worktree(task_id)

    assert brief_path.read_bytes() == original + b" "
    assert not (tmp_path / ".worktree" / "tasks" / task_id).exists()


@pytest.mark.parametrize("index_flag", ("--assume-unchanged", "--skip-worktree"))
def test_start_rejects_parent_changes_hidden_by_index_flags(
    tmp_path: Path,
    index_flag: str,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service, key=f"hidden-parent-change-{index_flag}")
    task_id = str(brief["task_id"])
    _commit_brief(tmp_path, brief)
    _git(tmp_path, "update-index", index_flag, "README.md")
    readme = tmp_path / "README.md"
    readme.write_text("hidden parent change\n", encoding="utf-8")
    assert _git(tmp_path, "status", "--porcelain") == ""

    with pytest.raises(TaskError, match="clean|index|ambiguous"):
        service._ensure_worktree(task_id)

    assert readme.read_text(encoding="utf-8") == "hidden parent change\n"
    assert not (tmp_path / ".worktree" / "tasks" / task_id).exists()


def test_start_requires_brief_base_commit_to_ancestor_current_head(
    tmp_path: Path,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service, key="unrelated-base")
    task_id = str(brief["task_id"])
    tree = _git(tmp_path, "show", "-s", "--format=%T", "HEAD")
    unrelated = _git(tmp_path, "commit-tree", tree, "-m", "unrelated root")
    brief["base_commit"] = unrelated
    _rehash_brief(brief)
    brief_path = tmp_path / "tasks" / task_id / "brief.json"
    status_path = tmp_path / ".aros" / "tasks" / task_id / "status.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    status["brief_sha256"] = brief["brief_sha256"]
    index_path = next((tmp_path / ".aros" / "tasks" / "idempotency").iterdir())
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["brief_sha256"] = brief["brief_sha256"]
    atomic_write_json(brief_path, brief)
    atomic_write_json(status_path, status)
    atomic_write_json(index_path, index)
    _commit_brief(tmp_path, brief)

    with pytest.raises(TaskError, match="ancestor|base commit"):
        service._ensure_worktree(task_id)

    assert not (tmp_path / ".worktree" / "tasks" / task_id).exists()


def test_start_ignores_git_replacement_refs_for_base_and_checkout_bytes(
    tmp_path: Path,
) -> None:
    base_commit = _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service, key="replacement-ref")
    task_id = str(brief["task_id"])
    _commit_brief(tmp_path, brief)
    builder = tmp_path / ".worktree" / "replacement-builder"
    _git(
        tmp_path,
        "worktree",
        "add",
        "-q",
        "-b",
        "replacement-builder",
        str(builder),
        f"{base_commit}^",
    )
    (builder / "README.md").write_text("replacement bytes\n", encoding="utf-8")
    _git(builder, "add", "README.md")
    _git(builder, "commit", "-qm", "replacement commit")
    replacement = _git(builder, "rev-parse", "HEAD")
    _git(tmp_path, "replace", base_commit, replacement)
    exact = subprocess.run(
        [
            "git",
            "--no-replace-objects",
            "-C",
            str(tmp_path),
            "show",
            f"{base_commit}:README.md",
        ],
        check=True,
        capture_output=True,
    ).stdout

    service._ensure_worktree(task_id)

    checkout = tmp_path / ".worktree" / "tasks" / task_id
    assert (checkout / "README.md").read_bytes() == exact
    assert (checkout / "README.md").read_bytes() != b"replacement bytes\n"


@pytest.mark.parametrize("kind", ("directory", "file", "symlink", "broken_symlink"))
def test_start_rejects_and_preserves_a_preexisting_target_path(
    tmp_path: Path,
    kind: str,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service, key=f"target-conflict-{kind}")
    task_id = str(brief["task_id"])
    _commit_brief(tmp_path, brief)
    target = tmp_path / ".worktree" / "tasks" / task_id
    target.parent.mkdir(parents=True)
    link_target = tmp_path / "preserved-link-target"
    if kind == "directory":
        target.mkdir()
        (target / "preserve.txt").write_text("directory\n", encoding="utf-8")
    elif kind == "file":
        target.write_text("file\n", encoding="utf-8")
    elif kind == "symlink":
        link_target.mkdir()
        target.symlink_to(link_target, target_is_directory=True)
    else:
        target.symlink_to(link_target, target_is_directory=True)

    with pytest.raises(TaskError, match="target|worktree|symlink|conflict"):
        service._ensure_worktree(task_id)

    assert target.lstat()
    if kind == "directory":
        assert (target / "preserve.txt").read_text(encoding="utf-8") == "directory\n"
    elif kind == "file":
        assert target.read_text(encoding="utf-8") == "file\n"
    elif kind == "symlink":
        assert target.is_symlink() and link_target.is_dir()
    else:
        assert target.is_symlink() and not link_target.exists()


@pytest.mark.parametrize("relative", (".worktree", ".worktree/tasks"))
def test_start_rejects_a_symlinked_worktree_root_without_following_it(
    tmp_path: Path,
    relative: str,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service, key=f"symlink-root-{relative}")
    task_id = str(brief["task_id"])
    _commit_brief(tmp_path, brief)
    alias = tmp_path / relative
    alias.parent.mkdir(parents=True, exist_ok=True)
    if alias.is_dir():
        alias.rmdir()
    outside = tmp_path / "outside-worktrees"
    outside.mkdir()
    alias.symlink_to(outside, target_is_directory=True)

    with pytest.raises(TaskError, match="plain directory|symlink|contain"):
        service._ensure_worktree(task_id)

    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("conflict", ("exact", "prefix", "descendant", "checked_out"))
def test_start_rejects_and_preserves_conflicting_task_branch_refs(
    tmp_path: Path,
    conflict: str,
) -> None:
    base_commit = _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service, key=f"branch-conflict-{conflict}")
    task_id = str(brief["task_id"])
    _commit_brief(tmp_path, brief)
    branch = f"aros/task/{task_id}"
    if conflict == "exact":
        existing = branch
        _git(tmp_path, "branch", existing, base_commit)
    elif conflict == "prefix":
        existing = "aros/task"
        _git(tmp_path, "branch", existing, base_commit)
    elif conflict == "descendant":
        existing = f"{branch}/nested"
        _git(tmp_path, "branch", existing, base_commit)
    else:
        existing = branch
        other = tmp_path / ".worktree" / "foreign-task-branch"
        other.parent.mkdir(exist_ok=True)
        _git(tmp_path, "worktree", "add", "-q", "-b", branch, str(other), base_commit)
        (other / "preserve.txt").write_text("dirty\n", encoding="utf-8")

    with pytest.raises(TaskError, match="branch|ref|checked out|conflict"):
        service._ensure_worktree(task_id)

    assert _git_ref_exists(tmp_path, f"refs/heads/{existing}")
    if conflict == "checked_out":
        assert (other / "preserve.txt").read_text(encoding="utf-8") == "dirty\n"


def test_start_rejects_a_target_registered_to_another_worktree(
    tmp_path: Path,
) -> None:
    base_commit = _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service, key="registered-target")
    task_id = str(brief["task_id"])
    _commit_brief(tmp_path, brief)
    target = tmp_path / ".worktree" / "tasks" / task_id
    target.parent.mkdir(parents=True)
    _git(tmp_path, "worktree", "add", "-q", "--detach", str(target), base_commit)
    preserve = target / "preserve.txt"
    preserve.write_text("registered and dirty\n", encoding="utf-8")

    with pytest.raises(TaskError, match="registered|target|worktree|conflict"):
        service._ensure_worktree(task_id)

    assert preserve.read_text(encoding="utf-8") == "registered and dirty\n"
    assert str(target) in _git(tmp_path, "worktree", "list", "--porcelain")


def test_start_rejects_and_never_prunes_a_stale_worktree_registration(
    tmp_path: Path,
) -> None:
    base_commit = _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service, key="stale-registration")
    task_id = str(brief["task_id"])
    _commit_brief(tmp_path, brief)
    target = tmp_path / ".worktree" / "tasks" / task_id
    target.parent.mkdir(parents=True)
    _git(tmp_path, "worktree", "add", "-q", "--detach", str(target), base_commit)
    preserved = tmp_path / ".worktree" / "preserved-stale-task"
    target.rename(preserved)
    before = _git(
        tmp_path,
        "worktree",
        "list",
        "--porcelain",
        "--expire=now",
    )
    assert str(target) in before and "prunable" in before

    with pytest.raises(TaskError, match="stale|prunable|registered|worktree"):
        service._ensure_worktree(task_id)

    after = _git(
        tmp_path,
        "worktree",
        "list",
        "--porcelain",
        "--expire=now",
    )
    assert after == before
    assert preserved.is_dir()


def test_git_subprocesses_do_not_load_ambient_dynamic_libraries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compiler = Path("/usr/bin/cc")
    if not compiler.is_file():
        pytest.skip("a C compiler is required for the LD_PRELOAD regression")
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service, key="malicious-dynamic-loader")
    task_id = str(brief["task_id"])
    _commit_brief(tmp_path, brief)
    marker = tmp_path / ".git" / "preload-ran"
    source = tmp_path / ".git" / "preload.c"
    library = tmp_path / ".git" / "preload.so"
    source.write_text(
        "#define _GNU_SOURCE\n"
        "#include <fcntl.h>\n"
        "#include <link.h>\n"
        "#include <unistd.h>\n"
        "unsigned int la_version(unsigned int version) { return LAV_CURRENT; }\n"
        "__attribute__((constructor)) static void mark(void) {\n"
        f"  int fd = open({json.dumps(str(marker))}, O_WRONLY | O_CREAT, 0600);\n"
        "  if (fd >= 0) close(fd);\n"
        "}\n",
        encoding="utf-8",
    )
    subprocess.run(
        [str(compiler), "-shared", "-fPIC", "-o", str(library), str(source)],
        check=True,
        capture_output=True,
    )
    monkeypatch.setenv("LD_PRELOAD", str(library))
    monkeypatch.setenv("LD_LIBRARY_PATH", str(library.parent))
    monkeypatch.setenv("LD_AUDIT", str(library))
    monkeypatch.setenv("DYLD_INSERT_LIBRARIES", str(library))

    status = service._ensure_worktree(task_id)

    assert status["state"] == "worktree_ready"
    assert not marker.exists()


def test_concurrent_and_repeated_start_reuses_one_owned_worktree(
    tmp_path: Path,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service, key="concurrent-start")
    task_id = str(brief["task_id"])
    _commit_brief(tmp_path, brief)

    with ThreadPoolExecutor(max_workers=4) as pool:
        statuses = list(pool.map(lambda _index: service._ensure_worktree(task_id), range(4)))

    assert all(status == statuses[0] for status in statuses)
    ownership = json.loads(
        (tmp_path / ".aros" / "tasks" / task_id / "ownership.json").read_text(
            encoding="utf-8"
        )
    )
    assert ownership["actor"] == brief["actor"]
    registry = _git(tmp_path, "worktree", "list", "--porcelain")
    assert registry.count(str(tmp_path / ".worktree" / "tasks" / task_id)) == 1
    assert registry.count(f"branch refs/heads/aros/task/{task_id}") == 1


def test_repeated_start_and_ready_readers_preserve_dirty_advanced_worktree(
    tmp_path: Path,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service, key="repeat-dirty-advanced")
    task_id = str(brief["task_id"])
    _commit_brief(tmp_path, brief)
    first = service._ensure_worktree(task_id)
    worktree = tmp_path / ".worktree" / "tasks" / task_id
    (worktree / "README.md").write_text("advanced child\n", encoding="utf-8")
    _git(worktree, "add", "README.md")
    _git(worktree, "commit", "-qm", "advance child")
    advanced = _git(worktree, "rev-parse", "HEAD")
    untracked = worktree / "untracked-child.txt"
    untracked.write_text("preserve child dirt\n", encoding="utf-8")

    second = service._ensure_worktree(task_id)

    assert second == first
    assert service.status(task_id) == first
    assert service.list() == [first]
    assert _git(worktree, "rev-parse", "HEAD") == advanced
    assert untracked.read_text(encoding="utf-8") == "preserve child dirt\n"
    registry = _git(tmp_path, "worktree", "list", "--porcelain")
    assert registry.count(str(worktree)) == 1


@pytest.mark.parametrize("status_state", ("missing", "prepared"))
def test_ready_status_recovers_from_valid_create_once_ownership(
    tmp_path: Path,
    status_state: str,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service, key=f"ready-recovery-{status_state}")
    task_id = str(brief["task_id"])
    _commit_brief(tmp_path, brief)
    ready = service._ensure_worktree(task_id)
    status_path = tmp_path / ".aros" / "tasks" / task_id / "status.json"
    if status_state == "missing":
        status_path.unlink()
    else:
        atomic_write_json(
            status_path,
            {
                "schema_version": 1,
                "task_id": task_id,
                "state": "prepared",
                "brief_sha256": brief["brief_sha256"],
                "updated_at": brief["created_at"],
            },
        )

    assert TaskService(tmp_path).status(task_id) == ready
    assert json.loads(status_path.read_text(encoding="utf-8")) == ready


@pytest.mark.parametrize("reader", ("status", "list", "start"))
def test_ready_task_fails_closed_if_create_once_ownership_is_missing(
    tmp_path: Path,
    reader: str,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service, key=f"missing-ownership-{reader}")
    task_id = str(brief["task_id"])
    _commit_brief(tmp_path, brief)
    service._ensure_worktree(task_id)
    worktree = tmp_path / ".worktree" / "tasks" / task_id
    preserve = worktree / "preserve.txt"
    preserve.write_text("owned work\n", encoding="utf-8")
    (tmp_path / ".aros" / "tasks" / task_id / "ownership.json").unlink()

    with pytest.raises(TaskError, match="ownership"):
        if reader == "status":
            service.status(task_id)
        elif reader == "list":
            service.list()
        else:
            service._ensure_worktree(task_id)

    assert preserve.read_text(encoding="utf-8") == "owned work\n"


@pytest.mark.parametrize("reader", ("status", "list", "start"))
def test_owned_task_fails_closed_on_tamper_without_touching_child_work(
    tmp_path: Path,
    reader: str,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service, key=f"tampered-ownership-{reader}")
    task_id = str(brief["task_id"])
    _commit_brief(tmp_path, brief)
    service._ensure_worktree(task_id)
    worktree = tmp_path / ".worktree" / "tasks" / task_id
    preserve = worktree / "preserve-tamper.txt"
    preserve.write_text("never remove\n", encoding="utf-8")
    ownership_path = tmp_path / ".aros" / "tasks" / task_id / "ownership.json"
    ownership = json.loads(ownership_path.read_text(encoding="utf-8"))
    ownership["actor"] = "tampered"
    atomic_write_json(ownership_path, ownership)

    with pytest.raises(TaskError, match="ownership"):
        if reader == "status":
            service.status(task_id)
        elif reader == "list":
            service.list()
        else:
            service._ensure_worktree(task_id)

    assert preserve.read_text(encoding="utf-8") == "never remove\n"


def test_owned_task_rejects_a_misregistered_worktree_without_cleanup(
    tmp_path: Path,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service, key="misregistered-owned-worktree")
    task_id = str(brief["task_id"])
    _commit_brief(tmp_path, brief)
    service._ensure_worktree(task_id)
    worktree = tmp_path / ".worktree" / "tasks" / task_id
    moved = tmp_path / ".worktree" / "tasks" / f"{task_id}-moved"
    _git(tmp_path, "worktree", "move", str(worktree), str(moved))
    preserve = moved / "preserve-after-move.txt"
    preserve.write_text("misregistered\n", encoding="utf-8")

    with pytest.raises(TaskError, match="ownership|registered|worktree|path"):
        service.status(task_id)

    assert preserve.read_text(encoding="utf-8") == "misregistered\n"
    assert moved.is_dir()


def test_partial_start_without_ownership_is_preserved_and_never_adopted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service, key="partial-before-ownership")
    task_id = str(brief["task_id"])
    _commit_brief(tmp_path, brief)
    original_create_json = tasks_module.create_json

    class InjectedInterruption(RuntimeError):
        pass

    def interrupt_ownership(path: str | Path, value: object) -> bool:
        if Path(path).name == "ownership.json":
            raise InjectedInterruption
        return original_create_json(path, value)

    monkeypatch.setattr(tasks_module, "create_json", interrupt_ownership)
    with pytest.raises(InjectedInterruption):
        service._ensure_worktree(task_id)
    monkeypatch.setattr(tasks_module, "create_json", original_create_json)
    worktree = tmp_path / ".worktree" / "tasks" / task_id
    preserve = worktree / "partial-work.txt"
    preserve.write_text("partial\n", encoding="utf-8")
    assert not (tmp_path / ".aros" / "tasks" / task_id / "ownership.json").exists()

    with pytest.raises(TaskError, match="unowned|branch|worktree|conflict"):
        service._ensure_worktree(task_id)
    with pytest.raises(TaskError, match="unowned|branch|worktree|ownership"):
        service.status(task_id)

    assert preserve.read_text(encoding="utf-8") == "partial\n"


def test_partial_start_with_valid_ownership_recovers_worktree_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service, key="partial-after-ownership")
    task_id = str(brief["task_id"])
    _commit_brief(tmp_path, brief)
    original_create_json = tasks_module.create_json

    class InjectedInterruption(RuntimeError):
        pass

    def interrupt_after_ownership(path: str | Path, value: object) -> bool:
        created = original_create_json(path, value)
        if Path(path).name == "ownership.json":
            raise InjectedInterruption
        return created

    monkeypatch.setattr(tasks_module, "create_json", interrupt_after_ownership)
    with pytest.raises(InjectedInterruption):
        service._ensure_worktree(task_id)
    monkeypatch.setattr(tasks_module, "create_json", original_create_json)

    ready = TaskService(tmp_path).status(task_id)

    ownership = json.loads(
        (tmp_path / ".aros" / "tasks" / task_id / "ownership.json").read_text(
            encoding="utf-8"
        )
    )
    assert ready["state"] == "worktree_ready"
    assert ready["ownership_sha256"] == ownership["ownership_sha256"]


@pytest.mark.parametrize(
    "mutation",
    ("commit", "untracked", "staged", "ignored", "mode"),
)
def test_start_never_promotes_a_racy_new_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    base_commit = _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service, key=f"new-checkout-race-{mutation}")
    task_id = str(brief["task_id"])
    _commit_brief(tmp_path, brief)
    worktree = tmp_path / ".worktree" / "tasks" / task_id
    original_create_json = tasks_module.create_json
    injected = False

    def mutate_before_ownership(path: str | Path, value: object) -> bool:
        nonlocal injected
        if Path(path).name == "ownership.json" and not injected:
            injected = True
            if mutation == "commit":
                (worktree / "README.md").write_text(
                    "racy committed child\n",
                    encoding="utf-8",
                )
                _git(worktree, "add", "README.md")
                _git(worktree, "commit", "-qm", "racy child commit")
            elif mutation == "untracked":
                (worktree / "race-untracked.txt").write_text(
                    "preserve\n",
                    encoding="utf-8",
                )
            elif mutation == "staged":
                (worktree / "README.md").write_text(
                    "racy staged child\n",
                    encoding="utf-8",
                )
                _git(worktree, "add", "README.md")
            elif mutation == "ignored":
                ignored = worktree / ".worktree" / "race-ignored.txt"
                ignored.parent.mkdir()
                ignored.write_text("preserve\n", encoding="utf-8")
            elif mutation == "mode":
                (worktree / "README.md").chmod(0o755)
        return original_create_json(path, value)

    monkeypatch.setattr(tasks_module, "create_json", mutate_before_ownership)

    with pytest.raises(TaskError, match="checkout|base|clean|mode|index"):
        service._ensure_worktree(task_id)

    monkeypatch.setattr(tasks_module, "create_json", original_create_json)
    assert injected
    ownership_path = tmp_path / ".aros" / "tasks" / task_id / "ownership.json"
    assert ownership_path.is_file()
    status = json.loads(
        (tmp_path / ".aros" / "tasks" / task_id / "status.json").read_text(
            encoding="utf-8"
        )
    )
    assert status["state"] == "prepared"
    with pytest.raises(TaskError, match="checkout|base|clean|mode|index"):
        service.status(task_id)
    if mutation == "commit":
        assert _git(worktree, "rev-parse", "HEAD") != base_commit
    elif mutation == "untracked":
        assert (worktree / "race-untracked.txt").is_file()
    elif mutation == "staged":
        assert _git(worktree, "diff", "--cached", "--name-only") == "README.md"
    elif mutation == "ignored":
        assert (worktree / ".worktree" / "race-ignored.txt").is_file()
    elif mutation == "mode":
        assert (worktree / "README.md").stat().st_mode & 0o111


def test_start_rejects_racy_mode_when_repository_disables_filemode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_workspace(tmp_path)
    _git(tmp_path, "config", "core.fileMode", "false")
    service = TaskService(tmp_path)
    brief = _create(service, key="new-checkout-race-disabled-filemode")
    task_id = str(brief["task_id"])
    _commit_brief(tmp_path, brief)
    worktree = tmp_path / ".worktree" / "tasks" / task_id
    original_create_json = tasks_module.create_json
    injected = False

    def mutate_before_ownership(path: str | Path, value: object) -> bool:
        nonlocal injected
        if Path(path).name == "ownership.json" and not injected:
            injected = True
            (worktree / "README.md").chmod(0o755)
        return original_create_json(path, value)

    monkeypatch.setattr(tasks_module, "create_json", mutate_before_ownership)

    with pytest.raises(TaskError, match="checkout|clean|mode"):
        service._ensure_worktree(task_id)

    assert injected
    assert (worktree / "README.md").stat().st_mode & 0o111
    assert (tmp_path / ".aros" / "tasks" / task_id / "ownership.json").is_file()


@pytest.mark.parametrize("status_state", ("missing", "prepared"))
def test_ownership_recovery_rejects_a_dirty_partial_checkout(
    tmp_path: Path,
    status_state: str,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service, key=f"dirty-ownership-recovery-{status_state}")
    task_id = str(brief["task_id"])
    _commit_brief(tmp_path, brief)
    service._ensure_worktree(task_id)
    worktree = tmp_path / ".worktree" / "tasks" / task_id
    dirty = worktree / "partial-untracked.txt"
    dirty.write_text("preserve\n", encoding="utf-8")
    status_path = tmp_path / ".aros" / "tasks" / task_id / "status.json"
    if status_state == "missing":
        status_path.unlink()
    else:
        atomic_write_json(
            status_path,
            {
                "schema_version": 1,
                "task_id": task_id,
                "state": "prepared",
                "brief_sha256": brief["brief_sha256"],
                "updated_at": brief["created_at"],
            },
        )

    with pytest.raises(TaskError, match="checkout|clean"):
        TaskService(tmp_path).status(task_id)

    assert dirty.read_text(encoding="utf-8") == "preserve\n"


def test_parent_head_race_preserves_unowned_partial_start_and_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service, key="parent-head-race")
    task_id = str(brief["task_id"])
    original_head = _commit_brief(tmp_path, brief)
    original_run = tasks_module.subprocess.run
    raced = False

    def move_head_before_worktree_add(*args: object, **kwargs: object) -> object:
        nonlocal raced
        command = args[0]
        if (
            not raced
            and isinstance(command, list)
            and "worktree" in command
            and "add" in command
        ):
            raced = True
            original_run(
                [
                    "git",
                    "-C",
                    str(tmp_path),
                    "commit",
                    "--allow-empty",
                    "-qm",
                    "race parent HEAD",
                ],
                check=True,
            )
        return original_run(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(tasks_module.subprocess, "run", move_head_before_worktree_add)

    with pytest.raises(TaskError, match="HEAD.*changed|stable"):
        service._ensure_worktree(task_id)

    assert raced
    assert _git(tmp_path, "rev-parse", "HEAD") != original_head
    worktree = tmp_path / ".worktree" / "tasks" / task_id
    assert worktree.is_dir()
    assert not (tmp_path / ".aros" / "tasks" / task_id / "ownership.json").exists()
    with pytest.raises(TaskError, match="unowned|branch|worktree|conflict"):
        service._ensure_worktree(task_id)
    assert worktree.is_dir()


def test_start_disables_real_post_checkout_and_filter_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_workspace(tmp_path)
    payload = tmp_path / "payload.txt"
    payload.write_text("repository bytes\n", encoding="utf-8")
    (tmp_path / ".gitattributes").write_text(
        "payload.txt filter=malicious\n",
        encoding="utf-8",
    )
    _git(tmp_path, "add", ".gitattributes", "payload.txt")
    _git(tmp_path, "commit", "-qm", "add filtered payload")
    base_commit = _git(tmp_path, "rev-parse", "HEAD")
    service = TaskService(tmp_path)
    brief = _create(service, key="malicious-checkout-config")
    task_id = str(brief["task_id"])
    _commit_brief(tmp_path, brief)
    markers = {
        name: tmp_path / f"{name}-ran"
        for name in ("hook", "clean", "smudge", "process", "ambient")
    }
    hooks = tmp_path / ".git" / "malicious-hooks"
    hooks.mkdir()
    post_checkout = hooks / "post-checkout"
    post_checkout.write_text(
        f"#!/bin/sh\ntouch {shlex.quote(str(markers['hook']))}\n",
        encoding="utf-8",
    )
    post_checkout.chmod(0o755)
    _git(tmp_path, "config", "core.hooksPath", str(hooks))
    for kind in ("clean", "smudge"):
        command = (
            f"sh -c 'touch {shlex.quote(str(markers[kind]))}; cat'"
        )
        _git(tmp_path, "config", f"filter.malicious.{kind}", command)
    _git(
        tmp_path,
        "config",
        "filter.malicious.process",
        f"sh -c 'touch {shlex.quote(str(markers['process']))}; exit 1'",
    )
    _git(tmp_path, "config", "filter.malicious.required", "true")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(hooks))

    status = service._ensure_worktree(task_id)

    checkout = tmp_path / ".worktree" / "tasks" / task_id
    assert status["state"] == "worktree_ready"
    assert (checkout / "payload.txt").read_bytes() == b"repository bytes\n"
    assert _git(checkout, "rev-parse", "HEAD") == base_commit
    assert all(not marker.exists() for marker in markers.values())


def test_start_accepts_clean_git_native_eol_checkout(tmp_path: Path) -> None:
    _init_workspace(tmp_path)
    tracked = tmp_path / "native-eol.txt"
    tracked.write_bytes(b"line-one\nline-two\n")
    (tmp_path / ".gitattributes").write_text(
        "native-eol.txt text eol=crlf\n",
        encoding="utf-8",
    )
    _git(tmp_path, "add", ".gitattributes", "native-eol.txt")
    _git(tmp_path, "commit", "-qm", "add Git-native EOL checkout")
    service = TaskService(tmp_path)
    brief = _create(service, key="git-native-eol")
    task_id = str(brief["task_id"])
    _commit_brief(tmp_path, brief)

    status = service._ensure_worktree(task_id)

    child = tmp_path / ".worktree" / "tasks" / task_id
    checked_out = child / "native-eol.txt"
    assert status["state"] == "worktree_ready"
    assert checked_out.read_bytes() == b"line-one\r\nline-two\r\n"
    assert _git(child, "status", "--porcelain=v1", "--untracked-files=all") == ""


def test_start_git_commands_are_scrubbed_pinned_and_nondestructive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service, key="git-command-boundary")
    task_id = str(brief["task_id"])
    _commit_brief(tmp_path, brief)
    calls: list[tuple[list[str], dict[str, str]]] = []
    original_run = tasks_module.subprocess.run

    def record_git(*args: object, **kwargs: object) -> object:
        command = args[0]
        environment = kwargs.get("env")
        if isinstance(command, list) and "git" in command:
            assert isinstance(environment, dict)
            calls.append((command[command.index("git") :], environment))
        return original_run(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setenv("GIT_DIR", str(tmp_path / "foreign.git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "foreign-worktree"))
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "foreign-pythonpath"))
    monkeypatch.setenv("PYTHONWARNINGS", "error")
    monkeypatch.setattr(tasks_module.subprocess, "run", record_git)

    service._ensure_worktree(task_id)

    assert calls
    assert all(
        {key for key in env if key.startswith("GIT_")}
        == {"GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM"}
        for _, env in calls
    )
    assert all(
        env["GIT_CONFIG_GLOBAL"] == os.devnull
        and env["GIT_CONFIG_NOSYSTEM"] == "1"
        for _, env in calls
    )
    assert all(not any(key.startswith("PYTHON") for key in env) for _, env in calls)
    assert all(
        any(str(service._git_dir) in argument for argument in command)
        for command, _ in calls
    )
    worktree_add = [
        command
        for command, _ in calls
        if "worktree" in command and "add" in command
    ]
    assert len(worktree_add) == 1
    worktree_add_argv = worktree_add[0]
    expected_target = str(tmp_path / ".worktree" / "tasks" / task_id)
    assert expected_target in worktree_add_argv
    assert "--force" not in worktree_add_argv
    assert "prune" not in worktree_add_argv
    assert "core.hooksPath=/dev/null" in worktree_add_argv
    forbidden = {"--force", "reset", "clean", "prune", "remove"}
    assert all(not forbidden.intersection(command) for command, _ in calls)


def test_create_freezes_brief_and_prepared_status_without_execution(
    tmp_path: Path,
) -> None:
    head = _init_workspace(tmp_path)
    dirty = tmp_path / "unrelated.txt"
    dirty.write_text("preserve me\n", encoding="utf-8")
    marker = tmp_path / "adapter-ran"
    worktrees_before = _git(tmp_path, "worktree", "list", "--porcelain")
    service = TaskService(tmp_path)

    brief = service.create(
        "  Inspect committed state  ",
        actor="  principal  ",
        mode="read_only",
        adapter_argv=[
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(marker)!r}).touch()",
            "  exact argument  ",
        ],
        capabilities={"network": False, "shell": False},
        deliverables=["reports/inspection.json"],
        acceptance=["python -m pytest -q"],
        timeout_seconds=12.5,
        idempotency_key="  inspect-once  ",
    )

    task_id = str(brief["task_id"])
    assert re.fullmatch(r"TASK-\d{8}-[A-Za-z0-9][A-Za-z0-9-]*", task_id)
    assert set(brief) == {
        "schema_version",
        "task_id",
        "objective",
        "mode",
        "base_commit",
        "actor",
        "adapter_argv",
        "capabilities",
        "deliverables",
        "acceptance",
        "timeout_seconds",
        "idempotency_key",
        "request_sha256",
        "created_at",
        "brief_sha256",
    }
    assert brief["schema_version"] == 1
    assert brief["objective"] == "Inspect committed state"
    assert brief["mode"] == "read_only"
    assert brief["base_commit"] == head
    assert re.fullmatch(r"[0-9a-f]{40}", str(brief["base_commit"]))
    assert brief["actor"] == "principal"
    assert brief["adapter_argv"][-1] == "  exact argument  "
    assert brief["capabilities"] == {"network": False, "shell": False}
    assert brief["deliverables"] == ["reports/inspection.json"]
    assert brief["acceptance"] == ["python -m pytest -q"]
    assert brief["timeout_seconds"] == 12.5
    assert brief["idempotency_key"] == "inspect-once"
    assert str(brief["created_at"]).endswith("Z")
    request = {
        "objective": "Inspect committed state",
        "actor": "principal",
        "mode": "read_only",
        "adapter_argv": brief["adapter_argv"],
        "capabilities": brief["capabilities"],
        "deliverables": brief["deliverables"],
        "acceptance": brief["acceptance"],
        "timeout_seconds": 12.5,
        "idempotency_key": "inspect-once",
    }
    assert brief["request_sha256"] == json_sha256(request)
    assert brief["brief_sha256"] == json_sha256(
        {key: value for key, value in brief.items() if key != "brief_sha256"}
    )

    brief_path = tmp_path / "tasks" / task_id / "brief.json"
    assert json.loads(brief_path.read_text(encoding="utf-8")) == brief
    status = {
        "schema_version": 1,
        "task_id": task_id,
        "state": "prepared",
        "brief_sha256": brief["brief_sha256"],
        "updated_at": brief["created_at"],
    }
    assert service.status(task_id) == status
    assert service.list() == [status]
    assert json.loads(
        (tmp_path / ".aros" / "tasks" / task_id / "status.json").read_text(
            encoding="utf-8"
        )
    ) == status
    key_digest = hashlib.sha256(b"inspect-once").hexdigest()
    index_path = tmp_path / ".aros" / "tasks" / "idempotency" / f"{key_digest}.json"
    assert index_path.is_file()
    assert "inspect-once" not in str(index_path.relative_to(tmp_path))

    assert dirty.read_text(encoding="utf-8") == "preserve me\n"
    assert _git(tmp_path, "rev-parse", "HEAD") == head
    assert _git(tmp_path, "worktree", "list", "--porcelain") == worktrees_before
    assert not marker.exists()
    assert not (tmp_path / ".worktree" / "tasks").exists()


def test_service_requires_exact_git_root_and_initialized_aros_workspace(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    _git(repository, "config", "user.email", "aros@example.invalid")
    _git(repository, "config", "user.name", "AROS test")
    (repository / "README.md").write_text("# repository\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-qm", "initial state")

    with pytest.raises(TaskError, match="not initialized"):
        TaskService(repository)

    init_workspace(repository, "Exact root test")
    nested = repository / "nested"
    nested.mkdir()
    with pytest.raises(TaskError, match="Git repository root"):
        TaskService(nested)

    alias = tmp_path / "repository-alias"
    alias.symlink_to(repository, target_is_directory=True)
    with pytest.raises(TaskError, match="exact Git repository root"):
        TaskService(alias)


def test_git_probes_ignore_foreign_ambient_repository_and_config_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    foreign = tmp_path / "foreign"
    workspace.mkdir()
    foreign.mkdir()
    workspace_head = _init_workspace(workspace)
    _init_workspace(foreign)
    (foreign / "foreign.txt").write_text("foreign head\n", encoding="utf-8")
    _git(foreign, "add", "foreign.txt")
    _git(foreign, "commit", "-qm", "distinct foreign head")
    assert _git(foreign, "rev-parse", "HEAD") != workspace_head
    monkeypatch.setenv("GIT_DIR", str(foreign / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(foreign))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.worktree")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(foreign))

    service = TaskService(workspace)
    brief = _create(service, key="ambient-git-overrides")

    assert brief["base_commit"] == workspace_head


def test_task_service_git_binding_does_not_pin_inode_identity() -> None:
    source = inspect.getsource(TaskService)

    assert not {
        name
        for name in (
            "_git_dir_identity",
            "_git_common_dir_identity",
            "_require_pinned_git_identity",
        )
        if name in source
    }


def test_create_rejects_a_changed_git_directory_association(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    foreign = tmp_path / "foreign"
    workspace.mkdir()
    foreign.mkdir()
    _init_workspace(workspace)
    _init_workspace(foreign)
    service = TaskService(workspace)
    (workspace / ".git").rename(workspace / ".git-original")
    (workspace / ".git").write_text(
        f"gitdir: {(foreign / '.git').resolve()}\n",
        encoding="utf-8",
    )

    with pytest.raises(TaskError, match="Git directory association"):
        _create(service, key="changed-git-association")


def test_linked_worktree_rejects_common_git_directory_redirection(
    tmp_path: Path,
) -> None:
    primary = tmp_path / "primary"
    linked = tmp_path / "linked"
    foreign = tmp_path / "foreign"
    primary.mkdir()
    foreign.mkdir()
    _init_workspace(primary)
    _init_workspace(foreign)
    subprocess.run(
        [
            "git",
            "-C",
            str(primary),
            "worktree",
            "add",
            "-q",
            "-b",
            "linked-test",
            str(linked),
        ],
        check=True,
    )
    (linked / ".aros").mkdir()
    service = TaskService(linked)
    git_dir = Path(_git(linked, "rev-parse", "--absolute-git-dir"))
    (git_dir / "commondir").write_text(
        f"{(foreign / '.git').resolve()}\n",
        encoding="utf-8",
    )

    with pytest.raises(TaskError, match="common Git directory"):
        service._require_git_root()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("objective", "  ", "objective"),
        ("actor", "", "actor"),
        ("idempotency_key", "\t", "idempotency_key"),
        ("mode", "readonly", "mode"),
        ("mode", 1, "mode"),
        ("adapter_argv", [], "adapter_argv"),
        ("adapter_argv", ("adapter",), "adapter_argv"),
        ("adapter_argv", [""], "adapter_argv"),
        ("adapter_argv", ["adapter", "bad\x00argument"], "adapter_argv"),
        ("capabilities", {"network": False}, "capabilities"),
        (
            "capabilities",
            {"network": False, "shell": False, "filesystem": True},
            "capabilities",
        ),
        ("capabilities", {"network": 0, "shell": True}, "booleans"),
        ("deliverables", ("result.json",), "deliverables"),
        ("deliverables", ["result.json", 3], "deliverables"),
        ("acceptance", "python verify.py", "acceptance"),
        ("acceptance", [None], "acceptance"),
        ("timeout_seconds", 0, "positive"),
        ("timeout_seconds", -1, "positive"),
        ("timeout_seconds", True, "positive"),
        ("timeout_seconds", "60", "positive"),
        ("timeout_seconds", math.inf, "finite"),
        ("timeout_seconds", math.nan, "finite"),
    ),
)
def test_create_rejects_invalid_request_fields_without_writing_records(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    objective: object = "bounded objective"
    request = _request()
    if field == "objective":
        objective = value
    else:
        request[field] = value

    with pytest.raises(TaskError, match=message):
        service.create(objective, **request)  # type: ignore[arg-type]

    assert not (tmp_path / "tasks").exists()
    assert not (tmp_path / ".aros" / "tasks").exists()


def test_create_rejects_an_oversized_integer_timeout_as_a_task_error(
    tmp_path: Path,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    request = _request()
    request["timeout_seconds"] = 10**10_000

    with pytest.raises(TaskError, match="finite"):
        service.create("bounded objective", **request)  # type: ignore[arg-type]

    assert not (tmp_path / "tasks").exists()


@pytest.mark.parametrize(
    "field",
    (
        "objective",
        "actor",
        "idempotency_key",
        "adapter_argv",
        "deliverables",
        "acceptance",
    ),
)
def test_create_rejects_lone_surrogates_in_external_strings(
    tmp_path: Path,
    field: str,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    surrogate = "\ud800"
    objective = "bounded objective"
    request = _request()
    if field == "objective":
        objective = surrogate
    elif field in {"actor", "idempotency_key"}:
        request[field] = surrogate
    elif field == "adapter_argv":
        request[field] = ["adapter", surrogate]
    else:
        request[field] = [surrogate]

    with pytest.raises(TaskError, match="UTF-8"):
        service.create(objective, **request)  # type: ignore[arg-type]

    assert not (tmp_path / "tasks").exists()


def test_create_is_idempotent_for_the_same_request_and_rejects_a_change(
    tmp_path: Path,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    request = _request(key="one-logical-task")

    first = service.create("bounded objective", **request)  # type: ignore[arg-type]
    first_status = service.status(str(first["task_id"]))
    second = service.create("bounded objective", **request)  # type: ignore[arg-type]

    assert second == first
    assert service.status(str(second["task_id"])) == first_status
    assert len(list((tmp_path / "tasks").glob("TASK-*/brief.json"))) == 1
    with pytest.raises(TaskError, match="idempotency key.*different task request"):
        service.create("changed objective", **request)  # type: ignore[arg-type]
    assert len(list((tmp_path / "tasks").glob("TASK-*/brief.json"))) == 1


@pytest.mark.parametrize(
    "missing",
    ("status", "index", "both", "runtime", "runtime_and_index"),
)
def test_create_recovers_missing_prepared_records_from_the_immutable_brief(
    tmp_path: Path,
    missing: str,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    key = "recover-partial-create"
    brief = _create(service, key=key)
    task_id = str(brief["task_id"])
    runtime_path = tmp_path / ".aros" / "tasks" / task_id
    status_path = runtime_path / "status.json"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    index_path = tmp_path / ".aros" / "tasks" / "idempotency" / f"{digest}.json"
    if missing in {"status", "both", "runtime", "runtime_and_index"}:
        status_path.unlink()
    if missing in {"runtime", "runtime_and_index"}:
        runtime_path.rmdir()
    if missing in {"index", "both", "runtime_and_index"}:
        index_path.unlink()

    replayed = _create(service, key=key)

    assert replayed == brief
    assert service.status(task_id) == {
        "schema_version": 1,
        "task_id": task_id,
        "state": "prepared",
        "brief_sha256": brief["brief_sha256"],
        "updated_at": brief["created_at"],
    }
    index = json.loads(index_path.read_text(encoding="utf-8"))
    assert index["idempotency_key_sha256"] == digest
    assert index["request_sha256"] == brief["request_sha256"]
    assert index["brief_sha256"] == brief["brief_sha256"]


def test_precommit_task_staging_is_ignored_and_preserved(
    tmp_path: Path,
) -> None:
    _init_workspace(tmp_path)
    interrupted = tmp_path / "tasks" / ".staging" / "interrupted-publication"
    interrupted.mkdir(parents=True)
    marker = interrupted / "brief.json"
    marker.write_text("ambiguous staged material\n", encoding="utf-8")
    service = TaskService(tmp_path)

    assert service.list() == []
    brief = _create(service, key="after-interruption")

    assert marker.read_text(encoding="utf-8") == "ambiguous staged material\n"
    assert service.list() == [service.status(str(brief["task_id"]))]


def test_empty_preauthority_task_container_is_ignored_and_preserved(
    tmp_path: Path,
) -> None:
    _init_workspace(tmp_path)
    empty = tmp_path / "tasks" / "TASK-20260802-empty-remnant"
    empty.mkdir(parents=True)
    service = TaskService(tmp_path)

    assert service.list() == []
    brief = _create(service, key="after-empty-container")

    assert empty.is_dir()
    assert list(empty.iterdir()) == []
    assert service.list() == [service.status(str(brief["task_id"]))]


def test_nonempty_preauthority_task_container_fails_closed_and_is_preserved(
    tmp_path: Path,
) -> None:
    _init_workspace(tmp_path)
    ambiguous = tmp_path / "tasks" / "TASK-20260802-ambiguous-remnant"
    ambiguous.mkdir(parents=True)
    marker = ambiguous / "unknown.bin"
    marker.write_bytes(b"preserve")
    service = TaskService(tmp_path)

    with pytest.raises(TaskError, match="ambiguous.*without.*brief"):
        service.list()
    with pytest.raises(TaskError, match="ambiguous.*without.*brief"):
        _create(service, key="blocked-by-ambiguous-container")

    assert marker.read_bytes() == b"preserve"


def test_different_key_create_recovers_a_published_brief_after_interruption(
    tmp_path: Path,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    first = _create(service, key="interrupted-first", objective="first task")
    first_id = str(first["task_id"])
    runtime_path = tmp_path / ".aros" / "tasks" / first_id
    (runtime_path / "status.json").unlink()
    runtime_path.rmdir()
    first_digest = hashlib.sha256(b"interrupted-first").hexdigest()
    (
        tmp_path / ".aros" / "tasks" / "idempotency" / f"{first_digest}.json"
    ).unlink()

    second = _create(service, key="after-interruption", objective="second task")

    assert service.status(first_id)["brief_sha256"] == first["brief_sha256"]
    assert {status["task_id"] for status in service.list()} == {
        first["task_id"],
        second["task_id"],
    }


def test_different_key_publications_serialize_without_inventory_races(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    publication_reached = Event()
    release_publication = Event()
    second_started = Event()
    reader_started = Event()
    original_create_directory = tasks_module._create_plain_directory

    def pause_legacy_visible_directory(path: Path, description: str) -> None:
        original_create_directory(path, description)
        if description == "versioned task path" and not publication_reached.is_set():
            publication_reached.set()
            assert release_publication.wait(timeout=5)

    monkeypatch.setattr(
        tasks_module,
        "_create_plain_directory",
        pause_legacy_visible_directory,
    )
    original_publish = getattr(TaskService, "_publish_staged_brief", None)
    if original_publish is not None:

        def pause_atomic_publication(
            self: TaskService,
            staging: Path,
            target: Path,
        ) -> None:
            original_publish(self, staging, target)
            if not publication_reached.is_set():
                publication_reached.set()
                assert release_publication.wait(timeout=5)

        monkeypatch.setattr(TaskService, "_publish_staged_brief", pause_atomic_publication)

    def create_second() -> dict[str, object]:
        second_started.set()
        return _create(service, key="publication-two", objective="second task")

    def read_inventory() -> list[dict[str, object]]:
        reader_started.set()
        return service.list()

    with ThreadPoolExecutor(max_workers=3) as pool:
        first_future = pool.submit(
            _create,
            service,
            key="publication-one",
            objective="first task",
        )
        assert publication_reached.wait(timeout=5)
        second_future = pool.submit(create_second)
        reader_future = pool.submit(read_inventory)
        assert second_started.wait(timeout=5)
        assert reader_started.wait(timeout=5)
        release_publication.set()
        first = first_future.result(timeout=5)
        second = second_future.result(timeout=5)
        observed = reader_future.result(timeout=5)

    final = service.list()
    task_ids = {str(first["task_id"]), str(second["task_id"])}
    assert len(task_ids) == 2
    assert {str(status["task_id"]) for status in final} == task_ids
    assert {str(status["task_id"]) for status in observed} <= task_ids


def test_publication_syncs_target_and_parent_before_staging_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    synced: list[Path] = []
    monkeypatch.setattr(tasks_module, "_fsync_directory", synced.append)

    brief = _create(service, key="sync-publication-parents")

    target = tmp_path / "tasks" / str(brief["task_id"])
    target_sync = synced.index(target)
    assert synced[target_sync : target_sync + 2] == [target, tmp_path / "tasks"]
    assert tmp_path / "tasks" / ".staging" in synced


def test_publication_link_failure_preserves_staging_and_empty_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)

    def fail_cross_device_link(
        _source: Path,
        _target: Path,
        *,
        follow_symlinks: bool,
    ) -> None:
        assert follow_symlinks is False
        raise OSError(errno.EXDEV, "cross-device link")

    monkeypatch.setattr(tasks_module.os, "link", fail_cross_device_link)

    with pytest.raises(TaskError, match="task brief publication"):
        _create(service, key="link-failure")

    staged = list((tmp_path / "tasks" / ".staging").glob("TASK-*/brief.json"))
    targets = list((tmp_path / "tasks").glob("TASK-*"))
    assert len(staged) == 1
    assert len(targets) == 1
    assert list(targets[0].iterdir()) == []
    assert service.list() == []


def test_publication_never_clobbers_a_race_created_brief(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    original_link = tasks_module.os.link

    def create_foreign_destination_then_link(
        source: Path,
        target: Path,
        *,
        follow_symlinks: bool,
    ) -> None:
        target.write_text("foreign\n", encoding="utf-8")
        original_link(source, target, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(
        tasks_module.os,
        "link",
        create_foreign_destination_then_link,
    )

    with pytest.raises(TaskError, match="task brief publication"):
        _create(service, key="link-eexist")

    targets = list((tmp_path / "tasks").glob("TASK-*"))
    assert len(targets) == 1
    assert (targets[0] / "brief.json").read_text(encoding="utf-8") == "foreign\n"
    assert list((tmp_path / "tasks" / ".staging").glob("TASK-*/brief.json"))


@pytest.mark.parametrize("reader", ("status", "list"))
def test_immediate_post_link_crash_is_recoverable_and_preserves_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reader: str,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    original_link = tasks_module.os.link

    class InjectedInterruption(RuntimeError):
        pass

    def interrupt_after_link(
        source: Path,
        target: Path,
        *,
        follow_symlinks: bool,
    ) -> None:
        original_link(source, target, follow_symlinks=follow_symlinks)
        raise InjectedInterruption

    monkeypatch.setattr(tasks_module.os, "link", interrupt_after_link)
    with pytest.raises(InjectedInterruption):
        _create(service, key=f"immediate-link-crash-{reader}")
    monkeypatch.setattr(tasks_module.os, "link", original_link)
    targets = list((tmp_path / "tasks").glob("TASK-*"))
    staged = list((tmp_path / "tasks" / ".staging").glob("TASK-*/brief.json"))
    assert len(targets) == len(staged) == 1
    task_id = targets[0].name
    published = targets[0] / "brief.json"
    assert published.stat().st_nlink == 2
    assert staged[0].samefile(published)

    fresh = TaskService(tmp_path)
    result = fresh.status(task_id) if reader == "status" else fresh.list()

    if reader == "status":
        assert result["task_id"] == task_id  # type: ignore[index]
    else:
        assert [status["task_id"] for status in result] == [task_id]  # type: ignore[union-attr]
    assert not staged[0].exists()
    assert published.stat().st_nlink == 1


@pytest.mark.parametrize("kind", ("different_inode", "symlink"))
def test_reconciliation_rejects_and_preserves_ambiguous_staging_brief(
    tmp_path: Path,
    kind: str,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service, key=f"ambiguous-staging-{kind}")
    task_id = str(brief["task_id"])
    authoritative = tmp_path / "tasks" / task_id / "brief.json"
    staging = tmp_path / "tasks" / ".staging" / task_id
    staging.mkdir()
    staged_brief = staging / "brief.json"
    if kind == "different_inode":
        staged_brief.write_text("{}\n", encoding="utf-8")
    else:
        staged_brief.symlink_to(authoritative)

    with pytest.raises(TaskError, match="ambiguous task staging"):
        service.status(task_id)

    assert staged_brief.exists()
    assert authoritative.is_file()


def test_reconciliation_unlinks_proven_alias_but_preserves_extra_staging_material(
    tmp_path: Path,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service, key="staging-alias-with-extra")
    task_id = str(brief["task_id"])
    authoritative = tmp_path / "tasks" / task_id / "brief.json"
    staging = tmp_path / "tasks" / ".staging" / task_id
    staging.mkdir()
    staged_brief = staging / "brief.json"
    os.link(authoritative, staged_brief, follow_symlinks=False)
    extra = staging / "unexpected.txt"
    extra.write_text("preserve\n", encoding="utf-8")

    with pytest.raises(TaskError, match="ambiguous material"):
        service.list()

    assert not staged_brief.exists()
    assert extra.read_text(encoding="utf-8") == "preserve\n"
    assert authoritative.stat().st_nlink == 1


def test_reconciliation_removes_an_empty_staging_cleanup_remnant(
    tmp_path: Path,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service, key="empty-staging-cleanup-remnant")
    task_id = str(brief["task_id"])
    staging = tmp_path / "tasks" / ".staging" / task_id
    staging.mkdir()

    assert service.status(task_id)["task_id"] == task_id

    assert not staging.exists()


def test_task_publication_has_no_linux_specific_rename_helper() -> None:
    assert not hasattr(tasks_module, "_rename_noreplace")


def test_first_create_durably_syncs_record_roots_and_lock_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    synced_directories: list[Path] = []
    synced_files: list[Path] = []
    original_fsync = tasks_module.os.fsync

    def record_file_sync(descriptor: int) -> None:
        try:
            synced_files.append(Path(f"/proc/self/fd/{descriptor}").resolve())
        except OSError:
            pass
        original_fsync(descriptor)

    monkeypatch.setattr(tasks_module, "_fsync_directory", synced_directories.append)
    monkeypatch.setattr(tasks_module.os, "fsync", record_file_sync)

    _create(service, key="durable-first-create")

    required_directories = {
        tmp_path,
        tmp_path / ".aros",
        tmp_path / "tasks",
        tmp_path / ".aros" / "tasks",
        tmp_path / ".aros" / "locks",
    }
    assert required_directories <= set(synced_directories)
    lock_files = [path for path in synced_files if path.parent.name == "locks"]
    assert any(path.name.startswith("task-idempotency-") for path in lock_files)
    assert any(path.name == "task-record-publication.lock" for path in lock_files)
    for lock_file in (tmp_path / ".aros" / "locks").iterdir():
        assert lock_file.stat().st_mode & 0o777 == 0o600


def test_create_restricts_existing_plain_lock_files_to_mode_0600(
    tmp_path: Path,
) -> None:
    _init_workspace(tmp_path)
    key = "restrict-existing-locks"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    locks_root = tmp_path / ".aros" / "locks"
    locks_root.mkdir()
    lock_paths = (
        locks_root / f"task-idempotency-{digest}.lock",
        locks_root / "task-record-publication.lock",
    )
    for lock_path in lock_paths:
        lock_path.write_bytes(b"")
        lock_path.chmod(0o666)
    service = TaskService(tmp_path)

    _create(service, key=key)

    assert all(path.stat().st_mode & 0o777 == 0o600 for path in lock_paths)


@pytest.mark.parametrize(
    "unsupported_errno",
    sorted(
        {
            errno.EOPNOTSUPP,
            getattr(errno, "ENOTSUP", errno.EOPNOTSUPP),
            errno.ENOSYS,
        }
    ),
)
def test_durable_lock_accepts_unsupported_fchmod_without_losing_integrity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unsupported_errno: int,
) -> None:
    _init_workspace(tmp_path)
    TaskService(tmp_path)
    locks = tmp_path / ".aros" / "locks"
    locks.mkdir()
    lock = locks / "unsupported-mode.lock"
    lock.write_bytes(b"")
    lock.chmod(0o666)
    synced: list[Path] = []
    original_fsync = tasks_module.os.fsync

    def unsupported(_descriptor: int, _mode: int) -> None:
        raise OSError(unsupported_errno, os.strerror(unsupported_errno))

    def record_sync(descriptor: int) -> None:
        synced.append(Path(f"/proc/self/fd/{descriptor}").resolve())
        original_fsync(descriptor)

    monkeypatch.setattr(tasks_module.os, "fchmod", unsupported)
    monkeypatch.setattr(tasks_module.os, "fsync", record_sync)

    tasks_module._ensure_durable_lock_file(lock, "test durable lock")
    with tasks_module.file_lock(lock):
        pass

    assert lock in synced
    assert lock.stat().st_nlink == 1
    assert stat.S_IMODE(lock.stat().st_mode) == 0o666


def test_durable_lock_rejects_other_fchmod_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_workspace(tmp_path)
    TaskService(tmp_path)
    locks = tmp_path / ".aros" / "locks"
    locks.mkdir()
    lock = locks / "denied-mode.lock"
    lock.write_bytes(b"")
    lock.chmod(0o666)

    def denied(_descriptor: int, _mode: int) -> None:
        raise OSError(errno.EPERM, os.strerror(errno.EPERM))

    monkeypatch.setattr(tasks_module.os, "fchmod", denied)

    with pytest.raises(TaskError, match="sync.*durable lock"):
        tasks_module._ensure_durable_lock_file(lock, "test durable lock")

    assert lock.stat().st_nlink == 1
    assert stat.S_IMODE(lock.stat().st_mode) == 0o666


def test_create_rejects_a_hardlinked_lock_before_changing_its_mode(
    tmp_path: Path,
) -> None:
    _init_workspace(tmp_path)
    locks_root = tmp_path / ".aros" / "locks"
    locks_root.mkdir()
    outside = tmp_path / ".git" / "outside-lock"
    outside.write_text("preserve\n", encoding="utf-8")
    outside.chmod(0o640)
    lock = locks_root / "task-record-publication.lock"
    os.link(outside, lock)
    mode_before = outside.stat().st_mode & 0o777
    service = TaskService(tmp_path)

    with pytest.raises(TaskError, match="hardlink|link count"):
        _create(service, key="hardlinked-publication-lock")

    assert outside.read_text(encoding="utf-8") == "preserve\n"
    assert outside.stat().st_nlink == lock.stat().st_nlink == 2
    assert outside.stat().st_mode & 0o777 == mode_before
    assert lock.stat().st_mode & 0o777 == mode_before


@pytest.mark.parametrize("reader", ("status", "list"))
def test_fresh_read_durably_recreates_missing_derived_roots_and_publication_lock(
    tmp_path: Path,
    reader: str,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service, key=f"missing-derived-roots-{reader}")
    task_id = str(brief["task_id"])
    runtime_root = tmp_path / ".aros" / "tasks"
    runtime_task = runtime_root / task_id
    (runtime_task / "status.json").unlink()
    runtime_task.rmdir()
    for index in (runtime_root / "idempotency").iterdir():
        index.unlink()
    (runtime_root / "idempotency").rmdir()
    runtime_root.rmdir()
    locks_root = tmp_path / ".aros" / "locks"
    for lock in locks_root.iterdir():
        lock.unlink()
    locks_root.rmdir()

    fresh = TaskService(tmp_path)
    result = fresh.status(task_id) if reader == "status" else fresh.list()

    if reader == "status":
        assert result["task_id"] == task_id  # type: ignore[index]
    else:
        assert [status["task_id"] for status in result] == [task_id]  # type: ignore[union-attr]
    publication_lock = locks_root / "task-record-publication.lock"
    assert publication_lock.is_file()
    assert publication_lock.stat().st_mode & 0o777 == 0o600
    assert (runtime_root / task_id / "status.json").is_file()


def test_create_rejects_a_noncommit_head_even_when_it_is_40_hex(
    tmp_path: Path,
) -> None:
    _init_workspace(tmp_path)
    blob = _git(tmp_path, "hash-object", "-w", "README.md")
    _git(tmp_path, "update-ref", "refs/tags/blob-head", blob)
    _git(tmp_path, "symbolic-ref", "HEAD", "refs/tags/blob-head")
    assert _git(tmp_path, "rev-parse", "--verify", "HEAD") == blob
    service = TaskService(tmp_path)

    with pytest.raises(TaskError, match="committed 40-hex Git HEAD"):
        _create(service)


@pytest.mark.parametrize("relative", ("tasks", ".aros/tasks", ".aros/locks"))
def test_create_rejects_symlinked_reserved_directories(
    tmp_path: Path,
    relative: str,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    target = tmp_path / "alias-target"
    target.mkdir()
    reserved = tmp_path / relative
    reserved.parent.mkdir(parents=True, exist_ok=True)
    reserved.symlink_to(target, target_is_directory=True)

    with pytest.raises(TaskError, match="symlink|plain directory"):
        _create(service)

    assert list(target.iterdir()) == []


@pytest.mark.parametrize("relative", (".aros", "AROS.md", "memory/NOW.md"))
def test_service_rejects_symlinked_workspace_control_paths(
    tmp_path: Path,
    relative: str,
) -> None:
    _init_workspace(tmp_path)
    control = tmp_path / relative
    if control.is_dir():
        control.rmdir()
        target = tmp_path / "control-target"
        target.mkdir()
        control.symlink_to(target, target_is_directory=True)
    else:
        control.unlink()
        target = tmp_path / "control-target"
        target.write_text("control\n", encoding="utf-8")
        control.symlink_to(target)

    with pytest.raises(TaskError, match="symlink|not initialized"):
        TaskService(tmp_path)


@pytest.mark.parametrize("kind", ("versioned", "runtime"))
def test_create_rejects_preexisting_task_directories_before_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    task_id = "TASK-20260802-conflict"
    monkeypatch.setattr(service, "_new_task_id", lambda _objective: task_id)
    versioned = tmp_path / "tasks" / task_id
    runtime = tmp_path / ".aros" / "tasks" / task_id
    conflict = versioned if kind == "versioned" else runtime
    conflict.mkdir(parents=True)

    with pytest.raises(TaskError, match="conflict|already exists"):
        _create(service)

    assert not (versioned / "brief.json").exists()
    assert not (runtime / "status.json").exists()


def test_new_task_id_uses_a_64_bit_lowercase_hex_suffix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    token_sizes: list[int] = []

    def token_hex(size: int) -> str:
        token_sizes.append(size)
        return "ab" * size

    monkeypatch.setattr(tasks_module.secrets, "token_hex", token_hex)

    task_id = service._new_task_id("  Keep Random Label  ")

    assert token_sizes == [8]
    assert re.fullmatch(
        r"TASK-\d{8}-keep-random-label-[0-9a-f]{16}",
        task_id,
    )


@pytest.mark.parametrize("kind", ("versioned", "runtime", "staging"))
def test_create_retries_a_preexisting_candidate_without_changing_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    first = "TASK-20260803-bounded-objective-0000000000000001"
    second = "TASK-20260803-bounded-objective-0000000000000002"
    if kind == "staging":
        staging = tmp_path / "tasks" / ".staging"
        staging.mkdir(parents=True)
        conflict = staging / first
        conflict_bytes = b"preserve conflicting task material\n"
        conflict.write_bytes(conflict_bytes)
    else:
        monkeypatch.setattr(service, "_new_task_id", lambda _objective: first)
        _create(service, key=f"preexisting-{kind}")
        conflict = (
            tmp_path / "tasks" / first / "brief.json"
            if kind == "versioned"
            else tmp_path / ".aros" / "tasks" / first / "status.json"
        )
        conflict_bytes = conflict.read_bytes()
    task_ids = iter((first, second))
    monkeypatch.setattr(service, "_new_task_id", lambda _objective: next(task_ids))

    brief = _create(service)

    assert brief["task_id"] == second
    assert conflict.read_bytes() == conflict_bytes
    assert (tmp_path / "tasks" / second / "brief.json").is_file()
    assert (tmp_path / ".aros" / "tasks" / second / "status.json").is_file()


def test_create_fails_closed_after_bounded_task_id_conflicts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    task_ids = [
        f"TASK-20260803-bounded-objective-{attempt:016x}"
        for attempt in range(8)
    ]
    staging = tmp_path / "tasks" / ".staging"
    staging.mkdir(parents=True)
    conflicts = {
        staging / task_id: f"conflict {task_id}\n".encode()
        for task_id in task_ids
    }
    for path, content in conflicts.items():
        path.write_bytes(content)
    generated: list[str] = []
    candidates = iter(task_ids)

    def new_task_id(_objective: str) -> str:
        candidate = next(candidates)
        generated.append(candidate)
        return candidate

    monkeypatch.setattr(service, "_new_task_id", new_task_id)

    with pytest.raises(TaskError, match="conflict|unique|allocate"):
        _create(service)

    assert generated == task_ids
    assert all(path.read_bytes() == content for path, content in conflicts.items())
    assert not list((tmp_path / "tasks").glob("TASK-*"))
    runtime = tmp_path / ".aros" / "tasks"
    assert not [path for path in runtime.iterdir() if path.name != "idempotency"]
    assert not list((runtime / "idempotency").iterdir())


@pytest.mark.parametrize("kind", ("versioned", "runtime"))
def test_create_rejects_symlinked_task_directories_without_following_them(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    task_id = "TASK-20260802-symlink"
    monkeypatch.setattr(service, "_new_task_id", lambda _objective: task_id)
    versioned = tmp_path / "tasks" / task_id
    runtime = tmp_path / ".aros" / "tasks" / task_id
    link = versioned if kind == "versioned" else runtime
    link.parent.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "task-alias-target"
    target.mkdir()
    link.symlink_to(target, target_is_directory=True)

    with pytest.raises(TaskError, match="symlink|conflict|already exists"):
        _create(service)

    assert list(target.iterdir()) == []


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("hash", "brief hash"),
        ("identity", "brief identity"),
        ("request_hash", "request hash"),
        ("extra_field", "brief schema"),
        ("base_commit", "base_commit"),
        ("capabilities", "capabilities"),
    ),
)
def test_status_strictly_validates_brief_readback(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    created = _create(service)
    task_id = str(created["task_id"])
    brief_path = tmp_path / "tasks" / task_id / "brief.json"
    status_path = tmp_path / ".aros" / "tasks" / task_id / "status.json"
    brief = json.loads(brief_path.read_text(encoding="utf-8"))
    status = json.loads(status_path.read_text(encoding="utf-8"))

    if mutation == "hash":
        brief["objective"] = "tampered objective"
    elif mutation == "identity":
        brief["task_id"] = "TASK-20260802-other"
        _rehash_brief(brief)
    elif mutation == "request_hash":
        brief["request_sha256"] = "0" * 64
        _rehash_brief(brief)
        status["brief_sha256"] = brief["brief_sha256"]
    elif mutation == "extra_field":
        brief["unexpected"] = True
        _rehash_brief(brief)
        status["brief_sha256"] = brief["brief_sha256"]
    elif mutation == "base_commit":
        brief["base_commit"] = "not-a-commit"
        _rehash_brief(brief)
        status["brief_sha256"] = brief["brief_sha256"]
    elif mutation == "capabilities":
        brief["capabilities"] = {"network": 0, "shell": True}
        brief["request_sha256"] = json_sha256(_request_from_brief(brief))
        _rehash_brief(brief)
        status["brief_sha256"] = brief["brief_sha256"]
    else:  # pragma: no cover - parametrization is exhaustive
        raise AssertionError(mutation)
    atomic_write_json(brief_path, brief)
    atomic_write_json(status_path, status)

    with pytest.raises(TaskError, match=message):
        service.status(task_id)


def test_status_normalizes_a_lone_surrogate_in_a_tampered_brief(
    tmp_path: Path,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service)
    task_id = str(brief["task_id"])
    brief_path = tmp_path / "tasks" / task_id / "brief.json"
    brief["objective"] = "\ud800"
    brief_path.write_text(
        json.dumps(brief, ensure_ascii=True, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(TaskError, match="UTF-8"):
        service.status(task_id)


@pytest.mark.parametrize(
    ("mutate", "message"),
    (
        (lambda status: status.update(unexpected=True), "status schema"),
        (lambda status: status.update(state="running"), "task status"),
        (
            lambda status: status.update(task_id="TASK-20260802-other"),
            "status identity",
        ),
        (lambda status: status.update(brief_sha256="0" * 64), "brief hash"),
    ),
)
def test_status_strictly_validates_runtime_readback(
    tmp_path: Path,
    mutate: Callable[[dict[str, object]], None],
    message: str,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service)
    task_id = str(brief["task_id"])
    status_path = tmp_path / ".aros" / "tasks" / task_id / "status.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    mutate(status)
    atomic_write_json(status_path, status)

    with pytest.raises(TaskError, match=message):
        service.status(task_id)


@pytest.mark.parametrize("record", ("brief", "status"))
def test_status_rejects_symlinked_record_files(
    tmp_path: Path,
    record: str,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service)
    task_id = str(brief["task_id"])
    path = (
        tmp_path / "tasks" / task_id / "brief.json"
        if record == "brief"
        else tmp_path / ".aros" / "tasks" / task_id / "status.json"
    )
    target = tmp_path / f"{record}-alias-target.json"
    path.rename(target)
    path.symlink_to(target)

    with pytest.raises(TaskError, match="symlink|plain file"):
        service.status(task_id)


@pytest.mark.parametrize("relative", ("tasks", ".aros/tasks", ".aros"))
def test_status_rejects_a_symlinked_record_parent_after_construction(
    tmp_path: Path,
    relative: str,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service)
    task_id = str(brief["task_id"])
    parent = tmp_path / relative
    target = tmp_path / f"{relative.replace('/', '-')}-parent-target"
    parent.rename(target)
    parent.symlink_to(target, target_is_directory=True)

    with pytest.raises(TaskError, match="symlink|plain directory"):
        service.status(task_id)


def test_status_rejects_a_noncanonical_task_id_before_path_access(
    tmp_path: Path,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)

    with pytest.raises(TaskError, match="invalid task ID"):
        service.status("TASK-20260802-trailing-")


def test_status_rejects_non_ascii_task_id_date_digits_before_path_access(
    tmp_path: Path,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)

    with pytest.raises(TaskError, match="invalid task ID"):
        service.status("TASK-２０２６０８０２-child")


def test_status_rejects_a_noncanonical_brief_timestamp(
    tmp_path: Path,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service)
    task_id = str(brief["task_id"])
    brief_path = tmp_path / "tasks" / task_id / "brief.json"
    status_path = tmp_path / ".aros" / "tasks" / task_id / "status.json"
    brief["created_at"] = "Z"
    _rehash_brief(brief)
    status = json.loads(status_path.read_text(encoding="utf-8"))
    status["brief_sha256"] = brief["brief_sha256"]
    status["updated_at"] = "Z"
    atomic_write_json(brief_path, brief)
    atomic_write_json(status_path, status)

    with pytest.raises(TaskError, match="created_at.*UTC timestamp"):
        service.status(task_id)


def test_status_rejects_a_calendar_invalid_brief_timestamp(
    tmp_path: Path,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    key = "calendar-invalid-timestamp"
    brief = _create(service, key=key)
    task_id = str(brief["task_id"])
    brief_path = tmp_path / "tasks" / task_id / "brief.json"
    status_path = tmp_path / ".aros" / "tasks" / task_id / "status.json"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    index_path = tmp_path / ".aros" / "tasks" / "idempotency" / f"{digest}.json"
    invalid_timestamp = "2026-02-31T12:00:00.000Z"
    brief["created_at"] = invalid_timestamp
    _rehash_brief(brief)
    status = json.loads(status_path.read_text(encoding="utf-8"))
    status["brief_sha256"] = brief["brief_sha256"]
    status["updated_at"] = invalid_timestamp
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["brief_sha256"] = brief["brief_sha256"]
    index["created_at"] = invalid_timestamp
    atomic_write_json(brief_path, brief)
    atomic_write_json(status_path, status)
    atomic_write_json(index_path, index)

    with pytest.raises(TaskError, match="created_at.*UTC timestamp"):
        service.status(task_id)


def test_idempotency_index_is_strict_and_contains_no_plaintext_key(
    tmp_path: Path,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    key = "private-stable-key"
    brief = _create(service, key=key)
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    index_path = tmp_path / ".aros" / "tasks" / "idempotency" / f"{digest}.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))

    assert set(index) == {
        "schema_version",
        "idempotency_key_sha256",
        "request_sha256",
        "task_id",
        "brief_sha256",
        "created_at",
    }
    assert index["idempotency_key_sha256"] == digest
    assert index["request_sha256"] == brief["request_sha256"]
    assert index["brief_sha256"] == brief["brief_sha256"]
    assert key not in index_path.name
    assert key not in json.dumps(index, sort_keys=True)


@pytest.mark.parametrize("reader", ("status", "list"))
@pytest.mark.parametrize("problem", ("missing", "tampered", "conflicting"))
def test_task_readback_requires_a_strictly_bound_idempotency_index(
    tmp_path: Path,
    reader: str,
    problem: str,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    key = "readback-index-authority"
    brief = _create(service, key=key)
    task_id = str(brief["task_id"])
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    index_path = tmp_path / ".aros" / "tasks" / "idempotency" / f"{digest}.json"
    if problem == "missing":
        index_path.unlink()
    elif problem == "tampered":
        index = json.loads(index_path.read_text(encoding="utf-8"))
        index["unexpected"] = True
        atomic_write_json(index_path, index)
    else:
        brief_path = tmp_path / "tasks" / task_id / "brief.json"
        status_path = tmp_path / ".aros" / "tasks" / task_id / "status.json"
        status = json.loads(status_path.read_text(encoding="utf-8"))
        brief["base_commit"] = "0" * 40
        _rehash_brief(brief)
        status["brief_sha256"] = brief["brief_sha256"]
        atomic_write_json(brief_path, brief)
        atomic_write_json(status_path, status)

    if problem == "missing":
        result = service.status(task_id) if reader == "status" else service.list()
        if reader == "status":
            assert result["task_id"] == task_id  # type: ignore[index]
        else:
            assert [status["task_id"] for status in result] == [task_id]  # type: ignore[union-attr]
    else:
        with pytest.raises(TaskError, match="idempotency index"):
            service.status(task_id) if reader == "status" else service.list()


@pytest.mark.parametrize("reader", ("status", "list"))
def test_fresh_read_recovers_immediately_after_brief_authority_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reader: str,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    original_publish = TaskService._publish_staged_brief

    class InjectedInterruption(RuntimeError):
        pass

    def interrupt_after_publication(
        self: TaskService,
        staging: Path,
        target: Path,
    ) -> None:
        original_publish(self, staging, target)
        raise InjectedInterruption

    monkeypatch.setattr(
        TaskService,
        "_publish_staged_brief",
        interrupt_after_publication,
    )
    with pytest.raises(InjectedInterruption):
        _create(service, key=f"crash-before-derived-{reader}")
    monkeypatch.setattr(TaskService, "_publish_staged_brief", original_publish)
    task_directories = sorted((tmp_path / "tasks").glob("TASK-*"))
    assert len(task_directories) == 1
    task_id = task_directories[0].name
    assert not (tmp_path / ".aros" / "tasks" / task_id).exists()

    fresh = TaskService(tmp_path)
    result = fresh.status(task_id) if reader == "status" else fresh.list()

    if reader == "status":
        assert result["task_id"] == task_id  # type: ignore[index]
    else:
        assert [status["task_id"] for status in result] == [task_id]  # type: ignore[union-attr]


@pytest.mark.parametrize("mutation", ("extra", "key_hash", "brief_hash"))
def test_create_rejects_a_tampered_idempotency_index(
    tmp_path: Path,
    mutation: str,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    key = "tamper-index"
    _create(service, key=key)
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    index_path = tmp_path / ".aros" / "tasks" / "idempotency" / f"{digest}.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    if mutation == "extra":
        index["unexpected"] = True
    elif mutation == "key_hash":
        index["idempotency_key_sha256"] = "0" * 64
    else:
        index["brief_sha256"] = "0" * 64
    atomic_write_json(index_path, index)

    with pytest.raises(TaskError, match="idempotency index"):
        _create(service, key=key)


def test_list_is_sorted_and_rejects_unrecognized_task_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    task_ids = iter(("TASK-20260802-zeta", "TASK-20260802-alpha"))
    monkeypatch.setattr(service, "_new_task_id", lambda _objective: next(task_ids))
    zeta = _create(service, key="zeta")
    alpha = _create(service, key="alpha")

    assert service.list() == [
        service.status(str(alpha["task_id"])),
        service.status(str(zeta["task_id"])),
    ]

    (tmp_path / "tasks" / "unrecognized").mkdir()
    with pytest.raises(TaskError, match="unrecognized task entry"):
        service.list()


def test_message_appends_a_strict_create_once_hash_chain(tmp_path: Path) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service)
    task_id = str(brief["task_id"])

    first = service.message(task_id, "  inspect the failing seed  ", "  principal  ")
    second = service.message(task_id, "record the exact output", "principal")

    messages = tmp_path / ".aros" / "tasks" / task_id / "messages"
    assert sorted(path.name for path in messages.iterdir()) == [
        "00000000000000000001.json",
        "00000000000000000002.json",
    ]
    assert first == json.loads(
        (messages / "00000000000000000001.json").read_text(encoding="utf-8")
    )
    assert second == json.loads(
        (messages / "00000000000000000002.json").read_text(encoding="utf-8")
    )
    assert set(first) == {
        "schema_version",
        "task_id",
        "sequence",
        "actor",
        "text",
        "created_at",
        "previous_message_sha256",
        "message_sha256",
    }
    assert first["schema_version"] == 1
    assert first["task_id"] == task_id
    assert first["sequence"] == 1
    assert first["actor"] == "principal"
    assert first["text"] == "inspect the failing seed"
    assert first["previous_message_sha256"] is None
    assert first["message_sha256"] == json_sha256(
        {key: value for key, value in first.items() if key != "message_sha256"}
    )
    assert second["sequence"] == 2
    assert second["previous_message_sha256"] == first["message_sha256"]
    assert second["message_sha256"] == json_sha256(
        {key: value for key, value in second.items() if key != "message_sha256"}
    )
    assert not ({"delivered", "acknowledged", "steered"} & set(second))


def test_message_accepts_single_link_mode_normalization_when_unenforced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_workspace(tmp_path)
    evidence = _normalized_permission_probe(
        device=(tmp_path / ".aros").stat().st_dev
    )
    monkeypatch.setattr(
        tasks_module,
        "_probe_filesystem_permissions",
        lambda _runtime: dict(evidence),
    )
    service = TaskService(tmp_path)
    brief = _create(service, key="normalized-message")
    task_id = str(brief["task_id"])
    first = service.message(task_id, "first", "principal")
    path = (
        tmp_path
        / ".aros"
        / "tasks"
        / task_id
        / "messages"
        / "00000000000000000001.json"
    )
    path.chmod(0o666)

    second = service.message(task_id, "second", "principal")

    assert first["message_sha256"] == second["previous_message_sha256"]
    assert path.lstat().st_nlink == 1
    assert stat.S_IMODE(path.lstat().st_mode) == 0o666


def test_message_rejects_mode_drift_when_permissions_are_enforced(
    tmp_path: Path,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service, key="strict-message-mode")
    task_id = str(brief["task_id"])
    service.message(task_id, "first", "principal")
    path = (
        tmp_path
        / ".aros"
        / "tasks"
        / task_id
        / "messages"
        / "00000000000000000001.json"
    )
    path.chmod(0o666)

    with pytest.raises(TaskError, match="message.*restrictive|permission|mode"):
        service.message(task_id, "must not append", "principal")


def test_concurrent_messages_are_contiguous_and_each_created_once(
    tmp_path: Path,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service)
    task_id = str(brief["task_id"])

    with ThreadPoolExecutor(max_workers=8) as pool:
        records = list(
            pool.map(
                lambda number: service.message(
                    task_id,
                    f"message {number}",
                    f"actor-{number}",
                ),
                range(24),
            )
        )

    ordered = sorted(records, key=lambda record: int(record["sequence"]))
    assert [record["sequence"] for record in ordered] == list(range(1, 25))
    assert {record["text"] for record in ordered} == {
        f"message {number}" for number in range(24)
    }
    previous = None
    for record in ordered:
        assert record["previous_message_sha256"] == previous
        previous = record["message_sha256"]
    message_paths = sorted(
        (tmp_path / ".aros" / "tasks" / task_id / "messages").iterdir()
    )
    assert [path.name for path in message_paths] == [
        f"{sequence:020d}.json" for sequence in range(1, 25)
    ]


@pytest.mark.parametrize("problem", ("tamper", "gap", "symlink", "unknown"))
def test_message_rejects_an_ambiguous_existing_chain_without_appending(
    tmp_path: Path,
    problem: str,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service)
    task_id = str(brief["task_id"])
    service.message(task_id, "first", "principal")
    messages = tmp_path / ".aros" / "tasks" / task_id / "messages"
    first = messages / "00000000000000000001.json"
    if problem == "tamper":
        record = json.loads(first.read_text(encoding="utf-8"))
        record["text"] = "changed"
        atomic_write_json(first, record)
    elif problem == "gap":
        first.rename(messages / "00000000000000000002.json")
    elif problem == "symlink":
        target = tmp_path / "message-target.json"
        first.rename(target)
        first.symlink_to(target)
    else:
        (messages / "README").write_text("unknown\n", encoding="utf-8")

    with pytest.raises(TaskError, match="message"):
        service.message(task_id, "must not append", "principal")

    assert not (messages / "00000000000000000003.json").exists()


@pytest.mark.parametrize(
    ("text", "actor"),
    (("", "principal"), ("message", "  "), ("\ud800", "principal")),
)
def test_message_rejects_noncanonical_external_text_without_creating_a_mailbox(
    tmp_path: Path,
    text: str,
    actor: str,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service)
    task_id = str(brief["task_id"])

    with pytest.raises(TaskError, match="message|actor|UTF-8"):
        service.message(task_id, text, actor)

    assert not (tmp_path / ".aros" / "tasks" / task_id / "messages").exists()


def test_collect_records_the_reviewed_b_c_r_protocol_without_assimilation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, brief, ownership, final = _create_terminal_task(tmp_path)
    task_id = str(brief["task_id"])
    returned, child_commit, return_commit = _commit_child_return(
        tmp_path,
        brief,
        ownership,
    )
    parent_head = _git(tmp_path, "rev-parse", "HEAD")
    refs_before = _git(tmp_path, "show-ref")
    semantic_before = {
        path: (tmp_path / path).read_bytes() for path in ("AROS.md", "memory/NOW.md")
    }
    child_return_path = (
        Path(str(ownership["worktree_path"])) / "tasks" / task_id / "return.json"
    )
    original_read_object = tasks_module._read_object

    def reject_mutable_return(path: Path, description: str) -> dict[str, object]:
        if path == child_return_path:
            raise AssertionError("collect read mutable child return")
        return original_read_object(path, description)

    monkeypatch.setattr(tasks_module, "_read_object", reject_mutable_return)

    collected = service.collect(task_id)

    collected_path = tmp_path / "tasks" / task_id / "collected.json"
    assert collected == json.loads(collected_path.read_text(encoding="utf-8"))
    assert set(collected) == {
        "schema_version",
        "task_id",
        "brief_sha256",
        "ownership_sha256",
        "branch_ref",
        "base_commit",
        "child_commit",
        "return_commit",
        "final_state",
        "final_sha256",
        "return",
        "collected_at",
        "collected_sha256",
    }
    assert collected["schema_version"] == 1
    assert collected["task_id"] == task_id
    assert collected["brief_sha256"] == brief["brief_sha256"]
    assert collected["ownership_sha256"] == ownership["ownership_sha256"]
    assert collected["branch_ref"] == f"refs/heads/{ownership['branch']}"
    assert collected["base_commit"] == brief["base_commit"]
    assert collected["child_commit"] == child_commit
    assert collected["return_commit"] == return_commit
    assert collected["final_state"] == "completed"
    assert collected["final_sha256"] == final["final_sha256"]
    assert collected["return"] == returned
    assert collected["collected_sha256"] == json_sha256(
        {key: value for key, value in collected.items() if key != "collected_sha256"}
    )
    assert "patch" not in collected
    assert _git(tmp_path, "rev-parse", "HEAD") == parent_head
    assert _git(tmp_path, "show-ref") == refs_before
    assert not (tmp_path / "alpha.txt").exists()
    assert not (tmp_path / "zeta.txt").exists()
    assert {
        path: (tmp_path / path).read_bytes() for path in semantic_before
    } == semantic_before
    first_identity = collected_path.stat().st_ino

    assert service.collect(task_id) == collected
    assert collected_path.stat().st_ino == first_identity


def test_collect_derives_utf8_byte_sorted_changed_files_from_b_to_c(
    tmp_path: Path,
) -> None:
    service, brief, ownership, _final = _create_terminal_task(tmp_path)
    task_id = str(brief["task_id"])
    returned, _child_commit, _return_commit = _commit_child_return(
        tmp_path,
        brief,
        ownership,
        changed_files=["é.txt", "z.txt", "nested/a.txt"],
    )

    collected = service.collect(task_id)

    assert returned["changed_files"] == ["nested/a.txt", "z.txt", "é.txt"]
    assert collected["return"]["changed_files"] == returned["changed_files"]  # type: ignore[index]


@pytest.mark.parametrize("mutation", ("order", "extra", "missing"))
def test_collect_rejects_return_changed_files_that_do_not_exactly_match_b_to_c(
    tmp_path: Path,
    mutation: str,
) -> None:
    service, brief, ownership, _final = _create_terminal_task(tmp_path)
    task_id = str(brief["task_id"])
    returned, child_commit, _return_commit = _commit_child_return(
        tmp_path,
        brief,
        ownership,
    )
    worktree = Path(str(ownership["worktree_path"]))
    _git(worktree, "reset", "--soft", child_commit)
    if mutation == "order":
        returned["changed_files"] = list(reversed(returned["changed_files"]))  # type: ignore[arg-type]
    elif mutation == "extra":
        returned["changed_files"] = [*returned["changed_files"], "return.json"]  # type: ignore[misc]
    else:
        returned["changed_files"] = returned["changed_files"][:1]  # type: ignore[index]
    returned["return_sha256"] = json_sha256(
        {key: value for key, value in returned.items() if key != "return_sha256"}
    )
    atomic_write_json(worktree / "tasks" / task_id / "return.json", returned)
    _git(worktree, "add", "--", f"tasks/{task_id}/return.json")
    _git(worktree, "commit", "-qm", "record invalid child return")

    with pytest.raises(TaskError, match="changed_files"):
        service.collect(task_id)

    assert not (tmp_path / "tasks" / task_id / "collected.json").exists()


def test_collect_returns_deterministic_completed_no_return_without_inventing_one(
    tmp_path: Path,
) -> None:
    service, brief, ownership, final = _create_terminal_task(tmp_path)
    task_id = str(brief["task_id"])
    worktree = Path(str(ownership["worktree_path"]))
    (worktree / "artifact.txt").write_text("unreturned artifact\n", encoding="utf-8")
    _git(worktree, "add", "artifact.txt")
    _git(worktree, "commit", "-qm", "produce unreturned artifact")
    head_commit = _git(worktree, "rev-parse", "HEAD")

    result = service.collect(task_id)

    assert result == {
        "schema_version": 1,
        "task_id": task_id,
        "state": "completed_no_return",
        "brief_sha256": brief["brief_sha256"],
        "ownership_sha256": ownership["ownership_sha256"],
        "branch_ref": f"refs/heads/{ownership['branch']}",
        "base_commit": brief["base_commit"],
        "head_commit": head_commit,
        "final_state": "completed",
        "final_sha256": final["final_sha256"],
    }
    assert service.collect(task_id) == result
    assert not (tmp_path / "tasks" / task_id / "collected.json").exists()
    assert not (worktree / "tasks" / task_id / "return.json").exists()


@pytest.mark.parametrize("state", ("failed_process", "timed_out"))
def test_noncompleted_terminal_task_requires_a_valid_return(
    tmp_path: Path,
    state: str,
) -> None:
    service, brief, _ownership, _final = _create_terminal_task(
        tmp_path,
        state=state,
    )
    task_id = str(brief["task_id"])

    with pytest.raises(TaskError, match="return"):
        service.collect(task_id)

    assert not (tmp_path / "tasks" / task_id / "collected.json").exists()


@pytest.mark.parametrize("state", ("failed_process", "timed_out"))
def test_noncompleted_terminal_task_can_collect_an_explicit_valid_return(
    tmp_path: Path,
    state: str,
) -> None:
    service, brief, ownership, final = _create_terminal_task(
        tmp_path,
        state=state,
    )
    task_id = str(brief["task_id"])
    _commit_child_return(tmp_path, brief, ownership)

    collected = service.collect(task_id)

    assert collected["final_state"] == state
    assert collected["final_sha256"] == final["final_sha256"]


def test_read_only_task_can_return_without_an_artifact_commit(tmp_path: Path) -> None:
    service, brief, ownership, _final = _create_terminal_task(
        tmp_path,
        mode="read_only",
    )
    task_id = str(brief["task_id"])
    returned, child_commit, return_commit = _commit_child_return(
        tmp_path,
        brief,
        ownership,
        changed_files=[],
    )

    collected = service.collect(task_id)

    assert child_commit == brief["base_commit"]
    assert returned["changed_files"] == []
    assert collected["child_commit"] == child_commit
    assert collected["return_commit"] == return_commit


def test_collect_rejects_active_and_lost_process_states(tmp_path: Path) -> None:
    active_root = tmp_path / "active"
    _init_workspace(active_root)
    active_service = TaskService(active_root)
    active_brief = _create(active_service)
    _commit_brief(active_root, active_brief)
    active_service._ensure_worktree(str(active_brief["task_id"]))

    with pytest.raises(TaskError, match="not terminal"):
        active_service.collect(str(active_brief["task_id"]))

    lost_root = tmp_path / "lost"
    lost_service, lost_brief, _ownership, _final = _create_terminal_task(lost_root)
    lost_id = str(lost_brief["task_id"])
    runtime = lost_root / ".aros" / "tasks" / lost_id
    (runtime / "final.json").unlink()
    (runtime / "status.json").unlink()
    launch_path = runtime / "launch.json"
    launch = json.loads(launch_path.read_text(encoding="utf-8"))
    launch["launched_at"] = "2000-01-01T00:00:00.000Z"
    launch["launch_sha256"] = json_sha256(
        {key: value for key, value in launch.items() if key != "launch_sha256"}
    )
    atomic_write_json(launch_path, launch)

    with pytest.raises(TaskError, match="not terminal"):
        lost_service.collect(lost_id)


def test_collect_rejects_a_hardlinked_final_receipt_as_not_create_once(
    tmp_path: Path,
) -> None:
    service, brief, ownership, _final = _create_terminal_task(tmp_path)
    task_id = str(brief["task_id"])
    _commit_child_return(tmp_path, brief, ownership)
    final_path = tmp_path / ".aros" / "tasks" / task_id / "final.json"
    os.link(final_path, tmp_path / "final-alias.json")

    with pytest.raises(TaskError, match="final.*restrictive|create-once"):
        service.collect(task_id)

    assert not (tmp_path / "tasks" / task_id / "collected.json").exists()


@pytest.mark.parametrize(
    "mutation",
    ("extra_field", "empty_summary", "wrong_list", "hash", "lineage"),
)
def test_collect_rejects_a_malformed_or_tampered_return_blob(
    tmp_path: Path,
    mutation: str,
) -> None:
    service, brief, ownership, _final = _create_terminal_task(tmp_path)
    task_id = str(brief["task_id"])
    returned, child_commit, _return_commit = _commit_child_return(
        tmp_path,
        brief,
        ownership,
    )
    worktree = Path(str(ownership["worktree_path"]))
    _git(worktree, "reset", "--soft", child_commit)
    if mutation == "extra_field":
        returned["unexpected"] = True
    elif mutation == "empty_summary":
        returned["summary"] = "  "
    elif mutation == "wrong_list":
        returned["evidence"] = "not-a-list"
    elif mutation == "hash":
        returned["return_sha256"] = "0" * 64
    else:
        returned["child_commit"] = str(brief["base_commit"])
    if mutation != "hash":
        returned["return_sha256"] = json_sha256(
            {key: value for key, value in returned.items() if key != "return_sha256"}
        )
    atomic_write_json(worktree / "tasks" / task_id / "return.json", returned)
    _git(worktree, "add", "--", f"tasks/{task_id}/return.json")
    _git(worktree, "commit", "-qm", "record malformed child return")

    with pytest.raises(TaskError, match="return"):
        service.collect(task_id)

    assert not (tmp_path / "tasks" / task_id / "collected.json").exists()


@pytest.mark.parametrize("topology", ("extra_path", "merge_commit"))
def test_collect_rejects_a_non_return_only_or_multiparent_r(
    tmp_path: Path,
    topology: str,
) -> None:
    service, brief, ownership, _final = _create_terminal_task(tmp_path)
    task_id = str(brief["task_id"])
    _returned, child_commit, return_commit = _commit_child_return(
        tmp_path,
        brief,
        ownership,
    )
    worktree = Path(str(ownership["worktree_path"]))
    if topology == "extra_path":
        _git(worktree, "reset", "--soft", child_commit)
        (worktree / "extra.txt").write_text("not return metadata\n", encoding="utf-8")
        _git(
            worktree,
            "add",
            "--",
            "extra.txt",
            f"tasks/{task_id}/return.json",
        )
        _git(worktree, "commit", "-qm", "return plus extra path")
    else:
        branch = str(ownership["branch"])
        _git(worktree, "checkout", "-qb", "return-side", child_commit)
        (worktree / "side.txt").write_text("side\n", encoding="utf-8")
        _git(worktree, "add", "side.txt")
        _git(worktree, "commit", "-qm", "side parent")
        _git(worktree, "checkout", "-q", branch)
        assert _git(worktree, "rev-parse", "HEAD") == return_commit
        _git(worktree, "merge", "--no-ff", "-qm", "multiparent return", "return-side")

    with pytest.raises(TaskError, match="one parent|only"):
        service.collect(task_id)

    assert not (tmp_path / "tasks" / task_id / "collected.json").exists()


def test_collect_rejects_a_return_symlink_whose_target_is_strict_json(
    tmp_path: Path,
) -> None:
    service, brief, ownership, _final = _create_terminal_task(tmp_path)
    task_id = str(brief["task_id"])
    returned, child_commit, _return_commit = _commit_child_return(
        tmp_path,
        brief,
        ownership,
    )
    worktree = Path(str(ownership["worktree_path"]))
    relative = f"tasks/{task_id}/return.json"
    return_path = worktree / relative
    _git(worktree, "reset", "--soft", child_commit)
    return_path.unlink()
    target = json.dumps(
        returned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return_path.symlink_to(target)
    _git(worktree, "add", "--", relative)
    _git(worktree, "commit", "-qm", "record symlink child return")
    return_commit = _git(worktree, "rev-parse", "HEAD")

    assert return_path.is_symlink()
    assert json.loads(os.readlink(return_path)) == returned
    assert _git(worktree, "ls-tree", return_commit, "--", relative).startswith(
        "120000 blob "
    )

    with pytest.raises(TaskError, match="return.*regular|return.*mode"):
        service.collect(task_id)

    assert not (tmp_path / "tasks" / task_id / "collected.json").exists()


@pytest.mark.parametrize("material", ("modified", "staged", "untracked", "ignored"))
def test_collect_rejects_and_preserves_all_dirty_child_material(
    tmp_path: Path,
    material: str,
) -> None:
    service, brief, ownership, _final = _create_terminal_task(tmp_path)
    task_id = str(brief["task_id"])
    _commit_child_return(tmp_path, brief, ownership)
    worktree = Path(str(ownership["worktree_path"]))
    if material in {"modified", "staged"}:
        dirty_path = worktree / "alpha.txt"
        dirty_path.write_text("dirty tracked bytes\n", encoding="utf-8")
        if material == "staged":
            _git(worktree, "add", "alpha.txt")
    elif material == "untracked":
        dirty_path = worktree / "untracked.txt"
        dirty_path.write_text("untracked bytes\n", encoding="utf-8")
    else:
        dirty_path = worktree / ".aros" / "ignored.txt"
        dirty_path.parent.mkdir(parents=True, exist_ok=True)
        dirty_path.write_text("ignored bytes\n", encoding="utf-8")

    with pytest.raises(TaskError, match="clean"):
        service.collect(task_id)

    assert dirty_path.exists()
    assert not (tmp_path / "tasks" / task_id / "collected.json").exists()


def test_collect_rechecks_terminal_head_and_clean_state_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, brief, ownership, _final = _create_terminal_task(tmp_path)
    task_id = str(brief["task_id"])
    _commit_child_return(tmp_path, brief, ownership)
    worktree = Path(str(ownership["worktree_path"]))
    original_snapshot = service._collection_snapshot
    calls = 0

    def race(loaded_brief: dict[str, object]) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 2:
            (worktree / "raced.txt").write_text("raced bytes\n", encoding="utf-8")
        return original_snapshot(loaded_brief)

    monkeypatch.setattr(service, "_collection_snapshot", race)

    with pytest.raises(TaskError, match="clean"):
        service.collect(task_id)

    assert calls == 2
    assert (worktree / "raced.txt").exists()
    assert not (tmp_path / "tasks" / task_id / "collected.json").exists()


@pytest.mark.parametrize("tamper", ("hash", "valid_conflict", "symlink"))
def test_existing_collection_rejects_tamper_or_conflict(
    tmp_path: Path,
    tamper: str,
) -> None:
    service, brief, ownership, _final = _create_terminal_task(tmp_path)
    task_id = str(brief["task_id"])
    _commit_child_return(tmp_path, brief, ownership)
    service.collect(task_id)
    collected_path = tmp_path / "tasks" / task_id / "collected.json"
    if tamper == "symlink":
        target = tmp_path / "collected-target.json"
        collected_path.rename(target)
        collected_path.symlink_to(target)
    else:
        collected = json.loads(collected_path.read_text(encoding="utf-8"))
        collected["child_commit"] = "0" * 40
        if tamper == "valid_conflict":
            collected["collected_sha256"] = json_sha256(
                {
                    key: value
                    for key, value in collected.items()
                    if key != "collected_sha256"
                }
            )
        atomic_write_json(collected_path, collected)

    with pytest.raises(TaskError, match="collection|symlink|plain file"):
        service.collect(task_id)


def test_existing_collection_rejects_a_hardlink_as_not_create_once(
    tmp_path: Path,
) -> None:
    service, brief, ownership, _final = _create_terminal_task(tmp_path)
    task_id = str(brief["task_id"])
    _commit_child_return(tmp_path, brief, ownership)
    service.collect(task_id)
    collected_path = tmp_path / "tasks" / task_id / "collected.json"
    os.link(collected_path, tmp_path / "collected-alias.json")

    with pytest.raises(TaskError, match="collection.*plain file|create-once"):
        service.collect(task_id)


def test_existing_valid_collection_is_idempotent_after_child_becomes_dirty(
    tmp_path: Path,
) -> None:
    service, brief, ownership, _final = _create_terminal_task(tmp_path)
    task_id = str(brief["task_id"])
    _commit_child_return(tmp_path, brief, ownership)
    collected = service.collect(task_id)
    worktree = Path(str(ownership["worktree_path"]))
    dirty = worktree / "after-collection.txt"
    dirty.write_text("preserve after collection\n", encoding="utf-8")

    assert service.collect(task_id) == collected
    with pytest.raises(TaskError, match="clean"):
        service.prune(task_id)
    assert dirty.is_file()


def test_existing_collection_remains_readable_after_retained_branch_advances(
    tmp_path: Path,
) -> None:
    service, brief, ownership, _final = _create_terminal_task(tmp_path)
    task_id = str(brief["task_id"])
    _returned, _child_commit, return_commit = _commit_child_return(
        tmp_path,
        brief,
        ownership,
    )
    collected = service.collect(task_id)
    collected_path = tmp_path / "tasks" / task_id / "collected.json"
    collected_bytes = collected_path.read_bytes()
    collected_inode = collected_path.stat().st_ino
    worktree = Path(str(ownership["worktree_path"]))
    later = worktree / "later-child-work.txt"
    later.write_text("legitimate retained branch work\n", encoding="utf-8")
    _git(worktree, "add", "later-child-work.txt")
    _git(worktree, "commit", "-qm", "continue retained child work")
    advanced_commit = _git(worktree, "rev-parse", "HEAD")

    assert advanced_commit != return_commit
    assert service.collect(task_id) == collected
    assert collected_path.read_bytes() == collected_bytes
    assert collected_path.stat().st_ino == collected_inode

    with pytest.raises(TaskError, match="branch|collection|return|HEAD"):
        service.prune(task_id)

    assert worktree.is_dir()
    assert later.read_text(encoding="utf-8") == "legitimate retained branch work\n"
    assert _git(worktree, "rev-parse", "HEAD") == advanced_commit


def test_preserve_is_an_idempotent_validated_noop_snapshot(tmp_path: Path) -> None:
    service, brief, ownership, _final = _create_terminal_task(tmp_path)
    task_id = str(brief["task_id"])
    _returned, _child_commit, return_commit = _commit_child_return(
        tmp_path,
        brief,
        ownership,
    )
    service.collect(task_id)
    worktree = Path(str(ownership["worktree_path"]))
    parent_head = _git(tmp_path, "rev-parse", "HEAD")
    refs_before = _git(tmp_path, "show-ref")
    child_status = _git(worktree, "status", "--porcelain=v1", "--ignored")
    status_bytes = (tmp_path / ".aros" / "tasks" / task_id / "status.json").read_bytes()

    preserved = service.preserve(task_id)

    assert preserved == {
        "schema_version": 1,
        "task_id": task_id,
        "state": "preserved",
        "brief_sha256": brief["brief_sha256"],
        "ownership_sha256": ownership["ownership_sha256"],
        "actor": ownership["actor"],
        "worktree_path": str(worktree),
        "branch_ref": f"refs/heads/{ownership['branch']}",
        "base_commit": brief["base_commit"],
        "head_commit": return_commit,
        "clean": True,
    }
    assert service.preserve(task_id) == preserved
    assert worktree.is_dir()
    assert _git(worktree, "status", "--porcelain=v1", "--ignored") == child_status
    assert _git(tmp_path, "rev-parse", "HEAD") == parent_head
    assert _git(tmp_path, "show-ref") == refs_before
    assert (
        tmp_path / ".aros" / "tasks" / task_id / "status.json"
    ).read_bytes() == status_bytes
    assert not (tmp_path / ".aros" / "tasks" / task_id / "preserved.json").exists()


@pytest.mark.parametrize("material", ("tracked", "untracked", "ignored"))
def test_preserve_reports_dirty_without_editing_or_removing_it(
    tmp_path: Path,
    material: str,
) -> None:
    service, brief, ownership, _final = _create_terminal_task(tmp_path)
    task_id = str(brief["task_id"])
    _commit_child_return(tmp_path, brief, ownership)
    worktree = Path(str(ownership["worktree_path"]))
    if material == "tracked":
        path = worktree / "alpha.txt"
    elif material == "untracked":
        path = worktree / "untracked-preserved.txt"
    else:
        path = worktree / ".aros" / "ignored-preserved.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{material} must survive\n", encoding="utf-8")
    before = path.read_bytes()

    preserved = service.preserve(task_id)

    assert preserved["state"] == "preserved"
    assert preserved["clean"] is False
    assert path.read_bytes() == before
    assert worktree.is_dir()


def test_prune_removes_only_the_clean_collected_worktree_and_keeps_git_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, brief, ownership, final = _create_terminal_task(tmp_path)
    task_id = str(brief["task_id"])
    _returned, child_commit, return_commit = _commit_child_return(
        tmp_path,
        brief,
        ownership,
    )
    collected = service.collect(task_id)
    worktree = Path(str(ownership["worktree_path"]))
    original_git = service._safe_git_result
    mutation_calls: list[tuple[str, ...]] = []

    def inspect_git(*args: str, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if args[:2] == ("worktree", "remove") or "delete" in args:
            mutation_calls.append(args)
        return original_git(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(service, "_safe_git_result", inspect_git)

    pruned = service.prune(task_id)

    assert set(pruned) == {
        "schema_version",
        "task_id",
        "state",
        "brief_sha256",
        "ownership_sha256",
        "collected_sha256",
        "branch_ref",
        "return_commit",
        "final_state",
        "final_sha256",
        "worktree_path",
        "removed_at",
        "prune_sha256",
        "pruned_sha256",
    }
    assert pruned["schema_version"] == 1
    assert pruned["task_id"] == task_id
    assert pruned["state"] == "pruned"
    assert pruned["brief_sha256"] == brief["brief_sha256"]
    assert pruned["ownership_sha256"] == ownership["ownership_sha256"]
    assert pruned["collected_sha256"] == collected["collected_sha256"]
    assert pruned["branch_ref"] == f"refs/heads/{ownership['branch']}"
    assert pruned["return_commit"] == return_commit
    assert pruned["final_state"] == final["state"]
    assert pruned["final_sha256"] == final["final_sha256"]
    assert pruned["worktree_path"] == str(worktree)
    assert pruned["pruned_sha256"] == json_sha256(
        {key: value for key, value in pruned.items() if key != "pruned_sha256"}
    )
    assert mutation_calls == [("worktree", "remove", str(worktree))]
    assert not worktree.exists()
    branch_ref = f"refs/heads/{ownership['branch']}"
    assert _git(tmp_path, "rev-parse", branch_ref) == return_commit
    assert _git(tmp_path, "cat-file", "-t", child_commit) == "commit"
    assert _git(tmp_path, "cat-file", "-t", return_commit) == "commit"
    assert service.status(task_id) == pruned
    assert service.list() == [pruned]


def test_prune_accepts_mode_normalized_intent_and_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = _normalized_permission_probe()
    monkeypatch.setattr(
        tasks_module,
        "_probe_filesystem_permissions",
        lambda runtime: {
            **evidence,
            "device": runtime.stat().st_dev,
        },
    )
    service, brief, ownership, _final = _create_terminal_task(tmp_path)
    task_id = str(brief["task_id"])
    _commit_child_return(tmp_path, brief, ownership)
    service.collect(task_id)
    original_create = tasks_module.create_json

    def create_with_normalized_mode(path: Path, value: object) -> bool:
        created = original_create(path, value)
        target = Path(path)
        if created and target.name in {"prune.json", "pruned.json"}:
            target.chmod(0o666)
        return created

    monkeypatch.setattr(tasks_module, "create_json", create_with_normalized_mode)

    pruned = service.prune(task_id)

    runtime = tmp_path / ".aros" / "tasks" / task_id
    for name in ("prune.json", "pruned.json"):
        metadata = (runtime / name).lstat()
        assert stat.S_ISREG(metadata.st_mode)
        assert metadata.st_nlink == 1
        assert stat.S_IMODE(metadata.st_mode) == 0o666
    assert pruned["state"] == "pruned"
    assert TaskService(tmp_path).status(task_id) == pruned


def test_prune_rejects_mode_drift_when_permissions_are_enforced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, brief, ownership, _final = _create_terminal_task(tmp_path)
    task_id = str(brief["task_id"])
    _commit_child_return(tmp_path, brief, ownership)
    service.collect(task_id)

    def fail_remove(_target: Path) -> None:
        raise TaskError("injected remove failure")

    monkeypatch.setattr(service, "_remove_task_worktree", fail_remove)
    with pytest.raises(TaskError, match="injected"):
        service.prune(task_id)
    intent_path = tmp_path / ".aros" / "tasks" / task_id / "prune.json"
    intent_path.chmod(0o666)

    with pytest.raises(TaskError, match="prune intent.*restrictive|permission|mode"):
        service.status(task_id)


def test_prune_is_idempotent_after_success_without_removing_again(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, brief, ownership, _final = _create_terminal_task(tmp_path)
    task_id = str(brief["task_id"])
    _commit_child_return(tmp_path, brief, ownership)
    service.collect(task_id)
    first = service.prune(task_id)
    receipt_path = tmp_path / ".aros" / "tasks" / task_id / "pruned.json"
    receipt_inode = receipt_path.stat().st_ino
    calls: list[tuple[str, ...]] = []
    original_git = TaskService._safe_git_result

    def inspect_git(
        self: TaskService,
        *args: str,
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        if args[:2] == ("worktree", "remove"):
            calls.append(args)
        return original_git(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(TaskService, "_safe_git_result", inspect_git)

    assert TaskService(tmp_path).prune(task_id) == first
    assert receipt_path.stat().st_ino == receipt_inode
    assert calls == []


def test_existing_collection_remains_idempotently_readable_after_prune(
    tmp_path: Path,
) -> None:
    service, brief, ownership, _final = _create_terminal_task(tmp_path)
    task_id = str(brief["task_id"])
    _commit_child_return(tmp_path, brief, ownership)
    collected = service.collect(task_id)
    service.prune(task_id)

    assert TaskService(tmp_path).collect(task_id) == collected


def test_prune_recovers_a_crash_after_git_removed_the_worktree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, brief, ownership, _final = _create_terminal_task(tmp_path)
    task_id = str(brief["task_id"])
    _commit_child_return(tmp_path, brief, ownership)
    service.collect(task_id)
    worktree = Path(str(ownership["worktree_path"]))
    original_create = tasks_module.create_json

    class InjectedCrash(RuntimeError):
        pass

    def crash_before_receipt(path: Path, value: object) -> bool:
        if Path(path).name == "pruned.json":
            raise InjectedCrash
        return original_create(path, value)

    monkeypatch.setattr(tasks_module, "create_json", crash_before_receipt)
    with pytest.raises(InjectedCrash):
        service.prune(task_id)
    monkeypatch.setattr(tasks_module, "create_json", original_create)

    runtime = tmp_path / ".aros" / "tasks" / task_id
    assert (runtime / "prune.json").is_file()
    assert not (runtime / "pruned.json").exists()
    assert not worktree.exists()

    recovered = TaskService(tmp_path).prune(task_id)

    assert recovered["state"] == "pruned"
    assert TaskService(tmp_path).status(task_id) == recovered
    assert not worktree.exists()


def test_prune_recovers_a_crash_before_git_removed_the_exact_registration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, brief, ownership, _final = _create_terminal_task(tmp_path)
    task_id = str(brief["task_id"])
    _commit_child_return(tmp_path, brief, ownership)
    collected = service.collect(task_id)
    worktree = Path(str(ownership["worktree_path"]))

    class InjectedCrash(RuntimeError):
        pass

    def remove_directory_only(target: Path) -> None:
        shutil.rmtree(target)
        raise InjectedCrash

    monkeypatch.setattr(service, "_remove_task_worktree", remove_directory_only)
    with pytest.raises(InjectedCrash):
        service.prune(task_id)

    runtime = tmp_path / ".aros" / "tasks" / task_id
    intent_path = runtime / "prune.json"
    intent = json.loads(intent_path.read_text(encoding="utf-8"))
    assert intent["ownership_sha256"] == ownership["ownership_sha256"]
    assert intent["collected_sha256"] == collected["collected_sha256"]
    assert intent["branch_ref"] == f"refs/heads/{ownership['branch']}"
    assert intent["worktree_path"] == str(worktree)
    assert intent["prune_sha256"] == json_sha256(
        {key: value for key, value in intent.items() if key != "prune_sha256"}
    )
    assert not worktree.exists()
    assert not (runtime / "pruned.json").exists()
    interrupted = _git(
        tmp_path,
        "worktree",
        "list",
        "--porcelain",
        "--expire=now",
    )
    assert str(worktree) in interrupted
    assert f"branch refs/heads/{ownership['branch']}" in interrupted
    assert "prunable" in interrupted

    worktree_calls: list[tuple[str, ...]] = []
    original_git = TaskService._safe_git_result

    def inspect_git(
        self: TaskService,
        *args: str,
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        if args[:1] == ("worktree",):
            worktree_calls.append(args)
        return original_git(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(TaskService, "_safe_git_result", inspect_git)

    recovered = TaskService(tmp_path).prune(task_id)

    assert recovered["state"] == "pruned"
    assert recovered["prune_sha256"] == intent["prune_sha256"]
    assert recovered["pruned_sha256"] == json_sha256(
        {key: value for key, value in recovered.items() if key != "pruned_sha256"}
    )
    assert (
        json.loads((runtime / "pruned.json").read_text(encoding="utf-8")) == recovered
    )
    assert [call for call in worktree_calls if call[:2] == ("worktree", "remove")] == [
        ("worktree", "remove", str(worktree))
    ]
    assert not any(call[:2] == ("worktree", "prune") for call in worktree_calls)
    assert str(worktree) not in _git(
        tmp_path,
        "worktree",
        "list",
        "--porcelain",
        "--expire=now",
    )
    assert _git(tmp_path, "rev-parse", f"refs/heads/{ownership['branch']}") == str(
        collected["return_commit"]
    )
    assert TaskService(tmp_path).status(task_id) == recovered


def test_prune_recovery_rejects_and_preserves_an_unrelated_prunable_registration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, brief, ownership, _final = _create_terminal_task(tmp_path)
    task_id = str(brief["task_id"])
    _commit_child_return(tmp_path, brief, ownership)
    service.collect(task_id)
    worktree = Path(str(ownership["worktree_path"]))
    unrelated = tmp_path / ".worktree" / "unrelated-prunable"
    unrelated_branch = "unrelated-prunable"
    _git(
        tmp_path,
        "worktree",
        "add",
        "-q",
        "-b",
        unrelated_branch,
        str(unrelated),
        "HEAD",
    )

    class InjectedCrash(RuntimeError):
        pass

    def remove_directory_only(target: Path) -> None:
        shutil.rmtree(target)
        raise InjectedCrash

    monkeypatch.setattr(service, "_remove_task_worktree", remove_directory_only)
    with pytest.raises(InjectedCrash):
        service.prune(task_id)
    shutil.rmtree(unrelated)

    before = _git(
        tmp_path,
        "worktree",
        "list",
        "--porcelain",
        "--expire=now",
    )
    assert str(worktree) in before
    assert str(unrelated) in before
    assert sum(line.startswith("prunable ") for line in before.splitlines()) == 2

    with pytest.raises(
        TaskError,
        match=re.escape(f"stale or prunable Git worktree registration: {unrelated}"),
    ):
        TaskService(tmp_path).prune(task_id)

    after = _git(
        tmp_path,
        "worktree",
        "list",
        "--porcelain",
        "--expire=now",
    )
    assert after == before
    assert _git_ref_exists(tmp_path, f"refs/heads/{ownership['branch']}")
    assert _git_ref_exists(tmp_path, f"refs/heads/{unrelated_branch}")
    assert not (tmp_path / ".aros" / "tasks" / task_id / "pruned.json").exists()


def test_status_rejects_a_tampered_persisted_prune_intent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, brief, ownership, _final = _create_terminal_task(tmp_path)
    task_id = str(brief["task_id"])
    _commit_child_return(tmp_path, brief, ownership)
    service.collect(task_id)

    def fail_remove(_target: Path) -> None:
        raise TaskError("injected remove failure")

    monkeypatch.setattr(service, "_remove_task_worktree", fail_remove)
    with pytest.raises(TaskError, match="injected"):
        service.prune(task_id)
    intent_path = tmp_path / ".aros" / "tasks" / task_id / "prune.json"
    intent = json.loads(intent_path.read_text(encoding="utf-8"))
    intent["prune_sha256"] = "0" * 64
    atomic_write_json(intent_path, intent)

    with pytest.raises(TaskError, match="prune intent"):
        service.status(task_id)


@pytest.mark.parametrize("material", ("modified", "staged", "untracked", "ignored"))
def test_prune_rejects_and_preserves_every_kind_of_dirty_material(
    tmp_path: Path,
    material: str,
) -> None:
    service, brief, ownership, _final = _create_terminal_task(tmp_path)
    task_id = str(brief["task_id"])
    _commit_child_return(tmp_path, brief, ownership)
    service.collect(task_id)
    worktree = Path(str(ownership["worktree_path"]))
    if material in {"modified", "staged"}:
        path = worktree / "alpha.txt"
        path.write_text("dirty before prune\n", encoding="utf-8")
        if material == "staged":
            _git(worktree, "add", "alpha.txt")
    elif material == "untracked":
        path = worktree / "untracked-before-prune.txt"
        path.write_text("untracked before prune\n", encoding="utf-8")
    else:
        path = worktree / ".aros" / "ignored-before-prune.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("ignored before prune\n", encoding="utf-8")

    with pytest.raises(TaskError, match="clean"):
        service.prune(task_id)

    assert path.exists()
    assert worktree.is_dir()
    assert not (tmp_path / ".aros" / "tasks" / task_id / "pruned.json").exists()


def test_prune_requires_collection_and_an_unchanged_return_head(
    tmp_path: Path,
) -> None:
    uncollected_root = tmp_path / "uncollected"
    service, brief, ownership, _final = _create_terminal_task(uncollected_root)
    task_id = str(brief["task_id"])
    _commit_child_return(uncollected_root, brief, ownership)
    worktree = Path(str(ownership["worktree_path"]))

    with pytest.raises(TaskError, match="collect"):
        service.prune(task_id)
    assert worktree.is_dir()

    moved_root = tmp_path / "advanced"
    service, brief, ownership, _final = _create_terminal_task(moved_root)
    task_id = str(brief["task_id"])
    _commit_child_return(moved_root, brief, ownership)
    service.collect(task_id)
    worktree = Path(str(ownership["worktree_path"]))
    (worktree / "advanced.txt").write_text("advance branch\n", encoding="utf-8")
    _git(worktree, "add", "advanced.txt")
    _git(worktree, "commit", "-qm", "advance after collection")

    with pytest.raises(TaskError, match="collection|return|HEAD|branch"):
        service.prune(task_id)
    assert worktree.is_dir()


@pytest.mark.parametrize("problem", ("half_missing", "moved", "symlink"))
def test_prune_rejects_half_missing_moved_or_symlinked_ownership(
    tmp_path: Path,
    problem: str,
) -> None:
    service, brief, ownership, _final = _create_terminal_task(tmp_path)
    task_id = str(brief["task_id"])
    _commit_child_return(tmp_path, brief, ownership)
    service.collect(task_id)
    worktree = Path(str(ownership["worktree_path"]))
    if problem == "half_missing":
        _git(tmp_path, "worktree", "remove", str(worktree))
        preserved = None
    else:
        preserved = tmp_path / f"preserved-{problem}"
        worktree.rename(preserved)
        if problem == "symlink":
            worktree.symlink_to(preserved, target_is_directory=True)

    with pytest.raises(TaskError, match="worktree|symlink|prune"):
        service.prune(task_id)

    if preserved is not None:
        assert preserved.is_dir()
    assert not (tmp_path / ".aros" / "tasks" / task_id / "pruned.json").exists()
