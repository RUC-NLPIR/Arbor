"""Bounded, read-only attention over canonical meaning and candidate reality."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from . import worktrees as _worktrees
from .attention_fit import (
    add_omission,
    bound_items,
    context_views,
    fit_packet,
    freeze_json,
    packet_json,
)
from .checkpoint import CheckpointError, read_checkpoint_projection_state
from .eval import EvalError, read_eval_inventory
from .observations import ObservationCatalog, ObservationError, ObservationRecord
from .research_files import (
    ResearchFileError,
    SemanticDocument,
    parse_semantic_document_bytes,
)
from .runs import RunError, read_run_inventory
from .store import AnchoredReadError, AnchoredWorkspaceReader
from .transition_index import TransitionIndex
from .worktrees import (
    RepositoryBinding,
    WorktreeError,
    bind_repository,
    read_candidate_status,
    read_repository_snapshot,
    read_worktree_inventory,
)


DEFAULT_ATTENTION_MAX_CHARS = 8_000
MIN_ATTENTION_MAX_CHARS = 512
MAX_ATTENTION_MAX_CHARS = 16_000

_TOP_LEVEL_KEYS = {
    "schema_version",
    "snapshot",
    "active_question",
    "current_uncertainty",
    "recent_evidence_delta",
    "hypotheses",
    "pending_measurements",
    "unassimilated_returns",
    "current_obligations",
    "remaining_budget",
    "blocked_reasons",
    "authority",
    "warnings",
    "omitted",
}
_QUESTION_ID = re.compile(r"^Q-[A-Za-z0-9][A-Za-z0-9-]*$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_ACTIVE_QUESTION_HEADINGS = (
    "Question",
    "Current best answer",
    "Current uncertainty",
    "Resolution criterion",
    "Stop / pivot criterion",
    "Expected information gain",
)
_RUN_TERMINAL_STATES = {
    "completed",
    "failed_process",
    "timed_out",
    "cancelled",
}
_MAX_EXCERPT_CHARS = 768
_MAX_DIRTY_PATHS = 48
_MAX_WORKTREES = 16
_MAX_RIVALS = 16
_MAX_PENDING = 32
_MAX_RETURNS = 32
_OPERATIONAL_SNAPSHOT_ATTEMPTS = 3
_AUTHORITY_CONTEXT_STATES = {
    "available",
    "unavailable",
    "blocked",
    "denied",
    "expired",
}
_BUDGET_CONTEXT_STATES = {
    "available",
    "unavailable",
    "not_configured",
    "exhausted",
    "blocked",
    "denied",
}


@dataclass(frozen=True)
class AttentionAuthorityContext:
    """Host-supplied authority facts with immutable defensive outer views."""

    authority: Mapping[str, object]
    remaining_budget: Mapping[str, object]
    institutional_obligations: tuple[Mapping[str, object], ...]

    def __post_init__(self) -> None:
        authority = freeze_json(self.authority, "authority")
        budget = freeze_json(self.remaining_budget, "remaining_budget")
        if not isinstance(authority, Mapping):
            raise TypeError("authority must be a mapping")
        if not isinstance(budget, Mapping):
            raise TypeError("remaining_budget must be a mapping")
        _validate_context_state(
            authority,
            "authority",
            _AUTHORITY_CONTEXT_STATES,
        )
        _validate_context_state(
            budget,
            "remaining_budget",
            _BUDGET_CONTEXT_STATES,
        )
        if not isinstance(self.institutional_obligations, tuple) or any(
            not isinstance(item, Mapping) for item in self.institutional_obligations
        ):
            raise TypeError("institutional_obligations must be a tuple of mappings")
        institutional = tuple(
            freeze_json(item, "institutional_obligations")
            for item in self.institutional_obligations
        )
        object.__setattr__(self, "authority", authority)
        object.__setattr__(self, "remaining_budget", budget)
        object.__setattr__(
            self,
            "institutional_obligations",
            institutional,
        )


def _validate_context_state(
    value: Mapping[str, object],
    field: str,
    allowed: set[str],
) -> None:
    state = value.get("state")
    if not isinstance(state, str):
        raise TypeError(f"{field}.state must be a string")
    if state not in allowed:
        raise ValueError(f"{field}.state is unknown or unbounded: {state!r}")


@dataclass(frozen=True)
class _CanonicalSemanticDocument:
    semantic: SemanticDocument
    content_sha256: str
    blob_oid: str

    @property
    def path(self) -> str:
        return self.semantic.path

    @property
    def frontmatter(self) -> Mapping[str, object]:
        return self.semantic.frontmatter

    @property
    def sections(self) -> Mapping[str, str]:
        return self.semantic.sections


@dataclass(frozen=True)
class _GitEntry:
    path: str
    blob_oid: str


class ResearchAttentionService:
    """Observe one candidate against one canonical Git repository."""

    def __init__(
        self,
        candidate_root: str | Path,
        *,
        canonical_repository: RepositoryBinding | None = None,
    ):
        try:
            self.candidate_repository = bind_repository(candidate_root)
        except WorktreeError as error:
            workspace = Path(candidate_root).expanduser().resolve()
            raise ValueError(
                "workspace is not initialized; run `aros init` at the Git root: "
                f"{workspace}"
            ) from error
        if canonical_repository is None:
            self.canonical_repository = self.candidate_repository
        else:
            observed = bind_repository(canonical_repository.root)
            if observed != canonical_repository:
                raise ValueError("canonical repository binding is stale or invalid")
            self.canonical_repository = observed

    def build(
        self,
        max_chars: int = DEFAULT_ATTENTION_MAX_CHARS,
        context: AttentionAuthorityContext | None = None,
    ) -> dict[str, object]:
        """Build one deterministic packet without persisting or reconciling state."""
        _validate_max_chars(max_chars)
        if context is not None and not isinstance(context, AttentionAuthorityContext):
            raise TypeError("context must be an AttentionAuthorityContext or None")
        _require_initialized_candidate(self.candidate_repository)

        warnings: list[str] = []
        omitted: dict[str, int] = {}
        canonical_facts = _repository_facts(self.canonical_repository)
        candidate_facts = _repository_facts(self.candidate_repository)
        candidate_state = _candidate_state(
            self.candidate_repository,
            warnings,
            omitted,
        )
        projection_state = _projection_state(
            self.candidate_repository,
            self.canonical_repository,
            candidate_facts["head"],
            canonical_facts["head"],
            canonical_facts["ref"],
        )
        if projection_state == "projection_pending":
            _warn(warnings, "projection_pending")
        snapshot = {
            "canonical": canonical_facts["head"],
            "canonical_ref": canonical_facts["ref"],
            "canonical_branch": canonical_facts["branch"],
            "canonical_repository": canonical_facts["repository"],
            "projection_state": projection_state,
            "candidate": {**candidate_facts, **candidate_state},
        }
        canonical_head = canonical_facts["head"]
        if not isinstance(canonical_head, str):
            _warn(warnings, "canonical_head_unavailable")

        documents: dict[str, _CanonicalSemanticDocument | None] = {}

        def document(path: str) -> _CanonicalSemanticDocument | None:
            if path not in documents:
                documents[path] = _load_semantic_document(
                    self.canonical_repository,
                    canonical_head,
                    path,
                    warnings,
                )
            return documents[path]

        frontier = document("questions/FRONTIER.md")
        active_question, question = _active_question(
            self.canonical_repository,
            canonical_head,
            frontier,
            warnings,
            omitted,
        )
        if question is not None:
            documents[question.path] = question

        now = document("memory/NOW.md")
        current_model = document("model/CURRENT.md")
        current_uncertainty = _current_uncertainty(
            question,
            now,
            current_model,
            warnings,
            omitted,
        )
        scientific_obligations = _matching_sections(
            now,
            "obligation",
            omitted,
        )
        semantic_blockers = _matching_sections(now, "blocker", omitted)

        hypotheses = _hypotheses(
            self.canonical_repository,
            canonical_head,
            current_model,
            warnings,
            omitted,
        )
        terminal, run_inventory, eval_inventory, availability = (
            _operational_snapshot(
            self.candidate_repository.root,
            warnings,
            )
        )
        candidate_snapshot = snapshot["candidate"]
        assert isinstance(candidate_snapshot, dict)
        candidate_snapshot["availability"] = availability
        transition_index = TransitionIndex(
            self.candidate_repository,
            self.canonical_repository,
        ).read()
        if transition_index.state == "complete":
            assimilated = set(transition_index.assimilations)
            recent_evidence_delta = _recent_evidence_delta(
                transition_index.latest_evidence_transition
            )
        else:
            assimilated = set()
            recent_evidence_delta = []
            _warn(warnings, "index_incomplete")
        visible_terminal = (
            _visible_terminal_observations(terminal, eval_inventory)
            if terminal is not None
            else ()
        )
        visible_terminal = tuple(
            record for record in visible_terminal if record.ref not in assimilated
        )
        unassimilated = (
            bound_items(
                [_observation_item(record) for record in visible_terminal],
                _MAX_RETURNS,
                "terminal observations",
                omitted,
            )
            if terminal is not None
            else []
        )
        pending = (
            _pending_measurements(
                run_inventory,
                eval_inventory,
                terminal,
                omitted,
            )
            if terminal is not None
            else []
        )

        authority, remaining_budget, institutional = _authority_views(
            context,
            omitted,
        )
        blocked = _blocked_reasons(
            semantic_blockers,
            pending,
            authority,
            remaining_budget,
            availability,
            candidate_state,
        )
        packet: dict[str, object] = {
            "schema_version": 1,
            "snapshot": snapshot,
            "active_question": active_question,
            "current_uncertainty": current_uncertainty,
            "recent_evidence_delta": recent_evidence_delta,
            "hypotheses": hypotheses,
            "pending_measurements": pending,
            "unassimilated_returns": unassimilated,
            "current_obligations": {
                "scientific": scientific_obligations,
                "institutional": institutional,
            },
            "remaining_budget": remaining_budget,
            "blocked_reasons": blocked,
            "authority": authority,
            "warnings": warnings,
            "omitted": omitted,
        }
        if recent_evidence_delta and len(packet_json(packet)) > max_chars:
            latest = recent_evidence_delta[0]
            transition_id = latest["transition_id"]
            detail_count = len(latest["assimilations"]) + len(
                latest["evidence_links"]
            )
            add_omission(
                omitted,
                f"transition:{transition_id}:detail",
                max(1, detail_count),
            )
            packet["recent_evidence_delta"] = [
                {
                    "transition_id": transition_id,
                    "commit": latest["commit"],
                }
            ]
        if omitted:
            _warn(warnings, "truncated")
        _fit_with_recent_evidence_priority(packet, max_chars)
        if (
            _repository_facts(self.canonical_repository) != canonical_facts
            or _repository_facts(self.candidate_repository) != candidate_facts
        ):
            raise ValueError("repository snapshot changed while building attention")
        return packet

    @staticmethod
    def render_text(packet: dict[str, object]) -> str:
        """Render the supplied packet itself, with no independent summary path."""
        if not isinstance(packet, dict) or set(packet) != _TOP_LEVEL_KEYS:
            raise ValueError("packet must have the exact ResearchAttentionPacket shape")
        return packet_json(packet)


def _validate_max_chars(max_chars: int) -> None:
    if (
        isinstance(max_chars, bool)
        or not isinstance(max_chars, int)
        or not MIN_ATTENTION_MAX_CHARS <= max_chars <= MAX_ATTENTION_MAX_CHARS
    ):
        raise ValueError(
            "max_chars must be an integer from "
            f"{MIN_ATTENTION_MAX_CHARS} through {MAX_ATTENTION_MAX_CHARS}"
        )


def _require_initialized_candidate(repository: RepositoryBinding) -> None:
    try:
        with AnchoredWorkspaceReader(repository.root) as reader:
            reader.require_repository(
                repository.root,
                repository.git_dir,
                repository.common_dir,
            )
            reader.require_file("AROS.md")
            reader.require_file("memory/NOW.md")
    except (AnchoredReadError, OSError) as error:
        raise ValueError(
            "workspace is not initialized; run `aros init` at the Git root: "
            f"{repository.root}"
        ) from error


def _repository_facts(repository: RepositoryBinding) -> dict[str, object]:
    return read_repository_snapshot(repository)


def _projection_state(
    candidate: RepositoryBinding,
    canonical: RepositoryBinding,
    candidate_head: object,
    canonical_head: object,
    canonical_ref: object,
) -> str:
    if (
        not isinstance(candidate_head, str)
        or not isinstance(canonical_head, str)
        or not isinstance(canonical_ref, str)
    ):
        return "unavailable"
    try:
        runtime = read_checkpoint_projection_state(
            candidate,
            canonical_commit=canonical_head,
            canonical_ref=canonical_ref,
        )
    except (CheckpointError, OSError, ValueError):
        return "unavailable"
    if runtime.state == "projection_pending":
        return "projection_pending"
    if candidate.common_dir == canonical.common_dir:
        return "current"
    if candidate_head == canonical_head:
        return "current"
    result = _worktrees._git_result(
        canonical,
        "merge-base",
        "--is-ancestor",
        candidate_head,
        canonical_head,
    )
    if result.returncode == 0:
        return "projection_pending"
    return "conflict" if result.returncode == 1 else "unavailable"


def _candidate_state(
    repository: RepositoryBinding,
    warnings: list[str],
    omitted: dict[str, int],
) -> dict[str, object]:
    git_status = _dirty_paths(repository, warnings, omitted)
    worktrees = _worktrees_view(repository, warnings, omitted)
    dirty = git_status.get("dirty_paths", [])
    assert isinstance(dirty, list)
    pending_refs = [
        {
            "path": str(item["path"]),
            "state_sha256": item["state_sha256"],
        }
        for item in dirty
        if str(item["path"]).startswith("transitions/")
        and str(item["path"]).endswith("/proposal.json")
    ]
    pending_bounded = bound_items(
        pending_refs,
        _MAX_DIRTY_PATHS,
        "transitions",
        omitted,
    )
    return {
        "git_status": git_status,
        "worktrees": worktrees,
        "pending_transition_refs": pending_bounded,
    }


def _dirty_paths(
    repository: RepositoryBinding,
    warnings: list[str],
    omitted: dict[str, int],
) -> dict[str, object]:
    try:
        status = read_candidate_status(repository)
    except (OSError, RuntimeError, WorktreeError, ValueError):
        _warn(warnings, "operational_read_failed:candidate_status")
        return {"state": "unavailable", "error": "read_failed"}
    dirty_paths = status["dirty_paths"]
    assert isinstance(dirty_paths, list)
    status["dirty_paths"] = bound_items(
        dirty_paths,
        _MAX_DIRTY_PATHS,
        "git status",
        omitted,
    )
    return status


def _worktrees_view(
    repository: RepositoryBinding,
    warnings: list[str],
    omitted: dict[str, int],
) -> dict[str, object]:
    try:
        inventory = [dict(item) for item in read_worktree_inventory(repository)]
    except (OSError, RuntimeError, WorktreeError, ValueError):
        _warn(warnings, "operational_read_failed:worktrees")
        return {"state": "unavailable", "error": "read_failed"}
    return {
        "state": "available",
        "items": bound_items(inventory, _MAX_WORKTREES, "worktrees", omitted),
    }


def _load_semantic_document(
    repository: RepositoryBinding,
    head: object,
    path: str,
    warnings: list[str],
) -> _CanonicalSemanticDocument | None:
    if not isinstance(head, str):
        _warn(warnings, f"semantic_view_unavailable:{path}")
        return None
    try:
        entry = _regular_git_entry(repository, head, path)
        raw = _worktrees._git_bytes(repository, "cat-file", "blob", entry.blob_oid)
        semantic = parse_semantic_document_bytes(path, raw)
    except (ResearchFileError, ValueError, WorktreeError):
        if _git_path_exists(repository, head, path):
            _warn(warnings, f"malformed_semantic_view:{path}")
        else:
            _warn(warnings, f"missing_semantic_view:{path}")
        return None
    return _CanonicalSemanticDocument(
        semantic=semantic,
        content_sha256=hashlib.sha256(raw).hexdigest(),
        blob_oid=entry.blob_oid,
    )


def _regular_git_entry(
    repository: RepositoryBinding,
    head: str,
    path: str,
) -> _GitEntry:
    raw = _worktrees._git_bytes(
        repository,
        "--literal-pathspecs",
        "ls-tree",
        "-z",
        "--full-tree",
        head,
        "--",
        path,
    )
    records = [record for record in raw.split(b"\0") if record]
    if len(records) != 1:
        raise ValueError("path does not name exactly one Git entry")
    header, separator, raw_path = records[0].partition(b"\t")
    fields = header.split(b" ")
    if (
        separator != b"\t"
        or raw_path != path.encode("utf-8")
        or len(fields) != 3
        or fields[0] not in {b"100644", b"100755"}
        or fields[1] != b"blob"
        or re.fullmatch(rb"[0-9a-f]{40}", fields[2]) is None
    ):
        raise ValueError("path does not name a regular Git blob")
    return _GitEntry(path=path, blob_oid=fields[2].decode("ascii"))


def _git_path_exists(repository: RepositoryBinding, head: str, path: str) -> bool:
    try:
        raw = _worktrees._git_bytes(
            repository,
            "--literal-pathspecs",
            "ls-tree",
            "-z",
            "--full-tree",
            head,
            "--",
            path,
        )
    except WorktreeError:
        return False
    return bool(raw)


def _active_question(
    repository: RepositoryBinding,
    head: object,
    frontier: _CanonicalSemanticDocument | None,
    warnings: list[str],
    omitted: dict[str, int],
) -> tuple[dict[str, object] | None, _CanonicalSemanticDocument | None]:
    if frontier is None:
        return None, None
    focus = frontier.frontmatter.get("focus_question")
    if focus is None or focus == "":
        return None, None
    if not isinstance(focus, str) or _QUESTION_ID.fullmatch(focus) is None:
        _warn(warnings, "malformed_focus_question")
        return None, None
    path = f"questions/{focus}/question.md"
    question = _load_semantic_document(repository, head, path, warnings)
    if question is None:
        return {
            "id": focus,
            "path": path,
            "content_sha256": None,
            "sections": [],
        }, None
    section_refs: list[dict[str, object]] = []
    for heading in _ACTIVE_QUESTION_HEADINGS:
        if heading not in question.sections:
            _warn(warnings, f"missing_section:{path}:{heading}")
            continue
        section_refs.append(_section_ref(question, heading, omitted))
    return {
        "id": focus,
        "path": path,
        "content_sha256": question.content_sha256,
        "sections": section_refs,
    }, question


def _current_uncertainty(
    question: _CanonicalSemanticDocument | None,
    now: _CanonicalSemanticDocument | None,
    current_model: _CanonicalSemanticDocument | None,
    warnings: list[str],
    omitted: dict[str, int],
) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for document in (question, now, current_model):
        if document is None:
            continue
        headings = [
            heading
            for heading in document.sections
            if "uncertaint" in heading.casefold()
        ]
        if not headings:
            _warn(warnings, f"missing_section:{document.path}:Current uncertainty")
            continue
        result.extend(_section_ref(document, heading, omitted) for heading in headings)
    return result


def _matching_sections(
    document: _CanonicalSemanticDocument | None,
    fragment: str,
    omitted: dict[str, int],
) -> list[dict[str, object]]:
    if document is None:
        return []
    return [
        _section_ref(document, heading, omitted)
        for heading in document.sections
        if fragment in heading.casefold()
    ]


def _section_ref(
    document: _CanonicalSemanticDocument,
    heading: str,
    omitted: dict[str, int],
) -> dict[str, object]:
    content = document.sections[heading]
    excerpt = content[:_MAX_EXCERPT_CHARS]
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    if len(excerpt) != len(content):
        add_omission(omitted, f"{document.path}#{heading}")
    return {
        "path": document.path,
        "heading": heading,
        "content_sha256": content_hash,
        "excerpt": excerpt,
    }


def _hypotheses(
    repository: RepositoryBinding,
    head: object,
    current_model: _CanonicalSemanticDocument | None,
    warnings: list[str],
    omitted: dict[str, int],
) -> dict[str, list[dict[str, object]]]:
    leading = []
    if current_model is not None:
        leading.append(
            {
                "path": current_model.path,
                "content_sha256": current_model.content_sha256,
                "blob_oid": current_model.blob_oid,
            }
        )
    rivals: list[dict[str, object]] = []
    if isinstance(head, str):
        try:
            entries = _regular_git_entries(repository, head, "model/rivals")
        except (UnicodeDecodeError, ValueError, WorktreeError):
            _warn(warnings, "malformed_semantic_view:model/rivals")
            entries = []
        rivals = [
            {"path": entry.path, "blob_oid": entry.blob_oid}
            for entry in entries
            if entry.path.endswith(".md")
        ]
    return {
        "leading": leading,
        "competing": bound_items(
            rivals,
            _MAX_RIVALS,
            "model/rivals",
            omitted,
        ),
    }


def _regular_git_entries(
    repository: RepositoryBinding,
    head: str,
    prefix: str,
) -> list[_GitEntry]:
    raw = _worktrees._git_bytes(
        repository,
        "--literal-pathspecs",
        "ls-tree",
        "-r",
        "-z",
        "--full-tree",
        head,
        "--",
        prefix,
    )
    entries: list[_GitEntry] = []
    for record in (item for item in raw.split(b"\0") if item):
        header, separator, raw_path = record.partition(b"\t")
        fields = header.split(b" ")
        path = raw_path.decode("utf-8")
        if (
            separator != b"\t"
            or len(fields) != 3
            or fields[0] not in {b"100644", b"100755"}
            or fields[1] != b"blob"
            or re.fullmatch(rb"[0-9a-f]{40}", fields[2]) is None
            or not path.startswith(f"{prefix}/")
        ):
            raise ValueError("rival path does not name a regular Git blob")
        entries.append(_GitEntry(path, fields[2].decode("ascii")))
    return sorted(entries, key=lambda entry: entry.path)


def _terminal_observations(
    root: Path,
    warnings: list[str],
) -> tuple[tuple[ObservationRecord, ...] | None, dict[str, str]]:
    try:
        return (
            ObservationCatalog(root).enumerate_terminal(),
            {"state": "available"},
        )
    except (ObservationError, OSError, RuntimeError):
        _warn(warnings, "operational_read_failed:terminal_observations")
        return None, {"state": "unavailable", "error": "read_failed"}


def _operational_snapshot(
    root: Path,
    warnings: list[str],
) -> tuple[
    tuple[ObservationRecord, ...] | None,
    tuple[dict[str, object], ...] | None,
    tuple[dict[str, object], ...] | None,
    dict[str, dict[str, str]],
]:
    for _attempt in range(_OPERATIONAL_SNAPSHOT_ATTEMPTS):
        before, before_availability = _terminal_observations(root, warnings)
        runs, run_availability = _run_inventory(root, warnings)
        evals, eval_availability = _eval_inventory(root, warnings)
        after, after_availability = _terminal_observations(root, warnings)
        terminal_availability = (
            after_availability
            if before is not None
            else before_availability
        )
        availability = {
            "runs": run_availability,
            "evals": eval_availability,
            "terminal_observations": terminal_availability,
        }
        if before is None or after is None:
            return None, runs, evals, availability
        if _terminal_identity(before) == _terminal_identity(after):
            return after, runs, evals, availability
    _warn(warnings, "operational_snapshot_unstable")
    unavailable = {
        name: {"state": "unavailable", "error": "snapshot_unstable"}
        for name in ("runs", "evals", "terminal_observations")
    }
    return None, None, None, unavailable


def _terminal_identity(
    records: tuple[ObservationRecord, ...],
) -> tuple[tuple[str, str, str], ...]:
    return tuple(
        sorted(
            (record.kind, record.ref, record.record_sha256)
            for record in records
        )
    )


def _run_inventory(
    root: Path,
    warnings: list[str],
) -> tuple[tuple[dict[str, object], ...] | None, dict[str, str]]:
    try:
        return read_run_inventory(root), {"state": "available"}
    except (OSError, RuntimeError, RunError, ValueError):
        _warn(warnings, "operational_read_failed:runs")
        return None, {"state": "unavailable", "error": "read_failed"}


def _eval_inventory(
    root: Path,
    warnings: list[str],
) -> tuple[tuple[dict[str, object], ...] | None, dict[str, str]]:
    try:
        return read_eval_inventory(root), {"state": "available"}
    except (OSError, RuntimeError, EvalError, ValueError):
        _warn(warnings, "operational_read_failed:evals")
        return None, {"state": "unavailable", "error": "read_failed"}


def _visible_terminal_observations(
    terminal: tuple[ObservationRecord, ...],
    eval_inventory: tuple[dict[str, object], ...] | None,
) -> tuple[ObservationRecord, ...]:
    linked_runs = {
        str(item["run_id"])
        for item in (eval_inventory or ())
        if isinstance(item.get("run_id"), str)
    }
    return tuple(
        record
        for record in terminal
        if not (
            record.kind == "run_final"
            and isinstance(record.payload.get("run_id"), str)
            and record.payload["run_id"] in linked_runs
        )
    )


def _recent_evidence_delta(value: object) -> list[dict[str, object]]:
    if value is None:
        return []
    assimilations = getattr(value, "assimilations")
    evidence_links = getattr(value, "evidence_links")
    return [
        {
            "transition_id": getattr(value, "transition_id"),
            "commit": getattr(value, "commit"),
            "assimilations": [
                {
                    "observation_ref": getattr(item, "observation_ref"),
                    "affected_paths": list(getattr(item, "affected_paths")),
                    "rationale": getattr(item, "rationale"),
                    "record_sha256": getattr(item, "record_sha256"),
                }
                for item in assimilations
            ],
            "evidence_links": [_plain_json(item) for item in evidence_links],
        }
    ]


def _fit_with_recent_evidence_priority(
    packet: dict[str, object],
    max_chars: int,
) -> None:
    try:
        fit_packet(packet, max_chars)
        return
    except ValueError:
        recent = packet["recent_evidence_delta"]
        if not isinstance(recent, list) or not recent:
            raise
    omitted = packet["omitted"]
    assert isinstance(omitted, dict)
    omitted_count = max(1, sum(int(count) for count in omitted.values()))
    packet.update(
        {
            "snapshot": {},
            "active_question": None,
            "current_uncertainty": [],
            "hypotheses": {"leading": [], "competing": []},
            "pending_measurements": [],
            "unassimilated_returns": [],
            "current_obligations": {"scientific": [], "institutional": []},
            "remaining_budget": {},
            "blocked_reasons": [],
            "authority": {},
            "warnings": ["truncated"],
            "omitted": {"aros boot": omitted_count},
        }
    )
    if len(packet_json(packet)) <= max_chars:
        return
    packet["recent_evidence_delta"] = []
    if len(packet_json(packet)) > max_chars:
        raise ValueError("max_chars is too small for the attention packet shape")


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _plain_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain_json(item) for item in value]
    return value


def _observation_item(record: ObservationRecord) -> dict[str, object]:
    pointers: list[dict[str, object]] = [
        {"path": path} for path in record.versioned_paths
    ]
    run_id = record.payload.get("run_id")
    if isinstance(run_id, str):
        pointers.extend(
            (
                {"path": f".aros/runs/{run_id}/stdout.log"},
                {"path": f".aros/runs/{run_id}/stderr.log"},
            )
        )
    return {
        "kind": record.kind,
        "ref": record.ref,
        "record_sha256": record.record_sha256,
        "candidate_commit": record.candidate_commit,
        "measurement_state": record.measurement_state,
        "versioned_paths": list(record.versioned_paths),
        "retrieval_pointers": pointers,
    }


def _pending_measurements(
    run_inventory: tuple[dict[str, object], ...] | None,
    eval_inventory: tuple[dict[str, object], ...] | None,
    terminal: tuple[ObservationRecord, ...],
    omitted: dict[str, int],
) -> list[dict[str, object]]:
    terminal_paths = {
        path for record in terminal for path in record.versioned_paths
    }
    pending_evals: list[dict[str, object]] = []
    linked_runs: set[str] = set()
    for status in eval_inventory or ():
        run_id = status.get("run_id")
        if isinstance(run_id, str):
            linked_runs.add(run_id)
        receipt_ref = f"eval/evaluations/{status['eval_id']}/receipt.json"
        if receipt_ref in terminal_paths:
            continue
        eval_id = str(status["eval_id"])
        terminal_missing = status.get("evaluation_state") == "completed"
        retrieval_pointers = [
            {"path": f".aros/evaluations/{eval_id}/request.json"},
            {"path": receipt_ref},
        ]
        if isinstance(run_id, str):
            retrieval_pointers.extend(
                (
                    {"path": f"runs/{run_id}/manifest.json"},
                    {"path": f".aros/runs/{run_id}/status.json"},
                    {"path": f"runs/{run_id}/final.json"},
                )
            )
        pending_evals.append(
            {
                "kind": "eval",
                "ref": f".aros/evaluations/{eval_id}/request.json",
                "record_sha256": None,
                "candidate_commit": status.get("candidate_commit"),
                "measurement_state": (
                    "terminal_observation_missing"
                    if terminal_missing
                    else status.get("measurement_state")
                ),
                "process_state": status.get("referenced_process_state"),
                "evaluation_state": status.get("evaluation_state"),
                "run_id": run_id,
                "reason": (
                    "terminal_observation_missing"
                    if terminal_missing
                    else status.get("reason")
                ),
                "versioned_paths": [],
                "retrieval_pointers": retrieval_pointers,
            }
        )
    pending_runs: list[dict[str, object]] = []
    for status in run_inventory or ():
        run_id = status.get("run_id")
        state = status.get("state")
        assert isinstance(run_id, str)
        assert isinstance(state, str)
        final_ref = f"runs/{run_id}/final.json"
        if final_ref in terminal_paths or run_id in linked_runs:
            continue
        manifest_ref = f"runs/{run_id}/manifest.json"
        terminal_missing = state in _RUN_TERMINAL_STATES
        pending_runs.append(
            {
                "kind": "run",
                "ref": manifest_ref,
                "record_sha256": status.get("manifest_sha256"),
                "candidate_commit": status.get("candidate_commit"),
                "measurement_state": (
                    "terminal_observation_missing" if terminal_missing else None
                ),
                "process_state": state,
                "updated_at": status.get("updated_at"),
                "reason": status.get("reason"),
                "versioned_paths": [manifest_ref],
                "retrieval_pointers": [
                    {"path": f".aros/runs/{run_id}/status.json"},
                    {"path": final_ref},
                ],
            }
        )
    combined = sorted(
        [*pending_evals, *pending_runs],
        key=lambda item: (str(item["kind"]), str(item["ref"])),
    )
    return bound_items(
        combined,
        _MAX_PENDING,
        "pending measurements",
        omitted,
    )


def _authority_views(
    context: AttentionAuthorityContext | None,
    omitted: dict[str, int],
) -> tuple[dict[str, object], dict[str, object], list[dict[str, object]]]:
    if context is None:
        return (
            {
                "state": "unavailable",
                "enforcement_class": "unavailable",
                "reason": "host_context_not_supplied",
            },
            {
                "state": "not_configured",
                "enforcement_class": "unavailable",
                "reason": "host_context_not_supplied",
            },
            [],
        )
    return context_views(
        context.authority,
        context.remaining_budget,
        context.institutional_obligations,
        omitted,
    )


def _blocked_reasons(
    semantic: list[dict[str, object]],
    pending: list[dict[str, object]],
    authority: dict[str, object],
    remaining_budget: dict[str, object],
    availability: dict[str, dict[str, str]],
    candidate_state: dict[str, object],
) -> list[dict[str, object]]:
    blocked: list[dict[str, object]] = [
        {"layer": "semantic", "ref": item} for item in semantic
    ]
    for item in pending:
        if (
            item.get("evaluation_state") == "lost"
            or item.get("process_state") in {"lost", "missing"}
            or item.get("measurement_state") == "terminal_observation_missing"
        ):
            blocked.append(
                {
                    "layer": "operational",
                    "ref": item["ref"],
                    "reason": item.get("reason")
                    or item.get("measurement_state"),
                }
            )
    for name, status in availability.items():
        if status.get("state") == "unavailable":
            blocked.append(
                {
                    "layer": "operational",
                    "ref": (
                        "terminal_inventory"
                        if name == "terminal_observations"
                        else f"{name.removesuffix('s')}_inventory"
                    ),
                    "reason": status.get("error") or "unavailable",
                }
            )
    for key, ref in (
        ("git_status", "candidate_git_status"),
        ("worktrees", "worktree_inventory"),
    ):
        status = candidate_state.get(key)
        if isinstance(status, dict) and status.get("state") == "unavailable":
            blocked.append(
                {
                    "layer": "operational",
                    "ref": ref,
                    "reason": status.get("error") or "unavailable",
                }
            )
    authority_state = str(authority.get("state", "")).casefold()
    if authority_state in {"blocked", "denied", "expired", "unavailable"}:
        blocked.append(
            {
                "layer": "authority",
                "reason": authority.get("reason") or authority_state,
            }
        )
    explicit = authority.get("blocked_reasons")
    if isinstance(explicit, list):
        blocked.extend(
            {"layer": "authority", "reason": reason} for reason in explicit
        )
    budget_state = str(remaining_budget.get("state", "")).casefold()
    if budget_state in {"exhausted", "blocked", "denied"}:
        blocked.append(
            {
                "layer": "budget",
                "reason": remaining_budget.get("reason") or budget_state,
            }
        )
    return blocked


def _warn(warnings: list[str], warning: str) -> None:
    if warning not in warnings:
        warnings.append(warning)


__all__ = [
    "AttentionAuthorityContext",
    "ResearchAttentionService",
]
