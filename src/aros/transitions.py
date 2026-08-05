"""Exact read-only audit of explicit Principal research transitions."""

from __future__ import annotations

import hashlib
import re
import stat
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import tasks as _tasks
from . import worktrees as _worktrees
from .eval import EvalError, read_validated_eval_receipt
from .observations import (
    ObservationCatalog,
    ObservationError,
    ObservationRecord,
    validate_task_measurement_lineage,
)
from .research_files import (
    EvidenceLinkOccurrence,
    ResearchFileError,
    ResearchFileLimitError,
    ResearchFileStructureError,
    SemanticDocument,
    parse_semantic_document_bytes,
)
from .runs import RunError, read_validated_run_final, read_validated_run_manifest
from .store import (
    AnchoredReadError,
    AnchoredReadLimitError,
    AnchoredReadStructureError,
    AnchoredWorkspaceReader,
    JsonStructureError,
    _strict_json_loads,
    json_sha256,
    validate_json_shape,
)
from .tasks import TaskError, read_validated_task_collection
from .worktrees import RepositoryBinding, WorktreeError


_PROPOSAL_FIELDS = {
    "schema_version",
    "base_commit",
    "workspace_paths",
    "assimilations",
}
_ASSIMILATION_FIELDS = {"observation_ref", "affected_paths", "rationale"}
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TRANSITION_ID = re.compile(
    r"^T-[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?$"
)
_TASK_PATH = re.compile(
    r"^tasks/(TASK-[0-9]{8}-[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)/"
    r"(brief|collected)\.json$"
)
_RUN_PATH = re.compile(
    r"^runs/(RUN-[A-Za-z0-9][A-Za-z0-9-]*)/(manifest|final)\.json$"
)
_EVAL_PATH = re.compile(
    r"^eval/evaluations/(EVAL-[0-9a-f]{64})/receipt\.json$"
)
_SEMANTIC_PREFIXES = (
    "memory/",
    "questions/",
    "knowledge/claims/",
    "ideas/",
    "model/",
)
MAX_PROPOSAL_BYTES = 1_048_576
MAX_WORKSPACE_PATHS = 256
MAX_ASSIMILATIONS = 256
MAX_AFFECTED_PATHS = 256
MAX_PATH_BYTES = 1_024
MAX_REFERENCE_BYTES = 1_024
MAX_RATIONALE_BYTES = 4_096
MAX_EVIDENCE_SCOPE_BYTES = 4_096
MAX_OBSERVATION_CLOSURE_PATHS = 1_024
MAX_PATH_COMPONENTS = 16
MAX_VERSIONED_FILE_BYTES = 16_777_216
MAX_EVIDENCE_LINKS_PER_FILE = 1_024
MAX_EVIDENCE_LINKS_AGGREGATE = 4_096
MAX_JSON_DEPTH = 64
MAX_JSON_NODES = 10_000
MAX_AUDIT_CAPTURE_BYTES = 67_108_864


class TransitionError(ValueError):
    """A transition proposal or audit input is unsafe or ambiguous."""


class _ExecutableFileError(TransitionError):
    pass


class _ResourceLimitError(TransitionError):
    pass


@dataclass(frozen=True)
class Assimilation:
    observation_ref: str
    affected_paths: tuple[str, ...]
    rationale: str


@dataclass(frozen=True)
class TransitionProposal:
    schema_version: int
    base_commit: str
    workspace_paths: tuple[str, ...]
    assimilations: tuple[Assimilation, ...]


@dataclass(frozen=True)
class _SemanticState:
    document: SemanticDocument
    added_links: tuple[EvidenceLinkOccurrence, ...]


@dataclass(frozen=True)
class _CurrentFile:
    content: bytes
    mode: str


def load_transition_proposal(
    root: str | Path,
    proposal_ref: str,
) -> TransitionProposal:
    """Strictly load one exact proposal; its directory remains its identity."""
    _proposal_identity(proposal_ref)
    try:
        with AnchoredWorkspaceReader(root) as reader:
            repository = _worktrees.bind_repository(reader.root)
            reader.require_repository(
                repository.root,
                repository.git_dir,
                repository.common_dir,
            )
            _reject_nested_repository(reader, proposal_ref)
            snapshot = _worktrees.read_repository_snapshot(repository)
            head = snapshot.get("head")
            if isinstance(head, str):
                gitlink = _worktrees.find_repository_gitlink_ancestor(
                    repository,
                    head,
                    proposal_ref,
                )
                if gitlink is not None:
                    raise TransitionError(
                        f"proposal descends from base gitlink: {gitlink}"
                    )
            current = _read_anchored_file(
                reader,
                proposal_ref,
                max_bytes=MAX_PROPOSAL_BYTES,
                require_non_executable=True,
            )
            return _parse_proposal(current.content)
    except TransitionError:
        raise
    except (AnchoredReadError, OSError, TypeError, UnicodeError, ValueError) as error:
        raise TransitionError(f"invalid transition proposal: {proposal_ref}: {error}") from error


def build_operational_proposal(
    base_commit: str,
    workspace_paths: Iterable[str],
    record_sha256: str,
) -> tuple[str, dict[str, object]]:
    """Build the exact four-field proposal for service-owned records."""
    if not isinstance(base_commit, str) or _COMMIT.fullmatch(base_commit) is None:
        raise TransitionError("operational base_commit must be 40 lowercase hex")
    if not isinstance(record_sha256, str) or _SHA256.fullmatch(record_sha256) is None:
        raise TransitionError("operational record_sha256 must be 64 lowercase hex")
    if isinstance(workspace_paths, (str, bytes)):
        raise TransitionError("operational workspace_paths must be an iterable of paths")
    try:
        normalized: set[str] = set()
        for count, path in enumerate(workspace_paths, start=1):
            if count > MAX_WORKSPACE_PATHS:
                raise TransitionError(
                    "operational workspace_paths exceeds 256 entries"
                )
            normalized.add(_canonical_path(path))
        paths = sorted(normalized)
    except TypeError as error:
        raise TransitionError("operational workspace_paths are invalid") from error
    for path in paths:
        owner = _path_owner(path)
        if owner not in {"task", "run", "eval"}:
            raise TransitionError(
                f"operational path must be service-owned: {path}"
            )
    transition_id = f"T-OPS-{base_commit[:12]}-{record_sha256[:12]}"
    return transition_id, {
        "schema_version": 1,
        "base_commit": base_commit,
        "workspace_paths": paths,
        "assimilations": [],
    }


