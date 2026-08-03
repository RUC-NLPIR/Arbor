"""Durable child-task record tests for AROS."""

from __future__ import annotations

import errno
import hashlib
import inspect
import json
import math
import os
import re
import shlex
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from typing import Callable

import pytest

import arbor.aros.tasks as tasks_module
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


def _git_ref_exists(root: Path, ref: str) -> bool:
    return subprocess.run(
        ["git", "-C", str(root), "show-ref", "--verify", "--quiet", ref],
        check=False,
    ).returncode == 0


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


def _commit_brief(root: Path, brief: dict[str, object]) -> str:
    task_id = str(brief["task_id"])
    _git(root, "add", f"tasks/{task_id}/brief.json")
    _git(root, "commit", "-qm", f"record {task_id}")
    return _git(root, "rev-parse", "HEAD")


@pytest.mark.parametrize("mode", ("read_only", "write"))
def test_start_prepares_a_branch_attached_owned_worktree_without_execution(
    tmp_path: Path,
    mode: str,
) -> None:
    base_commit = _init_workspace(tmp_path)
    marker = tmp_path / "adapter-ran"
    service = TaskService(tmp_path)
    request = _request(key=f"start-{mode}")
    request["mode"] = mode
    request["adapter_argv"] = [
        sys.executable,
        "-c",
        f"from pathlib import Path; Path({str(marker)!r}).touch()",
    ]
    brief = service.create("prepare isolated child", **request)  # type: ignore[arg-type]
    task_id = str(brief["task_id"])
    parent_head = _commit_brief(tmp_path, brief)
    worktree = (tmp_path / ".worktree" / "tasks" / task_id).absolute()
    branch = f"aros/task/{task_id}"

    status = service._ensure_worktree(task_id, actor="delegate-principal")

    ownership_path = tmp_path / ".aros" / "tasks" / task_id / "ownership.json"
    ownership = json.loads(ownership_path.read_text(encoding="utf-8"))
    assert set(ownership) == {
        "schema_version",
        "task_id",
        "brief_sha256",
        "actor",
        "worktree_path",
        "branch",
        "base_commit",
        "parent_head",
        "acquired_at",
        "ownership_sha256",
    }
    assert ownership == {
        **ownership,
        "schema_version": 1,
        "task_id": task_id,
        "brief_sha256": brief["brief_sha256"],
        "actor": "delegate-principal",
        "worktree_path": str(worktree),
        "branch": branch,
        "base_commit": base_commit,
        "parent_head": parent_head,
        "ownership_sha256": json_sha256(
            {
                key: value
                for key, value in ownership.items()
                if key != "ownership_sha256"
            }
        ),
    }
    assert status == {
        "schema_version": 1,
        "task_id": task_id,
        "state": "worktree_ready",
        "brief_sha256": brief["brief_sha256"],
        "ownership_sha256": ownership["ownership_sha256"],
        "updated_at": ownership["acquired_at"],
    }
    assert service.status(task_id) == status
    assert service.list() == [status]
    assert worktree.is_dir()
    assert Path(_git(worktree, "rev-parse", "--show-toplevel")) == worktree
    assert _git(worktree, "branch", "--show-current") == branch
    assert _git(worktree, "rev-parse", "HEAD") == base_commit
    assert _git(tmp_path, "rev-parse", "HEAD") == parent_head
    assert not marker.exists()


@pytest.mark.parametrize("dirty_kind", ("unstaged", "staged", "untracked"))
def test_start_rejects_and_preserves_a_dirty_parent_without_allocating(
    tmp_path: Path,
    dirty_kind: str,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service, key=f"dirty-parent-{dirty_kind}")
    task_id = str(brief["task_id"])
    _commit_brief(tmp_path, brief)
    dirty = tmp_path / ("README.md" if dirty_kind != "untracked" else "untracked.txt")
    dirty.write_text(f"preserve {dirty_kind}\n", encoding="utf-8")
    if dirty_kind == "staged":
        _git(tmp_path, "add", "README.md")

    with pytest.raises(TaskError, match="clean|dirty"):
        service._ensure_worktree(task_id)

    assert dirty.read_text(encoding="utf-8") == f"preserve {dirty_kind}\n"
    assert not (tmp_path / ".worktree" / "tasks" / task_id).exists()
    assert not _git_ref_exists(tmp_path, f"refs/heads/aros/task/{task_id}")


def test_start_requires_the_brief_to_be_committed_at_current_head(
    tmp_path: Path,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service, key="uncommitted-brief")
    task_id = str(brief["task_id"])

    with pytest.raises(TaskError, match="committed|clean"):
        service._ensure_worktree(task_id)

    assert (tmp_path / "tasks" / task_id / "brief.json").is_file()
    assert not (tmp_path / ".worktree" / "tasks" / task_id).exists()


def test_start_compares_committed_and_working_brief_bytes_even_if_index_hides_change(
    tmp_path: Path,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service, key="hidden-brief-mismatch")
    task_id = str(brief["task_id"])
    _commit_brief(tmp_path, brief)
    brief_path = tmp_path / "tasks" / task_id / "brief.json"
    _git(tmp_path, "update-index", "--assume-unchanged", f"tasks/{task_id}/brief.json")
    original = brief_path.read_bytes()
    brief_path.write_bytes(original + b" ")
    assert _git(tmp_path, "status", "--porcelain") == ""

    with pytest.raises(TaskError, match="committed brief|bytes|mismatch"):
        service._ensure_worktree(task_id)

    assert brief_path.read_bytes() == original + b" "
    assert not (tmp_path / ".worktree" / "tasks" / task_id).exists()


@pytest.mark.parametrize("index_flag", ("--assume-unchanged", "--skip-worktree"))
def test_start_rejects_parent_changes_hidden_by_index_flags(
    tmp_path: Path,
    index_flag: str,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service, key=f"hidden-parent-change-{index_flag}")
    task_id = str(brief["task_id"])
    _commit_brief(tmp_path, brief)
    _git(tmp_path, "update-index", index_flag, "README.md")
    readme = tmp_path / "README.md"
    readme.write_text("hidden parent change\n", encoding="utf-8")
    assert _git(tmp_path, "status", "--porcelain") == ""

    with pytest.raises(TaskError, match="clean|index|ambiguous"):
        service._ensure_worktree(task_id)

    assert readme.read_text(encoding="utf-8") == "hidden parent change\n"
    assert not (tmp_path / ".worktree" / "tasks" / task_id).exists()


