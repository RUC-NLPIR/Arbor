"""Project one Task execution onto the shared durable Run service."""

from __future__ import annotations

import math
import os
import re
import stat
import sys
from collections.abc import Callable
from contextlib import ExitStack, contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from .checkpoint import CheckpointError
from .runs import RunError, RunService, read_validated_run_manifest
from .store import AnchoredWorkspaceReader, create_json, file_lock, json_sha256, utc_now


_TASK_ID = re.compile(
    r"^TASK-[0-9]{8}-[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?$"
)
_RUN_ID = re.compile(r"^RUN-[A-Za-z0-9][A-Za-z0-9-]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_UTC_TIMESTAMP = re.compile(
    r"^\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])"
    r"T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d\.\d{3}Z$"
)
_COMMIT_PATHS = Callable[[tuple[str, ...], str], dict[str, object]]
_BINDING_FIELDS = {
    "schema_version",
    "task_id",
    "brief_sha256",
    "ownership_sha256",
    "run_id",
    "run_manifest_ref",
    "run_manifest_sha256",
    "created_at",
    "binding_sha256",
}
_TASK_RUN_STATES = {
    "prepared",
    "launched",
    "running",
    "completed",
    "failed_process",
    "timed_out",
    "cancelled",
    "lost",
}
_TERMINAL_PROCESS_STATES = {
    "completed",
    "failed_process",
    "timed_out",
    "cancelled",
}
class TaskRunError(ValueError):
    """Raised when a Task-to-Run binding is invalid or unsafe."""


def task_run_argv(root: Path, task_id: str) -> list[str]:
    """Return the sole durable Run command for one Task adapter."""
    return [
        sys.executable,
        "-m",
        "arbor.aros.task_adapter",
        "--workspace",
        str(root),
        "--task-id",
        task_id,
    ]


def project_task_status(
    brief: dict[str, object],
    ownership: dict[str, object],
    binding: dict[str, object],
    run_status: dict[str, object],
) -> dict[str, object]:
    """Project one validated Run status without process-authority claims."""
    if not all(
        isinstance(value, dict)
        for value in (brief, ownership, binding, run_status)
    ):
        raise TaskRunError("Task status projection inputs must be JSON objects")
    task_id = _validate_task_id(brief.get("task_id"))
    brief_sha256 = _validate_hash(
        brief.get("brief_sha256"),
        "Task status brief_sha256",
    )
    ownership_sha256 = _validate_hash(
        ownership.get("ownership_sha256"),
        "Task status ownership_sha256",
    )
    run_id = _validate_run_id(binding.get("run_id"))
    manifest_sha256 = _validate_hash(
        binding.get("run_manifest_sha256"),
        "Task status run_manifest_sha256",
    )
    if (
        ownership.get("task_id") != task_id
        or ownership.get("brief_sha256") != brief_sha256
        or binding.get("task_id") != task_id
        or binding.get("brief_sha256") != brief_sha256
        or binding.get("ownership_sha256") != ownership_sha256
    ):
        raise TaskRunError(f"Task status authority identity mismatch: {task_id}")
    if run_status.get("schema_version") != 1:
        raise TaskRunError(f"invalid Run status schema version: {run_id}")
    if run_status.get("run_id") != run_id:
        raise TaskRunError(f"Run status identity mismatch: {run_id}")
    if run_status.get("manifest_sha256") != manifest_sha256:
        raise TaskRunError(f"Run status manifest identity mismatch: {run_id}")
    state = run_status.get("state")
    if not isinstance(state, str) or state not in _TASK_RUN_STATES:
        raise TaskRunError(f"invalid Run status state: {run_id}")
    updated_at = _validate_timestamp(
        run_status.get("updated_at"),
        "Run status updated_at",
    )
    expected_final_ref = f"runs/{run_id}/final.json"
    final_ref = run_status.get("final_ref")
    terminal = state in {
        "completed",
        "failed_process",
        "timed_out",
        "cancelled",
    }
    if (terminal and final_ref != expected_final_ref) or (
        not terminal and final_ref is not None
    ):
        raise TaskRunError(f"invalid Run status final_ref: {run_id}")
    reason = run_status.get("reason")
    if reason is not None and (
        not isinstance(reason, str) or not reason.strip() or reason != reason.strip()
    ):
        raise TaskRunError(f"invalid Run status reason: {run_id}")
    return {
        "schema_version": 1,
        "task_id": task_id,
        "state": "launched" if state == "prepared" else state,
        "brief_sha256": brief_sha256,
        "ownership_sha256": ownership_sha256,
        "run_id": run_id,
        "run_manifest_sha256": manifest_sha256,
        "updated_at": updated_at,
        "final_ref": final_ref,
        "reason": reason,
    }


