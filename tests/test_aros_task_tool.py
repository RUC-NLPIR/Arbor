"""Principal-facing system call for durable AROS child tasks."""

from __future__ import annotations

import asyncio
import ast
import importlib
import json
from pathlib import Path
from typing import Any

import pytest

from arbor.aros.tasks import TaskError


class FakeTaskService:
    instances: list["FakeTaskService"] = []
    start_result: dict[str, Any] = {
        "task_id": "TASK-test",
        "state": "running",
        "final_ref": None,
    }
    status_result: dict[str, Any] = {
        "task_id": "TASK-test",
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
        self.calls.append(("start", task_id, actor, commit_paths))
        return dict(self.start_result)

    def status(
        self,
        task_id: str,
        *,
        commit_paths: Any = None,
    ) -> dict[str, Any]:
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
            "collected_sha256": "c" * 64,
        }

    def collect_with_commit(self, task_id: str):  # type: ignore[no-untyped-def]
        record = self.collect(task_id)
        return (
            record,
            (f"tasks/{task_id}/collected.json",),
            f"Record task {task_id} collection",
        )

    def preserve(self, task_id: str) -> dict[str, Any]:
        self.calls.append(("preserve", task_id))
        return {"task_id": task_id, "state": "preserved"}

    def prune(self, task_id: str) -> dict[str, Any]:
        self.calls.append(("prune", task_id))
        return {"task_id": task_id, "state": "pruned"}


@pytest.fixture(autouse=True)
def fake_task_service(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeTaskService.instances.clear()
    FakeTaskService.start_result = {
        "task_id": "TASK-test",
        "state": "running",
        "final_ref": None,
    }
    FakeTaskService.status_result = {
        "task_id": "TASK-test",
        "state": "running",
        "final_ref": None,
    }
    try:
        module = importlib.import_module("arbor.aros.task_tool")
    except ModuleNotFoundError:
        return
    monkeypatch.setattr(module, "TaskService", FakeTaskService)


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


def test_collect_records_terminal_ref_after_commit(tmp_path: Path) -> None:
    events: list[object] = []

    def commit(paths: tuple[str, ...], message: str) -> dict[str, object]:
        events.append((paths, message))
        return {"commit": "e" * 40}

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
        "tasks/TASK-test/collected.json",
    ]
    assert output["checkpoint"] == {"commit": "e" * 40}


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
                "state": "running",
                "final_ref": None,
            },
        ),
        (
            "collect",
            ("collect", "TASK-test"),
            {
                "task_id": "TASK-test",
                "state": "collected",
                "collected_sha256": "c" * 64,
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