def test_start_requires_brief_base_commit_to_ancestor_current_head(
    tmp_path: Path,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service, key="unrelated-base")
    task_id = str(brief["task_id"])
    tree = _git(tmp_path, "show", "-s", "--format=%T", "HEAD")
    unrelated = _git(tmp_path, "commit-tree", tree, "-m", "unrelated root")
    brief["base_commit"] = unrelated
    _rehash_brief(brief)
    brief_path = tmp_path / "tasks" / task_id / "brief.json"
    status_path = tmp_path / ".aros" / "tasks" / task_id / "status.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    status["brief_sha256"] = brief["brief_sha256"]
    index_path = next((tmp_path / ".aros" / "tasks" / "idempotency").iterdir())
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["brief_sha256"] = brief["brief_sha256"]
    atomic_write_json(brief_path, brief)
    atomic_write_json(status_path, status)
    atomic_write_json(index_path, index)
    _commit_brief(tmp_path, brief)

    with pytest.raises(TaskError, match="ancestor|base commit"):
        service._ensure_worktree(task_id)

    assert not (tmp_path / ".worktree" / "tasks" / task_id).exists()


def test_start_ignores_git_replacement_refs_for_base_and_checkout_bytes(
    tmp_path: Path,
) -> None:
    base_commit = _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service, key="replacement-ref")
    task_id = str(brief["task_id"])
    _commit_brief(tmp_path, brief)
    builder = tmp_path / ".worktree" / "replacement-builder"
    _git(
        tmp_path,
        "worktree",
        "add",
        "-q",
        "-b",
        "replacement-builder",
        str(builder),
        f"{base_commit}^",
    )
    (builder / "README.md").write_text("replacement bytes\n", encoding="utf-8")
    _git(builder, "add", "README.md")
    _git(builder, "commit", "-qm", "replacement commit")
    replacement = _git(builder, "rev-parse", "HEAD")
    _git(tmp_path, "replace", base_commit, replacement)
    exact = subprocess.run(
        [
            "git",
            "--no-replace-objects",
            "-C",
            str(tmp_path),
            "show",
            f"{base_commit}:README.md",
        ],
        check=True,
        capture_output=True,
    ).stdout

    service._ensure_worktree(task_id)

    checkout = tmp_path / ".worktree" / "tasks" / task_id
    assert (checkout / "README.md").read_bytes() == exact
    assert (checkout / "README.md").read_bytes() != b"replacement bytes\n"


@pytest.mark.parametrize("kind", ("directory", "file", "symlink", "broken_symlink"))
def test_start_rejects_and_preserves_a_preexisting_target_path(
    tmp_path: Path,
    kind: str,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service, key=f"target-conflict-{kind}")
    task_id = str(brief["task_id"])
    _commit_brief(tmp_path, brief)
    target = tmp_path / ".worktree" / "tasks" / task_id
    target.parent.mkdir(parents=True)
    link_target = tmp_path / "preserved-link-target"
    if kind == "directory":
        target.mkdir()
        (target / "preserve.txt").write_text("directory\n", encoding="utf-8")
    elif kind == "file":
        target.write_text("file\n", encoding="utf-8")
    elif kind == "symlink":
        link_target.mkdir()
        target.symlink_to(link_target, target_is_directory=True)
    else:
        target.symlink_to(link_target, target_is_directory=True)

    with pytest.raises(TaskError, match="target|worktree|symlink|conflict"):
        service._ensure_worktree(task_id)

    assert target.lstat()
    if kind == "directory":
        assert (target / "preserve.txt").read_text(encoding="utf-8") == "directory\n"
    elif kind == "file":
        assert target.read_text(encoding="utf-8") == "file\n"
    elif kind == "symlink":
        assert target.is_symlink() and link_target.is_dir()
    else:
        assert target.is_symlink() and not link_target.exists()


@pytest.mark.parametrize("relative", (".worktree", ".worktree/tasks"))
def test_start_rejects_a_symlinked_worktree_root_without_following_it(
    tmp_path: Path,
    relative: str,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service, key=f"symlink-root-{relative}")
    task_id = str(brief["task_id"])
    _commit_brief(tmp_path, brief)
    alias = tmp_path / relative
    alias.parent.mkdir(parents=True, exist_ok=True)
    if alias.is_dir():
        alias.rmdir()
    outside = tmp_path / "outside-worktrees"
    outside.mkdir()
    alias.symlink_to(outside, target_is_directory=True)

    with pytest.raises(TaskError, match="plain directory|symlink|contain"):
        service._ensure_worktree(task_id)

    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("conflict", ("exact", "prefix", "descendant", "checked_out"))
def test_start_rejects_and_preserves_conflicting_task_branch_refs(
    tmp_path: Path,
    conflict: str,
) -> None:
    base_commit = _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service, key=f"branch-conflict-{conflict}")
    task_id = str(brief["task_id"])
    _commit_brief(tmp_path, brief)
    branch = f"aros/task/{task_id}"
    if conflict == "exact":
        existing = branch
        _git(tmp_path, "branch", existing, base_commit)
    elif conflict == "prefix":
        existing = "aros/task"
        _git(tmp_path, "branch", existing, base_commit)
    elif conflict == "descendant":
        existing = f"{branch}/nested"
        _git(tmp_path, "branch", existing, base_commit)
    else:
        existing = branch
        other = tmp_path / ".worktree" / "foreign-task-branch"
        other.parent.mkdir(exist_ok=True)
        _git(tmp_path, "worktree", "add", "-q", "-b", branch, str(other), base_commit)
        (other / "preserve.txt").write_text("dirty\n", encoding="utf-8")

    with pytest.raises(TaskError, match="branch|ref|checked out|conflict"):
        service._ensure_worktree(task_id)

    assert _git_ref_exists(tmp_path, f"refs/heads/{existing}")
    if conflict == "checked_out":
        assert (other / "preserve.txt").read_text(encoding="utf-8") == "dirty\n"


def test_start_rejects_a_target_registered_to_another_worktree(
    tmp_path: Path,
) -> None:
    base_commit = _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service, key="registered-target")
    task_id = str(brief["task_id"])
    _commit_brief(tmp_path, brief)
    target = tmp_path / ".worktree" / "tasks" / task_id
    target.parent.mkdir(parents=True)
    _git(tmp_path, "worktree", "add", "-q", "--detach", str(target), base_commit)
    preserve = target / "preserve.txt"
    preserve.write_text("registered and dirty\n", encoding="utf-8")

    with pytest.raises(TaskError, match="registered|target|worktree|conflict"):
        service._ensure_worktree(task_id)

    assert preserve.read_text(encoding="utf-8") == "registered and dirty\n"
    assert str(target) in _git(tmp_path, "worktree", "list", "--porcelain")


def test_start_rejects_and_never_prunes_a_stale_worktree_registration(
    tmp_path: Path,
) -> None:
    base_commit = _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service, key="stale-registration")
    task_id = str(brief["task_id"])
    _commit_brief(tmp_path, brief)
    target = tmp_path / ".worktree" / "tasks" / task_id
    target.parent.mkdir(parents=True)
    _git(tmp_path, "worktree", "add", "-q", "--detach", str(target), base_commit)
    preserved = tmp_path / ".worktree" / "preserved-stale-task"
    target.rename(preserved)
    before = _git(
        tmp_path,
        "worktree",
        "list",
        "--porcelain",
        "--expire=now",
    )
    assert str(target) in before and "prunable" in before

    with pytest.raises(TaskError, match="stale|prunable|registered|worktree"):
        service._ensure_worktree(task_id)

    after = _git(
        tmp_path,
        "worktree",
        "list",
        "--porcelain",
        "--expire=now",
    )
    assert after == before
    assert preserved.is_dir()


