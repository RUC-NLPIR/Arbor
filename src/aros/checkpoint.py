"""Prepare audited checkpoint candidate trees without creating commits or refs."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType

from .store import (
    AnchoredReadError,
    AnchoredReadLimitError,
    AnchoredWorkspaceReader,
    JsonStructureError,
    _strict_json_loads,
    canonical_json_bytes,
    create_json,
    json_sha256,
    read_json_strict_no_repair,
    validate_json_shape,
)
from .transitions import (
    MAX_AUDIT_CAPTURE_BYTES,
    MAX_JSON_DEPTH,
    MAX_JSON_NODES,
    MAX_OBSERVATION_CLOSURE_PATHS,
    MAX_VERSIONED_FILE_BYTES,
    MAX_WORKSPACE_PATHS,
    TransitionAuditService,
)
from .worktrees import (
    RepositoryBinding,
    WorktreeError,
    bind_repository,
    read_repository_snapshot,
    read_repository_tree_entries,
    resolve_repository_commit,
    run_git,
)


_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TRANSITION = re.compile(r"^T-[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?$")
_AUDIT_FIELDS = {
    "schema_version",
    "transition_id",
    "base_commit",
    "current_head",
    "proposal_blob_sha256",
    "path_receipts",
    "observation_closure",
    "assimilation_links",
    "audit_payload_sha256",
    "candidate_subject_sha256",
    "mechanically_valid",
    "issues",
}
_PREPARED_FIELDS = {
    "schema_version",
    "transition_id",
    "prepared_ref",
    "proposal_ref",
    "proposal_blob_sha256",
    "canonical_ref",
    "base_commit",
    "audit_payload_sha256",
    "audit_file_sha256",
    "candidate_subject_sha256",
    "candidate_tree",
    "message_sha256",
    "candidate_paths",
    "index_ref",
    "index_sha256",
}
_SERVICE_COMMIT_FIELDS = {
    "apparatus_commit",
    "candidate_commit",
    "child_commit",
    "return_commit",
}
MAX_MESSAGE_BYTES = 1_048_576
MAX_AUDIT_FILE_BYTES = MAX_VERSIONED_FILE_BYTES
MAX_PREPARED_BYTES = 4_194_304


class CheckpointError(ValueError):
    """An audited checkpoint candidate cannot be prepared exactly."""


@dataclass(frozen=True)
class CandidatePathReceipt:
    path: str
    mode: str
    blob_oid: str
    content_sha256: str


@dataclass(frozen=True)
class PreparedCheckpoint:
    transition_id: str
    prepared_ref: str
    candidate_subject_sha256: str
    audit_payload_sha256: str
    audit_testimony: Mapping[str, object]
    base_commit: str
    candidate_tree: str
    candidate_paths: tuple[CandidatePathReceipt, ...]
    proposal_ref: str
    proposal_blob_sha256: str
    message_sha256: str
    canonical_ref: str
    index_ref: str
    index_sha256: str


@dataclass(frozen=True)
class _FileSnapshot:
    exists: bool
    identity: tuple[int, int, int, int, int, int] | None
    content: bytes | None


@dataclass(frozen=True)
class _ExpectedPath:
    path: str
    mode: str
    blob_oid: str | None
    content_sha256: str | None
    exact_content: bytes | None = None


class CheckpointService:
    """Prepare one immutable candidate tree from a valid transition audit."""

    def __init__(
        self,
        candidate_root: str | Path,
        *,
        canonical_repository: RepositoryBinding,
        canonical_ref: str,
        audit_service: TransitionAuditService | None = None,
    ):
        try:
            candidate = bind_repository(candidate_root)
            if not isinstance(canonical_repository, RepositoryBinding):
                raise WorktreeError("canonical repository binding is required")
            if bind_repository(canonical_repository.root) != canonical_repository:
                raise WorktreeError("canonical repository binding changed")
            if (
                not isinstance(canonical_ref, str)
                or not canonical_ref.startswith("refs/")
                or "\x00" in canonical_ref
            ):
                raise WorktreeError("canonical_ref must be a full Git ref")
            canonical_ref.encode("utf-8")
            resolve_repository_commit(canonical_repository, canonical_ref)
            resolve_repository_commit(candidate, canonical_ref)
            service = audit_service or TransitionAuditService(
                candidate.root,
                canonical_ref=canonical_ref,
            )
            if (
                not isinstance(service, TransitionAuditService)
                or service.repository != candidate
                or service.root != candidate.root
                or service.canonical_ref != canonical_ref
            ):
                raise WorktreeError(
                    "audit service must be bound to the candidate and canonical ref"
                )
        except (OSError, TypeError, UnicodeError, ValueError, WorktreeError) as error:
            raise CheckpointError(f"invalid checkpoint host context: {error}") from error
        self.candidate_repository = candidate
        self.canonical_repository = canonical_repository
        self.canonical_ref = canonical_ref
        self.audit_service = service

    def prepare(self, proposal_ref: str, message: str) -> PreparedCheckpoint:
        """Prepare an exact candidate tree without changing HEAD or any ref."""
        transition_id = _proposal_identity(proposal_ref)
        message_sha256 = _message_sha256(message)
        try:
            with _checkpoint_lock(self.candidate_repository.root, transition_id):
                return self._prepare_locked(
                    transition_id,
                    proposal_ref,
                    message_sha256,
                )
        except CheckpointError:
            raise
        except (
            AnchoredReadError,
            JsonStructureError,
            OSError,
            TypeError,
            UnicodeError,
            ValueError,
            WorktreeError,
        ) as error:
            raise CheckpointError(
                f"checkpoint preparation failed: {type(error).__name__}: {error}"
            ) from error

    def _prepare_locked(
        self,
        transition_id: str,
        proposal_ref: str,
        message_sha256: str,
    ) -> PreparedCheckpoint:
        self._require_bindings()
        candidate_before = read_repository_snapshot(self.candidate_repository)
        canonical_before = read_repository_snapshot(self.canonical_repository)
        candidate_ref = resolve_repository_commit(
            self.candidate_repository,
            self.canonical_ref,
        )
        canonical_ref = resolve_repository_commit(
            self.canonical_repository,
            self.canonical_ref,
        )
        user_index = _snapshot_file(self.candidate_repository.git_dir / "index")

        audit = self.audit_service.audit(proposal_ref)
        _validate_audit_testimony(audit, transition_id)
        if audit["mechanically_valid"] is not True:
            codes = sorted(
                str(issue.get("code", "invalid_audit"))
                for issue in audit["issues"]
                if isinstance(issue, dict) and issue.get("severity") == "error"
            )
            detail = ",".join(codes) or "invalid_audit"
            raise CheckpointError(
                f"transition audit is not mechanically valid: {detail}"
            )
        base_commit = _required_commit(audit["base_commit"], "audit base_commit")
        if (
            audit["current_head"] != base_commit
            or candidate_ref != base_commit
            or canonical_ref != base_commit
            or candidate_before.get("head") != base_commit
            or canonical_before.get("head") != base_commit
        ):
            raise CheckpointError(
                "candidate and canonical ref/HEAD must equal the audited base"
            )
        self._require_unchanged_authority(
            candidate_before,
            canonical_before,
            base_commit,
            user_index,
        )

        audit_bytes = _stored_json_bytes(audit)
        if len(audit_bytes) > MAX_AUDIT_FILE_BYTES:
            raise CheckpointError("audit testimony exceeds the checkpoint bound")
        audit_ref = f"transitions/{transition_id}/audit.json"
        expected = _expected_candidate_paths(
            audit,
            proposal_ref,
            audit_ref,
            audit_bytes,
        )
        observation_paths = _observation_path_expectations(audit)
        checkpoint_root = _ensure_runtime_directory(
            self.candidate_repository.root,
            (".aros", "checkpoints", transition_id),
        )
        staged = _staged_paths(
            self.candidate_repository,
            base_commit,
            user_index,
            checkpoint_root,
        )
        overlap = sorted(staged & set(expected))
        if overlap:
            raise CheckpointError(
                f"ordinary index overlaps audited candidate paths: {overlap}"
            )

        audit_path = self.candidate_repository.root / audit_ref
        _create_once_audit(audit_path, audit_bytes, audit)
        index_path = checkpoint_root / "index"
        index_ref = index_path.relative_to(self.candidate_repository.root).as_posix()
        captured: dict[str, bytes] = {}
        receipts: tuple[CandidatePathReceipt, ...]
        candidate_tree: str

        with AnchoredWorkspaceReader(
            self.candidate_repository.root,
            max_capture_bytes=MAX_AUDIT_CAPTURE_BYTES,
        ) as reader:
            reader.require_repository(
                self.candidate_repository.root,
                self.candidate_repository.git_dir,
                self.candidate_repository.common_dir,
            )
            built: list[CandidatePathReceipt] = []
            for item in expected.values():
                raw, mode = _read_anchored_candidate(reader, item.path)
                digest = hashlib.sha256(raw).hexdigest()
                blob_oid = _blob_oid(raw)
                if (
                    mode != item.mode
                    or (item.blob_oid is not None and blob_oid != item.blob_oid)
                    or (
                        item.content_sha256 is not None
                        and digest != item.content_sha256
                    )
                    or (
                        item.exact_content is not None
                        and raw != item.exact_content
                    )
                ):
                    raise CheckpointError(
                        f"audited candidate path changed: {item.path}"
                    )
                captured[item.path] = raw
                built.append(
                    CandidatePathReceipt(
                        path=item.path,
                        mode=mode,
                        blob_oid=blob_oid,
                        content_sha256=digest,
                    )
                )
            for item in observation_paths.values():
                if item.path in captured:
                    raw = captured[item.path]
                    mode = expected[item.path].mode
                else:
                    raw, mode = _read_anchored_candidate(reader, item.path)
                    captured[item.path] = raw
                if mode != item.mode or _blob_oid(raw) != item.blob_oid:
                    raise CheckpointError(
                        f"audited observation path changed: {item.path}"
                    )
            receipts = tuple(sorted(built, key=lambda item: item.path))
            _initialize_index(
                self.canonical_repository,
                index_path,
                base_commit,
            )
            for receipt in receipts:
                _stage_blob(
                    self.canonical_repository,
                    index_path,
                    receipt,
                    captured[receipt.path],
                )
            commits = _audited_commit_oids(audit, captured)
            _import_commit_objects(
                self.canonical_repository,
                self.candidate_repository,
                commits,
            )
            candidate_tree = _write_tree(self.canonical_repository, index_path)
            _verify_candidate_tree(
                self.canonical_repository,
                base_commit,
                candidate_tree,
                receipts,
                transition_id,
            )

        repeated_audit = self.audit_service.audit(proposal_ref)
        if repeated_audit != audit:
            raise CheckpointError("transition audit changed during preparation")
        self._require_unchanged_authority(
            candidate_before,
            canonical_before,
            base_commit,
            user_index,
        )
        index_sha256 = hashlib.sha256(_read_plain_file(index_path)).hexdigest()
        prepared_ref = f".aros/checkpoints/{transition_id}/prepared.json"
        record: dict[str, object] = {
            "schema_version": 1,
            "transition_id": transition_id,
            "prepared_ref": prepared_ref,
            "proposal_ref": proposal_ref,
            "proposal_blob_sha256": audit["proposal_blob_sha256"],
            "canonical_ref": self.canonical_ref,
            "base_commit": base_commit,
            "audit_payload_sha256": audit["audit_payload_sha256"],
            "audit_file_sha256": hashlib.sha256(audit_bytes).hexdigest(),
            "candidate_subject_sha256": audit["candidate_subject_sha256"],
            "candidate_tree": candidate_tree,
            "message_sha256": message_sha256,
            "candidate_paths": [
                {
                    "path": receipt.path,
                    "mode": receipt.mode,
                    "blob_oid": receipt.blob_oid,
                    "content_sha256": receipt.content_sha256,
                }
                for receipt in receipts
            ],
            "index_ref": index_ref,
            "index_sha256": index_sha256,
        }
        prepared_path = self.candidate_repository.root / prepared_ref
        existing = _read_prepared_if_present(prepared_path)
        if existing is not None and existing != record:
            raise CheckpointError("prepared checkpoint retry conflicts with existing record")
        if existing is None and not create_json(prepared_path, record):
            existing = _read_prepared_if_present(prepared_path)
            if existing != record:
                raise CheckpointError(
                    "prepared checkpoint create-once publication conflict"
                )
        if _read_prepared_if_present(prepared_path) != record:
            raise CheckpointError("prepared checkpoint record changed after publication")
        self._require_unchanged_authority(
            candidate_before,
            canonical_before,
            base_commit,
            user_index,
        )
        return PreparedCheckpoint(
            transition_id=transition_id,
            prepared_ref=prepared_ref,
            candidate_subject_sha256=str(audit["candidate_subject_sha256"]),
            audit_payload_sha256=str(audit["audit_payload_sha256"]),
            audit_testimony=_freeze_mapping(audit),
            base_commit=base_commit,
            candidate_tree=candidate_tree,
            candidate_paths=receipts,
            proposal_ref=proposal_ref,
            proposal_blob_sha256=str(audit["proposal_blob_sha256"]),
            message_sha256=message_sha256,
            canonical_ref=self.canonical_ref,
            index_ref=index_ref,
            index_sha256=index_sha256,
        )

    def _require_bindings(self) -> None:
        if bind_repository(self.candidate_repository.root) != self.candidate_repository:
            raise CheckpointError("candidate repository binding changed")
        if bind_repository(self.canonical_repository.root) != self.canonical_repository:
            raise CheckpointError("canonical repository binding changed")

    def _require_unchanged_authority(
        self,
        candidate_before: dict[str, object],
        canonical_before: dict[str, object],
        base_commit: str,
        user_index: _FileSnapshot,
    ) -> None:
        self._require_bindings()
        if (
            read_repository_snapshot(self.candidate_repository) != candidate_before
            or read_repository_snapshot(self.canonical_repository) != canonical_before
            or resolve_repository_commit(
                self.candidate_repository,
                self.canonical_ref,
            )
            != base_commit
            or resolve_repository_commit(
                self.canonical_repository,
                self.canonical_ref,
            )
            != base_commit
        ):
            raise CheckpointError("repository root, HEAD, or canonical ref drifted")
        if _snapshot_file(self.candidate_repository.git_dir / "index") != user_index:
            raise CheckpointError("ordinary user index drifted during checkpoint preparation")


def _proposal_identity(proposal_ref: object) -> str:
    if not isinstance(proposal_ref, str) or "\x00" in proposal_ref:
        raise CheckpointError("proposal_ref must be a canonical transition proposal path")
    try:
        proposal_ref.encode("utf-8")
    except UnicodeError as error:
        raise CheckpointError("proposal_ref must be valid UTF-8") from error
    parts = PurePosixPath(proposal_ref).parts
    if (
        len(parts) != 3
        or parts[0] != "transitions"
        or parts[2] != "proposal.json"
        or PurePosixPath(*parts).as_posix() != proposal_ref
        or _TRANSITION.fullmatch(parts[1]) is None
    ):
        raise CheckpointError("proposal_ref must identify transitions/T-*/proposal.json")
    return parts[1]


def _message_sha256(message: object) -> str:
    if not isinstance(message, str):
        raise CheckpointError("checkpoint message must be a string")
    try:
        encoded = message.encode("utf-8")
    except UnicodeError as error:
        raise CheckpointError("checkpoint message must be valid UTF-8") from error
    if len(encoded) > MAX_MESSAGE_BYTES:
        raise CheckpointError("checkpoint message exceeds 1048576 bytes")
    return hashlib.sha256(encoded).hexdigest()


def _validate_audit_testimony(
    audit: object,
    transition_id: str,
) -> None:
    if not isinstance(audit, dict) or set(audit) != _AUDIT_FIELDS:
        raise CheckpointError("transition audit testimony has an invalid shape")
    try:
        validate_json_shape(
            audit,
            max_depth=MAX_JSON_DEPTH,
            max_nodes=max(MAX_JSON_NODES, 50_000),
        )
        encoded = canonical_json_bytes(audit)
    except (JsonStructureError, TypeError, UnicodeError, ValueError) as error:
        raise CheckpointError(f"transition audit testimony is invalid: {error}") from error
    if len(encoded) > MAX_AUDIT_FILE_BYTES:
        raise CheckpointError("transition audit testimony exceeds the checkpoint bound")
    if (
        audit.get("schema_version") != 1
        or audit.get("transition_id") != transition_id
        or type(audit.get("mechanically_valid")) is not bool
        or not isinstance(audit.get("issues"), list)
        or not isinstance(audit.get("path_receipts"), list)
        or not isinstance(audit.get("observation_closure"), list)
    ):
        raise CheckpointError("transition audit testimony has invalid identity fields")
    payload = {
        key: value
        for key, value in audit.items()
        if key not in {"audit_payload_sha256", "candidate_subject_sha256"}
    }
    if audit.get("audit_payload_sha256") != json_sha256(payload):
        raise CheckpointError("transition audit payload hash does not match testimony")
    workspace: list[list[object]] = []
    for receipt in audit["path_receipts"]:
        if not isinstance(receipt, dict):
            raise CheckpointError("transition audit path receipt is invalid")
        try:
            workspace.append(
                [receipt["path"], receipt["owner"], receipt["blob_oid"]]
            )
        except KeyError as error:
            raise CheckpointError("transition audit path receipt is incomplete") from error
    derived: list[list[object]] = []
    for record in audit["observation_closure"]:
        if not isinstance(record, dict) or not isinstance(record.get("paths"), list):
            raise CheckpointError("transition audit observation closure is invalid")
        for path in record["paths"]:
            if not isinstance(path, dict):
                raise CheckpointError("transition audit closure path is invalid")
            if path.get("state") == "derived":
                derived.append([path.get("path"), path.get("blob_oid")])
    subject = {
        "schema_version": 1,
        "transition_id": transition_id,
        "base_commit": audit["base_commit"],
        "workspace": sorted(workspace),
        "observation_closure": sorted(derived),
        "proposal_blob_sha256": audit["proposal_blob_sha256"],
        "audit_payload_sha256": audit["audit_payload_sha256"],
    }
    if audit.get("candidate_subject_sha256") != json_sha256(subject):
        raise CheckpointError("transition audit candidate subject hash is invalid")


def _expected_candidate_paths(
    audit: dict[str, object],
    proposal_ref: str,
    audit_ref: str,
    audit_bytes: bytes,
) -> dict[str, _ExpectedPath]:
    proposal_sha256 = _required_sha256(
        audit["proposal_blob_sha256"],
        "proposal blob SHA-256",
    )
    expected = {
        proposal_ref: _ExpectedPath(
            proposal_ref,
            "100644",
            None,
            proposal_sha256,
        ),
        audit_ref: _ExpectedPath(
            audit_ref,
            "100644",
            _blob_oid(audit_bytes),
            hashlib.sha256(audit_bytes).hexdigest(),
            audit_bytes,
        ),
    }
    receipts = audit["path_receipts"]
    assert isinstance(receipts, list)
    for receipt in receipts:
        if not isinstance(receipt, dict):
            raise CheckpointError("audit path receipt is invalid")
        item = _expected_from_receipt(receipt)
        _add_expected(expected, item)
    closure = audit["observation_closure"]
    assert isinstance(closure, list)
    for record in closure:
        if not isinstance(record, dict) or not isinstance(record.get("paths"), list):
            raise CheckpointError("audit observation closure is invalid")
        for raw_path in record["paths"]:
            if not isinstance(raw_path, dict):
                raise CheckpointError("audit observation path is invalid")
            state = raw_path.get("state")
            if state not in {"workspace", "ref_only", "derived"}:
                raise CheckpointError("audit observation path state is invalid")
            if state != "derived":
                continue
            item = _ExpectedPath(
                _candidate_path(raw_path.get("path")),
                _required_mode(raw_path.get("mode")),
                _required_commit(raw_path.get("blob_oid"), "audit blob OID"),
                None,
            )
            _add_expected(expected, item)
    if len(expected) > MAX_WORKSPACE_PATHS + MAX_OBSERVATION_CLOSURE_PATHS + 2:
        raise CheckpointError("too many checkpoint candidate paths")
    return dict(sorted(expected.items()))


def _expected_from_receipt(receipt: dict[str, object]) -> _ExpectedPath:
    owner = receipt.get("owner")
    if owner not in {"semantic", "task", "run", "eval"}:
        raise CheckpointError("audit path receipt owner is invalid")
    return _ExpectedPath(
        _candidate_path(receipt.get("path")),
        _required_mode(receipt.get("mode")),
        _required_commit(receipt.get("blob_oid"), "audit blob OID"),
        _required_sha256(receipt.get("content_sha256"), "audit content SHA-256"),
    )


def _observation_path_expectations(
    audit: dict[str, object],
) -> dict[str, _ExpectedPath]:
    expected: dict[str, _ExpectedPath] = {}
    closure = audit["observation_closure"]
    assert isinstance(closure, list)
    for record in closure:
        if not isinstance(record, dict) or not isinstance(record.get("paths"), list):
            raise CheckpointError("audit observation closure is invalid")
        for raw_path in record["paths"]:
            if not isinstance(raw_path, dict) or raw_path.get("state") not in {
                "workspace",
                "ref_only",
                "derived",
            }:
                raise CheckpointError("audit observation path is invalid")
            item = _ExpectedPath(
                _candidate_path(raw_path.get("path")),
                _required_mode(raw_path.get("mode")),
                _required_commit(raw_path.get("blob_oid"), "audit blob OID"),
                None,
            )
            prior = expected.get(item.path)
            if prior is not None and prior != item:
                raise CheckpointError(
                    f"conflicting observation receipts for path: {item.path}"
                )
            expected[item.path] = item
    return dict(sorted(expected.items()))


def _add_expected(
    expected: dict[str, _ExpectedPath],
    item: _ExpectedPath,
) -> None:
    prior = expected.get(item.path)
    if prior is not None and prior != item:
        raise CheckpointError(f"conflicting audit receipts for path: {item.path}")
    expected[item.path] = item


def _candidate_path(value: object) -> str:
    candidate = PurePosixPath(value) if isinstance(value, str) else PurePosixPath()
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or "\\" in value
        or candidate.is_absolute()
        or candidate.as_posix() != value
        or any(part in {"", ".", ".."} for part in candidate.parts)
        or value.startswith((".aros/", ".git/"))
    ):
        raise CheckpointError(f"invalid checkpoint candidate path: {value!r}")
    try:
        value.encode("utf-8")
    except UnicodeError as error:
        raise CheckpointError("checkpoint candidate path must be UTF-8") from error
    return value


def _required_mode(value: object) -> str:
    if value != "100644":
        raise CheckpointError("checkpoint candidate modes must be 100644")
    return "100644"


def _required_commit(value: object, description: str) -> str:
    if not isinstance(value, str) or _COMMIT.fullmatch(value) is None:
        raise CheckpointError(f"{description} must be 40 lowercase hex")
    return value


def _required_sha256(value: object, description: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise CheckpointError(f"{description} must be 64 lowercase hex")
    return value


def _read_anchored_candidate(
    reader: AnchoredWorkspaceReader,
    relative: str,
) -> tuple[bytes, str]:
    key = reader._workspace_file_key(relative)
    with reader._open_file(key) as (descriptor, anchored):
        if anchored.identity[4] > MAX_VERSIONED_FILE_BYTES:
            raise CheckpointError(f"checkpoint candidate file exceeds bound: {relative}")
        mode = "100755" if stat.S_IMODE(anchored.identity[2]) & 0o111 else "100644"
        try:
            reader._reserve_capture(key, anchored.identity[4])
        except AnchoredReadLimitError as error:
            raise CheckpointError(str(error)) from error
        payload, _size, _digest = reader._stream_file(
            descriptor,
            anchored,
            capture=True,
            capture_limit=None,
        )
    assert payload is not None
    return payload, mode


def _initialize_index(
    repository: RepositoryBinding,
    index_path: Path,
    base_commit: str,
) -> None:
    try:
        metadata = index_path.lstat()
    except FileNotFoundError:
        pass
    else:
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
        ):
            raise CheckpointError("checkpoint index is not a single-link plain file")
        index_path.unlink()
        _fsync_directory(index_path.parent)
    _git_success(
        run_git(repository, "read-tree", base_commit, index_file=index_path),
        "initialize checkpoint index",
    )
    metadata = index_path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise CheckpointError("Git did not create a plain checkpoint index")
    index_path.chmod(0o600)
    _fsync_file(index_path)
    _fsync_directory(index_path.parent)


def _stage_blob(
    repository: RepositoryBinding,
    index_path: Path,
    receipt: CandidatePathReceipt,
    content: bytes,
) -> None:
    result = _git_success(
        run_git(
            repository,
            "hash-object",
            "-w",
            "--stdin",
            input_bytes=content,
            index_file=index_path,
        ),
        f"write checkpoint blob: {receipt.path}",
    )
    try:
        observed = result.stdout.decode("ascii").strip()
    except UnicodeError as error:
        raise CheckpointError("Git hash-object returned non-ASCII output") from error
    if observed != receipt.blob_oid:
        raise CheckpointError(f"Git blob OID mismatch: {receipt.path}")
    _git_success(
        run_git(
            repository,
            "update-index",
            "--add",
            "--cacheinfo",
            f"{receipt.mode},{receipt.blob_oid},{receipt.path}",
            index_file=index_path,
        ),
        f"stage checkpoint blob: {receipt.path}",
    )


def _write_tree(repository: RepositoryBinding, index_path: Path) -> str:
    result = _git_success(
        run_git(repository, "write-tree", index_file=index_path),
        "write checkpoint tree",
    )
    try:
        tree = result.stdout.decode("ascii").strip()
    except UnicodeError as error:
        raise CheckpointError("Git write-tree returned non-ASCII output") from error
    if _COMMIT.fullmatch(tree) is None:
        raise CheckpointError("Git write-tree returned an invalid tree OID")
    kind = _git_success(
        run_git(repository, "cat-file", "-t", tree),
        "validate checkpoint tree",
    ).stdout
    if kind != b"tree\n":
        raise CheckpointError("checkpoint candidate object is not a tree")
    index_path.chmod(0o600)
    _fsync_file(index_path)
    _fsync_directory(index_path.parent)
    return tree


def _verify_candidate_tree(
    repository: RepositoryBinding,
    base_commit: str,
    candidate_tree: str,
    receipts: tuple[CandidatePathReceipt, ...],
    transition_id: str,
) -> None:
    result = _git_success(
        run_git(
            repository,
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "-r",
            "-z",
            "--no-renames",
            base_commit,
            candidate_tree,
            "--",
        ),
        "verify checkpoint tree delta",
    )
    changed = _decode_paths(result.stdout, "checkpoint tree delta")
    expected = {receipt.path for receipt in receipts}
    if changed != expected:
        raise CheckpointError("checkpoint tree differs outside exact audited paths")
    entries = {
        entry.path: entry
        for entry in read_repository_tree_entries(
            repository,
            candidate_tree,
            expected,
        )
    }
    if set(entries) != expected:
        raise CheckpointError("checkpoint tree is missing an audited path")
    for receipt in receipts:
        entry = entries[receipt.path]
        if (
            entry.kind != "blob"
            or entry.mode != receipt.mode
            or entry.oid != receipt.blob_oid
        ):
            raise CheckpointError(
                f"checkpoint tree receipt mismatch: {receipt.path}"
            )
    admission = f"transitions/{transition_id}/admission.json"
    if read_repository_tree_entries(repository, candidate_tree, (admission,)):
        raise CheckpointError("checkpoint candidate tree already contains admission.json")


def _audited_commit_oids(
    audit: dict[str, object],
    captured: Mapping[str, bytes],
) -> tuple[str, ...]:
    commits: set[str] = set()
    closure = audit["observation_closure"]
    assert isinstance(closure, list)
    for record in closure:
        if not isinstance(record, dict):
            raise CheckpointError("audit observation record is invalid")
        value = record.get("candidate_commit")
        if value is not None:
            commits.add(_required_commit(value, "audited candidate commit"))
    for path, raw in captured.items():
        if not path.endswith(".json") or not path.startswith(
            ("tasks/", "runs/", "eval/")
        ):
            continue
        try:
            value = _strict_json_loads(raw)
            validate_json_shape(
                value,
                max_depth=MAX_JSON_DEPTH,
                max_nodes=MAX_JSON_NODES,
            )
        except (JsonStructureError, TypeError, UnicodeError, ValueError) as error:
            raise CheckpointError(
                f"audited service record is no longer strict JSON: {path}"
            ) from error
        pending: list[object] = [value]
        while pending:
            item = pending.pop()
            if isinstance(item, dict):
                for key, child in item.items():
                    if key in _SERVICE_COMMIT_FIELDS and child is not None:
                        commits.add(
                            _required_commit(child, f"audited {key}")
                        )
                    pending.append(child)
            elif isinstance(item, list):
                pending.extend(item)
    return tuple(sorted(commits))


def _import_commit_objects(
    canonical: RepositoryBinding,
    candidate: RepositoryBinding,
    commits: tuple[str, ...],
) -> None:
    if not commits:
        return
    for oid in commits:
        _require_exact_commit(candidate, oid)
    if canonical.common_dir == candidate.common_dir:
        for oid in commits:
            _require_exact_commit(canonical, oid)
        return
    refs_before = _git_success(
        run_git(canonical, "for-each-ref", "--format=%(refname)%00%(objectname)"),
        "snapshot canonical refs",
    ).stdout
    fetch_paths = {canonical.git_dir / "FETCH_HEAD", canonical.common_dir / "FETCH_HEAD"}
    fetch_before = {path: _snapshot_file(path) for path in fetch_paths}
    for oid in commits:
        present = run_git(canonical, "cat-file", "-e", f"{oid}^{{commit}}")
        if present.returncode == 0:
            _require_exact_commit(canonical, oid)
            continue
        _git_success(
            run_git(
                canonical,
                "fetch",
                "--no-tags",
                "--no-write-fetch-head",
                str(candidate.root),
                oid,
            ),
            f"import audited commit object: {oid}",
        )
        _require_exact_commit(canonical, oid)
    refs_after = _git_success(
        run_git(canonical, "for-each-ref", "--format=%(refname)%00%(objectname)"),
        "verify canonical refs",
    ).stdout
    if refs_after != refs_before or any(
        _snapshot_file(path) != snapshot
        for path, snapshot in fetch_before.items()
    ):
        raise CheckpointError("audited object import changed a ref or FETCH_HEAD")


def _require_exact_commit(repository: RepositoryBinding, oid: str) -> None:
    result = _git_success(
        run_git(repository, "rev-parse", "--verify", f"{oid}^{{commit}}"),
        f"validate audited commit: {oid}",
    )
    if result.stdout.strip() != oid.encode("ascii"):
        raise CheckpointError(f"audited commit did not resolve exactly: {oid}")


def _staged_paths(
    repository: RepositoryBinding,
    base_commit: str,
    user_index: _FileSnapshot,
    runtime: Path,
) -> set[str]:
    if not user_index.exists:
        return set()
    assert user_index.content is not None
    snapshot_path = runtime / "user-index.snapshot"
    _replace_runtime_file(snapshot_path, user_index.content)
    try:
        result = _git_success(
            run_git(
                repository,
                "diff",
                "--cached",
                "--name-only",
                "-z",
                "--no-renames",
                base_commit,
                "--",
                index_file=snapshot_path,
            ),
            "inspect staged candidate overlap",
        )
        return _decode_paths(result.stdout, "ordinary index")
    finally:
        try:
            snapshot_path.unlink()
        except FileNotFoundError:
            pass
        _fsync_directory(runtime)


def _decode_paths(raw: bytes, description: str) -> set[str]:
    result: set[str] = set()
    for item in (part for part in raw.split(b"\0") if part):
        try:
            path = item.decode("utf-8")
        except UnicodeError as error:
            raise CheckpointError(f"{description} contains a non-UTF-8 path") from error
        canonical = _candidate_path(path)
        if canonical in result:
            raise CheckpointError(f"{description} repeats a path")
        result.add(canonical)
    return result


def _read_prepared_if_present(path: Path) -> dict[str, object] | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_size > MAX_PREPARED_BYTES
    ):
        raise CheckpointError("prepared checkpoint must be a bounded create-once file")
    try:
        value = read_json_strict_no_repair(path)
        validate_json_shape(
            value,
            max_depth=MAX_JSON_DEPTH,
            max_nodes=max(MAX_JSON_NODES, 20_000),
        )
    except (JsonStructureError, OSError, TypeError, UnicodeError, ValueError) as error:
        raise CheckpointError(f"prepared checkpoint record is invalid: {error}") from error
    if not isinstance(value, dict) or set(value) != _PREPARED_FIELDS:
        raise CheckpointError("prepared checkpoint record has an invalid shape")
    return value


def _create_once_audit(
    path: Path,
    content: bytes,
    audit: dict[str, object],
) -> None:
    created = create_json(path, audit)
    existing = _read_plain_file(path, max_bytes=MAX_AUDIT_FILE_BYTES)
    try:
        decoded = _strict_json_loads(existing)
        validate_json_shape(
            decoded,
            max_depth=MAX_JSON_DEPTH,
            max_nodes=max(MAX_JSON_NODES, 50_000),
        )
    except (JsonStructureError, TypeError, UnicodeError, ValueError) as error:
        raise CheckpointError("existing audit file is not strict JSON") from error
    if existing != content or decoded != audit:
        conflict = "publication" if created else "existing"
        raise CheckpointError(f"{conflict} audit file conflicts byte-for-byte")
    if stat.S_IMODE(path.stat().st_mode) & 0o111:
        raise CheckpointError("existing audit file must be non-executable")


@contextmanager
def _checkpoint_lock(root: Path, transition_id: str) -> Iterator[None]:
    lock_root = _ensure_runtime_directory(root, (".aros", "locks"))
    lock_path = lock_root / f"checkpoint-prepare-{transition_id}.lock"
    descriptor = os.open(
        lock_path,
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        metadata = os.fstat(descriptor)
        observed = lock_path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or (metadata.st_dev, metadata.st_ino)
            != (observed.st_dev, observed.st_ino)
        ):
            raise CheckpointError("checkpoint lock must be a single-link plain file")
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        _fsync_directory(lock_root)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _ensure_runtime_directory(root: Path, parts: tuple[str, ...]) -> Path:
    current = root
    for component in parts:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            os.mkdir(current, 0o700)
            _fsync_directory(current.parent)
            metadata = current.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise CheckpointError(f"runtime path must be a plain directory: {current}")
        if current.resolve(strict=True) != current:
            raise CheckpointError(f"runtime path must be exact: {current}")
    return current


def _snapshot_file(path: Path) -> _FileSnapshot:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return _FileSnapshot(False, None, None)
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
    ):
        raise CheckpointError(f"authority file must be a single-link plain file: {path}")
    content = _read_plain_file(path)
    observed = path.lstat()
    identity = (
        observed.st_dev,
        observed.st_ino,
        observed.st_mode,
        observed.st_nlink,
        observed.st_size,
        observed.st_mtime_ns,
    )
    return _FileSnapshot(True, identity, content)


def _read_plain_file(path: Path, *, max_bytes: int | None = None) -> bytes:
    before = path.lstat()
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or (max_bytes is not None and before.st_size > max_bytes)
    ):
        raise CheckpointError(f"path must be a bounded single-link plain file: {path}")
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise CheckpointError(f"file identity changed while opening: {path}")
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(descriptor, 65_536)
            if not chunk:
                break
            size += len(chunk)
            if max_bytes is not None and size > max_bytes:
                raise CheckpointError(f"file exceeds checkpoint bound: {path}")
            chunks.append(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    observed = path.lstat()
    if (
        (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        or (observed.st_dev, observed.st_ino, observed.st_size, observed.st_mtime_ns)
        != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    ):
        raise CheckpointError(f"file changed while reading: {path}")
    return b"".join(chunks)


def _replace_runtime_file(path: Path, content: bytes) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        pass
    else:
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
        ):
            raise CheckpointError("runtime index snapshot is unsafe")
        path.unlink()
    descriptor = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        _write_all(descriptor, content)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(path.parent)


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def _fsync_file(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _git_success(
    result: subprocess.CompletedProcess[bytes],
    operation: str,
) -> subprocess.CompletedProcess[bytes]:
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).decode(
            "utf-8",
            errors="replace",
        ).strip()
        raise CheckpointError(f"{operation} failed: {detail or result.returncode}")
    return result


def _blob_oid(content: bytes) -> str:
    return hashlib.sha1(
        b"blob " + str(len(content)).encode("ascii") + b"\0" + content
    ).hexdigest()


def _stored_json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def _freeze(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _freeze_mapping(value: dict[str, object]) -> Mapping[str, object]:
    frozen = _freeze(value)
    assert isinstance(frozen, Mapping)
    return frozen


__all__ = [
    "CandidatePathReceipt",
    "CheckpointError",
    "CheckpointService",
    "PreparedCheckpoint",
]