def commit_terminal_run_if_present(
    root: Path,
    binding: dict[str, object],
    status: dict[str, object],
    commit_paths: _COMMIT_PATHS,
) -> dict[str, object]:
    """Commit one create-once Run final before exposing its reference."""
    workspace = _validate_root(root)
    if not isinstance(binding, dict) or not isinstance(status, dict):
        raise TaskRunError("Task terminal Run inputs must be JSON objects")
    if not callable(commit_paths):
        raise TaskRunError("Task terminal Run commit_paths must be callable")
    run_id = _validate_run_id(binding.get("run_id"))
    manifest_sha256 = _validate_hash(
        binding.get("run_manifest_sha256"),
        "Task terminal Run manifest_sha256",
    )
    status_state = status.get("state")
    status_final_ref = status.get("final_ref")
    if status_state == "lost":
        if status_final_ref is not None:
            raise TaskRunError(f"lost Run cannot expose a final_ref: {run_id}")
        return status
    try:
        terminal = RunService(workspace).terminal_with_commit(run_id)
    except (OSError, RunError) as error:
        raise TaskRunError(f"unable to load terminal Run: {run_id}") from error
    if terminal is None:
        if status_state in _TERMINAL_PROCESS_STATES:
            raise TaskRunError(f"terminal Run final is unavailable: {run_id}")
        return status
    if (
        not isinstance(terminal, tuple)
        or len(terminal) != 3
        or not isinstance(terminal[0], dict)
        or terminal[1] != (f"runs/{run_id}/final.json",)
        or not isinstance(terminal[2], str)
        or not terminal[2]
    ):
        raise TaskRunError(f"invalid terminal Run commit request: {run_id}")
    final, paths, message = terminal
    final_ref = paths[0]
    if (
        final.get("run_id") != run_id
        or final.get("manifest_sha256") != manifest_sha256
        or status.get("run_id") not in (None, run_id)
        or status.get("manifest_sha256") not in (None, manifest_sha256)
    ):
        raise TaskRunError(f"terminal Run identity mismatch: {run_id}")
    if status_state in _TERMINAL_PROCESS_STATES and (
        final.get("state") != status_state
        or status.get("final_ref") != final_ref
    ):
        raise TaskRunError(f"terminal Run status mismatch: {run_id}")
    try:
        final_bytes = (workspace / final_ref).read_bytes()
    except OSError as error:
        raise TaskRunError(f"unable to snapshot terminal Run: {run_id}") from error
    pre_head, already_committed_clean = _pre_callback_manifest_state(
        workspace,
        final_ref,
        final_bytes,
    )
    try:
        commit_result = commit_paths(paths, message)
    except (CheckpointError, OSError, RunError) as error:
        raise TaskRunError(f"unable to commit terminal Run: {run_id}") from error
    _validate_commit_result(
        workspace,
        final_ref,
        commit_result,
        expected_bytes=final_bytes,
        expected_manifest_sha256=manifest_sha256,
        pre_head=pre_head,
        already_committed_clean=already_committed_clean,
    )
    head, working = _require_committed_manifest(workspace, final_ref)
    _validate_single_touch_manifest_history(
        workspace,
        final_ref,
        working,
        head,
    )
    try:
        return RunService(workspace).status(run_id)
    except (OSError, RunError) as error:
        raise TaskRunError(f"unable to re-read terminal Run: {run_id}") from error