def test_git_subprocesses_do_not_load_ambient_dynamic_libraries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compiler = Path("/usr/bin/cc")
    if not compiler.is_file():
        pytest.skip("a C compiler is required for the LD_PRELOAD regression")
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service, key="malicious-dynamic-loader")
    task_id = str(brief["task_id"])
    _commit_brief(tmp_path, brief)
    marker = tmp_path / ".git" / "preload-ran"
    source = tmp_path / ".git" / "preload.c"
    library = tmp_path / ".git" / "preload.so"
    source.write_text(
        "#define _GNU_SOURCE\n"
        "#include <fcntl.h>\n"
        "#include <link.h>\n"
        "#include <unistd.h>\n"
        "unsigned int la_version(unsigned int version) { return LAV_CURRENT; }\n"
        "__attribute__((constructor)) static void mark(void) {\n"
        f"  int fd = open({json.dumps(str(marker))}, O_WRONLY | O_CREAT, 0600);\n"
        "  if (fd >= 0) close(fd);\n"
        "}\n",
        encoding="utf-8",
    )
    subprocess.run(
        [str(compiler), "-shared", "-fPIC", "-o", str(library), str(source)],
        check=True,
        capture_output=True,
    )
    monkeypatch.setenv("LD_PRELOAD", str(library))
    monkeypatch.setenv("LD_LIBRARY_PATH", str(library.parent))
    monkeypatch.setenv("LD_AUDIT", str(library))
    monkeypatch.setenv("DYLD_INSERT_LIBRARIES", str(library))

    status = service._ensure_worktree(task_id)

    assert status["state"] == "worktree_ready"
    assert not marker.exists()


def test_concurrent_and_repeated_start_reuses_one_owned_worktree(
    tmp_path: Path,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service, key="concurrent-start")
    task_id = str(brief["task_id"])
    _commit_brief(tmp_path, brief)

    with ThreadPoolExecutor(max_workers=4) as pool:
        statuses = list(pool.map(lambda _index: service._ensure_worktree(task_id), range(4)))

    assert all(status == statuses[0] for status in statuses)
    ownership = json.loads(
        (tmp_path / ".aros" / "tasks" / task_id / "ownership.json").read_text(
            encoding="utf-8"
        )
    )
    assert ownership["actor"] == brief["actor"]
    registry = _git(tmp_path, "worktree", "list", "--porcelain")
    assert registry.count(str(tmp_path / ".worktree" / "tasks" / task_id)) == 1
    assert registry.count(f"branch refs/heads/aros/task/{task_id}") == 1


def test_repeated_start_and_ready_readers_preserve_dirty_advanced_worktree(
    tmp_path: Path,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service, key="repeat-dirty-advanced")
    task_id = str(brief["task_id"])
    _commit_brief(tmp_path, brief)
    first = service._ensure_worktree(task_id)
    worktree = tmp_path / ".worktree" / "tasks" / task_id
    (worktree / "README.md").write_text("advanced child\n", encoding="utf-8")
    _git(worktree, "add", "README.md")
    _git(worktree, "commit", "-qm", "advance child")
    advanced = _git(worktree, "rev-parse", "HEAD")
    untracked = worktree / "untracked-child.txt"
    untracked.write_text("preserve child dirt\n", encoding="utf-8")

    second = service._ensure_worktree(task_id)

    assert second == first
    assert service.status(task_id) == first
    assert service.list() == [first]
    assert _git(worktree, "rev-parse", "HEAD") == advanced
    assert untracked.read_text(encoding="utf-8") == "preserve child dirt\n"
    registry = _git(tmp_path, "worktree", "list", "--porcelain")
    assert registry.count(str(worktree)) == 1


@pytest.mark.parametrize("status_state", ("missing", "prepared"))
def test_ready_status_recovers_from_valid_create_once_ownership(
    tmp_path: Path,
    status_state: str,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service, key=f"ready-recovery-{status_state}")
    task_id = str(brief["task_id"])
    _commit_brief(tmp_path, brief)
    ready = service._ensure_worktree(task_id)
    status_path = tmp_path / ".aros" / "tasks" / task_id / "status.json"
    if status_state == "missing":
        status_path.unlink()
    else:
        atomic_write_json(
            status_path,
            {
                "schema_version": 1,
                "task_id": task_id,
                "state": "prepared",
                "brief_sha256": brief["brief_sha256"],
                "updated_at": brief["created_at"],
            },
        )

    assert TaskService(tmp_path).status(task_id) == ready
    assert json.loads(status_path.read_text(encoding="utf-8")) == ready


@pytest.mark.parametrize("reader", ("status", "list", "start"))
def test_ready_task_fails_closed_if_create_once_ownership_is_missing(
    tmp_path: Path,
    reader: str,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service, key=f"missing-ownership-{reader}")
    task_id = str(brief["task_id"])
    _commit_brief(tmp_path, brief)
    service._ensure_worktree(task_id)
    worktree = tmp_path / ".worktree" / "tasks" / task_id
    preserve = worktree / "preserve.txt"
    preserve.write_text("owned work\n", encoding="utf-8")
    (tmp_path / ".aros" / "tasks" / task_id / "ownership.json").unlink()

    with pytest.raises(TaskError, match="ownership"):
        if reader == "status":
            service.status(task_id)
        elif reader == "list":
            service.list()
        else:
            service._ensure_worktree(task_id)

    assert preserve.read_text(encoding="utf-8") == "owned work\n"


@pytest.mark.parametrize("reader", ("status", "list", "start"))
def test_owned_task_fails_closed_on_tamper_without_touching_child_work(
    tmp_path: Path,
    reader: str,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service, key=f"tampered-ownership-{reader}")
    task_id = str(brief["task_id"])
    _commit_brief(tmp_path, brief)
    service._ensure_worktree(task_id)
    worktree = tmp_path / ".worktree" / "tasks" / task_id
    preserve = worktree / "preserve-tamper.txt"
    preserve.write_text("never remove\n", encoding="utf-8")
    ownership_path = tmp_path / ".aros" / "tasks" / task_id / "ownership.json"
    ownership = json.loads(ownership_path.read_text(encoding="utf-8"))
    ownership["actor"] = "tampered"
    atomic_write_json(ownership_path, ownership)

    with pytest.raises(TaskError, match="ownership"):
        if reader == "status":
            service.status(task_id)
        elif reader == "list":
            service.list()
        else:
            service._ensure_worktree(task_id)

    assert preserve.read_text(encoding="utf-8") == "never remove\n"


