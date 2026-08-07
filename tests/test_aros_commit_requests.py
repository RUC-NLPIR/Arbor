"""Owning services return plain paths and messages for Git commits."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from arbor.aros.eval import EvalService
from arbor.aros.runs import RunService
from arbor.aros.tasks import TaskService


def _repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    subprocess.run(
        ["git", "-C", str(root), "config", "user.name", "Commit Request Test"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    (root / ".gitignore").write_text("/.aros/\n/.worktree/\n", encoding="utf-8")
    (root / "AROS.md").write_text("# Mission\n", encoding="utf-8")
    (root / "memory").mkdir()
    (root / "memory/NOW.md").write_text("# NOW\n", encoding="utf-8")
    (root / ".aros").mkdir()
    subprocess.run(
        ["git", "-C", str(root), "add", ".gitignore", "AROS.md", "memory/NOW.md"],
        check=True,
    )
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "base"], check=True)


def test_task_create_returns_plain_commit_request(tmp_path: Path) -> None:
    _repo(tmp_path)

    record, paths, message = TaskService(tmp_path).create_with_commit(
        "Inspect one direction",
        actor="principal",
        mode="write",
        adapter_argv=["researcher"],
        capabilities={"network": False, "shell": True},
        deliverables=["report.md"],
        acceptance=["read report"],
        timeout_seconds=60,
        idempotency_key="task-commit-request",
    )

    task_id = str(record["task_id"])
    assert paths == (f"tasks/{task_id}/brief.json",)
    assert message == f"Record task {task_id} brief"


def test_run_prepare_returns_plain_commit_request(tmp_path: Path) -> None:
    _repo(tmp_path)

    record, paths, message = RunService(tmp_path).prepare_with_commit(
        [sys.executable, "-c", "print('measurement')"],
        idempotency_key="run-commit-request",
        actor="principal",
        security_profile="trusted-local",
    )

    run_id = str(record["run_id"])
    assert paths == (f"runs/{run_id}/manifest.json",)
    assert message == f"Record run {run_id} manifest"


def test_eval_run_returns_plain_commit_request_only_for_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = EvalService.__new__(EvalService)
    receipt = {
        "eval_id": "EVAL-" + "a" * 64,
        "receipt_sha256": "b" * 64,
        "run_id": "RUN-eval",
    }
    monkeypatch.setattr(service, "run", lambda *_args, **_kwargs: dict(receipt))

    record, paths, message = service.run_with_commit(
        "quality",
        "1",
        "c" * 40,
        actor="principal",
        idempotency_key="eval-commit-request",
    )

    assert record == receipt
    assert paths == (
        f"eval/evaluations/{receipt['eval_id']}/receipt.json",
        "runs/RUN-eval/final.json",
        "runs/RUN-eval/manifest.json",
    )
    assert message == f"Record evaluation {receipt['eval_id']} receipt"

    monkeypatch.setattr(
        service,
        "run",
        lambda *_args, **_kwargs: {
            "eval_id": "EVAL-" + "d" * 64,
            "evaluation_state": "lost",
        },
    )
    _lost, lost_paths, lost_message = service.run_with_commit(
        "quality",
        "1",
        "c" * 40,
        actor="principal",
        idempotency_key="eval-lost",
    )
    assert lost_paths is None
    assert lost_message is None
