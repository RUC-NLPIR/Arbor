"""Principal-facing system call for durable AROS runs."""

from __future__ import annotations

import asyncio
import ast
import json
from pathlib import Path
from typing import Any

import pytest

from arbor.aros import run_tool
from arbor.aros.operational import build_operational_intent
from arbor.aros.run_tool import RunTool
from arbor.aros.runs import RunError


class FakeRunService:
    instances: list["FakeRunService"] = []

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.calls: list[tuple[Any, ...]] = []
        self.instances.append(self)

    def prepare(
        self,
        argv: list[str],
        *,
        cwd: str,
        timeout_seconds: float,
        idempotency_key: str,
        actor: str,
        label: str | None = None,
        security_profile: str,
        writable_paths: list[str],
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "prepare",
                argv,
                cwd,
                timeout_seconds,
                idempotency_key,
                actor,
                label,
                security_profile,
                writable_paths,
            )
        )
        return {
            "run_id": "RUN-test",
            "state": "prepared",
            "manifest_sha256": "a" * 64,
        }

    def prepare_with_operational_intent(self, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        manifest = self.prepare(*args, **kwargs)
        return manifest, build_operational_intent(
            ("runs/RUN-test/manifest.json",),
            "a" * 64,
        )

    def start(self, run_id: str, *, actor: str | None = None) -> dict[str, Any]:
        self.calls.append(("start", run_id, actor))
        return {"run_id": run_id, "state": "running"}

    def status(self, run_id: str) -> dict[str, Any]:
        self.calls.append(("status", run_id))
        return {
            "run_id": run_id,
            "state": "completed" if run_id == "RUN-terminal" else "running",
        }

    def terminal_operational_intent(self, run_id: str):  # type: ignore[no-untyped-def]
        self.calls.append(("terminal_intent", run_id))
        if run_id != "RUN-terminal":
            return None
        return build_operational_intent(
            ("runs/RUN-terminal/final.json",),
            "f" * 64,
        )

    def list(self) -> list[dict[str, Any]]:
        self.calls.append(("list",))
        return [{"run_id": "RUN-test", "state": "completed"}]

    def tail(self, run_id: str, *, stream: str, max_bytes: int) -> str:
        self.calls.append(("tail", run_id, stream, max_bytes))
        return "first line\nsecond line\n"

    def stop(
        self,
        run_id: str,
        *,
        actor: str,
        reason: str,
        signal_name: str,
    ) -> dict[str, Any]:
        self.calls.append(("stop", run_id, actor, reason, signal_name))
        return {
            "run_id": run_id,
            "actor": actor,
            "reason": reason,
            "signal": signal_name,
        }


@pytest.fixture(autouse=True)
def fake_run_service(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeRunService.instances.clear()
    monkeypatch.setattr(run_tool, "RunService", FakeRunService)


def _execute(tool: RunTool, **kwargs: Any) -> str:
    return asyncio.run(tool.execute(**kwargs))


def test_run_tool_exposes_one_action_based_system_call(tmp_path: Path) -> None:
    tool = RunTool(cwd=str(tmp_path))

    assert tool.name == "Run"
    assert tool.input_schema["properties"]["action"]["enum"] == [
        "start",
        "status",
        "list",
        "tail",
        "stop",
    ]
    assert tool.input_schema["properties"]["security_profile"] == {
        "type": "string",
        "enum": ["isolated-linux", "trusted-local"],
        "default": "isolated-linux",
        "description": "Run isolation profile (default: isolated-linux).",
    }
    assert tool.input_schema["properties"]["writable_paths"]["type"] == "array"


def test_start_prepares_then_starts_as_principal(tmp_path: Path) -> None:
    tool = RunTool(cwd=str(tmp_path))

    output = _execute(
        tool,
        action="start",
        argv=["python", "train.py", "--seed", "7"],
        cwd="experiments/demo",
        timeout_seconds=42,
        idempotency_key="demo-1",
        label="demo run",
    )

    service = FakeRunService.instances[0]
    assert service.root == tmp_path
    assert service.calls == [
        (
            "prepare",
            ["python", "train.py", "--seed", "7"],
            "experiments/demo",
            42,
            "demo-1",
            "principal",
            "demo run",
            "isolated-linux",
            [],
        ),
        ("start", "RUN-test", "principal"),
    ]
    assert json.loads(output) == {
        "run_id": "RUN-test",
        "state": "running",
        "admission_required": True,
        "operational_intent": {
            "schema_version": 1,
            "workspace_paths": ["runs/RUN-test/manifest.json"],
            "record_sha256": "a" * 64,
        },
    }


def test_start_admits_manifest_only_after_run_started(tmp_path: Path) -> None:
    calls: list[object] = []

    def admit(intent: object) -> dict[str, object]:
        assert FakeRunService.instances[0].calls[-1] == (
            "start",
            "RUN-test",
            "principal",
        )
        calls.append(intent)
        return {"state": "admitted", "commit": "b" * 40}

    tool = RunTool(cwd=str(tmp_path), operational_admission=admit)

    output = json.loads(
        _execute(
            tool,
            action="start",
            argv=["python", "train.py"],
            idempotency_key="run-callback",
        )
    )

    assert len(calls) == 1
    assert output["admission_required"] is False
    assert output["operational_checkpoint"] == {
        "state": "admitted",
        "commit": "b" * 40,
    }


def test_start_explicitly_forwards_trusted_local_and_writable_paths(
    tmp_path: Path,
) -> None:
    tool = RunTool(cwd=str(tmp_path))

    _execute(
        tool,
        action="start",
        argv=["python", "train.py"],
        idempotency_key="trusted-1",
        security_profile="trusted-local",
        writable_paths=["artifacts", "checkpoints", "artifacts"],
    )

    assert FakeRunService.instances[0].calls[0] == (
        "prepare",
        ["python", "train.py"],
        ".",
        3600,
        "trusted-1",
        "principal",
        None,
        "trusted-local",
        ["artifacts", "checkpoints", "artifacts"],
    )


def test_status_and_list_return_json(tmp_path: Path) -> None:
    tool = RunTool(cwd=str(tmp_path))

    status = _execute(tool, action="status", run_id="RUN-test")
    listed = _execute(tool, action="list")

    assert json.loads(status) == {"run_id": "RUN-test", "state": "running"}
    assert json.loads(listed) == [{"run_id": "RUN-test", "state": "completed"}]
    assert FakeRunService.instances[0].calls == [("status", "RUN-test")]
    assert FakeRunService.instances[1].calls == [("list",)]


def test_terminal_status_admits_final_record_at_foreground_seam(tmp_path: Path) -> None:
    calls: list[object] = []

    def admit(intent: object) -> dict[str, object]:
        calls.append(intent)
        return {"state": "admitted", "commit": "e" * 40}

    tool = RunTool(cwd=str(tmp_path), operational_admission=admit)

    output = json.loads(_execute(tool, action="status", run_id="RUN-terminal"))

    assert len(calls) == 1
    assert FakeRunService.instances[0].calls == [
        ("status", "RUN-terminal"),
        ("terminal_intent", "RUN-terminal"),
    ]
    assert output["state"] == "completed"
    assert output["admission_required"] is False
    assert output["operational_intent"]["workspace_paths"] == [
        "runs/RUN-terminal/final.json"
    ]


def test_tail_returns_selected_stream_verbatim(tmp_path: Path) -> None:
    tool = RunTool(cwd=str(tmp_path))

    output = _execute(
        tool,
        action="tail",
        run_id="RUN-test",
        stream="stderr",
        max_bytes=2048,
    )

    assert output == "first line\nsecond line\n"
    assert FakeRunService.instances[0].calls == [
        ("tail", "RUN-test", "stderr", 2048),
    ]


def test_stop_requires_reason_and_records_principal_actor(tmp_path: Path) -> None:
    tool = RunTool(cwd=str(tmp_path))

    with pytest.raises(RunError, match="reason is required"):
        _execute(tool, action="stop", run_id="RUN-test")
    assert FakeRunService.instances == []

    output = _execute(
        tool,
        action="stop",
        run_id="RUN-test",
        reason="evidence is sufficient",
        signal_name="INT",
    )

    assert FakeRunService.instances[0].calls == [
        ("stop", "RUN-test", "principal", "evidence is sufficient", "INT"),
    ]
    assert json.loads(output)["actor"] == "principal"


def test_run_tool_does_not_import_legacy_or_metric_parsing_modules() -> None:
    source = Path("src/aros/run_tool.py").read_text(encoding="utf-8")
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

    forbidden = ("coordinator", "executor", "run_training", "metric")
    assert not any(part in module for module in imported for part in forbidden)
