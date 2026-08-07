from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from arbor.aros.runs import RunService
from arbor.aros.tasks import TaskService
from arbor.aros.workspace import init_workspace


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _git_bytes(root: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
    ).stdout


def _workspace(root: Path) -> None:
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "Task on Run Test")
    _git(root, "config", "user.email", "task-run@example.invalid")
    (root / "README.md").write_text("# fixture\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-qm", "base")
    init_workspace(root, "Test Task on Run.")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "initialize AROS")


def _brief(service: TaskService) -> dict[str, object]:
    return service.create(
        "Produce one reviewed return.",
        actor="principal",
        mode="write",
        adapter_argv=[sys.executable, "worker.py"],
        capabilities={"network": False, "shell": True},
        deliverables=["tasks/<task-id>/return.json"],
        acceptance=["return commit is valid"],
        timeout_seconds=60,
        idempotency_key="task-on-run-contract",
    )


def _commit(root: Path):
    from arbor.aros.checkpoint import GitCheckpoint

    return GitCheckpoint(root).commit_paths


def _launched_run_status(root: Path, run_id: str) -> dict[str, object]:
    manifest = json.loads(
        (root / "runs" / run_id / "manifest.json").read_text(encoding="utf-8")
    )
    launched_at = "2026-08-07T00:00:00.000Z"
    return {
        "schema_version": 1,
        "run_id": run_id,
        "state": "launched",
        "manifest_sha256": manifest["manifest_sha256"],
        "updated_at": launched_at,
        "actor": "principal",
        "carrier": "tmux",
        "tmux_session": f"aros-{run_id.lower()}",
        "host": "task-on-run.test",
        "launch_receipt_sha256": "a" * 64,
        "launched_at": launched_at,
    }


def _running_run_status(root: Path, run_id: str) -> dict[str, object]:
    launched = _launched_run_status(root, run_id)
    running_at = "2026-08-07T00:00:01.000Z"
    return {
        **launched,
        "state": "running",
        "updated_at": running_at,
        "runner_pid": 101,
        "process_pid": 102,
        "process_pgid": 102,
        "process_start_token": "linux-proc-start:1234",
        "started_at": running_at,
        "heartbeat_at": running_at,
    }


def _json_keys(value: object) -> Iterator[str]:
    if isinstance(value, dict):
        for key, nested in value.items():
            yield key
            yield from _json_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _json_keys(nested)


def test_start_commits_one_run_manifest_before_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _brief(service)
    _git(tmp_path, "add", f"tasks/{brief['task_id']}/brief.json")
    _git(tmp_path, "commit", "-qm", "record task brief")
    started_run_ids: list[str] = []

    def fake_start(
        _service: RunService,
        run_id: str,
        *,
        actor: str | None = None,
    ) -> dict[str, object]:
        manifests = sorted(tmp_path.glob("runs/RUN-*/manifest.json"))
        assert len(manifests) == 1
        manifest_path = manifests[0]
        assert manifest_path == tmp_path / "runs" / run_id / "manifest.json"
        binding_path = (
            tmp_path / ".aros/tasks" / str(brief["task_id"]) / "run.json"
        )
        assert binding_path.is_file()
        binding = json.loads(binding_path.read_text(encoding="utf-8"))
        assert binding["task_id"] == brief["task_id"]
        assert binding["run_id"] == run_id
        assert _git_bytes(tmp_path, "show", f"HEAD:runs/{run_id}/manifest.json") == (
            manifest_path.read_bytes()
        )
        started_run_ids.append(run_id)
        return _launched_run_status(tmp_path, run_id)

    monkeypatch.setattr(RunService, "start", fake_start)

    status = service.start(
        str(brief["task_id"]),
        actor="principal",
        commit_paths=_commit(tmp_path),
    )

    binding = json.loads(
        (
            tmp_path / ".aros/tasks" / str(brief["task_id"]) / "run.json"
        ).read_text(encoding="utf-8")
    )
    assert binding["task_id"] == brief["task_id"]
    assert status["run_id"] == binding["run_id"]
    assert started_run_ids == [binding["run_id"]]
    assert _git(tmp_path, "show", f"HEAD:runs/{binding['run_id']}/manifest.json")
    assert not (tmp_path / ".aros/tasks" / str(brief["task_id"]) / "launch.json").exists()


def test_status_projects_run_identity_without_task_process_claims(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _brief(service)
    _git(tmp_path, "add", f"tasks/{brief['task_id']}/brief.json")
    _git(tmp_path, "commit", "-qm", "record task brief")

    def fake_start(
        _service: RunService,
        run_id: str,
        *,
        actor: str | None = None,
    ) -> dict[str, object]:
        return _launched_run_status(tmp_path, run_id)

    def fake_status(
        _service: RunService,
        run_id: str,
        *,
        reconcile: bool = True,
        reader: object | None = None,
    ) -> dict[str, object]:
        return _running_run_status(tmp_path, run_id)

    monkeypatch.setattr(RunService, "start", fake_start)
    monkeypatch.setattr(RunService, "status", fake_status)

    started = service.start(
        str(brief["task_id"]),
        actor="principal",
        commit_paths=_commit(tmp_path),
    )
    status = service.status(str(brief["task_id"]))
    binding = json.loads(
        (
            tmp_path / ".aros/tasks" / str(brief["task_id"]) / "run.json"
        ).read_text(encoding="utf-8")
    )

    assert started["run_id"] == binding["run_id"]
    assert status["run_id"] == binding["run_id"]
    assert status["state"] == "running"
    assert status["updated_at"] == "2026-08-07T00:00:01.000Z"
    manifest = json.loads(
        (
            tmp_path / "runs" / str(binding["run_id"]) / "manifest.json"
        ).read_text(encoding="utf-8")
    )
    assert status["run_manifest_sha256"] == manifest["manifest_sha256"]
    assert status["final_ref"] is None
    assert status["reason"] is None
    assert set(status) == {
        "schema_version",
        "task_id",
        "state",
        "brief_sha256",
        "ownership_sha256",
        "run_id",
        "run_manifest_sha256",
        "updated_at",
        "final_ref",
        "reason",
    }
    forbidden = (
        "pid",
        "pgid",
        "token",
        "carrier",
        "tmux",
        "launch",
        "execution",
        "adapter",
    )
    assert not {
        key for key in status if any(fragment in key for fragment in forbidden)
    }


def test_task_runtime_contains_no_duplicate_process_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _brief(service)
    _git(tmp_path, "add", f"tasks/{brief['task_id']}/brief.json")
    _git(tmp_path, "commit", "-qm", "record task brief")

    def fake_start(
        _service: RunService,
        run_id: str,
        *,
        actor: str | None = None,
    ) -> dict[str, object]:
        return _running_run_status(tmp_path, run_id)

    monkeypatch.setattr(RunService, "start", fake_start)

    service.start(
        str(brief["task_id"]),
        actor="principal",
        commit_paths=_commit(tmp_path),
    )

    runtime = tmp_path / ".aros/tasks" / str(brief["task_id"])
    entries = {
        path.name
        for path in runtime.iterdir()
    }
    assert entries <= {
        "status.json",
        "ownership.json",
        "run.json",
        "messages",
        "home",
        "tmp",
    }
    forbidden = (
        "pid",
        "pgid",
        "start_token",
        "carrier",
        "tmux",
        "launch_sha256",
        "execution_sha256",
        "adapter_sha256",
    )
    for path in runtime.rglob("*.json"):
        record = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(record, dict)
        assert not {
            key
            for key in _json_keys(record)
            if any(fragment in key for fragment in forbidden)
        }, path
