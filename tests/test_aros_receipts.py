"""Pure receipt hashing compatibility tests."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import arbor.aros.runner as runner_module
import arbor.aros.runs as runs_module
import arbor.aros.task_runner as task_runner_module
import arbor.aros.tasks as tasks_module
import pytest
from arbor.aros.receipts import content_receipt, digest_chunks, record_sha256
from arbor.aros.store import FINAL_IDENTITY_FIELDS, json_sha256
from tests.test_aros_runs import (
    _init_clean_repo,
    _prepare,
    _require_tmux,
    _wait_for_state,
)
from tests.test_aros_task_runner import _create_committed_task, _wait_terminal


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


def _worktree_python_launcher(root: Path) -> Path:
    import_root = root / "runner-import"
    import_root.mkdir()
    (import_root / "arbor").symlink_to(
        Path(__file__).resolve().parents[1] / "src",
        target_is_directory=True,
    )
    launcher = root / "python"
    launcher.write_text(
        f"#!{sys.executable}\n"
        "import os\n"
        "import sys\n"
        f"os.environ['PYTHONPATH'] = {str(import_root)!r}\n"
        f"os.execv({sys.executable!r}, [{sys.executable!r}, *sys.argv[1:]])\n",
        encoding="utf-8",
    )
    launcher.chmod(0o700)
    return launcher


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
    _init_clean_repo(root)
    launcher = _worktree_python_launcher(tmp_path)
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "runner-import"))
    service, manifest = _prepare(
        root,
        argv=[
            sys.executable,
            "-c",
            "import sys;print('run stdout');print('run stderr', file=sys.stderr)",
        ],
        key="receipt-compatibility-run",
    )
    run_id = str(manifest["run_id"])
    monkeypatch.setattr(runs_module, "sys", SimpleNamespace(executable=str(launcher)))

    try:
        service.start(run_id)
        terminal = _wait_for_state(service, run_id)
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


def test_completed_task_persists_compatible_receipts_and_self_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_tmux()
    observed_record_sha256 = Mock(wraps=record_sha256)
    observed_digest_chunks = Mock(wraps=digest_chunks)
    observed_content_receipt = Mock(wraps=content_receipt)

    monkeypatch.setattr(
        tasks_module, "record_sha256", observed_record_sha256, raising=False
    )
    monkeypatch.setattr(
        tasks_module, "digest_chunks", observed_digest_chunks, raising=False
    )
    monkeypatch.setattr(
        tasks_module, "content_receipt", observed_content_receipt, raising=False
    )
    root = tmp_path / "workspace"
    service, brief = _create_committed_task(
        root,
        [
            sys.executable,
            "-c",
            "import sys;print('task stdout');print('task stderr', file=sys.stderr)",
        ],
        key="receipt-compatibility-task",
    )
    task_id = str(brief["task_id"])
    socket_name = tasks_module._tmux_socket_name(root, task_id)

    try:
        service.start(task_id)
        terminal = _wait_terminal(service, task_id)
        runtime = root / ".aros" / "tasks" / task_id
        final = json.loads((runtime / "final.json").read_text(encoding="utf-8"))
        expected_outputs = {
            "stdout": b"task stdout\n",
            "stderr": b"task stderr\n",
        }

        assert terminal["state"] == "completed"
        assert set(final) == task_runner_module._FINAL_FIELDS
        assert final["state"] == "completed"
        assert final["exit_code"] == 0
        assert final["brief_sha256"] == brief["brief_sha256"]
        for stream, content in expected_outputs.items():
            relative = f".aros/tasks/{task_id}/{stream}.log"
            expected = content_receipt(
                relative,
                *digest_chunks([content]),
            )
            assert final[stream] == expected
            assert tasks_module._file_receipt(
                runtime / f"{stream}.log",
                relative,
                permissions_enforced=bool(
                    final["filesystem_permissions_enforced"]
                ),
            ) == expected
        assert final["final_sha256"] == record_sha256(final, "final_sha256")
        assert tasks_module._record_sha256(final, "final_sha256") == final[
            "final_sha256"
        ]
        assert observed_record_sha256.call_count >= 1
        assert observed_digest_chunks.call_count >= 2
        assert observed_content_receipt.call_count >= 2
    finally:
        with suppress(Exception):
            status = service.status(task_id)
            if status["state"] in {"launched", "running"}:
                service.stop(
                    task_id,
                    actor="principal",
                    reason="receipt compatibility cleanup",
                )
        subprocess.run(
            ["tmux", "-L", socket_name, "kill-server"],
            capture_output=True,
            check=False,
        )