def _binding_path(root: Path, task_id: str) -> Path:
    return root / ".aros" / "tasks" / task_id / "run.json"


def _binding_sha256(binding: dict[str, object]) -> str:
    payload = dict(binding)
    payload.pop("binding_sha256", None)
    try:
        return json_sha256(payload)
    except (TypeError, UnicodeError) as error:
        raise TaskRunError("Task Run binding must be canonical UTF-8 JSON") from error


def _ensure_adapter_runtime(root: Path, task_id: str) -> Path:
    """Create or validate the private physical directories used by the adapter."""
    _require_physical_directory(root / ".aros", "AROS runtime directory")
    _require_physical_directory(
        root / ".aros" / "tasks",
        "Task runtime root",
    )
    runtime = root / ".aros" / "tasks" / task_id
    _ensure_private_directory(runtime, "Task adapter runtime")
    _ensure_private_directory(runtime / "home", "Task adapter HOME")
    _ensure_private_directory(runtime / "tmp", "Task adapter TMPDIR")
    return runtime


def load_task_run(
    root: Path,
    brief: dict[str, object],
    ownership: dict[str, object],
) -> dict[str, object]:
    """Strictly load and validate one immutable Task-to-Run binding."""
    workspace = _validate_inputs(root, brief, ownership)
    task_id = str(brief["task_id"])
    _validate_adapter_runtime(workspace, task_id)
    path = _binding_path(workspace, task_id)
    try:
        with AnchoredWorkspaceReader(workspace) as reader:
            raw = reader(path)
    except (OSError, TypeError, UnicodeError, ValueError) as error:
        raise TaskRunError(f"unable to read Task Run binding: {task_id}") from error
    if not isinstance(raw, dict):
        raise TaskRunError(f"invalid Task Run binding schema: {task_id}")
    binding: dict[str, object] = raw
    if (
        set(binding) != _BINDING_FIELDS
        or type(binding.get("schema_version")) is not int
    ):
        raise TaskRunError(f"invalid Task Run binding schema: {task_id}")
    if binding["schema_version"] != 1:
        raise TaskRunError(f"invalid Task Run binding schema version: {task_id}")
    _validate_hash(binding["binding_sha256"], "Task Run binding_sha256")
    if binding["binding_sha256"] != _binding_sha256(binding):
        raise TaskRunError(f"Task Run binding hash mismatch: {task_id}")
    if binding["task_id"] != task_id:
        raise TaskRunError(f"Task Run binding Task identity mismatch: {task_id}")
    if binding["brief_sha256"] != brief["brief_sha256"]:
        raise TaskRunError(f"Task Run binding brief identity mismatch: {task_id}")
    if binding["ownership_sha256"] != ownership["ownership_sha256"]:
        raise TaskRunError(f"Task Run binding ownership identity mismatch: {task_id}")
    for field in (
        "brief_sha256",
        "ownership_sha256",
        "run_manifest_sha256",
    ):
        _validate_hash(binding[field], f"Task Run binding {field}")
    run_id = _validate_run_id(binding["run_id"])
    manifest_ref = f"runs/{run_id}/manifest.json"
    if binding["run_manifest_ref"] != manifest_ref:
        raise TaskRunError(f"Task Run binding manifest ref mismatch: {task_id}")
    _validate_timestamp(binding["created_at"], "Task Run binding created_at")

    try:
        manifest = read_validated_run_manifest(workspace, run_id)
    except (OSError, RunError, TypeError, UnicodeError, ValueError) as error:
        raise TaskRunError(
            f"invalid Run manifest referenced by Task Run binding: {task_id}"
        ) from error
    _validate_bound_manifest(workspace, brief, ownership, binding, manifest)
    head, working = _require_committed_manifest(workspace, manifest_ref)
    _validate_single_touch_manifest_history(
        workspace,
        manifest_ref,
        working,
        head,
    )
    return dict(binding)