def test_owned_task_rejects_a_misregistered_worktree_without_cleanup(
    tmp_path: Path,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service, key="misregistered-owned-worktree")
    task_id = str(brief["task_id"])
    _commit_brief(tmp_path, brief)
    service._ensure_worktree(task_id)
    worktree = tmp_path / ".worktree" / "tasks" / task_id
    moved = tmp_path / ".worktree" / "tasks" / f"{task_id}-moved"
    _git(tmp_path, "worktree", "move", str(worktree), str(moved))
    preserve = moved / "preserve-after-move.txt"
    preserve.write_text("misregistered\n", encoding="utf-8")

    with pytest.raises(TaskError, match="ownership|registered|worktree|path"):
        service.status(task_id)

    assert preserve.read_text(encoding="utf-8") == "misregistered\n"
    assert moved.is_dir()


def test_partial_start_without_ownership_is_preserved_and_never_adopted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service, key="partial-before-ownership")
    task_id = str(brief["task_id"])
    _commit_brief(tmp_path, brief)
    original_create_json = tasks_module.create_json

    class InjectedInterruption(RuntimeError):
        pass

    def interrupt_ownership(path: str | Path, value: object) -> bool:
        if Path(path).name == "ownership.json":
            raise InjectedInterruption
        return original_create_json(path, value)

    monkeypatch.setattr(tasks_module, "create_json", interrupt_ownership)
    with pytest.raises(InjectedInterruption):
        service._ensure_worktree(task_id)
    monkeypatch.setattr(tasks_module, "create_json", original_create_json)
    worktree = tmp_path / ".worktree" / "tasks" / task_id
    preserve = worktree / "partial-work.txt"
    preserve.write_text("partial\n", encoding="utf-8")
    assert not (tmp_path / ".aros" / "tasks" / task_id / "ownership.json").exists()

    with pytest.raises(TaskError, match="unowned|branch|worktree|conflict"):
        service._ensure_worktree(task_id)
    with pytest.raises(TaskError, match="unowned|branch|worktree|ownership"):
        service.status(task_id)

    assert preserve.read_text(encoding="utf-8") == "partial\n"


def test_partial_start_with_valid_ownership_recovers_worktree_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service, key="partial-after-ownership")
    task_id = str(brief["task_id"])
    _commit_brief(tmp_path, brief)
    original_create_json = tasks_module.create_json

    class InjectedInterruption(RuntimeError):
        pass

    def interrupt_after_ownership(path: str | Path, value: object) -> bool:
        created = original_create_json(path, value)
        if Path(path).name == "ownership.json":
            raise InjectedInterruption
        return created

    monkeypatch.setattr(tasks_module, "create_json", interrupt_after_ownership)
    with pytest.raises(InjectedInterruption):
        service._ensure_worktree(task_id)
    monkeypatch.setattr(tasks_module, "create_json", original_create_json)

    ready = TaskService(tmp_path).status(task_id)

    ownership = json.loads(
        (tmp_path / ".aros" / "tasks" / task_id / "ownership.json").read_text(
            encoding="utf-8"
        )
    )
    assert ready["state"] == "worktree_ready"
    assert ready["ownership_sha256"] == ownership["ownership_sha256"]


@pytest.mark.parametrize(
    "mutation",
    ("commit", "untracked", "staged", "ignored", "mode"),
)
def test_start_never_promotes_a_racy_new_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    base_commit = _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service, key=f"new-checkout-race-{mutation}")
    task_id = str(brief["task_id"])
    _commit_brief(tmp_path, brief)
    worktree = tmp_path / ".worktree" / "tasks" / task_id
    original_create_json = tasks_module.create_json
    injected = False

    def mutate_before_ownership(path: str | Path, value: object) -> bool:
        nonlocal injected
        if Path(path).name == "ownership.json" and not injected:
            injected = True
            if mutation == "commit":
                (worktree / "README.md").write_text(
                    "racy committed child\n",
                    encoding="utf-8",
                )
                _git(worktree, "add", "README.md")
                _git(worktree, "commit", "-qm", "racy child commit")
            elif mutation == "untracked":
                (worktree / "race-untracked.txt").write_text(
                    "preserve\n",
                    encoding="utf-8",
                )
            elif mutation == "staged":
                (worktree / "README.md").write_text(
                    "racy staged child\n",
                    encoding="utf-8",
                )
                _git(worktree, "add", "README.md")
            elif mutation == "ignored":
                ignored = worktree / ".worktree" / "race-ignored.txt"
                ignored.parent.mkdir()
                ignored.write_text("preserve\n", encoding="utf-8")
            elif mutation == "mode":
                (worktree / "README.md").chmod(0o755)
        return original_create_json(path, value)

    monkeypatch.setattr(tasks_module, "create_json", mutate_before_ownership)

    with pytest.raises(TaskError, match="checkout|base|clean|mode|index"):
        service._ensure_worktree(task_id)

    monkeypatch.setattr(tasks_module, "create_json", original_create_json)
    assert injected
    ownership_path = tmp_path / ".aros" / "tasks" / task_id / "ownership.json"
    assert ownership_path.is_file()
    status = json.loads(
        (tmp_path / ".aros" / "tasks" / task_id / "status.json").read_text(
            encoding="utf-8"
        )
    )
    assert status["state"] == "prepared"
    with pytest.raises(TaskError, match="checkout|base|clean|mode|index"):
        service.status(task_id)
    if mutation == "commit":
        assert _git(worktree, "rev-parse", "HEAD") != base_commit
    elif mutation == "untracked":
        assert (worktree / "race-untracked.txt").is_file()
    elif mutation == "staged":
        assert _git(worktree, "diff", "--cached", "--name-only") == "README.md"
    elif mutation == "ignored":
        assert (worktree / ".worktree" / "race-ignored.txt").is_file()
    elif mutation == "mode":
        assert (worktree / "README.md").stat().st_mode & 0o111


def test_start_rejects_racy_mode_when_repository_disables_filemode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_workspace(tmp_path)
    _git(tmp_path, "config", "core.fileMode", "false")
    service = TaskService(tmp_path)
    brief = _create(service, key="new-checkout-race-disabled-filemode")
    task_id = str(brief["task_id"])
    _commit_brief(tmp_path, brief)
    worktree = tmp_path / ".worktree" / "tasks" / task_id
    original_create_json = tasks_module.create_json
    injected = False

    def mutate_before_ownership(path: str | Path, value: object) -> bool:
        nonlocal injected
        if Path(path).name == "ownership.json" and not injected:
            injected = True
            (worktree / "README.md").chmod(0o755)
        return original_create_json(path, value)

    monkeypatch.setattr(tasks_module, "create_json", mutate_before_ownership)

    with pytest.raises(TaskError, match="checkout|clean|mode"):
        service._ensure_worktree(task_id)

    assert injected
    assert (worktree / "README.md").stat().st_mode & 0o111
    assert (tmp_path / ".aros" / "tasks" / task_id / "ownership.json").is_file()