class TransitionAuditService:
    """Build deterministic mechanical receipts without changing workspace state."""

    def __init__(self, root: str | Path, *, canonical_ref: str):
        _bounded_utf8(canonical_ref, MAX_REFERENCE_BYTES, "canonical_ref")
        if not canonical_ref.startswith("refs/"):
            raise TransitionError("canonical_ref must be a full Git ref")
        try:
            self.repository = _worktrees.bind_repository(root)
        except WorktreeError as error:
            raise TransitionError(f"invalid transition workspace: {root}") from error
        self.root = self.repository.root
        self.canonical_ref = canonical_ref

    def audit(self, proposal_ref: str) -> dict[str, object]:
        """Audit one proposal as a revalidated, side-effect-free snapshot."""
        try:
            return self._audit_snapshot(proposal_ref)
        except (AnchoredReadError, OSError, WorktreeError) as error:
            issues: list[dict[str, str]] = []
            _add_issue(
                issues,
                "error",
                "concurrent_drift",
                _safe_ref(proposal_ref),
                f"audit snapshot changed or became unsafe: {type(error).__name__}",
            )
            return _finalize_audit(
                transition_id=_safe_transition_id(proposal_ref),
                base_commit=None,
                current_head=None,
                proposal_blob_sha256=None,
                path_receipts=[],
                observation_closure=[],
                assimilation_links=[],
                issues=issues,
            )

    def _audit_snapshot(self, proposal_ref: str) -> dict[str, object]:
        issues: list[dict[str, str]] = []
        path_receipts: list[dict[str, object]] = []
        observation_closure: list[dict[str, object]] = []
        assimilation_links: list[dict[str, object]] = []
        transition_id = _safe_transition_id(proposal_ref)
        proposal_blob_sha256: str | None = None
        base_commit: str | None = None
        current_head: str | None = None

        with AnchoredWorkspaceReader(
            self.root,
            max_json_bytes=MAX_VERSIONED_FILE_BYTES,
            max_json_depth=MAX_JSON_DEPTH,
            max_json_nodes=MAX_JSON_NODES,
            max_capture_bytes=MAX_AUDIT_CAPTURE_BYTES,
        ) as reader:
            repository = _worktrees.bind_repository(reader.root)
            if repository != self.repository:
                raise WorktreeError("transition repository binding changed")
            reader.require_repository(
                repository.root,
                repository.git_dir,
                repository.common_dir,
            )
            before = _worktrees.read_repository_snapshot(repository)
            if isinstance(before.get("head"), str):
                current_head = str(before["head"])
            try:
                canonical_commit = _worktrees.resolve_repository_commit(
                    repository,
                    self.canonical_ref,
                )
            except WorktreeError as error:
                canonical_commit = None
                _add_issue(
                    issues,
                    "error",
                    "canonical_ref_invalid",
                    self.canonical_ref,
                    str(error),
                )

            proposal: TransitionProposal | None = None
            try:
                transition_id = _proposal_identity(proposal_ref)
            except TransitionError as error:
                _add_issue(
                    issues,
                    "error",
                    "invalid_proposal_ref",
                    _safe_ref(proposal_ref),
                    str(error),
                )
            else:
                try:
                    _reject_nested_repository(reader, proposal_ref)
                    if canonical_commit is not None:
                        gitlink = _worktrees.find_repository_gitlink_ancestor(
                            repository,
                            canonical_commit,
                            proposal_ref,
                        )
                        if gitlink is not None:
                            raise TransitionError(
                                f"proposal descends from base gitlink: {gitlink}"
                            )
                except (TransitionError, WorktreeError) as error:
                    _add_issue(
                        issues,
                        "error",
                        "submodule_proposal_path",
                        proposal_ref,
                        str(error),
                    )
                else:
                    try:
                        proposal_file = _read_anchored_file(
                            reader,
                            proposal_ref,
                            max_bytes=MAX_PROPOSAL_BYTES,
                            require_non_executable=True,
                        )
                        proposal_blob_sha256 = hashlib.sha256(
                            proposal_file.content
                        ).hexdigest()
                        proposal = _parse_proposal(proposal_file.content)
                        base_commit = proposal.base_commit
                    except (AnchoredReadError, OSError) as error:
                        _add_issue(
                            issues,
                            "error",
                            "unsafe_proposal",
                            proposal_ref,
                            f"proposal is not an anchored ordinary file: {type(error).__name__}",
                        )
                    except _ResourceLimitError as error:
                        _add_issue(
                            issues,
                            "error",
                            "resource_limit_proposal",
                            proposal_ref,
                            str(error),
                        )
                    except _ExecutableFileError as error:
                        _add_issue(
                            issues,
                            "error",
                            "executable_proposal",
                            proposal_ref,
                            str(error),
                        )
                    except TransitionError as error:
                        _add_issue(
                            issues,
                            "error",
                            "invalid_proposal_schema",
                            proposal_ref,
                            str(error),
                        )

            if proposal is not None:
                if canonical_commit is None or proposal.base_commit != canonical_commit:
                    _add_issue(
                        issues,
                        "error",
                        "stale_base",
                        proposal_ref,
                        "proposal base_commit is not the current canonical ref commit",
                    )
                (
                    documents,
                    changed_paths,
                    direct_observation_refs,
                    evidence_resource_limited,
                ) = self._audit_workspace_paths(
                    reader,
                    repository,
                    proposal,
                    path_receipts,
                    issues,
                )
                observation_refs = {
                    *direct_observation_refs,
                    *(item.observation_ref for item in proposal.assimilations),
                }
                records = self._resolve_observations(
                    reader,
                    observation_refs,
                    issues,
                )
                assimilated_records = self._audit_assimilations(
                    transition_id,
                    proposal,
                    documents,
                    changed_paths,
                    records,
                    evidence_resource_limited,
                    assimilation_links,
                    issues,
                )
                closure_paths = _bounded_closure_paths(records.values())
                if closure_paths is None:
                    _add_issue(
                        issues,
                        "error",
                        "observation_closure_limit",
                        "observations",
                        "observation closure exceeds 1024 unique paths",
                    )
                else:
                    query_paths = {
                        path
                        for path in closure_paths
                        if _closure_path_is_versioned(path)
                    }
                    try:
                        closure_base_entries = _read_base_tree_entries(
                            repository,
                            proposal.base_commit,
                            query_paths,
                        )
                    except WorktreeError as error:
                        _add_issue(
                            issues,
                            "error",
                            "invalid_observation_base_tree",
                            proposal.base_commit,
                            str(error),
                        )
                        closure_base_entries = {}
                    emitted_closure_paths: set[str] = set()
                    observation_closure.extend(
                        self._observation_closure(
                            reader,
                            proposal,
                            record,
                            closure_base_entries,
                            emitted_closure_paths,
                            issues,
                        )
                        for record in records.values()
                    )
                _audit_joint_lineage(assimilated_records, issues)

            after = _worktrees.read_repository_snapshot(repository)
            try:
                canonical_after = _worktrees.resolve_repository_commit(
                    repository,
                    self.canonical_ref,
                )
            except WorktreeError:
                canonical_after = None
            if (
                after != before
                or canonical_after != canonical_commit
                or _worktrees.bind_repository(reader.root) != repository
            ):
                _add_issue(
                    issues,
                    "error",
                    "concurrent_drift",
                    self.canonical_ref,
                    "repository binding, ref, or HEAD changed during audit",
                )

            return _finalize_audit(
                transition_id=transition_id,
                base_commit=base_commit,
                current_head=current_head,
                proposal_blob_sha256=proposal_blob_sha256,
                path_receipts=path_receipts,
                observation_closure=observation_closure,
                assimilation_links=assimilation_links,
                issues=issues,
            )

    def _audit_workspace_paths(
        self,
        reader: AnchoredWorkspaceReader,
        repository: RepositoryBinding,
        proposal: TransitionProposal,
        receipts: list[dict[str, object]],
        issues: list[dict[str, str]],
    ) -> tuple[dict[str, _SemanticState], set[str], set[str], bool]:
        documents: dict[str, _SemanticState] = {}
        changed_paths: set[str] = set()
        direct_observation_refs: set[str] = set()
        evidence_link_count = 0
        evidence_resource_limited = False
        try:
            base_entries = _read_base_tree_entries(
                repository,
                proposal.base_commit,
                proposal.workspace_paths,
            )
        except WorktreeError as error:
            _add_issue(
                issues,
                "error",
                "invalid_base_tree",
                proposal.base_commit,
                str(error),
            )
            base_entries = {}
        for path in proposal.workspace_paths:
            owner = _path_owner(path)
            if owner is None:
                _add_issue(
                    issues,
                    "error",
                    "unsupported_workspace_path",
                    path,
                    "path is outside approved semantic and service-owned roots",
                )
                continue
            try:
                gitlink = _base_gitlink_ancestor(base_entries, path)
                if gitlink is not None:
                    _add_issue(
                        issues,
                        "error",
                        "submodule_workspace_path",
                        path,
                        f"path descends from base gitlink: {gitlink}",
                    )
                    continue
                _reject_nested_repository(reader, path)
                current = _read_anchored_file(
                    reader,
                    path,
                    max_bytes=MAX_VERSIONED_FILE_BYTES,
                )
            except _ResourceLimitError as error:
                _add_issue(
                    issues,
                    "error",
                    "resource_limit_current_file",
                    path,
                    str(error),
                )
                continue
            except TransitionError as error:
                _add_issue(
                    issues,
                    "error",
                    "submodule_workspace_path",
                    path,
                    str(error),
                )
                continue
            except WorktreeError as error:
                _add_issue(
                    issues,
                    "error",
                    "invalid_base_path",
                    path,
                    str(error),
                )
                continue
            except (AnchoredReadError, OSError) as error:
                _add_issue(
                    issues,
                    "error",
                    "unsafe_current_path",
                    path,
                    f"path is not an anchored ordinary file: {type(error).__name__}",
                )
                continue
            raw = current.content
            blob_oid = _blob_oid(raw)
            receipts.append(
                {
                    "path": path,
                    "owner": owner,
                    "mode": current.mode,
                    "blob_oid": blob_oid,
                    "content_sha256": hashlib.sha256(raw).hexdigest(),
                }
            )
            if current.mode != "100644":
                _add_issue(
                    issues,
                    "error",
                    "executable_workspace_path",
                    path,
                    "workspace file mode must be non-executable 100644",
                )
            if owner == "semantic":
                try:
                    document = parse_semantic_document_bytes(
                        path,
                        raw,
                        max_evidence_links=min(
                            MAX_EVIDENCE_LINKS_PER_FILE,
                            MAX_EVIDENCE_LINKS_AGGREGATE - evidence_link_count,
                        ),
                    )
                except ResearchFileLimitError as error:
                    evidence_resource_limited = True
                    _add_issue(
                        issues,
                        "error",
                        "resource_limit_evidence_links",
                        path,
                        str(error),
                    )
                    continue
                except ResearchFileStructureError as error:
                    evidence_resource_limited = True
                    _add_issue(
                        issues,
                        "error",
                        "resource_limit_semantic_structure",
                        path,
                        str(error),
                    )
                    continue
                except (ResearchFileError, TypeError, UnicodeError) as error:
                    _add_issue(
                        issues,
                        "error",
                        "invalid_semantic_file",
                        path,
                        str(error),
                    )
                    continue
                evidence_link_count += len(document.evidence_links)
                try:
                    base_entry = _base_regular_entry(path, base_entries)
                except WorktreeError as error:
                    _add_issue(
                        issues,
                        "error",
                        "invalid_base_path",
                        path,
                        str(error),
                    )
                    continue
                base_document: SemanticDocument | None = None
                base_is_exact = (
                    base_entry is not None
                    and base_entry.oid == blob_oid
                    and base_entry.mode == current.mode
                )
                if base_is_exact:
                    base_document = document
                    _add_issue(
                        issues,
                        "error",
                        "semantic_path_unchanged",
                        path,
                        "selected semantic path does not differ from base_commit",
                    )
                else:
                    changed_paths.add(path)
                    try:
                        base = _base_repository_file(
                            reader,
                            repository,
                            path,
                            base_entries,
                        )
                    except (
                        _ResourceLimitError,
                        _worktrees.WorktreeLimitError,
                    ) as error:
                        evidence_resource_limited = True
                        _add_issue(
                            issues,
                            "error",
                            "resource_limit_base_semantic",
                            path,
                            f"base semantic baseline unavailable: {error}",
                        )
                        continue
                    except WorktreeError as error:
                        _add_issue(
                            issues,
                            "error",
                            "invalid_base_path",
                            path,
                            str(error),
                        )
                        continue
                    if base is not None:
                        try:
                            base_document = parse_semantic_document_bytes(
                                path,
                                base.content,
                                max_evidence_links=MAX_EVIDENCE_LINKS_PER_FILE,
                            )
                        except (
                            ResearchFileError,
                            TypeError,
                            UnicodeError,
                        ) as error:
                            _add_issue(
                                issues,
                                "warning",
                                "invalid_base_semantic",
                                path,
                                f"base semantic baseline ignored: {error}",
                            )
                documents[path] = _SemanticState(
                    document=document,
                    added_links=_added_evidence_links(document, base_document),
                )
                _audit_evidence_bounds(document, issues)
                for warning in document.warnings:
                    _add_issue(
                        issues,
                        "warning",
                        "missing_recommended_heading",
                        path,
                        warning,
                    )
            else:
                terminal_ref = _terminal_observation_ref(path)
                if terminal_ref is not None:
                    direct_observation_refs.add(terminal_ref)
                    continue
                try:
                    _validate_service_record(reader, self.root, path, owner)
                except (EvalError, RunError, TaskError, TypeError, ValueError) as error:
                    _add_issue(
                        issues,
                        "error",
                        "invalid_service_record",
                        path,
                        str(error),
                    )
        return (
            documents,
            changed_paths,
            direct_observation_refs,
            evidence_resource_limited,
        )

    def _resolve_observations(
        self,
        reader: AnchoredWorkspaceReader,
        observation_refs: set[str],
        issues: list[dict[str, str]],
    ) -> dict[str, ObservationRecord]:
        if not observation_refs:
            return {}
        try:
            catalog = ObservationCatalog(reader.root)
        except ObservationError as error:
            _add_issue(
                issues,
                "error",
                "invalid_observation_catalog",
                "observations",
                str(error),
            )
            return {}
        records: dict[str, ObservationRecord] = {}
        for observation_ref in sorted(observation_refs):
            try:
                records[observation_ref] = catalog.resolve(
                    observation_ref,
                    reader=reader,
                )
            except ObservationError as error:
                _add_issue(
                    issues,
                    "error",
                    (
                        "resource_limit_observation_json"
                        if _caused_by_anchored_limit(error)
                        else "invalid_observation"
                    ),
                    observation_ref,
                    str(error),
                )
        return records

    def _audit_assimilations(
        self,
        transition_id: str,
        proposal: TransitionProposal,
        documents: Mapping[str, _SemanticState],
        changed_paths: set[str],
        records: Mapping[str, ObservationRecord],
        evidence_resource_limited: bool,
        links: list[dict[str, object]],
        issues: list[dict[str, str]],
    ) -> list[ObservationRecord]:
        assimilated_records: list[ObservationRecord] = []
        record_by_assimilation: dict[Assimilation, ObservationRecord] = {}
        for assimilation in proposal.assimilations:
            target_path, anchor = _rationale_parts(assimilation.rationale)
            for affected in assimilation.affected_paths:
                if affected not in proposal.workspace_paths:
                    _add_issue(
                        issues,
                        "error",
                        "affected_path_undeclared",
                        affected,
                        "assimilation affected path is absent from workspace_paths",
                    )
                elif _path_owner(affected) != "semantic":
                    _add_issue(
                        issues,
                        "error",
                        "affected_path_not_semantic",
                        affected,
                        "assimilation affected paths must be semantic",
                    )
                elif affected not in changed_paths:
                    _add_issue(
                        issues,
                        "error",
                        "affected_path_unchanged",
                        affected,
                        "assimilation affected path must differ from base_commit",
                    )
            if target_path not in assimilation.affected_paths:
                _add_issue(
                    issues,
                    "error",
                    "rationale_path_invalid",
                    assimilation.rationale,
                    "rationale path must be one of the assimilation affected_paths",
                )
            state = documents.get(target_path)
            if state is None or anchor not in state.document.sections:
                _add_issue(
                    issues,
                    "error",
                    "rationale_anchor_missing",
                    assimilation.rationale,
                    "rationale heading does not exist in its semantic file",
                )
            record = records.get(assimilation.observation_ref)
            if record is not None:
                assimilated_records.append(record)
                record_by_assimilation[assimilation] = record
            if record is not None and record.kind == "measurement" and (
                record.measurement_state not in {
                "valid",
                "underpowered",
                }
            ):
                _add_issue(
                    issues,
                    "error",
                    "invalid_measurement_state",
                    record.ref,
                    "measurement observations must be valid or underpowered",
                )
            if state is None or anchor not in state.document.sections:
                continue

        if evidence_resource_limited:
            return assimilated_records

        matched: Counter[Assimilation] = Counter()
        for path, state in sorted(documents.items()):
            for occurrence in state.added_links:
                matches = [
                    assimilation
                    for assimilation in proposal.assimilations
                    if path in assimilation.affected_paths
                    and _rationale_parts(assimilation.rationale)
                    == (path, occurrence.anchor)
                    and assimilation.observation_ref
                    == occurrence.link.observation_ref
                ]
                if len(matches) != 1:
                    _add_issue(
                        issues,
                        "error",
                        "evidence_delta_link_declaration_mismatch",
                        f"{path}#{occurrence.anchor}:{occurrence.ordinal}",
                        "EvidenceLink delta must match exactly one declared assimilation",
                    )
                    continue
                assimilation = matches[0]
                matched[assimilation] += 1
                record = record_by_assimilation.get(assimilation)
                if record is None:
                    continue
                if (
                    record.kind in {"run_final", "eval_outcome"}
                    and occurrence.link.relation != "context"
                ):
                    _add_issue(
                        issues,
                        "error",
                        "nonmeasurement_evidence",
                        assimilation.rationale,
                        "process or evaluation outcomes may only be context",
                    )
                links.append(
                    _link_receipt(
                        transition_id,
                        assimilation.observation_ref,
                        occurrence,
                    )
                )

        for assimilation, record in record_by_assimilation.items():
            target_path, _anchor = _rationale_parts(assimilation.rationale)
            if (
                record.kind == "measurement"
                and target_path.startswith("knowledge/claims/")
                and matched[assimilation] == 0
            ):
                _add_issue(
                    issues,
                    "error",
                    "measurement_evidence_link_missing",
                    assimilation.rationale,
                    "measurement Claim assimilation requires a new matching EvidenceLink",
                )
        return assimilated_records

    def _observation_closure(
        self,
        reader: AnchoredWorkspaceReader,
        proposal: TransitionProposal,
        record: ObservationRecord,
        base_entries: Mapping[str, _worktrees.RepositoryTreeEntry],
        emitted_paths: set[str],
        issues: list[dict[str, str]],
    ) -> dict[str, object]:
        paths: list[dict[str, object]] = []
        versioned_paths: list[str] = []
        seen: set[str] = set()
        for path in sorted(record.versioned_paths):
            if path in seen:
                _add_issue(
                    issues,
                    "error",
                    "duplicate_observation_path",
                    record.ref,
                    f"observation lineage repeats versioned path: {path}",
                )
                continue
            seen.add(path)
            try:
                canonical = _canonical_path(path)
            except TransitionError:
                _add_issue(
                    issues,
                    "error",
                    "invalid_observation_path",
                    record.ref,
                    f"observation lineage contains an unsafe path: {path}",
                )
                continue
            owner = _path_owner(canonical)
            if canonical.startswith((".aros/", ".git/")):
                _add_issue(
                    issues,
                    "error",
                    "runtime_observation_closure",
                    record.ref,
                    f"runtime path cannot enter observation closure: {canonical}",
                )
                continue
            if owner not in {"task", "run", "eval"}:
                _add_issue(
                    issues,
                    "error",
                    "unsupported_observation_closure",
                    record.ref,
                    f"observation path is not service-owned: {canonical}",
                )
                continue
            versioned_paths.append(canonical)
            if canonical in emitted_paths:
                continue
            try:
                gitlink = _base_gitlink_ancestor(base_entries, canonical)
                if gitlink is not None:
                    _add_issue(
                        issues,
                        "error",
                        "submodule_observation_closure",
                        record.ref,
                        f"observation path descends from base gitlink: {gitlink}",
                    )
                    continue
                _reject_nested_repository(reader, canonical)
                current = _read_anchored_file(
                    reader,
                    canonical,
                    max_bytes=MAX_VERSIONED_FILE_BYTES,
                )
                base = _base_regular_entry(canonical, base_entries)
            except _ResourceLimitError as error:
                _add_issue(
                    issues,
                    "error",
                    "resource_limit_observation_file",
                    canonical,
                    str(error),
                )
                continue
            except TransitionError as error:
                _add_issue(
                    issues,
                    "error",
                    "submodule_observation_closure",
                    record.ref,
                    str(error),
                )
                continue
            except (AnchoredReadError, OSError, WorktreeError) as error:
                _add_issue(
                    issues,
                    "error",
                    "observation_closure_missing",
                    record.ref,
                    f"versioned observation path is unavailable: {canonical}: {type(error).__name__}",
                )
                continue
            raw = current.content
            blob_oid = _blob_oid(raw)
            paths.append(
                {
                    "path": canonical,
                    "mode": current.mode,
                    "state": (
                        "workspace"
                        if canonical in proposal.workspace_paths
                        else (
                            "ref_only"
                            if (
                                base is not None
                                and base.oid == blob_oid
                                and base.mode == current.mode
                            )
                            else "derived"
                        )
                    ),
                    "blob_oid": blob_oid,
                }
            )
            emitted_paths.add(canonical)
            if current.mode != "100644":
                _add_issue(
                    issues,
                    "error",
                    "executable_observation_closure",
                    canonical,
                    "observation file mode must be non-executable 100644",
                )
        task_base_status: str | None = None
        if record.kind == "task_return":
            task_base_status = (
                "current"
                if record.payload.get("base_commit") == proposal.base_commit
                else "stale"
            )
        return {
            "observation_ref": record.ref,
            "kind": record.kind,
            "record_sha256": record.record_sha256,
            "versioned_paths": versioned_paths,
            "candidate_commit": record.candidate_commit,
            "measurement_state": record.measurement_state,
            "task_base_status": task_base_status,
            "paths": paths,
        }