def ensure_task_run(
    root: Path,
    brief: dict[str, object],
    ownership: dict[str, object],
    *,
    actor: str,
    commit_paths: _COMMIT_PATHS,
) -> dict[str, object]:
    """Create once, commit, and publish one Task-to-Run binding."""
    workspace = _validate_root(root)
    if not isinstance(brief, dict) or not isinstance(ownership, dict):
        raise TaskRunError("Task Run brief and ownership must be JSON objects")
    task_id = _validate_task_id(brief.get("task_id"))
    with _task_run_locks(workspace, task_id):
        workspace = _validate_inputs(workspace, brief, ownership)
        run_actor = _validate_actor(actor)
        if run_actor != ownership["actor"]:
            raise TaskRunError(f"Task Run actor conflicts with ownership: {task_id}")
        if not callable(commit_paths):
            raise TaskRunError("Task Run commit_paths must be callable")
        _ensure_adapter_runtime(workspace, task_id)
        return _ensure_task_run_locked(
            workspace,
            brief,
            ownership,
            actor=run_actor,
            commit_paths=commit_paths,
        )


def _ensure_task_run_locked(
    workspace: Path,
    brief: dict[str, object],
    ownership: dict[str, object],
    *,
    actor: str,
    commit_paths: _COMMIT_PATHS,
) -> dict[str, object]:
    task_id = str(brief["task_id"])
    binding_path = _binding_path(workspace, task_id)
    if _path_exists(binding_path):
        return load_task_run(workspace, brief, ownership)

    idempotency_key = f"task-run-v1:{brief['brief_sha256']}"
    try:
        runs = RunService(workspace)
        manifest = runs.manifest_for_idempotency_key(idempotency_key)
        if manifest is None:
            manifest = runs.prepare(
                task_run_argv(workspace, task_id),
                cwd=".",
                timeout_seconds=_task_timeout(brief),
                idempotency_key=idempotency_key,
                actor=actor,
                label=f"task-{task_id.lower()}",
                security_profile="trusted-local",
            )
    except (OSError, RunError, TypeError, UnicodeError, ValueError) as error:
        raise TaskRunError(f"unable to prepare Task Run: {task_id}") from error

    validated_manifest_sha256 = _validate_task_manifest(
        workspace,
        brief,
        ownership,
        manifest,
    )
    run_id = _validate_run_id(manifest.get("run_id"))
    manifest_ref = f"runs/{run_id}/manifest.json"
    try:
        manifest_bytes = (workspace / manifest_ref).read_bytes()
    except OSError as error:
        raise TaskRunError(f"unable to snapshot Task Run manifest: {task_id}") from error
    pre_head, already_committed_clean = _pre_callback_manifest_state(
        workspace,
        manifest_ref,
        manifest_bytes,
    )
    try:
        commit_result = commit_paths(
            (manifest_ref,),
            f"Record Task {task_id} Run {run_id}",
        )
    except (CheckpointError, OSError) as error:
        raise TaskRunError(f"unable to commit Task Run manifest: {task_id}") from error
    _validate_commit_result(
        workspace,
        manifest_ref,
        commit_result,
        expected_bytes=manifest_bytes,
        expected_manifest_sha256=validated_manifest_sha256,
        pre_head=pre_head,
        already_committed_clean=already_committed_clean,
    )

    binding: dict[str, object] = {
        "schema_version": 1,
        "task_id": task_id,
        "brief_sha256": brief["brief_sha256"],
        "ownership_sha256": ownership["ownership_sha256"],
        "run_id": run_id,
        "run_manifest_ref": manifest_ref,
        "run_manifest_sha256": validated_manifest_sha256,
        "created_at": utc_now(),
    }
    binding["binding_sha256"] = _binding_sha256(binding)
    parent_identity = _binding_parent_identity(binding_path)
    try:
        created = create_json(binding_path, binding)
    except (OSError, TypeError, UnicodeError, ValueError) as error:
        raise TaskRunError(f"unable to publish Task Run binding: {task_id}") from error
    if _binding_parent_identity(binding_path) != parent_identity:
        if created:
            _discard_failed_binding(binding_path)
        raise TaskRunError(f"Task Run binding parent identity changed: {task_id}")
    return load_task_run(workspace, brief, ownership)


