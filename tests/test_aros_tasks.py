"""Durable child-task record tests for AROS."""

from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable

import pytest

from arbor.aros.store import atomic_write_json, json_sha256
from arbor.aros.tasks import TaskError, TaskService
from arbor.aros.workspace import init_workspace


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _init_workspace(root: Path) -> str:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    _git(root, "config", "user.email", "aros@example.invalid")
    _git(root, "config", "user.name", "AROS test")
    (root / "README.md").write_text("# test workspace\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-qm", "initial state")
    init_workspace(root, "Test child task records")
    _git(root, "add", ".gitignore", "AGENTS.md", "AROS.md", "memory/NOW.md")
    _git(root, "commit", "-qm", "initialize AROS")
    return _git(root, "rev-parse", "HEAD")


def _request(*, key: str = "task-key") -> dict[str, object]:
    return {
        "actor": "principal",
        "mode": "write",
        "adapter_argv": ["adapter", "--exact"],
        "capabilities": {"network": False, "shell": True},
        "deliverables": ["result.json"],
        "acceptance": ["python verify.py"],
        "timeout_seconds": 60,
        "idempotency_key": key,
    }


def _create(
    service: TaskService,
    *,
    key: str = "task-key",
    objective: str = "bounded objective",
) -> dict[str, object]:
    return service.create(objective, **_request(key=key))  # type: ignore[arg-type]


def _request_from_brief(brief: dict[str, object]) -> dict[str, object]:
    return {
        field: brief[field]
        for field in (
            "objective",
            "actor",
            "mode",
            "adapter_argv",
            "capabilities",
            "deliverables",
            "acceptance",
            "timeout_seconds",
            "idempotency_key",
        )
    }


def _rehash_brief(brief: dict[str, object]) -> None:
    brief["brief_sha256"] = json_sha256(
        {key: value for key, value in brief.items() if key != "brief_sha256"}
    )


def test_create_freezes_brief_and_prepared_status_without_execution(
    tmp_path: Path,
) -> None:
    head = _init_workspace(tmp_path)
    dirty = tmp_path / "unrelated.txt"
    dirty.write_text("preserve me\n", encoding="utf-8")
    marker = tmp_path / "adapter-ran"
    worktrees_before = _git(tmp_path, "worktree", "list", "--porcelain")
    service = TaskService(tmp_path)

    brief = service.create(
        "  Inspect committed state  ",
        actor="  principal  ",
        mode="read_only",
        adapter_argv=[
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(marker)!r}).touch()",
            "  exact argument  ",
        ],
        capabilities={"network": False, "shell": False},
        deliverables=["reports/inspection.json"],
        acceptance=["python -m pytest -q"],
        timeout_seconds=12.5,
        idempotency_key="  inspect-once  ",
    )

    task_id = str(brief["task_id"])
    assert re.fullmatch(r"TASK-\d{8}-[A-Za-z0-9][A-Za-z0-9-]*", task_id)
    assert set(brief) == {
        "schema_version",
        "task_id",
        "objective",
        "mode",
        "base_commit",
        "actor",
        "adapter_argv",
        "capabilities",
        "deliverables",
        "acceptance",
        "timeout_seconds",
        "idempotency_key",
        "request_sha256",
        "created_at",
        "brief_sha256",
    }
    assert brief["schema_version"] == 1
    assert brief["objective"] == "Inspect committed state"
    assert brief["mode"] == "read_only"
    assert brief["base_commit"] == head
    assert re.fullmatch(r"[0-9a-f]{40}", str(brief["base_commit"]))
    assert brief["actor"] == "principal"
    assert brief["adapter_argv"][-1] == "  exact argument  "
    assert brief["capabilities"] == {"network": False, "shell": False}
    assert brief["deliverables"] == ["reports/inspection.json"]
    assert brief["acceptance"] == ["python -m pytest -q"]
    assert brief["timeout_seconds"] == 12.5
    assert brief["idempotency_key"] == "inspect-once"
    assert str(brief["created_at"]).endswith("Z")
    request = {
        "objective": "Inspect committed state",
        "actor": "principal",
        "mode": "read_only",
        "adapter_argv": brief["adapter_argv"],
        "capabilities": brief["capabilities"],
        "deliverables": brief["deliverables"],
        "acceptance": brief["acceptance"],
        "timeout_seconds": 12.5,
        "idempotency_key": "inspect-once",
    }
    assert brief["request_sha256"] == json_sha256(request)
    assert brief["brief_sha256"] == json_sha256(
        {key: value for key, value in brief.items() if key != "brief_sha256"}
    )

    brief_path = tmp_path / "tasks" / task_id / "brief.json"
    assert json.loads(brief_path.read_text(encoding="utf-8")) == brief
    status = {
        "schema_version": 1,
        "task_id": task_id,
        "state": "prepared",
        "brief_sha256": brief["brief_sha256"],
        "updated_at": brief["created_at"],
    }
    assert service.status(task_id) == status
    assert service.list() == [status]
    assert json.loads(
        (tmp_path / ".aros" / "tasks" / task_id / "status.json").read_text(
            encoding="utf-8"
        )
    ) == status
    key_digest = hashlib.sha256(b"inspect-once").hexdigest()
    index_path = tmp_path / ".aros" / "tasks" / "idempotency" / f"{key_digest}.json"
    assert index_path.is_file()
    assert "inspect-once" not in str(index_path.relative_to(tmp_path))

    assert dirty.read_text(encoding="utf-8") == "preserve me\n"
    assert _git(tmp_path, "rev-parse", "HEAD") == head
    assert _git(tmp_path, "worktree", "list", "--porcelain") == worktrees_before
    assert not marker.exists()
    assert not (tmp_path / ".worktree" / "tasks").exists()


