"""Visible evaluator registration and one-attempt request admission."""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import socket
import stat
import sys
import threading
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import eval_records as _eval_records
from . import store as _store
from . import worktrees as _worktrees
from .eval_records import parse_visible_manifest, validate_measurement_receipt
from .receipts import record_sha256
from .store import create_json, file_lock, read_json_strict, utc_now


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DESCRIPTOR_METADATA_FIELDS = {
    "manifest_ref",
    "manifest_commit",
    "manifest_blob_sha256",
    "apparatus_tree",
    "registration_actor",
    "registered_at",
    "descriptor_sha256",
}
_execution_lease_close_guard = threading.Lock()


class EvalError(ValueError):
    """Raised when an evaluation registration or request is unsafe."""


@dataclass(frozen=True)
class ExistingEvaluation:
    status: dict[str, object]


@dataclass
class ExecutionLease:
    request: dict[str, object]
    execution: dict[str, object]
    lock_fd: int

    def __enter__(self) -> ExecutionLease:
        if self.lock_fd < 0:
            raise RuntimeError("execution lease is already closed")
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        with _execution_lease_close_guard:
            if self.lock_fd < 0:
                return
            descriptor = self.lock_fd
            self.lock_fd = -1
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


class EvalService:
    """Freeze visible evaluator registrations in one exact Git workspace."""

    def __init__(self, root: str | Path):
        self.repository = _worktrees.bind_repository(root)
        self.root = self.repository.root

    def register(self, manifest_ref: str, *, actor: str) -> dict[str, object]:
        """Register one tracked manifest and its exact apparatus blobs."""
        reference = _manifest_reference(manifest_ref)
        registration_actor = _required_text(actor, "actor")
        _require_clean_registration(self.repository)
        manifest_commit = _worktrees._git_text(
            self.repository,
            "rev-parse",
            "--verify",
            "HEAD^{commit}",
        )
        manifest_blob = _read_regular_git_blob(
            self.repository,
            manifest_commit,
            reference,
            "manifest",
        )
        try:
            manifest_value = _store._strict_json_loads(manifest_blob)
        except (UnicodeError, ValueError) as error:
            raise EvalError(
                f"visible evaluator manifest must be strict UTF-8 JSON: {error}"
            ) from error
        try:
            manifest = parse_visible_manifest(manifest_value)
        except ValueError as error:
            raise EvalError(f"invalid visible evaluator manifest: {error}") from error
        parts = PurePosixPath(reference).parts
        if (
            manifest["evaluator_id"] != parts[2]
            or manifest["evaluator_version"] != parts[3]
        ):
            raise EvalError("manifest path does not match evaluator identity")

        apparatus_commit = str(manifest["apparatus_commit"])
        resolved_apparatus = _worktrees._git_text(
            self.repository,
            "rev-parse",
            "--verify",
            f"{apparatus_commit}^{{commit}}",
        )
        if resolved_apparatus != apparatus_commit:
            raise EvalError("apparatus_commit is not an exact Git commit")
        apparatus_tree = _worktrees._git_text(
            self.repository,
            "rev-parse",
            "--verify",
            f"{apparatus_commit}^{{tree}}",
        )
        for apparatus_path in manifest["apparatus_paths"]:  # type: ignore[union-attr]
            path = str(apparatus_path["path"])
            blob = _read_regular_git_blob(
                self.repository,
                apparatus_commit,
                path,
                "apparatus",
            )
            if hashlib.sha256(blob).hexdigest() != apparatus_path["blob_sha256"]:
                raise EvalError(f"apparatus blob hash mismatch: {path}")
        _require_clean_registration(self.repository)

        descriptor: dict[str, object] = {
            **manifest,
            "manifest_ref": reference,
            "manifest_commit": manifest_commit,
            "manifest_blob_sha256": hashlib.sha256(manifest_blob).hexdigest(),
            "apparatus_tree": apparatus_tree,
            "registration_actor": registration_actor,
            "registered_at": utc_now(),
        }
        descriptor["descriptor_sha256"] = record_sha256(
            descriptor,
            "descriptor_sha256",
        )
        path = (
            self.root
            / ".aros"
            / "evaluators"
            / str(manifest["evaluator_id"])
            / str(manifest["evaluator_version"])
            / "descriptor.json"
        )
        if not create_json(path, descriptor):
            raise EvalError(f"evaluator descriptor already exists: {path}")
        return descriptor

    def _publish_request(
        self,
        evaluator_id: str,
        version: str,
        candidate_commit: str,
        actor: str,
        idempotency_key: str,
    ) -> tuple[dict[str, object], bool]:
        """Create one immutable evaluation request without claiming execution."""
        evaluator = _identifier(evaluator_id, "evaluator_id")
        evaluator_version = _identifier(version, "evaluator_version")
        request_actor = _required_text(actor, "actor")
        key = _required_text(idempotency_key, "idempotency_key")
        candidate_literal = _commit_literal(candidate_commit, "candidate")
        key_digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        eval_id = f"EVAL-{key_digest}"
        path = self.root / ".aros" / "evaluations" / eval_id / "request.json"
        identity = {
            "eval_id": eval_id,
            "evaluator_id": evaluator,
            "evaluator_version": evaluator_version,
            "candidate_commit": candidate_literal,
            "actor": request_actor,
            "idempotency_key_sha256": key_digest,
        }
        existing = self._load_existing_request(path, identity)
        if existing is not None:
            return existing, False

        candidate = _resolve_exact_commit(
            self.repository,
            candidate_literal,
            "candidate",
        )
        descriptor = self._load_descriptor(evaluator, evaluator_version)
        request: dict[str, object] = {
            "schema_version": 1,
            **identity,
            "descriptor_sha256": descriptor["descriptor_sha256"],
            "candidate_commit": candidate,
            "apparatus_commit": descriptor["apparatus_commit"],
            "created_at": utc_now(),
        }
        request["request_sha256"] = record_sha256(request, "request_sha256")
        if create_json(path, request):
            return request, True
        existing = self._load_existing_request(path, identity)
        if existing is None:
            raise EvalError(f"evaluation request disappeared after create conflict: {path}")
        return existing, False

    def _load_existing_request(
        self,
        path: Path,
        identity: dict[str, object],
    ) -> dict[str, object] | None:
        try:
            existing_value = read_json_strict(path)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as error:
            raise EvalError(f"invalid existing evaluation request: {path}") from error
        try:
            existing = _eval_records._validate_request(existing_value)
        except ValueError as error:
            raise EvalError(f"invalid existing evaluation request: {path}") from error
        if any(existing[field] != value for field, value in identity.items()):
            raise EvalError("idempotency key belongs to a different request")
        return dict(existing)

    def _begin_execution(
        self,
        evaluator_id: str,
        version: str,
        candidate_commit: str,
        actor: str,
        idempotency_key: str,
    ) -> ExecutionLease | ExistingEvaluation:
        """Publish and hold the sole local execution claim for one request."""
        _require_linux_claim_runtime()
        key = _required_text(idempotency_key, "idempotency_key")
        key_digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        idempotency_lock = (
            self.root
            / ".aros"
            / "locks"
            / f"eval-idempotency-{key_digest}.lock"
        )
        with file_lock(idempotency_lock):
            request, created = self._publish_request(
                evaluator_id,
                version,
                candidate_commit,
                actor,
                key,
            )
            if not created:
                return self._existing_evaluation(request)
            execution_lock = self._execution_lock_path(str(request["eval_id"]))
            lock_fd = _acquire_execution_lock(execution_lock)
            if lock_fd is None:
                raise EvalError("new evaluation request could not acquire execution lock")
            try:
                broker_pid = os.getpid()
                start_token = _linux_process_start_token(broker_pid)
                if start_token is None:
                    raise EvalError("unable to bind local evaluation broker identity")
                execution: dict[str, object] = {
                    "schema_version": 1,
                    "eval_id": request["eval_id"],
                    "request_sha256": request["request_sha256"],
                    "host": _required_text(socket.gethostname(), "host"),
                    "broker_pid": broker_pid,
                    "broker_start_token": start_token,
                    "claimed_at": utc_now(),
                }
                execution["execution_sha256"] = record_sha256(
                    execution,
                    "execution_sha256",
                )
                execution_path = self._execution_path(str(request["eval_id"]))
                if not create_json(execution_path, execution):
                    raise EvalError("evaluation execution claim already exists")
                _eval_records._validate_execution(execution, request)
                return ExecutionLease(dict(request), execution, lock_fd)
            except BaseException:
                _release_execution_lock(lock_fd)
                raise

    def _existing_evaluation(
        self,
        request: dict[str, object],
    ) -> ExistingEvaluation:
        receipt = self._load_receipt(request, None)
        execution_path = self._execution_path(str(request["eval_id"]))
        try:
            execution_value = read_json_strict(execution_path)
            execution = _eval_records._validate_execution(execution_value, request)
        except FileNotFoundError:
            if receipt is not None:
                raise EvalError("existing receipt execution lineage is missing")
            return self._receipt_or_lost(
                request,
                None,
                "request has no execution claim",
            )
        except (OSError, ValueError) as error:
            raise EvalError("existing evaluation execution claim is invalid") from error
        if receipt is not None:
            if receipt["execution_sha256"] != execution["execution_sha256"]:
                raise EvalError("existing receipt execution lineage mismatch")
            return ExistingEvaluation(receipt)
        if execution["host"] != socket.gethostname():
            return self._receipt_or_lost(
                request,
                execution,
                "execution claim host is not local",
            )
        lock_state, lock_fd, lock_identity = _observe_execution_lock(
            self._execution_lock_path(str(request["eval_id"]))
        )
        if lock_state == "acquired":
            assert lock_fd is not None
            try:
                return self._receipt_or_lost(
                    request,
                    execution,
                    "execution claim lock was released",
                )
            finally:
                _release_execution_lock(lock_fd)
        if lock_state == "missing":
            return self._receipt_or_lost(
                request,
                execution,
                "execution claim lock was released",
            )
        assert lock_identity is not None
        broker_pid = int(execution["broker_pid"])
        if _linux_process_start_token(broker_pid) != execution["broker_start_token"]:
            return self._receipt_or_lost(
                request,
                execution,
                "execution claim broker is not live",
            )
        if not _linux_broker_owns_lock(execution, lock_identity):
            return self._receipt_or_lost(
                request,
                execution,
                "execution claim broker does not own the lock",
            )
        receipt = self._load_receipt(request, execution)
        if receipt is not None:
            return ExistingEvaluation(receipt)
        return ExistingEvaluation(
            {
                "eval_id": request["eval_id"],
                "evaluation_state": "running",
                "referenced_process_state": "prepared",
                "measurement_state": "not_available",
                "run_id": None,
                "receipt_ref": None,
                "reason": "execution claim is live",
                "updated_at": execution["claimed_at"],
            }
        )

    def _load_receipt(
        self,
        request: dict[str, object],
        execution: dict[str, object] | None,
    ) -> dict[str, object] | None:
        receipt_path = self._receipt_path(str(request["eval_id"]))
        try:
            receipt_value = read_json_strict(receipt_path)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise EvalError("existing evaluation receipt is unreadable") from error
        except ValueError as error:
            raise EvalError("existing evaluation receipt is invalid") from error
        try:
            receipt = validate_measurement_receipt(receipt_value)
        except ValueError as error:
            raise EvalError("existing evaluation receipt is invalid") from error
        if any(
            receipt[field] != request[field]
            for field in (
                "eval_id",
                "descriptor_sha256",
                "request_sha256",
                "candidate_commit",
                "apparatus_commit",
            )
        ):
            raise EvalError("existing evaluation receipt lineage is invalid")
        if (
            execution is not None
            and receipt["execution_sha256"] != execution["execution_sha256"]
        ):
            raise EvalError("existing receipt execution lineage mismatch")
        return receipt

    def _receipt_or_lost(
        self,
        request: dict[str, object],
        execution: dict[str, object] | None,
        reason: str,
    ) -> ExistingEvaluation:
        receipt = self._load_receipt(request, execution)
        if receipt is not None:
            if execution is None:
                raise EvalError("existing receipt execution lineage is missing")
            return ExistingEvaluation(receipt)
        return self._lost_evaluation(request, reason)

    def _lost_evaluation(
        self,
        request: dict[str, object],
        reason: str,
    ) -> ExistingEvaluation:
        return ExistingEvaluation(
            {
                "eval_id": request["eval_id"],
                "evaluation_state": "lost",
                "referenced_process_state": "lost",
                "measurement_state": "not_available",
                "run_id": None,
                "receipt_ref": None,
                "reason": reason,
                "updated_at": utc_now(),
            }
        )

    def _execution_path(self, eval_id: str) -> Path:
        return self.root / ".aros" / "evaluations" / eval_id / "execution.json"

    def _receipt_path(self, eval_id: str) -> Path:
        return self.root / "eval" / "evaluations" / eval_id / "receipt.json"

    def _execution_lock_path(self, eval_id: str) -> Path:
        return self.root / ".aros" / "locks" / f"{eval_id}-execution.lock"

    def _load_descriptor(
        self,
        evaluator_id: str,
        version: str,
    ) -> dict[str, object]:
        path = (
            self.root
            / ".aros"
            / "evaluators"
            / evaluator_id
            / version
            / "descriptor.json"
        )
        try:
            value = read_json_strict(path)
        except (OSError, ValueError) as error:
            raise EvalError(f"unable to load evaluator descriptor: {path}") from error
        if type(value) is not dict or not _DESCRIPTOR_METADATA_FIELDS.issubset(value):
            raise EvalError("evaluator descriptor has invalid fields")
        manifest_value = {
            field: item
            for field, item in value.items()
            if field not in _DESCRIPTOR_METADATA_FIELDS
        }
        try:
            manifest = parse_visible_manifest(manifest_value)
        except ValueError as error:
            raise EvalError(f"invalid evaluator descriptor manifest: {error}") from error
        if (
            manifest["evaluator_id"] != evaluator_id
            or manifest["evaluator_version"] != version
            or value["manifest_ref"]
            != f"eval/suites/{evaluator_id}/{version}/manifest.json"
            or not _is_commit(value["manifest_commit"])
            or not _is_hash(value["manifest_blob_sha256"])
            or not _is_commit(value["apparatus_tree"])
            or not isinstance(value["registration_actor"], str)
            or not value["registration_actor"]
            or not isinstance(value["registered_at"], str)
            or not _is_hash(value["descriptor_sha256"])
            or value["descriptor_sha256"]
            != record_sha256(value, "descriptor_sha256")
        ):
            raise EvalError("evaluator descriptor binding is invalid")
        return dict(value)