@contextmanager
def _task_run_locks(root: Path, task_id: str) -> Iterator[None]:
    stack = ExitStack()
    try:
        from .tasks import TaskError, TaskService, _ensure_durable_lock_file

        tasks = TaskService(root)
        publication_lock = tasks._publication_lock_path()
        _ensure_durable_lock_file(
            publication_lock,
            "task record publication lock",
        )
        stack.enter_context(file_lock(publication_lock))
        lifecycle_lock = tasks._lifecycle_lock_path(task_id)
        _ensure_durable_lock_file(lifecycle_lock, "task lifecycle lock")
        stack.enter_context(file_lock(lifecycle_lock))
    except (OSError, TaskError) as error:
        stack.close()
        raise TaskRunError(f"unable to lock Task Run publication: {task_id}") from error
    try:
        yield
    finally:
        stack.close()


def _validate_inputs(
    root: Path,
    brief: dict[str, object],
    ownership: dict[str, object],
) -> Path:
    workspace = _validate_root(root)
    if not isinstance(brief, dict) or not isinstance(ownership, dict):
        raise TaskRunError("Task Run brief and ownership must be JSON objects")
    task_id = _validate_task_id(brief.get("task_id"))
    try:
        from .tasks import (
            TaskError,
            _OWNERSHIP_FIELDS,
            _PureTaskGit,
            _ownership_sha256,
            _validate_task_brief,
            _validate_timestamp,
        )
        from .worktrees import WorktreeError, bind_repository

        with AnchoredWorkspaceReader(workspace) as reader:
            recorded_brief = reader(workspace / "tasks" / task_id / "brief.json")
            recorded_ownership = reader(
                workspace / ".aros" / "tasks" / task_id / "ownership.json"
            )
        if not isinstance(recorded_brief, dict):
            raise TaskError(f"invalid task brief: {task_id}")
        _validate_task_brief(recorded_brief, task_id)
        if (
            not isinstance(recorded_ownership, dict)
            or set(recorded_ownership) != _OWNERSHIP_FIELDS
            or type(recorded_ownership.get("schema_version")) is not int
            or recorded_ownership["schema_version"] != 1
            or recorded_ownership.get("task_id") != task_id
            or recorded_ownership.get("brief_sha256")
            != recorded_brief["brief_sha256"]
            or recorded_ownership.get("base_commit")
            != recorded_brief["base_commit"]
            or recorded_ownership.get("branch") != f"aros/task/{task_id}"
            or recorded_ownership.get("worktree_path")
            != str(workspace / ".worktree" / "tasks" / task_id)
        ):
            raise TaskError(f"invalid task ownership: {task_id}")
        ownership_actor = _validate_actor(recorded_ownership.get("actor"))
        if ownership_actor != recorded_ownership["actor"]:
            raise TaskError(f"invalid task ownership actor: {task_id}")
        parent_head = recorded_ownership.get("parent_head")
        repository = bind_repository(workspace)
        if (
            not isinstance(parent_head, str)
            or _COMMIT.fullmatch(parent_head) is None
            or not _PureTaskGit(repository)._is_commit(parent_head)
        ):
            raise TaskError(f"invalid task ownership parent HEAD: {task_id}")
        _validate_timestamp(
            recorded_ownership.get("acquired_at"),
            "task ownership acquired_at",
        )
        if recorded_ownership.get("ownership_sha256") != _ownership_sha256(
            recorded_ownership
        ):
            raise TaskError(f"task ownership hash mismatch: {task_id}")
    except (OSError, TaskError, WorktreeError, ValueError) as error:
        raise TaskRunError(f"invalid Task authority for Run binding: {task_id}") from error
    if brief != recorded_brief:
        raise TaskRunError(f"Task Run brief is not authoritative: {task_id}")
    if ownership != recorded_ownership:
        raise TaskRunError(f"Task Run ownership is not authoritative: {task_id}")
    return workspace


