"""Immutable task briefs and prepared runtime state for AROS children."""

from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import re
import secrets
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .store import (
    atomic_write_json,
    create_json,
    file_lock,
    json_sha256,
    read_json,
    utc_now,
)


_TASK_ID = re.compile(
    r"^TASK-[0-9]{8}-[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?$"
)
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TASK_STAGING_DIRECTORY = ".staging"
_UTC_TIMESTAMP = re.compile(
    r"^\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])"
    r"T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d\.\d{3}Z$"
)
_REQUEST_FIELDS = (
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
_BRIEF_FIELDS = {
    "schema_version",
    "task_id",
    *_REQUEST_FIELDS,
    "base_commit",
    "request_sha256",
    "created_at",
    "brief_sha256",
}
_STATUS_FIELDS = {
    "schema_version",
    "task_id",
    "state",
    "brief_sha256",
    "updated_at",
}
_READY_STATUS_FIELDS = {*_STATUS_FIELDS, "ownership_sha256"}
_TERMINAL_STATES = {"completed", "failed_process", "timed_out", "cancelled"}
_MESSAGE_FIELDS = {
    "schema_version",
    "task_id",
    "sequence",
    "actor",
    "text",
    "created_at",
    "previous_message_sha256",
    "message_sha256",
}
_MESSAGE_FILENAME = re.compile(r"^[0-9]{20}\.json$")
_RETURN_FIELDS = {
    "schema_version",
    "task_id",
    "brief_sha256",
    "base_commit",
    "child_commit",
    "summary",
    "work_performed",
    "changed_files",
    "evidence",
    "deviations",
    "uncertainty",
    "follow_up",
    "return_sha256",
}
_COLLECTED_FIELDS = {
    "schema_version",
    "task_id",
    "brief_sha256",
    "ownership_sha256",
    "branch_ref",
    "base_commit",
    "child_commit",
    "return_commit",
    "final_state",
    "final_sha256",
    "return",
    "collected_at",
    "collected_sha256",
}
_PRUNE_FIELDS = {
    "schema_version",
    "task_id",
    "brief_sha256",
    "ownership_sha256",
    "collected_sha256",
    "branch_ref",
    "return_commit",
    "worktree_path",
    "requested_at",
    "prune_sha256",
}
_PRUNED_FIELDS = {
    "schema_version",
    "task_id",
    "state",
    "brief_sha256",
    "ownership_sha256",
    "collected_sha256",
    "branch_ref",
    "return_commit",
    "final_state",
    "final_sha256",
    "worktree_path",
    "removed_at",
    "prune_sha256",
    "pruned_sha256",
}
_LAUNCH_GRACE_SECONDS = 2.0
_OWNERSHIP_FIELDS = {
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
_INDEX_FIELDS = {
    "schema_version",
    "idempotency_key_sha256",
    "request_sha256",
    "task_id",
    "brief_sha256",
    "created_at",
}
_FILTER_CONFIG_KEY = re.compile(
    r"^filter\.(.+)\.(?:clean|smudge|process|required)$",
    re.IGNORECASE,
)
_FILTER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_TASK_RUNNER_BOOTSTRAP = (
    "import runpy,sys;"
    "controlled=sys.argv.pop(1);"
    "sys.path.insert(0,controlled);"
    "runpy.run_module('arbor.aros.task_runner',run_name='__main__')"
)
_UNSUPPORTED_FILE_MODE_ERRNOS = {
    errno.ENOSYS,
    getattr(errno, "ENOTSUP", errno.ENOSYS),
    getattr(errno, "EOPNOTSUPP", errno.ENOSYS),
}


class TaskError(ValueError):
    """Raised when a child-task record is invalid or unsafe."""


class TaskService:
    """Create and inspect child-task records in one AROS Git workspace."""

    def __init__(self, root: str | Path):
        supplied = Path(root).expanduser().absolute()
        try:
            resolved = supplied.resolve(strict=True)
        except OSError as error:
            raise TaskError(
                f"workspace must be an existing Git repository root: {supplied}"
            ) from error
        if supplied != resolved:
            raise TaskError(f"workspace must be the exact Git repository root: {supplied}")
        self.root = resolved
        self._git_dir, self._git_common_dir = self._require_git_root()
        self._require_initialized_workspace()
        self._filesystem_permission_probe = _validate_filesystem_permission_probe(
            _probe_filesystem_permissions(self.root / ".aros"),
            "workspace filesystem permission probe",
        )
        self._filesystem_permissions_enforced = (
            self._filesystem_permission_probe["enforced"] is True
        )

    def create(
        self,
        objective: str,
        *,
        actor: str,
        mode: str,
        adapter_argv: list[str],
        capabilities: dict[str, bool],
        deliverables: list[str],
        acceptance: list[str],
        timeout_seconds: float,
        idempotency_key: str,
    ) -> dict[str, object]:
        """Freeze one versioned task brief without starting child execution."""
        request = _normalize_request(
            objective=objective,
            actor=actor,
            mode=mode,
            adapter_argv=adapter_argv,
            capabilities=capabilities,
            deliverables=deliverables,
            acceptance=acceptance,
            timeout_seconds=timeout_seconds,
            idempotency_key=idempotency_key,
        )
        request_sha256 = json_sha256(request)
        key = str(request["idempotency_key"])
        key_digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        self._ensure_record_roots()
        lock_path = self._idempotency_lock_path(key)
        _ensure_durable_lock_file(lock_path, "task idempotency lock")

        with file_lock(lock_path):
            _require_plain_file(lock_path, "task idempotency lock")
            publication_lock = self._publication_lock_path()
            _ensure_durable_lock_file(
                publication_lock,
                "task record publication lock",
            )
            with file_lock(publication_lock):
                _require_plain_file(
                    publication_lock,
                    "task record publication lock",
                )
                return self._create_locked(
                    request,
                    request_sha256,
                    key,
                    key_digest,
                )

    def _create_locked(
        self,
        request: dict[str, object],
        request_sha256: str,
        key: str,
        key_digest: str,
    ) -> dict[str, object]:
        self._reconcile_authoritative_briefs()
        index_path = self._idempotency_index_path(key)
        if _path_exists(index_path):
            index = self._load_idempotency_index(index_path, key_digest)
            task_id = str(index["task_id"])
            brief = self._load_brief(task_id)
            self._validate_index_binding(index, brief)
            if index["request_sha256"] != request_sha256:
                raise TaskError(
                    "idempotency key already belongs to a different task request"
                )
            return brief

        existing = self._brief_for_idempotency_key(key)
        if existing is not None:
            if existing["request_sha256"] != request_sha256:
                raise TaskError(
                    "idempotency key already belongs to a different task request"
                )
            return existing

        self._require_git_root()
        base_commit = self._git_output(
            "rev-parse",
            "--verify",
            "HEAD^{commit}",
            pinned=True,
        )
        self._require_git_root()
        if base_commit is None or _COMMIT.fullmatch(base_commit) is None:
            raise TaskError("child tasks require a committed 40-hex Git HEAD")
        for _ in range(8):
            task_id = self._new_task_id(str(request["objective"]))
            self._validate_task_id(task_id)
            versioned_directory = self.root / "tasks" / task_id
            runtime_directory = self.root / ".aros" / "tasks" / task_id
            staging_directory = (
                self.root / "tasks" / _TASK_STAGING_DIRECTORY / task_id
            )
            if not any(
                _path_exists(path)
                for path in (
                    versioned_directory,
                    runtime_directory,
                    staging_directory,
                )
            ):
                break
        else:
            raise TaskError("task ID allocation exhausted after repeated conflicts")
        _require_absent(versioned_directory, "versioned task path")
        _require_absent(runtime_directory, "runtime task path")
        _require_absent(staging_directory, "versioned task staging path")

        created_at = utc_now()
        brief: dict[str, object] = {
            "schema_version": 1,
            "task_id": task_id,
            **request,
            "base_commit": base_commit,
            "request_sha256": request_sha256,
            "created_at": created_at,
        }
        brief["brief_sha256"] = _brief_sha256(brief)
        _create_plain_directory(staging_directory, "versioned task staging path")
        if not create_json(staging_directory / "brief.json", brief):
            raise TaskError(f"staged task brief already exists: {task_id}")
        if _read_object(staging_directory / "brief.json", "staged task brief") != brief:
            raise TaskError(f"staged task brief differs after write: {task_id}")

        self._publish_staged_brief(staging_directory, versioned_directory)
        published = self._load_brief(task_id)
        if published != brief:
            raise TaskError(f"published task brief differs from staging: {task_id}")
        self._recover_prepared_records(published)
        self._validate_inventory()
        return published

    def start(
        self,
        task_id: str,
        *,
        actor: str | None = None,
    ) -> dict[str, object]:
        """Ensure ownership and request the task's sole adapter launch attempt."""
        self._validate_task_id(task_id)
        self._ensure_record_roots()
        if _path_exists(self._launch_path(task_id)):
            return self._existing_execution_status(task_id, actor)

        self._ensure_worktree(task_id, actor=actor)
        publication_lock = self._publication_lock_path()
        _ensure_durable_lock_file(publication_lock, "task record publication lock")
        with file_lock(publication_lock):
            lifecycle_lock = self._lifecycle_lock_path(task_id)
            _ensure_durable_lock_file(lifecycle_lock, "task lifecycle lock")
            with file_lock(lifecycle_lock):
                self._reconcile_authoritative_briefs()
                brief = self._load_brief(task_id)
                ownership = self._load_ownership(brief)
                if _path_exists(self._launch_path(task_id)):
                    return self._existing_execution_status_locked(
                        brief,
                        ownership,
                        actor,
                    )
                status = self._load_task_status(brief, ownership)
                if status["state"] != "worktree_ready":
                    raise TaskError(f"task is not ready for launch: {task_id}")
                launch_actor = _validate_text(
                    actor if actor is not None else ownership["actor"],
                    "actor",
                )
                if launch_actor != ownership["actor"]:
                    raise TaskError(f"task ownership actor conflict: {task_id}")
                tmux = shutil.which("tmux")
                if tmux is None:
                    raise TaskError("tmux is required for durable child tasks")
                runtime = self._runtime_path(task_id)
                self._prepare_execution_paths(runtime)
                launched_at = utc_now()
                session_name = f"aros-task-{task_id.lower()}"
                socket_name = _tmux_socket_name(self.root, task_id)
                runner_cwd = runtime / "home"
                runner_invocation = [
                    sys.executable,
                    "-I",
                    "-c",
                    _TASK_RUNNER_BOOTSTRAP,
                    str(runtime / "runner-import"),
                    "--workspace",
                    str(self.root),
                    "--task-id",
                    task_id,
                ]
                launch: dict[str, object] = {
                    "schema_version": 1,
                    "task_id": task_id,
                    "actor": launch_actor,
                    "brief_sha256": brief["brief_sha256"],
                    "ownership_sha256": ownership["ownership_sha256"],
                    "base_commit": brief["base_commit"],
                    "security_profile": "trusted-local",
                    "isolation_scope": "application",
                    "capabilities_enforced": False,
                    "filesystem_permissions_enforced": (
                        self._filesystem_permissions_enforced
                    ),
                    "filesystem_permission_probe": dict(
                        self._filesystem_permission_probe
                    ),
                    "carrier": "tmux",
                    "tmux_session": session_name,
                    "tmux_socket": socket_name,
                    "host": socket.gethostname(),
                    "runner_version": 1,
                    "runner_cwd": str(runner_cwd),
                    "runner_invocation": runner_invocation,
                    "launched_at": launched_at,
                }
                launch["launch_sha256"] = _record_sha256(
                    launch,
                    "launch_sha256",
                )
                if not create_json(self._launch_path(task_id), launch):
                    return self._existing_execution_status_locked(
                        brief,
                        ownership,
                        actor,
                    )
                recorded_launch = self._load_launch(brief, ownership)
                if recorded_launch != launch:
                    raise TaskError(f"task launch differs after write: {task_id}")
                from .task_runner import launched_status

                atomic_write_json(
                    self._status_path(task_id),
                    launched_status(brief, ownership, recorded_launch),
                )

        try:
            from .task_runner import runner_environment

            result = subprocess.run(
                [
                    tmux,
                    "-L",
                    socket_name,
                    "new-session",
                    "-d",
                    "-s",
                    session_name,
                    "-c",
                    str(runner_cwd),
                    shlex.join(runner_invocation),
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                env=runner_environment(runtime),
            )
        except subprocess.TimeoutExpired:
            result = None
        except OSError as error:
            self._record_carrier_failure(task_id, str(error))
            raise TaskError(f"tmux launch failed for task {task_id}: {error}") from error
        if result is not None and result.returncode != 0:
            detail = (result.stderr or result.stdout).strip() or "unknown tmux error"
            self._record_carrier_failure(task_id, detail)
            raise TaskError(f"tmux launch failed for task {task_id}: {detail}")

        deadline = time.monotonic() + _LAUNCH_GRACE_SECONDS
        latest = self.status(task_id)
        while latest["state"] == "launched" and time.monotonic() < deadline:
            time.sleep(0.02)
            latest = self.status(task_id)
        return latest

    def _existing_execution_status(
        self,
        task_id: str,
        actor: str | None,
    ) -> dict[str, object]:
        publication_lock = self._publication_lock_path()
        _ensure_durable_lock_file(publication_lock, "task record publication lock")
        with file_lock(publication_lock):
            lifecycle_lock = self._lifecycle_lock_path(task_id)
            _ensure_durable_lock_file(lifecycle_lock, "task lifecycle lock")
            with file_lock(lifecycle_lock):
                self._reconcile_authoritative_briefs()
                brief = self._load_brief(task_id)
                ownership = self._load_ownership(brief)
                return self._existing_execution_status_locked(
                    brief,
                    ownership,
                    actor,
                )

    def _existing_execution_status_locked(
        self,
        brief: dict[str, object],
        ownership: dict[str, object],
        actor: str | None,
    ) -> dict[str, object]:
        task_id = str(brief["task_id"])
        launch = self._load_launch(brief, ownership)
        if actor is not None and _validate_text(actor, "actor") != launch["actor"]:
            raise TaskError(f"task launch actor conflict: {task_id}")
        return self._reconcile_execution(brief, ownership, launch)

    def _ensure_worktree(
        self,
        task_id: str,
        *,
        actor: str | None = None,
    ) -> dict[str, object]:
        """Prepare one owned task worktree without launching its adapter."""
        self._validate_task_id(task_id)
        self._ensure_record_roots()
        publication_lock = self._publication_lock_path()
        _ensure_durable_lock_file(publication_lock, "task record publication lock")
        with file_lock(publication_lock):
            lifecycle_lock = self._lifecycle_lock_path(task_id)
            _ensure_durable_lock_file(lifecycle_lock, "task lifecycle lock")
            with file_lock(lifecycle_lock):
                self._reconcile_authoritative_briefs()
                brief = self._load_brief(task_id)
                return self._ensure_worktree_locked(brief, actor)

    def _ensure_worktree_locked(
        self,
        brief: dict[str, object],
        actor: str | None,
    ) -> dict[str, object]:
        task_id = str(brief["task_id"])
        status = self._status_unlocked(task_id)
        owner = _validate_text(
            actor if actor is not None else brief["actor"],
            "actor",
        )
        if status["state"] not in {"prepared", "worktree_ready"}:
            ownership = self._load_ownership(brief)
            if actor is not None and ownership["actor"] != owner:
                raise TaskError(f"task ownership actor conflict: {task_id}")
            return status
        parent_head = self._require_startable_parent(brief)
        if status["state"] == "worktree_ready":
            ownership = self._load_ownership(brief)
            if actor is not None and ownership["actor"] != owner:
                raise TaskError(f"task ownership actor conflict: {task_id}")
            self._require_parent_unchanged(brief, parent_head)
            return self._load_task_status(brief, self._load_ownership(brief))

        target = self._worktree_target(task_id, create_roots=True)
        branch = f"aros/task/{task_id}"
        self._require_unallocated_worktree(target, branch)
        self._add_task_worktree(target, branch, str(brief["base_commit"]))
        self._require_parent_unchanged(brief, parent_head)
        self._validate_new_checkout(target, branch, str(brief["base_commit"]))

        acquired_at = utc_now()
        ownership: dict[str, object] = {
            "schema_version": 1,
            "task_id": task_id,
            "brief_sha256": brief["brief_sha256"],
            "actor": owner,
            "worktree_path": str(target),
            "branch": branch,
            "base_commit": brief["base_commit"],
            "parent_head": parent_head,
            "acquired_at": acquired_at,
        }
        ownership["ownership_sha256"] = _ownership_sha256(ownership)
        ownership_path = self._ownership_path(task_id)
        if not create_json(ownership_path, ownership):
            raise TaskError(f"task ownership already exists: {task_id}")
        recorded = self._load_ownership(brief)
        if recorded != ownership:
            raise TaskError(f"task ownership differs after write: {task_id}")
        self._require_parent_unchanged(brief, parent_head)
        self._validate_new_checkout(target, branch, str(brief["base_commit"]))

        ready = _worktree_ready_status(brief, recorded)
        atomic_write_json(self._status_path(task_id), ready)
        self._require_parent_unchanged(brief, parent_head)
        return self._load_task_status(brief, self._load_ownership(brief))

    def status(self, task_id: str) -> dict[str, object]:
        """Return validated prepared or worktree-ready runtime state."""
        self._validate_task_id(task_id)
        self._ensure_record_roots()
        publication_lock = self._publication_lock_path()
        _ensure_durable_lock_file(publication_lock, "task record publication lock")
        with file_lock(publication_lock):
            lifecycle_lock = self._lifecycle_lock_path(task_id)
            _ensure_durable_lock_file(lifecycle_lock, "task lifecycle lock")
            with file_lock(lifecycle_lock):
                self._reconcile_authoritative_briefs()
                return self._status_unlocked(task_id)

    def stop(
        self,
        task_id: str,
        *,
        actor: str,
        reason: str,
        signal_name: str = "TERM",
    ) -> dict[str, object]:
        """Persist one attributed stop request for the task runner."""
        self._validate_task_id(task_id)
        self._ensure_record_roots()
        publication_lock = self._publication_lock_path()
        _ensure_durable_lock_file(publication_lock, "task record publication lock")
        with file_lock(publication_lock):
            lifecycle_lock = self._lifecycle_lock_path(task_id)
            _ensure_durable_lock_file(lifecycle_lock, "task lifecycle lock")
            with file_lock(lifecycle_lock):
                self._reconcile_authoritative_briefs()
                from .task_runner import request_stop_locked

                request = request_stop_locked(
                    self,
                    task_id,
                    actor=actor,
                    reason=reason,
                    signal_name=signal_name,
                )
        from .task_runner import deliver_stop

        _ensure_durable_lock_file(
            self._stop_delivery_lock_path(task_id),
            "task stop delivery lock",
        )
        deliver_stop(self, task_id)
        return request

    def message(
        self,
        task_id: str,
        message: str,
        actor: str,
    ) -> dict[str, object]:
        """Append one immutable mailbox record without claiming delivery."""
        self._validate_task_id(task_id)
        text = _validate_text(message, "message")
        canonical_actor = _validate_text(actor, "actor")
        self._ensure_record_roots()
        publication_lock = self._publication_lock_path()
        _ensure_durable_lock_file(publication_lock, "task record publication lock")
        with file_lock(publication_lock):
            lifecycle_lock = self._lifecycle_lock_path(task_id)
            _ensure_durable_lock_file(lifecycle_lock, "task lifecycle lock")
            with file_lock(lifecycle_lock):
                self._reconcile_authoritative_briefs()
                brief = self._load_brief(task_id)
                self._load_bound_idempotency_index(brief)
                messages = self._load_messages(brief)
                directory = self._messages_path(task_id)
                if not _path_exists(directory):
                    _create_plain_directory(directory, "task message directory")
                sequence = len(messages) + 1
                record: dict[str, object] = {
                    "schema_version": 1,
                    "task_id": task_id,
                    "sequence": sequence,
                    "actor": canonical_actor,
                    "text": text,
                    "created_at": utc_now(),
                    "previous_message_sha256": (
                        messages[-1]["message_sha256"] if messages else None
                    ),
                }
                record["message_sha256"] = _record_sha256(
                    record,
                    "message_sha256",
                )
                path = directory / f"{sequence:020d}.json"
                if not create_json(path, record):
                    raise TaskError(f"task message already exists: {path}")
                recorded = self._load_messages(brief)
                if len(recorded) != sequence or recorded[-1] != record:
                    raise TaskError(f"task message differs after write: {task_id}")
                return recorded[-1]

    def _load_messages(
        self,
        brief: dict[str, object],
    ) -> list[dict[str, object]]:
        task_id = str(brief["task_id"])
        directory = self._messages_path(task_id)
        if not _path_exists(directory):
            return []
        _require_plain_directory(directory, "task message directory")
        try:
            paths = sorted(directory.iterdir(), key=lambda path: path.name)
        except OSError as error:
            raise TaskError(
                f"unable to inspect task message directory: {directory}"
            ) from error
        messages: list[dict[str, object]] = []
        previous: str | None = None
        for expected_sequence, path in enumerate(paths, start=1):
            if _MESSAGE_FILENAME.fullmatch(path.name) is None:
                raise TaskError(f"unrecognized task message entry: {path}")
            if path.name != f"{expected_sequence:020d}.json":
                raise TaskError(f"task message sequence has a gap: {path}")
            _require_restrictive_plain_file(
                path,
                "task message",
                permissions_enforced=self._filesystem_permissions_enforced,
            )
            record = _read_object(path, "task message")
            if (
                set(record) != _MESSAGE_FIELDS
                or type(record.get("schema_version")) is not int
                or type(record.get("sequence")) is not int
            ):
                raise TaskError(f"invalid task message schema: {path}")
            if record["schema_version"] != 1:
                raise TaskError(f"invalid task message schema version: {path}")
            if record["task_id"] != task_id:
                raise TaskError(f"task message identity mismatch: {path}")
            if record["sequence"] != expected_sequence:
                raise TaskError(f"task message sequence mismatch: {path}")
            actor = _validate_text(record["actor"], "task message actor")
            text = _validate_text(record["text"], "task message text")
            if actor != record["actor"] or text != record["text"]:
                raise TaskError(f"task message text is not canonical: {path}")
            _validate_timestamp(record["created_at"], "task message created_at")
            prior = record["previous_message_sha256"]
            if previous is None:
                if prior is not None:
                    raise TaskError(f"task message chain has an invalid origin: {path}")
            else:
                _validate_hash(prior, "task message previous_message_sha256")
                if prior != previous:
                    raise TaskError(f"task message chain mismatch: {path}")
            _validate_hash(record["message_sha256"], "task message message_sha256")
            if record["message_sha256"] != _record_sha256(
                record,
                "message_sha256",
            ):
                raise TaskError(f"task message hash mismatch: {path}")
            previous = str(record["message_sha256"])
            messages.append(record)
        return messages

    def collect(self, task_id: str) -> dict[str, object]:
        """Record reviewed child commit pointers without assimilating them."""
        self._validate_task_id(task_id)
        self._ensure_record_roots()
        publication_lock = self._publication_lock_path()
        _ensure_durable_lock_file(publication_lock, "task record publication lock")
        with file_lock(publication_lock):
            lifecycle_lock = self._lifecycle_lock_path(task_id)
            _ensure_durable_lock_file(lifecycle_lock, "task lifecycle lock")
            with file_lock(lifecycle_lock):
                self._reconcile_authoritative_briefs()
                brief = self._load_brief(task_id)
                self._load_bound_idempotency_index(brief)
                collected_path = self._collected_path(task_id)
                if _path_exists(collected_path):
                    ownership = self._load_ownership(
                        brief,
                        check_worktree=False,
                    )
                    return self._load_historical_collection(
                        brief,
                        ownership,
                    )
                if _path_exists(self._pruned_path(task_id)):
                    raise TaskError(f"pruned task is missing its collection: {task_id}")
                snapshot = self._collection_snapshot(brief)
                if snapshot.get("state") == "completed_no_return":
                    if _path_exists(collected_path):
                        raise TaskError(
                            f"task collection conflicts with missing return: {task_id}"
                        )
                    if self._collection_snapshot(brief) != snapshot:
                        raise TaskError(
                            f"task collection changed before no-return result: {task_id}"
                        )
                    return snapshot
                collected: dict[str, object] = {
                    "schema_version": 1,
                    "task_id": task_id,
                    "brief_sha256": brief["brief_sha256"],
                    "ownership_sha256": snapshot["ownership_sha256"],
                    "branch_ref": snapshot["branch_ref"],
                    "base_commit": brief["base_commit"],
                    "child_commit": snapshot["child_commit"],
                    "return_commit": snapshot["return_commit"],
                    "final_state": snapshot["final_state"],
                    "final_sha256": snapshot["final_sha256"],
                    "return": snapshot["return"],
                    "collected_at": utc_now(),
                }
                collected["collected_sha256"] = _record_sha256(
                    collected,
                    "collected_sha256",
                )

                if self._collection_snapshot(brief) != snapshot:
                    raise TaskError(
                        f"task collection changed before publication: {task_id}"
                    )
                if not create_json(collected_path, collected):
                    return self._load_collected(brief, snapshot)
                recorded = self._load_collected(brief, snapshot)
                if recorded != collected:
                    raise TaskError(f"task collection differs after write: {task_id}")
                return recorded

    def preserve(self, task_id: str) -> dict[str, object]:
        """Return an owned-worktree snapshot without changing child material."""
        self._validate_task_id(task_id)
        self._ensure_record_roots()
        publication_lock = self._publication_lock_path()
        _ensure_durable_lock_file(publication_lock, "task record publication lock")
        with file_lock(publication_lock):
            lifecycle_lock = self._lifecycle_lock_path(task_id)
            _ensure_durable_lock_file(lifecycle_lock, "task lifecycle lock")
            with file_lock(lifecycle_lock):
                self._reconcile_authoritative_briefs()
                brief = self._load_brief(task_id)
                self._load_bound_idempotency_index(brief)
                if _path_exists(self._pruned_path(task_id)):
                    raise TaskError(f"task worktree is already pruned: {task_id}")
                ownership = self._load_ownership(brief)
                head_commit, clean = self._owned_worktree_snapshot(ownership)
                return {
                    "schema_version": 1,
                    "task_id": task_id,
                    "state": "preserved",
                    "brief_sha256": brief["brief_sha256"],
                    "ownership_sha256": ownership["ownership_sha256"],
                    "actor": ownership["actor"],
                    "worktree_path": ownership["worktree_path"],
                    "branch_ref": f"refs/heads/{ownership['branch']}",
                    "base_commit": brief["base_commit"],
                    "head_commit": head_commit,
                    "clean": clean,
                }

    def prune(self, task_id: str) -> dict[str, object]:
        """Explicitly remove one clean collected worktree without deleting its branch."""
        self._validate_task_id(task_id)
        self._ensure_record_roots()
        publication_lock = self._publication_lock_path()
        _ensure_durable_lock_file(publication_lock, "task record publication lock")
        with file_lock(publication_lock):
            lifecycle_lock = self._lifecycle_lock_path(task_id)
            _ensure_durable_lock_file(lifecycle_lock, "task lifecycle lock")
            with file_lock(lifecycle_lock):
                self._reconcile_authoritative_briefs()
                brief = self._load_brief(task_id)
                self._load_bound_idempotency_index(brief)
                ownership = self._load_ownership(brief, check_worktree=False)
                if _path_exists(self._pruned_path(task_id)):
                    receipt = self._validated_pruned_status(brief, ownership)
                    self._require_collection_branch_tip(receipt)
                    return receipt
                if not _path_exists(self._collected_path(task_id)):
                    raise TaskError(
                        f"task must have a strict collection before prune: {task_id}"
                    )

                if _path_exists(self._prune_path(task_id)):
                    collected = self._load_historical_collection(
                        brief,
                        ownership,
                    )
                    self._require_collection_branch_tip(collected)
                    intent = self._load_prune_intent(brief, ownership, collected)
                    state = self._worktree_removal_state(ownership)
                    if state == "absent":
                        return self._create_pruned_receipt(
                            brief,
                            ownership,
                            collected,
                            intent,
                        )
                else:
                    ownership = self._load_ownership(brief)
                    snapshot = self._collection_snapshot(brief)
                    collected = self._load_collected(brief, snapshot)
                    intent = self._create_prune_intent(
                        brief,
                        ownership,
                        collected,
                    )

                ownership = self._load_ownership(brief)
                snapshot = self._collection_snapshot(brief)
                current = self._load_collected(brief, snapshot)
                if current != collected:
                    raise TaskError(f"task collection changed before prune: {task_id}")
                if self._load_prune_intent(brief, ownership, current) != intent:
                    raise TaskError(
                        f"task prune intent changed before removal: {task_id}"
                    )
                target = Path(str(ownership["worktree_path"]))
                self._remove_task_worktree(target)
                if self._worktree_removal_state(ownership) != "absent":
                    raise TaskError(f"task worktree remains after prune: {task_id}")
                after = self._load_historical_collection(brief, ownership)
                self._require_collection_branch_tip(after)
                if after != collected:
                    raise TaskError(f"task collection changed during prune: {task_id}")
                return self._create_pruned_receipt(
                    brief,
                    ownership,
                    after,
                    intent,
                )

    def _load_historical_collection(
        self,
        brief: dict[str, object],
        ownership: dict[str, object],
    ) -> dict[str, object]:
        task_id = str(brief["task_id"])
        status = self._load_task_status(brief, ownership, check_material=False)
        if status["state"] not in _TERMINAL_STATES:
            raise TaskError(f"task is not terminal for collection: {task_id}")
        launch = self._load_launch(brief, ownership)
        final = self._load_final(brief, ownership, launch)
        branch_ref = f"refs/heads/{ownership['branch']}"
        collected_raw = _read_object(
            self._collected_path(task_id),
            "task collection",
        )
        return_commit = collected_raw.get("return_commit")
        if (
            not isinstance(return_commit, str)
            or _COMMIT.fullmatch(return_commit) is None
        ):
            raise TaskError(f"invalid task collection return commit: {task_id}")
        reviewed = self._load_reviewed_return(
            brief,
            ownership,
            return_commit=return_commit,
        )
        snapshot = {
            "ownership_sha256": ownership["ownership_sha256"],
            "branch_ref": branch_ref,
            "child_commit": reviewed["child_commit"],
            "return_commit": reviewed["return_commit"],
            "final_state": final["state"],
            "final_sha256": final["final_sha256"],
            "return": reviewed["return"],
        }
        return self._load_collected(brief, snapshot)

    def _require_collection_branch_tip(
        self,
        collected: dict[str, object],
    ) -> None:
        task_id = str(collected["task_id"])
        branch_ref = str(collected["branch_ref"])
        return_commit = str(collected["return_commit"])
        branch_tip = self._safe_git_text(
            "rev-parse",
            "--verify",
            f"{branch_ref}^{{commit}}",
        )
        if branch_tip != return_commit:
            raise TaskError(f"task branch changed after collection: {task_id}")

    def _create_prune_intent(
        self,
        brief: dict[str, object],
        ownership: dict[str, object],
        collected: dict[str, object],
    ) -> dict[str, object]:
        task_id = str(brief["task_id"])
        intent: dict[str, object] = {
            "schema_version": 1,
            "task_id": task_id,
            "brief_sha256": brief["brief_sha256"],
            "ownership_sha256": ownership["ownership_sha256"],
            "collected_sha256": collected["collected_sha256"],
            "branch_ref": collected["branch_ref"],
            "return_commit": collected["return_commit"],
            "worktree_path": ownership["worktree_path"],
            "requested_at": utc_now(),
        }
        intent["prune_sha256"] = _record_sha256(intent, "prune_sha256")
        if not create_json(self._prune_path(task_id), intent):
            return self._load_prune_intent(brief, ownership, collected)
        recorded = self._load_prune_intent(brief, ownership, collected)
        if recorded != intent:
            raise TaskError(f"task prune intent differs after write: {task_id}")
        return recorded

    def _load_prune_intent(
        self,
        brief: dict[str, object],
        ownership: dict[str, object],
        collected: dict[str, object],
    ) -> dict[str, object]:
        task_id = str(brief["task_id"])
        path = self._prune_path(task_id)
        _require_restrictive_plain_file(
            path,
            "task prune intent",
            permissions_enforced=self._filesystem_permissions_enforced,
        )
        intent = _read_object(path, "task prune intent")
        if (
            set(intent) != _PRUNE_FIELDS
            or type(intent.get("schema_version")) is not int
        ):
            raise TaskError(f"invalid task prune intent schema: {task_id}")
        if intent["schema_version"] != 1 or intent["task_id"] != task_id:
            raise TaskError(f"task prune intent identity mismatch: {task_id}")
        for field in (
            "brief_sha256",
            "ownership_sha256",
            "collected_sha256",
            "prune_sha256",
        ):
            _validate_hash(intent[field], f"task prune intent {field}")
        expected = {
            "brief_sha256": brief["brief_sha256"],
            "ownership_sha256": ownership["ownership_sha256"],
            "collected_sha256": collected["collected_sha256"],
            "branch_ref": collected["branch_ref"],
            "return_commit": collected["return_commit"],
            "worktree_path": ownership["worktree_path"],
        }
        if any(intent[field] != value for field, value in expected.items()):
            raise TaskError(f"task prune intent binding mismatch: {task_id}")
        _validate_timestamp(intent["requested_at"], "task prune requested_at")
        if intent["prune_sha256"] != _record_sha256(intent, "prune_sha256"):
            raise TaskError(f"task prune intent hash mismatch: {task_id}")
        return intent

    def _create_pruned_receipt(
        self,
        brief: dict[str, object],
        ownership: dict[str, object],
        collected: dict[str, object],
        intent: dict[str, object],
    ) -> dict[str, object]:
        task_id = str(brief["task_id"])
        receipt: dict[str, object] = {
            "schema_version": 1,
            "task_id": task_id,
            "state": "pruned",
            "brief_sha256": brief["brief_sha256"],
            "ownership_sha256": ownership["ownership_sha256"],
            "collected_sha256": collected["collected_sha256"],
            "branch_ref": collected["branch_ref"],
            "return_commit": collected["return_commit"],
            "final_state": collected["final_state"],
            "final_sha256": collected["final_sha256"],
            "worktree_path": ownership["worktree_path"],
            "removed_at": utc_now(),
            "prune_sha256": intent["prune_sha256"],
        }
        receipt["pruned_sha256"] = _record_sha256(receipt, "pruned_sha256")
        if not create_json(self._pruned_path(task_id), receipt):
            return self._load_pruned_receipt(
                brief,
                ownership,
                collected,
                intent,
            )
        recorded = self._load_pruned_receipt(
            brief,
            ownership,
            collected,
            intent,
        )
        if recorded != receipt:
            raise TaskError(f"task pruned receipt differs after write: {task_id}")
        return recorded

    def _load_pruned_receipt(
        self,
        brief: dict[str, object],
        ownership: dict[str, object],
        collected: dict[str, object],
        intent: dict[str, object],
    ) -> dict[str, object]:
        task_id = str(brief["task_id"])
        path = self._pruned_path(task_id)
        _require_restrictive_plain_file(
            path,
            "task pruned receipt",
            permissions_enforced=self._filesystem_permissions_enforced,
        )
        receipt = _read_object(path, "task pruned receipt")
        if (
            set(receipt) != _PRUNED_FIELDS
            or type(receipt.get("schema_version")) is not int
        ):
            raise TaskError(f"invalid task pruned receipt schema: {task_id}")
        if (
            receipt["schema_version"] != 1
            or receipt["task_id"] != task_id
            or receipt["state"] != "pruned"
        ):
            raise TaskError(f"task pruned receipt identity mismatch: {task_id}")
        for field in (
            "brief_sha256",
            "ownership_sha256",
            "collected_sha256",
            "final_sha256",
            "prune_sha256",
            "pruned_sha256",
        ):
            _validate_hash(receipt[field], f"task pruned receipt {field}")
        expected = {
            "brief_sha256": brief["brief_sha256"],
            "ownership_sha256": ownership["ownership_sha256"],
            "collected_sha256": collected["collected_sha256"],
            "branch_ref": collected["branch_ref"],
            "return_commit": collected["return_commit"],
            "final_state": collected["final_state"],
            "final_sha256": collected["final_sha256"],
            "worktree_path": ownership["worktree_path"],
            "prune_sha256": intent["prune_sha256"],
        }
        if any(receipt[field] != value for field, value in expected.items()):
            raise TaskError(f"task pruned receipt binding mismatch: {task_id}")
        _validate_timestamp(receipt["removed_at"], "task pruned removed_at")
        if receipt["pruned_sha256"] != _record_sha256(
            receipt,
            "pruned_sha256",
        ):
            raise TaskError(f"task pruned receipt hash mismatch: {task_id}")
        return receipt

    def _validated_pruned_status(
        self,
        brief: dict[str, object],
        ownership: dict[str, object],
    ) -> dict[str, object]:
        collected = self._load_historical_collection(brief, ownership)
        intent = self._load_prune_intent(brief, ownership, collected)
        receipt = self._load_pruned_receipt(
            brief,
            ownership,
            collected,
            intent,
        )
        if self._worktree_removal_state(ownership) != "absent":
            raise TaskError(
                f"pruned task worktree unexpectedly exists: {brief['task_id']}"
            )
        return receipt

    def _worktree_removal_state(
        self,
        ownership: dict[str, object],
    ) -> str:
        target = Path(str(ownership["worktree_path"]))
        branch_ref = f"refs/heads/{ownership['branch']}"
        registrations = self._worktree_registrations()
        path_matches = [
            item
            for item in registrations
            if self._same_path(str(item["worktree"]), target)
        ]
        branch_matches = [
            item for item in registrations if item.get("branch") == branch_ref
        ]
        exists = _path_exists(target)
        if exists and len(path_matches) == 1 and path_matches == branch_matches:
            return "present"
        if not exists and not path_matches and not branch_matches:
            return "absent"
        raise TaskError(f"task worktree removal state is ambiguous: {target}")

    def _remove_task_worktree(self, target: Path) -> None:
        result = self._safe_git_result("worktree", "remove", str(target))
        if result.returncode != 0:
            raise TaskError(f"unable to prune task worktree: {_git_error(result)}")

    def _collection_snapshot(
        self,
        brief: dict[str, object],
    ) -> dict[str, object]:
        task_id = str(brief["task_id"])
        status = self._status_unlocked(task_id)
        if status["state"] not in _TERMINAL_STATES:
            raise TaskError(f"task is not terminal for collection: {task_id}")
        ownership = self._load_ownership(brief)
        launch = self._load_launch(brief, ownership)
        final = self._load_final(brief, ownership, launch)
        if status["state"] != final["state"]:
            raise TaskError(f"task terminal state changed during collection: {task_id}")
        return_commit = self._require_clean_owned_worktree(ownership)
        relative = f"tasks/{task_id}/return.json"
        if (
            self._safe_git_result(
                "cat-file",
                "-e",
                f"{return_commit}:{relative}",
            ).returncode
            != 0
        ):
            if final["state"] != "completed":
                raise TaskError(
                    f"terminal task requires a valid return for collection: {task_id}"
                )
            return {
                "schema_version": 1,
                "task_id": task_id,
                "state": "completed_no_return",
                "brief_sha256": brief["brief_sha256"],
                "ownership_sha256": ownership["ownership_sha256"],
                "branch_ref": f"refs/heads/{ownership['branch']}",
                "base_commit": brief["base_commit"],
                "head_commit": return_commit,
                "final_state": final["state"],
                "final_sha256": final["final_sha256"],
            }
        reviewed = self._load_reviewed_return(
            brief,
            ownership,
            return_commit=return_commit,
        )
        return {
            "ownership_sha256": ownership["ownership_sha256"],
            "branch_ref": f"refs/heads/{ownership['branch']}",
            "child_commit": reviewed["child_commit"],
            "return_commit": reviewed["return_commit"],
            "final_state": final["state"],
            "final_sha256": final["final_sha256"],
            "return": reviewed["return"],
        }

    def _load_reviewed_return(
        self,
        brief: dict[str, object],
        ownership: dict[str, object],
        *,
        return_commit: str | None = None,
    ) -> dict[str, object]:
        task_id = str(brief["task_id"])
        if return_commit is None:
            return_commit = self._require_clean_owned_worktree(ownership)
        parent_line = self._safe_git_text(
            "rev-list",
            "--parents",
            "-n",
            "1",
            return_commit,
        ).split()
        if len(parent_line) != 2 or parent_line[0] != return_commit:
            raise TaskError(f"task return HEAD must have exactly one parent: {task_id}")
        child_commit = parent_line[1]
        if _COMMIT.fullmatch(child_commit) is None or not self._is_commit(child_commit):
            raise TaskError(f"task child commit is invalid: {task_id}")
        base_commit = str(brief["base_commit"])
        if not self._safe_git_success(
            "merge-base",
            "--is-ancestor",
            base_commit,
            child_commit,
        ):
            raise TaskError(f"task child commit does not descend from base: {task_id}")

        relative = f"tasks/{task_id}/return.json"
        prior = self._safe_git_result("cat-file", "-e", f"{child_commit}:{relative}")
        if prior.returncode == 0:
            raise TaskError(f"task child commit already contains its return: {task_id}")
        changed_in_return = self._safe_git_bytes(
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            "--name-only",
            "-z",
            child_commit,
            return_commit,
            "--",
        )
        if changed_in_return != relative.encode("utf-8") + b"\0":
            raise TaskError(f"task return commit must change only {relative}")
        tree = self._safe_git_bytes(
            "ls-tree",
            "-z",
            return_commit,
            "--",
            relative,
        )
        entries = tree.split(b"\0")
        if len(entries) != 2 or entries[1] or not entries[0]:
            raise TaskError(f"task return path is ambiguous in Git tree: {task_id}")
        metadata, separator, path = entries[0].partition(b"\t")
        fields = metadata.split(b" ")
        if (
            not separator
            or len(fields) != 3
            or path != relative.encode("utf-8")
            or fields[0] not in {b"100644", b"100755"}
            or fields[1] != b"blob"
        ):
            raise TaskError(f"task return path must be a regular Git file: {task_id}")
        blob = self._safe_git_bytes(
            "cat-file",
            "blob",
            f"{return_commit}:{relative}",
        )
        try:
            value = json.loads(blob.decode("utf-8"))
        except (UnicodeError, ValueError) as error:
            raise TaskError(f"unable to read task return blob: {task_id}") from error
        if not isinstance(value, dict):
            raise TaskError(f"invalid task return: {task_id}")
        returned: dict[str, object] = value
        self._validate_return(
            returned,
            brief,
            child_commit=child_commit,
        )
        return {
            "child_commit": child_commit,
            "return_commit": return_commit,
            "return": returned,
        }

    def _validate_return(
        self,
        returned: dict[str, object],
        brief: dict[str, object],
        *,
        child_commit: str,
    ) -> None:
        task_id = str(brief["task_id"])
        if (
            set(returned) != _RETURN_FIELDS
            or type(returned.get("schema_version")) is not int
        ):
            raise TaskError(f"invalid task return schema: {task_id}")
        if returned["schema_version"] != 1 or returned["task_id"] != task_id:
            raise TaskError(f"task return identity mismatch: {task_id}")
        _validate_hash(returned["brief_sha256"], "task return brief_sha256")
        if (
            returned["brief_sha256"] != brief["brief_sha256"]
            or returned["base_commit"] != brief["base_commit"]
            or returned["child_commit"] != child_commit
        ):
            raise TaskError(f"task return lineage mismatch: {task_id}")
        summary = _validate_text(returned["summary"], "task return summary")
        if summary != returned["summary"]:
            raise TaskError(f"task return summary is not canonical: {task_id}")
        for field in (
            "work_performed",
            "changed_files",
            "evidence",
            "deviations",
            "uncertainty",
            "follow_up",
        ):
            _validate_string_list(returned[field], f"task return {field}")
        changed_files = self._changed_files(
            str(brief["base_commit"]),
            child_commit,
        )
        if returned["changed_files"] != changed_files:
            raise TaskError(f"task return changed_files mismatch: {task_id}")
        _validate_hash(returned["return_sha256"], "task return return_sha256")
        if returned["return_sha256"] != _record_sha256(
            returned,
            "return_sha256",
        ):
            raise TaskError(f"task return hash mismatch: {task_id}")

    def _changed_files(self, base_commit: str, child_commit: str) -> list[str]:
        raw = self._safe_git_bytes(
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            "--name-only",
            "-z",
            base_commit,
            child_commit,
            "--",
        )
        if raw and not raw.endswith(b"\0"):
            raise TaskError("Git returned an ambiguous changed-files list")
        encoded = [item for item in raw.split(b"\0") if item]
        try:
            return [item.decode("utf-8") for item in sorted(encoded)]
        except UnicodeError as error:
            raise TaskError("task changed files must be valid UTF-8") from error

    def _require_clean_owned_worktree(
        self,
        ownership: dict[str, object],
    ) -> str:
        tip, clean = self._owned_worktree_snapshot(ownership)
        if not clean:
            raise TaskError(
                f"task worktree is not completely clean: {ownership['worktree_path']}"
            )
        return tip

    def _owned_worktree_snapshot(
        self,
        ownership: dict[str, object],
    ) -> tuple[str, bool]:
        target = Path(str(ownership["worktree_path"]))
        tip = self._validate_worktree(
            target,
            str(ownership["branch"]),
            str(ownership["base_commit"]),
        )
        child_git_dir = self._worktree_git_directory(target)
        dirty = self._safe_git_bytes(
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--ignored=matching",
            "--ignore-submodules=none",
            git_dir=child_git_dir,
            work_tree=target,
        )
        index_entries = self._safe_git_bytes(
            "ls-files",
            "-v",
            "-z",
            git_dir=child_git_dir,
            work_tree=target,
        )
        ambiguous_index = any(
            record[:1] == b"S" or record[:1].islower()
            for record in index_entries.split(b"\0")
            if record
        )
        if (
            self._validate_worktree(
                target,
                str(ownership["branch"]),
                str(ownership["base_commit"]),
            )
            != tip
        ):
            raise TaskError(
                f"task worktree HEAD changed while checking clean: {target}"
            )
        return tip, not dirty and not ambiguous_index

    def _load_collected(
        self,
        brief: dict[str, object],
        snapshot: dict[str, object],
    ) -> dict[str, object]:
        task_id = str(brief["task_id"])
        path = self._collected_path(task_id)
        _require_single_link_plain_file(path, "task collection")
        collected = _read_object(path, "task collection")
        if (
            set(collected) != _COLLECTED_FIELDS
            or type(collected.get("schema_version")) is not int
        ):
            raise TaskError(f"invalid task collection schema: {task_id}")
        if collected["schema_version"] != 1 or collected["task_id"] != task_id:
            raise TaskError(f"task collection identity mismatch: {task_id}")
        for field in (
            "brief_sha256",
            "ownership_sha256",
            "final_sha256",
            "collected_sha256",
        ):
            _validate_hash(collected[field], f"task collection {field}")
        expected = {
            "brief_sha256": brief["brief_sha256"],
            "ownership_sha256": snapshot["ownership_sha256"],
            "branch_ref": snapshot["branch_ref"],
            "base_commit": brief["base_commit"],
            "child_commit": snapshot["child_commit"],
            "return_commit": snapshot["return_commit"],
            "final_state": snapshot["final_state"],
            "final_sha256": snapshot["final_sha256"],
            "return": snapshot["return"],
        }
        if any(collected[field] != value for field, value in expected.items()):
            raise TaskError(f"task collection binding conflict: {task_id}")
        _validate_timestamp(collected["collected_at"], "task collection collected_at")
        if collected["collected_sha256"] != _record_sha256(
            collected,
            "collected_sha256",
        ):
            raise TaskError(f"task collection hash mismatch: {task_id}")
        return collected

    def _status_unlocked(self, task_id: str) -> dict[str, object]:
        brief = self._load_brief(task_id)
        self._load_bound_idempotency_index(brief)
        if _path_exists(self._pruned_path(task_id)):
            ownership = self._load_ownership(brief, check_worktree=False)
            return self._validated_pruned_status(brief, ownership)
        ownership = (
            self._load_ownership(brief)
            if _path_exists(self._ownership_path(task_id))
            else None
        )
        if ownership is not None and _path_exists(self._launch_path(task_id)):
            launch = self._load_launch(brief, ownership)
            return self._reconcile_execution(brief, ownership, launch)
        return self._load_task_status(brief, ownership)

    def _load_task_status(
        self,
        brief: dict[str, object],
        ownership: dict[str, object] | None,
        *,
        check_material: bool = True,
    ) -> dict[str, object]:
        task_id = str(brief["task_id"])
        _require_plain_directory(self.root / ".aros", "AROS runtime directory")
        _require_plain_directory(
            self.root / ".aros" / "tasks", "runtime tasks directory"
        )
        runtime_directory = self.root / ".aros" / "tasks" / task_id
        _require_plain_directory(runtime_directory, "task runtime directory")
        status = _read_object(runtime_directory / "status.json", "task status")
        state = status.get("state")
        if state not in {"prepared", "worktree_ready"}:
            from .task_runner import validate_execution_status

            if ownership is None:
                raise TaskError(
                    f"invalid task status: launched task ownership is missing: {task_id}"
                )
            return validate_execution_status(
                self,
                brief,
                ownership,
                status,
            )
        fields = _READY_STATUS_FIELDS if state == "worktree_ready" else _STATUS_FIELDS
        if set(status) != fields or type(status.get("schema_version")) is not int:
            raise TaskError(f"invalid task status schema: {task_id}")
        if status["schema_version"] != 1:
            raise TaskError(f"invalid task status schema version: {task_id}")
        if status["task_id"] != task_id:
            raise TaskError(f"task status identity mismatch: {task_id}")
        _validate_hash(status["brief_sha256"], "task status brief_sha256")
        if status["brief_sha256"] != brief["brief_sha256"]:
            raise TaskError(f"task status brief hash mismatch: {task_id}")
        _validate_timestamp(status["updated_at"], "task status updated_at")
        if state == "prepared":
            if ownership is not None:
                raise TaskError(f"prepared task has unreconciled ownership: {task_id}")
            if status["updated_at"] != brief["created_at"]:
                raise TaskError(f"task status timestamp mismatch: {task_id}")
            if check_material:
                self._require_no_unowned_worktree_material(brief)
        elif state == "worktree_ready":
            if ownership is None:
                raise TaskError(f"worktree-ready task ownership is missing: {task_id}")
            _validate_hash(
                status["ownership_sha256"],
                "task status ownership_sha256",
            )
            if status["ownership_sha256"] != ownership["ownership_sha256"]:
                raise TaskError(f"task status ownership hash mismatch: {task_id}")
            if status["updated_at"] != ownership["acquired_at"]:
                raise TaskError(f"task status timestamp mismatch: {task_id}")
        return status

    def list(self) -> list[dict[str, object]]:
        """Return task statuses in stable task-ID order."""
        versioned = self._versioned_task_ids()
        runtime = self._runtime_task_ids()
        if not versioned and not runtime:
            return []
        self._ensure_record_roots()
        publication_lock = self._publication_lock_path()
        _ensure_durable_lock_file(publication_lock, "task record publication lock")
        with file_lock(publication_lock):
            self._reconcile_authoritative_briefs()
            return self._list_unlocked()

    def _list_unlocked(self) -> list[dict[str, object]]:
        versioned = self._versioned_task_ids()
        runtime = self._runtime_task_ids()
        if versioned != runtime:
            raise TaskError("task record inventory conflict between versioned and runtime paths")
        statuses: list[dict[str, object]] = []
        for task_id in sorted(versioned):
            lifecycle_lock = self._lifecycle_lock_path(task_id)
            _ensure_durable_lock_file(lifecycle_lock, "task lifecycle lock")
            with file_lock(lifecycle_lock):
                statuses.append(self._status_unlocked(task_id))
        return statuses

    def _load_brief(self, task_id: str) -> dict[str, object]:
        self._validate_task_id(task_id)
        _require_plain_directory(self.root / "tasks", "versioned tasks directory")
        directory = self.root / "tasks" / task_id
        _require_plain_directory(directory, "task brief directory")
        brief = _read_object(directory / "brief.json", "task brief")
        if set(brief) != _BRIEF_FIELDS or type(brief.get("schema_version")) is not int:
            raise TaskError(f"invalid task brief schema: {task_id}")
        if brief["schema_version"] != 1:
            raise TaskError(f"invalid task brief schema version: {task_id}")
        if brief["task_id"] != task_id:
            raise TaskError(f"task brief identity mismatch: {task_id}")
        _validate_hash(brief["brief_sha256"], "task brief_sha256")
        if brief["brief_sha256"] != _brief_sha256(brief):
            raise TaskError(f"task brief hash mismatch: {task_id}")
        if not isinstance(brief["base_commit"], str) or _COMMIT.fullmatch(brief["base_commit"]) is None:
            raise TaskError(f"invalid task brief base_commit: {task_id}")
        _validate_timestamp(brief["created_at"], "task brief created_at")
        normalized = _normalize_request(
            **{field: brief[field] for field in _REQUEST_FIELDS}  # type: ignore[arg-type]
        )
        if any(brief[field] != normalized[field] for field in _REQUEST_FIELDS):
            raise TaskError(f"task brief request is not canonical: {task_id}")
        _validate_hash(brief["request_sha256"], "task brief request_sha256")
        if brief["request_sha256"] != json_sha256(normalized):
            raise TaskError(f"task brief request hash mismatch: {task_id}")
        return brief

    def _load_idempotency_index(
        self,
        path: Path,
        key_digest: str,
    ) -> dict[str, object]:
        index = _read_object(path, "task idempotency index")
        if set(index) != _INDEX_FIELDS or type(index.get("schema_version")) is not int:
            raise TaskError("invalid task idempotency index schema")
        if index["schema_version"] != 1:
            raise TaskError("invalid task idempotency index schema version")
        for field in ("idempotency_key_sha256", "request_sha256", "brief_sha256"):
            try:
                _validate_hash(index[field], f"task idempotency index {field}")
            except TaskError as error:
                raise TaskError("invalid task idempotency index hash") from error
        if index["idempotency_key_sha256"] != key_digest:
            raise TaskError("invalid task idempotency index key hash")
        if not _valid_task_id(index["task_id"]):
            raise TaskError("invalid task idempotency index task identity")
        try:
            _validate_timestamp(index["created_at"], "task idempotency index created_at")
        except TaskError as error:
            raise TaskError("invalid task idempotency index timestamp") from error
        return index

    def _validate_index_binding(
        self,
        index: dict[str, object],
        brief: dict[str, object],
    ) -> None:
        if (
            index["task_id"] != brief["task_id"]
            or index["request_sha256"] != brief["request_sha256"]
            or index["brief_sha256"] != brief["brief_sha256"]
            or index["created_at"] != brief["created_at"]
        ):
            raise TaskError("invalid task idempotency index binding")

    def _load_bound_idempotency_index(
        self,
        brief: dict[str, object],
    ) -> dict[str, object]:
        key = str(brief["idempotency_key"])
        key_digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        _require_plain_directory(self.root / ".aros", "AROS runtime directory")
        _require_plain_directory(
            self.root / ".aros" / "tasks", "runtime tasks directory"
        )
        _require_plain_directory(
            self.root / ".aros" / "tasks" / "idempotency",
            "task idempotency directory",
        )
        index = self._load_idempotency_index(
            self._idempotency_index_path(key),
            key_digest,
        )
        self._validate_index_binding(index, brief)
        return index

    def _recover_prepared_records(
        self,
        brief: dict[str, object],
    ) -> None:
        task_id = str(brief["task_id"])
        key = str(brief["idempotency_key"])
        key_digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        runtime_path = self.root / ".aros" / "tasks" / task_id
        if not _path_exists(runtime_path):
            _create_plain_directory(runtime_path, "runtime task path")
        else:
            _require_plain_directory(runtime_path, "task runtime directory")
        status_path = runtime_path / "status.json"
        ownership_path = self._ownership_path(task_id)
        index_path = self._idempotency_index_path(key)
        if not _path_exists(index_path):
            create_json(index_path, _idempotency_index(brief, key_digest))
        if _path_exists(self._pruned_path(task_id)):
            ownership = self._load_ownership(brief, check_worktree=False)
            self._validated_pruned_status(brief, ownership)
            self._load_bound_idempotency_index(brief)
            return
        if _path_exists(self._prune_path(task_id)):
            ownership = self._load_ownership(brief, check_worktree=False)
            removal_state = self._worktree_removal_state(ownership)
            collected = self._load_historical_collection(
                brief,
                ownership,
            )
            self._require_collection_branch_tip(collected)
            intent = self._load_prune_intent(brief, ownership, collected)
            if removal_state == "absent":
                self._create_pruned_receipt(
                    brief,
                    ownership,
                    collected,
                    intent,
                )
                self._load_bound_idempotency_index(brief)
                return
        ownership = (
            self._load_ownership(brief)
            if _path_exists(ownership_path)
            else None
        )
        if ownership is not None and _path_exists(self._launch_path(task_id)):
            launch = self._load_launch(brief, ownership)
            self._reconcile_execution(brief, ownership, launch)
            self._load_bound_idempotency_index(brief)
            return
        if not _path_exists(status_path):
            if ownership is not None:
                self._validate_new_checkout(
                    Path(str(ownership["worktree_path"])),
                    str(ownership["branch"]),
                    str(ownership["base_commit"]),
                )
            derived = (
                _worktree_ready_status(brief, ownership)
                if ownership is not None
                else _prepared_status(brief)
            )
            create_json(status_path, derived)
        elif ownership is not None:
            status = _read_object(status_path, "task status")
            if status.get("state") == "prepared":
                self._load_task_status(
                    brief,
                    None,
                    check_material=False,
                )
                self._validate_new_checkout(
                    Path(str(ownership["worktree_path"])),
                    str(ownership["branch"]),
                    str(ownership["base_commit"]),
                )
                atomic_write_json(
                    status_path,
                    _worktree_ready_status(brief, ownership),
                )
        self._load_task_status(brief, ownership)
        self._load_bound_idempotency_index(brief)

    def _reconcile_authoritative_briefs(self) -> None:
        versioned = self._versioned_task_ids()
        runtime = self._runtime_task_ids()
        if runtime - versioned:
            raise TaskError(
                "task record inventory conflict: runtime state has no versioned brief"
            )
        for task_id in sorted(versioned):
            self._reconcile_staging_alias(task_id)
            brief = self._load_brief(task_id)
            self._recover_prepared_records(brief)
        self._validate_inventory()

    def _reconcile_staging_alias(self, task_id: str) -> None:
        staging = self.root / "tasks" / _TASK_STAGING_DIRECTORY / task_id
        if not _path_exists(staging):
            return
        authoritative = self.root / "tasks" / task_id / "brief.json"
        staged_brief = staging / "brief.json"
        try:
            staging_identity = _plain_directory_identity(
                staging,
                "versioned task staging path",
            )
            if not _path_exists(staged_brief):
                self._remove_empty_staging_directory(staging, staging_identity)
                return
            authoritative_identity = _plain_file_identity(
                authoritative,
                "authoritative task brief",
            )
            staged_identity = _plain_file_identity(
                staged_brief,
                "staged task brief",
            )
        except TaskError as error:
            raise TaskError(f"ambiguous task staging material: {staging}") from error
        if staged_identity != authoritative_identity:
            raise TaskError(f"ambiguous task staging brief identity: {staging}")
        self._remove_staging_alias(
            staging,
            staging_identity,
            staged_brief,
            staged_identity,
            authoritative,
        )

    def _publish_staged_brief(self, staging: Path, target: Path) -> None:
        staged_brief = staging / "brief.json"
        _require_plain_directory(staging, "versioned task staging path")
        staging_identity = _plain_directory_identity(
            staging,
            "versioned task staging path",
        )
        staged_identity = _plain_file_identity(staged_brief, "staged task brief")
        _require_absent(target, "versioned task path")
        _create_plain_directory(target, "versioned task path")
        published_brief = target / "brief.json"
        try:
            os.link(staged_brief, published_brief, follow_symlinks=False)
        except OSError as error:
            raise TaskError(f"task brief publication failed: {error}") from error
        if _plain_file_identity(published_brief, "published task brief") != staged_identity:
            raise TaskError("published task brief does not bind to staged brief")
        _fsync_directory(target)
        _fsync_directory(target.parent)
        self._remove_staging_alias(
            staging,
            staging_identity,
            staged_brief,
            staged_identity,
            published_brief,
        )
        _require_plain_directory(target, "versioned task path")
        _require_plain_file(published_brief, "published task brief")

    def _remove_staging_alias(
        self,
        staging: Path,
        staging_identity: tuple[int, int],
        staged_brief: Path,
        staged_identity: tuple[int, int],
        published_brief: Path,
    ) -> None:
        if _plain_directory_identity(
            staging,
            "versioned task staging path",
        ) != staging_identity:
            raise TaskError("task staging directory identity changed after publication")
        if (
            _plain_file_identity(staged_brief, "staged task brief") != staged_identity
            or _plain_file_identity(published_brief, "published task brief")
            != staged_identity
        ):
            raise TaskError("task staging alias identity changed after publication")
        try:
            staged_brief.unlink()
        except OSError as error:
            raise TaskError(f"unable to remove staged task brief alias: {staged_brief}") from error
        _fsync_directory(staging)

        self._remove_empty_staging_directory(staging, staging_identity)

    def _remove_empty_staging_directory(
        self,
        staging: Path,
        staging_identity: tuple[int, int],
    ) -> None:
        try:
            has_unexpected_content = any(staging.iterdir())
        except OSError as error:
            raise TaskError(f"unable to inspect task staging directory: {staging}") from error
        if has_unexpected_content:
            raise TaskError(f"task staging directory contains ambiguous material: {staging}")
        if _plain_directory_identity(
            staging,
            "versioned task staging path",
        ) != staging_identity:
            raise TaskError("task staging directory identity changed during cleanup")
        try:
            staging.rmdir()
        except OSError as error:
            raise TaskError(f"unable to remove empty task staging directory: {staging}") from error
        _fsync_directory(staging.parent)

    def _brief_for_idempotency_key(self, key: str) -> dict[str, object] | None:
        matches = [
            brief
            for task_id in sorted(self._versioned_task_ids())
            if (brief := self._load_brief(task_id))["idempotency_key"] == key
        ]
        if len(matches) > 1:
            raise TaskError("duplicate task briefs use the same idempotency key")
        return matches[0] if matches else None

    def _load_ownership(
        self,
        brief: dict[str, object],
        *,
        check_worktree: bool = True,
    ) -> dict[str, object]:
        task_id = str(brief["task_id"])
        ownership = _read_object(
            self._ownership_path(task_id),
            "task ownership",
        )
        if (
            set(ownership) != _OWNERSHIP_FIELDS
            or type(ownership.get("schema_version")) is not int
        ):
            raise TaskError(f"invalid task ownership schema: {task_id}")
        if ownership["schema_version"] != 1:
            raise TaskError(f"invalid task ownership schema version: {task_id}")
        if ownership["task_id"] != task_id:
            raise TaskError(f"task ownership identity mismatch: {task_id}")
        _validate_hash(ownership["brief_sha256"], "task ownership brief_sha256")
        if ownership["brief_sha256"] != brief["brief_sha256"]:
            raise TaskError(f"task ownership brief hash mismatch: {task_id}")
        actor = _validate_text(ownership["actor"], "task ownership actor")
        if actor != ownership["actor"]:
            raise TaskError(f"task ownership actor is not canonical: {task_id}")
        target = self._worktree_target(task_id, create_roots=False)
        if ownership["worktree_path"] != str(target):
            raise TaskError(f"task ownership worktree path mismatch: {task_id}")
        branch = f"aros/task/{task_id}"
        if ownership["branch"] != branch:
            raise TaskError(f"task ownership branch mismatch: {task_id}")
        if ownership["base_commit"] != brief["base_commit"]:
            raise TaskError(f"task ownership base commit mismatch: {task_id}")
        parent_head = ownership["parent_head"]
        if not isinstance(parent_head, str) or _COMMIT.fullmatch(parent_head) is None:
            raise TaskError(f"invalid task ownership parent HEAD: {task_id}")
        if not self._is_commit(parent_head):
            raise TaskError(f"task ownership parent HEAD is not a commit: {task_id}")
        _validate_timestamp(ownership["acquired_at"], "task ownership acquired_at")
        _validate_hash(
            ownership["ownership_sha256"],
            "task ownership ownership_sha256",
        )
        if ownership["ownership_sha256"] != _ownership_sha256(ownership):
            raise TaskError(f"task ownership hash mismatch: {task_id}")
        if check_worktree:
            self._validate_owned_worktree(ownership)
        return ownership

    def _load_launch(
        self,
        brief: dict[str, object],
        ownership: dict[str, object],
    ) -> dict[str, object]:
        from .task_runner import load_launch

        return load_launch(self, brief, ownership)

    def _load_final(
        self,
        brief: dict[str, object],
        ownership: dict[str, object],
        launch: dict[str, object],
    ) -> dict[str, object]:
        from .task_runner import load_final

        return load_final(self, brief, ownership, launch)

    def _reconcile_execution(
        self,
        brief: dict[str, object],
        ownership: dict[str, object],
        launch: dict[str, object],
    ) -> dict[str, object]:
        from .task_runner import (
            adapter_claim_is_live,
            execution_claim_is_live,
            launched_status,
            load_adapter_claim,
            load_execution_claim,
            lost_status,
            process_status_is_live,
            running_status_from,
            running_status_from_claims,
            terminal_status,
        )

        task_id = str(brief["task_id"])
        final_path = self._final_path(task_id)
        if _path_exists(final_path):
            final = self._load_final(brief, ownership, launch)
            terminal = terminal_status(brief, ownership, launch, final)
            current = (
                _read_object(self._status_path(task_id), "task status")
                if _path_exists(self._status_path(task_id))
                else None
            )
            if current != terminal:
                atomic_write_json(self._status_path(task_id), terminal)
            return self._load_task_status(brief, ownership)

        raw_status = (
            _read_object(self._status_path(task_id), "task status")
            if _path_exists(self._status_path(task_id))
            else None
        )
        if raw_status is not None and raw_status.get("state") in {
            "launched",
            "running",
            "lost",
        }:
            status = self._load_task_status(brief, ownership)
            if process_status_is_live(status):
                running = running_status_from(status)
                if running != status:
                    atomic_write_json(self._status_path(task_id), running)
                return running
        elif raw_status is not None and raw_status.get("state") in _TERMINAL_STATES:
            raise TaskError(f"terminal task is missing its final receipt: {task_id}")

        execution = None
        adapter = None
        if _path_exists(self._adapter_path(task_id)):
            execution = load_execution_claim(self, brief, ownership, launch)
            adapter = load_adapter_claim(
                self,
                brief,
                ownership,
                launch,
                execution,
            )
            if adapter_claim_is_live(adapter):
                running = running_status_from_claims(
                    brief,
                    ownership,
                    launch,
                    execution,
                    adapter,
                )
                atomic_write_json(self._status_path(task_id), running)
                return self._load_task_status(brief, ownership)
        elif _path_exists(self._execution_path(task_id)):
            execution = load_execution_claim(self, brief, ownership, launch)

        if execution is not None and execution_claim_is_live(execution):
            if adapter is not None:
                running = running_status_from_claims(
                    brief,
                    ownership,
                    launch,
                    execution,
                    adapter,
                )
                atomic_write_json(self._status_path(task_id), running)
                return self._load_task_status(brief, ownership)
            launched = launched_status(brief, ownership, launch)
            if raw_status != launched:
                atomic_write_json(self._status_path(task_id), launched)
            return launched

        if _seconds_since(str(launch["launched_at"])) < _LAUNCH_GRACE_SECONDS:
            launched = launched_status(brief, ownership, launch)
            if raw_status != launched:
                atomic_write_json(self._status_path(task_id), launched)
            return launched

        lost = lost_status(brief, ownership, launch, raw_status)
        if raw_status != lost:
            atomic_write_json(self._status_path(task_id), lost)
        return lost

    def _prepare_execution_paths(
        self,
        runtime: Path,
        *,
        reuse_logs: bool = False,
        filesystem_permissions_enforced: bool | None = None,
    ) -> None:
        if filesystem_permissions_enforced is None:
            filesystem_permissions_enforced = (
                self._filesystem_permissions_enforced
            )
        if type(filesystem_permissions_enforced) is not bool:
            raise TaskError("invalid task filesystem permission policy")
        _require_plain_directory(runtime, "task runtime directory")
        home = runtime / "home"
        temporary = runtime / "tmp"
        import_root = runtime / "runner-import"
        if reuse_logs:
            _require_directory_entries(home, "task adapter HOME", set())
            _require_directory_entries(temporary, "task adapter TMPDIR", set())
            _require_directory_entries(
                import_root,
                "task runner import root",
                {"arbor"},
            )
        else:
            _create_plain_directory(home, "task adapter HOME")
            _create_plain_directory(temporary, "task adapter TMPDIR")
            _create_plain_directory(import_root, "task runner import root")
        package_alias = import_root / "arbor"
        package_source = Path(__file__).resolve().parent.parent
        _require_plain_directory(package_source, "AROS Python package source")
        if not reuse_logs:
            try:
                package_alias.symlink_to(package_source, target_is_directory=True)
            except OSError as error:
                raise TaskError("unable to create task runner package alias") from error
            _fsync_directory(import_root)
        try:
            alias_mode = package_alias.lstat().st_mode
            alias_target = package_alias.resolve(strict=True)
        except OSError as error:
            raise TaskError("invalid task runner package alias") from error
        if not stat.S_ISLNK(alias_mode) or alias_target != package_source:
            raise TaskError("task runner package alias does not bind workspace source")
        _require_absent(runtime / "adapter.json", "task adapter claim")
        _ensure_restrictive_plain_file(
            runtime / "stdout.log",
            "task stdout log",
            allow_existing=reuse_logs,
            permissions_enforced=filesystem_permissions_enforced,
        )
        _ensure_restrictive_plain_file(
            runtime / "stderr.log",
            "task stderr log",
            allow_existing=reuse_logs,
            permissions_enforced=filesystem_permissions_enforced,
        )
        _require_absent(runtime / "final.json", "task final receipt")

    def _record_carrier_failure(self, task_id: str, detail: str) -> None:
        from .task_runner import record_carrier_failure

        record_carrier_failure(self, task_id, detail)

    def _require_startable_parent(
        self,
        brief: dict[str, object],
    ) -> str:
        self._require_git_root()
        head = self._safe_git_text("rev-parse", "--verify", "HEAD^{commit}")
        if _COMMIT.fullmatch(head) is None:
            raise TaskError("task start requires a committed 40-hex parent HEAD")
        dirty = self._safe_git_bytes(
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--ignore-submodules=none",
        )
        if dirty:
            raise TaskError("task start requires a clean parent workspace")

        task_id = str(brief["task_id"])
        relative = f"tasks/{task_id}/brief.json"
        blob = self._safe_git_text(
            "rev-parse",
            "--verify",
            f"{head}:{relative}",
        )
        if not blob or self._safe_git_text("cat-file", "-t", blob) != "blob":
            raise TaskError(f"task start requires a committed brief at HEAD: {task_id}")
        committed = self._safe_git_bytes("cat-file", "blob", blob)
        working = _read_plain_bytes(
            self.root / relative,
            "working task brief",
        )
        if (
            committed != working
            or hashlib.sha256(committed).digest()
            != hashlib.sha256(working).digest()
        ):
            raise TaskError(f"committed brief bytes mismatch working brief: {task_id}")
        index_entries = self._safe_git_bytes("ls-files", "-v", "-z")
        if any(
            record[:1] == b"S" or record[:1].islower()
            for record in index_entries.split(b"\0")
            if record
        ):
            raise TaskError(
                "task start requires an unambiguous clean parent index; "
                "assume-unchanged and skip-worktree entries are not allowed"
            )

        base_commit = str(brief["base_commit"])
        if not self._is_commit(base_commit):
            raise TaskError(f"task brief base commit is not a commit: {task_id}")
        if not self._safe_git_success(
            "merge-base",
            "--is-ancestor",
            base_commit,
            head,
        ):
            raise TaskError(
                f"task brief base commit is not an ancestor of parent HEAD: {task_id}"
            )
        self._require_git_root()
        if self._safe_git_text("rev-parse", "--verify", "HEAD^{commit}") != head:
            raise TaskError("parent HEAD changed during task start validation")
        return head

    def _require_parent_unchanged(
        self,
        brief: dict[str, object],
        expected_head: str,
    ) -> None:
        if self._require_startable_parent(brief) != expected_head:
            raise TaskError("parent HEAD changed during task start")

    def _worktree_target(self, task_id: str, *, create_roots: bool) -> Path:
        root = self.root / ".worktree"
        tasks = root / "tasks"
        if create_roots:
            _ensure_plain_directory(root, "AROS worktree root")
            _ensure_plain_directory(tasks, "task worktree root")
        else:
            if _path_exists(root):
                _require_plain_directory(root, "AROS worktree root")
            if _path_exists(tasks):
                _require_plain_directory(tasks, "task worktree root")
        target = tasks / task_id
        if not target.is_absolute() or target.parent != tasks:
            raise TaskError(f"unsafe task worktree containment: {target}")
        if _path_exists(tasks) and tasks.resolve(strict=True) != tasks:
            raise TaskError(f"unsafe task worktree containment: {tasks}")
        return target

    def _require_no_unowned_worktree_material(
        self,
        brief: dict[str, object],
    ) -> None:
        task_id = str(brief["task_id"])
        target = self._worktree_target(task_id, create_roots=False)
        branch_ref = f"refs/heads/aros/task/{task_id}"
        if _path_exists(target):
            raise TaskError(f"unowned task worktree path exists: {target}")
        registrations = self._worktree_registrations()
        if any(
            self._same_path(str(item["worktree"]), target)
            or item.get("branch") == branch_ref
            for item in registrations
        ):
            raise TaskError(
                f"unowned task worktree or branch registration exists: {task_id}"
            )
        if branch_ref in self._local_branch_refs():
            raise TaskError(f"unowned task branch exists: {branch_ref}")

    def _require_unallocated_worktree(self, target: Path, branch: str) -> None:
        _require_absent(target, "task worktree target")
        branch_ref = f"refs/heads/{branch}"
        registrations = self._worktree_registrations()
        if any(
            self._same_path(str(item["worktree"]), target)
            for item in registrations
        ):
            raise TaskError(f"task worktree target is already registered: {target}")
        if any(item.get("branch") == branch_ref for item in registrations):
            raise TaskError(f"task branch is checked out elsewhere: {branch}")
        for existing in self._local_branch_refs():
            if (
                existing == branch_ref
                or existing.startswith(f"{branch_ref}/")
                or branch_ref.startswith(f"{existing}/")
            ):
                raise TaskError(f"task branch ref conflict: {existing}")

    def _add_task_worktree(self, target: Path, branch: str, base_commit: str) -> None:
        result = self._safe_git_result(
            "worktree", "add", "-b", branch, str(target), base_commit
        )
        if result.returncode != 0:
            raise TaskError(
                f"unable to create task worktree: {_git_error(result)}"
            )

    def _validate_new_checkout(
        self,
        target: Path,
        branch: str,
        base_commit: str,
    ) -> None:
        self._require_new_checkout_metadata(target, branch, base_commit)

    def _require_new_checkout_metadata(
        self,
        target: Path,
        branch: str,
        base_commit: str,
    ) -> None:
        tip = self._validate_worktree(target, branch, base_commit)
        if tip != base_commit:
            raise TaskError("new task checkout does not remain at its brief base commit")
        child_git_dir = self._worktree_git_directory(target)
        status = self._safe_git_bytes(
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--ignored=matching",
            "--ignore-submodules=none",
            git_dir=child_git_dir,
            work_tree=target,
        )
        if status:
            raise TaskError("new task checkout must be completely clean")
        index_entries = self._safe_git_bytes(
            "ls-files",
            "-v",
            "-z",
            git_dir=child_git_dir,
            work_tree=target,
        )
        if any(
            record[:1] == b"S" or record[:1].islower()
            for record in index_entries.split(b"\0")
            if record
        ):
            raise TaskError("new task checkout index flags are ambiguous")
        index = self._safe_git_result(
            "diff-index",
            "--cached",
            "--quiet",
            base_commit,
            "--",
            git_dir=child_git_dir,
            work_tree=target,
        )
        if index.returncode != 0:
            raise TaskError("new task checkout index differs from its base commit")

    def _validate_owned_worktree(self, ownership: dict[str, object]) -> None:
        self._validate_worktree(
            Path(str(ownership["worktree_path"])),
            str(ownership["branch"]),
            str(ownership["base_commit"]),
        )

    def _validate_worktree(
        self,
        target: Path,
        branch: str,
        base_commit: str,
    ) -> str:
        self._require_git_root()
        _require_plain_directory(target, "owned task worktree")
        branch_ref = f"refs/heads/{branch}"
        registrations = self._worktree_registrations()
        path_matches = [
            item
            for item in registrations
            if self._same_path(str(item["worktree"]), target)
        ]
        if len(path_matches) != 1:
            raise TaskError(f"owned task worktree is not uniquely registered: {target}")
        registration = path_matches[0]
        if registration.get("branch") != branch_ref:
            raise TaskError(f"owned task worktree branch registration mismatch: {target}")
        branch_matches = [
            item for item in registrations if item.get("branch") == branch_ref
        ]
        if len(branch_matches) != 1:
            raise TaskError(f"owned task branch is registered elsewhere: {branch}")

        child_git_dir = self._worktree_git_directory(target)
        common = Path(
            self._safe_git_text(
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
                git_dir=child_git_dir,
                work_tree=target,
            )
        ).resolve(strict=True)
        if common != self._git_common_dir:
            raise TaskError(f"owned task worktree common Git directory mismatch: {target}")
        top = Path(
            self._safe_git_text(
                "rev-parse",
                "--show-toplevel",
                git_dir=child_git_dir,
                work_tree=target,
            )
        ).resolve(strict=True)
        if top != target:
            raise TaskError(f"owned task worktree root mismatch: {target}")
        attached = self._safe_git_text(
            "symbolic-ref",
            "-q",
            "HEAD",
            git_dir=child_git_dir,
            work_tree=target,
        )
        if attached != branch_ref:
            raise TaskError(f"owned task worktree is not attached to {branch}")
        tip = self._safe_git_text(
            "rev-parse",
            "--verify",
            "HEAD^{commit}",
            git_dir=child_git_dir,
            work_tree=target,
        )
        if _COMMIT.fullmatch(tip) is None:
            raise TaskError(f"owned task worktree has an invalid HEAD: {target}")
        if registration.get("HEAD") != tip:
            raise TaskError(f"owned task worktree registry HEAD mismatch: {target}")
        branch_tip = self._safe_git_text(
            "rev-parse",
            "--verify",
            f"{branch_ref}^{{commit}}",
        )
        if branch_tip != tip:
            raise TaskError(f"owned task branch tip mismatch: {branch}")
        if not self._safe_git_success(
            "merge-base",
            "--is-ancestor",
            base_commit,
            tip,
        ):
            raise TaskError(f"owned task branch no longer descends from its base: {branch}")
        if self._worktree_git_directory(target) != child_git_dir:
            raise TaskError(
                f"owned task worktree Git directory association changed: {target}"
            )
        self._require_git_root()
        return tip

    def _worktree_git_directory(self, target: Path) -> Path:
        marker = target / ".git"
        raw = _read_plain_bytes(marker, "task worktree Git marker")
        try:
            line = raw.decode("utf-8").rstrip("\n")
        except UnicodeError as error:
            raise TaskError(f"invalid task worktree Git marker: {marker}") from error
        if "\n" in line or not line.startswith("gitdir: "):
            raise TaskError(f"invalid task worktree Git marker: {marker}")
        git_dir = Path(line.removeprefix("gitdir: "))
        if not git_dir.is_absolute():
            git_dir = target / git_dir
        try:
            resolved = git_dir.resolve(strict=True)
        except OSError as error:
            raise TaskError(f"invalid task worktree Git directory: {target}") from error
        _plain_directory_identity(resolved, "task worktree Git directory")
        if not resolved.is_relative_to(self._git_common_dir / "worktrees"):
            raise TaskError(f"task worktree Git directory escaped common Git data: {target}")
        return resolved

    def _worktree_registrations(self) -> list[dict[str, object]]:
        raw = self._safe_git_bytes(
            "worktree",
            "list",
            "--porcelain",
            "-z",
            "--expire=now",
        )
        registrations: list[dict[str, object]] = []
        for raw_record in raw.split(b"\0\0"):
            if not raw_record:
                continue
            record: dict[str, object] = {}
            for field in raw_record.strip(b"\0").split(b"\0"):
                key, separator, value = field.partition(b" ")
                try:
                    name = key.decode("ascii")
                except UnicodeError as error:
                    raise TaskError("invalid Git worktree registration") from error
                if name in record:
                    raise TaskError("ambiguous Git worktree registration")
                if not separator:
                    record[name] = True
                elif name == "worktree":
                    record[name] = os.fsdecode(value)
                else:
                    try:
                        record[name] = value.decode("utf-8")
                    except UnicodeError as error:
                        raise TaskError("invalid Git worktree registration") from error
            if not isinstance(record.get("worktree"), str):
                raise TaskError("invalid Git worktree registration")
            if "prunable" in record:
                raise TaskError(
                    f"stale or prunable Git worktree registration: "
                    f"{record['worktree']}"
                )
            registered_path = Path(str(record["worktree"]))
            try:
                registered_mode = registered_path.lstat().st_mode
            except OSError as error:
                raise TaskError(
                    f"stale Git worktree registration: {registered_path}"
                ) from error
            if stat.S_ISLNK(registered_mode) or not stat.S_ISDIR(registered_mode):
                raise TaskError(
                    f"ambiguous Git worktree registration path: {registered_path}"
                )
            registrations.append(record)
        return registrations

    def _local_branch_refs(self) -> set[str]:
        output = self._safe_git_text(
            "for-each-ref",
            "--format=%(refname)",
            "refs/heads",
        )
        return set(output.splitlines()) if output else set()

    def _is_commit(self, value: str) -> bool:
        if _COMMIT.fullmatch(value) is None:
            return False
        result = self._safe_git_result(
            "rev-parse",
            "--verify",
            f"{value}^{{commit}}",
        )
        return result.returncode == 0 and result.stdout.strip() == value.encode()

    def _safe_git_text(
        self,
        *args: str,
        git_dir: Path | None = None,
        work_tree: Path | None = None,
    ) -> str:
        result = self._safe_git_result(
            *args,
            git_dir=git_dir,
            work_tree=work_tree,
        )
        if result.returncode != 0:
            raise TaskError(f"Git command failed: {' '.join(args)}: {_git_error(result)}")
        try:
            return result.stdout.decode("utf-8").strip()
        except UnicodeError as error:
            raise TaskError(f"Git command returned invalid UTF-8: {' '.join(args)}") from error

    def _safe_git_bytes(
        self,
        *args: str,
        git_dir: Path | None = None,
        work_tree: Path | None = None,
    ) -> bytes:
        result = self._safe_git_result(
            *args,
            git_dir=git_dir,
            work_tree=work_tree,
        )
        if result.returncode != 0:
            raise TaskError(f"Git command failed: {' '.join(args)}: {_git_error(result)}")
        return result.stdout

    def _safe_git_success(self, *args: str) -> bool:
        return self._safe_git_result(*args).returncode == 0

    def _safe_git_result(
        self,
        *args: str,
        git_dir: Path | None = None,
        work_tree: Path | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        return self._pinned_git_result(
            *args,
            git_dir=git_dir,
            work_tree=work_tree,
            configs=self._safe_git_configs(),
        )

    def _safe_git_configs(self) -> tuple[str, ...]:
        base = (
            "core.hooksPath=/dev/null",
            "core.fileMode=true",
            "core.fsmonitor=false",
            "core.autocrlf=false",
            "core.attributesFile=/dev/null",
        )
        result = self._pinned_git_result(
            "config",
            "--null",
            "--name-only",
            "--get-regexp",
            r"^filter\..*\.(clean|smudge|process|required)$",
            configs=base,
        )
        if result.returncode not in {0, 1}:
            raise TaskError(f"unable to inspect Git filter configuration: {_git_error(result)}")
        names: set[str] = set()
        for raw_key in result.stdout.split(b"\0"):
            if not raw_key:
                continue
            try:
                key = raw_key.decode("utf-8")
            except UnicodeError as error:
                raise TaskError("ambiguous Git filter configuration") from error
            match = _FILTER_CONFIG_KEY.fullmatch(key)
            if match is None or _FILTER_NAME.fullmatch(match.group(1)) is None:
                raise TaskError(f"ambiguous Git filter configuration: {key!r}")
            names.add(match.group(1))
        overrides = list(base)
        for name in sorted(names):
            overrides.extend(
                (
                    f"filter.{name}.clean=cat",
                    f"filter.{name}.smudge=cat",
                    f"filter.{name}.process=",
                    f"filter.{name}.required=false",
                )
            )
        return tuple(overrides)

    def _pinned_git_result(
        self,
        *args: str,
        git_dir: Path | None = None,
        work_tree: Path | None = None,
        configs: tuple[str, ...] = (),
    ) -> subprocess.CompletedProcess[bytes]:
        command = self._pinned_git_command(
            *args,
            git_dir=git_dir,
            work_tree=work_tree,
            configs=configs,
        )
        try:
            return subprocess.run(
                command,
                capture_output=True,
                text=False,
                timeout=10,
                check=False,
                env=_git_environment(),
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise TaskError(f"Git command failed: {' '.join(args)}") from error

    def _pinned_git_command(
        self,
        *args: str,
        git_dir: Path | None = None,
        work_tree: Path | None = None,
        configs: tuple[str, ...] = (),
    ) -> list[str]:
        selected_git_dir = git_dir or self._git_dir
        selected_work_tree = work_tree or self.root
        command = [
            "git",
            "--no-replace-objects",
            f"--git-dir={selected_git_dir}",
            f"--work-tree={selected_work_tree}",
        ]
        for config in configs:
            command.extend(("-c", config))
        command.extend(args)
        return command

    @staticmethod
    def _same_path(raw: str, target: Path) -> bool:
        raw_absolute = Path(raw).absolute()
        target_absolute = target.absolute()
        if os.path.normcase(str(raw_absolute)) == os.path.normcase(
            str(target_absolute)
        ):
            return True
        return os.path.normcase(str(raw_absolute.resolve(strict=False))) == os.path.normcase(
            str(target_absolute.resolve(strict=False))
        )

    def _ensure_record_roots(self) -> None:
        _require_plain_directory(self.root / ".aros", "AROS runtime directory")
        versioned = self.root / "tasks"
        _ensure_plain_directory(versioned, "versioned tasks directory")
        _ensure_plain_directory(
            versioned / _TASK_STAGING_DIRECTORY,
            "task brief staging directory",
        )
        runtime = self.root / ".aros" / "tasks"
        _ensure_plain_directory(runtime, "runtime tasks directory")
        _ensure_plain_directory(runtime / "idempotency", "task idempotency directory")
        _ensure_plain_directory(self.root / ".aros" / "locks", "AROS locks directory")
        self._sync_record_root_chain()

    def _sync_record_root_chain(self) -> None:
        paths = (
            self.root,
            self.root / ".aros",
            self.root / "tasks",
            self.root / "tasks" / _TASK_STAGING_DIRECTORY,
            self.root / ".aros" / "tasks",
            self.root / ".aros" / "tasks" / "idempotency",
            self.root / ".aros" / "locks",
        )
        for path in paths:
            _require_plain_directory(path, "task record root")
            _fsync_directory(path)

    def _validate_inventory(self) -> None:
        if self._versioned_task_ids() != self._runtime_task_ids():
            raise TaskError("task record inventory conflict between versioned and runtime paths")

    def _versioned_task_ids(self) -> set[str]:
        return _task_directory_ids(self.root / "tasks", runtime=False)

    def _runtime_task_ids(self) -> set[str]:
        _require_plain_directory(self.root / ".aros", "AROS runtime directory")
        return _task_directory_ids(self.root / ".aros" / "tasks", runtime=True)

    def _require_git_root(
        self,
    ) -> tuple[Path, Path]:
        pinned = hasattr(self, "_git_dir")
        top = self._git_output("rev-parse", "--show-toplevel", pinned=pinned)
        if top is None or Path(top).resolve() != self.root:
            raise TaskError(f"workspace must be the Git repository root: {self.root}")
        raw_git_dir = self._git_output(
            "rev-parse",
            "--absolute-git-dir",
            pinned=pinned,
        )
        if raw_git_dir is None:
            raise TaskError(f"unable to resolve Git directory association: {self.root}")
        git_dir = Path(raw_git_dir).resolve()
        marker_git_dir = self._git_directory_from_marker()
        if git_dir != marker_git_dir:
            raise TaskError(f"invalid Git directory association: {self.root}")
        pinned_git_dir = getattr(self, "_git_dir", git_dir)
        if git_dir != pinned_git_dir:
            raise TaskError(f"Git directory association changed: {self.root}")

        raw_common_dir = self._git_output(
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
            pinned=pinned,
        )
        if raw_common_dir is None:
            raise TaskError(f"unable to resolve common Git directory: {self.root}")
        common_dir = Path(raw_common_dir)
        if not common_dir.is_absolute():
            common_dir = self.root / common_dir
        common_dir = common_dir.resolve()
        pinned_common_dir = getattr(self, "_git_common_dir", common_dir)
        if common_dir != pinned_common_dir:
            raise TaskError(f"common Git directory association changed: {self.root}")
        return git_dir, common_dir

    def _git_directory_from_marker(self) -> Path:
        marker = self.root / ".git"
        try:
            mode = marker.lstat().st_mode
        except OSError as error:
            raise TaskError(f"invalid Git directory association: {self.root}") from error
        if stat.S_ISDIR(mode):
            return marker.resolve()
        if not stat.S_ISREG(mode):
            raise TaskError(f"invalid Git directory association: {self.root}")
        try:
            lines = marker.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as error:
            raise TaskError(f"invalid Git directory association: {self.root}") from error
        if len(lines) != 1 or not lines[0].startswith("gitdir: "):
            raise TaskError(f"invalid Git directory association: {self.root}")
        target = Path(lines[0].removeprefix("gitdir: "))
        if not target.is_absolute():
            target = self.root / target
        try:
            resolved = target.resolve(strict=True)
        except OSError as error:
            raise TaskError(f"invalid Git directory association: {self.root}") from error
        if not resolved.is_dir():
            raise TaskError(f"invalid Git directory association: {self.root}")
        return resolved

    def _require_initialized_workspace(self) -> None:
        try:
            _require_plain_file(self.root / "AROS.md", "AROS mission")
            _require_plain_directory(self.root / "memory", "AROS memory directory")
            _require_plain_file(self.root / "memory" / "NOW.md", "AROS working memory")
            _require_plain_directory(self.root / ".aros", "AROS runtime directory")
        except TaskError as error:
            raise TaskError(
                f"workspace is not initialized; run `aros init` at the Git root: "
                f"{self.root}: {error}"
            ) from error

    def _git_output(self, *args: str, pinned: bool = False) -> str | None:
        command = ["git", "--no-replace-objects"]
        if pinned:
            command.extend(
                (
                    f"--git-dir={self._git_dir}",
                    f"--work-tree={self.root}",
                )
            )
        else:
            command.extend(("-C", str(self.root)))
        command.extend(args)
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                env=_git_environment(),
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise TaskError(f"Git command failed: {' '.join(args)}") from error
        return result.stdout.strip() if result.returncode == 0 else None

    def _new_task_id(self, objective: str) -> str:
        date = datetime.now(timezone.utc).strftime("%Y%m%d")
        label = re.sub(r"[^a-z0-9]+", "-", objective.lower()).strip("-")[:32]
        return f"TASK-{date}-{label or 'child'}-{secrets.token_hex(8)}"

    def _validate_task_id(self, task_id: str) -> None:
        if not _valid_task_id(task_id):
            raise TaskError(f"invalid task ID: {task_id!r}")

    def _idempotency_lock_path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.root / ".aros" / "locks" / f"task-idempotency-{digest}.lock"

    def _publication_lock_path(self) -> Path:
        return self.root / ".aros" / "locks" / "task-record-publication.lock"

    def _lifecycle_lock_path(self, task_id: str) -> Path:
        return self.root / ".aros" / "locks" / f"task-lifecycle-{task_id}.lock"

    def _stop_delivery_lock_path(self, task_id: str) -> Path:
        return self.root / ".aros" / "locks" / f"task-stop-delivery-{task_id}.lock"

    def _status_path(self, task_id: str) -> Path:
        return self.root / ".aros" / "tasks" / task_id / "status.json"

    def _runtime_path(self, task_id: str) -> Path:
        self._validate_task_id(task_id)
        return self.root / ".aros" / "tasks" / task_id

    def _ownership_path(self, task_id: str) -> Path:
        return self.root / ".aros" / "tasks" / task_id / "ownership.json"

    def _launch_path(self, task_id: str) -> Path:
        return self._runtime_path(task_id) / "launch.json"

    def _final_path(self, task_id: str) -> Path:
        return self._runtime_path(task_id) / "final.json"

    def _execution_path(self, task_id: str) -> Path:
        return self._runtime_path(task_id) / "execution.json"

    def _adapter_path(self, task_id: str) -> Path:
        return self._runtime_path(task_id) / "adapter.json"

    def _stop_path(self, task_id: str) -> Path:
        return self._runtime_path(task_id) / "stop.json"

    def _stop_result_path(self, task_id: str) -> Path:
        return self._runtime_path(task_id) / "stop-result.json"

    def _messages_path(self, task_id: str) -> Path:
        return self._runtime_path(task_id) / "messages"

    def _collected_path(self, task_id: str) -> Path:
        self._validate_task_id(task_id)
        return self.root / "tasks" / task_id / "collected.json"

    def _prune_path(self, task_id: str) -> Path:
        return self._runtime_path(task_id) / "prune.json"

    def _pruned_path(self, task_id: str) -> Path:
        return self._runtime_path(task_id) / "pruned.json"

    def _idempotency_index_path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.root / ".aros" / "tasks" / "idempotency" / f"{digest}.json"


def _record_sha256(record: dict[str, object], hash_field: str) -> str:
    payload = dict(record)
    payload.pop(hash_field, None)
    try:
        return json_sha256(payload)
    except (TypeError, UnicodeError) as error:
        raise TaskError("task execution record must be canonical UTF-8 JSON") from error


def _request_file_mode(descriptor: int, mode: int) -> bool:
    try:
        os.fchmod(descriptor, mode)
    except OSError as error:
        if error.errno not in _UNSUPPORTED_FILE_MODE_ERRNOS:
            raise
        return False
    return True


def _probe_filesystem_permissions(runtime: Path) -> dict[str, object]:
    _require_plain_directory(runtime, "AROS runtime directory")
    path = runtime / f".task-permission-probe-{secrets.token_hex(16)}"
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise TaskError(f"unable to create filesystem permission probe: {path}") from error
    try:
        created = os.fstat(descriptor)
    except OSError as error:
        os.close(descriptor)
        raise TaskError(f"unable to inspect filesystem permission probe: {path}") from error
    try:
        mode_supported = _request_file_mode(descriptor, 0o600)
        observed = os.fstat(descriptor)
        pathname = path.lstat()
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_nlink != 1
            or observed.st_size != 0
            or (observed.st_dev, observed.st_ino)
            != (pathname.st_dev, pathname.st_ino)
        ):
            raise TaskError(f"invalid filesystem permission probe inode: {path}")
        os.fsync(descriptor)
        return {
            "requested_mode": 0o600,
            "observed_mode": stat.S_IMODE(observed.st_mode),
            "mode_request_supported": mode_supported,
            "device": observed.st_dev,
            "enforced": mode_supported and stat.S_IMODE(observed.st_mode) == 0o600,
        }
    except OSError as error:
        raise TaskError(f"unable to observe filesystem permissions: {path}") from error
    finally:
        cleanup_error: BaseException | None = None
        try:
            try:
                current = path.lstat()
            except FileNotFoundError:
                current = None
            if current is not None and (
                current.st_dev,
                current.st_ino,
            ) == (created.st_dev, created.st_ino):
                path.unlink()
                _fsync_directory(runtime)
        except BaseException as error:
            cleanup_error = error
        finally:
            os.close(descriptor)
        if cleanup_error is not None:
            raise TaskError(
                f"unable to remove filesystem permission probe: {path}"
            ) from cleanup_error


def _validate_filesystem_permission_probe(
    value: object,
    description: str,
) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {
        "requested_mode",
        "observed_mode",
        "mode_request_supported",
        "device",
        "enforced",
    }:
        raise TaskError(f"invalid {description}")
    requested_mode = value.get("requested_mode")
    observed_mode = value.get("observed_mode")
    mode_request_supported = value.get("mode_request_supported")
    device = value.get("device")
    enforced = value.get("enforced")
    if (
        type(requested_mode) is not int
        or requested_mode != 0o600
        or type(observed_mode) is not int
        or stat.S_IMODE(observed_mode) != observed_mode
        or type(mode_request_supported) is not bool
        or type(device) is not int
        or device < 0
        or type(enforced) is not bool
        or enforced
        != (mode_request_supported and observed_mode == requested_mode)
    ):
        raise TaskError(f"invalid {description}")
    return dict(value)


def _tmux_socket_name(root: Path, task_id: str) -> str:
    digest = hashlib.sha256(f"{root}\0{task_id}".encode("utf-8")).hexdigest()[:20]
    return f"aros-task-{digest}"


def _seconds_since(timestamp: str) -> float:
    _validate_timestamp(timestamp, "task launch timestamp")
    launched = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
        tzinfo=timezone.utc
    )
    return max(0.0, (datetime.now(timezone.utc) - launched).total_seconds())


def _file_receipt(
    path: Path,
    relative: str,
    *,
    permissions_enforced: bool,
) -> dict[str, object]:
    if type(permissions_enforced) is not bool:
        raise TaskError("invalid task filesystem permission policy")
    _require_plain_file(path, "task output log")
    identity = _plain_file_identity(path, "task output log")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise TaskError(f"unable to open task output log: {path}") from error
    digest = hashlib.sha256()
    size = 0
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or (
                permissions_enforced
                and stat.S_IMODE(metadata.st_mode) != 0o600
            )
            or (metadata.st_dev, metadata.st_ino) != identity
        ):
            raise TaskError(f"task output log is not a restrictive plain file: {path}")
        expected_size = metadata.st_size
        os.fsync(descriptor)
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                size += len(chunk)
                digest.update(chunk)
        final_metadata = os.fstat(descriptor)
        if (
            size != expected_size
            or final_metadata.st_size != expected_size
            or (final_metadata.st_dev, final_metadata.st_ino) != identity
            or _plain_file_identity(path, "task output log") != identity
        ):
            raise TaskError(f"task output log changed while reading: {path}")
    except OSError as error:
        raise TaskError(f"unable to read task output log: {path}") from error
    finally:
        os.close(descriptor)
    return {"path": relative, "bytes": size, "sha256": digest.hexdigest()}


def _normalize_request(
    *,
    objective: object,
    actor: object,
    mode: object,
    adapter_argv: object,
    capabilities: object,
    deliverables: object,
    acceptance: object,
    timeout_seconds: object,
    idempotency_key: object,
) -> dict[str, object]:
    return {
        "objective": _validate_text(objective, "objective"),
        "actor": _validate_text(actor, "actor"),
        "mode": _validate_mode(mode),
        "adapter_argv": _validate_argv(adapter_argv),
        "capabilities": _validate_capabilities(capabilities),
        "deliverables": _validate_string_list(deliverables, "deliverables"),
        "acceptance": _validate_string_list(acceptance, "acceptance"),
        "timeout_seconds": _validate_timeout(timeout_seconds),
        "idempotency_key": _validate_text(idempotency_key, "idempotency_key"),
    }


def _git_environment() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("GIT_", "PYTHON", "LD_", "DYLD_"))
    }


