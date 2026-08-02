"""Immutable task briefs and prepared runtime state for AROS children."""

from __future__ import annotations

import hashlib
import math
import os
import re
import secrets
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .store import create_json, file_lock, json_sha256, read_json, utc_now


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
_INDEX_FIELDS = {
    "schema_version",
    "idempotency_key_sha256",
    "request_sha256",
    "task_id",
    "brief_sha256",
    "created_at",
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
        (
            self._git_dir,
            self._git_dir_identity,
            self._git_common_dir,
            self._git_common_dir_identity,
        ) = self._require_git_root()
        self._require_initialized_workspace()

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
        _require_plain_file_or_missing(lock_path, "task idempotency lock")

        with file_lock(lock_path):
            _require_plain_file(lock_path, "task idempotency lock")
            publication_lock = self._publication_lock_path()
            _require_plain_file_or_missing(
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
        task_id = self._new_task_id(str(request["objective"]))
        self._validate_task_id(task_id)
        versioned_directory = self.root / "tasks" / task_id
        runtime_directory = self.root / ".aros" / "tasks" / task_id
        staging_directory = (
            self.root / "tasks" / _TASK_STAGING_DIRECTORY / task_id
        )
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

    def status(self, task_id: str) -> dict[str, object]:
        """Return prepared runtime state bound to its immutable brief."""
        self._validate_task_id(task_id)
        publication_lock = self._publication_lock_path()
        _require_plain_file(publication_lock, "task record publication lock")
        with file_lock(publication_lock):
            self._reconcile_authoritative_briefs()
            return self._status_unlocked(task_id)

    def _status_unlocked(self, task_id: str) -> dict[str, object]:
        brief = self._load_brief(task_id)
        self._load_bound_idempotency_index(brief)
        return self._load_prepared_status(brief)

    def _load_prepared_status(
        self,
        brief: dict[str, object],
    ) -> dict[str, object]:
        task_id = str(brief["task_id"])
        _require_plain_directory(self.root / ".aros", "AROS runtime directory")
        _require_plain_directory(
            self.root / ".aros" / "tasks", "runtime tasks directory"
        )
        runtime_directory = self.root / ".aros" / "tasks" / task_id
        _require_plain_directory(runtime_directory, "task runtime directory")
        status = _read_object(runtime_directory / "status.json", "task status")
        if set(status) != _STATUS_FIELDS or type(status.get("schema_version")) is not int:
            raise TaskError(f"invalid task status schema: {task_id}")
        if status["schema_version"] != 1:
            raise TaskError(f"invalid task status schema version: {task_id}")
        if status["task_id"] != task_id:
            raise TaskError(f"task status identity mismatch: {task_id}")
        if status["state"] != "prepared":
            raise TaskError(f"invalid task status state: {task_id}")
        _validate_hash(status["brief_sha256"], "task status brief_sha256")
        if status["brief_sha256"] != brief["brief_sha256"]:
            raise TaskError(f"task status brief hash mismatch: {task_id}")
        _validate_timestamp(status["updated_at"], "task status updated_at")
        if status["updated_at"] != brief["created_at"]:
            raise TaskError(f"task status timestamp mismatch: {task_id}")
        return status

    def list(self) -> list[dict[str, object]]:
        """Return task statuses in stable task-ID order."""
        versioned = self._versioned_task_ids()
        runtime = self._runtime_task_ids()
        if not versioned and not runtime:
            return []
        publication_lock = self._publication_lock_path()
        _require_plain_file(publication_lock, "task record publication lock")
        with file_lock(publication_lock):
            self._reconcile_authoritative_briefs()
            return self._list_unlocked()

    def _list_unlocked(self) -> list[dict[str, object]]:
        versioned = self._versioned_task_ids()
        runtime = self._runtime_task_ids()
        if versioned != runtime:
            raise TaskError("task record inventory conflict between versioned and runtime paths")
        return [self._status_unlocked(task_id) for task_id in sorted(versioned)]

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
        index_path = self._idempotency_index_path(key)
        if not _path_exists(status_path):
            create_json(status_path, _prepared_status(brief))
        if not _path_exists(index_path):
            create_json(index_path, _idempotency_index(brief, key_digest))
        self._load_prepared_status(brief)
        self._load_bound_idempotency_index(brief)

    def _reconcile_authoritative_briefs(self) -> None:
        versioned = self._versioned_task_ids()
        runtime = self._runtime_task_ids()
        if runtime - versioned:
            raise TaskError(
                "task record inventory conflict: runtime state has no versioned brief"
            )
        for task_id in sorted(versioned):
            self._recover_prepared_records(self._load_brief(task_id))
        self._validate_inventory()

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
    ) -> tuple[Path, tuple[int, int], Path, tuple[int, int]]:
        top = self._git_output("rev-parse", "--show-toplevel")
        if top is None or Path(top).resolve() != self.root:
            raise TaskError(f"workspace must be the Git repository root: {self.root}")
        raw_git_dir = self._git_output("rev-parse", "--absolute-git-dir")
        if raw_git_dir is None:
            raise TaskError(f"unable to resolve Git directory association: {self.root}")
        git_dir = Path(raw_git_dir).resolve()
        marker_git_dir = self._git_directory_from_marker()
        if git_dir != marker_git_dir:
            raise TaskError(f"invalid Git directory association: {self.root}")
        git_dir_identity = _plain_directory_identity(git_dir, "Git directory")
        pinned_git_dir = getattr(self, "_git_dir", git_dir)
        pinned_git_identity = getattr(
            self,
            "_git_dir_identity",
            git_dir_identity,
        )
        if git_dir != pinned_git_dir:
            raise TaskError(f"Git directory association changed: {self.root}")
        if git_dir_identity != pinned_git_identity:
            raise TaskError(f"Git directory identity changed: {self.root}")

        raw_common_dir = self._git_output(
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        )
        if raw_common_dir is None:
            raise TaskError(f"unable to resolve common Git directory: {self.root}")
        common_dir = Path(raw_common_dir)
        if not common_dir.is_absolute():
            common_dir = self.root / common_dir
        common_dir = common_dir.resolve()
        common_identity = _plain_directory_identity(
            common_dir,
            "common Git directory",
        )
        pinned_common_dir = getattr(self, "_git_common_dir", common_dir)
        pinned_common_identity = getattr(
            self,
            "_git_common_dir_identity",
            common_identity,
        )
        if common_dir != pinned_common_dir:
            raise TaskError(f"common Git directory association changed: {self.root}")
        if common_identity != pinned_common_identity:
            raise TaskError(f"common Git directory identity changed: {self.root}")
        return git_dir, git_dir_identity, common_dir, common_identity

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
        command = ["git"]
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
        return f"TASK-{date}-{label or 'child'}-{secrets.token_hex(2)}"

    def _validate_task_id(self, task_id: str) -> None:
        if not _valid_task_id(task_id):
            raise TaskError(f"invalid task ID: {task_id!r}")

    def _idempotency_lock_path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.root / ".aros" / "locks" / f"task-idempotency-{digest}.lock"

    def _publication_lock_path(self) -> Path:
        return self.root / ".aros" / "locks" / "task-record-publication.lock"

    def _idempotency_index_path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.root / ".aros" / "tasks" / "idempotency" / f"{digest}.json"


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
        if not key.startswith("GIT_")
    }