def _manifest_reference(value: object) -> str:
    if not isinstance(value, str):
        raise EvalError("manifest_ref must be a string")
    path = PurePosixPath(value)
    if (
        "\x00" in value
        or "\\" in value
        or path.is_absolute()
        or path.as_posix() != value
        or len(path.parts) != 5
        or path.parts[:2] != ("eval", "suites")
        or path.parts[4] != "manifest.json"
    ):
        raise EvalError(
            "manifest_ref must be eval/suites/<evaluator-id>/<version>/manifest.json"
        )
    return value


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise EvalError(f"{field} must be a non-empty string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise EvalError(f"{field} must be valid UTF-8") from error
    return value


def _identifier(value: object, field: str) -> str:
    text = _required_text(value, field)
    if _IDENTIFIER.fullmatch(text) is None:
        raise EvalError(f"{field} must be a safe path component")
    return text


def _resolve_exact_commit(
    repo: _worktrees.RepositoryBinding,
    value: object,
    description: str,
) -> str:
    commit = _commit_literal(value, description)
    try:
        resolved = _worktrees._git_text(
            repo,
            "rev-parse",
            "--verify",
            f"{commit}^{{commit}}",
        )
    except _worktrees.WorktreeError as error:
        raise EvalError(f"{description}_commit is unavailable") from error
    if resolved != commit:
        raise EvalError(f"{description}_commit is not exact")
    return commit


def _commit_literal(value: object, description: str) -> str:
    if not _is_commit(value):
        raise EvalError(f"{description}_commit must be a full lowercase Git commit")
    return value


def _read_regular_git_blob(
    repo: _worktrees.RepositoryBinding,
    commit: str,
    path: str,
    description: str,
) -> bytes:
    entry = _worktrees._git_bytes(
        repo,
        "--literal-pathspecs",
        "ls-tree",
        "-z",
        "--full-tree",
        commit,
        "--",
        path,
    )
    records = [record for record in entry.split(b"\0") if record]
    expected_path = path.encode("utf-8")
    if len(records) != 1:
        raise EvalError(f"{description} path must name one regular Git blob: {path}")
    header, separator, raw_path = records[0].partition(b"\t")
    fields = header.split(b" ")
    if (
        separator != b"\t"
        or raw_path != expected_path
        or len(fields) != 3
        or fields[0] not in {b"100644", b"100755"}
        or fields[1] != b"blob"
        or re.fullmatch(rb"[0-9a-f]{40}", fields[2]) is None
    ):
        raise EvalError(f"{description} path must name one regular Git blob: {path}")
    return _worktrees._git_bytes(repo, "cat-file", "blob", fields[2].decode("ascii"))


def _is_commit(value: object) -> bool:
    return isinstance(value, str) and _COMMIT.fullmatch(value) is not None


def _is_hash(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _acquire_execution_lock(path: Path) -> int | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise EvalError(f"unable to open execution lock: {path}") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise EvalError(f"execution lock must be a single-link regular file: {path}")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            return None
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _observe_execution_lock(
    path: Path,
) -> tuple[str, int | None, tuple[int, int] | None]:
    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return "missing", None, None
    except OSError as error:
        raise EvalError(f"unable to inspect execution lock: {path}") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise EvalError(f"execution lock must be a single-link regular file: {path}")
        identity = (metadata.st_dev, metadata.st_ino)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            return "contended", None, identity
        return "acquired", descriptor, identity
    except BaseException:
        os.close(descriptor)
        raise


def _linux_broker_owns_lock(
    execution: dict[str, object],
    lock_identity: tuple[int, int],
) -> bool:
    broker_pid = int(execution["broker_pid"])
    recorded_token = str(execution["broker_start_token"])
    if _linux_process_start_token(broker_pid) != recorded_token:
        return False
    owns_lock_file = False
    try:
        with os.scandir(f"/proc/{broker_pid}/fd") as entries:
            for entry in entries:
                try:
                    metadata = entry.stat(follow_symlinks=True)
                except OSError:
                    continue
                if (metadata.st_dev, metadata.st_ino) == lock_identity:
                    owns_lock_file = True
                    break
    except OSError:
        return False
    return (
        _linux_process_start_token(broker_pid) == recorded_token
        and owns_lock_file
    )


def _linux_process_start_token(pid: int) -> str | None:
    if pid < 1:
        return None
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        fields_after_name = raw.rsplit(")", 1)[1].split()
        return f"linux-proc-start:{fields_after_name[19]}"
    except (OSError, IndexError, ValueError):
        return None


def _require_linux_claim_runtime() -> None:
    if sys.platform != "linux":
        raise EvalError("evaluation execution claim runtime requires Linux")


def _release_execution_lock(descriptor: int) -> None:
    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _require_clean_registration(repo: _worktrees.RepositoryBinding) -> None:
    try:
        _worktrees._reject_checkout_filters(repo)
    except _worktrees.WorktreeError as error:
        raise EvalError(f"registration filter configuration is unsafe: {error}") from error
    _reject_registration_hooks(repo)
    status_output = _worktrees._git_bytes(
        repo,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--ignore-submodules=none",
    )
    if status_output:
        raise EvalError("registration repository is dirty or contains untracked files")
    index_entries = _worktrees._git_bytes(repo, "ls-files", "-v", "-z")
    if any(
        entry[:1] == b"S" or entry[:1].islower()
        for entry in index_entries.split(b"\0")
        if entry
    ):
        raise EvalError("registration repository has ambiguous index flags")
    index = _worktrees._git_result(
        repo,
        "diff-index",
        "--cached",
        "--quiet",
        "HEAD",
        "--",
    )
    if index.returncode != 0:
        raise EvalError("registration repository is dirty")


def _reject_registration_hooks(repo: _worktrees.RepositoryBinding) -> None:
    configured = _worktrees._git_result(
        repo,
        "config",
        "--local",
        "--get",
        "core.hooksPath",
    )
    if configured.returncode == 0:
        raise EvalError("registration Git hook configuration is not allowed")
    if configured.returncode != 1:
        raise EvalError("unable to inspect registration Git hook configuration")
    hooks = repo.common_dir / "hooks"
    try:
        metadata = hooks.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise EvalError("unable to inspect registration Git hooks") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise EvalError("registration Git hooks path is ambiguous")
    try:
        entries = list(hooks.iterdir())
    except OSError as error:
        raise EvalError("unable to inspect registration Git hooks") from error
    if any(not entry.name.endswith(".sample") for entry in entries):
        raise EvalError("registration Git hooks are not allowed")