def _brief_sha256(brief: dict[str, object]) -> str:
    payload = dict(brief)
    payload.pop("brief_sha256", None)
    try:
        return json_sha256(payload)
    except (TypeError, UnicodeError) as error:
        raise TaskError("task brief must be canonical UTF-8 JSON") from error


def _ownership_sha256(ownership: dict[str, object]) -> str:
    payload = dict(ownership)
    payload.pop("ownership_sha256", None)
    try:
        return json_sha256(payload)
    except (TypeError, UnicodeError) as error:
        raise TaskError("task ownership must be canonical UTF-8 JSON") from error


def _prepared_status(brief: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "task_id": brief["task_id"],
        "state": "prepared",
        "brief_sha256": brief["brief_sha256"],
        "updated_at": brief["created_at"],
    }


def _worktree_ready_status(
    brief: dict[str, object],
    ownership: dict[str, object],
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "task_id": brief["task_id"],
        "state": "worktree_ready",
        "brief_sha256": brief["brief_sha256"],
        "ownership_sha256": ownership["ownership_sha256"],
        "updated_at": ownership["acquired_at"],
    }


def _idempotency_index(
    brief: dict[str, object],
    key_digest: str,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "idempotency_key_sha256": key_digest,
        "request_sha256": brief["request_sha256"],
        "task_id": brief["task_id"],
        "brief_sha256": brief["brief_sha256"],
        "created_at": brief["created_at"],
    }