@pytest.mark.parametrize("status_state", ("missing", "prepared"))
def test_ownership_recovery_rejects_a_dirty_partial_checkout(
    tmp_path: Path,
    status_state: str,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service, key=f"dirty-ownership-recovery-{status_state}")
    task_id = str(brief["task_id"])
    _commit_brief(tmp_path, brief)
    service._ensure_worktree(task_id)
    worktree = tmp_path / ".worktree" / "tasks" / task_id
    dirty = worktree / "partial-untracked.txt"
    dirty.write_text("preserve\n", encoding="utf-8")
    status_path = tmp_path / ".aros" / "tasks" / task_id / "status.json"
    if status_state == "missing":
        status_path.unlink()
    else:
        atomic_write_json(
            status_path,
            {
                "schema_version": 1,
                "task_id": task_id,
                "state": "prepared",
                "brief_sha256": brief["brief_sha256"],
                "updated_at": brief["created_at"],
            },
        )

    with pytest.raises(TaskError, match="checkout|clean"):
        TaskService(tmp_path).status(task_id)

    assert dirty.read_text(encoding="utf-8") == "preserve\n"


def test_parent_head_race_preserves_unowned_partial_start_and_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service, key="parent-head-race")
    task_id = str(brief["task_id"])
    original_head = _commit_brief(tmp_path, brief)
    original_run = tasks_module.subprocess.run
    raced = False

    def move_head_before_worktree_add(*args: object, **kwargs: object) -> object:
        nonlocal raced
        command = args[0]
        if (
            not raced
            and isinstance(command, list)
            and "worktree" in command
            and "add" in command
        ):
            raced = True
            original_run(
                [
                    "git",
                    "-C",
                    str(tmp_path),
                    "commit",
                    "--allow-empty",
                    "-qm",
                    "race parent HEAD",
                ],
                check=True,
            )
        return original_run(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(tasks_module.subprocess, "run", move_head_before_worktree_add)

    with pytest.raises(TaskError, match="HEAD.*changed|stable"):
        service._ensure_worktree(task_id)

    assert raced
    assert _git(tmp_path, "rev-parse", "HEAD") != original_head
    worktree = tmp_path / ".worktree" / "tasks" / task_id
    assert worktree.is_dir()
    assert not (tmp_path / ".aros" / "tasks" / task_id / "ownership.json").exists()
    with pytest.raises(TaskError, match="unowned|branch|worktree|conflict"):
        service._ensure_worktree(task_id)
    assert worktree.is_dir()


def test_start_disables_real_post_checkout_and_filter_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_workspace(tmp_path)
    payload = tmp_path / "payload.txt"
    payload.write_text("repository bytes\n", encoding="utf-8")
    (tmp_path / ".gitattributes").write_text(
        "payload.txt filter=malicious\n",
        encoding="utf-8",
    )
    _git(tmp_path, "add", ".gitattributes", "payload.txt")
    _git(tmp_path, "commit", "-qm", "add filtered payload")
    base_commit = _git(tmp_path, "rev-parse", "HEAD")
    service = TaskService(tmp_path)
    brief = _create(service, key="malicious-checkout-config")
    task_id = str(brief["task_id"])
    _commit_brief(tmp_path, brief)
    markers = {
        name: tmp_path / f"{name}-ran"
        for name in ("hook", "clean", "smudge", "process", "ambient")
    }
    hooks = tmp_path / ".git" / "malicious-hooks"
    hooks.mkdir()
    post_checkout = hooks / "post-checkout"
    post_checkout.write_text(
        f"#!/bin/sh\ntouch {shlex.quote(str(markers['hook']))}\n",
        encoding="utf-8",
    )
    post_checkout.chmod(0o755)
    _git(tmp_path, "config", "core.hooksPath", str(hooks))
    for kind in ("clean", "smudge"):
        command = (
            f"sh -c 'touch {shlex.quote(str(markers[kind]))}; cat'"
        )
        _git(tmp_path, "config", f"filter.malicious.{kind}", command)
    _git(
        tmp_path,
        "config",
        "filter.malicious.process",
        f"sh -c 'touch {shlex.quote(str(markers['process']))}; exit 1'",
    )
    _git(tmp_path, "config", "filter.malicious.required", "true")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(hooks))

    status = service._ensure_worktree(task_id)

    checkout = tmp_path / ".worktree" / "tasks" / task_id
    assert status["state"] == "worktree_ready"
    assert (checkout / "payload.txt").read_bytes() == b"repository bytes\n"
    assert _git(checkout, "rev-parse", "HEAD") == base_commit
    assert all(not marker.exists() for marker in markers.values())


def test_start_accepts_clean_git_native_eol_checkout(tmp_path: Path) -> None:
    _init_workspace(tmp_path)
    tracked = tmp_path / "native-eol.txt"
    tracked.write_bytes(b"line-one\nline-two\n")
    (tmp_path / ".gitattributes").write_text(
        "native-eol.txt text eol=crlf\n",
        encoding="utf-8",
    )
    _git(tmp_path, "add", ".gitattributes", "native-eol.txt")
    _git(tmp_path, "commit", "-qm", "add Git-native EOL checkout")
    service = TaskService(tmp_path)
    brief = _create(service, key="git-native-eol")
    task_id = str(brief["task_id"])
    _commit_brief(tmp_path, brief)

    status = service._ensure_worktree(task_id)

    child = tmp_path / ".worktree" / "tasks" / task_id
    checked_out = child / "native-eol.txt"
    assert status["state"] == "worktree_ready"
    assert checked_out.read_bytes() == b"line-one\r\nline-two\r\n"
    assert _git(child, "status", "--porcelain=v1", "--untracked-files=all") == ""


