"""Prepare and atomically finalize audited checkpoint transitions."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Protocol

from .store import (
    AnchoredReadError,
    AnchoredReadLimitError,
    AnchoredWorkspaceReader,
    JsonStructureError,
    _strict_json_loads,
    canonical_json_bytes,
    create_json,
    json_sha256,
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
    WorktreeLimitError,
    bind_repository,
    read_tree_into_index,
    read_repository_refs_snapshot,
    read_repository_snapshot,
    read_repository_tree_entries,
    resolve_repository_commit,
    run_checkpoint_ref_transaction,
    run_git,
    update_index_cacheinfo,
    write_index_tree,
)


_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TRANSITION = re.compile(r"^T-[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?$")
_TASK_OBSERVATION = re.compile(
    r"^tasks/(TASK-[0-9]{8}-[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)/"
    r"collected\.json$"
)
_RUN_OBSERVATION = re.compile(
    r"^runs/(RUN-[A-Za-z0-9][A-Za-z0-9-]*)/final\.json$"
)
_EVAL_OBSERVATION = re.compile(
    r"^eval/evaluations/(EVAL-[0-9a-f]{64})/receipt\.json$"
)
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
    "ordinary_index_entries",
    "index_ref",
    "index_sha256",
}
_SERVICE_COMMIT_FIELDS = {
    "apparatus_commit",
    "candidate_commit",
    "child_commit",
    "return_commit",
}
_ADMISSION_RECEIPT_FIELDS = {
    "schemaVersion",
    "decision",
    "candidateSubjectSHA256",
    "auditPayloadSHA256",
    "contractID",
    "revision",
    "specHash",
    "workspaceID",
    "canonicalRef",
    "sessionID",
    "promptID",
    "attempt",
    "attemptKey",
    "leaseOwner",
    "leaseExpiresAt",
    "capability",
    "budgetBefore",
    "charge",
    "budgetRemaining",
    "evaluatorPolicyRefs",
    "researchContractBindingSHA256",
    "auditImplementationID",
    "trustedExecutionClosureSHA256",
    "enforcementClass",
    "authorityDomainID",
    "issuedAt",
    "receiptSHA256",
}
_FINALIZE_FENCE_FIELDS = {
    "schemaVersion",
    "receiptSHA256",
    "reservationID",
    "revision",
    "researchContractBindingSHA256",
    "sessionID",
    "promptID",
    "attempt",
    "attemptKey",
    "leaseOwner",
    "leaseExpiresAt",
    "issuedAt",
    "expiresAt",
    "fenceSHA256",
}
_BUDGET_SNAPSHOT_FIELDS = {"turns", "actions", "deadline"}
_BUDGET_COUNTER_FIELDS = {"limit", "used", "remaining"}
_FENCE_RECEIPT_BINDINGS = {
    "revision",
    "researchContractBindingSHA256",
    "sessionID",
    "promptID",
    "attempt",
    "attemptKey",
    "leaseOwner",
    "leaseExpiresAt",
}
MAX_MESSAGE_BYTES = 1_048_576
MAX_AUDIT_FILE_BYTES = MAX_VERSIONED_FILE_BYTES
MAX_PREPARED_BYTES = 4_194_304
MAX_CHECKPOINT_INDEX_BYTES = 4_194_304
MAX_ADMISSION_RECEIPT_BYTES = 1_048_576
MAX_FINALIZE_FENCE_BYTES = 65_536
MAX_FETCH_HEAD_BYTES = 1_048_576
MAX_CANONICAL_REFS = 20_000
MAX_CANONICAL_REF_SNAPSHOT_BYTES = 4_194_304


class CheckpointError(ValueError):
    """An audited checkpoint candidate cannot be prepared exactly."""


class AdmissionGateway(Protocol):
    """Broker-owned admission authority kept outside model-controlled inputs."""

    def admit_transition(
        self,
        *,
        candidate_subject_sha256: str,
        audit_payload_sha256: str,
        audit_testimony: Mapping[str, object],
    ) -> bytes: ...

    def revalidate_transition(self, receipt: bytes) -> bytes: ...


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


@dataclass(frozen=True)
class _FinalizationState:
    record: dict[str, object]
    prepared_bytes: bytes
    audit: dict[str, object]
    audit_bytes: bytes
    message_bytes: bytes
    index_bytes: bytes
    receipts: tuple[CandidatePathReceipt, ...]
    observation_updates: tuple[tuple[str, str], ...]


class CheckpointService:
    """Prepare one immutable candidate tree from a valid transition audit."""

    def __init__(
        self,
        candidate_root: str | Path,
        *,
        canonical_repository: RepositoryBinding,
        canonical_ref: str,
        audit_service: TransitionAuditService | None = None,
        gateway: AdmissionGateway | None = None,
        clock: Callable[[], int] | None = None,
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
            if clock is not None and not callable(clock):
                raise TypeError("checkpoint clock must be callable")
        except (OSError, TypeError, UnicodeError, ValueError, WorktreeError) as error:
            raise CheckpointError(f"invalid checkpoint host context: {error}") from error
        self.candidate_repository = candidate
        self.canonical_repository = canonical_repository
        self.canonical_ref = canonical_ref
        self.audit_service = service
        self.gateway = gateway
        self.clock = clock or _epoch_milliseconds

    def prepare(self, proposal_ref: str, message: str) -> PreparedCheckpoint:
        """Prepare an exact candidate tree without changing HEAD or any ref."""
        transition_id = _proposal_identity(proposal_ref)
        message_bytes = _message_bytes(message)
        message_sha256 = hashlib.sha256(message_bytes).hexdigest()
        try:
            with _checkpoint_lock(self.candidate_repository.root, transition_id):
                return self._prepare_locked(
                    transition_id,
                    proposal_ref,
                    message_sha256,
                    message_bytes,
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

    def checkpoint(self, proposal_ref: str, message: str) -> dict[str, object]:
        """Prepare, admit once, revalidate once, and finalize one transition."""
        if self.gateway is None:
            raise CheckpointError("checkpoint requires an injected admission gateway")
        prepared = self.prepare(proposal_ref, message)
        receipt = self.gateway.admit_transition(
            candidate_subject_sha256=prepared.candidate_subject_sha256,
            audit_payload_sha256=prepared.audit_payload_sha256,
            audit_testimony=prepared.audit_testimony,
        )
        if not isinstance(receipt, bytes):
            raise CheckpointError("admission gateway must return exact receipt bytes")
        fence = self.gateway.revalidate_transition(receipt)
        if not isinstance(fence, bytes):
            raise CheckpointError("admission gateway must return exact fence bytes")
        return self.finalize(prepared.prepared_ref, receipt, fence)

    def finalize(
        self,
        prepared_ref: str,
        admission_receipt: bytes,
        finalize_fence: bytes,
    ) -> dict[str, object]:
        """Finalize one exact prepared checkpoint with an atomic Git CAS."""
        transition_id = _prepared_identity(prepared_ref)
        try:
            with _checkpoint_lock(self.candidate_repository.root, transition_id):
                return self._finalize_locked(
                    transition_id,
                    prepared_ref,
                    admission_receipt,
                    finalize_fence,
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
                f"checkpoint finalization failed: {type(error).__name__}: {error}"
            ) from error

    def _finalize_locked(
        self,
        transition_id: str,
        prepared_ref: str,
        admission_receipt: bytes,
        finalize_fence: bytes,
    ) -> dict[str, object]:
        state = _load_finalization_state(self, prepared_ref, transition_id)
        receipt = _decode_admission_receipt(admission_receipt)
        _require_receipt_binding(receipt, state.record, self.canonical_ref)
        _decode_finalize_fence(
            finalize_fence,
            receipt=receipt,
            now_ms=self.clock(),
        )

        admission_ref = f"transitions/{transition_id}/admission.json"
        final_index = (
            self.candidate_repository.root
            / ".aros"
            / "checkpoints"
            / transition_id
            / "index-final"
        )
        _replace_runtime_file(final_index, state.index_bytes)
        admission_path_receipt = CandidatePathReceipt(
            path=admission_ref,
            mode="100644",
            blob_oid=_blob_oid(admission_receipt),
            content_sha256=hashlib.sha256(admission_receipt).hexdigest(),
        )
        _stage_blob(
            self.canonical_repository,
            final_index,
            admission_path_receipt,
            admission_receipt,
        )
        final_tree = _write_tree(self.canonical_repository, final_index)
        final_index_snapshot = _snapshot_file(
            final_index,
            max_bytes=MAX_CHECKPOINT_INDEX_BYTES,
        )
        if final_index_snapshot.content is None:
            raise CheckpointError("final checkpoint index disappeared after tree write")
        candidate_tree = str(state.record["candidate_tree"])
        _verify_final_tree(
            self.canonical_repository,
            candidate_tree,
            final_tree,
            admission_path_receipt,
        )
        base_commit = str(state.record["base_commit"])
        commit = _commit_final_tree(
            self.canonical_repository,
            final_tree,
            base_commit,
            state.message_bytes,
        )

        repeated = _load_finalization_state(self, prepared_ref, transition_id)
        if repeated != state:
            raise CheckpointError("prepared checkpoint state drifted before CAS")
        _verify_final_tree(
            self.canonical_repository,
            candidate_tree,
            final_tree,
            admission_path_receipt,
        )
        _verify_final_commit(
            self.canonical_repository,
            commit,
            final_tree,
            base_commit,
            state.message_bytes,
        )
        receipt = _decode_admission_receipt(admission_receipt)
        _require_receipt_binding(receipt, repeated.record, self.canonical_ref)
        _decode_finalize_fence(
            finalize_fence,
            receipt=receipt,
            now_ms=self.clock(),
        )
        if (
            _snapshot_file(
                final_index,
                max_bytes=MAX_CHECKPOINT_INDEX_BYTES,
            )
            != final_index_snapshot
        ):
            raise CheckpointError("final checkpoint index drifted before CAS")
        _verify_index_snapshot_tree(
            self.canonical_repository,
            final_index.parent,
            final_index_snapshot.content,
            final_tree,
        )

        def require_current_fence() -> None:
            _decode_finalize_fence(
                finalize_fence,
                receipt=receipt,
                now_ms=self.clock(),
            )

        _atomic_ref_transaction(
            self.canonical_repository,
            canonical_ref=self.canonical_ref,
            base_commit=base_commit,
            new_commit=commit,
            observation_updates=state.observation_updates,
            validate_current_fence=require_current_fence,
        )
        return {
            "schema_version": 1,
            "transition_id": transition_id,
            "canonical_ref": self.canonical_ref,
            "commit": commit,
            "state": "projection_pending",
        }

    def _prepare_locked(
        self,
        transition_id: str,
        proposal_ref: str,
        message_sha256: str,
        message_bytes: bytes,
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
        user_index = _snapshot_file(
            self.candidate_repository.git_dir / "index",
            max_bytes=MAX_CHECKPOINT_INDEX_BYTES,
        )

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
        prepared_ref = f".aros/checkpoints/{transition_id}/prepared.json"
        index_ref = f".aros/checkpoints/{transition_id}/index"
        audit_file_sha256 = hashlib.sha256(audit_bytes).hexdigest()
        known_binding = {
            "schema_version": 1,
            "transition_id": transition_id,
            "prepared_ref": prepared_ref,
            "proposal_ref": proposal_ref,
            "proposal_blob_sha256": audit["proposal_blob_sha256"],
            "canonical_ref": self.canonical_ref,
            "base_commit": base_commit,
            "audit_payload_sha256": audit["audit_payload_sha256"],
            "audit_file_sha256": audit_file_sha256,
            "candidate_subject_sha256": audit["candidate_subject_sha256"],
            "message_sha256": message_sha256,
            "index_ref": index_ref,
        }
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
        prepared_path = checkpoint_root / "prepared.json"
        message_path = checkpoint_root / "message"
        existing = _read_prepared_if_present(prepared_path)
        if existing is not None and any(
            existing[0][key] != value for key, value in known_binding.items()
        ):
            raise CheckpointError(
                "prepared checkpoint retry conflicts with durable binding"
            )
        existing_message = _read_message_if_present(message_path)
        if existing_message is not None and existing_message != message_bytes:
            raise CheckpointError(
                "checkpoint message artifact conflicts byte-for-byte"
            )
        ordinary_index_entries = _ordinary_index_entries(
            self.candidate_repository,
            user_index,
            set(expected),
            checkpoint_root,
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
        audit_snapshot_before = _snapshot_file(
            audit_path,
            max_bytes=MAX_AUDIT_FILE_BYTES,
        )
        if audit_snapshot_before.exists:
            _require_exact_audit_file(audit_path, audit_bytes, audit)
        index_path = checkpoint_root / "index"
        captured: dict[str, bytes] = {}
        receipts: tuple[CandidatePathReceipt, ...]
        candidate_tree: str
        prepared: PreparedCheckpoint

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
                if item.path == audit_ref:
                    raw, mode = audit_bytes, "100644"
                    if audit_snapshot_before.exists:
                        observed, observed_mode = _read_anchored_candidate(
                            reader,
                            item.path,
                        )
                        if observed != raw or observed_mode != mode:
                            raise CheckpointError(
                                "existing audit file changed before preparation"
                            )
                else:
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
            reader.revalidate()
            index_snapshot = _snapshot_file(
                index_path,
                max_bytes=MAX_CHECKPOINT_INDEX_BYTES,
            )
            if not index_snapshot.exists or index_snapshot.content is None:
                raise CheckpointError("checkpoint index disappeared after tree creation")
            _verify_index_snapshot_tree(
                self.canonical_repository,
                checkpoint_root,
                index_snapshot.content,
                candidate_tree,
            )
            index_sha256 = hashlib.sha256(index_snapshot.content).hexdigest()
            reader.verify_stream(
                index_ref,
                expected_size=len(index_snapshot.content),
                expected_sha256=index_sha256,
                capture_limit=None,
            )
            record: dict[str, object] = {
                "schema_version": 1,
                "transition_id": transition_id,
                "prepared_ref": prepared_ref,
                "proposal_ref": proposal_ref,
                "proposal_blob_sha256": audit["proposal_blob_sha256"],
                "canonical_ref": self.canonical_ref,
                "base_commit": base_commit,
                "audit_payload_sha256": audit["audit_payload_sha256"],
                "audit_file_sha256": audit_file_sha256,
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
                "ordinary_index_entries": ordinary_index_entries,
                "index_ref": index_ref,
                "index_sha256": index_sha256,
            }
            prepared_bytes = _stored_json_bytes(record)
            existing = _read_prepared_if_present(prepared_path)
            if existing is not None and existing != (record, prepared_bytes):
                raise CheckpointError(
                    "prepared checkpoint retry conflicts byte-for-byte"
                )
            _create_once_message(message_path, message_bytes)
            message_snapshot = _snapshot_file(
                message_path,
                max_bytes=MAX_MESSAGE_BYTES,
            )
            _require_exact_message(message_path, message_bytes, message_sha256)
            _create_once_audit(audit_path, audit_bytes, audit)
            observed_audit, audit_mode = _read_anchored_candidate(reader, audit_ref)
            if observed_audit != audit_bytes or audit_mode != "100644":
                raise CheckpointError("published audit file differs from testimony")

            if existing is None and not create_json(prepared_path, record):
                existing = _read_prepared_if_present(prepared_path)
                if existing != (record, prepared_bytes):
                    raise CheckpointError(
                        "prepared checkpoint create-once publication conflict"
                    )
            if _read_prepared_if_present(prepared_path) != (record, prepared_bytes):
                raise CheckpointError(
                    "prepared checkpoint bytes changed after publication"
                )
            reader.verify_stream(
                prepared_ref,
                expected_size=len(prepared_bytes),
                expected_sha256=hashlib.sha256(prepared_bytes).hexdigest(),
                capture_limit=None,
            )
            reader.verify_stream(
                f".aros/checkpoints/{transition_id}/message",
                expected_size=len(message_bytes),
                expected_sha256=message_sha256,
                capture_limit=None,
            )
            reader.revalidate()
            self._require_unchanged_authority(
                candidate_before,
                canonical_before,
                base_commit,
                user_index,
            )
            if (
                _snapshot_file(
                    index_path,
                    max_bytes=MAX_CHECKPOINT_INDEX_BYTES,
                )
                != index_snapshot
            ):
                raise CheckpointError(
                    "checkpoint temp index changed after prepared publication"
                )
            _require_exact_audit_file(audit_path, audit_bytes, audit)
            _verify_candidate_tree(
                self.canonical_repository,
                base_commit,
                candidate_tree,
                receipts,
                transition_id,
            )
            if _read_prepared_if_present(prepared_path) != (record, prepared_bytes):
                raise CheckpointError(
                    "prepared checkpoint bytes changed during final validation"
                )
            if (
                _snapshot_file(
                    index_path,
                    max_bytes=MAX_CHECKPOINT_INDEX_BYTES,
                )
                != index_snapshot
            ):
                raise CheckpointError(
                    "checkpoint temp index changed during final validation"
                )
            if (
                _snapshot_file(
                    message_path,
                    max_bytes=MAX_MESSAGE_BYTES,
                )
                != message_snapshot
            ):
                raise CheckpointError(
                    "checkpoint message artifact changed during final validation"
                )
            _require_exact_message(message_path, message_bytes, message_sha256)
            self._require_unchanged_authority(
                candidate_before,
                canonical_before,
                base_commit,
                user_index,
            )
            reader.revalidate()
            prepared = PreparedCheckpoint(
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
        return prepared

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
        if (
            _snapshot_file(
                self.candidate_repository.git_dir / "index",
                max_bytes=MAX_CHECKPOINT_INDEX_BYTES,
            )
            != user_index
        ):
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


def _prepared_identity(prepared_ref: object) -> str:
    if not isinstance(prepared_ref, str) or "\x00" in prepared_ref:
        raise CheckpointError("prepared_ref must be a canonical checkpoint path")
    try:
        prepared_ref.encode("utf-8")
    except UnicodeError as error:
        raise CheckpointError("prepared_ref must be valid UTF-8") from error
    parts = PurePosixPath(prepared_ref).parts
    if (
        len(parts) != 4
        or parts[:2] != (".aros", "checkpoints")
        or parts[3] != "prepared.json"
        or PurePosixPath(*parts).as_posix() != prepared_ref
        or _TRANSITION.fullmatch(parts[2]) is None
    ):
        raise CheckpointError(
            "prepared_ref must identify .aros/checkpoints/T-*/prepared.json"
        )
    return parts[2]


def _validate_prepared_record(
    value: dict[str, object],
    raw: bytes,
    *,
    prepared_ref: str,
    transition_id: str,
    canonical_ref: str,
) -> tuple[CandidatePathReceipt, ...]:
    if raw != _stored_json_bytes(value):
        raise CheckpointError("prepared checkpoint bytes are not exact canonical storage")
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["transition_id"] != transition_id
        or value["prepared_ref"] != prepared_ref
        or value["canonical_ref"] != canonical_ref
        or value["index_ref"]
        != f".aros/checkpoints/{transition_id}/index"
        or _proposal_identity(value["proposal_ref"]) != transition_id
    ):
        raise CheckpointError("prepared checkpoint identity binding is invalid")
    _required_commit(value["base_commit"], "prepared base_commit")
    _required_commit(value["candidate_tree"], "prepared candidate_tree")
    for field in (
        "proposal_blob_sha256",
        "audit_payload_sha256",
        "audit_file_sha256",
        "candidate_subject_sha256",
        "message_sha256",
        "index_sha256",
    ):
        _required_sha256(value[field], f"prepared {field}")
    raw_receipts = value["candidate_paths"]
    if not isinstance(raw_receipts, list) or len(raw_receipts) > (
        MAX_WORKSPACE_PATHS + MAX_OBSERVATION_CLOSURE_PATHS + 2
    ):
        raise CheckpointError("prepared checkpoint candidate paths are invalid")
    receipts: list[CandidatePathReceipt] = []
    for item in raw_receipts:
        if not isinstance(item, dict) or set(item) != {
            "path",
            "mode",
            "blob_oid",
            "content_sha256",
        }:
            raise CheckpointError("prepared candidate receipt has invalid fields")
        receipts.append(
            CandidatePathReceipt(
                path=_candidate_path(item["path"]),
                mode=_required_mode(item["mode"]),
                blob_oid=_required_commit(
                    item["blob_oid"],
                    "prepared candidate blob OID",
                ),
                content_sha256=_required_sha256(
                    item["content_sha256"],
                    "prepared candidate content SHA-256",
                ),
            )
        )
    ordered = tuple(sorted(receipts, key=lambda item: item.path))
    if tuple(receipts) != ordered or len({item.path for item in ordered}) != len(ordered):
        raise CheckpointError("prepared candidate receipts must be unique and sorted")
    _validate_ordinary_index_entries(
        value["ordinary_index_entries"],
        {item.path for item in ordered},
    )
    return ordered


def _validate_ordinary_index_entries(
    value: object,
    candidate_paths: set[str],
) -> list[dict[str, str]]:
    if not isinstance(value, list) or len(value) > len(candidate_paths):
        raise CheckpointError("prepared ordinary index projection is invalid")
    entries: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {
            "path",
            "mode",
            "blob_oid",
        }:
            raise CheckpointError("prepared ordinary index entry has invalid fields")
        path = _candidate_path(item["path"])
        mode = item["mode"]
        if (
            path not in candidate_paths
            or not isinstance(mode, str)
            or re.fullmatch(r"[0-7]{6}", mode) is None
        ):
            raise CheckpointError("prepared ordinary index entry is invalid")
        entries.append(
            {
                "path": path,
                "mode": mode,
                "blob_oid": _required_commit(
                    item["blob_oid"],
                    "prepared ordinary index blob OID",
                ),
            }
        )
    ordered = sorted(entries, key=lambda item: item["path"])
    if entries != ordered or len({item["path"] for item in entries}) != len(entries):
        raise CheckpointError("prepared ordinary index entries must be unique and sorted")
    return entries


def _load_finalization_state(
    service: CheckpointService,
    prepared_ref: str,
    transition_id: str,
) -> _FinalizationState:
    service._require_bindings()
    prepared_path = service.candidate_repository.root / prepared_ref
    loaded = _read_prepared_if_present(prepared_path)
    if loaded is None:
        raise CheckpointError("prepared checkpoint record is missing")
    record, prepared_bytes = loaded
    receipts = _validate_prepared_record(
        record,
        prepared_bytes,
        prepared_ref=prepared_ref,
        transition_id=transition_id,
        canonical_ref=service.canonical_ref,
    )
    base_commit = str(record["base_commit"])
    for description, repository in (
        ("candidate", service.candidate_repository),
        ("canonical", service.canonical_repository),
    ):
        snapshot = read_repository_snapshot(repository)
        if snapshot.get("head") != base_commit or snapshot.get("ref") != service.canonical_ref:
            raise CheckpointError(
                f"{description} HEAD/ref drifted from prepared base"
            )
        if resolve_repository_commit(repository, service.canonical_ref) != base_commit:
            raise CheckpointError(
                f"{description} canonical ref drifted from prepared base"
            )

    checkpoint_root = prepared_path.parent
    message_path = checkpoint_root / "message"
    message_bytes = _read_message_if_present(message_path)
    if message_bytes is None:
        raise CheckpointError("prepared checkpoint message artifact is missing")
    _require_exact_message(
        message_path,
        message_bytes,
        str(record["message_sha256"]),
    )

    audit_ref = f"transitions/{transition_id}/audit.json"
    audit_path = service.candidate_repository.root / audit_ref
    audit_bytes = _read_plain_file(audit_path, max_bytes=MAX_AUDIT_FILE_BYTES)
    if hashlib.sha256(audit_bytes).hexdigest() != record["audit_file_sha256"]:
        raise CheckpointError("prepared audit file hash does not match")
    try:
        audit_value = _strict_json_loads(audit_bytes)
        validate_json_shape(
            audit_value,
            max_depth=MAX_JSON_DEPTH,
            max_nodes=max(MAX_JSON_NODES, 50_000),
        )
    except (
        JsonStructureError,
        TypeError,
        UnicodeError,
        ValueError,
    ) as error:
        raise CheckpointError("prepared audit file is invalid") from error
    if not isinstance(audit_value, dict) or audit_bytes != _stored_json_bytes(audit_value):
        raise CheckpointError("prepared audit bytes are not exact canonical storage")
    audit: dict[str, object] = audit_value
    _validate_audit_testimony(audit, transition_id)
    if (
        audit["mechanically_valid"] is not True
        or audit["base_commit"] != base_commit
        or audit["current_head"] != base_commit
        or audit["proposal_blob_sha256"] != record["proposal_blob_sha256"]
        or audit["audit_payload_sha256"] != record["audit_payload_sha256"]
        or audit["candidate_subject_sha256"] != record["candidate_subject_sha256"]
    ):
        raise CheckpointError("prepared audit binding does not match record")

    proposal_ref = str(record["proposal_ref"])
    expected = _expected_candidate_paths(
        audit,
        proposal_ref,
        audit_ref,
        audit_bytes,
    )
    if set(expected) != {item.path for item in receipts}:
        raise CheckpointError("prepared candidate receipt paths do not match audit")
    observation_paths = _observation_path_expectations(audit)
    captured: dict[str, bytes] = {}
    observed_receipts: list[CandidatePathReceipt] = []
    with AnchoredWorkspaceReader(
        service.candidate_repository.root,
        max_capture_bytes=MAX_AUDIT_CAPTURE_BYTES,
    ) as reader:
        reader.require_repository(
            service.candidate_repository.root,
            service.candidate_repository.git_dir,
            service.candidate_repository.common_dir,
        )
        for receipt in receipts:
            raw, mode = _read_anchored_candidate(reader, receipt.path)
            observed = CandidatePathReceipt(
                path=receipt.path,
                mode=mode,
                blob_oid=_blob_oid(raw),
                content_sha256=hashlib.sha256(raw).hexdigest(),
            )
            expected_item = expected[receipt.path]
            if (
                observed != receipt
                or mode != expected_item.mode
                or (
                    expected_item.blob_oid is not None
                    and observed.blob_oid != expected_item.blob_oid
                )
                or (
                    expected_item.content_sha256 is not None
                    and observed.content_sha256 != expected_item.content_sha256
                )
                or (
                    expected_item.exact_content is not None
                    and raw != expected_item.exact_content
                )
            ):
                raise CheckpointError(
                    f"prepared candidate receipt drifted: {receipt.path}"
                )
            captured[receipt.path] = raw
            observed_receipts.append(observed)
        for item in observation_paths.values():
            if item.path in captured:
                raw = captured[item.path]
                mode = expected[item.path].mode
            else:
                raw, mode = _read_anchored_candidate(reader, item.path)
                captured[item.path] = raw
            if mode != item.mode or _blob_oid(raw) != item.blob_oid:
                raise CheckpointError(
                    f"prepared observation receipt drifted: {item.path}"
                )
        reader.revalidate()
    if tuple(observed_receipts) != receipts:
        raise CheckpointError("prepared candidate receipts changed order")

    index_path = service.candidate_repository.root / str(record["index_ref"])
    index_snapshot = _snapshot_file(
        index_path,
        max_bytes=MAX_CHECKPOINT_INDEX_BYTES,
    )
    if index_snapshot.content is None or hashlib.sha256(
        index_snapshot.content
    ).hexdigest() != record["index_sha256"]:
        raise CheckpointError("prepared checkpoint index bytes or hash changed")
    _verify_index_snapshot_tree(
        service.canonical_repository,
        checkpoint_root,
        index_snapshot.content,
        str(record["candidate_tree"]),
    )
    _verify_candidate_tree(
        service.canonical_repository,
        base_commit,
        str(record["candidate_tree"]),
        receipts,
        transition_id,
    )

    user_index = _snapshot_file(
        service.candidate_repository.git_dir / "index",
        max_bytes=MAX_CHECKPOINT_INDEX_BYTES,
    )
    ordinary_entries = _ordinary_index_entries(
        service.candidate_repository,
        user_index,
        {item.path for item in receipts},
        checkpoint_root,
    )
    if ordinary_entries != record["ordinary_index_entries"]:
        raise CheckpointError("ordinary index admitted-path projection drifted")

    updates = _observation_ref_updates(
        audit,
        captured,
        candidate_repository=service.candidate_repository,
        canonical_repository=service.canonical_repository,
    )
    if _read_prepared_if_present(prepared_path) != (record, prepared_bytes):
        raise CheckpointError("prepared checkpoint bytes changed during finalization")
    _require_exact_audit_file(audit_path, audit_bytes, audit)
    _require_exact_message(
        message_path,
        message_bytes,
        str(record["message_sha256"]),
    )
    service._require_bindings()
    return _FinalizationState(
        record=record,
        prepared_bytes=prepared_bytes,
        audit=audit,
        audit_bytes=audit_bytes,
        message_bytes=message_bytes,
        index_bytes=index_snapshot.content,
        receipts=receipts,
        observation_updates=updates,
    )


def _observation_ref_updates(
    audit: dict[str, object],
    captured: Mapping[str, bytes],
    *,
    candidate_repository: RepositoryBinding,
    canonical_repository: RepositoryBinding,
) -> tuple[tuple[str, str], ...]:
    closure = audit["observation_closure"]
    if not isinstance(closure, list):
        raise CheckpointError("audit observation closure is invalid")
    updates: dict[str, str] = {}
    for item in closure:
        if not isinstance(item, dict):
            raise CheckpointError("audit observation ref testimony is invalid")
        observation_ref = item.get("observation_ref")
        kind = item.get("kind")
        record_sha256 = item.get("record_sha256")
        immutable_ref = item.get("immutable_ref")
        target_commit = item.get("target_commit")
        if (
            not isinstance(observation_ref, str)
            or not isinstance(kind, str)
            or not isinstance(record_sha256, str)
            or _SHA256.fullmatch(record_sha256) is None
            or not isinstance(immutable_ref, str)
            or not isinstance(target_commit, str)
            or _COMMIT.fullmatch(target_commit) is None
        ):
            raise CheckpointError("audit observation ref fields are invalid")
        raw = captured.get(observation_ref)
        if raw is None:
            raise CheckpointError("audit observation record bytes are unavailable")
        try:
            payload = _strict_json_loads(raw)
            validate_json_shape(
                payload,
                max_depth=MAX_JSON_DEPTH,
                max_nodes=MAX_JSON_NODES,
            )
        except (
            JsonStructureError,
            TypeError,
            UnicodeError,
            ValueError,
        ) as error:
            raise CheckpointError("audit observation record is invalid JSON") from error
        if not isinstance(payload, dict):
            raise CheckpointError("audit observation record must be an object")

        stable_id: str | None = None
        expected_kind: str
        expected_record_sha256: object
        expected_target: object
        if (matched := _TASK_OBSERVATION.fullmatch(observation_ref)) is not None:
            stable_id = matched.group(1)
            expected_kind = "task_return"
            expected_record_sha256 = payload.get("collected_sha256")
            expected_target = payload.get("return_commit")
        elif (matched := _RUN_OBSERVATION.fullmatch(observation_ref)) is not None:
            stable_id = matched.group(1)
            expected_kind = "run_final"
            expected_record_sha256 = json_sha256(payload)
            expected_target = payload.get("candidate_commit") or payload.get(
                "base_commit"
            )
        elif (matched := _EVAL_OBSERVATION.fullmatch(observation_ref)) is not None:
            stable_id = matched.group(1)
            expected_kind = (
                "measurement"
                if payload.get("measurement_state") in {"valid", "underpowered"}
                else "eval_outcome"
            )
            expected_record_sha256 = payload.get("receipt_sha256")
            expected_target = payload.get("candidate_commit")
        else:
            raise CheckpointError("audit observation reference is unsupported")
        expected_ref = (
            f"refs/aros/observations/{expected_kind}/{stable_id}/"
            f"{record_sha256}"
        )
        if (
            kind != expected_kind
            or record_sha256 != expected_record_sha256
            or target_commit != expected_target
            or immutable_ref != expected_ref
            or run_git(
                canonical_repository,
                "check-ref-format",
                immutable_ref,
            ).returncode
            != 0
        ):
            raise CheckpointError("audit observation ref testimony does not match record")
        _require_exact_commit(candidate_repository, target_commit)
        _require_exact_commit(canonical_repository, target_commit)
        prior = updates.get(immutable_ref)
        if prior is not None and prior != target_commit:
            raise CheckpointError("audit observation ref targets conflict")
        updates[immutable_ref] = target_commit
    return tuple(sorted(updates.items()))


def _message_bytes(message: object) -> bytes:
    if not isinstance(message, str):
        raise CheckpointError("checkpoint message must be a string")
    try:
        encoded = message.encode("utf-8")
    except UnicodeError as error:
        raise CheckpointError("checkpoint message must be valid UTF-8") from error
    if len(encoded) > MAX_MESSAGE_BYTES:
        raise CheckpointError("checkpoint message exceeds 1048576 bytes")
    return encoded


def _epoch_milliseconds() -> int:
    return time.time_ns() // 1_000_000


def _decode_admission_receipt(raw: bytes) -> dict[str, object]:
    receipt = _decode_canonical_record(
        raw,
        fields=_ADMISSION_RECEIPT_FIELDS,
        hash_field="receiptSHA256",
        max_bytes=MAX_ADMISSION_RECEIPT_BYTES,
        description="admission receipt",
    )
    if type(receipt["schemaVersion"]) is not int or receipt["schemaVersion"] != 1:
        raise CheckpointError("admission receipt schemaVersion must be integer 1")
    if receipt["decision"] != "allow":
        raise CheckpointError("admission receipt decision must be allow")
    if receipt["capability"] != "checkpoint":
        raise CheckpointError("admission receipt capability must be checkpoint")
    for field in (
        "candidateSubjectSHA256",
        "auditPayloadSHA256",
        "specHash",
        "researchContractBindingSHA256",
        "trustedExecutionClosureSHA256",
        "receiptSHA256",
    ):
        _required_sha256(receipt[field], f"admission receipt {field}")
    for field in (
        "contractID",
        "workspaceID",
        "canonicalRef",
        "sessionID",
        "promptID",
        "attemptKey",
        "leaseOwner",
        "auditImplementationID",
        "authorityDomainID",
    ):
        _bounded_receipt_text(receipt[field], f"admission receipt {field}")
    if not str(receipt["canonicalRef"]).startswith("refs/"):
        raise CheckpointError("admission receipt canonicalRef must be a full Git ref")
    if receipt["enforcementClass"] not in {"cooperative", "mediated"}:
        raise CheckpointError("admission receipt enforcementClass is invalid")
    revision = _nonnegative_integer(
        receipt["revision"],
        "admission receipt revision",
    )
    attempt = _nonnegative_integer(
        receipt["attempt"],
        "admission receipt attempt",
    )
    issued_at = _nonnegative_integer(
        receipt["issuedAt"],
        "admission receipt issuedAt timestamp",
    )
    lease_expires_at = _nonnegative_integer(
        receipt["leaseExpiresAt"],
        "admission receipt leaseExpiresAt timestamp",
    )
    if revision < 1 or attempt < 1:
        raise CheckpointError("admission receipt revision and attempt must be positive")
    if issued_at >= lease_expires_at:
        raise CheckpointError("admission receipt lease timestamp is invalid")

    _validate_budget_snapshot(receipt["budgetBefore"], "budgetBefore")
    _validate_budget_snapshot(
        receipt["budgetRemaining"],
        "budgetRemaining",
    )
    charge = receipt["charge"]
    if not isinstance(charge, dict) or set(charge) != {"actions"}:
        raise CheckpointError("admission receipt charge has invalid fields")
    _nonnegative_integer(
        charge["actions"],
        "admission receipt charge actions",
    )

    policies = receipt["evaluatorPolicyRefs"]
    if not isinstance(policies, list) or len(policies) > 256:
        raise CheckpointError("admission receipt evaluatorPolicyRefs is invalid")
    for policy in policies:
        _bounded_receipt_text(
            policy,
            "admission receipt evaluatorPolicyRef",
            max_bytes=1_024,
        )
    return receipt


def _decode_finalize_fence(
    raw: bytes,
    *,
    receipt: Mapping[str, object],
    now_ms: int,
) -> dict[str, object]:
    fence = _decode_canonical_record(
        raw,
        fields=_FINALIZE_FENCE_FIELDS,
        hash_field="fenceSHA256",
        max_bytes=MAX_FINALIZE_FENCE_BYTES,
        description="finalize fence",
    )
    if type(fence["schemaVersion"]) is not int or fence["schemaVersion"] != 1:
        raise CheckpointError("finalize fence schemaVersion must be integer 1")
    for field in (
        "receiptSHA256",
        "researchContractBindingSHA256",
        "fenceSHA256",
    ):
        _required_sha256(fence[field], f"finalize fence {field}")
    for field in (
        "reservationID",
        "sessionID",
        "promptID",
        "attemptKey",
        "leaseOwner",
    ):
        _bounded_receipt_text(fence[field], f"finalize fence {field}")
    for field in (
        "revision",
        "attempt",
        "leaseExpiresAt",
        "issuedAt",
        "expiresAt",
    ):
        _nonnegative_integer(fence[field], f"finalize fence {field}")
    current = _nonnegative_integer(now_ms, "finalize fence current time")
    if not isinstance(receipt, Mapping) or set(receipt) != _ADMISSION_RECEIPT_FIELDS:
        raise CheckpointError("finalize fence receipt binding is invalid")
    if fence["receiptSHA256"] != receipt["receiptSHA256"]:
        raise CheckpointError("finalize fence receiptSHA256 does not match receipt")
    for field in _FENCE_RECEIPT_BINDINGS:
        if fence[field] != receipt[field]:
            raise CheckpointError(
                f"finalize fence {field} does not match admission receipt"
            )
    issued_at = int(fence["issuedAt"])
    expires_at = int(fence["expiresAt"])
    lease_expires_at = int(fence["leaseExpiresAt"])
    if issued_at > expires_at or not issued_at <= current <= expires_at:
        raise CheckpointError("finalize fence is outside its valid time window")
    if current >= lease_expires_at:
        raise CheckpointError("finalize fence admission lease has expired")
    return fence


def _require_receipt_binding(
    receipt: Mapping[str, object],
    prepared: Mapping[str, object],
    canonical_ref: str,
) -> None:
    if receipt["candidateSubjectSHA256"] != prepared["candidate_subject_sha256"]:
        raise CheckpointError("admission receipt candidate subject does not match")
    if receipt["auditPayloadSHA256"] != prepared["audit_payload_sha256"]:
        raise CheckpointError("admission receipt audit payload does not match")
    if receipt["canonicalRef"] != canonical_ref:
        raise CheckpointError("admission receipt canonical ref does not match service")


def _decode_canonical_record(
    raw: bytes,
    *,
    fields: set[str],
    hash_field: str,
    max_bytes: int,
    description: str,
) -> dict[str, object]:
    if not isinstance(raw, bytes) or not raw or len(raw) > max_bytes:
        raise CheckpointError(f"{description} bytes exceed the bound")
    try:
        value = _strict_json_loads(raw)
        validate_json_shape(value, max_depth=8, max_nodes=2_048)
    except (
        JsonStructureError,
        TypeError,
        UnicodeError,
        ValueError,
    ) as error:
        raise CheckpointError(f"{description} is not strict UTF-8 JSON: {error}") from error
    if not isinstance(value, dict) or set(value) != fields:
        raise CheckpointError(f"{description} has invalid fields")
    try:
        canonical = canonical_json_bytes(value)
    except (TypeError, UnicodeError, ValueError) as error:
        raise CheckpointError(f"{description} is not canonical JSON") from error
    if canonical != raw:
        raise CheckpointError(f"{description} bytes are not exact canonical JSON")
    observed_hash = value[hash_field]
    if not isinstance(observed_hash, str) or _SHA256.fullmatch(observed_hash) is None:
        raise CheckpointError(f"{description} {hash_field} is invalid")
    payload = {key: item for key, item in value.items() if key != hash_field}
    expected_hash = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    if observed_hash != expected_hash:
        raise CheckpointError(f"{description} self-hash does not match")
    return value


def _validate_budget_snapshot(
    value: object,
    field: str,
) -> None:
    if not isinstance(value, dict) or set(value) != _BUDGET_SNAPSHOT_FIELDS:
        raise CheckpointError(f"admission receipt {field} has invalid budget fields")
    _nonnegative_integer(
        value["deadline"],
        f"admission receipt {field} deadline",
    )
    for name in ("turns", "actions"):
        counter = value[name]
        if not isinstance(counter, dict) or set(counter) != _BUDGET_COUNTER_FIELDS:
            raise CheckpointError(
                f"admission receipt {field}.{name} has invalid budget fields"
            )
        for key in ("limit", "used", "remaining"):
            _nonnegative_integer(
                counter[key],
                f"admission receipt {field}.{name}.{key}",
            )


def _nonnegative_integer(value: object, description: str) -> int:
    if type(value) is not int or value < 0:
        raise CheckpointError(f"{description} must be a nonnegative integer timestamp or count")
    return value


def _bounded_receipt_text(
    value: object,
    description: str,
    *,
    max_bytes: int = 4_096,
) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise CheckpointError(f"{description} must be a non-empty string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError as error:
        raise CheckpointError(f"{description} must be valid UTF-8") from error
    if len(encoded) > max_bytes:
        raise CheckpointError(f"{description} exceeds its byte bound")
    return value


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
        read_tree_into_index(
            repository,
            base_commit,
            index_file=index_path,
        ),
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
        update_index_cacheinfo(
            repository,
            index_file=index_path,
            mode=receipt.mode,
            oid=receipt.blob_oid,
            path=receipt.path,
        ),
        f"stage checkpoint blob: {receipt.path}",
    )


def _write_tree(repository: RepositoryBinding, index_path: Path) -> str:
    result = _git_success(
        write_index_tree(repository, index_file=index_path),
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


def _verify_index_snapshot_tree(
    repository: RepositoryBinding,
    runtime: Path,
    index_bytes: bytes,
    candidate_tree: str,
) -> None:
    verification_index = runtime / "index-verification"
    _replace_runtime_file(verification_index, index_bytes)
    try:
        result = _git_success(
            run_git(
                repository,
                "--no-optional-locks",
                "write-tree",
                index_file=verification_index,
            ),
            "verify snapshotted checkpoint index",
        )
        try:
            observed_tree = result.stdout.decode("ascii").strip()
        except UnicodeError as error:
            raise CheckpointError(
                "snapshotted checkpoint index returned non-ASCII tree output"
            ) from error
        if observed_tree != candidate_tree:
            raise CheckpointError(
                "snapshotted checkpoint index does not reproduce candidate tree"
            )
    finally:
        try:
            verification_index.unlink()
        except FileNotFoundError:
            pass
        _fsync_directory(runtime)


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
    base_entries = {
        entry.path: entry
        for entry in read_repository_tree_entries(
            repository,
            base_commit,
            expected,
        )
    }
    expected_changed = {
        receipt.path
        for receipt in receipts
        if (
            (base_entry := base_entries.get(receipt.path)) is None
            or base_entry.kind != "blob"
            or base_entry.mode != receipt.mode
            or base_entry.oid != receipt.blob_oid
        )
    }
    if changed != expected_changed:
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


def _verify_final_tree(
    repository: RepositoryBinding,
    candidate_tree: str,
    final_tree: str,
    admission: CandidatePathReceipt,
) -> None:
    changed = _decode_paths(
        _git_success(
            run_git(
                repository,
                "diff-tree",
                "--no-commit-id",
                "--name-only",
                "-r",
                "-z",
                "--no-renames",
                candidate_tree,
                final_tree,
                "--",
            ),
            "verify final checkpoint tree delta",
        ).stdout,
        "final checkpoint tree delta",
    )
    if changed != {admission.path}:
        raise CheckpointError("final tree must equal candidate tree plus admission.json")
    entries = read_repository_tree_entries(
        repository,
        final_tree,
        (admission.path,),
    )
    if len(entries) != 1 or (
        entries[0].path != admission.path
        or entries[0].kind != "blob"
        or entries[0].mode != admission.mode
        or entries[0].oid != admission.blob_oid
    ):
        raise CheckpointError("final tree admission receipt does not match")


def _commit_final_tree(
    repository: RepositoryBinding,
    final_tree: str,
    base_commit: str,
    message_bytes: bytes,
) -> str:
    result = _git_success(
        run_git(
            repository,
            "commit-tree",
            final_tree,
            "-p",
            base_commit,
            "-F",
            "-",
            input_bytes=message_bytes,
        ),
        "create final checkpoint commit",
    )
    try:
        commit = result.stdout.decode("ascii").strip()
    except UnicodeError as error:
        raise CheckpointError("Git commit-tree returned non-ASCII output") from error
    _required_commit(commit, "final checkpoint commit")
    _verify_final_commit(
        repository,
        commit,
        final_tree,
        base_commit,
        message_bytes,
    )
    return commit


def _verify_final_commit(
    repository: RepositoryBinding,
    commit: str,
    final_tree: str,
    base_commit: str,
    message_bytes: bytes,
) -> None:
    _require_exact_commit(repository, commit)
    raw = _git_success(
        run_git(repository, "cat-file", "commit", commit),
        "read final checkpoint commit",
    ).stdout
    headers, separator, message = raw.partition(b"\n\n")
    if not separator or message != message_bytes:
        raise CheckpointError("final checkpoint commit message is not exact")
    lines = headers.split(b"\n")
    trees = [line.removeprefix(b"tree ") for line in lines if line.startswith(b"tree ")]
    parents = [
        line.removeprefix(b"parent ") for line in lines if line.startswith(b"parent ")
    ]
    if trees != [final_tree.encode("ascii")] or parents != [base_commit.encode("ascii")]:
        raise CheckpointError("final checkpoint commit tree or sole parent is invalid")


def _atomic_ref_transaction(
    repository: RepositoryBinding,
    *,
    canonical_ref: str,
    base_commit: str,
    new_commit: str,
    observation_updates: tuple[tuple[str, str], ...],
    validate_current_fence: Callable[[], None],
) -> None:
    if resolve_repository_commit(repository, canonical_ref) != base_commit:
        raise CheckpointError("canonical ref lost checkpoint CAS before transaction")
    create: list[tuple[str, str]] = []
    verify: list[tuple[str, str]] = []
    for ref, target in observation_updates:
        exists = run_git(repository, "show-ref", "--verify", "--quiet", ref)
        if exists.returncode == 0:
            result = _git_success(
                run_git(repository, "show-ref", "--hash", "--verify", ref),
                f"read immutable observation ref: {ref}",
            )
            try:
                observed = result.stdout.decode("ascii").strip()
            except UnicodeError as error:
                raise CheckpointError(
                    "immutable observation ref returned non-ASCII output"
                ) from error
            if observed != target:
                raise CheckpointError(
                    f"immutable observation ref conflicts with target: {ref}"
                )
            verify.append((ref, target))
        elif exists.returncode == 1:
            create.append((ref, target))
        else:
            _git_success(exists, f"inspect immutable observation ref: {ref}")
    commands = [
        "start",
        f"update {canonical_ref} {new_commit} {base_commit}",
        *(f"create {ref} {target}" for ref, target in create),
        *(f"verify {ref} {target}" for ref, target in verify),
        "prepare",
        "commit",
    ]
    transaction = ("\n".join(commands) + "\n").encode("ascii")
    _git_success(
        run_checkpoint_ref_transaction(
            repository,
            input_bytes=transaction,
            validate_fence=validate_current_fence,
        ),
        "atomic checkpoint CAS ref transaction",
    )
    if resolve_repository_commit(repository, canonical_ref) != new_commit:
        raise CheckpointError("canonical ref differs after checkpoint CAS")
    for ref, target in observation_updates:
        result = _git_success(
            run_git(repository, "show-ref", "--hash", "--verify", ref),
            f"verify immutable observation ref: {ref}",
        )
        if result.stdout.strip() != target.encode("ascii"):
            raise CheckpointError(
                f"immutable observation ref differs after transaction: {ref}"
            )


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
    refs_before = _snapshot_canonical_refs(canonical, "snapshot canonical refs")
    fetch_paths = {canonical.git_dir / "FETCH_HEAD", canonical.common_dir / "FETCH_HEAD"}
    fetch_before = {
        path: _snapshot_file(path, max_bytes=MAX_FETCH_HEAD_BYTES)
        for path in fetch_paths
    }
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
    refs_after = _snapshot_canonical_refs(canonical, "verify canonical refs")
    if refs_after != refs_before or any(
        _snapshot_file(path, max_bytes=MAX_FETCH_HEAD_BYTES) != snapshot
        for path, snapshot in fetch_before.items()
    ):
        raise CheckpointError("audited object import changed a ref or FETCH_HEAD")


def _snapshot_canonical_refs(
    repository: RepositoryBinding,
    description: str,
) -> bytes:
    try:
        snapshot = read_repository_refs_snapshot(
            repository,
            max_refs=MAX_CANONICAL_REFS,
            max_bytes=MAX_CANONICAL_REF_SNAPSHOT_BYTES,
        )
    except WorktreeLimitError as error:
        raise CheckpointError(
            "canonical ref snapshot exceeds checkpoint bound"
        ) from error
    except WorktreeError as error:
        raise CheckpointError(f"{description}: {error}") from error
    count = snapshot.count(b"\n")
    if snapshot and not snapshot.endswith(b"\n"):
        count += 1
    if (
        len(snapshot) > MAX_CANONICAL_REF_SNAPSHOT_BYTES
        or count > MAX_CANONICAL_REFS
    ):
        raise CheckpointError("canonical ref snapshot exceeds checkpoint bound")
    return snapshot


def _require_exact_commit(repository: RepositoryBinding, oid: str) -> None:
    result = _git_success(
        run_git(repository, "rev-parse", "--verify", f"{oid}^{{commit}}"),
        f"validate audited commit: {oid}",
    )
    if result.stdout.strip() != oid.encode("ascii"):
        raise CheckpointError(f"audited commit did not resolve exactly: {oid}")


def _ordinary_index_entries(
    repository: RepositoryBinding,
    user_index: _FileSnapshot,
    paths: set[str],
    runtime: Path,
) -> list[dict[str, str]]:
    if not user_index.exists:
        return []
    assert user_index.content is not None
    snapshot_path = runtime / "user-index.projection"
    _replace_runtime_file(snapshot_path, user_index.content)
    try:
        result = _git_success(
            run_git(
                repository,
                "ls-files",
                "--stage",
                "-z",
                "--",
                *sorted(paths),
                index_file=snapshot_path,
            ),
            "inspect ordinary index admitted entries",
        )
        if len(result.stdout) > MAX_CHECKPOINT_INDEX_BYTES:
            raise CheckpointError("ordinary index projection exceeds bound")
        entries: list[dict[str, str]] = []
        seen: set[str] = set()
        for raw in (item for item in result.stdout.split(b"\0") if item):
            metadata, separator, raw_path = raw.partition(b"\t")
            fields = metadata.split(b" ")
            if not separator or len(fields) != 3 or fields[2] != b"0":
                raise CheckpointError("ordinary index projection is unmerged or invalid")
            try:
                mode = fields[0].decode("ascii")
                oid = fields[1].decode("ascii")
                path = raw_path.decode("utf-8")
            except UnicodeError as error:
                raise CheckpointError(
                    "ordinary index projection has invalid encoding"
                ) from error
            path = _candidate_path(path)
            if (
                path not in paths
                or path in seen
                or not re.fullmatch(r"[0-7]{6}", mode)
                or _COMMIT.fullmatch(oid) is None
            ):
                raise CheckpointError("ordinary index projection is invalid")
            seen.add(path)
            entries.append({"path": path, "mode": mode, "blob_oid": oid})
        return sorted(entries, key=lambda item: item["path"])
    finally:
        try:
            snapshot_path.unlink()
        except FileNotFoundError:
            pass
        _fsync_directory(runtime)


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


def _read_message_if_present(path: Path) -> bytes | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        _remove_message_temp_aliases(path, identity=None)
        return None
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size > MAX_MESSAGE_BYTES
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise CheckpointError(
            "checkpoint message must be a bounded create-once 0600 plain file"
        )
    identity = (metadata.st_dev, metadata.st_ino)
    if metadata.st_nlink > 1:
        _remove_message_temp_aliases(path, identity=identity)
        metadata = path.lstat()
    if (
        metadata.st_nlink != 1
        or (metadata.st_dev, metadata.st_ino) != identity
    ):
        raise CheckpointError(
            "checkpoint message must be a create-once single-link plain file"
        )
    return _read_plain_file(path, max_bytes=MAX_MESSAGE_BYTES)


def _create_once_message(path: Path, content: bytes) -> None:
    existing = _read_message_if_present(path)
    if existing is not None:
        _require_exact_message(
            path,
            content,
            hashlib.sha256(content).hexdigest(),
        )
        return
    descriptor, temporary = tempfile.mkstemp(
        prefix=_message_temp_prefix(path),
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path = Path(temporary)
    try:
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, content)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        temporary_metadata = temporary_path.lstat()
        if (
            not stat.S_ISREG(temporary_metadata.st_mode)
            or temporary_metadata.st_nlink != 1
            or stat.S_IMODE(temporary_metadata.st_mode) != 0o600
            or temporary_metadata.st_size > MAX_MESSAGE_BYTES
            or _read_plain_file(
                temporary_path,
                max_bytes=MAX_MESSAGE_BYTES,
            )
            != content
        ):
            raise CheckpointError("checkpoint message temporary file is invalid")
        try:
            os.link(temporary_path, path, follow_symlinks=False)
        except FileExistsError:
            pass
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary_path.unlink(missing_ok=True)
        finally:
            _fsync_directory(path.parent)
    _require_exact_message(
        path,
        content,
        hashlib.sha256(content).hexdigest(),
    )


def _message_temp_prefix(path: Path) -> str:
    digest = hashlib.sha256(os.fsencode(path.name)).hexdigest()
    return f".aros-message-{digest}."


def _remove_message_temp_aliases(
    path: Path,
    *,
    identity: tuple[int, int] | None,
) -> None:
    prefix = _message_temp_prefix(path)
    removed = False
    for candidate in path.parent.iterdir():
        if (
            not candidate.name.startswith(prefix)
            or not candidate.name.endswith(".tmp")
        ):
            continue
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(metadata.st_mode) or (
            identity is not None
            and (metadata.st_dev, metadata.st_ino) != identity
        ):
            continue
        candidate.unlink(missing_ok=True)
        removed = True
    if removed:
        _fsync_directory(path.parent)


def _require_exact_message(
    path: Path,
    content: bytes,
    message_sha256: str,
) -> None:
    observed = _read_message_if_present(path)
    if (
        observed is None
        or observed != content
        or hashlib.sha256(observed).hexdigest() != message_sha256
    ):
        raise CheckpointError("checkpoint message bytes or hash do not match")


def _read_prepared_if_present(
    path: Path,
) -> tuple[dict[str, object], bytes] | None:
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
        raw = _read_plain_file(path, max_bytes=MAX_PREPARED_BYTES)
        value = _strict_json_loads(raw)
        validate_json_shape(
            value,
            max_depth=MAX_JSON_DEPTH,
            max_nodes=max(MAX_JSON_NODES, 20_000),
        )
    except (JsonStructureError, OSError, TypeError, UnicodeError, ValueError) as error:
        raise CheckpointError(f"prepared checkpoint record is invalid: {error}") from error
    if not isinstance(value, dict) or set(value) != _PREPARED_FIELDS:
        raise CheckpointError("prepared checkpoint record has an invalid shape")
    return value, raw


def _create_once_audit(
    path: Path,
    content: bytes,
    audit: dict[str, object],
) -> None:
    created = create_json(path, audit)
    try:
        _require_exact_audit_file(path, content, audit)
    except CheckpointError as error:
        conflict = "publication" if created else "existing"
        raise CheckpointError(
            f"{conflict} audit file conflicts byte-for-byte: {error}"
        ) from error


def _require_exact_audit_file(
    path: Path,
    content: bytes,
    audit: dict[str, object],
) -> None:
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
        raise CheckpointError("audit file conflicts byte-for-byte")
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


def _snapshot_file(path: Path, *, max_bytes: int) -> _FileSnapshot:
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
    content = _read_plain_file(path, max_bytes=max_bytes)
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
    "AdmissionGateway",
    "CandidatePathReceipt",
    "CheckpointError",
    "CheckpointService",
    "PreparedCheckpoint",
]