def _valid_task_id(value: object) -> bool:
    return isinstance(value, str) and len(value) <= 128 and _TASK_ID.fullmatch(value) is not None


def _validate_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TaskError(f"{field} must be a non-empty string")
    return _validate_utf8(value.strip(), field)


def _validate_mode(value: object) -> str:
    if not isinstance(value, str):
        raise TaskError("mode must be read_only or write")
    _validate_utf8(value, "mode")
    if value not in {"read_only", "write"}:
        raise TaskError("mode must be read_only or write")
    return value


def _validate_argv(value: object) -> list[str]:
    if not isinstance(value, list) or not value:
        raise TaskError("adapter_argv must be a non-empty list of strings")
    if any(not isinstance(item, str) or not item or "\x00" in item for item in value):
        raise TaskError("adapter_argv must contain only non-empty strings without NUL bytes")
    for item in value:
        _validate_utf8(item, "adapter_argv")
    return list(value)


def _validate_capabilities(value: object) -> dict[str, bool]:
    if not isinstance(value, dict) or set(value) != {"network", "shell"}:
        raise TaskError("capabilities must contain exactly network and shell")
    if any(type(item) is not bool for item in value.values()):
        raise TaskError("capabilities network and shell values must be booleans")
    return {"network": value["network"], "shell": value["shell"]}