def _validate_bound_manifest(
    root: Path,
    brief: dict[str, object],
    ownership: dict[str, object],
    binding: dict[str, object],
    manifest: dict[str, object],
) -> None:
    task_id = str(brief["task_id"])
    manifest_hash = _validate_task_manifest(root, brief, ownership, manifest)
    if manifest_hash != binding["run_manifest_sha256"]:
        raise TaskRunError(f"Run manifest differs from Task Run binding: {task_id}")


def _validate_task_manifest(
    root: Path,
    brief: dict[str, object],
    ownership: dict[str, object],
    manifest: dict[str, object],
) -> str:
    task_id = str(brief["task_id"])
    run_id = _validate_run_id(manifest.get("run_id"))
    manifest_hash = _validate_hash(
        manifest.get("manifest_sha256"),
        "Task Run manifest_sha256",
    )
    expected_timeout = _task_timeout(brief)
    if (
        type(manifest.get("schema_version")) is not int
        or manifest["schema_version"] != 1
        or manifest.get("run_id") != run_id
        or manifest.get("argv") != task_run_argv(root, task_id)
        or manifest.get("idempotency_key")
        != f"task-run-v1:{brief['brief_sha256']}"
        or manifest.get("actor") != ownership["actor"]
        or manifest.get("cwd") != "."
        or type(manifest.get("timeout_seconds")) is not float
        or manifest["timeout_seconds"] != expected_timeout
        or manifest.get("label") != f"task-{task_id.lower()}"[:32]
        or manifest.get("security_profile") != "trusted-local"
    ):
        raise TaskRunError(f"Run manifest differs from Task request: {task_id}")
    _validate_timestamp(manifest.get("created_at"), "Task Run manifest created_at")
    return manifest_hash


def _validate_commit_result(
    root: Path,
    manifest_ref: str,
    result: object,
    *,
    expected_bytes: bytes,
    expected_manifest_sha256: str,
    pre_head: str,
    already_committed_clean: bool,
) -> None:
    if (
        not isinstance(result, dict)
        or not isinstance(result.get("commit"), str)
        or _COMMIT.fullmatch(result["commit"]) is None
        or result.get("paths") != [manifest_ref]
        or result.get("enforcement_class") != "cooperative"
        or _SHA256.fullmatch(expected_manifest_sha256) is None
    ):
        raise TaskRunError("invalid Task Run manifest commit result")
    commit = str(result["commit"])
    head, blob, working = _committed_manifest_bytes(root, manifest_ref, commit)
    if commit != head:
        raise TaskRunError("Task Run manifest commit is not current HEAD")
    if blob != expected_bytes or working != expected_bytes:
        raise TaskRunError("Task Run committed manifest differs from prepared snapshot")
    try:
        from .tasks import TaskError, _PureTaskGit
        from .worktrees import WorktreeError, bind_repository

        git = _PureTaskGit(bind_repository(root))
        dirty = git._safe_git_text(
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--",
            manifest_ref,
        )
        if dirty:
            raise TaskRunError("Task Run manifest is dirty after commit callback")
        if already_committed_clean:
            if head != pre_head or commit != pre_head:
                raise TaskRunError("callback changed HEAD for a reused Task Run manifest")
        else:
            if result.get("reused") is True or head == pre_head:
                raise TaskRunError("new Task Run manifest cannot be reported as reused")
            parent = git._safe_git_text("rev-parse", f"{commit}^")
            if parent != pre_head:
                raise TaskRunError("Task Run manifest commit parent mismatch")
            changed = git._safe_git_text(
                "diff-tree",
                "--no-commit-id",
                "--name-only",
                "-r",
                commit,
            ).splitlines()
            if changed != [manifest_ref]:
                raise TaskRunError("Task Run commit contains unexpected paths")
    except (OSError, TaskError, WorktreeError) as error:
        raise TaskRunError("unable to validate Task Run commit paths") from error


