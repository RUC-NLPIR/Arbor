"""Principal-facing system call for durable AROS child tasks."""

from __future__ import annotations

import asyncio
import ast
import errno
import importlib
import json
import os
import shutil
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

from arbor.aros.checkpoint import CheckpointError, GitCheckpoint
from arbor.aros.tasks import TaskError, TaskService


class FakeTaskService:
    instances: list["FakeTaskService"] = []
    start_result: dict[str, Any] = {
        "task_id": "TASK-test",
        "run_id": "RUN-test",
        "state": "running",
    }
    status_result: dict[str, Any] = {
        "task_id": "TASK-test",
        "run_id": "RUN-test",
        "state": "running",
        "final_ref": None,
    }

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.calls: list[tuple[Any, ...]] = []
        self.instances.append(self)

    def create(
        self,
        objective: str,
        *,
        actor: str,
        mode: str,
        adapter_argv: list[str],
        capabilities: dict[str, bool],
        deliverables: list[str],
        acceptance: list[str],
        timeout_seconds: float,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "create",
                objective,
                actor,
                mode,
                adapter_argv,
                capabilities,
                deliverables,
                acceptance,
                timeout_seconds,
                idempotency_key,
            )
        )
        return {"task_id": "TASK-test", "state": "prepared"}

    def create_with_commit(self, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        record = self.create(*args, **kwargs)
        return (
            record,
            ("tasks/TASK-test/brief.json",),
            "Record task TASK-test brief",
        )

    def start(
        self,
        task_id: str,
        *,
        actor: str | None = None,
        commit_paths: Any = None,
    ) -> dict[str, Any]:
        assert callable(commit_paths)
        self.calls.append(("start", task_id, actor, commit_paths))
        return dict(self.start_result)

    def status(
        self,
        task_id: str,
        *,
        commit_paths: Any = None,
    ) -> dict[str, Any]:
        assert commit_paths is None or callable(commit_paths)
        self.calls.append(("status", task_id, commit_paths))
        return dict(self.status_result)

    def list(self) -> list[dict[str, Any]]:
        self.calls.append(("list",))
        return [{"task_id": "TASK-test", "state": "completed"}]

    def message(self, task_id: str, message: str, actor: str) -> dict[str, Any]:
        self.calls.append(("message", task_id, message, actor))
        return {"task_id": task_id, "text": message, "actor": actor}

    def stop(
        self,
        task_id: str,
        *,
        actor: str,
        reason: str,
        signal_name: str = "TERM",
    ) -> dict[str, Any]:
        self.calls.append(("stop", task_id, actor, reason, signal_name))
        return {"task_id": task_id, "actor": actor, "reason": reason}

    def collect(self, task_id: str) -> dict[str, Any]:
        self.calls.append(("collect", task_id))
        return {
            "task_id": task_id,
            "state": "collected",
            "run_id": "RUN-test",
            "run_manifest_ref": "runs/RUN-test/manifest.json",
            "run_manifest_sha256": "b" * 64,
            "collected_sha256": "c" * 64,
            "run_final_ref": "runs/RUN-test/final.json",
            "run_final_sha256": "d" * 64,
        }

    def collect_with_commit(self, task_id: str):  # type: ignore[no-untyped-def]
        record = self.collect(task_id)
        return (
            record,
            (f"tasks/{task_id}/collected.json",),
            f"Record task {task_id} collection",
        )

    def collect_and_commit(
        self,
        task_id: str,
        commit_paths: Any,
    ) -> tuple[dict[str, Any], dict[str, object]]:
        self.calls.append(("collect_and_commit", task_id, commit_paths))
        record = {
            "task_id": task_id,
            "state": "collected",
            "run_id": "RUN-test",
            "run_manifest_ref": "runs/RUN-test/manifest.json",
            "run_manifest_sha256": "b" * 64,
            "collected_sha256": "c" * 64,
            "run_final_ref": "runs/RUN-test/final.json",
            "run_final_sha256": "d" * 64,
        }
        paths = (f"tasks/{task_id}/collected.json",)
        checkpoint = commit_paths(paths, f"Record task {task_id} collection")
        if (
            not isinstance(checkpoint, dict)
            or checkpoint.get("paths") != list(paths)
            or checkpoint.get("enforcement_class") != "cooperative"
        ):
            raise TaskError("invalid task collection checkpoint")
        return record, checkpoint

    def preserve(self, task_id: str) -> dict[str, Any]:
        self.calls.append(("preserve", task_id))
        return {"task_id": task_id, "state": "preserved"}

    def prune(self, task_id: str) -> dict[str, Any]:
        self.calls.append(("prune", task_id))
        return {"task_id": task_id, "state": "pruned"}


class FakeObservationCatalog:
    resolved: list[str] = []

    def __init__(self, _root: str | Path) -> None:
        pass

    def resolve(self, ref: str) -> object:
        self.resolved.append(ref)
        return object()


@pytest.fixture(autouse=True)
def fake_task_service(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeTaskService.instances.clear()
    FakeTaskService.start_result = {
        "task_id": "TASK-test",
        "run_id": "RUN-test",
        "state": "running",
    }
    FakeTaskService.status_result = {
        "task_id": "TASK-test",
        "run_id": "RUN-test",
        "state": "running",
        "final_ref": None,
    }
    FakeObservationCatalog.resolved.clear()
    try:
        module = importlib.import_module("arbor.aros.task_tool")
    except ModuleNotFoundError:
        return
    monkeypatch.setattr(module, "TaskService", FakeTaskService)
    monkeypatch.setattr(module, "ObservationCatalog", FakeObservationCatalog)


def _task_tool() -> Any:
    module = importlib.import_module("arbor.aros.task_tool")
    return module.TaskTool


def _execute(tool: Any, **kwargs: Any) -> str:
    return asyncio.run(tool.execute(**kwargs))


def test_task_tool_exposes_one_flat_action_based_system_call(tmp_path: Path) -> None:
    tool = _task_tool()(cwd=str(tmp_path))
    schema = tool.input_schema

    assert tool.name == "Task"
    assert tool.is_read_only is False
    assert tool.persist_threshold == float("inf")
    assert schema["required"] == ["action"]
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {
        "action",
        "task_id",
        "objective",
        "mode",
        "adapter_argv",
        "capabilities",
        "deliverables",
        "acceptance",
        "timeout_seconds",
        "idempotency_key",
        "message",
        "reason",
    }
    assert schema["properties"]["action"]["enum"] == [
        "create",
        "start",
        "status",
        "list",
        "message",
        "stop",
        "collect",
        "preserve",
        "prune",
    ]
    assert schema["properties"]["mode"]["enum"] == ["read_only", "write"]
    assert schema["properties"]["adapter_argv"] == {
        "type": "array",
        "items": {"type": "string", "minLength": 1},
        "minItems": 1,
    }
    assert schema["properties"]["capabilities"] == {
        "type": "object",
        "properties": {
            "network": {"type": "boolean"},
            "shell": {"type": "boolean"},
        },
        "required": ["network", "shell"],
        "additionalProperties": False,
    }


@pytest.mark.parametrize("surface", ["description", "schema"])
def test_task_tool_publishes_the_trusted_local_task_boundary(
    tmp_path: Path,
    surface: str,
) -> None:
    tool = _task_tool()(cwd=str(tmp_path))
    if surface == "description":
        text = tool.description
    else:
        text = tool.input_schema.get("description", "")
    text = " ".join(text.lower().split())

    claims = (
        "trusted-local and application-scoped, not a security sandbox",
        "network and shell capability flags are audit declarations and are not enforced",
        "secrets and untrusted adapters are unsupported",
        "daemonizing or new-session descendants that do not drain fail closed as lost "
        "with no terminal receipt",
    )
    for claim in claims:
        assert claim in text, surface


def test_create_freezes_default_brief_without_starting(tmp_path: Path) -> None:
    tool = _task_tool()(cwd=str(tmp_path))

    output = _execute(
        tool,
        action="create",
        objective="inspect the failing seed",
        mode="read_only",
        adapter_argv=["python", "-c", "print('a; $HOME')"],
        idempotency_key="seed-inspection",
    )

    service = FakeTaskService.instances[0]
    assert service.root == tmp_path
    assert service.calls == [
        (
            "create",
            "inspect the failing seed",
            "principal",
            "read_only",
            ["python", "-c", "print('a; $HOME')"],
            {"network": False, "shell": False},
            [],
            [],
            3600,
            "seed-inspection",
        ),
    ]
    assert json.loads(output) == {
        "task_id": "TASK-test",
        "state": "prepared",
    }
    assert not any(tmp_path.rglob("proposal.json"))


def test_create_callback_commits_paths_once(tmp_path: Path) -> None:
    calls: list[tuple[tuple[str, ...], str]] = []

    def commit(paths: tuple[str, ...], message: str) -> dict[str, object]:
        calls.append((paths, message))
        return {"commit": "d" * 40}

    tool = _task_tool()(cwd=str(tmp_path), commit_paths=commit)

    output = _execute(
        tool,
        action="create",
        objective="inspect the failing seed",
        mode="read_only",
        adapter_argv=["adapter"],
        idempotency_key="seed-callback",
    )

    assert calls == [
        (("tasks/TASK-test/brief.json",), "Record task TASK-test brief")
    ]
    assert json.loads(output) == {
        "task_id": "TASK-test",
        "state": "prepared",
        "checkpoint": {"commit": "d" * 40},
    }


def test_collect_records_run_then_task_refs_after_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = importlib.import_module("arbor.aros.task_tool")
    events: list[object] = []

    class OrderedCatalog:
        def __init__(self, _root: str | Path) -> None:
            pass

        def resolve(self, ref: str) -> object:
            events.append(("resolve", ref))
            return object()

    monkeypatch.setattr(module, "ObservationCatalog", OrderedCatalog)

    def commit(paths: tuple[str, ...], message: str) -> dict[str, object]:
        events.append((paths, message))
        return {
            "commit": "e" * 40,
            "paths": list(paths),
            "enforcement_class": "cooperative",
        }

    tool = _task_tool()(
        cwd=str(tmp_path),
        commit_paths=commit,
        record_observation=lambda ref: events.append(ref),
    )

    output = json.loads(_execute(tool, action="collect", task_id="TASK-test"))

    assert events == [
        (
            ("tasks/TASK-test/collected.json",),
            "Record task TASK-test collection",
        ),
        ("resolve", "runs/RUN-test/final.json"),
        ("resolve", "tasks/TASK-test/collected.json"),
        "runs/RUN-test/final.json",
        "tasks/TASK-test/collected.json",
    ]
    assert output["checkpoint"] == {
        "commit": "e" * 40,
        "paths": ["tasks/TASK-test/collected.json"],
        "enforcement_class": "cooperative",
    }
    assert FakeTaskService.instances[0].calls == [
        ("collect_and_commit", "TASK-test", commit)
    ]


def test_collect_callback_failure_records_no_observations(tmp_path: Path) -> None:
    observations: list[str] = []

    def fail_commit(_paths: tuple[str, ...], _message: str) -> dict[str, object]:
        raise RuntimeError("injected commit failure")

    tool = _task_tool()(
        cwd=str(tmp_path),
        commit_paths=fail_commit,
        record_observation=observations.append,
    )

    with pytest.raises(RuntimeError, match="injected commit failure"):
        _execute(tool, action="collect", task_id="TASK-test")

    assert observations == []


def test_collect_wrong_commit_result_records_no_observations(tmp_path: Path) -> None:
    observations: list[str] = []
    tool = _task_tool()(
        cwd=str(tmp_path),
        commit_paths=lambda _paths, _message: {
            "commit": "not-a-commit",
            "paths": ["wrong.json"],
        },
        record_observation=observations.append,
    )

    with pytest.raises(TaskError, match="commit|checkpoint"):
        _execute(tool, action="collect", task_id="TASK-test")

    assert observations == []


def test_collect_forged_plausible_callback_records_no_observations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arbor.aros import task_tool as task_tool_module
    import test_aros_task_on_run as task_run_support

    _service, brief, _ownership, _binding, _final, _child, _return = (
        task_run_support._terminal_task_run(tmp_path, monkeypatch)
    )
    task_id = str(brief["task_id"])
    observations: list[str] = []
    monkeypatch.setattr(task_tool_module, "TaskService", TaskService)

    def forged(
        paths: tuple[str, ...],
        _message: str,
    ) -> dict[str, object]:
        return {
            "commit": task_run_support._git(tmp_path, "rev-parse", "HEAD"),
            "paths": list(paths),
            "enforcement_class": "cooperative",
        }

    tool = _task_tool()(
        cwd=str(tmp_path),
        commit_paths=forged,
        record_observation=observations.append,
    )

    with pytest.raises(TaskError, match="commit|collection|Run final"):
        _execute(tool, action="collect", task_id=task_id)

    assert observations == []


def test_collect_staged_exact_records_fail_closed_without_cleanup_or_observation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arbor.aros import task_tool as task_tool_module
    import test_aros_task_on_run as task_run_support

    service, brief, _ownership, _binding, _final, _child, _return = (
        task_run_support._terminal_task_run(tmp_path, monkeypatch)
    )
    task_id = str(brief["task_id"])
    collected = service.collect(task_id)
    refs = [
        str(collected["run_final_ref"]),
        f"tasks/{task_id}/collected.json",
    ]
    task_run_support._git(tmp_path, "add", *refs)
    before = {ref: (tmp_path / ref).read_bytes() for ref in refs}
    observations: list[str] = []
    monkeypatch.setattr(task_tool_module, "TaskService", TaskService)
    tool = _task_tool()(
        cwd=str(tmp_path),
        commit_paths=GitCheckpoint(tmp_path).commit_paths,
        record_observation=observations.append,
    )

    with pytest.raises((TaskError, CheckpointError), match="Git state|index|staged"):
        _execute(tool, action="collect", task_id=task_id)

    assert observations == []
    assert task_run_support._git(
        tmp_path,
        "diff",
        "--cached",
        "--name-only",
    ).splitlines() == sorted(refs)
    assert {ref: (tmp_path / ref).read_bytes() for ref in refs} == before


def test_create_forwards_explicit_bounded_brief_fields(tmp_path: Path) -> None:
    tool = _task_tool()(cwd=str(tmp_path))

    _execute(
        tool,
        action="create",
        objective="produce the report",
        mode="write",
        adapter_argv=["adapter", "--exact"],
        capabilities={"network": True, "shell": True},
        deliverables=["report.json"],
        acceptance=["python verify.py"],
        timeout_seconds=42,
        idempotency_key="report-1",
    )

    assert FakeTaskService.instances[0].calls == [
        (
            "create",
            "produce the report",
            "principal",
            "write",
            ["adapter", "--exact"],
            {"network": True, "shell": True},
            ["report.json"],
            ["python verify.py"],
            42,
            "report-1",
        ),
    ]


def test_start_passes_commit_callback_and_records_terminal_observation(
    tmp_path: Path,
) -> None:
    observations: list[str] = []

    def commit(paths: tuple[str, ...], message: str) -> dict[str, object]:
        raise AssertionError(f"fake service must own commit: {paths!r} {message!r}")

    FakeTaskService.start_result = {
        "task_id": "TASK-test",
        "state": "completed",
        "run_id": "RUN-test",
        "final_ref": "runs/RUN-test/final.json",
    }
    tool = _task_tool()(
        cwd=str(tmp_path),
        commit_paths=commit,
        record_observation=observations.append,
    )

    output = json.loads(_execute(tool, action="start", task_id="TASK-test"))

    assert FakeTaskService.instances[0].calls == [
        ("start", "TASK-test", "principal", commit),
    ]
    assert output == FakeTaskService.start_result
    assert observations == ["runs/RUN-test/final.json"]


def test_start_without_commit_callback_fails_before_service_preparation(
    tmp_path: Path,
) -> None:
    tool = _task_tool()(cwd=str(tmp_path))

    with pytest.raises(TaskError, match="commit_paths|commit"):
        _execute(tool, action="start", task_id="TASK-test")

    assert FakeTaskService.instances == []


def test_collect_without_commit_callback_fails_before_service_collection(
    tmp_path: Path,
) -> None:
    tool = _task_tool()(cwd=str(tmp_path))

    with pytest.raises(TaskError, match="commit_paths|commit"):
        _execute(tool, action="collect", task_id="TASK-test")

    assert FakeTaskService.instances == []


def test_status_passes_commit_callback_and_records_terminal_observation_once(
    tmp_path: Path,
) -> None:
    observations: list[str] = []

    def commit(paths: tuple[str, ...], message: str) -> dict[str, object]:
        raise AssertionError(f"fake service must own commit: {paths!r} {message!r}")

    FakeTaskService.status_result = {
        "task_id": "TASK-test",
        "state": "completed",
        "run_id": "RUN-test",
        "final_ref": "runs/RUN-test/final.json",
    }
    tool = _task_tool()(
        cwd=str(tmp_path),
        commit_paths=commit,
        record_observation=observations.append,
    )

    output = json.loads(_execute(tool, action="status", task_id="TASK-test"))

    assert FakeTaskService.instances[0].calls == [
        ("status", "TASK-test", commit),
    ]
    assert output == FakeTaskService.status_result
    assert observations == ["runs/RUN-test/final.json"]


def test_real_task_start_reuses_unenforced_probe_for_adapter_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import arbor.aros.task_run as task_run_module
    import test_aros_task_on_run as task_run_support
    from arbor.aros.runs import RunService

    task_run_support._workspace(tmp_path)
    real_fchmod = os.fchmod

    def normalize_mode(descriptor: int, _mode: int) -> None:
        opened = os.fstat(descriptor)
        real_fchmod(descriptor, 0o777 if stat.S_ISDIR(opened.st_mode) else 0o666)

    monkeypatch.setattr(task_run_module.os, "fchmod", normalize_mode)
    service = TaskService(tmp_path)
    brief = task_run_support._brief(service)
    task_id = str(brief["task_id"])
    task_run_support._git(tmp_path, "add", f"tasks/{task_id}/brief.json")
    task_run_support._git(tmp_path, "commit", "-qm", "record task brief")

    def fake_start(
        _service: RunService,
        run_id: str,
        *,
        actor: str | None = None,
    ) -> dict[str, object]:
        assert actor == "principal"
        return task_run_support._launched_run_status(tmp_path, run_id)

    monkeypatch.setattr(RunService, "start", fake_start)
    status = service.start(
        task_id,
        actor="principal",
        commit_paths=GitCheckpoint(tmp_path).commit_paths,
    )

    assert service._filesystem_permission_probe["observed_mode"] == 0o666
    assert service._filesystem_permissions_enforced is False
    runtime = tmp_path / ".aros/tasks" / task_id
    assert {
        stat.S_IMODE(path.stat().st_mode)
        for path in (runtime, runtime / "home", runtime / "tmp")
    } == {0o777}
    assert status["run_id"] == service.status(task_id)["run_id"]

    home = runtime / "home"
    home.rmdir()
    outside = tmp_path / "substituted-home"
    outside.mkdir()
    home.symlink_to(outside, target_is_directory=True)
    with pytest.raises(TaskError, match="Task Run|inspect"):
        service.status(task_id)


@pytest.mark.parametrize(
    ("error_number", "permissions_enforced", "accepted"),
    [
        (errno.EOPNOTSUPP, False, True),
        (errno.EOPNOTSUPP, True, False),
        (errno.EPERM, False, False),
    ],
)
def test_adapter_runtime_accepts_only_recognized_unenforced_fchmod_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_number: int,
    permissions_enforced: bool,
    accepted: bool,
) -> None:
    import arbor.aros.task_run as task_run_module

    (tmp_path / ".aros/tasks").mkdir(parents=True)

    def fail_fchmod(_descriptor: int, _mode: int) -> None:
        raise OSError(error_number, os.strerror(error_number))

    monkeypatch.setattr(task_run_module.os, "fchmod", fail_fchmod)

    def call() -> Path:
        return task_run_module._ensure_adapter_runtime(
            tmp_path,
            "TASK-20260807-permission-test",
            permissions_enforced=permissions_enforced,
        )

    if accepted:
        assert call().is_dir()
    else:
        with pytest.raises(task_run_module.TaskRunError, match="validate"):
            call()


def test_task_run_argv_forces_no_bytecode_internal_adapter(tmp_path: Path) -> None:
    from arbor.aros.task_run import task_run_argv

    assert task_run_argv(tmp_path, "TASK-20260807-test") == [
        sys.executable,
        "-B",
        "-m",
        "arbor.aros.task_adapter",
        "--workspace",
        str(tmp_path),
        "--task-id",
        "TASK-20260807-test",
    ]


def test_run_backed_status_ignores_only_moving_owned_branch_tip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import test_aros_task_on_run as task_run_support
    from arbor.aros.runs import RunService
    from arbor.aros.task_run import ensure_task_run

    service, brief, ownership = task_run_support._prepared_task(tmp_path)
    task_id = str(brief["task_id"])
    binding = ensure_task_run(
        tmp_path,
        brief,
        ownership,
        actor="principal",
        commit_paths=GitCheckpoint(tmp_path).commit_paths,
    )
    monkeypatch.setattr(
        RunService,
        "status",
        lambda _service, _run_id: task_run_support._running_run_status(
            tmp_path,
            str(binding["run_id"]),
        ),
    )

    def moving_branch(_ownership: dict[str, object]) -> None:
        raise TaskError("owned task branch tip mismatch during child commit")

    monkeypatch.setattr(service, "_validate_owned_worktree", moving_branch)

    assert service.status(task_id)["state"] == "running"
    with pytest.raises(TaskError, match="branch tip mismatch"):
        service.preserve(task_id)


def test_run_backed_status_checks_identity_during_legitimate_child_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import test_aros_task_on_run as task_run_support
    from arbor.aros.runs import RunService
    from arbor.aros.task_run import ensure_task_run

    service, brief, ownership = task_run_support._prepared_task(tmp_path)
    task_id = str(brief["task_id"])
    binding = ensure_task_run(
        tmp_path,
        brief,
        ownership,
        actor="principal",
        commit_paths=GitCheckpoint(tmp_path).commit_paths,
    )
    monkeypatch.setattr(
        RunService,
        "status",
        lambda _service, _run_id: task_run_support._running_run_status(
            tmp_path,
            str(binding["run_id"]),
        ),
    )
    assert hasattr(service, "_validate_owned_worktree_identity")
    validate_identity = service._validate_owned_worktree_identity
    worktree = Path(str(ownership["worktree_path"]))
    committed = False

    def validate_while_committing(value: dict[str, object]) -> None:
        nonlocal committed
        validate_identity(value)
        if committed:
            return
        (worktree / "concurrent.txt").write_text("committed concurrently\n")
        task_run_support._git(worktree, "add", "concurrent.txt")
        task_run_support._git(worktree, "commit", "-qm", "concurrent child commit")
        committed = True

    monkeypatch.setattr(
        service,
        "_validate_owned_worktree_identity",
        validate_while_committing,
    )

    assert service.status(task_id)["state"] == "running"
    assert committed is True
    assert service.preserve(task_id)["clean"] is True


@pytest.mark.parametrize(
    "corruption",
    ["missing", "symlink", "detached", "wrong_branch", "wrong_registration"],
)
def test_run_backed_status_rejects_owned_worktree_identity_corruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    corruption: str,
) -> None:
    import test_aros_task_on_run as task_run_support
    from arbor.aros.runs import RunService
    from arbor.aros.task_run import ensure_task_run

    service, brief, ownership = task_run_support._prepared_task(tmp_path)
    task_id = str(brief["task_id"])
    binding = ensure_task_run(
        tmp_path,
        brief,
        ownership,
        actor="principal",
        commit_paths=GitCheckpoint(tmp_path).commit_paths,
    )
    monkeypatch.setattr(
        RunService,
        "status",
        lambda _service, _run_id: task_run_support._running_run_status(
            tmp_path,
            str(binding["run_id"]),
        ),
    )
    worktree = Path(str(ownership["worktree_path"]))
    moved = worktree.with_name(worktree.name + "-moved")
    if corruption == "missing":
        worktree.rename(moved)
    elif corruption == "symlink":
        worktree.rename(moved)
        worktree.symlink_to(moved, target_is_directory=True)
    elif corruption == "detached":
        task_run_support._git(worktree, "checkout", "-q", "--detach")
    elif corruption == "wrong_branch":
        task_run_support._git(worktree, "checkout", "-qb", "foreign-task-branch")
    else:
        task_run_support._git(tmp_path, "worktree", "move", str(worktree), str(moved))
        shutil.copytree(moved, worktree)

    with pytest.raises(TaskError, match="worktree|branch|registered|inspect"):
        service.status(task_id)