def test_service_requires_exact_git_root_and_initialized_aros_workspace(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    _git(repository, "config", "user.email", "aros@example.invalid")
    _git(repository, "config", "user.name", "AROS test")
    (repository / "README.md").write_text("# repository\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-qm", "initial state")

    with pytest.raises(TaskError, match="not initialized"):
        TaskService(repository)

    init_workspace(repository, "Exact root test")
    nested = repository / "nested"
    nested.mkdir()
    with pytest.raises(TaskError, match="Git repository root"):
        TaskService(nested)

    alias = tmp_path / "repository-alias"
    alias.symlink_to(repository, target_is_directory=True)
    with pytest.raises(TaskError, match="exact Git repository root"):
        TaskService(alias)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("objective", "  ", "objective"),
        ("actor", "", "actor"),
        ("idempotency_key", "\t", "idempotency_key"),
        ("mode", "readonly", "mode"),
        ("mode", 1, "mode"),
        ("adapter_argv", [], "adapter_argv"),
        ("adapter_argv", ("adapter",), "adapter_argv"),
        ("adapter_argv", [""], "adapter_argv"),
        ("adapter_argv", ["adapter", "bad\x00argument"], "adapter_argv"),
        ("capabilities", {"network": False}, "capabilities"),
        (
            "capabilities",
            {"network": False, "shell": False, "filesystem": True},
            "capabilities",
        ),
        ("capabilities", {"network": 0, "shell": True}, "booleans"),
        ("deliverables", ("result.json",), "deliverables"),
        ("deliverables", ["result.json", 3], "deliverables"),
        ("acceptance", "python verify.py", "acceptance"),
        ("acceptance", [None], "acceptance"),
        ("timeout_seconds", 0, "positive"),
        ("timeout_seconds", -1, "positive"),
        ("timeout_seconds", True, "positive"),
        ("timeout_seconds", "60", "positive"),
        ("timeout_seconds", math.inf, "finite"),
        ("timeout_seconds", math.nan, "finite"),
    ),
)
def test_create_rejects_invalid_request_fields_without_writing_records(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    objective: object = "bounded objective"
    request = _request()
    if field == "objective":
        objective = value
    else:
        request[field] = value

    with pytest.raises(TaskError, match=message):
        service.create(objective, **request)  # type: ignore[arg-type]

    assert not (tmp_path / "tasks").exists()
    assert not (tmp_path / ".aros" / "tasks").exists()


def test_create_rejects_an_oversized_integer_timeout_as_a_task_error(
    tmp_path: Path,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    request = _request()
    request["timeout_seconds"] = 10**10_000

    with pytest.raises(TaskError, match="finite"):
        service.create("bounded objective", **request)  # type: ignore[arg-type]

    assert not (tmp_path / "tasks").exists()


def test_create_is_idempotent_for_the_same_request_and_rejects_a_change(
    tmp_path: Path,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    request = _request(key="one-logical-task")

    first = service.create("bounded objective", **request)  # type: ignore[arg-type]
    first_status = service.status(str(first["task_id"]))
    second = service.create("bounded objective", **request)  # type: ignore[arg-type]

    assert second == first
    assert service.status(str(second["task_id"])) == first_status
    assert len(list((tmp_path / "tasks").glob("TASK-*/brief.json"))) == 1
    with pytest.raises(TaskError, match="idempotency key.*different task request"):
        service.create("changed objective", **request)  # type: ignore[arg-type]
    assert len(list((tmp_path / "tasks").glob("TASK-*/brief.json"))) == 1


@pytest.mark.parametrize("missing", ("status", "index", "both"))
def test_create_recovers_missing_prepared_records_from_the_immutable_brief(
    tmp_path: Path,
    missing: str,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    key = "recover-partial-create"
    brief = _create(service, key=key)
    task_id = str(brief["task_id"])
    status_path = tmp_path / ".aros" / "tasks" / task_id / "status.json"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    index_path = tmp_path / ".aros" / "tasks" / "idempotency" / f"{digest}.json"
    if missing in {"status", "both"}:
        status_path.unlink()
    if missing in {"index", "both"}:
        index_path.unlink()

    replayed = _create(service, key=key)

    assert replayed == brief
    assert service.status(task_id) == {
        "schema_version": 1,
        "task_id": task_id,
        "state": "prepared",
        "brief_sha256": brief["brief_sha256"],
        "updated_at": brief["created_at"],
    }
    index = json.loads(index_path.read_text(encoding="utf-8"))
    assert index["idempotency_key_sha256"] == digest
    assert index["request_sha256"] == brief["request_sha256"]
    assert index["brief_sha256"] == brief["brief_sha256"]


def test_create_rejects_a_noncommit_head_even_when_it_is_40_hex(
    tmp_path: Path,
) -> None:
    _init_workspace(tmp_path)
    blob = _git(tmp_path, "hash-object", "-w", "README.md")
    _git(tmp_path, "update-ref", "refs/tags/blob-head", blob)
    _git(tmp_path, "symbolic-ref", "HEAD", "refs/tags/blob-head")
    assert _git(tmp_path, "rev-parse", "--verify", "HEAD") == blob
    service = TaskService(tmp_path)

    with pytest.raises(TaskError, match="committed 40-hex Git HEAD"):
        _create(service)


@pytest.mark.parametrize("relative", ("tasks", ".aros/tasks", ".aros/locks"))
def test_create_rejects_symlinked_reserved_directories(
    tmp_path: Path,
    relative: str,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    target = tmp_path / "alias-target"
    target.mkdir()
    reserved = tmp_path / relative
    reserved.parent.mkdir(parents=True, exist_ok=True)
    reserved.symlink_to(target, target_is_directory=True)

    with pytest.raises(TaskError, match="symlink|plain directory"):
        _create(service)

    assert list(target.iterdir()) == []


@pytest.mark.parametrize("relative", (".aros", "AROS.md", "memory/NOW.md"))
def test_service_rejects_symlinked_workspace_control_paths(
    tmp_path: Path,
    relative: str,
) -> None:
    _init_workspace(tmp_path)
    control = tmp_path / relative
    if control.is_dir():
        control.rmdir()
        target = tmp_path / "control-target"
        target.mkdir()
        control.symlink_to(target, target_is_directory=True)
    else:
        control.unlink()
        target = tmp_path / "control-target"
        target.write_text("control\n", encoding="utf-8")
        control.symlink_to(target)

    with pytest.raises(TaskError, match="symlink|not initialized"):
        TaskService(tmp_path)


@pytest.mark.parametrize("kind", ("versioned", "runtime"))
def test_create_rejects_preexisting_task_directories_before_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    task_id = "TASK-20260802-conflict"
    monkeypatch.setattr(service, "_new_task_id", lambda _objective: task_id)
    versioned = tmp_path / "tasks" / task_id
    runtime = tmp_path / ".aros" / "tasks" / task_id
    conflict = versioned if kind == "versioned" else runtime
    conflict.mkdir(parents=True)

    with pytest.raises(TaskError, match="conflict|already exists"):
        _create(service)

    assert not (versioned / "brief.json").exists()
    assert not (runtime / "status.json").exists()


@pytest.mark.parametrize("kind", ("versioned", "runtime"))
def test_create_rejects_symlinked_task_directories_without_following_them(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    task_id = "TASK-20260802-symlink"
    monkeypatch.setattr(service, "_new_task_id", lambda _objective: task_id)
    versioned = tmp_path / "tasks" / task_id
    runtime = tmp_path / ".aros" / "tasks" / task_id
    link = versioned if kind == "versioned" else runtime
    link.parent.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "task-alias-target"
    target.mkdir()
    link.symlink_to(target, target_is_directory=True)

    with pytest.raises(TaskError, match="symlink|conflict|already exists"):
        _create(service)

    assert list(target.iterdir()) == []


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("hash", "brief hash"),
        ("identity", "brief identity"),
        ("request_hash", "request hash"),
        ("extra_field", "brief schema"),
        ("base_commit", "base_commit"),
        ("capabilities", "capabilities"),
    ),
)
def test_status_strictly_validates_brief_readback(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    created = _create(service)
    task_id = str(created["task_id"])
    brief_path = tmp_path / "tasks" / task_id / "brief.json"
    status_path = tmp_path / ".aros" / "tasks" / task_id / "status.json"
    brief = json.loads(brief_path.read_text(encoding="utf-8"))
    status = json.loads(status_path.read_text(encoding="utf-8"))

    if mutation == "hash":
        brief["objective"] = "tampered objective"
    elif mutation == "identity":
        brief["task_id"] = "TASK-20260802-other"
        _rehash_brief(brief)
    elif mutation == "request_hash":
        brief["request_sha256"] = "0" * 64
        _rehash_brief(brief)
        status["brief_sha256"] = brief["brief_sha256"]
    elif mutation == "extra_field":
        brief["unexpected"] = True
        _rehash_brief(brief)
        status["brief_sha256"] = brief["brief_sha256"]
    elif mutation == "base_commit":
        brief["base_commit"] = "not-a-commit"
        _rehash_brief(brief)
        status["brief_sha256"] = brief["brief_sha256"]
    elif mutation == "capabilities":
        brief["capabilities"] = {"network": 0, "shell": True}
        brief["request_sha256"] = json_sha256(_request_from_brief(brief))
        _rehash_brief(brief)
        status["brief_sha256"] = brief["brief_sha256"]
    else:  # pragma: no cover - parametrization is exhaustive
        raise AssertionError(mutation)
    atomic_write_json(brief_path, brief)
    atomic_write_json(status_path, status)

    with pytest.raises(TaskError, match=message):
        service.status(task_id)


@pytest.mark.parametrize(
    ("mutate", "message"),
    (
        (lambda status: status.update(unexpected=True), "status schema"),
        (lambda status: status.update(state="running"), "task status"),
        (
            lambda status: status.update(task_id="TASK-20260802-other"),
            "status identity",
        ),
        (lambda status: status.update(brief_sha256="0" * 64), "brief hash"),
    ),
)
def test_status_strictly_validates_runtime_readback(
    tmp_path: Path,
    mutate: Callable[[dict[str, object]], None],
    message: str,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service)
    task_id = str(brief["task_id"])
    status_path = tmp_path / ".aros" / "tasks" / task_id / "status.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    mutate(status)
    atomic_write_json(status_path, status)

    with pytest.raises(TaskError, match=message):
        service.status(task_id)


@pytest.mark.parametrize("record", ("brief", "status"))
def test_status_rejects_symlinked_record_files(
    tmp_path: Path,
    record: str,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service)
    task_id = str(brief["task_id"])
    path = (
        tmp_path / "tasks" / task_id / "brief.json"
        if record == "brief"
        else tmp_path / ".aros" / "tasks" / task_id / "status.json"
    )
    target = tmp_path / f"{record}-alias-target.json"
    path.rename(target)
    path.symlink_to(target)

    with pytest.raises(TaskError, match="symlink|plain file"):
        service.status(task_id)


@pytest.mark.parametrize("relative", ("tasks", ".aros/tasks", ".aros"))
def test_status_rejects_a_symlinked_record_parent_after_construction(
    tmp_path: Path,
    relative: str,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service)
    task_id = str(brief["task_id"])
    parent = tmp_path / relative
    target = tmp_path / f"{relative.replace('/', '-')}-parent-target"
    parent.rename(target)
    parent.symlink_to(target, target_is_directory=True)

    with pytest.raises(TaskError, match="symlink|plain directory"):
        service.status(task_id)


def test_status_rejects_a_noncanonical_task_id_before_path_access(
    tmp_path: Path,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)

    with pytest.raises(TaskError, match="invalid task ID"):
        service.status("TASK-20260802-trailing-")


def test_status_rejects_non_ascii_task_id_date_digits_before_path_access(
    tmp_path: Path,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)

    with pytest.raises(TaskError, match="invalid task ID"):
        service.status("TASK-２０２６０８０２-child")


def test_status_rejects_a_noncanonical_brief_timestamp(
    tmp_path: Path,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service)
    task_id = str(brief["task_id"])
    brief_path = tmp_path / "tasks" / task_id / "brief.json"
    status_path = tmp_path / ".aros" / "tasks" / task_id / "status.json"
    brief["created_at"] = "Z"
    _rehash_brief(brief)
    status = json.loads(status_path.read_text(encoding="utf-8"))
    status["brief_sha256"] = brief["brief_sha256"]
    status["updated_at"] = "Z"
    atomic_write_json(brief_path, brief)
    atomic_write_json(status_path, status)

    with pytest.raises(TaskError, match="created_at.*UTC timestamp"):
        service.status(task_id)


def test_status_rejects_a_calendar_invalid_brief_timestamp(
    tmp_path: Path,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    key = "calendar-invalid-timestamp"
    brief = _create(service, key=key)
    task_id = str(brief["task_id"])
    brief_path = tmp_path / "tasks" / task_id / "brief.json"
    status_path = tmp_path / ".aros" / "tasks" / task_id / "status.json"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    index_path = tmp_path / ".aros" / "tasks" / "idempotency" / f"{digest}.json"
    invalid_timestamp = "2026-02-31T12:00:00.000Z"
    brief["created_at"] = invalid_timestamp
    _rehash_brief(brief)
    status = json.loads(status_path.read_text(encoding="utf-8"))
    status["brief_sha256"] = brief["brief_sha256"]
    status["updated_at"] = invalid_timestamp
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["brief_sha256"] = brief["brief_sha256"]
    index["created_at"] = invalid_timestamp
    atomic_write_json(brief_path, brief)
    atomic_write_json(status_path, status)
    atomic_write_json(index_path, index)

    with pytest.raises(TaskError, match="created_at.*UTC timestamp"):
        service.status(task_id)


def test_idempotency_index_is_strict_and_contains_no_plaintext_key(
    tmp_path: Path,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    key = "private-stable-key"
    brief = _create(service, key=key)
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    index_path = tmp_path / ".aros" / "tasks" / "idempotency" / f"{digest}.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))

    assert set(index) == {
        "schema_version",
        "idempotency_key_sha256",
        "request_sha256",
        "task_id",
        "brief_sha256",
        "created_at",
    }
    assert index["idempotency_key_sha256"] == digest
    assert index["request_sha256"] == brief["request_sha256"]
    assert index["brief_sha256"] == brief["brief_sha256"]
    assert key not in index_path.name
    assert key not in json.dumps(index, sort_keys=True)


@pytest.mark.parametrize("reader", ("status", "list"))
@pytest.mark.parametrize("problem", ("missing", "tampered", "conflicting"))
def test_task_readback_requires_a_strictly_bound_idempotency_index(
    tmp_path: Path,
    reader: str,
    problem: str,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    key = "readback-index-authority"
    brief = _create(service, key=key)
    task_id = str(brief["task_id"])
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    index_path = tmp_path / ".aros" / "tasks" / "idempotency" / f"{digest}.json"
    if problem == "missing":
        index_path.unlink()
    elif problem == "tampered":
        index = json.loads(index_path.read_text(encoding="utf-8"))
        index["unexpected"] = True
        atomic_write_json(index_path, index)
    else:
        brief_path = tmp_path / "tasks" / task_id / "brief.json"
        status_path = tmp_path / ".aros" / "tasks" / task_id / "status.json"
        status = json.loads(status_path.read_text(encoding="utf-8"))
        brief["base_commit"] = "0" * 40
        _rehash_brief(brief)
        status["brief_sha256"] = brief["brief_sha256"]
        atomic_write_json(brief_path, brief)
        atomic_write_json(status_path, status)

    with pytest.raises(TaskError, match="idempotency index"):
        service.status(task_id) if reader == "status" else service.list()


@pytest.mark.parametrize("mutation", ("extra", "key_hash", "brief_hash"))
def test_create_rejects_a_tampered_idempotency_index(
    tmp_path: Path,
    mutation: str,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    key = "tamper-index"
    _create(service, key=key)
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    index_path = tmp_path / ".aros" / "tasks" / "idempotency" / f"{digest}.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    if mutation == "extra":
        index["unexpected"] = True
    elif mutation == "key_hash":
        index["idempotency_key_sha256"] = "0" * 64
    else:
        index["brief_sha256"] = "0" * 64
    atomic_write_json(index_path, index)

    with pytest.raises(TaskError, match="idempotency index"):
        _create(service, key=key)


def test_list_is_sorted_and_rejects_unrecognized_task_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    task_ids = iter(("TASK-20260802-zeta", "TASK-20260802-alpha"))
    monkeypatch.setattr(service, "_new_task_id", lambda _objective: next(task_ids))
    zeta = _create(service, key="zeta")
    alpha = _create(service, key="alpha")

    assert service.list() == [
        service.status(str(alpha["task_id"])),
        service.status(str(zeta["task_id"])),
    ]

    (tmp_path / "tasks" / "unrecognized").mkdir()
    with pytest.raises(TaskError, match="unrecognized task entry"):
        service.list()
