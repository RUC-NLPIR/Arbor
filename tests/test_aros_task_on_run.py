from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from arbor.aros.runs import RunError, RunService
from arbor.aros.store import json_sha256, manifest_sha256
from arbor.aros.task_run import (
    TaskRunError,
    ensure_task_run,
    load_task_run,
    project_task_status,
)
from arbor.aros.tasks import TaskError, TaskService
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


def _brief_with_key(
    service: TaskService,
    key: str,
) -> dict[str, object]:
    return service.create(
        f"Produce reviewed return {key}.",
        actor="principal",
        mode="write",
        adapter_argv=[sys.executable, "worker.py"],
        capabilities={"network": False, "shell": True},
        deliverables=["tasks/<task-id>/return.json"],
        acceptance=["return commit is valid"],
        timeout_seconds=60,
        idempotency_key=key,
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


def _prepared_task(
    root: Path,
) -> tuple[TaskService, dict[str, object], dict[str, object]]:
    _workspace(root)
    service = TaskService(root)
    brief = _brief(service)
    task_id = str(brief["task_id"])
    _git(root, "add", f"tasks/{task_id}/brief.json")
    _git(root, "commit", "-qm", "record task brief")
    service._ensure_worktree(task_id, actor="principal")
    return service, brief, service._load_ownership(brief)


def _fail_bound_run_launch(
    root: Path,
    binding: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[str, object], dict[str, object]]:
    import arbor.aros.runs as runs_module

    real_run = subprocess.run

    def fail_tmux(
        command: list[str],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        if command[0] != "/test/tmux":
            return real_run(command, **kwargs)
        return subprocess.CompletedProcess(
            command,
            7,
            stdout="",
            stderr="injected carrier failure",
        )

    monkeypatch.setattr(runs_module.shutil, "which", lambda _name: "/test/tmux")
    monkeypatch.setattr(runs_module.subprocess, "run", fail_tmux)
    run_id = str(binding["run_id"])
    service = RunService(root)
    with pytest.raises(RunError, match="tmux launch failed"):
        service.start(run_id, actor="principal")
    return service.status(run_id), service.read_validated_final(run_id)


def _task_runtime_snapshot(root: Path, task_id: str) -> dict[str, bytes]:
    runtime = root / ".aros" / "tasks" / task_id
    return {
        path.relative_to(runtime).as_posix(): path.read_bytes()
        for path in runtime.rglob("*")
        if path.is_file()
    }


def _write_json(path: Path, value: dict[str, object]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _commit_child_return(
    root: Path,
    brief: dict[str, object],
    ownership: dict[str, object],
) -> tuple[dict[str, object], str, str]:
    task_id = str(brief["task_id"])
    worktree = Path(str(ownership["worktree_path"]))
    artifact = worktree / "result.txt"
    artifact.write_text("reviewed result\n", encoding="utf-8")
    _git(worktree, "add", "result.txt")
    _git(worktree, "commit", "-qm", "produce reviewed result")
    child_commit = _git(worktree, "rev-parse", "HEAD")
    returned: dict[str, object] = {
        "schema_version": 1,
        "task_id": task_id,
        "brief_sha256": brief["brief_sha256"],
        "base_commit": brief["base_commit"],
        "child_commit": child_commit,
        "summary": "Produced one reviewed result.",
        "work_performed": ["wrote the requested result"],
        "changed_files": ["result.txt"],
        "evidence": ["the child commit contains the result"],
        "deviations": [],
        "uncertainty": [],
        "follow_up": [],
    }
    returned["return_sha256"] = json_sha256(returned)
    return_ref = f"tasks/{task_id}/return.json"
    return_path = worktree / return_ref
    return_path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(return_path, returned)
    _git(worktree, "add", return_ref)
    _git(worktree, "commit", "-qm", "record reviewed return")
    return returned, child_commit, _git(worktree, "rev-parse", "HEAD")


def _terminal_task_run(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    completed: bool = False,
    with_return: bool = True,
) -> tuple[
    TaskService,
    dict[str, object],
    dict[str, object],
    dict[str, object],
    dict[str, object] | None,
    str | None,
    str | None,
]:
    service, brief, ownership = _prepared_task(root)
    binding = ensure_task_run(
        root,
        brief,
        ownership,
        actor="principal",
        commit_paths=_commit(root),
    )
    _status, final = _fail_bound_run_launch(root, binding, monkeypatch)
    if completed:
        final = dict(final)
        final["state"] = "completed"
        final["exit_code"] = 0
        final.pop("error", None)
        _write_json(
            root / "runs" / str(binding["run_id"]) / "final.json",
            final,
        )
        final = RunService(root).read_validated_final(str(binding["run_id"]))
    if not with_return:
        return service, brief, ownership, binding, final, None, None
    _returned, child_commit, return_commit = _commit_child_return(
        root,
        brief,
        ownership,
    )
    return (
        service,
        brief,
        ownership,
        binding,
        final,
        child_commit,
        return_commit,
    )


def test_task_collection_binds_exact_owned_run_and_commits_both_new_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, brief, _ownership, binding, final, child_commit, return_commit = (
        _terminal_task_run(tmp_path, monkeypatch)
    )
    task_id = str(brief["task_id"])
    run_id = str(binding["run_id"])
    run_final_ref = f"runs/{run_id}/final.json"
    collected_ref = f"tasks/{task_id}/collected.json"

    collected, paths, message = service.collect_with_commit(task_id)

    assert set(collected) == {
        "schema_version",
        "task_id",
        "brief_sha256",
        "ownership_sha256",
        "branch_ref",
        "base_commit",
        "child_commit",
        "return_commit",
        "final_state",
        "run_id",
        "run_manifest_ref",
        "run_manifest_sha256",
        "run_final_ref",
        "run_final_sha256",
        "return",
        "collected_at",
        "collected_sha256",
    }
    assert collected["run_id"] == run_id
    assert collected["run_manifest_ref"] == binding["run_manifest_ref"]
    assert collected["run_manifest_sha256"] == binding["run_manifest_sha256"]
    assert collected["run_final_ref"] == run_final_ref
    assert collected["run_final_sha256"] == json_sha256(final)
    assert collected["final_state"] == final["state"]
    assert collected["child_commit"] == child_commit
    assert collected["return_commit"] == return_commit
    assert "final_sha256" not in collected
    assert paths == (run_final_ref, collected_ref)
    assert message == f"Record task {task_id} collection"

    checkpoint = _commit(tmp_path)(paths, message)
    assert checkpoint["paths"] == sorted(paths)
    assert _git(tmp_path, "status", "--porcelain") == ""
    repeated, repeated_paths, repeated_message = service.collect_with_commit(task_id)
    assert repeated == collected
    assert repeated_paths is None
    assert repeated_message is None


def test_task_collection_requests_only_new_collection_when_run_final_is_committed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, brief, _ownership, binding, _final, _child, _return = (
        _terminal_task_run(tmp_path, monkeypatch)
    )
    task_id = str(brief["task_id"])
    run_final_ref = f"runs/{binding['run_id']}/final.json"
    _commit(tmp_path)((run_final_ref,), "record terminal Run")

    _record, paths, _message = service.collect_with_commit(task_id)

    assert paths == (f"tasks/{task_id}/collected.json",)


def test_collect_rejects_rewritten_committed_run_final_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, brief, _ownership, binding, _final, _child, _return = (
        _terminal_task_run(tmp_path, monkeypatch)
    )
    task_id = str(brief["task_id"])
    final_ref = f"runs/{binding['run_id']}/final.json"
    final_path = tmp_path / final_ref
    _commit(tmp_path)((final_ref,), "record terminal Run")
    final_path.write_bytes(b" " + final_path.read_bytes())
    main_dirt = tmp_path / "unrelated-main-dirt.bin"
    main_dirt.write_bytes(b"main dirt\x00unchanged")
    dirt = {main_dirt: main_dirt.read_bytes()}

    with pytest.raises(TaskError, match="Run final|rewritten"):
        service.collect(task_id)

    assert not (tmp_path / "tasks" / task_id / "collected.json").exists()
    assert {path: path.read_bytes() for path in dirt} == dirt


def test_collect_rejects_whitespace_dirty_committed_collection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, brief, _ownership, _binding, _final, _child, _return = (
        _terminal_task_run(tmp_path, monkeypatch)
    )
    task_id = str(brief["task_id"])
    collected, paths, message = service.collect_with_commit(task_id)
    assert paths is not None and message is not None
    _commit(tmp_path)(paths, message)
    collected_path = tmp_path / "tasks" / task_id / "collected.json"
    collected_path.write_bytes(collected_path.read_bytes() + b" \n")

    with pytest.raises(TaskError, match="collection|rewritten"):
        service.collect(task_id)

    assert json.loads(collected_path.read_bytes()) == collected


def test_prune_rejects_whitespace_dirty_collection_without_removing_worktree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, brief, ownership, _binding, _final, _child, _return = (
        _terminal_task_run(tmp_path, monkeypatch)
    )
    task_id = str(brief["task_id"])
    _collected, paths, message = service.collect_with_commit(task_id)
    assert paths is not None and message is not None
    _commit(tmp_path)(paths, message)
    collected_path = tmp_path / "tasks" / task_id / "collected.json"
    collected_path.write_bytes(collected_path.read_bytes() + b" \n")
    worktree = Path(str(ownership["worktree_path"]))

    with pytest.raises(TaskError, match="collection|rewritten"):
        service.prune(task_id)

    assert worktree.is_dir()


def test_uncommitted_exact_collection_recovers_then_prunes_after_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, brief, ownership, binding, _final, _child, _return = (
        _terminal_task_run(tmp_path, monkeypatch)
    )
    task_id = str(brief["task_id"])
    final_ref = f"runs/{binding['run_id']}/final.json"
    collected_ref = f"tasks/{task_id}/collected.json"
    _commit(tmp_path)((final_ref,), "record terminal Run")
    created = service.collect(task_id)

    recovered, paths, message = service.collect_with_commit(task_id)

    assert recovered == created
    assert paths == (collected_ref,)
    assert message == f"Record task {task_id} collection"
    assert Path(str(ownership["worktree_path"])).is_dir()
    _commit(tmp_path)(paths, message)
    pruned = service.prune(task_id)
    assert pruned["state"] == "pruned"
    assert pruned["collected_sha256"] == created["collected_sha256"]


def test_prune_rejects_advanced_return_branch_before_worktree_removal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, brief, ownership, _binding, _final, _child, _return = (
        _terminal_task_run(tmp_path, monkeypatch)
    )
    task_id = str(brief["task_id"])
    _collected, paths, message = service.collect_with_commit(task_id)
    assert paths is not None and message is not None
    _commit(tmp_path)(paths, message)
    worktree = Path(str(ownership["worktree_path"]))
    (worktree / "post-collection.txt").write_text("advance\n", encoding="utf-8")
    _git(worktree, "add", "post-collection.txt")
    _git(worktree, "commit", "-qm", "advance retained task branch")
    before = {
        path.relative_to(worktree).as_posix(): path.read_bytes()
        for path in worktree.rglob("*")
        if path.is_file()
    }

    with pytest.raises(TaskError, match="branch|collection"):
        service.prune(task_id)

    assert worktree.is_dir()
    assert {
        path.relative_to(worktree).as_posix(): path.read_bytes()
        for path in worktree.rglob("*")
        if path.is_file()
    } == before


def test_collection_normalizes_invalid_utf8_git_path_to_task_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, brief, _ownership, binding, _final, _child, _return = (
        _terminal_task_run(tmp_path, monkeypatch)
    )
    task_id = str(brief["task_id"])
    final_ref = f"runs/{binding['run_id']}/final.json"
    invalid = os.fsencode(tmp_path) + b"/invalid-\xff-path"
    descriptor = os.open(invalid, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.write(descriptor, b"preserve invalid path\n")
    os.close(descriptor)
    subprocess.run(
        [
            b"git",
            b"-C",
            os.fsencode(tmp_path),
            b"add",
            os.fsencode(final_ref),
            b"invalid-\xff-path",
        ],
        check=True,
    )
    subprocess.run(
        [b"git", b"-C", os.fsencode(tmp_path), b"commit", b"-qm", b"invalid path"],
        check=True,
    )

    with pytest.raises(TaskError, match="UTF-8|commit paths|Run final"):
        service.collect(task_id)

    assert not (tmp_path / "tasks" / task_id / "collected.json").exists()
    assert Path(os.fsdecode(invalid)).read_bytes() == b"preserve invalid path\n"


def test_collect_and_status_serialize_terminal_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, brief, _ownership, binding, _final, _child, _return = (
        _terminal_task_run(tmp_path, monkeypatch)
    )
    task_id = str(brief["task_id"])
    callback_entered = threading.Event()
    release_callback = threading.Event()
    status_finished = threading.Event()
    commits: list[tuple[str, ...]] = []

    def blocking_commit(
        paths: tuple[str, ...],
        message: str,
    ) -> dict[str, object]:
        commits.append(paths)
        callback_entered.set()
        assert release_callback.wait(timeout=5)
        return _commit(tmp_path)(paths, message)

    def collect_once() -> tuple[dict[str, object], dict[str, object] | None]:
        return service.collect_and_commit(task_id, blocking_commit)

    def status_once() -> dict[str, object]:
        result = service.status(task_id, commit_paths=_commit(tmp_path))
        status_finished.set()
        return result

    with ThreadPoolExecutor(max_workers=2) as executor:
        collected_future = executor.submit(collect_once)
        assert callback_entered.wait(timeout=5)
        status_future = executor.submit(status_once)
        try:
            assert not status_finished.wait(timeout=0.2)
        finally:
            release_callback.set()
        collected, checkpoint = collected_future.result(timeout=5)
        status = status_future.result(timeout=5)

    assert checkpoint is not None
    assert checkpoint["commit"] == _git(tmp_path, "rev-parse", "HEAD")
    assert commits == [
        (
            f"runs/{binding['run_id']}/final.json",
            f"tasks/{task_id}/collected.json",
        )
    ]
    assert status["final_ref"] == collected["run_final_ref"]
    assert service.collect(task_id) == collected


def test_task_prune_receipt_preserves_collection_run_lineage_transitively(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, brief, _ownership, binding, _final, _child, _return = (
        _terminal_task_run(tmp_path, monkeypatch)
    )
    task_id = str(brief["task_id"])
    collected, paths, message = service.collect_with_commit(task_id)
    assert paths is not None and message is not None
    _commit(tmp_path)(paths, message)

    preserved = service.preserve(task_id)
    pruned = service.prune(task_id)

    assert preserved["head_commit"] == collected["return_commit"]
    assert pruned["state"] == "pruned"
    assert pruned["collected_sha256"] == collected["collected_sha256"]
    assert pruned["final_state"] == collected["final_state"]
    assert "final_sha256" not in pruned
    assert service.collect(task_id) == collected
    assert pruned["task_id"] == task_id
    assert collected["run_id"] == binding["run_id"]


@pytest.mark.parametrize("precommitted", [False, True])
def test_completed_no_return_commits_only_uncommitted_run_final(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    precommitted: bool,
) -> None:
    service, brief, _ownership, binding, final, _child, _return = _terminal_task_run(
        tmp_path,
        monkeypatch,
        completed=True,
        with_return=False,
    )
    task_id = str(brief["task_id"])
    run_final_ref = f"runs/{binding['run_id']}/final.json"
    if precommitted:
        _commit(tmp_path)((run_final_ref,), "record terminal Run")

    result, paths, message = service.collect_with_commit(task_id)

    assert result == {
        "schema_version": 1,
        "task_id": task_id,
        "state": "completed_no_return",
        "brief_sha256": brief["brief_sha256"],
        "ownership_sha256": binding["ownership_sha256"],
        "branch_ref": f"refs/heads/aros/task/{task_id}",
        "base_commit": brief["base_commit"],
        "head_commit": brief["base_commit"],
        "final_state": "completed",
        "run_id": binding["run_id"],
        "run_manifest_ref": binding["run_manifest_ref"],
        "run_manifest_sha256": binding["run_manifest_sha256"],
        "run_final_ref": run_final_ref,
        "run_final_sha256": json_sha256(final),
    }
    assert (tmp_path / "tasks" / task_id / "collected.json").exists() is False
    assert paths == (None if precommitted else (run_final_ref,))
    assert message == (None if precommitted else f"Record run {binding['run_id']} final")


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        pytest.param("run_id", None, id="missing-run-id"),
        pytest.param("run_id", "RUN-other", id="mismatched-run-id"),
        pytest.param("manifest_sha256", None, id="missing-manifest-hash"),
        pytest.param(
            "manifest_sha256",
            "d" * 64,
            id="mismatched-manifest-hash",
        ),
    ],
)
def test_project_task_status_requires_exact_run_identity(
    field: str,
    replacement: object,
) -> None:
    task_id = "TASK-20260807-projection"
    brief_sha256 = "a" * 64
    ownership_sha256 = "b" * 64
    run_id = "RUN-projection"
    manifest_sha256 = "c" * 64
    brief = {"task_id": task_id, "brief_sha256": brief_sha256}
    ownership = {
        "task_id": task_id,
        "brief_sha256": brief_sha256,
        "ownership_sha256": ownership_sha256,
    }
    binding = {
        "task_id": task_id,
        "brief_sha256": brief_sha256,
        "ownership_sha256": ownership_sha256,
        "run_id": run_id,
        "run_manifest_sha256": manifest_sha256,
    }
    run_status: dict[str, object] = {
        "schema_version": 1,
        "run_id": run_id,
        "state": "launched",
        "manifest_sha256": manifest_sha256,
        "updated_at": "2026-08-07T00:00:00.000Z",
    }
    if replacement is None:
        run_status.pop(field)
    else:
        run_status[field] = replacement

    with pytest.raises(TaskRunError, match="identity"):
        project_task_status(brief, ownership, binding, run_status)


@pytest.mark.parametrize("result_error", ["no-op", "wrong-path", "wrong-commit"])
def test_task_run_binding_rejects_invalid_commit_result(
    tmp_path: Path,
    result_error: str,
) -> None:
    _service, brief, ownership = _prepared_task(tmp_path)
    task_id = str(brief["task_id"])
    real_commit = _commit(tmp_path)

    def invalid_commit(paths: tuple[str, ...], message: str) -> dict[str, object]:
        if result_error == "no-op":
            return {
                "commit": _git(tmp_path, "rev-parse", "HEAD"),
                "paths": list(paths),
                "reused": True,
                "enforcement_class": "cooperative",
            }
        result = real_commit(paths, message)
        if result_error == "wrong-path":
            result["paths"] = ["README.md"]
        else:
            result["commit"] = _git(tmp_path, "rev-parse", "HEAD^")
        return result

    with pytest.raises(TaskRunError, match="commit"):
        ensure_task_run(
            tmp_path,
            brief,
            ownership,
            actor="principal",
            commit_paths=invalid_commit,
        )

    assert not (
        tmp_path / ".aros" / "tasks" / task_id / "run.json"
    ).exists()


def test_task_run_binding_rejects_commit_with_extra_path(
    tmp_path: Path,
) -> None:
    _service, brief, ownership = _prepared_task(tmp_path)
    task_id = str(brief["task_id"])

    def commit_extra_path(
        paths: tuple[str, ...],
        _message: str,
    ) -> dict[str, object]:
        extra_ref = "unexpected-task-run-file.txt"
        (tmp_path / extra_ref).write_text("unexpected\n", encoding="utf-8")
        _git(tmp_path, "add", paths[0], extra_ref)
        _git(tmp_path, "commit", "-qm", "commit task run with extra path")
        return {
            "commit": _git(tmp_path, "rev-parse", "HEAD"),
            "paths": list(paths),
            "reused": False,
            "enforcement_class": "cooperative",
        }

    with pytest.raises(TaskRunError, match="commit"):
        ensure_task_run(
            tmp_path,
            brief,
            ownership,
            actor="principal",
            commit_paths=commit_extra_path,
        )

    assert not (
        tmp_path / ".aros" / "tasks" / task_id / "run.json"
    ).exists()


def test_task_run_binding_rejects_extra_path_commit_labeled_reused(
    tmp_path: Path,
) -> None:
    _service, brief, ownership = _prepared_task(tmp_path)
    task_id = str(brief["task_id"])

    def lying_reused_commit(
        paths: tuple[str, ...],
        _message: str,
    ) -> dict[str, object]:
        extra_ref = "unexpected-reused-task-run-file.txt"
        (tmp_path / extra_ref).write_text("unexpected\n", encoding="utf-8")
        _git(tmp_path, "add", paths[0], extra_ref)
        _git(tmp_path, "commit", "-qm", "lie about reused task run")
        return {
            "commit": _git(tmp_path, "rev-parse", "HEAD"),
            "paths": list(paths),
            "reused": True,
            "enforcement_class": "cooperative",
        }

    with pytest.raises(TaskRunError, match="commit|reused"):
        ensure_task_run(
            tmp_path,
            brief,
            ownership,
            actor="principal",
            commit_paths=lying_reused_commit,
        )

    assert not (
        tmp_path / ".aros" / "tasks" / task_id / "run.json"
    ).exists()
    manifest_path = next(tmp_path.glob("runs/RUN-*/manifest.json"))
    original_manifest = manifest_path.read_bytes()
    with manifest_path.open("ab") as handle:
        handle.write(b" \n")
    _git(tmp_path, "add", str(manifest_path.relative_to(tmp_path)))
    _git(tmp_path, "commit", "-qm", "rewrite contaminated manifest")
    manifest_path.write_bytes(original_manifest)
    _git(tmp_path, "add", str(manifest_path.relative_to(tmp_path)))
    _git(tmp_path, "commit", "-qm", "restore contaminated manifest")
    with pytest.raises(TaskRunError, match="commit|manifest|path"):
        ensure_task_run(
            tmp_path,
            brief,
            ownership,
            actor="principal",
            commit_paths=_commit(tmp_path),
        )
    assert not (
        tmp_path / ".aros" / "tasks" / task_id / "run.json"
    ).exists()


def test_task_run_binding_accepts_clean_manifest_reused_without_head_change(
    tmp_path: Path,
) -> None:
    _service, brief, ownership = _prepared_task(tmp_path)
    real_commit = _commit(tmp_path)

    class CrashAfterCommit(RuntimeError):
        pass

    def commit_then_crash(
        paths: tuple[str, ...],
        message: str,
    ) -> dict[str, object]:
        real_commit(paths, message)
        raise CrashAfterCommit("after commit")

    with pytest.raises(CrashAfterCommit):
        ensure_task_run(
            tmp_path,
            brief,
            ownership,
            actor="principal",
            commit_paths=commit_then_crash,
        )
    pre_head = _git(tmp_path, "rev-parse", "HEAD")

    binding = ensure_task_run(
        tmp_path,
        brief,
        ownership,
        actor="principal",
        commit_paths=real_commit,
    )

    assert _git(tmp_path, "rev-parse", "HEAD") == pre_head
    assert binding["run_manifest_ref"]


def test_task_run_binding_rejects_callback_manifest_mutation_before_publication(
    tmp_path: Path,
) -> None:
    _service, brief, ownership = _prepared_task(tmp_path)
    task_id = str(brief["task_id"])
    real_commit = _commit(tmp_path)

    def mutate_then_commit(
        paths: tuple[str, ...],
        message: str,
    ) -> dict[str, object]:
        manifest_path = tmp_path / paths[0]
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["resource_request"] = {"mutated-by-callback": True}
        manifest["manifest_sha256"] = manifest_sha256(manifest)
        _write_json(manifest_path, manifest)
        return real_commit(paths, message)

    with pytest.raises(TaskRunError, match="commit|manifest"):
        ensure_task_run(
            tmp_path,
            brief,
            ownership,
            actor="principal",
            commit_paths=mutate_then_commit,
        )

    assert not (
        tmp_path / ".aros" / "tasks" / task_id / "run.json"
    ).exists()


def test_task_run_binding_rejects_manifest_left_dirty_by_callback(
    tmp_path: Path,
) -> None:
    _service, brief, ownership = _prepared_task(tmp_path)
    task_id = str(brief["task_id"])
    real_commit = _commit(tmp_path)

    def commit_then_dirty(
        paths: tuple[str, ...],
        message: str,
    ) -> dict[str, object]:
        result = real_commit(paths, message)
        with (tmp_path / paths[0]).open("ab") as handle:
            handle.write(b" \n")
        return result

    with pytest.raises(TaskRunError, match="commit|manifest|snapshot"):
        ensure_task_run(
            tmp_path,
            brief,
            ownership,
            actor="principal",
            commit_paths=commit_then_dirty,
        )

    assert not (
        tmp_path / ".aros" / "tasks" / task_id / "run.json"
    ).exists()


@pytest.mark.parametrize("invalid_field", ["brief", "ownership"])
def test_ensure_task_run_rejects_non_object_authority(
    tmp_path: Path,
    invalid_field: str,
) -> None:
    _service, brief, ownership = _prepared_task(tmp_path)
    supplied_brief: object = brief
    supplied_ownership: object = ownership
    if invalid_field == "brief":
        supplied_brief = []
    else:
        supplied_ownership = []

    with pytest.raises(TaskRunError, match="brief|ownership|object"):
        ensure_task_run(
            tmp_path,
            supplied_brief,  # type: ignore[arg-type]
            supplied_ownership,  # type: ignore[arg-type]
            actor="principal",
            commit_paths=_commit(tmp_path),
        )


def test_task_run_binding_preserves_unexpected_callback_exception(
    tmp_path: Path,
) -> None:
    _service, brief, ownership = _prepared_task(tmp_path)
    task_id = str(brief["task_id"])

    class CallbackFailure(RuntimeError):
        pass

    def broken_commit(_paths: tuple[str, ...], _message: str) -> dict[str, object]:
        raise CallbackFailure("injected callback failure")

    with pytest.raises(CallbackFailure, match="injected callback failure"):
        ensure_task_run(
            tmp_path,
            brief,
            ownership,
            actor="principal",
            commit_paths=broken_commit,
        )

    assert not (
        tmp_path / ".aros" / "tasks" / task_id / "run.json"
    ).exists()


def test_task_run_binding_preserves_unexpected_value_error_from_callback(
    tmp_path: Path,
) -> None:
    _service, brief, ownership = _prepared_task(tmp_path)

    class CallbackValueError(ValueError):
        pass

    def broken_commit(_paths: tuple[str, ...], _message: str) -> dict[str, object]:
        raise CallbackValueError("injected callback value failure")

    with pytest.raises(CallbackValueError, match="injected callback value failure"):
        ensure_task_run(
            tmp_path,
            brief,
            ownership,
            actor="principal",
            commit_paths=broken_commit,
        )


def test_task_run_lock_context_preserves_body_task_error(
    tmp_path: Path,
) -> None:
    _service, brief, ownership = _prepared_task(tmp_path)

    class SentinelTaskError(TaskError):
        pass

    def broken_commit(_paths: tuple[str, ...], _message: str) -> dict[str, object]:
        raise SentinelTaskError("sentinel callback task error")

    with pytest.raises(SentinelTaskError, match="sentinel callback task error"):
        ensure_task_run(
            tmp_path,
            brief,
            ownership,
            actor="principal",
            commit_paths=broken_commit,
        )


def test_load_task_run_requires_manifest_at_current_head(tmp_path: Path) -> None:
    _service, brief, ownership = _prepared_task(tmp_path)
    binding = ensure_task_run(
        tmp_path,
        brief,
        ownership,
        actor="principal",
        commit_paths=_commit(tmp_path),
    )
    manifest_ref = str(binding["run_manifest_ref"])
    manifest_path = tmp_path / manifest_ref
    manifest_bytes = manifest_path.read_bytes()
    _git(tmp_path, "rm", "-q", manifest_ref)
    _git(tmp_path, "commit", "-qm", "remove committed task run")
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_bytes(manifest_bytes)

    with pytest.raises(TaskRunError, match="HEAD|committed"):
        load_task_run(tmp_path, brief, ownership)


def test_load_task_run_rejects_rehashed_working_manifest(
    tmp_path: Path,
) -> None:
    _service, brief, ownership = _prepared_task(tmp_path)
    task_id = str(brief["task_id"])
    binding = ensure_task_run(
        tmp_path,
        brief,
        ownership,
        actor="principal",
        commit_paths=_commit(tmp_path),
    )
    manifest_path = tmp_path / str(binding["run_manifest_ref"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["resource_request"] = {"tampered": True}
    manifest["manifest_sha256"] = manifest_sha256(manifest)
    _write_json(manifest_path, manifest)
    binding["run_manifest_sha256"] = manifest["manifest_sha256"]
    binding["binding_sha256"] = json_sha256(
        {key: value for key, value in binding.items() if key != "binding_sha256"}
    )
    _write_json(
        tmp_path / ".aros" / "tasks" / task_id / "run.json",
        binding,
    )

    with pytest.raises(TaskRunError, match="HEAD|committed"):
        load_task_run(tmp_path, brief, ownership)


def test_task_run_load_rejects_restored_multi_touch_manifest_history(
    tmp_path: Path,
) -> None:
    service, brief, ownership = _prepared_task(tmp_path)
    binding = ensure_task_run(
        tmp_path,
        brief,
        ownership,
        actor="principal",
        commit_paths=_commit(tmp_path),
    )
    manifest_path = tmp_path / str(binding["run_manifest_ref"])
    original = manifest_path.read_bytes()
    manifest = json.loads(original)
    manifest["resource_request"] = {"post-binding-rewrite": True}
    manifest["manifest_sha256"] = manifest_sha256(manifest)
    _write_json(manifest_path, manifest)
    _git(tmp_path, "add", str(manifest_path.relative_to(tmp_path)))
    _git(tmp_path, "commit", "-qm", "rewrite bound task run manifest")
    manifest_path.write_bytes(original)
    _git(tmp_path, "add", str(manifest_path.relative_to(tmp_path)))
    _git(tmp_path, "commit", "-qm", "restore bound task run manifest")

    with pytest.raises(TaskRunError, match="history|commit|manifest"):
        load_task_run(tmp_path, brief, ownership)
    with pytest.raises(TaskError):
        service.adapter_context(str(brief["task_id"]))


def test_concurrent_task_run_binding_uses_one_publication(
    tmp_path: Path,
) -> None:
    _service, brief, ownership = _prepared_task(tmp_path)
    barrier = threading.Barrier(8)
    callback_lock = threading.Lock()
    callback_calls = 0
    real_commit = _commit(tmp_path)

    def serialized_commit(
        paths: tuple[str, ...],
        message: str,
    ) -> dict[str, object]:
        nonlocal callback_calls
        with callback_lock:
            callback_calls += 1
            return real_commit(paths, message)

    def bind() -> dict[str, object]:
        barrier.wait()
        return ensure_task_run(
            tmp_path,
            brief,
            ownership,
            actor="principal",
            commit_paths=serialized_commit,
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        bindings = list(executor.map(lambda _index: bind(), range(8)))

    assert all(binding == bindings[0] for binding in bindings)
    assert callback_calls == 1
    assert len(list(tmp_path.glob("runs/RUN-*/manifest.json"))) == 1


def test_task_run_binding_serializes_validation_and_runtime_preparation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import arbor.aros.task_run as task_run

    _service, brief, ownership = _prepared_task(tmp_path)
    state_lock = threading.Lock()
    second_entered = threading.Event()
    active = 0
    overlapped = False
    real_validate = task_run._validate_inputs

    def observed_validate(*args: object) -> Path:
        nonlocal active, overlapped
        with state_lock:
            active += 1
            if active > 1:
                overlapped = True
                second_entered.set()
        second_entered.wait(timeout=0.2)
        try:
            return real_validate(*args)  # type: ignore[arg-type]
        finally:
            with state_lock:
                active -= 1

    monkeypatch.setattr(task_run, "_validate_inputs", observed_validate)

    def bind() -> dict[str, object]:
        return ensure_task_run(
            tmp_path,
            brief,
            ownership,
            actor="principal",
            commit_paths=_commit(tmp_path),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        bindings = list(executor.map(lambda _index: bind(), range(2)))

    assert bindings[0] == bindings[1]
    assert not overlapped


def test_task_run_binding_recovers_committed_manifest_after_environment_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _service, brief, ownership = _prepared_task(tmp_path)
    task_id = str(brief["task_id"])
    real_commit = _commit(tmp_path)

    class CrashAfterCommit(RuntimeError):
        pass

    def commit_then_crash(
        paths: tuple[str, ...],
        message: str,
    ) -> dict[str, object]:
        real_commit(paths, message)
        raise CrashAfterCommit("crash after manifest commit")

    with pytest.raises(CrashAfterCommit, match="crash after manifest commit"):
        ensure_task_run(
            tmp_path,
            brief,
            ownership,
            actor="principal",
            commit_paths=commit_then_crash,
        )
    assert not (
        tmp_path / ".aros" / "tasks" / task_id / "run.json"
    ).exists()
    manifests = list(tmp_path.glob("runs/RUN-*/manifest.json"))
    assert len(manifests) == 1
    manifest_bytes = manifests[0].read_bytes()
    monkeypatch.setenv("LANG", "task-run-recovery-drift")

    binding = ensure_task_run(
        tmp_path,
        brief,
        ownership,
        actor="principal",
        commit_paths=real_commit,
    )

    assert binding["run_manifest_ref"] == manifests[0].relative_to(tmp_path).as_posix()
    assert manifests[0].read_bytes() == manifest_bytes
    assert len(list(tmp_path.glob("runs/RUN-*/manifest.json"))) == 1


@pytest.mark.parametrize(
    ("runtime_name", "mutation"),
    [
        pytest.param("runtime", "wrong-mode", id="runtime-wrong-mode"),
        pytest.param("home", "missing", id="home-missing"),
        pytest.param("tmp", "wrong-mode", id="tmp-wrong-mode"),
    ],
)
def test_load_task_run_does_not_repair_runtime(
    tmp_path: Path,
    runtime_name: str,
    mutation: str,
) -> None:
    _service, brief, ownership = _prepared_task(tmp_path)
    binding = ensure_task_run(
        tmp_path,
        brief,
        ownership,
        actor="principal",
        commit_paths=_commit(tmp_path),
    )
    runtime = tmp_path / ".aros" / "tasks" / str(brief["task_id"])
    target = runtime if runtime_name == "runtime" else runtime / runtime_name
    if mutation == "missing":
        target.rmdir()
    else:
        target.chmod(0o755)
    main_dirt = tmp_path / "unrelated-main-dirt.txt"
    child_dirt = Path(str(ownership["worktree_path"])) / "unrelated-child-dirt.txt"
    main_dirt.write_bytes(b"main dirt\x00unchanged")
    child_dirt.write_bytes(b"child dirt\x00unchanged")
    dirt = {main_dirt: main_dirt.read_bytes(), child_dirt: child_dirt.read_bytes()}

    with pytest.raises(TaskRunError):
        load_task_run(tmp_path, brief, ownership)

    if mutation == "missing":
        assert not target.exists()
    else:
        assert stat.S_IMODE(target.stat().st_mode) == 0o755
    assert {path: path.read_bytes() for path in dirt} == dirt
    assert binding["task_id"] == brief["task_id"]


def test_load_task_run_does_not_probe_filesystem_permissions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import arbor.aros.tasks as tasks_module

    _service, brief, ownership = _prepared_task(tmp_path)
    binding = ensure_task_run(
        tmp_path,
        brief,
        ownership,
        actor="principal",
        commit_paths=_commit(tmp_path),
    )

    def forbidden_probe(_runtime: Path) -> dict[str, object]:
        raise AssertionError("load_task_run must not probe filesystem permissions")

    monkeypatch.setattr(tasks_module, "_probe_filesystem_permissions", forbidden_probe)

    assert load_task_run(tmp_path, brief, ownership) == binding


@pytest.mark.parametrize("authority_kind", ["hardlink", "symlink"])
def test_load_task_run_rejects_linked_binding_authority(
    tmp_path: Path,
    authority_kind: str,
) -> None:
    _service, brief, ownership = _prepared_task(tmp_path)
    ensure_task_run(
        tmp_path,
        brief,
        ownership,
        actor="principal",
        commit_paths=_commit(tmp_path),
    )
    binding_path = (
        tmp_path / ".aros" / "tasks" / str(brief["task_id"]) / "run.json"
    )
    alias = tmp_path / f"binding-{authority_kind}.json"
    if authority_kind == "hardlink":
        os.link(binding_path, alias, follow_symlinks=False)
    else:
        binding_path.rename(alias)
        binding_path.symlink_to(alias)

    with pytest.raises(TaskRunError, match="binding"):
        load_task_run(tmp_path, brief, ownership)


def test_task_run_binding_revalidates_parent_identity_around_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import arbor.aros.task_run as task_run

    _service, brief, ownership = _prepared_task(tmp_path)
    task_id = str(brief["task_id"])
    observations = iter([(1, 1), (1, 2)])
    monkeypatch.setattr(
        task_run,
        "_binding_parent_identity",
        lambda _path: next(observations),
        raising=False,
    )

    with pytest.raises(TaskRunError, match="parent|identity"):
        ensure_task_run(
            tmp_path,
            brief,
            ownership,
            actor="principal",
            commit_paths=_commit(tmp_path),
        )

    assert not (
        tmp_path / ".aros" / "tasks" / task_id / "run.json"
    ).exists()


def test_failed_task_run_binding_preserves_parent_and_child_dirt(
    tmp_path: Path,
) -> None:
    _service, brief, ownership = _prepared_task(tmp_path)
    task_id = str(brief["task_id"])
    main_dirt = tmp_path / "unrelated-main-dirt.bin"
    child_dirt = Path(str(ownership["worktree_path"])) / "unrelated-child-dirt.bin"
    main_dirt.write_bytes(b"main dirt\x00unchanged")
    child_dirt.write_bytes(b"child dirt\x00unchanged")
    dirt = {main_dirt: main_dirt.read_bytes(), child_dirt: child_dirt.read_bytes()}

    with pytest.raises(TaskRunError):
        ensure_task_run(
            tmp_path,
            brief,
            ownership,
            actor="principal",
            commit_paths=_commit(tmp_path),
        )

    assert {path: path.read_bytes() for path in dirt} == dirt
    assert not (
        tmp_path / ".aros" / "tasks" / task_id / "run.json"
    ).exists()


def test_task_run_binding_normalizes_actor_like_run_service(
    tmp_path: Path,
) -> None:
    _service, brief, ownership = _prepared_task(tmp_path)

    binding = ensure_task_run(
        tmp_path,
        brief,
        ownership,
        actor="  principal  ",
        commit_paths=_commit(tmp_path),
    )

    manifest = json.loads(
        (tmp_path / str(binding["run_manifest_ref"])).read_text(encoding="utf-8")
    )
    assert manifest["actor"] == "principal"


def test_task_run_binding_is_create_once_idempotent_and_committed(
    tmp_path: Path,
) -> None:
    _workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _brief(service)
    task_id = str(brief["task_id"])
    _git(tmp_path, "add", f"tasks/{task_id}/brief.json")
    _git(tmp_path, "commit", "-qm", "record task brief")
    service._ensure_worktree(task_id, actor="principal")
    ownership = service._load_ownership(brief)

    first = ensure_task_run(
        tmp_path,
        brief,
        ownership,
        actor="principal",
        commit_paths=_commit(tmp_path),
    )
    second = ensure_task_run(
        tmp_path,
        brief,
        ownership,
        actor="principal",
        commit_paths=_commit(tmp_path),
    )

    assert first == second == load_task_run(tmp_path, brief, ownership)
    run_id = str(first["run_id"])
    manifest_ref = f"runs/{run_id}/manifest.json"
    assert first["run_manifest_ref"] == manifest_ref
    manifests = sorted(tmp_path.glob("runs/RUN-*/manifest.json"))
    assert manifests == [tmp_path / manifest_ref]
    assert _git_bytes(tmp_path, "show", f"HEAD:{manifest_ref}") == manifests[0].read_bytes()


def test_task_run_binding_tampering_fails_closed_without_another_run(
    tmp_path: Path,
) -> None:
    _workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _brief(service)
    task_id = str(brief["task_id"])
    _git(tmp_path, "add", f"tasks/{task_id}/brief.json")
    _git(tmp_path, "commit", "-qm", "record task brief")
    service._ensure_worktree(task_id, actor="principal")
    ownership = service._load_ownership(brief)
    binding = ensure_task_run(
        tmp_path,
        brief,
        ownership,
        actor="principal",
        commit_paths=_commit(tmp_path),
    )
    manifests = sorted(tmp_path.glob("runs/RUN-*/manifest.json"))
    binding_path = tmp_path / ".aros" / "tasks" / task_id / "run.json"
    tampered = dict(binding)
    tampered["run_id"] = "RUN-tampered"
    binding_path.write_text(json.dumps(tampered), encoding="utf-8")

    with pytest.raises(TaskRunError, match="binding"):
        load_task_run(tmp_path, brief, ownership)
    with pytest.raises(TaskRunError, match="binding"):
        ensure_task_run(
            tmp_path,
            brief,
            ownership,
            actor="principal",
            commit_paths=_commit(tmp_path),
        )

    assert sorted(tmp_path.glob("runs/RUN-*/manifest.json")) == manifests


def test_adapter_context_is_bound_to_task_run_and_owned_worktree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _brief(service)
    task_id = str(brief["task_id"])
    _git(tmp_path, "add", f"tasks/{task_id}/brief.json")
    _git(tmp_path, "commit", "-qm", "record task brief")
    service._ensure_worktree(task_id, actor="principal")
    ownership = service._load_ownership(brief)
    ensure_task_run(
        tmp_path,
        brief,
        ownership,
        actor="principal",
        commit_paths=_commit(tmp_path),
    )
    monkeypatch.setenv("AMBIENT_TASK_SECRET", "must-not-pass")

    context = service.adapter_context(task_id)

    assert context["argv"] == brief["adapter_argv"]
    assert context["worktree"] == ownership["worktree_path"]
    environment = context["environment"]
    assert isinstance(environment, dict)
    runtime = tmp_path / ".aros" / "tasks" / task_id
    assert environment["AROS_TASK_ID"] == task_id
    assert environment["AROS_TASK_BRIEF"] == str(
        tmp_path / "tasks" / task_id / "brief.json"
    )
    assert environment["AROS_TASK_WORKTREE"] == ownership["worktree_path"]
    assert environment["AROS_TASK_BASE_COMMIT"] == brief["base_commit"]
    assert environment["AROS_TASK_BRIEF_SHA256"] == brief["brief_sha256"]
    assert environment["HOME"] == str(runtime / "home")
    assert environment["TMPDIR"] == str(runtime / "tmp")
    assert "AMBIENT_TASK_SECRET" not in environment
    for name in ("home", "tmp"):
        path = runtime / name
        assert path.is_dir()
        assert not path.is_symlink()
        assert stat.S_IMODE(path.stat().st_mode) == 0o700


@pytest.mark.parametrize("runtime_name", ["home", "tmp"])
def test_task_run_binding_rejects_symlinked_runtime_before_publication(
    tmp_path: Path,
    runtime_name: str,
) -> None:
    _workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _brief(service)
    task_id = str(brief["task_id"])
    _git(tmp_path, "add", f"tasks/{task_id}/brief.json")
    _git(tmp_path, "commit", "-qm", "record task brief")
    service._ensure_worktree(task_id, actor="principal")
    ownership = service._load_ownership(brief)
    outside = tmp_path / f"outside-{runtime_name}"
    outside.mkdir()
    runtime = tmp_path / ".aros" / "tasks" / task_id
    (runtime / runtime_name).symlink_to(outside, target_is_directory=True)

    with pytest.raises(TaskRunError):
        ensure_task_run(
            tmp_path,
            brief,
            ownership,
            actor="principal",
            commit_paths=_commit(tmp_path),
        )

    assert not (runtime / "run.json").exists()
    assert list(tmp_path.glob("runs/RUN-*/manifest.json")) == []


@pytest.mark.parametrize(
    ("authority", "field", "value"),
    [
        pytest.param(
            "brief",
            "created_at",
            "not-a-timestamp",
            id="brief-created-at",
        ),
        pytest.param(
            "ownership",
            "branch",
            "aros/task/TASK-20260807-wrong",
            id="ownership-branch",
        ),
    ],
)
def test_task_run_binding_rejects_rehashed_malformed_task_authority(
    tmp_path: Path,
    authority: str,
    field: str,
    value: str,
) -> None:
    _workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _brief(service)
    task_id = str(brief["task_id"])
    _git(tmp_path, "add", f"tasks/{task_id}/brief.json")
    _git(tmp_path, "commit", "-qm", "record task brief")
    service._ensure_worktree(task_id, actor="principal")
    ownership = service._load_ownership(brief)
    authority_path = (
        tmp_path / "tasks" / task_id / "brief.json"
        if authority == "brief"
        else tmp_path / ".aros" / "tasks" / task_id / "ownership.json"
    )
    target = dict(brief) if authority == "brief" else dict(ownership)
    hash_field = "brief_sha256" if authority == "brief" else "ownership_sha256"
    target[field] = value
    target[hash_field] = json_sha256(
        {key: item for key, item in target.items() if key != hash_field}
    )
    _write_json(authority_path, target)
    authority_bytes = authority_path.read_bytes()

    with pytest.raises(TaskRunError):
        ensure_task_run(
            tmp_path,
            target if authority == "brief" else brief,
            target if authority == "ownership" else ownership,
            actor="principal",
            commit_paths=_commit(tmp_path),
        )

    assert authority_path.read_bytes() == authority_bytes
    assert not (
        tmp_path / ".aros" / "tasks" / task_id / "run.json"
    ).exists()
    assert list(tmp_path.glob("runs/RUN-*/manifest.json")) == []


@pytest.mark.parametrize("substitution", ["runtime", "home"])
def test_adapter_context_rejects_runtime_substitution_after_binding(
    tmp_path: Path,
    substitution: str,
) -> None:
    _workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _brief(service)
    task_id = str(brief["task_id"])
    _git(tmp_path, "add", f"tasks/{task_id}/brief.json")
    _git(tmp_path, "commit", "-qm", "record task brief")
    service._ensure_worktree(task_id, actor="principal")
    ownership = service._load_ownership(brief)
    ensure_task_run(
        tmp_path,
        brief,
        ownership,
        actor="principal",
        commit_paths=_commit(tmp_path),
    )
    runtime = tmp_path / ".aros" / "tasks" / task_id
    if substitution == "runtime":
        moved = tmp_path / "moved-runtime"
        runtime.rename(moved)
        runtime.symlink_to(moved, target_is_directory=True)
    else:
        home = runtime / "home"
        home.rmdir()
        outside = tmp_path / "outside-home-after-binding"
        outside.mkdir()
        home.symlink_to(outside, target_is_directory=True)

    with pytest.raises(TaskError):
        service.adapter_context(task_id)


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


def test_start_reuses_committed_run_after_crash_before_launch_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _brief(service)
    task_id = str(brief["task_id"])
    _git(tmp_path, "add", f"tasks/{task_id}/brief.json")
    _git(tmp_path, "commit", "-qm", "record task brief")
    real_commit = _commit(tmp_path)
    committed_paths: list[tuple[str, ...]] = []
    start_calls: list[str] = []

    def observed_commit(
        paths: tuple[str, ...],
        message: str,
    ) -> dict[str, object]:
        committed_paths.append(paths)
        return real_commit(paths, message)

    def crash_once(
        _service: RunService,
        run_id: str,
        *,
        actor: str | None = None,
    ) -> dict[str, object]:
        start_calls.append(run_id)
        binding_path = tmp_path / ".aros" / "tasks" / task_id / "run.json"
        binding = json.loads(binding_path.read_text(encoding="utf-8"))
        manifest_ref = f"runs/{run_id}/manifest.json"
        assert binding["run_id"] == run_id
        assert binding["run_manifest_ref"] == manifest_ref
        assert _git_bytes(tmp_path, "show", f"HEAD:{manifest_ref}") == (
            tmp_path / manifest_ref
        ).read_bytes()
        if len(start_calls) == 1:
            raise RuntimeError("crash after durable Task Run publication")
        return _launched_run_status(tmp_path, run_id)

    monkeypatch.setattr(RunService, "start", crash_once)

    with pytest.raises(TaskError, match=task_id):
        service.start(
            task_id,
            actor="principal",
            commit_paths=observed_commit,
        )

    binding = json.loads(
        (tmp_path / ".aros" / "tasks" / task_id / "run.json").read_text(
            encoding="utf-8"
        )
    )
    manifests = sorted(tmp_path.glob("runs/RUN-*/manifest.json"))
    assert manifests == [tmp_path / str(binding["run_manifest_ref"])]

    status = service.start(
        task_id,
        actor="principal",
        commit_paths=observed_commit,
    )

    assert status["state"] == "launched"
    assert status["run_id"] == binding["run_id"]
    assert start_calls == [binding["run_id"], binding["run_id"]]
    assert committed_paths == [(str(binding["run_manifest_ref"]),)]
    assert sorted(tmp_path.glob("runs/RUN-*/manifest.json")) == manifests


def test_start_requires_commit_callback_before_preparing_task_run(
    tmp_path: Path,
) -> None:
    _workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _brief(service)
    task_id = str(brief["task_id"])
    _git(tmp_path, "add", f"tasks/{task_id}/brief.json")
    _git(tmp_path, "commit", "-qm", "record task brief")

    with pytest.raises(TaskError, match="commit_paths|commit"):
        service.start(task_id, actor="principal")

    assert not (tmp_path / ".worktree" / "tasks" / task_id).exists()
    assert not (tmp_path / ".aros" / "tasks" / task_id / "ownership.json").exists()
    assert list(tmp_path.glob("runs/RUN-*/manifest.json")) == []


def test_start_commits_fast_terminal_run_before_returning_final_ref(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _brief(service)
    task_id = str(brief["task_id"])
    _git(tmp_path, "add", f"tasks/{task_id}/brief.json")
    _git(tmp_path, "commit", "-qm", "record task brief")
    terminal_statuses: dict[str, dict[str, object]] = {}
    terminal_finals: dict[str, dict[str, object]] = {}

    def fake_start(
        _service: RunService,
        run_id: str,
        *,
        actor: str | None = None,
    ) -> dict[str, object]:
        manifest = json.loads(
            (tmp_path / "runs" / run_id / "manifest.json").read_text(
                encoding="utf-8"
            )
        )
        finished_at = "2026-08-07T00:00:02.000Z"
        final = {
            "schema_version": 1,
            "run_id": run_id,
            "state": "completed",
            "manifest_sha256": manifest["manifest_sha256"],
        }
        final_ref = f"runs/{run_id}/final.json"
        _write_json(tmp_path / final_ref, final)
        status = {
            "schema_version": 1,
            "run_id": run_id,
            "state": "completed",
            "manifest_sha256": manifest["manifest_sha256"],
            "updated_at": finished_at,
            "finished_at": finished_at,
            "exit_code": 0,
            "final_ref": final_ref,
        }
        terminal_finals[run_id] = final
        terminal_statuses[run_id] = status
        return dict(status)

    def fake_terminal(
        _service: RunService,
        run_id: str,
    ) -> tuple[dict[str, object], tuple[str, ...], str]:
        return (
            dict(terminal_finals[run_id]),
            (f"runs/{run_id}/final.json",),
            f"Record run {run_id} final",
        )

    def fake_status(
        _service: RunService,
        run_id: str,
        *,
        reconcile: bool = True,
        reader: object | None = None,
    ) -> dict[str, object]:
        return dict(terminal_statuses[run_id])

    monkeypatch.setattr(RunService, "start", fake_start)
    monkeypatch.setattr(RunService, "terminal_with_commit", fake_terminal)
    monkeypatch.setattr(RunService, "status", fake_status)

    status = service.start(
        task_id,
        actor="principal",
        commit_paths=_commit(tmp_path),
    )

    final_ref = str(status["final_ref"])
    assert final_ref == f"runs/{status['run_id']}/final.json"
    assert _git_bytes(tmp_path, "show", f"HEAD:{final_ref}") == (
        tmp_path / final_ref
    ).read_bytes()
    assert (
        _git(
            tmp_path,
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "-r",
            "HEAD",
        )
        == final_ref
    )


@pytest.mark.parametrize("_attempt", range(20))
def test_start_and_status_serialize_fast_terminal_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _attempt: int,
) -> None:
    _workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _brief(service)
    task_id = str(brief["task_id"])
    _git(tmp_path, "add", f"tasks/{task_id}/brief.json")
    _git(tmp_path, "commit", "-qm", "record task brief")
    terminal_ready = threading.Event()
    terminal_seam = threading.Barrier(2)
    commit_seam = threading.Barrier(2)
    first_status_read = threading.Event()
    terminal_statuses: dict[str, dict[str, object]] = {}
    terminal_finals: dict[str, dict[str, object]] = {}

    def fake_start(
        _service: RunService,
        run_id: str,
        *,
        actor: str | None = None,
    ) -> dict[str, object]:
        manifest = json.loads(
            (tmp_path / "runs" / run_id / "manifest.json").read_text(
                encoding="utf-8"
            )
        )
        finished_at = "2026-08-07T00:00:02.000Z"
        final = {
            "schema_version": 1,
            "run_id": run_id,
            "state": "completed",
            "manifest_sha256": manifest["manifest_sha256"],
        }
        final_ref = f"runs/{run_id}/final.json"
        _write_json(tmp_path / final_ref, final)
        status = {
            "schema_version": 1,
            "run_id": run_id,
            "state": "completed",
            "manifest_sha256": manifest["manifest_sha256"],
            "updated_at": finished_at,
            "finished_at": finished_at,
            "exit_code": 0,
            "final_ref": final_ref,
        }
        terminal_finals[run_id] = final
        terminal_statuses[run_id] = status
        terminal_ready.set()
        terminal_seam.wait(timeout=5)
        return dict(status)

    def fake_terminal(
        _service: RunService,
        run_id: str,
    ) -> tuple[dict[str, object], tuple[str, ...], str]:
        return (
            dict(terminal_finals[run_id]),
            (f"runs/{run_id}/final.json",),
            f"Record run {run_id} final",
        )

    def fake_status(
        _service: RunService,
        run_id: str,
        *,
        reconcile: bool = True,
        reader: object | None = None,
    ) -> dict[str, object]:
        if not first_status_read.is_set():
            first_status_read.set()
            terminal_seam.wait(timeout=5)
        return dict(terminal_statuses[run_id])

    real_commit = _commit(tmp_path)

    def synchronized_commit(
        paths: tuple[str, ...],
        message: str,
    ) -> dict[str, object]:
        try:
            commit_seam.wait(timeout=0.25)
        except threading.BrokenBarrierError:
            pass
        return real_commit(paths, message)

    monkeypatch.setattr(RunService, "start", fake_start)
    monkeypatch.setattr(RunService, "terminal_with_commit", fake_terminal)
    monkeypatch.setattr(RunService, "status", fake_status)

    with ThreadPoolExecutor(max_workers=2) as executor:
        start_future = executor.submit(
            service.start,
            task_id,
            actor="principal",
            commit_paths=synchronized_commit,
        )
        assert terminal_ready.wait(timeout=5)
        status_future = executor.submit(
            service.status,
            task_id,
            commit_paths=synchronized_commit,
        )
        started = start_future.result(timeout=10)
        status = status_future.result(timeout=10)

    final_ref = str(started["final_ref"])
    assert status == started
    assert final_ref == f"runs/{started['run_id']}/final.json"
    assert _git(tmp_path, "log", "--format=%H", "--", final_ref).splitlines() == [
        _git(tmp_path, "rev-parse", "HEAD")
    ]
    assert _git(tmp_path, "status", "--short") == ""


def test_start_rejects_terminal_status_without_run_final(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _brief(service)
    task_id = str(brief["task_id"])
    _git(tmp_path, "add", f"tasks/{task_id}/brief.json")
    _git(tmp_path, "commit", "-qm", "record task brief")
    terminal_statuses: dict[str, dict[str, object]] = {}

    def terminal_without_final(
        _service: RunService,
        run_id: str,
        *,
        actor: str | None = None,
    ) -> dict[str, object]:
        manifest = json.loads(
            (tmp_path / "runs" / run_id / "manifest.json").read_text(
                encoding="utf-8"
            )
        )
        finished_at = "2026-08-07T00:00:02.000Z"
        status = {
            "schema_version": 1,
            "run_id": run_id,
            "state": "completed",
            "manifest_sha256": manifest["manifest_sha256"],
            "updated_at": finished_at,
            "finished_at": finished_at,
            "exit_code": 0,
            "final_ref": f"runs/{run_id}/final.json",
        }
        terminal_statuses[run_id] = status
        return dict(status)

    def terminal_status(
        _service: RunService,
        run_id: str,
        *,
        reconcile: bool = True,
        reader: object | None = None,
    ) -> dict[str, object]:
        return dict(terminal_statuses[run_id])

    monkeypatch.setattr(RunService, "start", terminal_without_final)
    monkeypatch.setattr(RunService, "status", terminal_status)

    with pytest.raises(TaskError, match=task_id) as raised:
        service.start(
            task_id,
            actor="principal",
            commit_paths=_commit(tmp_path),
        )

    assert isinstance(raised.value.__cause__, TaskRunError)
    assert list(tmp_path.glob("runs/RUN-*/final.json")) == []


@pytest.mark.parametrize("stage", ["ensure", "run", "terminal"])
@pytest.mark.parametrize("error_type", [RuntimeError, ValueError])
def test_start_translates_expected_operational_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    error_type: type[Exception],
) -> None:
    import arbor.aros.task_run as task_run_module

    _workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _brief(service)
    task_id = str(brief["task_id"])
    _git(tmp_path, "add", f"tasks/{task_id}/brief.json")
    _git(tmp_path, "commit", "-qm", "record task brief")
    injected = error_type(f"injected {stage} failure")
    real_commit = _commit(tmp_path)

    def commit_or_fail(
        paths: tuple[str, ...],
        message: str,
    ) -> dict[str, object]:
        if stage == "ensure":
            raise injected
        return real_commit(paths, message)

    if stage == "run":

        def fail_run(
            _service: RunService,
            run_id: str,
            *,
            actor: str | None = None,
        ) -> dict[str, object]:
            raise injected

        monkeypatch.setattr(RunService, "start", fail_run)
    elif stage == "terminal":

        def launched(
            _service: RunService,
            run_id: str,
            *,
            actor: str | None = None,
        ) -> dict[str, object]:
            return _launched_run_status(tmp_path, run_id)

        def fail_terminal(*_args: object, **_kwargs: object) -> dict[str, object]:
            raise injected

        monkeypatch.setattr(RunService, "start", launched)
        monkeypatch.setattr(
            task_run_module,
            "commit_terminal_run_if_present",
            fail_terminal,
        )

    with pytest.raises(TaskError, match=task_id) as raised:
        service.start(
            task_id,
            actor="principal",
            commit_paths=commit_or_fail,
        )

    assert raised.value.__cause__ is injected


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


@pytest.mark.parametrize(
    ("run_state", "task_state"),
    [
        pytest.param("prepared", "launched", id="prepared-projects-launched"),
        pytest.param("launched", "launched", id="launched"),
        pytest.param("running", "running", id="running"),
        pytest.param("completed", "completed", id="completed"),
        pytest.param("failed_process", "failed_process", id="failed-process"),
        pytest.param("timed_out", "timed_out", id="timed-out"),
        pytest.param("cancelled", "cancelled", id="cancelled"),
        pytest.param("lost", "lost", id="lost"),
    ],
)
def test_status_projects_every_run_state_exactly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_state: str,
    task_state: str,
) -> None:
    import arbor.aros.task_run as task_run_module

    _workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _brief(service)
    task_id = str(brief["task_id"])
    _git(tmp_path, "add", f"tasks/{task_id}/brief.json")
    _git(tmp_path, "commit", "-qm", "record task brief")
    monkeypatch.setattr(
        RunService,
        "start",
        lambda _service, run_id, *, actor=None: _launched_run_status(
            tmp_path,
            run_id,
        ),
    )
    service.start(task_id, actor="principal", commit_paths=_commit(tmp_path))
    binding = json.loads(
        (tmp_path / ".aros" / "tasks" / task_id / "run.json").read_text(
            encoding="utf-8"
        )
    )
    run_id = str(binding["run_id"])
    final_ref = (
        f"runs/{run_id}/final.json"
        if run_state in {"completed", "failed_process", "timed_out", "cancelled"}
        else None
    )
    run_status = {
        "schema_version": 1,
        "run_id": run_id,
        "state": run_state,
        "manifest_sha256": binding["run_manifest_sha256"],
        "updated_at": "2026-08-07T00:00:02.000Z",
        "final_ref": final_ref,
        "reason": None,
        "carrier": "must-not-project",
        "process_pid": 123,
    }
    monkeypatch.setattr(
        RunService,
        "status",
        lambda _service, requested_run_id, **_kwargs: {
            **run_status,
            "run_id": requested_run_id,
        },
    )
    monkeypatch.setattr(
        task_run_module,
        "commit_terminal_run_if_present",
        lambda _root, _binding, status, _callback: status,
    )

    projected = service.status(
        task_id,
        commit_paths=lambda _paths, _message: {},
    )

    assert projected == {
        "schema_version": 1,
        "task_id": task_id,
        "state": task_state,
        "brief_sha256": brief["brief_sha256"],
        "ownership_sha256": binding["ownership_sha256"],
        "run_id": run_id,
        "run_manifest_sha256": binding["run_manifest_sha256"],
        "updated_at": "2026-08-07T00:00:02.000Z",
        "final_ref": final_ref,
        "reason": None,
    }


def test_terminal_status_without_callback_hides_uncommitted_final_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _service, brief, ownership = _prepared_task(tmp_path)
    task_id = str(brief["task_id"])
    binding = ensure_task_run(
        tmp_path,
        brief,
        ownership,
        actor="principal",
        commit_paths=_commit(tmp_path),
    )
    run_id = str(binding["run_id"])
    final_ref = f"runs/{run_id}/final.json"
    final = {
        "schema_version": 1,
        "run_id": run_id,
        "state": "completed",
        "manifest_sha256": binding["run_manifest_sha256"],
    }
    _write_json(tmp_path / final_ref, final)
    terminal_status = {
        "schema_version": 1,
        "run_id": run_id,
        "state": "completed",
        "manifest_sha256": binding["run_manifest_sha256"],
        "updated_at": "2026-08-07T00:00:02.000Z",
        "final_ref": final_ref,
    }
    monkeypatch.setattr(
        RunService,
        "status",
        lambda _service, _run_id, **_kwargs: dict(terminal_status),
    )
    monkeypatch.setattr(
        RunService,
        "terminal_with_commit",
        lambda _service, _run_id: (
            dict(final),
            (final_ref,),
            f"Record run {run_id} final",
        ),
    )
    before = (_git(tmp_path, "rev-parse", "HEAD"), _git(tmp_path, "status", "--short"))

    status = TaskService(tmp_path).status(task_id)

    assert status["state"] == "completed"
    assert status["final_ref"] is None
    assert (_git(tmp_path, "rev-parse", "HEAD"), _git(tmp_path, "status", "--short")) == before


def test_terminal_status_with_callback_commits_and_exposes_final(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _service, brief, ownership = _prepared_task(tmp_path)
    task_id = str(brief["task_id"])
    binding = ensure_task_run(
        tmp_path,
        brief,
        ownership,
        actor="principal",
        commit_paths=_commit(tmp_path),
    )
    run_id = str(binding["run_id"])
    final_ref = f"runs/{run_id}/final.json"
    final = {
        "schema_version": 1,
        "run_id": run_id,
        "state": "completed",
        "manifest_sha256": binding["run_manifest_sha256"],
    }
    _write_json(tmp_path / final_ref, final)
    terminal_status = {
        "schema_version": 1,
        "run_id": run_id,
        "state": "completed",
        "manifest_sha256": binding["run_manifest_sha256"],
        "updated_at": "2026-08-07T00:00:02.000Z",
        "final_ref": final_ref,
    }
    monkeypatch.setattr(
        RunService,
        "status",
        lambda _service, _run_id, **_kwargs: dict(terminal_status),
    )
    monkeypatch.setattr(
        RunService,
        "terminal_with_commit",
        lambda _service, _run_id: (
            dict(final),
            (final_ref,),
            f"Record run {run_id} final",
        ),
    )

    status = TaskService(tmp_path).status(
        task_id,
        commit_paths=_commit(tmp_path),
    )

    assert status["final_ref"] == final_ref
    assert _git_bytes(tmp_path, "show", f"HEAD:{final_ref}") == (
        tmp_path / final_ref
    ).read_bytes()


def test_terminal_status_without_callback_exposes_valid_committed_final_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _service, brief, ownership = _prepared_task(tmp_path)
    task_id = str(brief["task_id"])
    binding = ensure_task_run(
        tmp_path,
        brief,
        ownership,
        actor="principal",
        commit_paths=_commit(tmp_path),
    )
    run_status, final = _fail_bound_run_launch(tmp_path, binding, monkeypatch)
    run_id = str(binding["run_id"])
    final_ref = f"runs/{run_id}/final.json"
    _commit(tmp_path)((final_ref,), f"Record run {run_id} final")
    assert RunService(tmp_path).read_validated_final(run_id) == final
    before = (
        _git(tmp_path, "rev-parse", "HEAD"),
        _git(tmp_path, "status", "--short"),
    )

    status = TaskService(tmp_path).status(task_id)

    assert status["state"] == run_status["state"]
    assert status["final_ref"] == final_ref
    assert (
        _git(tmp_path, "rev-parse", "HEAD"),
        _git(tmp_path, "status", "--short"),
    ) == before
    assert RunService(tmp_path).read_validated_final(run_id) == final


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param("no-op", id="no-op"),
        pytest.param("wrong-result", id="wrong-result"),
        pytest.param("extra-path", id="commits-extra-path"),
        pytest.param("dirty-final", id="leaves-final-dirty"),
        pytest.param("runtime-error", id="raises-runtime-error"),
        pytest.param("value-error", id="raises-value-error"),
    ],
)
def test_status_rejects_adversarial_terminal_commit_callback_without_task_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    _service, brief, ownership = _prepared_task(tmp_path)
    task_id = str(brief["task_id"])
    binding = ensure_task_run(
        tmp_path,
        brief,
        ownership,
        actor="principal",
        commit_paths=_commit(tmp_path),
    )
    _run_status, _final = _fail_bound_run_launch(tmp_path, binding, monkeypatch)
    run_id = str(binding["run_id"])
    final_ref = f"runs/{run_id}/final.json"
    final_path = tmp_path / final_ref
    final_bytes = final_path.read_bytes()
    main_dirt = tmp_path / "unrelated-main-dirt.bin"
    child_dirt = Path(str(ownership["worktree_path"])) / "unrelated-child-dirt.bin"
    main_dirt.write_bytes(b"main dirt\x00unchanged")
    child_dirt.write_bytes(b"child dirt\x00unchanged")
    dirt = {main_dirt: main_dirt.read_bytes(), child_dirt: child_dirt.read_bytes()}
    runtime_before = _task_runtime_snapshot(tmp_path, task_id)

    def bad_commit(
        paths: tuple[str, ...],
        _message: str,
    ) -> dict[str, object]:
        if failure == "runtime-error":
            raise RuntimeError("injected terminal callback failure")
        if failure == "value-error":
            raise ValueError("injected terminal callback failure")
        if failure == "wrong-result":
            return {"commit": "not-a-commit", "paths": list(paths)}
        if failure == "no-op":
            return {
                "commit": _git(tmp_path, "rev-parse", "HEAD"),
                "paths": list(paths),
                "reused": True,
                "enforcement_class": "cooperative",
            }
        if failure == "extra-path":
            extra_ref = "unexpected-terminal-commit.txt"
            (tmp_path / extra_ref).write_text("unexpected\n", encoding="utf-8")
            _git(tmp_path, "add", paths[0], extra_ref)
            _git(tmp_path, "commit", "-qm", "commit terminal with extra path")
        else:
            _git(tmp_path, "add", paths[0])
            _git(tmp_path, "commit", "-qm", "commit then dirty terminal")
            final_path.write_bytes(final_bytes + b"mutated")
        return {
            "commit": _git(tmp_path, "rev-parse", "HEAD"),
            "paths": list(paths),
            "reused": False,
            "enforcement_class": "cooperative",
        }

    result: dict[str, object] | None = None
    with pytest.raises(TaskError, match=task_id) as raised:
        result = TaskService(tmp_path).status(
            task_id,
            commit_paths=bad_commit,
        )

    assert result is None
    assert raised.value.__cause__ is not None
    assert _task_runtime_snapshot(tmp_path, task_id) == runtime_before
    assert {path: path.read_bytes() for path in dirt} == dirt


def test_lost_status_never_inspects_or_commits_a_terminal_final(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _service, brief, ownership = _prepared_task(tmp_path)
    task_id = str(brief["task_id"])
    binding = ensure_task_run(
        tmp_path,
        brief,
        ownership,
        actor="principal",
        commit_paths=_commit(tmp_path),
    )
    monkeypatch.setattr(
        RunService,
        "status",
        lambda _service, run_id, **_kwargs: {
            "schema_version": 1,
            "run_id": run_id,
            "state": "lost",
            "manifest_sha256": binding["run_manifest_sha256"],
            "updated_at": "2026-08-07T00:00:02.000Z",
            "final_ref": None,
            "reason": "process identity unavailable",
        },
    )
    monkeypatch.setattr(
        RunService,
        "terminal_with_commit",
        lambda *_args, **_kwargs: pytest.fail("lost must not inspect a final"),
    )

    status = TaskService(tmp_path).status(task_id, commit_paths=_commit(tmp_path))

    assert status["state"] == "lost"
    assert status["final_ref"] is None
    with pytest.raises(TaskError, match="not terminal"):
        TaskService(tmp_path).collect(task_id)


def test_stop_delegates_to_bound_run_without_task_stop_material(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _service, brief, ownership = _prepared_task(tmp_path)
    task_id = str(brief["task_id"])
    binding = ensure_task_run(
        tmp_path,
        brief,
        ownership,
        actor="principal",
        commit_paths=_commit(tmp_path),
    )
    calls: list[tuple[object, ...]] = []
    receipt = {"schema_version": 1, "kind": "run_stop_request"}

    def fake_stop(
        _service: RunService,
        run_id: str,
        *,
        actor: str,
        reason: str,
        signal_name: str = "TERM",
    ) -> dict[str, object]:
        calls.append((run_id, actor, reason, signal_name))
        return receipt

    monkeypatch.setattr(RunService, "stop", fake_stop)

    result = TaskService(tmp_path).stop(
        task_id,
        actor="principal",
        reason="evidence complete",
        signal_name="INT",
    )

    assert calls == [(binding["run_id"], "principal", "evidence complete", "INT")]
    assert result == {
        "schema_version": 1,
        "task_id": task_id,
        "run_id": binding["run_id"],
        "run_stop": receipt,
    }
    assert list((tmp_path / ".aros" / "tasks" / task_id).glob("stop*.json")) == []


def test_stop_before_run_binding_fails_without_stop_material(tmp_path: Path) -> None:
    _service, brief, _ownership = _prepared_task(tmp_path)
    task_id = str(brief["task_id"])
    runtime = tmp_path / ".aros" / "tasks" / task_id
    before = sorted(path.relative_to(runtime) for path in runtime.rglob("*"))

    with pytest.raises(TaskError, match="Run binding|run binding|Task Run"):
        TaskService(tmp_path).stop(
            task_id,
            actor="principal",
            reason="not started",
        )

    assert sorted(path.relative_to(runtime) for path in runtime.rglob("*")) == before
    assert list(runtime.glob("stop*.json")) == []


def test_waiting_run_stop_does_not_hold_unrelated_task_status_locks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _workspace(tmp_path)
    service = TaskService(tmp_path)
    stopping_brief = _brief_with_key(service, "waiting-stop")
    unrelated_brief = _brief_with_key(service, "unrelated-status")
    stopping_task_id = str(stopping_brief["task_id"])
    unrelated_task_id = str(unrelated_brief["task_id"])
    _git(
        tmp_path,
        "add",
        f"tasks/{stopping_task_id}/brief.json",
        f"tasks/{unrelated_task_id}/brief.json",
    )
    _git(tmp_path, "commit", "-qm", "record stop lock tasks")
    service._ensure_worktree(stopping_task_id, actor="principal")
    ownership = service._load_ownership(stopping_brief)
    binding = ensure_task_run(
        tmp_path,
        stopping_brief,
        ownership,
        actor="principal",
        commit_paths=_commit(tmp_path),
    )
    stop_entered = threading.Event()
    release_stop = threading.Event()
    status_finished = threading.Event()
    receipt = {"schema_version": 1, "kind": "run_stop_request"}

    def waiting_stop(
        _service: RunService,
        run_id: str,
        *,
        actor: str,
        reason: str,
        signal_name: str = "TERM",
    ) -> dict[str, object]:
        assert run_id == binding["run_id"]
        stop_entered.set()
        assert release_stop.wait(timeout=5)
        return receipt

    def read_unrelated_status() -> dict[str, object]:
        status = TaskService(tmp_path).status(unrelated_task_id)
        status_finished.set()
        return status

    monkeypatch.setattr(RunService, "stop", waiting_stop)
    with ThreadPoolExecutor(max_workers=2) as executor:
        stop_future = executor.submit(
            service.stop,
            stopping_task_id,
            actor="principal",
            reason="bounded wait",
        )
        assert stop_entered.wait(timeout=5)
        status_future = executor.submit(read_unrelated_status)
        try:
            assert status_finished.wait(timeout=1)
        finally:
            release_stop.set()
        status = status_future.result(timeout=5)
        stopped = stop_future.result(timeout=5)

    assert status["state"] == "prepared"
    assert stopped == {
        "schema_version": 1,
        "task_id": stopping_task_id,
        "run_id": binding["run_id"],
        "run_stop": receipt,
    }


@pytest.mark.parametrize("failure_source", ["binding", "run-status"])
def test_list_translates_run_projection_failures_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_source: str,
) -> None:
    import arbor.aros.task_run as task_run_module

    _service, brief, ownership = _prepared_task(tmp_path)
    task_id = str(brief["task_id"])
    ensure_task_run(
        tmp_path,
        brief,
        ownership,
        actor="principal",
        commit_paths=_commit(tmp_path),
    )
    if failure_source == "binding":
        injected: Exception = TaskRunError("injected strict binding failure")

        def fail_binding(*_args: object, **_kwargs: object) -> dict[str, object]:
            raise injected

        monkeypatch.setattr(task_run_module, "load_task_run", fail_binding)
    else:
        injected = RunError("injected Run status failure")

        def fail_run_status(*_args: object, **_kwargs: object) -> dict[str, object]:
            raise injected

        monkeypatch.setattr(RunService, "status", fail_run_status)
    before = (
        _git(tmp_path, "rev-parse", "HEAD"),
        _git(tmp_path, "status", "--short"),
        _task_runtime_snapshot(tmp_path, task_id),
    )

    with pytest.raises(TaskError, match=task_id) as raised:
        TaskService(tmp_path).list()

    assert raised.value.__cause__ is injected
    assert (
        _git(tmp_path, "rev-parse", "HEAD"),
        _git(tmp_path, "status", "--short"),
        _task_runtime_snapshot(tmp_path, task_id),
    ) == before


def test_list_mixes_prepared_worktree_and_run_status_in_task_id_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import arbor.aros.task_run as task_run_module

    _workspace(tmp_path)
    service = TaskService(tmp_path)
    briefs = [_brief_with_key(service, f"mixed-{index}") for index in range(4)]
    _git(tmp_path, "add", *(f"tasks/{brief['task_id']}/brief.json" for brief in briefs))
    _git(tmp_path, "commit", "-qm", "record mixed task briefs")
    worktree_brief = briefs[1]
    service._ensure_worktree(str(worktree_brief["task_id"]), actor="principal")
    run_brief = briefs[2]
    run_task_id = str(run_brief["task_id"])
    service._ensure_worktree(run_task_id, actor="principal")
    run_ownership = service._load_ownership(run_brief)
    running_binding = ensure_task_run(
        tmp_path,
        run_brief,
        run_ownership,
        actor="principal",
        commit_paths=_commit(tmp_path),
    )
    terminal_brief = briefs[3]
    terminal_task_id = str(terminal_brief["task_id"])
    service._ensure_worktree(terminal_task_id, actor="principal")
    terminal_ownership = service._load_ownership(terminal_brief)
    terminal_binding = ensure_task_run(
        tmp_path,
        terminal_brief,
        terminal_ownership,
        actor="principal",
        commit_paths=_commit(tmp_path),
    )
    terminal_run_status, _terminal_final = _fail_bound_run_launch(
        tmp_path,
        terminal_binding,
        monkeypatch,
    )
    terminal_run_id = str(terminal_binding["run_id"])
    terminal_final_ref = f"runs/{terminal_run_id}/final.json"
    terminal_final_bytes = (tmp_path / terminal_final_ref).read_bytes()
    real_status = RunService.status

    def mixed_status(
        run_service: RunService,
        run_id: str,
        **kwargs: object,
    ) -> dict[str, object]:
        if run_id == running_binding["run_id"]:
            return _running_run_status(tmp_path, run_id)
        return real_status(run_service, run_id, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        RunService,
        "status",
        mixed_status,
    )
    monkeypatch.setattr(
        task_run_module,
        "commit_terminal_run_if_present",
        lambda *_args, **_kwargs: pytest.fail("list must not commit a terminal final"),
    )
    reconcile_calls = 0
    real_reconcile = service._reconcile_authoritative_briefs

    def count_reconcile() -> None:
        nonlocal reconcile_calls
        reconcile_calls += 1
        real_reconcile()

    monkeypatch.setattr(service, "_reconcile_authoritative_briefs", count_reconcile)
    before = (
        _git(tmp_path, "rev-parse", "HEAD"),
        _git(tmp_path, "status", "--short"),
        terminal_final_bytes,
    )

    statuses = service.list()

    assert reconcile_calls == 1
    assert [status["task_id"] for status in statuses] == sorted(
        str(brief["task_id"]) for brief in briefs
    )
    assert {status["task_id"]: status["state"] for status in statuses} == {
        briefs[0]["task_id"]: "prepared",
        worktree_brief["task_id"]: "worktree_ready",
        run_brief["task_id"]: "running",
        terminal_brief["task_id"]: terminal_run_status["state"],
    }
    assert next(
        status for status in statuses if status["task_id"] == run_brief["task_id"]
    )["run_id"] == running_binding["run_id"]
    terminal_status = next(
        status
        for status in statuses
        if status["task_id"] == terminal_brief["task_id"]
    )
    assert terminal_status["run_id"] == terminal_run_id
    assert terminal_status["final_ref"] is None
    assert (
        _git(tmp_path, "rev-parse", "HEAD"),
        _git(tmp_path, "status", "--short"),
        (tmp_path / terminal_final_ref).read_bytes(),
    ) == before


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


def test_task_adapter_execs_frozen_argv_in_owned_worktree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import arbor.aros.task_adapter as adapter

    worktree = tmp_path / "child"
    worktree.mkdir()
    context = {
        "argv": ["worker", "--exact"],
        "worktree": str(worktree),
        "environment": {
            "PATH": "/controlled/bin",
            "AROS_TASK_ID": "TASK-20260807-test",
        },
    }
    loaded: list[tuple[Path, str]] = []

    def fake_load_adapter_context(workspace: Path, task_id: str) -> dict[str, object]:
        loaded.append((workspace, task_id))
        return context

    monkeypatch.setattr(adapter, "load_adapter_context", fake_load_adapter_context)
    changed: list[Path] = []
    executed: list[tuple[str, list[str], dict[str, str]]] = []
    monkeypatch.setattr(adapter.os, "chdir", lambda path: changed.append(Path(path)))
    monkeypatch.setattr(
        adapter.os,
        "execvpe",
        lambda executable, argv, env: executed.append((executable, argv, env)),
    )

    assert adapter.main(
        ["--workspace", str(tmp_path), "--task-id", "TASK-20260807-test"]
    ) == 0
    assert loaded == [(tmp_path, "TASK-20260807-test")]
    assert changed == [worktree]
    assert executed == [("worker", ["worker", "--exact"], context["environment"])]


def test_task_adapter_environment_is_explicitly_allowlisted(tmp_path: Path) -> None:
    from arbor.aros.task_adapter import build_adapter_environment

    runtime = tmp_path / "runtime"
    brief_path = tmp_path / "tasks/TASK-20260807-test/brief.json"
    worktree = tmp_path / ".worktree/tasks/TASK-20260807-test"
    environment = build_adapter_environment(
        runtime,
        task_id="TASK-20260807-test",
        brief_path=brief_path,
        worktree=worktree,
        base_commit="a" * 40,
        brief_sha256="b" * 64,
        source={
            "PATH": "/controlled/bin",
            "LANG": "C.UTF-8",
            "SECRET_TOKEN": "must-not-pass",
            "PYTHONPATH": "/must/not/pass",
        },
    )

    assert environment == {
        "PATH": "/controlled/bin",
        "LANG": "C.UTF-8",
        "HOME": str(runtime / "home"),
        "TMPDIR": str(runtime / "tmp"),
        "AROS_TASK_ID": "TASK-20260807-test",
        "AROS_TASK_BRIEF": str(brief_path),
        "AROS_TASK_WORKTREE": str(worktree),
        "AROS_TASK_BASE_COMMIT": "a" * 40,
        "AROS_TASK_BRIEF_SHA256": "b" * 64,
    }


@pytest.mark.parametrize(
    "context",
    [
        pytest.param([], id="context-not-dict"),
        pytest.param(
            {"argv": ("worker",), "worktree": "/owned", "environment": {}},
            id="argv-not-list",
        ),
        pytest.param(
            {"argv": [], "worktree": "/owned", "environment": {}},
            id="argv-empty",
        ),
        pytest.param(
            {"argv": ["worker", 1], "worktree": "/owned", "environment": {}},
            id="argv-item-not-string",
        ),
        pytest.param(
            {"argv": ["worker", ""], "worktree": "/owned", "environment": {}},
            id="argv-item-empty",
        ),
        pytest.param(
            {"argv": ["worker"], "worktree": 1, "environment": {}},
            id="worktree-not-string",
        ),
        pytest.param(
            {"argv": ["worker"], "worktree": "", "environment": {}},
            id="worktree-empty",
        ),
        pytest.param(
            {"argv": ["worker"], "worktree": "/owned", "environment": []},
            id="environment-not-dict",
        ),
        pytest.param(
            {"argv": ["worker"], "worktree": "/owned", "environment": {1: "x"}},
            id="environment-key-not-string",
        ),
        pytest.param(
            {
                "argv": ["worker"],
                "worktree": "/owned",
                "environment": {"PATH": 1},
            },
            id="environment-value-not-string",
        ),
    ],
)
def test_task_adapter_rejects_invalid_context_before_side_effects(
    context: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import arbor.aros.task_adapter as adapter

    monkeypatch.setattr(adapter, "load_adapter_context", lambda *_args: context)
    changed: list[str] = []
    executed: list[tuple[str, list[str], dict[str, str]]] = []
    monkeypatch.setattr(adapter.os, "chdir", lambda path: changed.append(path))
    monkeypatch.setattr(
        adapter.os,
        "execvpe",
        lambda executable, argv, env: executed.append((executable, argv, env)),
    )

    with pytest.raises(ValueError, match="Task adapter context is invalid"):
        adapter.main(["--workspace", "/workspace", "--task-id", "TASK-test"])

    assert changed == []
    assert executed == []
