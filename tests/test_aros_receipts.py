"""Pure receipt hashing compatibility tests."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from unittest.mock import Mock

import arbor.aros.runner as runner_module
import arbor.aros.runs as runs_module
import pytest
from arbor.aros.checkpoint import GitCheckpoint
from arbor.aros.receipts import content_receipt, digest_chunks, record_sha256
from arbor.aros.runs import RunService
from arbor.aros.store import FINAL_IDENTITY_FIELDS, json_sha256
from arbor.aros.tasks import TaskService
from arbor.aros.workspace import init_workspace


def test_record_sha256_excludes_only_named_hash_field() -> None:
    record = {"schema_version": 1, "value": "x", "record_sha256": "old"}

    assert record_sha256(record, "record_sha256") == json_sha256(
        {"schema_version": 1, "value": "x"}
    )


def test_content_receipt_preserves_existing_shape() -> None:
    digest = hashlib.sha256(b"abc").hexdigest()

    assert content_receipt("stdout.log", 3, digest) == {
        "path": "stdout.log",
        "bytes": 3,
        "sha256": digest,
    }


def test_digest_chunks_hashes_content_without_joining() -> None:
    assert digest_chunks([b"ab", b"c"]) == (
        3,
        hashlib.sha256(b"abc").hexdigest(),
    )


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _init_repo(root: Path, *, task_workspace: bool = False) -> None:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    _git(root, "config", "user.email", "aros@example.invalid")
    _git(root, "config", "user.name", "AROS test")
    (root / "README.md").write_text("# receipt compatibility\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-qm", "initial state")
    if task_workspace:
        init_workspace(root, "Receipt compatibility")
        _git(
            root,
            "add",
            ".gitignore",
            "AGENTS.md",
            "AROS.md",
            "memory/NOW.md",
            "model/CURRENT.md",
            "questions/FRONTIER.md",
        )
        _git(root, "commit", "-qm", "initialize AROS")


def _wait_for_terminal(
    status: Callable[[], dict[str, object]],
    terminal_states: set[str],
) -> dict[str, object]:
    deadline = time.monotonic() + 10
    latest: dict[str, object] = {}
    while time.monotonic() < deadline:
        latest = status()
        if latest["state"] in terminal_states:
            return latest
        time.sleep(0.02)
    pytest.fail(f"operation did not reach a terminal state: {latest}")


def _require_tmux() -> None:
    if shutil.which("tmux") is None:
        pytest.skip("tmux is unavailable")


def test_completed_run_persists_compatible_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_tmux()
    observed_record_sha256 = Mock(wraps=record_sha256)
    observed_digest_chunks = Mock(wraps=digest_chunks)
    observed_content_receipt = Mock(wraps=content_receipt)

    monkeypatch.setattr(
        runs_module, "record_sha256", observed_record_sha256, raising=False
    )
    monkeypatch.setattr(
        runner_module, "digest_chunks", observed_digest_chunks, raising=False
    )
    monkeypatch.setattr(
        runner_module,
        "content_receipt",
        observed_content_receipt,
        raising=False,
    )
    root = tmp_path / "workspace"
    _init_repo(root)
    service = RunService(root)
    manifest = service.prepare(
        [
            sys.executable,
            "-c",
            "import sys;print('run stdout');print('run stderr', file=sys.stderr)",
        ],
        timeout_seconds=10,
        idempotency_key="receipt-compatibility-run",
        actor="test-principal",
        label="receipt-compatibility",
        security_profile="trusted-local",
    )
    run_id = str(manifest["run_id"])

    try:
        service.start(run_id)
        terminal = _wait_for_terminal(
            lambda: service.status(run_id),
            {"completed", "failed_process", "timed_out", "cancelled", "lost"},
        )
        final = json.loads(
            (root / "runs" / run_id / "final.json").read_text(encoding="utf-8")
        )
        prelaunch = json.loads(
            (
                root / ".aros" / "receipts" / f"{run_id}-prelaunch.json"
            ).read_text(encoding="utf-8")
        )
        runtime = root / ".aros" / "runs" / run_id
        expected_outputs = {
            "stdout": b"run stdout\n",
            "stderr": b"run stderr\n",
        }

        assert terminal["state"] == "completed"
        assert set(final) == {
            *FINAL_IDENTITY_FIELDS,
            "schema_version",
            "state",
            "exit_code",
            "started_at",
            "finished_at",
            "finalized_at",
            "duration_seconds",
            "resource_usage",
            "host",
            "actual_environment_sha256",
            "launch_receipt_sha256",
            "stdout",
            "stderr",
        }
        assert final["state"] == "completed"
        assert final["exit_code"] == 0
        assert final["manifest_sha256"] == manifest["manifest_sha256"]
        for stream, content in expected_outputs.items():
            relative = f".aros/runs/{run_id}/{stream}.log"
            expected = content_receipt(
                relative,
                *digest_chunks([content]),
            )
            assert final[stream] == expected
            assert runner_module._file_receipt(
                runtime / f"{stream}.log",
                relative,
            ) == expected
        assert prelaunch["receipt_sha256"] == record_sha256(
            prelaunch,
            "receipt_sha256",
        )
        assert final["launch_receipt_sha256"] == prelaunch["receipt_sha256"]
        assert observed_record_sha256.call_count >= 1
        assert observed_digest_chunks.call_count == 2
        assert observed_content_receipt.call_count == 2
    finally:
        with suppress(Exception):
            status = service.status(run_id)
            if status["state"] in {"launched", "running"}:
                service.stop(
                    run_id,
                    actor="test-principal",
                    reason="receipt compatibility cleanup",
                )
        subprocess.run(
            ["tmux", "kill-session", "-t", f"=aros-{run_id.lower()}"],
            capture_output=True,
            check=False,
        )


def test_completed_task_reads_and_verifies_its_bound_run_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_tmux()
    observed_record_sha256 = Mock(wraps=record_sha256)

    monkeypatch.setattr(
        runs_module, "record_sha256", observed_record_sha256, raising=False
    )
    root = tmp_path / "workspace"
    _init_repo(root, task_workspace=True)
    service = TaskService(root)
    brief = service.create(
        "execute exact child adapter",
        actor="principal",
        mode="write",
        adapter_argv=[
            sys.executable,
            "-c",
            "import sys;print('task stdout');print('task stderr', file=sys.stderr)",
        ],
        capabilities={"network": True, "shell": True},
        deliverables=["final receipt"],
        acceptance=["inspect immutable final receipt"],
        timeout_seconds=10,
        idempotency_key="receipt-compatibility-task",
    )
    task_id = str(brief["task_id"])
    _git(root, "add", f"tasks/{task_id}/brief.json")
    _git(root, "commit", "-qm", f"record {task_id}")
    run_id: str | None = None

    try:
        service.start(
            task_id,
            actor="principal",
            commit_paths=GitCheckpoint(root).commit_paths,
        )
        terminal = _wait_for_terminal(
            lambda: service.status(task_id),
            {"completed", "failed_process", "timed_out", "cancelled", "lost"},
        )
        run_id = str(terminal["run_id"])
        runs = RunService(root)
        final = runs.read_validated_final(run_id)
        expected_outputs = {
            "stdout": b"task stdout\n",
            "stderr": b"task stderr\n",
        }

        assert terminal["state"] == "completed"
        assert final["state"] == "completed"
        assert final["exit_code"] == 0
        assert final["run_id"] == run_id
        assert final["manifest_sha256"] == terminal["run_manifest_sha256"]
        for stream, content in expected_outputs.items():
            relative = f".aros/runs/{run_id}/{stream}.log"
            expected = content_receipt(
                relative,
                *digest_chunks([content]),
            )
            assert final[stream] == expected
            runs.verify_output(run_id, stream)
        assert observed_record_sha256.call_count >= 1
    finally:
        with suppress(Exception):
            status = service.status(task_id)
            if status["state"] in {"launched", "running"}:
                service.stop(
                    task_id,
                    actor="principal",
                    reason="receipt compatibility cleanup",
                )
        if run_id is not None:
            subprocess.run(
                ["tmux", "kill-session", "-t", f"=aros-{run_id.lower()}"],
                capture_output=True,
                check=False,
            )
