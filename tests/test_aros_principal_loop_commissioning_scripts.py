from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
ADAPTER = ROOT / "commissioning/principal_loop/task_adapter.py"
SCORER = ROOT / "commissioning/principal_loop/evaluation/score.py"
DRIVER = ROOT / "scripts/commission_aros_principal_loop.py"
VERIFIER = ROOT / "scripts/verify_aros_principal_loop_commissioning.py"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_scorer_emits_one_strict_metric(tmp_path: Path) -> None:
    (tmp_path / "candidate-mode.txt").write_text("success\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(SCORER)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )

    assert json.loads(result.stdout) == {
        "schema_version": 1,
        "metric": 1.0,
        "sample_count": 1,
    }
    assert result.stderr == ""


def test_scorer_fails_closed_without_success_input(tmp_path: Path) -> None:
    (tmp_path / "candidate-mode.txt").write_text("baseline\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(SCORER)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert result.stdout == ""


def test_task_adapter_writes_strict_b_c_r_topology(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "adapter@example.invalid")
    _git(tmp_path, "config", "user.name", "Adapter Test")
    (tmp_path / "baseline.txt").write_text("baseline\n", encoding="utf-8")
    _git(tmp_path, "add", "baseline.txt")
    _git(tmp_path, "commit", "-qm", "baseline")
    base = _git(tmp_path, "rev-parse", "HEAD")
    task_id = "TASK-20260805-commissioning-adapter-test"
    brief_sha256 = "a" * 64
    environment = {
        **os.environ,
        "AROS_TASK_ID": task_id,
        "AROS_TASK_BRIEF_SHA256": brief_sha256,
        "AROS_TASK_BASE_COMMIT": base,
    }

    subprocess.run(
        [sys.executable, str(ADAPTER)],
        cwd=tmp_path,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    return_commit = _git(tmp_path, "rev-parse", "HEAD")
    child_commit = _git(tmp_path, "rev-parse", "HEAD^")
    assert _git(tmp_path, "rev-parse", "HEAD^^") == base
    assert _git(tmp_path, "diff-tree", "--no-commit-id", "--name-only", "-r", child_commit) == "candidate-mode.txt"
    assert _git(tmp_path, "diff-tree", "--no-commit-id", "--name-only", "-r", return_commit) == f"tasks/{task_id}/return.json"
    returned = json.loads((tmp_path / "tasks" / task_id / "return.json").read_bytes())
    assert returned["child_commit"] == child_commit
    assert returned["base_commit"] == base
    assert returned["brief_sha256"] == brief_sha256
    unhashed = {key: value for key, value in returned.items() if key != "return_sha256"}
    canonical = json.dumps(
        unhashed,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert returned["return_sha256"] == hashlib.sha256(canonical).hexdigest()


def test_verifier_rejects_unrelated_task_and_measurement(tmp_path: Path) -> None:
    evidence = {
        "schema_version": 1,
        "enforcement_class": "cooperative",
        "project": str(tmp_path),
        "task": {
            "task_id": "TASK-test",
            "child_commit": "1" * 40,
            "return_commit": "3" * 40,
            "collected_ref": "tasks/TASK-test/collected.json",
            "collected_sha256": "a" * 64,
        },
        "eval": {
            "eval_id": "EVAL-" + "b" * 64,
            "candidate_commit": "2" * 40,
            "receipt_ref": "eval/evaluations/EVAL-" + "b" * 64 + "/receipt.json",
            "receipt_sha256": "c" * 64,
            "metric": 1.0,
        },
        "checkpoint": {
            "transition_id": "T-E2E-ASSIMILATE",
            "base_commit": "4" * 40,
            "commit": "5" * 40,
            "receipt_sha256": "d" * 64,
        },
        "restart": {
            "complete_packet": {},
            "missing_cache_packet": {},
            "rebuilt_packet": {},
        },
        "commands": [],
    }
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(evidence), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(VERIFIER), str(path)],
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "candidate_commit" in result.stderr


def test_driver_exposes_one_explicit_aros_entry_and_runtime() -> None:
    result = subprocess.run(
        [sys.executable, str(DRIVER), "--help"],
        capture_output=True,
        text=True,
        check=True,
    )

    assert "--aros" in result.stdout
    assert "--runtime" in result.stdout
    assert "opencode" not in result.stdout.lower()