@pytest.mark.parametrize("mutation", ["hash", "path"])
def test_run_backed_status_still_rejects_tampered_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    import arbor.aros.tasks as tasks_module
    import test_aros_task_on_run as task_run_support
    from arbor.aros.runs import RunService
    from arbor.aros.task_run import ensure_task_run

    service, brief, ownership = task_run_support._prepared_task(tmp_path)
    task_id = str(brief["task_id"])
    binding = ensure_task_run(
        tmp_path,
        brief,
        ownership,
        actor="principal",
        commit_paths=GitCheckpoint(tmp_path).commit_paths,
    )
    monkeypatch.setattr(
        RunService,
        "status",
        lambda _service, _run_id: task_run_support._running_run_status(
            tmp_path,
            str(binding["run_id"]),
        ),
    )
    tampered = dict(ownership)
    if mutation == "hash":
        tampered["ownership_sha256"] = "0" * 64
    else:
        tampered["worktree_path"] = str(tmp_path / "substituted-worktree")
        tampered["ownership_sha256"] = tasks_module._ownership_sha256(tampered)
    task_run_support._write_json(
        tmp_path / ".aros/tasks" / task_id / "ownership.json",
        tampered,
    )

    with pytest.raises(TaskError, match="ownership|worktree"):
        service.status(task_id)