def _parse_proposal(raw: bytes) -> TransitionProposal:
    try:
        value = _strict_json_loads(raw)
    except (TypeError, UnicodeError, ValueError) as error:
        raise TransitionError(f"proposal must be strict UTF-8 JSON: {error}") from error
    try:
        validate_json_shape(
            value,
            max_depth=MAX_JSON_DEPTH,
            max_nodes=MAX_JSON_NODES,
        )
    except JsonStructureError as error:
        raise _ResourceLimitError(
            f"proposal JSON structure exceeds limits: {error}"
        ) from error
    if not isinstance(value, dict) or set(value) != _PROPOSAL_FIELDS:
        raise TransitionError("proposal must contain exactly the four required fields")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise TransitionError("proposal schema_version must be integer 1")
    base_commit = value["base_commit"]
    if not isinstance(base_commit, str) or _COMMIT.fullmatch(base_commit) is None:
        raise TransitionError("proposal base_commit must be 40 lowercase hex")
    raw_paths = value["workspace_paths"]
    if not isinstance(raw_paths, list) or any(not isinstance(path, str) for path in raw_paths):
        raise TransitionError("proposal workspace_paths must be a list of strings")
    if len(raw_paths) > MAX_WORKSPACE_PATHS:
        raise TransitionError("proposal workspace_paths exceeds 256 entries")
    workspace_paths = tuple(_canonical_path(path) for path in raw_paths)
    if len(set(workspace_paths)) != len(workspace_paths) or tuple(
        sorted(workspace_paths)
    ) != workspace_paths:
        raise TransitionError("proposal workspace_paths must be unique and sorted")

    raw_assimilations = value["assimilations"]
    if not isinstance(raw_assimilations, list):
        raise TransitionError("proposal assimilations must be a list")
    if len(raw_assimilations) > MAX_ASSIMILATIONS:
        raise TransitionError("proposal assimilations exceeds 256 entries")
    assimilations: list[Assimilation] = []
    for raw_assimilation in raw_assimilations:
        if not isinstance(raw_assimilation, dict) or set(raw_assimilation) != _ASSIMILATION_FIELDS:
            raise TransitionError("assimilation must contain exactly three required fields")
        observation_ref = raw_assimilation["observation_ref"]
        rationale = raw_assimilation["rationale"]
        raw_affected = raw_assimilation["affected_paths"]
        if not isinstance(observation_ref, str) or not observation_ref:
            raise TransitionError("assimilation observation_ref must be a non-empty string")
        _bounded_utf8(
            observation_ref,
            MAX_REFERENCE_BYTES,
            "assimilation observation_ref",
        )
        _canonical_path(observation_ref)
        if not isinstance(raw_affected, list) or not raw_affected or any(
            not isinstance(path, str) for path in raw_affected
        ):
            raise TransitionError("assimilation affected_paths must be a non-empty list")
        if len(raw_affected) > MAX_AFFECTED_PATHS:
            raise TransitionError("assimilation affected_paths exceeds 256 entries")
        affected_paths = tuple(_canonical_path(path) for path in raw_affected)
        if len(set(affected_paths)) != len(affected_paths) or tuple(
            sorted(affected_paths)
        ) != affected_paths:
            raise TransitionError("assimilation affected_paths must be unique and sorted")
        if not isinstance(rationale, str):
            raise TransitionError("assimilation rationale must be a string")
        _bounded_utf8(rationale, MAX_RATIONALE_BYTES, "assimilation rationale")
        _rationale_parts(rationale)
        assimilations.append(
            Assimilation(
                observation_ref=observation_ref,
                affected_paths=affected_paths,
                rationale=rationale,
            )
        )
    keys = [
        (item.observation_ref, item.affected_paths, item.rationale)
        for item in assimilations
    ]
    if len({item.observation_ref for item in assimilations}) != len(assimilations):
        raise TransitionError("proposal assimilations must have unique observation refs")
    if keys != sorted(keys):
        raise TransitionError("proposal assimilations must be stably sorted")
    return TransitionProposal(
        schema_version=1,
        base_commit=base_commit,
        workspace_paths=workspace_paths,
        assimilations=tuple(assimilations),
    )