def _pre_callback_manifest_state(
    root: Path,
    manifest_ref: str,
    expected_bytes: bytes,
) -> tuple[str, bool]:
    try:
        from .tasks import TaskError, _PureTaskGit
        from .worktrees import WorktreeError, bind_repository

        git = _PureTaskGit(bind_repository(root))
        head = git._safe_git_text("rev-parse", "--verify", "HEAD^{commit}")
        committed = git._safe_git_result("show", f"{head}:{manifest_ref}")
        dirty = git._safe_git_text(
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--",
            manifest_ref,
        )
    except (OSError, TaskError, WorktreeError) as error:
        raise TaskRunError("unable to inspect Task Run manifest before commit") from error
    already_committed_clean = (
        committed.returncode == 0
        and committed.stdout == expected_bytes
        and not dirty
    )
    if already_committed_clean:
        _validate_single_touch_manifest_history(
            root,
            manifest_ref,
            expected_bytes,
            head,
        )
    return head, already_committed_clean


def _validate_single_touch_manifest_history(
    root: Path,
    manifest_ref: str,
    expected_bytes: bytes,
    head: str,
) -> None:
    try:
        from .tasks import TaskError, _PureTaskGit
        from .worktrees import WorktreeError, bind_repository

        git = _PureTaskGit(bind_repository(root))
        touching_commits = git._safe_git_text(
            "log",
            "--format=%H",
            "--",
            manifest_ref,
        ).splitlines()
        if len(touching_commits) != 1 or _COMMIT.fullmatch(touching_commits[0]) is None:
            raise TaskRunError("Task Run manifest history is not immutable")
        introducing_commit = touching_commits[0]
        introducing_blob = git._safe_git_bytes(
            "show",
            f"{introducing_commit}:{manifest_ref}",
        )
        introducing_paths = git._safe_git_text(
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "-r",
            introducing_commit,
        ).splitlines()
        if (
            introducing_blob != expected_bytes
            or introducing_paths != [manifest_ref]
            or not git._safe_git_success(
                "merge-base",
                "--is-ancestor",
                introducing_commit,
                head,
            )
        ):
            raise TaskRunError("Task Run manifest introducing commit is invalid")
    except (OSError, TaskError, WorktreeError) as error:
        raise TaskRunError(
            "unable to validate Task Run manifest introducing commit"
        ) from error


def _require_committed_manifest(root: Path, manifest_ref: str) -> tuple[str, bytes]:
    head, blob, working = _committed_manifest_bytes(root, manifest_ref, "HEAD")
    if _COMMIT.fullmatch(head) is None or blob != working:
        raise TaskRunError("Task Run manifest is not committed at current HEAD")
    return head, working


def _committed_manifest_bytes(
    root: Path,
    manifest_ref: str,
    commit: str,
) -> tuple[str, bytes, bytes]:
    try:
        from .tasks import TaskError, _PureTaskGit
        from .worktrees import WorktreeError, bind_repository

        git = _PureTaskGit(bind_repository(root))
        head = git._safe_git_text("rev-parse", "--verify", "HEAD^{commit}")
        blob = git._safe_git_bytes("show", f"{commit}:{manifest_ref}")
        working = (root / manifest_ref).read_bytes()
    except (OSError, TaskError, WorktreeError, ValueError) as error:
        raise TaskRunError("Task Run manifest is not committed at current HEAD") from error
    return head, blob, working


def _validate_root(root: Path) -> Path:
    if not isinstance(root, Path):
        raise TaskRunError("Task Run workspace root must be a Path")
    supplied = root.expanduser().absolute()
    try:
        resolved = supplied.resolve(strict=True)
    except OSError as error:
        raise TaskRunError(f"Task Run workspace does not exist: {supplied}") from error
    if supplied != resolved or not resolved.is_dir():
        raise TaskRunError(f"Task Run workspace root is not physical: {supplied}")
    return resolved