def test_message_and_stop_use_principal_actor(tmp_path: Path) -> None:
    tool = _task_tool()(cwd=str(tmp_path))

    _execute(
        tool,
        action="message",
        task_id="TASK-test",
        message="record exact evidence",
    )
    _execute(
        tool,
        action="stop",
        task_id="TASK-test",
        reason="evidence is sufficient",
    )

    assert FakeTaskService.instances[0].calls == [
        ("message", "TASK-test", "record exact evidence", "principal"),
    ]
    assert FakeTaskService.instances[1].calls == [
        ("stop", "TASK-test", "principal", "evidence is sufficient", "TERM"),
    ]


@pytest.mark.parametrize(
    ("action", "expected_call", "expected_result"),
    [
        (
            "status",
            ("status", "TASK-test", None),
            {
                "task_id": "TASK-test",
                "run_id": "RUN-test",
                "state": "running",
                "final_ref": None,
            },
        ),
        (
            "preserve",
            ("preserve", "TASK-test"),
            {"task_id": "TASK-test", "state": "preserved"},
        ),
        (
            "prune",
            ("prune", "TASK-test"),
            {"task_id": "TASK-test", "state": "pruned"},
        ),
    ],
)
def test_task_id_actions_forward_directly_and_return_json(
    tmp_path: Path,
    action: str,
    expected_call: tuple[Any, ...],
    expected_result: dict[str, Any],
) -> None:
    tool = _task_tool()(cwd=str(tmp_path))

    output = _execute(tool, action=action, task_id="TASK-test")

    assert FakeTaskService.instances[0].calls == [expected_call]
    assert output == json.dumps(expected_result, ensure_ascii=False, indent=2)


