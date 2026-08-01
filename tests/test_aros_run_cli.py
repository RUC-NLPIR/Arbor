"""CLI tests for durable AROS runs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from arbor.cli.commands import aros_cmd


runner = CliRunner()


class FakeRunService:
    instances: list["FakeRunService"] = []

    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[tuple[Any, ...]] = []
        self.instances.append(self)

    def prepare(
        self,
        argv: list[str],
        *,
        cwd: str,
        timeout_seconds: int,
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
        return {"run_id": "RUN-test", "state": "prepared"}

    def start(self, run_id: str) -> dict[str, Any]:
        self.calls.append(("start", run_id))
        return {"run_id": run_id, "state": "running"}

    def status(self, run_id: str) -> dict[str, Any]:
        self.calls.append(("status", run_id))
        return {"run_id": run_id, "state": "running"}

    def list(self) -> list[dict[str, Any]]:
        self.calls.append(("list",))
        return [{"run_id": "RUN-test", "state": "running"}]

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


def _install_fake(monkeypatch) -> None:
    FakeRunService.instances.clear()
    monkeypatch.setattr(aros_cmd, "RunService", FakeRunService)


def test_run_start_prepares_then_starts_exact_argv(
    tmp_path: Path, monkeypatch,
) -> None:
    _install_fake(monkeypatch)

    result = runner.invoke(
        aros_cmd.aros_app,
        [
            "run",
            "start",
            "--cwd",
            str(tmp_path),
            "--run-cwd",
            "experiments/demo",
            "--timeout-seconds",
            "42",
            "--idempotency-key",
            "demo-1",
            "--actor",
            "principal",
            "--label",
            "demo run",
            "--security-profile",
            "trusted-local",
            "--writable-path",
            "artifacts",
            "--writable-path",
            "checkpoints",
            "--",
            "python",
            "-m",
            "demo.train",
            "--seed",
            "7",
        ],
    )

    assert result.exit_code == 0, result.output
    service = FakeRunService.instances[0]
    assert service.root == tmp_path.resolve()
    assert service.calls == [
        (
            "prepare",
            ["python", "-m", "demo.train", "--seed", "7"],
            "experiments/demo",
            42,
            "demo-1",
            "principal",
            "demo run",
            "trusted-local",
            ["artifacts", "checkpoints"],
        ),
        ("start", "RUN-test"),
    ]
    assert json.loads(result.output) == {"run_id": "RUN-test", "state": "running"}


def test_run_start_defaults_to_isolated_linux_without_writable_paths(
    tmp_path: Path, monkeypatch,
) -> None:
    _install_fake(monkeypatch)

    result = runner.invoke(
        aros_cmd.aros_app,
        [
            "run",
            "start",
            "--cwd",
            str(tmp_path),
            "--idempotency-key",
            "isolated-default",
            "--",
            "python",
            "train.py",
        ],
    )

    assert result.exit_code == 0, result.output
    assert FakeRunService.instances[0].calls[0] == (
        "prepare",
        ["python", "train.py"],
        ".",
        3600,
        "isolated-default",
        "human",
        None,
        "isolated-linux",
        [],
    )


def test_run_start_requires_command(tmp_path: Path, monkeypatch) -> None:
    _install_fake(monkeypatch)

    result = runner.invoke(
        aros_cmd.aros_app,
        [
            "run",
            "start",
            "--cwd",
            str(tmp_path),
            "--idempotency-key",
            "demo-1",
        ],
    )

    assert result.exit_code == 2
    assert FakeRunService.instances == []


def test_run_start_requires_double_dash_before_command(
    tmp_path: Path, monkeypatch,
) -> None:
    _install_fake(monkeypatch)

    result = runner.invoke(
        aros_cmd.aros_app,
        [
            "run",
            "start",
            "--cwd",
            str(tmp_path),
            "--idempotency-key",
            "demo-1",
            "python",
            "train.py",
        ],
    )

    assert result.exit_code == 2
    assert "must follow --" in result.output
    assert FakeRunService.instances == []


def test_run_status_and_list_emit_json(tmp_path: Path, monkeypatch) -> None:
    _install_fake(monkeypatch)

    status = runner.invoke(
        aros_cmd.aros_app,
        ["run", "status", "RUN-test", "--cwd", str(tmp_path)],
    )
    assert status.exit_code == 0, status.output
    assert json.loads(status.output)["state"] == "running"
    assert FakeRunService.instances[-1].calls == [("status", "RUN-test")]

    listed = runner.invoke(
        aros_cmd.aros_app,
        ["run", "list", "--cwd", str(tmp_path)],
    )
    assert listed.exit_code == 0, listed.output
    assert json.loads(listed.output) == [
        {"run_id": "RUN-test", "state": "running"},
    ]
    assert FakeRunService.instances[-1].calls == [("list",)]


def test_run_tail_emits_selected_stream_verbatim(
    tmp_path: Path, monkeypatch,
) -> None:
    _install_fake(monkeypatch)

    result = runner.invoke(
        aros_cmd.aros_app,
        [
            "run",
            "tail",
            "RUN-test",
            "--cwd",
            str(tmp_path),
            "--stream",
            "stderr",
            "--max-bytes",
            "2048",
        ],
    )

    assert result.exit_code == 0, result.output
    assert result.output == "first line\nsecond line\n"
    assert FakeRunService.instances[0].calls == [
        ("tail", "RUN-test", "stderr", 2048),
    ]


def test_run_stop_requires_reason_and_defaults_actor_to_human(
    tmp_path: Path, monkeypatch,
) -> None:
    _install_fake(monkeypatch)

    missing_reason = runner.invoke(
        aros_cmd.aros_app,
        ["run", "stop", "RUN-test", "--cwd", str(tmp_path)],
    )
    assert missing_reason.exit_code == 2
    assert FakeRunService.instances == []

    result = runner.invoke(
        aros_cmd.aros_app,
        [
            "run",
            "stop",
            "RUN-test",
            "--cwd",
            str(tmp_path),
            "--reason",
            "human requested shutdown",
            "--signal",
            "INT",
        ],
    )

    assert result.exit_code == 0, result.output
    assert FakeRunService.instances[0].calls == [
        ("stop", "RUN-test", "human", "human requested shutdown", "INT"),
    ]
    assert json.loads(result.output)["actor"] == "human"
