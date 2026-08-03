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

    def start(self, task_id: str, *, actor: str | None = None) -> dict[str, Any]:
        self.calls.append(("start", task_id, actor))
        return {"task_id": task_id, "state": "running"}

    def status(self, task_id: str) -> dict[str, Any]:
        self.calls.append(("status", task_id))
        return {"task_id": task_id, "state": "running"}

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
        return {"task_id": task_id, "state": "collected"}

    def preserve(self, task_id: str) -> dict[str, Any]:
        self.calls.append(("preserve", task_id))
        return {"task_id": task_id, "state": "preserved"}

    def prune(self, task_id: str) -> dict[str, Any]:
        self.calls.append(("prune", task_id))
        return {"task_id": task_id, "state": "pruned"}


@pytest.fixture(autouse=True)
def fake_task_service(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeTaskService.instances.clear()
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
    assert output == json.dumps(
        {"task_id": "TASK-test", "state": "prepared"},
        ensure_ascii=False,
        indent=2,
    )


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


def test_start_message_and_stop_use_principal_actor(tmp_path: Path) -> None:
    tool = _task_tool()(cwd=str(tmp_path))

    _execute(tool, action="start", task_id="TASK-test")
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
        ("start", "TASK-test", "principal"),
    ]
    assert FakeTaskService.instances[1].calls == [
        ("message", "TASK-test", "record exact evidence", "principal"),
    ]
    assert FakeTaskService.instances[2].calls == [
        ("stop", "TASK-test", "principal", "evidence is sufficient", "TERM"),
    ]


@pytest.mark.parametrize(
    ("action", "expected_call", "expected_result"),
    [
        (
            "status",
            ("status", "TASK-test"),
            {"task_id": "TASK-test", "state": "running"},
        ),
        (
            "collect",
            ("collect", "TASK-test"),
            {"task_id": "TASK-test", "state": "collected"},
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
