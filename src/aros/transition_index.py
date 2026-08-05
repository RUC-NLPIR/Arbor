"""Bounded, Git-derived assimilation state for Research Attention."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from . import worktrees as _worktrees
from .checkpoint import (
    MAX_ADMISSION_RECEIPT_BYTES,
    MAX_AUDIT_FILE_BYTES,
    _admission_receipt_bindings,
    _decode_admission_receipt,
    _expected_candidate_paths,
    _stored_json_bytes,
    _validate_audit_testimony,
)
from .store import (
    AnchoredWorkspaceReader,
    JsonStructureError,
    _strict_json_loads,
    validate_json_shape,
)
from .transitions import (
    MAX_JSON_DEPTH,
    MAX_JSON_NODES,
    MAX_PROPOSAL_BYTES,
    MAX_VERSIONED_FILE_BYTES,
    TransitionProposal,
    _canonical_path,
    _parse_proposal,
)
from .worktrees import (
    RepositoryBinding,
    RepositoryTreeEntry,
    bind_repository,
    read_repository_blob,
    read_repository_snapshot,
    read_repository_tree_entries,
    run_git,
)


_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ADMISSION_PATH = re.compile(
    r"^transitions/(T-[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)/"
    r"admission\.json$"
)
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
_CACHE_FIELDS = {
    "schema_version",
    "head",
    "validated_through",
    "assimilations",
    "latest_evidence_transition",
}
_EVIDENCE_LINK_FIELDS = {
    "link_id",
    "observation_ref",
    "path",
    "anchor",
    "ordinal",
    "relation",
    "scope",
    "canonical_sha256",
}

TRANSITION_INDEX_CACHE = Path(".aros/indexes/transition-index.json")
MAX_NORMAL_TRANSITIONS = 256
MAX_NORMAL_HISTORY_COMMITS = 4_096
MAX_REBUILD_HISTORY_COMMITS = 20_000
MAX_CACHE_BYTES = 8_388_608
MAX_CACHE_NODES = 100_000
MAX_REBUILD_TRANSITIONS = MAX_REBUILD_HISTORY_COMMITS
MAX_REBUILD_ASSIMILATIONS = min(
    MAX_REBUILD_HISTORY_COMMITS,
    MAX_CACHE_NODES // 8,
)
MAX_ADMISSION_LIST_BYTES = 524_288
MAX_TRANSITION_CAPTURE_BYTES = 67_108_864
MAX_COMMIT_BYTES = 1_048_576
_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


class TransitionIndexError(ValueError):
    """Transition history cannot be proven within the configured bounds."""


class _IndexIncomplete(TransitionIndexError):
    pass


@dataclass(frozen=True)
class AssimilationRecord:
    observation_ref: str
    transition_id: str
    commit: str
    affected_paths: tuple[str, ...]
    rationale: str
    record_sha256: str


@dataclass(frozen=True)
class EvidenceTransitionRecord:
    transition_id: str
    commit: str
    assimilations: tuple[AssimilationRecord, ...]
    evidence_links: tuple[Mapping[str, object], ...]


@dataclass(frozen=True)
class TransitionIndexState:
    state: str
    head: str | None
    validated_through: str | None
    assimilations: Mapping[str, tuple[AssimilationRecord, ...]]
    latest_evidence_transition: EvidenceTransitionRecord | None


@dataclass(frozen=True)
class _ValidatedTransition:
    transition_id: str
    commit: str
    assimilations: tuple[AssimilationRecord, ...]
    evidence_links: tuple[Mapping[str, object], ...]


@dataclass(frozen=True)
class _AdmissionCandidate:
    entry: RepositoryTreeEntry
    commit: str


class _CaptureBudget:
    def __init__(self, limit: int):
        self.limit = limit
        self.used = 0

    def reserve(self, size: int) -> None:
        if type(size) is not int or size < 0 or self.used + size > self.limit:
            raise _IndexIncomplete("transition capture exceeds its byte bound")
        self.used += size


class TransitionIndex:
    """Read or explicitly rebuild a disposable cache from canonical Git."""

    def __init__(
        self,
        candidate_repository: RepositoryBinding,
        canonical_repository: RepositoryBinding,
    ):
        if not isinstance(candidate_repository, RepositoryBinding) or not isinstance(
            canonical_repository, RepositoryBinding
        ):
            raise TransitionIndexError("transition index requires repository bindings")
        if bind_repository(candidate_repository.root) != candidate_repository:
            raise TransitionIndexError("candidate repository binding is stale")
        if bind_repository(canonical_repository.root) != canonical_repository:
            raise TransitionIndexError("canonical repository binding is stale")
        self.candidate_repository = candidate_repository
        self.canonical_repository = canonical_repository
        self.cache_path = candidate_repository.root / TRANSITION_INDEX_CACHE

    def read(self) -> TransitionIndexState:
        """Validate one bounded cache without writing or repairing it."""
        head: str | None = None
        try:
            head, canonical_ref = _canonical_head_ref(self.canonical_repository)
            cache = _read_cache(self.candidate_repository.root)
            _validate_cache_envelope(cache, expected_head=head)
            derived = self._derive(
                head=head,
                canonical_ref=canonical_ref,
                history_limit=MAX_NORMAL_HISTORY_COMMITS,
                transition_limit=MAX_NORMAL_TRANSITIONS,
                assimilation_limit=MAX_NORMAL_TRANSITIONS,
            )
            if _encode_state(derived) != cache:
                raise _IndexIncomplete("transition index cache differs from Git")
            self._require_unchanged(head, canonical_ref)
            return derived
        except Exception:
            return _incomplete_state(head)

    def rebuild(self) -> TransitionIndexState:
        """Perform the only full-history scan and atomically replace the cache."""
        head: str | None = None
        try:
            head, canonical_ref = _canonical_head_ref(self.canonical_repository)
            state = self._derive(
                head=head,
                canonical_ref=canonical_ref,
                history_limit=MAX_REBUILD_HISTORY_COMMITS,
                transition_limit=MAX_REBUILD_TRANSITIONS,
                assimilation_limit=MAX_REBUILD_ASSIMILATIONS,
            )
            encoded = _encode_state(state)
            validate_json_shape(
                encoded,
                max_depth=MAX_JSON_DEPTH,
                max_nodes=MAX_CACHE_NODES,
            )
            payload = (
                json.dumps(encoded, ensure_ascii=False, sort_keys=True, indent=2)
                + "\n"
            ).encode("utf-8")
            if len(payload) > MAX_CACHE_BYTES:
                raise _IndexIncomplete(
                    "transition index cache exceeds its byte bound"
                )
            self._require_unchanged(head, canonical_ref)
            _require_safe_cache_parent(self.candidate_repository.root)
            _publish_cache(self.candidate_repository.root, payload)
            self._require_unchanged(head, canonical_ref)
            return state
        except Exception:
            return _incomplete_state(head)

    def _derive(
        self,
        *,
        head: str,
        canonical_ref: str,
        history_limit: int,
        transition_limit: int,
        assimilation_limit: int,
    ) -> TransitionIndexState:
        snapshot = read_repository_snapshot(self.canonical_repository)
        if snapshot.get("head") != head or snapshot.get("ref") != canonical_ref:
            raise _IndexIncomplete("canonical HEAD/ref is unavailable")
        commits, excluded_boundary = _first_parent_window(
            self.canonical_repository,
            head,
            limit=history_limit,
        )
        if excluded_boundary is not None:
            raise _IndexIncomplete("repository first-parent history exceeds its bound")
        positions = {commit: offset for offset, commit in enumerate(commits)}
        admissions = _historical_admissions(
            self.canonical_repository,
            head,
            commits,
            transition_limit=transition_limit,
        )
        budget = _CaptureBudget(MAX_TRANSITION_CAPTURE_BYTES)
        transitions: list[_ValidatedTransition] = []
        for admission in admissions:
            transition_id = _transition_from_admission_path(admission.entry.path)
            commit = admission.commit
            if commit not in positions:
                raise _IndexIncomplete("admission is outside the bounded first-parent window")
            transitions.append(
                _validate_admitted_transition(
                    self.canonical_repository,
                    head=head,
                    canonical_ref=canonical_ref,
                    transition_id=transition_id,
                    commit=commit,
                    head_admission=admission.entry,
                    budget=budget,
                )
            )
        transitions.sort(key=lambda item: positions[item.commit], reverse=True)
        total_records = sum(len(item.assimilations) for item in transitions)
        if total_records > assimilation_limit:
            raise _IndexIncomplete(
                "transition index exceeds its assimilation-entry bound"
            )
        assimilations: dict[str, list[AssimilationRecord]] = {}
        latest: EvidenceTransitionRecord | None = None
        for transition in transitions:
            for record in transition.assimilations:
                assimilations.setdefault(record.observation_ref, []).append(record)
            if transition.evidence_links:
                latest = EvidenceTransitionRecord(
                    transition_id=transition.transition_id,
                    commit=transition.commit,
                    assimilations=transition.assimilations,
                    evidence_links=transition.evidence_links,
                )
        return TransitionIndexState(
            state="complete",
            head=head,
            validated_through=head,
            assimilations=MappingProxyType(
                {
                    observation_ref: tuple(records)
                    for observation_ref, records in sorted(assimilations.items())
                }
            ),
            latest_evidence_transition=latest,
        )

    def _require_unchanged(self, expected_head: str, expected_ref: str) -> None:
        if (
            _canonical_head_ref(self.canonical_repository)
            != (expected_head, expected_ref)
            or bind_repository(self.candidate_repository.root)
            != self.candidate_repository
            or bind_repository(self.canonical_repository.root)
            != self.canonical_repository
        ):
            raise _IndexIncomplete("repository binding or canonical HEAD changed")


def _canonical_head_ref(repository: RepositoryBinding) -> tuple[str, str]:
    snapshot = read_repository_snapshot(repository)
    head = snapshot.get("head")
    canonical_ref = snapshot.get("ref")
    if (
        not isinstance(head, str)
        or _COMMIT.fullmatch(head) is None
        or not isinstance(canonical_ref, str)
    ):
        raise TransitionIndexError("canonical HEAD/ref is unavailable")
    return head, canonical_ref


def _incomplete_state(head: str | None) -> TransitionIndexState:
    return TransitionIndexState(
        state="index_incomplete",
        head=head,
        validated_through=None,
        assimilations=MappingProxyType({}),
        latest_evidence_transition=None,
    )


def _read_cache(root: Path) -> dict[str, object]:
    with AnchoredWorkspaceReader(
        root,
        max_json_bytes=MAX_CACHE_BYTES,
        max_json_depth=MAX_JSON_DEPTH,
        max_json_nodes=MAX_CACHE_NODES,
        max_capture_bytes=MAX_CACHE_BYTES,
    ) as reader:
        value = reader.read_json(TRANSITION_INDEX_CACHE)
    if not isinstance(value, dict):
        raise _IndexIncomplete("transition index cache must be an object")
    return value


def _validate_cache_envelope(
    value: dict[str, object], *, expected_head: str
) -> None:
    if set(value) != _CACHE_FIELDS or type(value.get("schema_version")) is not int:
        raise _IndexIncomplete("transition index cache has invalid fields")
    if (
        value["schema_version"] != 1
        or value["head"] != expected_head
        or value["validated_through"] != expected_head
    ):
        raise _IndexIncomplete("transition index cache HEAD binding is stale")
    raw_assimilations = value["assimilations"]
    if not isinstance(raw_assimilations, dict) or len(raw_assimilations) > 256:
        raise _IndexIncomplete("transition index assimilations are invalid")
    count = 0
    for raw_records in raw_assimilations.values():
        if not isinstance(raw_records, list) or not raw_records:
            raise _IndexIncomplete("transition index record list is invalid")
        count += len(raw_records)
        if count > MAX_NORMAL_TRANSITIONS:
            raise _IndexIncomplete("transition index exceeds 256 assimilation entries")


def _encode_state(state: TransitionIndexState) -> dict[str, object]:
    if state.state != "complete" or state.head is None:
        raise _IndexIncomplete("only complete transition state can be cached")
    return {
        "schema_version": 1,
        "head": state.head,
        "validated_through": state.validated_through,
        "assimilations": {
            observation_ref: [_encode_assimilation_record(item) for item in records]
            for observation_ref, records in sorted(state.assimilations.items())
        },
        "latest_evidence_transition": _encode_evidence_transition(
            state.latest_evidence_transition
        ),
    }


def transition_index_state_json(state: TransitionIndexState) -> dict[str, object]:
    """Return the explicit JSON-safe projection of one index operation."""
    if not isinstance(state, TransitionIndexState):
        raise TypeError("state must be a TransitionIndexState")
    return {
        "state": state.state,
        "head": state.head,
        "validated_through": state.validated_through,
        "assimilations": {
            observation_ref: [_encode_assimilation_record(item) for item in records]
            for observation_ref, records in sorted(state.assimilations.items())
        },
        "latest_evidence_transition": _encode_evidence_transition(
            state.latest_evidence_transition
        ),
    }


def _encode_assimilation_record(value: AssimilationRecord) -> dict[str, object]:
    return {
        "observation_ref": value.observation_ref,
        "transition_id": value.transition_id,
        "commit": value.commit,
        "affected_paths": list(value.affected_paths),
        "rationale": value.rationale,
        "record_sha256": value.record_sha256,
    }


def _encode_evidence_transition(
    value: EvidenceTransitionRecord | None,
) -> dict[str, object] | None:
    if value is None:
        return None
    return {
        "transition_id": value.transition_id,
        "commit": value.commit,
        "assimilations": [
            _encode_assimilation_record(item) for item in value.assimilations
        ],
        "evidence_links": [_thaw_json(item) for item in value.evidence_links],
    }


def _admission_paths_between(
    repository: RepositoryBinding,
    before: str,
    after: str,
    *,
    max_entries: int,
) -> tuple[str, ...]:
    raw = _bounded_git_capture(
        repository,
        (
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "-r",
            "-z",
            before,
            after,
            "--",
            ":(glob)transitions/T-*/admission.json",
        ),
        max_bytes=MAX_ADMISSION_LIST_BYTES,
        operation="list transition admissions",
    )
    if raw and not raw.endswith(b"\0"):
        raise _IndexIncomplete("transition admission list is not NUL terminated")
    paths: list[str] = []
    for record in (item for item in raw.split(b"\0") if item):
        try:
            path = record.decode("utf-8")
        except UnicodeError as error:
            raise _IndexIncomplete("transition admission path is not UTF-8") from error
        _transition_from_admission_path(path)
        paths.append(path)
        if len(paths) > max_entries:
            raise _IndexIncomplete("transition admissions exceed their bound")
    if len(set(paths)) != len(paths):
        raise _IndexIncomplete("transition admission paths are ambiguous")
    return tuple(paths)


def _historical_admissions(
    repository: RepositoryBinding,
    head: str,
    commits: tuple[str, ...],
    *,
    transition_limit: int,
) -> tuple[_AdmissionCandidate, ...]:
    result = run_git(
        repository,
        "log",
        "--first-parent",
        "--format=%H",
        f"--max-count={transition_limit + 1}",
        head,
        "--",
        ":(glob)transitions/T-*/admission.json",
    )
    if result.returncode != 0:
        raise _IndexIncomplete("unable to scan transition admission history")
    try:
        changed_commits = tuple(result.stdout.decode("ascii").splitlines())
    except UnicodeError as error:
        raise _IndexIncomplete("transition admission history is not ASCII") from error
    if len(changed_commits) > transition_limit or any(
        _COMMIT.fullmatch(commit) is None for commit in changed_commits
    ):
        raise _IndexIncomplete("transition admission history exceeds its bound")
    positions = {commit: offset for offset, commit in enumerate(commits)}
    admitted: dict[str, _AdmissionCandidate] = {}
    changes = 0
    for commit in reversed(changed_commits):
        position = positions.get(commit)
        if position is None:
            raise _IndexIncomplete("transition admission left first-parent history")
        parent = commits[position + 1] if position + 1 < len(commits) else _EMPTY_TREE
        paths = _admission_paths_between(
            repository,
            parent,
            commit,
            max_entries=transition_limit,
        )
        changes += len(paths)
        if changes > transition_limit:
            raise _IndexIncomplete("transition admission history exceeds its bound")
        before = {
            entry.path: entry
            for entry in (
                ()
                if parent == _EMPTY_TREE
                else read_repository_tree_entries(repository, parent, paths)
            )
        }
        after = {
            entry.path: entry
            for entry in read_repository_tree_entries(repository, commit, paths)
        }
        for path in paths:
            entry = after.get(path)
            if path in before or entry is None or path in admitted:
                raise _IndexIncomplete("admitted transition record is not immutable")
            admitted[path] = _AdmissionCandidate(entry=entry, commit=commit)
    head_entries = {
        entry.path: entry
        for entry in read_repository_tree_entries(repository, head, admitted)
    }
    if any(head_entries.get(path) != item.entry for path, item in admitted.items()):
        raise _IndexIncomplete("admitted transition record was deleted or changed")
    return tuple(admitted[path] for path in sorted(admitted))


def _first_parent_window(
    repository: RepositoryBinding,
    head: str,
    *,
    limit: int,
) -> tuple[tuple[str, ...], str | None]:
    result = run_git(
        repository,
        "rev-list",
        "--first-parent",
        f"--max-count={limit + 1}",
        head,
    )
    if result.returncode != 0:
        raise _IndexIncomplete("unable to read bounded first-parent history")
    try:
        commits = tuple(result.stdout.decode("ascii").splitlines())
    except UnicodeError as error:
        raise _IndexIncomplete("first-parent history is not ASCII") from error
    if not commits or commits[0] != head or any(
        _COMMIT.fullmatch(item) is None for item in commits
    ):
        raise _IndexIncomplete("first-parent history is invalid")
    if len(commits) > limit:
        return commits[:limit], commits[limit]
    return commits, None


def _validate_admitted_transition(
    repository: RepositoryBinding,
    *,
    head: str,
    canonical_ref: str,
    transition_id: str,
    commit: str,
    head_admission: RepositoryTreeEntry,
    budget: _CaptureBudget,
) -> _ValidatedTransition:
    proposal_ref = f"transitions/{transition_id}/proposal.json"
    audit_ref = f"transitions/{transition_id}/audit.json"
    admission_ref = f"transitions/{transition_id}/admission.json"
    final_tree, parent = _commit_tree_and_parent(repository, commit)
    entries = {
        entry.path: entry
        for entry in read_repository_tree_entries(
            repository,
            commit,
            (proposal_ref, audit_ref, admission_ref),
        )
    }
    if set(entries) != {proposal_ref, audit_ref, admission_ref} or any(
        entry.kind != "blob" or entry.mode != "100644" for entry in entries.values()
    ):
        raise _IndexIncomplete("admitted transition triplet is incomplete")
    if entries[admission_ref] != head_admission:
        raise _IndexIncomplete("canonical transition admission changed after admission")
    head_entries = {
        entry.path: entry
        for entry in read_repository_tree_entries(
            repository,
            head,
            (proposal_ref, audit_ref, admission_ref),
        )
    }
    if head_entries != entries:
        raise _IndexIncomplete("canonical transition triplet is not immutable")
    proposal_bytes = read_repository_blob(
        repository,
        entries[proposal_ref].oid,
        max_bytes=MAX_PROPOSAL_BYTES,
        reserve_bytes=budget.reserve,
    )
    audit_bytes = read_repository_blob(
        repository,
        entries[audit_ref].oid,
        max_bytes=MAX_AUDIT_FILE_BYTES,
        reserve_bytes=budget.reserve,
    )
    admission_bytes = read_repository_blob(
        repository,
        entries[admission_ref].oid,
        max_bytes=MAX_ADMISSION_RECEIPT_BYTES,
        reserve_bytes=budget.reserve,
    )
    proposal = _parse_proposal(proposal_bytes)
    if proposal.base_commit != parent:
        raise _IndexIncomplete("transition proposal base is not the sole parent")
    audit = _decode_audit(audit_bytes, transition_id)
    if (
        audit["mechanically_valid"] is not True
        or audit["base_commit"] != parent
        or audit["current_head"] != parent
        or audit["proposal_blob_sha256"]
        != hashlib.sha256(proposal_bytes).hexdigest()
    ):
        raise _IndexIncomplete("transition audit binding is invalid")
    receipt = _decode_admission_receipt(admission_bytes)
    receipt_subject, receipt_audit, receipt_ref, _receipt_sha256 = (
        _admission_receipt_bindings(receipt)
    )
    if (
        receipt_subject != audit["candidate_subject_sha256"]
        or receipt_audit != audit["audit_payload_sha256"]
        or receipt_ref is not None
        and receipt_ref != canonical_ref
    ):
        raise _IndexIncomplete("transition admission binding is invalid")
    expected = _expected_candidate_paths(
        audit,
        proposal_ref,
        audit_ref,
        audit_bytes,
    )
    workspace_receipts = audit["path_receipts"]
    if not isinstance(workspace_receipts, list) or tuple(
        item.get("path") for item in workspace_receipts if isinstance(item, dict)
    ) != proposal.workspace_paths:
        raise _IndexIncomplete("audit workspace paths differ from proposal")
    _verify_final_tree(
        repository,
        commit=commit,
        final_tree=final_tree,
        parent=parent,
        admission_ref=admission_ref,
        admission_entry=entries[admission_ref],
        expected=expected,
        known_bytes={proposal_ref: proposal_bytes, audit_ref: audit_bytes},
        budget=budget,
    )
    records = _assimilation_records(
        repository,
        proposal,
        audit,
        transition_id=transition_id,
        commit=commit,
    )
    raw_links = audit["assimilation_links"]
    if not isinstance(raw_links, list):
        raise _IndexIncomplete("transition audit evidence links are invalid")
    links = tuple(_freeze_evidence_link(item) for item in raw_links)
    return _ValidatedTransition(
        transition_id=transition_id,
        commit=commit,
        assimilations=records,
        evidence_links=links,
    )


def _decode_audit(raw: bytes, transition_id: str) -> dict[str, object]:
    try:
        value = _strict_json_loads(raw)
        validate_json_shape(
            value,
            max_depth=MAX_JSON_DEPTH,
            max_nodes=max(MAX_JSON_NODES, 50_000),
        )
    except (JsonStructureError, TypeError, UnicodeError, ValueError) as error:
        raise _IndexIncomplete("canonical transition audit is invalid") from error
    if not isinstance(value, dict) or raw != _stored_json_bytes(value):
        raise _IndexIncomplete("canonical transition audit bytes are not exact")
    _validate_audit_testimony(value, transition_id)
    return value


def _commit_tree_and_parent(
    repository: RepositoryBinding,
    commit: str,
) -> tuple[str, str]:
    size_result = run_git(repository, "cat-file", "-s", commit)
    if size_result.returncode != 0:
        raise _IndexIncomplete("admission commit object is unavailable")
    try:
        size = int(size_result.stdout.decode("ascii").strip())
    except (UnicodeError, ValueError) as error:
        raise _IndexIncomplete("admission commit size is invalid") from error
    if not 0 < size <= MAX_COMMIT_BYTES:
        raise _IndexIncomplete("admission commit exceeds its byte bound")
    result = run_git(repository, "cat-file", "commit", commit)
    if result.returncode != 0 or len(result.stdout) != size:
        raise _IndexIncomplete("admission commit bytes are unavailable")
    headers, separator, _message = result.stdout.partition(b"\n\n")
    if not separator:
        raise _IndexIncomplete("admission commit headers are invalid")
    trees = [line[5:] for line in headers.splitlines() if line.startswith(b"tree ")]
    parents = [line[7:] for line in headers.splitlines() if line.startswith(b"parent ")]
    if len(trees) != 1 or len(parents) != 1:
        raise _IndexIncomplete("admission commit must have one tree and one parent")
    try:
        tree = trees[0].decode("ascii")
        parent = parents[0].decode("ascii")
    except UnicodeError as error:
        raise _IndexIncomplete("admission commit tree or parent is not ASCII") from error
    if _COMMIT.fullmatch(tree) is None or _COMMIT.fullmatch(parent) is None:
        raise _IndexIncomplete("admission commit tree or parent is invalid")
    return tree, parent


def _verify_final_tree(
    repository: RepositoryBinding,
    *,
    commit: str,
    final_tree: str,
    parent: str,
    admission_ref: str,
    admission_entry: RepositoryTreeEntry,
    expected: Mapping[str, object],
    known_bytes: Mapping[str, bytes],
    budget: _CaptureBudget,
) -> None:
    del final_tree
    expected_paths = set(expected)
    all_paths = expected_paths | {admission_ref}
    entries = {
        entry.path: entry
        for entry in read_repository_tree_entries(repository, commit, all_paths)
    }
    if set(entries) != all_paths or entries[admission_ref] != admission_entry:
        raise _IndexIncomplete("admission tree is missing exact audited paths")
    parent_entries = {
        entry.path: entry
        for entry in read_repository_tree_entries(repository, parent, all_paths)
    }
    if admission_ref in parent_entries:
        raise _IndexIncomplete("candidate tree already contained admission receipt")
    expected_changed = {admission_ref}
    for path, expectation in expected.items():
        entry = entries[path]
        if (
            entry.kind != "blob"
            or entry.mode != expectation.mode
            or expectation.blob_oid is not None
            and entry.oid != expectation.blob_oid
        ):
            raise _IndexIncomplete("admission tree differs from audited receipt")
        content = known_bytes.get(path)
        if content is None and (
            expectation.content_sha256 is not None
            or expectation.exact_content is not None
        ):
            content = read_repository_blob(
                repository,
                entry.oid,
                max_bytes=MAX_VERSIONED_FILE_BYTES,
                reserve_bytes=budget.reserve,
            )
        if content is not None and (
            expectation.content_sha256 is not None
            and hashlib.sha256(content).hexdigest() != expectation.content_sha256
            or expectation.exact_content is not None
            and content != expectation.exact_content
        ):
            raise _IndexIncomplete("admission tree content hash differs from audit")
        parent_entry = parent_entries.get(path)
        if parent_entry is None or (
            parent_entry.mode,
            parent_entry.kind,
            parent_entry.oid,
        ) != (entry.mode, entry.kind, entry.oid):
            expected_changed.add(path)
    changed = _changed_paths(repository, parent, commit, len(all_paths))
    if changed != expected_changed:
        raise _IndexIncomplete("admission commit changed paths outside the audited tree")


def _changed_paths(
    repository: RepositoryBinding,
    parent: str,
    commit: str,
    expected_count: int,
) -> set[str]:
    raw = _bounded_git_capture(
        repository,
        (
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "-r",
            "-z",
            "--no-renames",
            parent,
            commit,
            "--",
        ),
        max_bytes=max(4_096, (expected_count + 1) * 4_096),
        operation="verify transition tree delta",
    )
    if raw and not raw.endswith(b"\0"):
        raise _IndexIncomplete("transition tree delta is not NUL terminated")
    paths: set[str] = set()
    for item in (part for part in raw.split(b"\0") if part):
        try:
            path = item.decode("utf-8")
        except UnicodeError as error:
            raise _IndexIncomplete("transition tree path is not UTF-8") from error
        _canonical_path(path)
        paths.add(path)
        if len(paths) > expected_count:
            raise _IndexIncomplete("transition tree changed too many paths")
    return paths


def _assimilation_records(
    repository: RepositoryBinding,
    proposal: TransitionProposal,
    audit: Mapping[str, object],
    *,
    transition_id: str,
    commit: str,
) -> tuple[AssimilationRecord, ...]:
    raw_closure = audit["observation_closure"]
    if not isinstance(raw_closure, list) or len(raw_closure) > 1_024:
        raise _IndexIncomplete("transition observation closure is invalid")
    closure: dict[str, Mapping[str, object]] = {}
    for item in raw_closure:
        if not isinstance(item, dict):
            raise _IndexIncomplete("transition observation closure record is invalid")
        observation_ref = item.get("observation_ref")
        if not isinstance(observation_ref, str) or observation_ref in closure:
            raise _IndexIncomplete("transition observation closure is ambiguous")
        closure[observation_ref] = item
        _verify_observation_ref(repository, item)
    records: list[AssimilationRecord] = []
    for assimilation in proposal.assimilations:
        testimony = closure.get(assimilation.observation_ref)
        if testimony is None:
            raise _IndexIncomplete("assimilation lacks validated observation testimony")
        record_sha256 = testimony.get("record_sha256")
        if not isinstance(record_sha256, str) or _SHA256.fullmatch(record_sha256) is None:
            raise _IndexIncomplete("assimilation record hash is invalid")
        records.append(
            AssimilationRecord(
                observation_ref=assimilation.observation_ref,
                transition_id=transition_id,
                commit=commit,
                affected_paths=assimilation.affected_paths,
                rationale=assimilation.rationale,
                record_sha256=record_sha256,
            )
        )
    return tuple(records)


def _verify_observation_ref(
    repository: RepositoryBinding,
    testimony: Mapping[str, object],
) -> None:
    observation_ref = testimony.get("observation_ref")
    kind = testimony.get("kind")
    record_sha256 = testimony.get("record_sha256")
    immutable_ref = testimony.get("immutable_ref")
    target = testimony.get("target_commit")
    if (
        not isinstance(observation_ref, str)
        or not isinstance(kind, str)
        or not isinstance(record_sha256, str)
        or _SHA256.fullmatch(record_sha256) is None
        or not isinstance(immutable_ref, str)
        or not isinstance(target, str)
        or _COMMIT.fullmatch(target) is None
    ):
        raise _IndexIncomplete("immutable observation testimony is invalid")
    match: re.Match[str] | None
    if kind == "task_return":
        match = _TASK_OBSERVATION.fullmatch(observation_ref)
    elif kind == "run_final":
        match = _RUN_OBSERVATION.fullmatch(observation_ref)
    elif kind in {"measurement", "eval_outcome"}:
        match = _EVAL_OBSERVATION.fullmatch(observation_ref)
    else:
        match = None
    if match is None:
        raise _IndexIncomplete("immutable observation kind/ref binding is invalid")
    expected = f"refs/aros/observations/{kind}/{match.group(1)}/{record_sha256}"
    if immutable_ref != expected:
        raise _IndexIncomplete("immutable observation ref identity is invalid")
    result = run_git(repository, "show-ref", "--hash", "--verify", immutable_ref)
    if result.returncode != 0:
        raise _IndexIncomplete("immutable observation ref is missing")
    try:
        observed = result.stdout.decode("ascii").strip()
    except UnicodeError as error:
        raise _IndexIncomplete("immutable observation target is not ASCII") from error
    if observed != target:
        raise _IndexIncomplete("immutable observation target changed")
    exists = run_git(repository, "cat-file", "-e", f"{target}^{{commit}}")
    if exists.returncode != 0:
        raise _IndexIncomplete("immutable observation target commit is missing")


def _freeze_evidence_link(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict) or set(value) != _EVIDENCE_LINK_FIELDS:
        raise _IndexIncomplete("transition EvidenceLink testimony is invalid")
    try:
        validate_json_shape(value, max_depth=16, max_nodes=1_024)
    except JsonStructureError as error:
        raise _IndexIncomplete("transition EvidenceLink exceeds bounds") from error
    return _freeze_json(value)


def _freeze_json(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _transition_from_admission_path(path: str) -> str:
    match = _ADMISSION_PATH.fullmatch(path)
    if match is None or len(path.encode("utf-8")) > 1_024:
        raise _IndexIncomplete("transition admission path is invalid")
    return match.group(1)


def _bounded_git_capture(
    repository: RepositoryBinding,
    args: tuple[str, ...],
    *,
    max_bytes: int,
    operation: str,
) -> bytes:
    command = _worktrees._git_command(repository, *args)
    environment = _worktrees._git_environment()
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                text=False,
                env=environment,
            )
            try:
                returncode = process.wait(timeout=10)
            except subprocess.TimeoutExpired as error:
                process.kill()
                process.wait()
                raise _IndexIncomplete(f"Git timed out while attempting to {operation}") from error
        except OSError as error:
            raise _IndexIncomplete(f"Git failed while attempting to {operation}") from error
        if bind_repository(repository.root) != repository:
            raise _IndexIncomplete("repository binding changed during Git capture")
        stdout_file.seek(0, os.SEEK_END)
        size = stdout_file.tell()
        if size > max_bytes:
            raise _IndexIncomplete(f"{operation} exceeds its byte bound")
        if returncode != 0:
            stderr_file.seek(0)
            detail = stderr_file.read(4_096).decode("utf-8", errors="replace").strip()
            raise _IndexIncomplete(f"unable to {operation}: {detail or returncode}")
        stdout_file.seek(0)
        return stdout_file.read()


def _publish_cache(root: Path, payload: bytes) -> None:
    directory = _open_cache_directory(root, create=True)
    temporary: str | None = None
    try:
        identity = _directory_identity(directory)
        for _attempt in range(16):
            candidate = f".transition-index-{secrets.token_hex(8)}.tmp"
            try:
                descriptor = os.open(
                    candidate,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                    0o600,
                    dir_fd=directory,
                )
            except FileExistsError:
                continue
            temporary = candidate
            break
        else:
            raise _IndexIncomplete("unable to reserve transition cache temp file")
        try:
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise _IndexIncomplete("transition cache write made no progress")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _require_current_cache_directory(root, identity)
        os.replace(
            temporary,
            TRANSITION_INDEX_CACHE.name,
            src_dir_fd=directory,
            dst_dir_fd=directory,
        )
        temporary = None
        os.fsync(directory)
        _require_current_cache_directory(root, identity)
    except OSError as error:
        raise _IndexIncomplete("unable to publish transition index cache") from error
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
        os.close(directory)


def _open_cache_directory(root: Path, *, create: bool) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    descriptors: list[int] = []
    try:
        descriptors.append(os.open(root, flags))
        for component in TRANSITION_INDEX_CACHE.parts[:-1]:
            try:
                child = os.open(component, flags, dir_fd=descriptors[-1])
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(component, mode=0o700, dir_fd=descriptors[-1])
                os.fsync(descriptors[-1])
                child = os.open(component, flags, dir_fd=descriptors[-1])
            descriptors.append(child)
        result = descriptors.pop()
    except OSError as error:
        raise _IndexIncomplete("transition index cache directory is unsafe") from error
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
    return result


def _directory_identity(descriptor: int) -> tuple[int, int]:
    metadata = os.fstat(descriptor)
    return metadata.st_dev, metadata.st_ino


def _require_current_cache_directory(
    root: Path,
    expected: tuple[int, int],
) -> None:
    current = _open_cache_directory(root, create=False)
    try:
        if _directory_identity(current) != expected:
            raise _IndexIncomplete("transition index cache directory changed")
    finally:
        os.close(current)


def _require_safe_cache_parent(root: Path) -> None:
    current = root
    for component in TRANSITION_INDEX_CACHE.parts[:-1]:
        current /= component
        try:
            current.lstat()
        except FileNotFoundError:
            continue
        if not current.is_dir() or current.is_symlink():
            raise _IndexIncomplete("transition index cache parent is unsafe")
        if current.resolve(strict=True) != current:
            raise _IndexIncomplete("transition index cache parent is ambiguous")


__all__ = [
    "AssimilationRecord",
    "EvidenceTransitionRecord",
    "MAX_REBUILD_ASSIMILATIONS",
    "MAX_REBUILD_TRANSITIONS",
    "MAX_NORMAL_TRANSITIONS",
    "TRANSITION_INDEX_CACHE",
    "TransitionIndex",
    "TransitionIndexError",
    "TransitionIndexState",
    "transition_index_state_json",
]
