"""Immutable task briefs and prepared runtime state for AROS children."""

from __future__ import annotations

import hashlib
import math
import re
import secrets
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .store import create_json, file_lock, json_sha256, read_json, utc_now


_TASK_ID = re.compile(
    r"^TASK-\d{8}-[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?$"
)
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
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
        self._require_git_root()
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
            self._validate_inventory()
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
                self._recover_prepared_records(brief, index_path, key_digest)
                return brief

            existing = self._brief_for_idempotency_key(key)
            if existing is not None:
                if existing["request_sha256"] != request_sha256:
                    raise TaskError(
                        "idempotency key already belongs to a different task request"
                    )
                self._recover_prepared_records(existing, index_path, key_digest)
                return existing

            base_commit = self._git_output("rev-parse", "--verify", "HEAD^{commit}")
            if base_commit is None or _COMMIT.fullmatch(base_commit) is None:
                raise TaskError("child tasks require a committed 40-hex Git HEAD")
            task_id = self._new_task_id(str(request["objective"]))
            self._validate_task_id(task_id)
            versioned_directory = self.root / "tasks" / task_id
            runtime_directory = self.root / ".aros" / "tasks" / task_id
            _require_absent(versioned_directory, "versioned task path")
            _require_absent(runtime_directory, "runtime task path")
            _create_plain_directory(versioned_directory, "versioned task path")
            try:
                _create_plain_directory(runtime_directory, "runtime task path")
            except BaseException:
                versioned_directory.rmdir()
                raise

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
            if not create_json(versioned_directory / "brief.json", brief):
                raise TaskError(f"task brief already exists: {task_id}")

            status = _prepared_status(brief)
            if not create_json(runtime_directory / "status.json", status):
                raise TaskError(f"task status already exists: {task_id}")
            if not create_json(index_path, _idempotency_index(brief, key_digest)):
                raise TaskError("task idempotency index already exists")
            return brief

    def status(self, task_id: str) -> dict[str, object]:
        """Return prepared runtime state bound to its immutable brief."""
        brief = self._load_brief(task_id)
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
        if versioned != runtime:
            raise TaskError("task record inventory conflict between versioned and runtime paths")
        return [self.status(task_id) for task_id in sorted(versioned)]

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

    def _recover_prepared_records(
        self,
        brief: dict[str, object],
        index_path: Path,
        key_digest: str,
    ) -> None:
        task_id = str(brief["task_id"])
        status_path = self.root / ".aros" / "tasks" / task_id / "status.json"
        if not _path_exists(status_path):
            create_json(status_path, _prepared_status(brief))
        self.status(task_id)
        if not _path_exists(index_path):
            create_json(index_path, _idempotency_index(brief, key_digest))
        index = self._load_idempotency_index(index_path, key_digest)
        self._validate_index_binding(index, brief)

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
        _ensure_plain_directory(self.root / "tasks", "versioned tasks directory")
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

    def _require_git_root(self) -> None:
        top = self._git_output("rev-parse", "--show-toplevel")
        if top is None or Path(top).resolve() != self.root:
            raise TaskError(f"workspace must be the Git repository root: {self.root}")

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

    def _git_output(self, *args: str) -> str | None:
        try:
            result = subprocess.run(
                ["git", "-C", str(self.root), *args],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
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


def _brief_sha256(brief: dict[str, object]) -> str:
    payload = dict(brief)
    payload.pop("brief_sha256", None)
    return json_sha256(payload)


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
    return value.strip()


def _validate_mode(value: object) -> str:
    if not isinstance(value, str) or value not in {"read_only", "write"}:
        raise TaskError("mode must be read_only or write")
    return value


def _validate_argv(value: object) -> list[str]:
    if not isinstance(value, list) or not value:
        raise TaskError("adapter_argv must be a non-empty list of strings")
    if any(not isinstance(item, str) or not item or "\x00" in item for item in value):
        raise TaskError("adapter_argv must contain only non-empty strings without NUL bytes")
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
    return list(value)


def _validate_timeout(value: object) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise TaskError("timeout_seconds must be a positive number")
    if not math.isfinite(value):
        raise TaskError("timeout_seconds must be finite")
    return value


def _validate_hash(value: object, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise TaskError(f"{field} must be a 64-hex SHA-256")
    return value


def _validate_timestamp(value: object, field: str) -> str:
    if not isinstance(value, str) or _UTC_TIMESTAMP.fullmatch(value) is None:
        raise TaskError(f"{field} must be a UTC timestamp")
    return value


def _task_directory_ids(root: Path, *, runtime: bool) -> set[str]:
    if not _path_exists(root):
        return set()
    _require_plain_directory(root, "runtime tasks directory" if runtime else "versioned tasks directory")
    task_ids: set[str] = set()
    for entry in root.iterdir():
        if runtime and entry.name == "idempotency":
            _require_plain_directory(entry, "task idempotency directory")
            continue
        if not _valid_task_id(entry.name):
            raise TaskError(f"unrecognized task entry: {entry}")
        _require_plain_directory(entry, "task record directory")
        task_ids.add(entry.name)
    return task_ids


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
