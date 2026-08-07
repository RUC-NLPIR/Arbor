"""CLI tests for durable AROS child tasks."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from arbor.aros.tasks import TaskError
from arbor.cli.commands import aros_cmd


runner = CliRunner()


class FakeTaskService:
    instances: list["FakeTaskService"] = []
    error: Exception | None = None

    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[tuple[Any, ...]] = []
        self.instances.append(self)

    def _raise_error(self) -> None:
        if self.error is not None:
            raise self.error

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
        timeout_seconds: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self._raise_error()
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
        return {"state": "prepared", "task_id": "TASK-test"}

    def start(
        self,
        task_id: str,
        *,
        actor: str | None = None,
        commit_paths: Any = None,
    ) -> dict[str, Any]:
        self._raise_error()
        self.calls.append(("start", task_id, actor, commit_paths))
        return {"state": "running", "task_id": task_id}

    def status(self, task_id: str) -> dict[str, Any]:
        self._raise_error()
        self.calls.append(("status", task_id))
        return {"state": "running", "task_id": task_id}

    def list(self) -> list[dict[str, Any]]:
        self._raise_error()
        self.calls.append(("list",))
        return [{"state": "completed", "task_id": "TASK-test"}]

    def message(self, task_id: str, message: str, actor: str) -> dict[str, Any]:
        self._raise_error()
        self.calls.append(("message", task_id, message, actor))
        return {"actor": actor, "task_id": task_id, "text": message}

    def stop(
        self,
        task_id: str,
        *,
        actor: str,
        reason: str,
        signal_name: str = "TERM",
    ) -> dict[str, Any]:
        self._raise_error()
        self.calls.append(("stop", task_id, actor, reason, signal_name))
        return {"actor": actor, "reason": reason, "task_id": task_id}

    def collect(self, task_id: str) -> dict[str, Any]:
        self._raise_error()
        self.calls.append(("collect", task_id))
        return {"state": "collected", "task_id": task_id}

    def preserve(self, task_id: str) -> dict[str, Any]:
        self._raise_error()
        self.calls.append(("preserve", task_id))
        return {"state": "preserved", "task_id": task_id}

    def prune(self, task_id: str) -> dict[str, Any]:
        self._raise_error()
        self.calls.append(("prune", task_id))
        return {"state": "pruned", "task_id": task_id}


@pytest.fixture(autouse=True)
def fake_task_service(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeTaskService.instances.clear()
    FakeTaskService.error = None
    monkeypatch.setattr(aros_cmd, "TaskService", FakeTaskService, raising=False)


def test_task_create_freezes_exact_adapter_argv_without_starting(
    tmp_path: Path,
) -> None:
    adapter_argv = [
        "python",
        "-c",
        "print('literal ; && $HOME `date`')",
        "--value=a b",
    ]

    result = runner.invoke(
        aros_cmd.aros_app,
        [
            "task",
            "create",
            "--cwd",
            str(tmp_path),
            "--objective",
            "produce one bounded report",
            "--mode",
            "write",
            "--idempotency-key",
            "report-1",
            "--timeout-seconds",
            "42",
            "--network",
            "--shell",
            "--deliverable",
            "report.json",
            "--deliverable",
            "notes.md",
            "--acceptance",
            "python verify.py",
            "--actor",
            "principal",
            "--",
            *adapter_argv,
        ],
    )

    assert result.exit_code == 0, result.output
    service = FakeTaskService.instances[0]
    assert service.root == tmp_path.resolve()
    assert service.calls == [
        (
            "create",
            "produce one bounded report",
            "principal",
            "write",
            adapter_argv,
            {"network": True, "shell": True},
            ["report.json", "notes.md"],
            ["python verify.py"],
            42,
            "report-1",
        ),
    ]
    expected = {"state": "prepared", "task_id": "TASK-test"}
    assert result.output == json.dumps(
        expected,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"


def test_task_create_defaults_are_bounded_and_human_attributed(
    tmp_path: Path,
) -> None:
    result = runner.invoke(
        aros_cmd.aros_app,
        [
            "task",
            "create",
            "--cwd",
            str(tmp_path),
            "--objective",
            "inspect the evidence",
            "--mode",
            "read_only",
            "--idempotency-key",
            "inspect-1",
            "--",
            "adapter",
            "--exact",
        ],
    )

    assert result.exit_code == 0, result.output
    assert FakeTaskService.instances[0].calls == [
        (
            "create",
            "inspect the evidence",
            "human",
            "read_only",
            ["adapter", "--exact"],
            {"network": False, "shell": False},
            [],
            [],
            3600,
            "inspect-1",
        ),
    ]


@pytest.mark.parametrize(
    ("args", "has_capability_options"),
    [
        pytest.param(["task", "--help"], False, id="task"),
        pytest.param(["task", "create", "--help"], True, id="create"),
    ],
)
def test_task_help_states_the_trusted_local_boundary(
    args: list[str],
    has_capability_options: bool,
) -> None:
    result = runner.invoke(aros_cmd.aros_app, args)

    assert result.exit_code == 0, result.output
    help_text = " ".join(result.output.replace("│", " ").lower().split())
    claims = (
        "trusted-local and application-scoped, not a security sandbox",
        "network and shell capability flags are audit declarations and are not enforced",
        "secrets and untrusted adapters are unsupported",
        "daemonizing or new-session descendants that do not drain fail closed as lost "
        "with no terminal receipt",
    )
    if has_capability_options:
        for claim in claims:
            assert claim in help_text, args
        assert "--network network audit declaration; not enforced." in help_text
        assert "--shell shell audit declaration; not enforced." in help_text
        assert "authorize network capability" not in help_text
        assert "authorize shell capability" not in help_text
    else:
        group_help, command_help = help_text.split("commands", maxsplit=1)
        for claim in claims:
            assert claim in group_help
            assert claim not in command_help
        assert "create freeze one immutable task brief without starting it." in command_help


def test_task_create_requires_double_dash_before_adapter_argv(
    tmp_path: Path,
) -> None:
    result = runner.invoke(
        aros_cmd.aros_app,
        [
            "task",
            "create",
            "--cwd",
            str(tmp_path),
            "--objective",
            "inspect",
            "--mode",
            "read_only",
            "--idempotency-key",
            "inspect-1",
            "adapter",
            "--exact",
        ],
    )

    assert result.exit_code == 2
    assert "command argv must follow --" in result.output
    assert FakeTaskService.instances == []


def test_task_create_requires_adapter_argv(tmp_path: Path) -> None:
    result = runner.invoke(
        aros_cmd.aros_app,
        [
            "task",
            "create",
            "--cwd",
            str(tmp_path),
            "--objective",
            "inspect",
            "--mode",
            "read_only",
            "--idempotency-key",
            "inspect-1",
            "--",
        ],
    )

    assert result.exit_code == 2
    assert "Missing argument" in result.output
    assert FakeTaskService.instances == []


def test_task_start_is_a_separate_human_attributed_action(tmp_path: Path) -> None:
    checkpoints: list[Any] = []

    class FakeCheckpoint:
        def __init__(self, root: Path) -> None:
            self.root = root
            self.commit_paths = object()
            checkpoints.append(self)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(aros_cmd, "GitCheckpoint", FakeCheckpoint)
        result = runner.invoke(
            aros_cmd.aros_app,
            ["task", "start", "TASK-test", "--cwd", str(tmp_path)],
        )

    assert result.exit_code == 0, result.output
    assert FakeTaskService.instances[0].root == tmp_path.resolve()
    assert len(checkpoints) == 1
    assert checkpoints[0].root == tmp_path.resolve()
    assert FakeTaskService.instances[0].calls == [
        ("start", "TASK-test", "human", checkpoints[0].commit_paths),
    ]
    assert json.loads(result.output) == {
        "state": "running",
        "task_id": "TASK-test",
    }


def test_task_status_and_list_emit_unwrapped_json(tmp_path: Path) -> None:
    status = runner.invoke(
        aros_cmd.aros_app,
        ["task", "status", "TASK-test", "--cwd", str(tmp_path)],
    )
    listed = runner.invoke(
        aros_cmd.aros_app,
        ["task", "list", "--cwd", str(tmp_path)],
    )

    assert status.exit_code == 0, status.output
    assert listed.exit_code == 0, listed.output
    assert json.loads(status.output) == {
        "state": "running",
        "task_id": "TASK-test",
    }
    assert json.loads(listed.output) == [
        {"state": "completed", "task_id": "TASK-test"},
    ]
    assert FakeTaskService.instances[0].calls == [("status", "TASK-test")]
    assert FakeTaskService.instances[1].calls == [("list",)]


def test_task_message_requires_text_and_defaults_actor_to_human(
    tmp_path: Path,
) -> None:
    missing = runner.invoke(
        aros_cmd.aros_app,
        ["task", "message", "TASK-test", "--cwd", str(tmp_path)],
    )
    assert missing.exit_code == 2
    assert FakeTaskService.instances == []

    result = runner.invoke(
        aros_cmd.aros_app,
        [
            "task",
            "message",
            "TASK-test",
            "--cwd",
            str(tmp_path),
            "--message",
            "record the exact output",
        ],
    )

    assert result.exit_code == 0, result.output
    assert FakeTaskService.instances[0].calls == [
        ("message", "TASK-test", "record the exact output", "human"),
    ]
    assert json.loads(result.output)["actor"] == "human"


def test_task_stop_requires_reason_and_uses_implicit_term(tmp_path: Path) -> None:
    missing = runner.invoke(
        aros_cmd.aros_app,
        ["task", "stop", "TASK-test", "--cwd", str(tmp_path)],
    )
    assert missing.exit_code == 2
    assert FakeTaskService.instances == []

    result = runner.invoke(
        aros_cmd.aros_app,
        [
            "task",
            "stop",
            "TASK-test",
            "--cwd",
            str(tmp_path),
            "--reason",
            "human requested shutdown",
            "--actor",
            "owner",
        ],
    )

    assert result.exit_code == 0, result.output
    assert FakeTaskService.instances[0].calls == [
        ("stop", "TASK-test", "owner", "human requested shutdown", "TERM"),
    ]
    assert json.loads(result.output)["reason"] == "human requested shutdown"


@pytest.mark.parametrize("action", ["collect", "preserve", "prune"])
def test_task_final_actions_forward_directly(
    tmp_path: Path,
    action: str,
) -> None:
    result = runner.invoke(
        aros_cmd.aros_app,
        ["task", action, "TASK-test", "--cwd", str(tmp_path)],
    )

    assert result.exit_code == 0, result.output
    assert FakeTaskService.instances[0].root == tmp_path.resolve()
    assert FakeTaskService.instances[0].calls == [(action, "TASK-test")]
    assert json.loads(result.output) == {
        "state": action + ("d" if action.endswith("e") else "ed"),
        "task_id": "TASK-test",
    }


def test_task_service_errors_are_reported_with_exit_code_two(tmp_path: Path) -> None:
    FakeTaskService.error = TaskError("invalid task brief")

    result = runner.invoke(
        aros_cmd.aros_app,
        ["task", "status", "TASK-test", "--cwd", str(tmp_path)],
    )

    assert result.exit_code == 2
    assert "error: invalid task brief" in result.output


def test_task_commands_do_not_construct_providers_or_import_legacy_control_plane() -> None:
    source = Path("src/cli/commands/aros_cmd.py").read_text(encoding="utf-8")
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
    task_functions = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("task_")
    ]
    assert {node.name for node in task_functions} == {
        "task_create_command",
        "task_start_command",
        "task_status_command",
        "task_list_command",
        "task_message_command",
        "task_stop_command",
        "task_collect_command",
        "task_preserve_command",
        "task_prune_command",
    }
    for function in task_functions:
        calls = {
            node.func.id
            for node in ast.walk(function)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "create_provider" not in calls