def test_list_forwards_directly_and_returns_unwrapped_json(tmp_path: Path) -> None:
    tool = _task_tool()(cwd=str(tmp_path))

    output = _execute(tool, action="list")

    assert FakeTaskService.instances[0].calls == [("list",)]
    assert json.loads(output) == [{"task_id": "TASK-test", "state": "completed"}]


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"action": "unknown"},
        {"action": "create"},
        {
            "action": "create",
            "objective": "objective",
            "mode": "write",
            "adapter_argv": ["adapter"],
        },
        {"action": "start"},
        {"action": "status"},
        {"action": "message", "task_id": "TASK-test"},
        {"action": "stop", "task_id": "TASK-test"},
        {"action": "collect"},
        {"action": "preserve"},
        {"action": "prune"},
    ],
)
def test_task_tool_rejects_unknown_actions_and_missing_action_fields(
    tmp_path: Path,
    kwargs: dict[str, Any],
) -> None:
    tool = _task_tool()(cwd=str(tmp_path))

    with pytest.raises(TaskError, match="required|unknown"):
        _execute(tool, **kwargs)

    assert FakeTaskService.instances == []


def test_task_tool_has_no_semantic_or_legacy_control_plane_surface() -> None:
    source = Path("src/aros/task_tool.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported.update(
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    )

    forbidden_imports = ("coordinator", "executor", "idea_tree", "mcp")
    assert not any(
        part in module for module in imported for part in forbidden_imports
    )
    forbidden_fields = {
        "actor",
        "provider",
        "model",
        "signal",
        "merge",
        "cherry_pick",
        "apply",
        "assimilate",
        "semantic",
    }
    schema_fields = set(_task_tool().input_schema["properties"])
    assert forbidden_fields.isdisjoint(schema_fields)