def _brief_sha256(brief: dict[str, object]) -> str:
    payload = dict(brief)
    payload.pop("brief_sha256", None)
    try:
        return json_sha256(payload)
    except (TypeError, UnicodeError) as error:
        raise TaskError("task brief must be canonical UTF-8 JSON") from error


def _prepared_status(brief: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "task_id": brief["task_id"],
        "state": "prepared",
        "brief_sha256": brief["brief_sha256"],
        "updated_at": brief["created_at"],
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


def _require_plain_file_or_missing(path: Path, description: str) -> None:
    if _path_exists(path):
        _require_plain_file(path, description)


def _ensure_plain_directory(path: Path, description: str) -> None:
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except OSError as error:
        raise TaskError(f"unable to create {description}: {path}") from error
    _require_plain_directory(path, description)


def _create_plain_directory(path: Path, description: str) -> None:
    try:
        path.mkdir(mode=0o700)
    except FileExistsError as error:
        raise TaskError(f"{description} conflict: path already exists: {path}") from error
    except OSError as error:
        raise TaskError(f"unable to create {description}: {path}") from error
    _require_plain_directory(path, description)


def _read_object(path: Path, description: str) -> dict[str, object]:
    _require_plain_file(path, description)
    try:
        value = read_json(path)
    except (OSError, ValueError) as error:
        raise TaskError(f"unable to read {description}: {path}") from error
    if not isinstance(value, dict):
        raise TaskError(f"invalid {description}: {path}")
    return value