def _proposal_identity(proposal_ref: str) -> str:
    canonical = _canonical_path(proposal_ref)
    path = PurePosixPath(canonical)
    if (
        len(path.parts) != 3
        or path.parts[0] != "transitions"
        or path.parts[2] != "proposal.json"
        or _TRANSITION_ID.fullmatch(path.parts[1]) is None
    ):
        raise TransitionError(
            "proposal path must be transitions/T-<safe-id>/proposal.json"
        )
    return path.parts[1]


def _safe_transition_id(proposal_ref: object) -> str:
    try:
        return _proposal_identity(proposal_ref)  # type: ignore[arg-type]
    except (TransitionError, TypeError):
        return ""


def _safe_ref(value: object) -> str:
    if not isinstance(value, str):
        return ""
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return ""
    return value if len(encoded) <= MAX_REFERENCE_BYTES else ""


def _bounded_utf8(value: object, maximum: int, field: str) -> str:
    if not isinstance(value, str):
        raise TransitionError(f"{field} must be a string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise TransitionError(f"{field} must contain valid UTF-8") from error
    if len(encoded) > maximum:
        raise TransitionError(f"{field} exceeds {maximum} UTF-8 bytes")
    return value


def _canonical_path(value: object) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise TransitionError(f"path must be a canonical relative path: {value!r}")
    _bounded_utf8(value, MAX_PATH_BYTES, "path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
        or len(path.parts) > MAX_PATH_COMPONENTS
    ):
        raise TransitionError(f"path must be a canonical relative path: {value!r}")
    return value


def _rationale_parts(rationale: str) -> tuple[str, str]:
    _bounded_utf8(rationale, MAX_RATIONALE_BYTES, "assimilation rationale")
    if rationale != rationale.strip() or "#" not in rationale:
        raise TransitionError("assimilation rationale must be exact path#Heading")
    path, separator, heading = rationale.partition("#")
    if not separator or not heading or heading != heading.strip():
        raise TransitionError("assimilation rationale must be exact path#Heading")
    return _canonical_path(path), heading


def _path_owner(path: str) -> str | None:
    if path == "AROS.md" or path.startswith(_SEMANTIC_PREFIXES):
        return "semantic"
    if _TASK_PATH.fullmatch(path) is not None:
        return "task"
    if _RUN_PATH.fullmatch(path) is not None:
        return "run"
    if _EVAL_PATH.fullmatch(path) is not None:
        return "eval"
    return None


def _terminal_observation_ref(path: str) -> str | None:
    task = _TASK_PATH.fullmatch(path)
    if task is not None and task.group(2) == "collected":
        return path
    run = _RUN_PATH.fullmatch(path)
    if run is not None and run.group(2) == "final":
        return path
    if _EVAL_PATH.fullmatch(path) is not None:
        return path
    return None


def _read_base_tree_entries(
    repository: RepositoryBinding,
    commit: str,
    paths: Iterable[str],
) -> dict[str, _worktrees.RepositoryTreeEntry]:
    query_paths: set[str] = set()
    for path in paths:
        candidate = PurePosixPath(_canonical_path(path))
        query_paths.update(
            PurePosixPath(*candidate.parts[:length]).as_posix()
            for length in range(1, len(candidate.parts) + 1)
        )
    return {
        entry.path: entry
        for entry in _worktrees.read_repository_tree_entries(
            repository,
            commit,
            query_paths,
        )
    }


def _base_gitlink_ancestor(
    entries: Mapping[str, _worktrees.RepositoryTreeEntry],
    path: str,
) -> str | None:
    candidate = PurePosixPath(path)
    for length in range(1, len(candidate.parts) + 1):
        ancestor = PurePosixPath(*candidate.parts[:length]).as_posix()
        entry = entries.get(ancestor)
        if entry is None:
            continue
        if entry.mode == "160000" and entry.kind == "commit":
            return ancestor
        if length < len(candidate.parts) and not (
            entry.mode == "040000" and entry.kind == "tree"
        ):
            raise WorktreeError(
                f"repository path descends through a non-tree entry: {ancestor}"
            )
    return None


def _base_repository_file(
    reader: AnchoredWorkspaceReader,
    repository: RepositoryBinding,
    path: str,
    entries: Mapping[str, _worktrees.RepositoryTreeEntry],
) -> _worktrees.RepositoryFile | None:
    entry = entries.get(path)
    if entry is None:
        return None
    if entry.mode not in {"100644", "100755"} or entry.kind != "blob":
        raise WorktreeError(f"repository path is not a regular SHA-1 blob: {path}")
    return _worktrees.RepositoryFile(
        path=path,
        mode=entry.mode,
        blob_oid=entry.oid,
        content=_worktrees.read_repository_blob(
            repository,
            entry.oid,
            max_bytes=MAX_VERSIONED_FILE_BYTES,
            reserve_bytes=lambda size: _reserve_base_blob(
                reader,
                entry.oid,
                size,
            ),
        ),
    )


def _reserve_base_blob(
    reader: AnchoredWorkspaceReader,
    blob_oid: str,
    size: int,
) -> None:
    try:
        reader.reserve_external_capture(f"git-blob:{blob_oid}", size)
    except AnchoredReadLimitError as error:
        raise _ResourceLimitError(str(error)) from error


def _base_regular_entry(
    path: str,
    entries: Mapping[str, _worktrees.RepositoryTreeEntry],
) -> _worktrees.RepositoryTreeEntry | None:
    entry = entries.get(path)
    if entry is None:
        return None
    if entry.mode not in {"100644", "100755"} or entry.kind != "blob":
        raise WorktreeError(f"repository path is not a regular SHA-1 blob: {path}")
    return entry


def _closure_path_is_versioned(path: object) -> bool:
    try:
        canonical = _canonical_path(path)
    except TransitionError:
        return False
    return (
        not canonical.startswith((".aros/", ".git/"))
        and _path_owner(canonical) in {"task", "run", "eval"}
    )


def _bounded_closure_paths(
    records: Iterable[ObservationRecord],
) -> set[str] | None:
    paths: set[str] = set()
    for record in records:
        for path in record.versioned_paths:
            paths.add(path)
            if len(paths) > MAX_OBSERVATION_CLOSURE_PATHS:
                return None
    return paths


def _audit_evidence_bounds(
    document: SemanticDocument,
    issues: list[dict[str, str]],
) -> None:
    for occurrence in document.evidence_links:
        try:
            _bounded_utf8(
                occurrence.link.observation_ref,
                MAX_REFERENCE_BYTES,
                "EvidenceLink observation_ref",
            )
        except TransitionError as error:
            _add_issue(
                issues,
                "error",
                "evidence_reference_too_long",
                f"{document.path}#{occurrence.anchor}",
                str(error),
            )
        try:
            _bounded_utf8(
                occurrence.link.scope,
                MAX_EVIDENCE_SCOPE_BYTES,
                "EvidenceLink scope",
            )
        except TransitionError as error:
            _add_issue(
                issues,
                "error",
                "evidence_scope_too_long",
                f"{document.path}#{occurrence.anchor}",
                str(error),
            )


def _added_evidence_links(
    current: SemanticDocument,
    base: SemanticDocument | None,
) -> tuple[EvidenceLinkOccurrence, ...]:
    remaining = Counter(
        _evidence_identity(occurrence)
        for occurrence in (() if base is None else base.evidence_links)
    )
    added: list[EvidenceLinkOccurrence] = []
    for occurrence in current.evidence_links:
        identity = _evidence_identity(occurrence)
        if remaining[identity]:
            remaining[identity] -= 1
        else:
            added.append(occurrence)
    return tuple(added)


def _evidence_identity(
    occurrence: EvidenceLinkOccurrence,
) -> tuple[str, str, int, str]:
    return (
        occurrence.path,
        occurrence.anchor,
        occurrence.ordinal,
        occurrence.canonical_sha256,
    )


def _read_anchored_file(
    reader: AnchoredWorkspaceReader,
    relative: str,
    *,
    max_bytes: int | None = None,
    require_non_executable: bool = False,
) -> _CurrentFile:
    key = reader._workspace_file_key(relative)
    with reader._open_file(key) as (descriptor, anchored):
        mode = (
            "100755"
            if stat.S_IMODE(anchored.identity[2]) & 0o111
            else "100644"
        )
        if require_non_executable and mode != "100644":
            raise _ExecutableFileError(
                "proposal file mode must be non-executable 100644"
            )
        if max_bytes is not None and anchored.identity[4] > max_bytes:
            raise _ResourceLimitError(
                f"file exceeds {max_bytes} bytes: {relative}"
            )
        try:
            reader._reserve_capture(key, anchored.identity[4])
        except AnchoredReadLimitError as error:
            raise _ResourceLimitError(str(error)) from error
        payload, _size, _digest = reader._stream_file(
            descriptor,
            anchored,
            capture=True,
            capture_limit=None,
        )
    assert payload is not None
    return _CurrentFile(content=payload, mode=mode)


def _reject_nested_repository(
    reader: AnchoredWorkspaceReader,
    relative: str,
) -> None:
    parent = PurePosixPath()
    for component in PurePosixPath(relative).parts[:-1]:
        parent /= component
        if ".git" in reader.listdir(parent.as_posix()):
            raise TransitionError(
                f"path is inside a nested repository or submodule: {relative}"
            )


def _blob_oid(raw: bytes) -> str:
    return hashlib.sha1(
        b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw
    ).hexdigest()


def _validate_service_record(
    reader: AnchoredWorkspaceReader,
    root: Path,
    path: str,
    owner: str,
) -> None:
    if owner == "task":
        match = _TASK_PATH.fullmatch(path)
        assert match is not None
        task_id, kind = match.groups()
        if kind == "collected":
            read_validated_task_collection(root, task_id, reader=reader)
        else:
            value = reader.read_json(path)
            if not isinstance(value, dict):
                raise TaskError(f"invalid task brief schema: {task_id}")
            _tasks._validate_task_brief(value, task_id)
        return
    if owner == "run":
        match = _RUN_PATH.fullmatch(path)
        assert match is not None
        run_id, kind = match.groups()
        if kind == "manifest":
            read_validated_run_manifest(root, run_id, reader=reader)
        else:
            read_validated_run_final(root, run_id, reader=reader)
        return
    match = _EVAL_PATH.fullmatch(path)
    assert owner == "eval" and match is not None
    read_validated_eval_receipt(root, match.group(1), reader=reader)


def _link_receipt(
    transition_id: str,
    observation_ref: str,
    occurrence: object,
) -> dict[str, object]:
    path = occurrence.path
    anchor = occurrence.anchor
    ordinal = occurrence.ordinal
    link = occurrence.link
    identity = {
        "schema_version": 1,
        "transition_id": transition_id,
        "path": path,
        "anchor": anchor,
        "ordinal": ordinal,
        "canonical_sha256": occurrence.canonical_sha256,
    }
    return {
        "link_id": f"EL-{json_sha256(identity)}",
        "observation_ref": observation_ref,
        "path": path,
        "anchor": anchor,
        "ordinal": ordinal,
        "relation": link.relation,
        "scope": link.scope,
        "canonical_sha256": occurrence.canonical_sha256,
    }


def _audit_joint_lineage(
    records: list[ObservationRecord],
    issues: list[dict[str, str]],
) -> None:
    tasks = [record for record in records if record.kind == "task_return"]
    measurements = [record for record in records if record.kind == "measurement"]
    if not tasks or not measurements:
        return
    task_groups: dict[str, list[ObservationRecord]] = defaultdict(list)
    measurement_groups: dict[str, list[ObservationRecord]] = defaultdict(list)
    for record in tasks:
        candidate = record.candidate_commit
        if not isinstance(candidate, str) or _COMMIT.fullmatch(candidate) is None:
            _add_issue(
                issues,
                "error",
                "task_measurement_candidate_mismatch",
                record.ref,
                "joint observation has no valid candidate commit",
            )
            continue
        task_groups[candidate].append(record)
    for record in measurements:
        candidate = record.candidate_commit
        if not isinstance(candidate, str) or _COMMIT.fullmatch(candidate) is None:
            _add_issue(
                issues,
                "error",
                "task_measurement_candidate_mismatch",
                record.ref,
                "joint observation has no valid candidate commit",
            )
            continue
        measurement_groups[candidate].append(record)
    for candidate in sorted(set(task_groups) | set(measurement_groups)):
        candidate_tasks = task_groups.get(candidate, [])
        candidate_measurements = measurement_groups.get(candidate, [])
        if not candidate_tasks or not candidate_measurements:
            unmatched = sorted(
                record.ref for record in [*candidate_tasks, *candidate_measurements]
            )
            _add_issue(
                issues,
                "error",
                "task_measurement_candidate_mismatch",
                "|".join(unmatched),
                f"candidate {candidate} lacks a matching Task or measurement observation",
            )
            continue
        try:
            validate_task_measurement_lineage(
                candidate_tasks[0],
                candidate_measurements[0],
            )
        except ObservationError as error:
            refs = sorted(
                record.ref for record in [*candidate_tasks, *candidate_measurements]
            )
            _add_issue(
                issues,
                "error",
                "task_measurement_candidate_mismatch",
                "|".join(refs),
                str(error),
            )


def _caused_by_anchored_limit(error: BaseException) -> bool:
    pending: list[BaseException] = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(
            current,
            (AnchoredReadLimitError, AnchoredReadStructureError),
        ):
            return True
        for related in (
            current.__cause__,
            current.__context__,
            getattr(current, "original_error", None),
            getattr(current, "revalidation_error", None),
        ):
            if isinstance(related, BaseException):
                pending.append(related)
    return False


def _add_issue(
    issues: list[dict[str, str]],
    severity: str,
    code: str,
    ref: str,
    detail: str,
) -> None:
    issue = {"severity": severity, "code": code, "ref": ref, "detail": detail}
    if issue not in issues:
        issues.append(issue)


def _finalize_audit(
    *,
    transition_id: str,
    base_commit: str | None,
    current_head: str | None,
    proposal_blob_sha256: str | None,
    path_receipts: list[dict[str, object]],
    observation_closure: list[dict[str, object]],
    assimilation_links: list[dict[str, object]],
    issues: list[dict[str, str]],
) -> dict[str, object]:
    ordered_receipts = sorted(
        path_receipts,
        key=lambda item: (str(item["path"]), str(item["owner"])),
    )
    for record in observation_closure:
        paths = record.get("paths")
        if isinstance(paths, list):
            paths.sort(key=lambda item: str(item["path"]))
    ordered_closure = sorted(
        observation_closure,
        key=lambda item: str(item["observation_ref"]),
    )
    ordered_links = sorted(
        assimilation_links,
        key=lambda item: (
            str(item["path"]),
            str(item["anchor"]),
            int(item["ordinal"]),
            str(item["observation_ref"]),
        ),
    )
    ordered_issues = sorted(
        issues,
        key=lambda item: (
            item["severity"],
            item["code"],
            item["ref"],
            item["detail"],
        ),
    )
    mechanically_valid = not any(
        issue["severity"] == "error" for issue in ordered_issues
    )
    payload: dict[str, object] = {
        "schema_version": 1,
        "transition_id": transition_id,
        "base_commit": base_commit,
        "current_head": current_head,
        "proposal_blob_sha256": proposal_blob_sha256,
        "path_receipts": ordered_receipts,
        "observation_closure": ordered_closure,
        "assimilation_links": ordered_links,
        "mechanically_valid": mechanically_valid,
        "issues": ordered_issues,
    }
    audit_payload_sha256 = json_sha256(payload)
    workspace = sorted(
        [item["path"], item["owner"], item["blob_oid"]]
        for item in ordered_receipts
    )
    derived_closure = sorted(
        [path["path"], path["blob_oid"]]
        for record in ordered_closure
        for path in record["paths"]
        if path["state"] == "derived"
    )
    candidate_subject_sha256 = json_sha256(
        {
            "schema_version": 1,
            "transition_id": transition_id,
            "base_commit": base_commit,
            "workspace": workspace,
            "observation_closure": derived_closure,
            "proposal_blob_sha256": proposal_blob_sha256,
            "audit_payload_sha256": audit_payload_sha256,
        }
    )
    return {
        "schema_version": 1,
        "transition_id": transition_id,
        "base_commit": base_commit,
        "current_head": current_head,
        "proposal_blob_sha256": proposal_blob_sha256,
        "path_receipts": ordered_receipts,
        "observation_closure": ordered_closure,
        "assimilation_links": ordered_links,
        "audit_payload_sha256": audit_payload_sha256,
        "candidate_subject_sha256": candidate_subject_sha256,
        "mechanically_valid": mechanically_valid,
        "issues": ordered_issues,
    }


__all__ = [
    "Assimilation",
    "MAX_AFFECTED_PATHS",
    "MAX_AUDIT_CAPTURE_BYTES",
    "MAX_ASSIMILATIONS",
    "MAX_EVIDENCE_SCOPE_BYTES",
    "MAX_EVIDENCE_LINKS_AGGREGATE",
    "MAX_EVIDENCE_LINKS_PER_FILE",
    "MAX_JSON_DEPTH",
    "MAX_JSON_NODES",
    "MAX_OBSERVATION_CLOSURE_PATHS",
    "MAX_PATH_BYTES",
    "MAX_PROPOSAL_BYTES",
    "MAX_RATIONALE_BYTES",
    "MAX_REFERENCE_BYTES",
    "MAX_WORKSPACE_PATHS",
    "MAX_VERSIONED_FILE_BYTES",
    "TransitionAuditService",
    "TransitionError",
    "TransitionProposal",
    "build_operational_proposal",
    "load_transition_proposal",
]