def test_start_git_commands_are_scrubbed_pinned_and_nondestructive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service, key="git-command-boundary")
    task_id = str(brief["task_id"])
    _commit_brief(tmp_path, brief)
    calls: list[tuple[list[str], dict[str, str]]] = []
    original_run = tasks_module.subprocess.run

    def record_git(*args: object, **kwargs: object) -> object:
        command = args[0]
        environment = kwargs.get("env")
        if isinstance(command, list) and "git" in command:
            assert isinstance(environment, dict)
            calls.append((command[command.index("git") :], environment))
        return original_run(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setenv("GIT_DIR", str(tmp_path / "foreign.git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "foreign-worktree"))
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "foreign-pythonpath"))
    monkeypatch.setenv("PYTHONWARNINGS", "error")
    monkeypatch.setattr(tasks_module.subprocess, "run", record_git)

    service._ensure_worktree(task_id)

    assert calls
    assert all(not any(key.startswith("GIT_") for key in env) for _, env in calls)
    assert all(not any(key.startswith("PYTHON") for key in env) for _, env in calls)
    assert all(
        any(str(service._git_dir) in argument for argument in command)
        for command, _ in calls
    )
    worktree_add = [
        command
        for command, _ in calls
        if "worktree" in command and "add" in command
    ]
    assert len(worktree_add) == 1
    worktree_add_argv = worktree_add[0]
    expected_target = str(tmp_path / ".worktree" / "tasks" / task_id)
    assert expected_target in worktree_add_argv
    assert "--force" not in worktree_add_argv
    assert "prune" not in worktree_add_argv
    assert "core.hooksPath=/dev/null" in worktree_add_argv
    forbidden = {"--force", "reset", "clean", "prune", "remove"}
    assert all(not forbidden.intersection(command) for command, _ in calls)


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


def test_git_probes_ignore_foreign_ambient_repository_and_config_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    foreign = tmp_path / "foreign"
    workspace.mkdir()
    foreign.mkdir()
    workspace_head = _init_workspace(workspace)
    _init_workspace(foreign)
    (foreign / "foreign.txt").write_text("foreign head\n", encoding="utf-8")
    _git(foreign, "add", "foreign.txt")
    _git(foreign, "commit", "-qm", "distinct foreign head")
    assert _git(foreign, "rev-parse", "HEAD") != workspace_head
    monkeypatch.setenv("GIT_DIR", str(foreign / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(foreign))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.worktree")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(foreign))

    service = TaskService(workspace)
    brief = _create(service, key="ambient-git-overrides")

    assert brief["base_commit"] == workspace_head


def test_task_service_git_binding_does_not_pin_inode_identity() -> None:
    source = inspect.getsource(TaskService)

    assert not {
        name
        for name in (
            "_git_dir_identity",
            "_git_common_dir_identity",
            "_require_pinned_git_identity",
        )
        if name in source
    }


def test_create_rejects_a_changed_git_directory_association(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    foreign = tmp_path / "foreign"
    workspace.mkdir()
    foreign.mkdir()
    _init_workspace(workspace)
    _init_workspace(foreign)
    service = TaskService(workspace)
    (workspace / ".git").rename(workspace / ".git-original")
    (workspace / ".git").write_text(
        f"gitdir: {(foreign / '.git').resolve()}\n",
        encoding="utf-8",
    )

    with pytest.raises(TaskError, match="Git directory association"):
        _create(service, key="changed-git-association")


def test_linked_worktree_rejects_common_git_directory_redirection(
    tmp_path: Path,
) -> None:
    primary = tmp_path / "primary"
    linked = tmp_path / "linked"
    foreign = tmp_path / "foreign"
    primary.mkdir()
    foreign.mkdir()
    _init_workspace(primary)
    _init_workspace(foreign)
    subprocess.run(
        [
            "git",
            "-C",
            str(primary),
            "worktree",
            "add",
            "-q",
            "-b",
            "linked-test",
            str(linked),
        ],
        check=True,
    )
    (linked / ".aros").mkdir()
    service = TaskService(linked)
    git_dir = Path(_git(linked, "rev-parse", "--absolute-git-dir"))
    (git_dir / "commondir").write_text(
        f"{(foreign / '.git').resolve()}\n",
        encoding="utf-8",
    )

    with pytest.raises(TaskError, match="common Git directory"):
        service._require_git_root()


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


@pytest.mark.parametrize(
    "field",
    (
        "objective",
        "actor",
        "idempotency_key",
        "adapter_argv",
        "deliverables",
        "acceptance",
    ),
)
def test_create_rejects_lone_surrogates_in_external_strings(
    tmp_path: Path,
    field: str,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    surrogate = "\ud800"
    objective = "bounded objective"
    request = _request()
    if field == "objective":
        objective = surrogate
    elif field in {"actor", "idempotency_key"}:
        request[field] = surrogate
    elif field == "adapter_argv":
        request[field] = ["adapter", surrogate]
    else:
        request[field] = [surrogate]

    with pytest.raises(TaskError, match="UTF-8"):
        service.create(objective, **request)  # type: ignore[arg-type]

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


@pytest.mark.parametrize(
    "missing",
    ("status", "index", "both", "runtime", "runtime_and_index"),
)
def test_create_recovers_missing_prepared_records_from_the_immutable_brief(
    tmp_path: Path,
    missing: str,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    key = "recover-partial-create"
    brief = _create(service, key=key)
    task_id = str(brief["task_id"])
    runtime_path = tmp_path / ".aros" / "tasks" / task_id
    status_path = runtime_path / "status.json"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    index_path = tmp_path / ".aros" / "tasks" / "idempotency" / f"{digest}.json"
    if missing in {"status", "both", "runtime", "runtime_and_index"}:
        status_path.unlink()
    if missing in {"runtime", "runtime_and_index"}:
        runtime_path.rmdir()
    if missing in {"index", "both", "runtime_and_index"}:
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


def test_precommit_task_staging_is_ignored_and_preserved(
    tmp_path: Path,
) -> None:
    _init_workspace(tmp_path)
    interrupted = tmp_path / "tasks" / ".staging" / "interrupted-publication"
    interrupted.mkdir(parents=True)
    marker = interrupted / "brief.json"
    marker.write_text("ambiguous staged material\n", encoding="utf-8")
    service = TaskService(tmp_path)

    assert service.list() == []
    brief = _create(service, key="after-interruption")

    assert marker.read_text(encoding="utf-8") == "ambiguous staged material\n"
    assert service.list() == [service.status(str(brief["task_id"]))]


def test_empty_preauthority_task_container_is_ignored_and_preserved(
    tmp_path: Path,
) -> None:
    _init_workspace(tmp_path)
    empty = tmp_path / "tasks" / "TASK-20260802-empty-remnant"
    empty.mkdir(parents=True)
    service = TaskService(tmp_path)

    assert service.list() == []
    brief = _create(service, key="after-empty-container")

    assert empty.is_dir()
    assert list(empty.iterdir()) == []
    assert service.list() == [service.status(str(brief["task_id"]))]


def test_nonempty_preauthority_task_container_fails_closed_and_is_preserved(
    tmp_path: Path,
) -> None:
    _init_workspace(tmp_path)
    ambiguous = tmp_path / "tasks" / "TASK-20260802-ambiguous-remnant"
    ambiguous.mkdir(parents=True)
    marker = ambiguous / "unknown.bin"
    marker.write_bytes(b"preserve")
    service = TaskService(tmp_path)

    with pytest.raises(TaskError, match="ambiguous.*without.*brief"):
        service.list()
    with pytest.raises(TaskError, match="ambiguous.*without.*brief"):
        _create(service, key="blocked-by-ambiguous-container")

    assert marker.read_bytes() == b"preserve"


def test_different_key_create_recovers_a_published_brief_after_interruption(
    tmp_path: Path,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    first = _create(service, key="interrupted-first", objective="first task")
    first_id = str(first["task_id"])
    runtime_path = tmp_path / ".aros" / "tasks" / first_id
    (runtime_path / "status.json").unlink()
    runtime_path.rmdir()
    first_digest = hashlib.sha256(b"interrupted-first").hexdigest()
    (
        tmp_path / ".aros" / "tasks" / "idempotency" / f"{first_digest}.json"
    ).unlink()

    second = _create(service, key="after-interruption", objective="second task")

    assert service.status(first_id)["brief_sha256"] == first["brief_sha256"]
    assert {status["task_id"] for status in service.list()} == {
        first["task_id"],
        second["task_id"],
    }


def test_different_key_publications_serialize_without_inventory_races(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    publication_reached = Event()
    release_publication = Event()
    second_started = Event()
    reader_started = Event()
    original_create_directory = tasks_module._create_plain_directory

    def pause_legacy_visible_directory(path: Path, description: str) -> None:
        original_create_directory(path, description)
        if description == "versioned task path" and not publication_reached.is_set():
            publication_reached.set()
            assert release_publication.wait(timeout=5)

    monkeypatch.setattr(
        tasks_module,
        "_create_plain_directory",
        pause_legacy_visible_directory,
    )
    original_publish = getattr(TaskService, "_publish_staged_brief", None)
    if original_publish is not None:

        def pause_atomic_publication(
            self: TaskService,
            staging: Path,
            target: Path,
        ) -> None:
            original_publish(self, staging, target)
            if not publication_reached.is_set():
                publication_reached.set()
                assert release_publication.wait(timeout=5)

        monkeypatch.setattr(TaskService, "_publish_staged_brief", pause_atomic_publication)

    def create_second() -> dict[str, object]:
        second_started.set()
        return _create(service, key="publication-two", objective="second task")

    def read_inventory() -> list[dict[str, object]]:
        reader_started.set()
        return service.list()

    with ThreadPoolExecutor(max_workers=3) as pool:
        first_future = pool.submit(
            _create,
            service,
            key="publication-one",
            objective="first task",
        )
        assert publication_reached.wait(timeout=5)
        second_future = pool.submit(create_second)
        reader_future = pool.submit(read_inventory)
        assert second_started.wait(timeout=5)
        assert reader_started.wait(timeout=5)
        release_publication.set()
        first = first_future.result(timeout=5)
        second = second_future.result(timeout=5)
        observed = reader_future.result(timeout=5)

    final = service.list()
    task_ids = {str(first["task_id"]), str(second["task_id"])}
    assert len(task_ids) == 2
    assert {str(status["task_id"]) for status in final} == task_ids
    assert {str(status["task_id"]) for status in observed} <= task_ids


def test_publication_syncs_target_and_parent_before_staging_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    synced: list[Path] = []
    monkeypatch.setattr(tasks_module, "_fsync_directory", synced.append)

    brief = _create(service, key="sync-publication-parents")

    target = tmp_path / "tasks" / str(brief["task_id"])
    target_sync = synced.index(target)
    assert synced[target_sync : target_sync + 2] == [target, tmp_path / "tasks"]
    assert tmp_path / "tasks" / ".staging" in synced


def test_publication_link_failure_preserves_staging_and_empty_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)

    def fail_cross_device_link(
        _source: Path,
        _target: Path,
        *,
        follow_symlinks: bool,
    ) -> None:
        assert follow_symlinks is False
        raise OSError(errno.EXDEV, "cross-device link")

    monkeypatch.setattr(tasks_module.os, "link", fail_cross_device_link)

    with pytest.raises(TaskError, match="task brief publication"):
        _create(service, key="link-failure")

    staged = list((tmp_path / "tasks" / ".staging").glob("TASK-*/brief.json"))
    targets = list((tmp_path / "tasks").glob("TASK-*"))
    assert len(staged) == 1
    assert len(targets) == 1
    assert list(targets[0].iterdir()) == []
    assert service.list() == []


def test_publication_never_clobbers_a_race_created_brief(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    original_link = tasks_module.os.link

    def create_foreign_destination_then_link(
        source: Path,
        target: Path,
        *,
        follow_symlinks: bool,
    ) -> None:
        target.write_text("foreign\n", encoding="utf-8")
        original_link(source, target, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(
        tasks_module.os,
        "link",
        create_foreign_destination_then_link,
    )

    with pytest.raises(TaskError, match="task brief publication"):
        _create(service, key="link-eexist")

    targets = list((tmp_path / "tasks").glob("TASK-*"))
    assert len(targets) == 1
    assert (targets[0] / "brief.json").read_text(encoding="utf-8") == "foreign\n"
    assert list((tmp_path / "tasks" / ".staging").glob("TASK-*/brief.json"))


@pytest.mark.parametrize("reader", ("status", "list"))
def test_immediate_post_link_crash_is_recoverable_and_preserves_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reader: str,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    original_link = tasks_module.os.link

    class InjectedInterruption(RuntimeError):
        pass

    def interrupt_after_link(
        source: Path,
        target: Path,
        *,
        follow_symlinks: bool,
    ) -> None:
        original_link(source, target, follow_symlinks=follow_symlinks)
        raise InjectedInterruption

    monkeypatch.setattr(tasks_module.os, "link", interrupt_after_link)
    with pytest.raises(InjectedInterruption):
        _create(service, key=f"immediate-link-crash-{reader}")
    monkeypatch.setattr(tasks_module.os, "link", original_link)
    targets = list((tmp_path / "tasks").glob("TASK-*"))
    staged = list((tmp_path / "tasks" / ".staging").glob("TASK-*/brief.json"))
    assert len(targets) == len(staged) == 1
    task_id = targets[0].name

    fresh = TaskService(tmp_path)
    result = fresh.status(task_id) if reader == "status" else fresh.list()

    if reader == "status":
        assert result["task_id"] == task_id  # type: ignore[index]
    else:
        assert [status["task_id"] for status in result] == [task_id]  # type: ignore[union-attr]
    assert not staged[0].exists()
    assert (targets[0] / "brief.json").stat().st_nlink == 1


@pytest.mark.parametrize("kind", ("different_inode", "symlink"))
def test_reconciliation_rejects_and_preserves_ambiguous_staging_brief(
    tmp_path: Path,
    kind: str,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service, key=f"ambiguous-staging-{kind}")
    task_id = str(brief["task_id"])
    authoritative = tmp_path / "tasks" / task_id / "brief.json"
    staging = tmp_path / "tasks" / ".staging" / task_id
    staging.mkdir()
    staged_brief = staging / "brief.json"
    if kind == "different_inode":
        staged_brief.write_text("{}\n", encoding="utf-8")
    else:
        staged_brief.symlink_to(authoritative)

    with pytest.raises(TaskError, match="ambiguous task staging"):
        service.status(task_id)

    assert staged_brief.exists()
    assert authoritative.is_file()


def test_reconciliation_unlinks_proven_alias_but_preserves_extra_staging_material(
    tmp_path: Path,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service, key="staging-alias-with-extra")
    task_id = str(brief["task_id"])
    authoritative = tmp_path / "tasks" / task_id / "brief.json"
    staging = tmp_path / "tasks" / ".staging" / task_id
    staging.mkdir()
    staged_brief = staging / "brief.json"
    os.link(authoritative, staged_brief, follow_symlinks=False)
    extra = staging / "unexpected.txt"
    extra.write_text("preserve\n", encoding="utf-8")

    with pytest.raises(TaskError, match="ambiguous material"):
        service.list()

    assert not staged_brief.exists()
    assert extra.read_text(encoding="utf-8") == "preserve\n"
    assert authoritative.stat().st_nlink == 1


def test_reconciliation_removes_an_empty_staging_cleanup_remnant(
    tmp_path: Path,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service, key="empty-staging-cleanup-remnant")
    task_id = str(brief["task_id"])
    staging = tmp_path / "tasks" / ".staging" / task_id
    staging.mkdir()

    assert service.status(task_id)["task_id"] == task_id

    assert not staging.exists()


def test_task_publication_has_no_linux_specific_rename_helper() -> None:
    assert not hasattr(tasks_module, "_rename_noreplace")


def test_first_create_durably_syncs_record_roots_and_lock_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    synced_directories: list[Path] = []
    synced_files: list[Path] = []
    original_fsync = tasks_module.os.fsync

    def record_file_sync(descriptor: int) -> None:
        try:
            synced_files.append(Path(f"/proc/self/fd/{descriptor}").resolve())
        except OSError:
            pass
        original_fsync(descriptor)

    monkeypatch.setattr(tasks_module, "_fsync_directory", synced_directories.append)
    monkeypatch.setattr(tasks_module.os, "fsync", record_file_sync)

    _create(service, key="durable-first-create")

    required_directories = {
        tmp_path,
        tmp_path / ".aros",
        tmp_path / "tasks",
        tmp_path / ".aros" / "tasks",
        tmp_path / ".aros" / "locks",
    }
    assert required_directories <= set(synced_directories)
    lock_files = [path for path in synced_files if path.parent.name == "locks"]
    assert any(path.name.startswith("task-idempotency-") for path in lock_files)
    assert any(path.name == "task-record-publication.lock" for path in lock_files)
    for lock_file in (tmp_path / ".aros" / "locks").iterdir():
        assert lock_file.stat().st_mode & 0o777 == 0o600


def test_create_restricts_existing_plain_lock_files_to_mode_0600(
    tmp_path: Path,
) -> None:
    _init_workspace(tmp_path)
    key = "restrict-existing-locks"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    locks_root = tmp_path / ".aros" / "locks"
    locks_root.mkdir()
    lock_paths = (
        locks_root / f"task-idempotency-{digest}.lock",
        locks_root / "task-record-publication.lock",
    )
    for lock_path in lock_paths:
        lock_path.write_bytes(b"")
        lock_path.chmod(0o666)
    service = TaskService(tmp_path)

    _create(service, key=key)

    assert all(path.stat().st_mode & 0o777 == 0o600 for path in lock_paths)


def test_create_rejects_a_hardlinked_lock_before_changing_its_mode(
    tmp_path: Path,
) -> None:
    _init_workspace(tmp_path)
    locks_root = tmp_path / ".aros" / "locks"
    locks_root.mkdir()
    outside = tmp_path / ".git" / "outside-lock"
    outside.write_text("preserve\n", encoding="utf-8")
    outside.chmod(0o640)
    lock = locks_root / "task-record-publication.lock"
    os.link(outside, lock)
    mode_before = outside.stat().st_mode & 0o777
    service = TaskService(tmp_path)

    with pytest.raises(TaskError, match="hardlink|link count"):
        _create(service, key="hardlinked-publication-lock")

    assert outside.read_text(encoding="utf-8") == "preserve\n"
    assert outside.stat().st_nlink == lock.stat().st_nlink == 2
    assert outside.stat().st_mode & 0o777 == mode_before
    assert lock.stat().st_mode & 0o777 == mode_before


@pytest.mark.parametrize("reader", ("status", "list"))
def test_fresh_read_durably_recreates_missing_derived_roots_and_publication_lock(
    tmp_path: Path,
    reader: str,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service, key=f"missing-derived-roots-{reader}")
    task_id = str(brief["task_id"])
    runtime_root = tmp_path / ".aros" / "tasks"
    runtime_task = runtime_root / task_id
    (runtime_task / "status.json").unlink()
    runtime_task.rmdir()
    for index in (runtime_root / "idempotency").iterdir():
        index.unlink()
    (runtime_root / "idempotency").rmdir()
    runtime_root.rmdir()
    locks_root = tmp_path / ".aros" / "locks"
    for lock in locks_root.iterdir():
        lock.unlink()
    locks_root.rmdir()

    fresh = TaskService(tmp_path)
    result = fresh.status(task_id) if reader == "status" else fresh.list()

    if reader == "status":
        assert result["task_id"] == task_id  # type: ignore[index]
    else:
        assert [status["task_id"] for status in result] == [task_id]  # type: ignore[union-attr]
    publication_lock = locks_root / "task-record-publication.lock"
    assert publication_lock.is_file()
    assert publication_lock.stat().st_mode & 0o777 == 0o600
    assert (runtime_root / task_id / "status.json").is_file()


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


def test_status_normalizes_a_lone_surrogate_in_a_tampered_brief(
    tmp_path: Path,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _create(service)
    task_id = str(brief["task_id"])
    brief_path = tmp_path / "tasks" / task_id / "brief.json"
    brief["objective"] = "\ud800"
    brief_path.write_text(
        json.dumps(brief, ensure_ascii=True, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(TaskError, match="UTF-8"):
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

    if problem == "missing":
        result = service.status(task_id) if reader == "status" else service.list()
        if reader == "status":
            assert result["task_id"] == task_id  # type: ignore[index]
        else:
            assert [status["task_id"] for status in result] == [task_id]  # type: ignore[union-attr]
    else:
        with pytest.raises(TaskError, match="idempotency index"):
            service.status(task_id) if reader == "status" else service.list()


@pytest.mark.parametrize("reader", ("status", "list"))
def test_fresh_read_recovers_immediately_after_brief_authority_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reader: str,
) -> None:
    _init_workspace(tmp_path)
    service = TaskService(tmp_path)
    original_publish = TaskService._publish_staged_brief

    class InjectedInterruption(RuntimeError):
        pass

    def interrupt_after_publication(
        self: TaskService,
        staging: Path,
        target: Path,
    ) -> None:
        original_publish(self, staging, target)
        raise InjectedInterruption

    monkeypatch.setattr(
        TaskService,
        "_publish_staged_brief",
        interrupt_after_publication,
    )
    with pytest.raises(InjectedInterruption):
        _create(service, key=f"crash-before-derived-{reader}")
    monkeypatch.setattr(TaskService, "_publish_staged_brief", original_publish)
    task_directories = sorted((tmp_path / "tasks").glob("TASK-*"))
    assert len(task_directories) == 1
    task_id = task_directories[0].name
    assert not (tmp_path / ".aros" / "tasks" / task_id).exists()

    fresh = TaskService(tmp_path)
    result = fresh.status(task_id) if reader == "status" else fresh.list()

    if reader == "status":
        assert result["task_id"] == task_id  # type: ignore[index]
    else:
        assert [status["task_id"] for status in result] == [task_id]  # type: ignore[union-attr]


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