def _validate_string_list(value: object, field: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise TaskError(f"{field} must be a list of strings")
    for item in value:
        _validate_utf8(item, field)
    return list(value)


def _validate_utf8(value: str, field: str) -> str:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise TaskError(f"{field} must be valid UTF-8") from error
    return value


def _validate_timeout(value: object) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise TaskError("timeout_seconds must be a positive number")
    try:
        finite = math.isfinite(value)
    except OverflowError as error:
        raise TaskError("timeout_seconds must be finite") from error
    if not finite:
        raise TaskError("timeout_seconds must be finite")
    return value


def _validate_hash(value: object, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise TaskError(f"{field} must be a 64-hex SHA-256")
    return value


def _validate_timestamp(value: object, field: str) -> str:
    if not isinstance(value, str) or _UTC_TIMESTAMP.fullmatch(value) is None:
        raise TaskError(f"{field} must be a UTC timestamp")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError as error:
        raise TaskError(f"{field} must be a UTC timestamp") from error
    return value


def _task_directory_ids(root: Path, *, runtime: bool) -> set[str]:
    if not _path_exists(root):
        return set()
    _require_plain_directory(root, "runtime tasks directory" if runtime else "versioned tasks directory")
    task_ids: set[str] = set()
    for entry in root.iterdir():
        if not runtime and entry.name == _TASK_STAGING_DIRECTORY:
            _require_plain_directory(entry, "task brief staging directory")
            continue
        if runtime and entry.name == "idempotency":
            _require_plain_directory(entry, "task idempotency directory")
            continue
        if not _valid_task_id(entry.name):
            raise TaskError(f"unrecognized task entry: {entry}")
        _require_plain_directory(entry, "task record directory")
        if not runtime:
            brief_path = entry / "brief.json"
            if not _path_exists(brief_path):
                try:
                    has_content = any(entry.iterdir())
                except OSError as error:
                    raise TaskError(f"unable to inspect task record directory: {entry}") from error
                if has_content:
                    raise TaskError(
                        f"ambiguous task record directory without a brief: {entry}"
                    )
                continue
            _require_plain_file(brief_path, "versioned task brief")
        task_ids.add(entry.name)
    return task_ids


def _read_plain_bytes(path: Path, description: str) -> bytes:
    identity = _plain_file_identity(path, description)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise TaskError(f"unable to open {description}: {path}") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise TaskError(f"{description} must be a plain file: {path}")
        if (metadata.st_dev, metadata.st_ino) != identity:
            raise TaskError(f"{description} identity changed while opening: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            payload = handle.read()
    except OSError as error:
        raise TaskError(f"unable to read {description}: {path}") from error
    finally:
        os.close(descriptor)
    if _plain_file_identity(path, description) != identity:
        raise TaskError(f"{description} identity changed while reading: {path}")
    return payload


def _git_error(result: subprocess.CompletedProcess[bytes]) -> str:
    detail = (result.stderr or result.stdout).decode("utf-8", errors="replace").strip()
    return detail or f"exit {result.returncode}"


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError as error:
        raise TaskError(f"unable to open task publication directory: {path}") from error
    try:
        os.fsync(descriptor)
    except OSError as error:
        raise TaskError(f"unable to sync task publication directory: {path}") from error
    finally:
        os.close(descriptor)


def _plain_directory_identity(path: Path, description: str) -> tuple[int, int]:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise TaskError(f"unable to inspect {description}: {path}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise TaskError(f"{description} must be a plain directory: {path}")
    return metadata.st_dev, metadata.st_ino


def _plain_file_identity(path: Path, description: str) -> tuple[int, int]:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise TaskError(f"unable to inspect {description}: {path}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise TaskError(f"{description} must be a plain file: {path}")
    return metadata.st_dev, metadata.st_ino


def _path_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise TaskError(f"unable to inspect AROS path: {path}") from error
    return True


def _require_absent(path: Path, description: str) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    except OSError as error:
        raise TaskError(f"unable to inspect {description}: {path}") from error
    kind = "symlink" if stat.S_ISLNK(mode) else "path"
    raise TaskError(f"{description} conflict: {kind} already exists: {path}")


def _require_plain_directory(path: Path, description: str) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as error:
        raise TaskError(f"{description} does not exist: {path}") from error
    except OSError as error:
        raise TaskError(f"unable to inspect {description}: {path}") from error
    if stat.S_ISLNK(mode):
        raise TaskError(f"{description} must be a plain directory, not a symlink: {path}")
    if not stat.S_ISDIR(mode):
        raise TaskError(f"{description} must be a plain directory: {path}")


def _require_plain_file(path: Path, description: str) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as error:
        raise TaskError(f"{description} does not exist: {path}") from error
    except OSError as error:
        raise TaskError(f"unable to inspect {description}: {path}") from error
    if stat.S_ISLNK(mode):
        raise TaskError(f"{description} must be a plain file, not a symlink: {path}")
    if not stat.S_ISREG(mode):
        raise TaskError(f"{description} must be a plain file: {path}")


def _require_restrictive_plain_file(
    path: Path,
    description: str,
    *,
    permissions_enforced: bool = True,
) -> None:
    if type(permissions_enforced) is not bool:
        raise TaskError("invalid task filesystem permission policy")
    _require_plain_file(path, description)
    try:
        metadata = path.lstat()
    except OSError as error:
        raise TaskError(f"unable to inspect {description}: {path}") from error
    if metadata.st_nlink != 1 or (
        permissions_enforced and stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise TaskError(f"{description} must be a restrictive plain file: {path}")


def _require_single_link_plain_file(path: Path, description: str) -> None:
    _require_plain_file(path, description)
    try:
        links = path.lstat().st_nlink
    except OSError as error:
        raise TaskError(f"unable to inspect {description}: {path}") from error
    if links != 1:
        raise TaskError(f"{description} must be a create-once plain file: {path}")


def _ensure_durable_lock_file(path: Path, description: str) -> None:
    _require_plain_directory(path.parent, f"{description} parent")
    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        _require_plain_file(path, description)
        try:
            descriptor = os.open(path, flags)
        except OSError as error:
            raise TaskError(f"unable to open {description}: {path}") from error
    except OSError as error:
        raise TaskError(f"unable to create {description}: {path}") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise TaskError(f"{description} must be a plain file: {path}")
        if metadata.st_nlink != 1:
            raise TaskError(f"{description} must not have a hardlink: {path}")
        if (metadata.st_dev, metadata.st_ino) != _plain_file_identity(path, description):
            raise TaskError(f"{description} identity changed while opening: {path}")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            _request_file_mode(descriptor, 0o600)
        os.fsync(descriptor)
    except OSError as error:
        raise TaskError(f"unable to sync {description}: {path}") from error
    finally:
        os.close(descriptor)
    _fsync_directory(path.parent)
    _require_plain_file(path, description)


def _ensure_restrictive_plain_file(
    path: Path,
    description: str,
    *,
    allow_existing: bool = False,
    permissions_enforced: bool,
) -> None:
    if type(permissions_enforced) is not bool:
        raise TaskError("invalid task filesystem permission policy")
    _require_plain_directory(path.parent, f"{description} parent")
    flags = (
        os.O_WRONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if not allow_existing:
            raise TaskError(f"{description} must not exist before publication: {path}")
        _require_plain_file(path, description)
        try:
            descriptor = os.open(path, flags)
        except OSError as error:
            raise TaskError(f"unable to open {description}: {path}") from error
    except OSError as error:
        raise TaskError(f"unable to create {description}: {path}") from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or (
                permissions_enforced
                and stat.S_IMODE(metadata.st_mode) != 0o600
            )
            or metadata.st_size != 0
            or (metadata.st_dev, metadata.st_ino)
            != _plain_file_identity(path, description)
        ):
            raise TaskError(f"{description} must be a restrictive plain file: {path}")
        os.fsync(descriptor)
    except OSError as error:
        raise TaskError(f"unable to sync {description}: {path}") from error
    finally:
        os.close(descriptor)
    _fsync_directory(path.parent)


def _ensure_plain_directory(path: Path, description: str) -> None:
    created = False
    try:
        path.mkdir(mode=0o700)
        created = True
    except FileExistsError:
        pass
    except OSError as error:
        raise TaskError(f"unable to create {description}: {path}") from error
    _require_plain_directory(path, description)
    if created:
        _fsync_directory(path.parent)


def _require_directory_entries(
    path: Path,
    description: str,
    expected: set[str],
) -> None:
    _require_plain_directory(path, description)
    try:
        actual = {entry.name for entry in path.iterdir()}
    except OSError as error:
        raise TaskError(f"unable to inspect {description}: {path}") from error
    if actual != expected:
        raise TaskError(f"{description} must contain exactly {sorted(expected)}: {path}")


def _create_plain_directory(path: Path, description: str) -> None:
    try:
        path.mkdir(mode=0o700)
    except FileExistsError as error:
        raise TaskError(f"{description} conflict: path already exists: {path}") from error
    except OSError as error:
        raise TaskError(f"unable to create {description}: {path}") from error
    _require_plain_directory(path, description)
    _fsync_directory(path.parent)


def _read_object(path: Path, description: str) -> dict[str, object]:
    _require_plain_file(path, description)
    try:
        value = read_json(path)
    except (OSError, ValueError) as error:
        raise TaskError(f"unable to read {description}: {path}: {error}") from error
    if not isinstance(value, dict):
        raise TaskError(f"invalid {description}: {path}")
    return value