def _validate_task_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) > 128
        or _TASK_ID.fullmatch(value) is None
    ):
        raise TaskRunError(f"invalid Task Run Task ID: {value!r}")
    return value


def _validate_run_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) > 128
        or _RUN_ID.fullmatch(value) is None
    ):
        raise TaskRunError(f"invalid Task Run Run ID: {value!r}")
    return value


def _validate_hash(value: object, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise TaskRunError(f"{field} must be a 64-hex SHA-256")
    return value


def _validate_timestamp(value: object, field: str) -> str:
    if not isinstance(value, str) or _UTC_TIMESTAMP.fullmatch(value) is None:
        raise TaskRunError(f"{field} must be a UTC timestamp")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError as error:
        raise TaskRunError(f"{field} must be a UTC timestamp") from error
    return value


def _validate_actor(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 1000:
        raise TaskRunError("Task Run actor must be a non-empty string")
    return value.strip()


def _task_timeout(brief: dict[str, object]) -> float:
    value = brief.get("timeout_seconds")
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or value <= 0
        or not math.isfinite(value)
    ):
        raise TaskRunError(f"invalid Task Run timeout: {brief.get('task_id')}")
    return float(value)


def _path_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise TaskRunError(f"unable to inspect Task Run path: {path}") from error
    return True


def _binding_parent_identity(path: Path) -> tuple[int, int]:
    parent = path.parent
    try:
        metadata = parent.lstat()
    except OSError as error:
        raise TaskRunError(f"Task Run binding parent is unavailable: {parent}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise TaskRunError(f"Task Run binding parent must be physical: {parent}")
    return metadata.st_dev, metadata.st_ino


def _discard_failed_binding(path: Path) -> None:
    try:
        metadata = path.lstat()
        if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
            path.unlink()
    except OSError:
        pass


def _validate_adapter_runtime(root: Path, task_id: str) -> Path:
    """Purely validate the private physical directories used by the adapter."""
    _require_physical_directory(root / ".aros", "AROS runtime directory")
    _require_physical_directory(root / ".aros" / "tasks", "Task runtime root")
    runtime = root / ".aros" / "tasks" / task_id
    _validate_private_directory(runtime, "Task adapter runtime")
    _validate_private_directory(runtime / "home", "Task adapter HOME")
    _validate_private_directory(runtime / "tmp", "Task adapter TMPDIR")
    return runtime


def _require_physical_directory(path: Path, description: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise TaskRunError(f"{description} is unavailable: {path}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise TaskRunError(f"{description} must be a physical directory: {path}")
    _open_directory(path, description, chmod=False, expected_mode=None)


def _validate_private_directory(path: Path, description: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise TaskRunError(f"{description} is unavailable: {path}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise TaskRunError(f"{description} must be a physical directory: {path}")
    _open_directory(path, description, chmod=False, expected_mode=0o700)


def _ensure_private_directory(path: Path, description: str) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
        except OSError as error:
            raise TaskRunError(f"unable to create {description}: {path}") from error
        try:
            metadata = path.lstat()
        except OSError as error:
            raise TaskRunError(f"unable to inspect {description}: {path}") from error
    except OSError as error:
        raise TaskRunError(f"unable to inspect {description}: {path}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise TaskRunError(f"{description} must be a physical directory: {path}")
    _open_directory(path, description, chmod=True, expected_mode=0o700)


def _open_directory(
    path: Path,
    description: str,
    *,
    chmod: bool,
    expected_mode: int | None,
) -> None:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise TaskRunError(f"unable to open {description}: {path}") from error
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISDIR(opened.st_mode):
            raise TaskRunError(f"{description} must be a physical directory: {path}")
        if chmod:
            os.fchmod(descriptor, 0o700)
        if (
            expected_mode is not None
            and stat.S_IMODE(os.fstat(descriptor).st_mode) != expected_mode
        ):
            raise TaskRunError(f"{description} permissions are not mode 0700: {path}")
    except OSError as error:
        raise TaskRunError(f"unable to validate {description}: {path}") from error
    finally:
        os.close(descriptor)
