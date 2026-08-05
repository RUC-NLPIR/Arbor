"""Bounded, read-only attention over canonical meaning and candidate reality."""

from __future__ import annotations

import hashlib
import json
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType

from . import worktrees as _worktrees
from .eval import EvalError, EvalService
from .observations import ObservationCatalog, ObservationError, ObservationRecord
from .research_files import (
    ResearchFileError,
    _sections,
    _split_frontmatter,
    _validate_navigation_identity,
)
from .runs import RunError, RunService, read_validated_run_manifest
from .store import AnchoredReadError, AnchoredWorkspaceReader
from .worktrees import RepositoryBinding, WorktreeError, bind_repository


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
_EVAL_ID = re.compile(r"^EVAL-[0-9a-f]{64}$")
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
_MAX_EVALS = 64
_MAX_OMITTED_POINTERS = 4


@dataclass(frozen=True)
class AttentionAuthorityContext:
    """Host-supplied authority facts with immutable defensive outer views."""

    authority: Mapping[str, object]
    remaining_budget: Mapping[str, object]
    institutional_obligations: tuple[Mapping[str, object], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.authority, Mapping):
            raise TypeError("authority must be a mapping")
        if not isinstance(self.remaining_budget, Mapping):
            raise TypeError("remaining_budget must be a mapping")
        if not isinstance(self.institutional_obligations, tuple) or any(
            not isinstance(item, Mapping) for item in self.institutional_obligations
        ):
            raise TypeError("institutional_obligations must be a tuple of mappings")
        object.__setattr__(self, "authority", MappingProxyType(dict(self.authority)))
        object.__setattr__(
            self,
            "remaining_budget",
            MappingProxyType(dict(self.remaining_budget)),
        )
        object.__setattr__(
            self,
            "institutional_obligations",
            tuple(
                MappingProxyType(dict(item))
                for item in self.institutional_obligations
            ),
        )


@dataclass(frozen=True)
class _SemanticDocument:
    path: str
    content_sha256: str
    blob_oid: str
    frontmatter: Mapping[str, object]
    sections: Mapping[str, str]


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
        if not _is_initialized_candidate(self.candidate_repository.root):
            raise ValueError(
                "workspace is not initialized; run `aros init` at the Git root: "
                f"{self.candidate_repository.root}"
            )

        warnings: list[str] = []
        omitted: dict[str, dict[str, object]] = {}
        canonical = _repository_facts(self.canonical_repository)
        candidate = _repository_facts(self.candidate_repository)
        candidate_state = _candidate_state(
            self.candidate_repository,
            warnings,
            omitted,
        )
        snapshot = {
            "canonical": canonical,
            "candidate": {**candidate, **candidate_state},
        }
        canonical_head = canonical["head"]
        if not isinstance(canonical_head, str):
            _warn(warnings, "canonical_head_unavailable")

        documents: dict[str, _SemanticDocument | None] = {}

        def document(path: str) -> _SemanticDocument | None:
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
        terminal = _terminal_observations(
            self.candidate_repository.root,
            warnings,
        )
        unassimilated = _bounded_items(
            [_observation_item(record) for record in terminal],
            _MAX_RETURNS,
            "unassimilated_returns",
            omitted,
        )
        pending = _pending_measurements(
            self.candidate_repository.root,
            terminal,
            warnings,
            omitted,
        )

        authority, remaining_budget, institutional = _authority_views(context)
        blocked = _blocked_reasons(
            semantic_blockers,
            pending,
            authority,
            remaining_budget,
        )
        _warn(warnings, "index_incomplete")

        packet: dict[str, object] = {
            "schema_version": 1,
            "snapshot": snapshot,
            "active_question": active_question,
            "current_uncertainty": current_uncertainty,
            "recent_evidence_delta": [],
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
        if omitted:
            _warn(warnings, "truncated")
        _fit_packet(packet, max_chars)
        return packet

    def render_text(self, packet: dict[str, object]) -> str:
        """Render the supplied packet itself, with no independent summary path."""
        if not isinstance(packet, dict) or set(packet) != _TOP_LEVEL_KEYS:
            raise ValueError("packet must have the exact ResearchAttentionPacket shape")
        return _packet_json(packet)


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


def _is_initialized_candidate(root: Path) -> bool:
    for relative in ("AROS.md", "memory/NOW.md"):
        current = root
        parts = PurePosixPath(relative).parts
        try:
            for component in parts[:-1]:
                current /= component
                metadata = current.lstat()
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                    return False
            metadata = (current / parts[-1]).lstat()
        except OSError:
            return False
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            return False
    return True


def _repository_facts(repository: RepositoryBinding) -> dict[str, object]:
    if bind_repository(repository.root) != repository:
        raise ValueError(f"repository binding changed: {repository.root}")
    head = _optional_git_text(
        repository,
        "rev-parse",
        "--verify",
        "HEAD^{commit}",
    )
    if head is not None and _COMMIT.fullmatch(head) is None:
        head = None
    ref = _optional_git_text(repository, "symbolic-ref", "--quiet", "HEAD")
    branch = ref.removeprefix("refs/heads/") if ref is not None else None
    return {
        "repository": str(repository.root),
        "head": head,
        "ref": ref,
        "branch": branch,
    }


def _candidate_state(
    repository: RepositoryBinding,
    warnings: list[str],
    omitted: dict[str, dict[str, object]],
) -> dict[str, object]:
    dirty = _dirty_paths(repository, warnings)
    worktrees = _worktrees_view(repository, warnings)
    dirty_bounded = _bounded_items(
        dirty,
        _MAX_DIRTY_PATHS,
        "snapshot.candidate.dirty_paths",
        omitted,
    )
    worktrees_bounded = _bounded_items(
        worktrees,
        _MAX_WORKTREES,
        "snapshot.candidate.worktrees",
        omitted,
    )
    pending_refs = [
        {
            "path": str(item["path"]),
            "state_sha256": item["state_sha256"],
        }
        for item in dirty
        if str(item["path"]).startswith("transitions/")
        and str(item["path"]).endswith("/proposal.json")
    ]
    pending_bounded = _bounded_items(
        pending_refs,
        _MAX_DIRTY_PATHS,
        "snapshot.candidate.pending_transition_refs",
        omitted,
    )
    return {
        "dirty": bool(dirty),
        "dirty_paths": dirty_bounded,
        "worktrees": worktrees_bounded,
        "pending_transition_refs": pending_bounded,
    }


def _dirty_paths(
    repository: RepositoryBinding,
    warnings: list[str],
) -> list[dict[str, object]]:
    try:
        result = _worktrees._git_result(
            repository,
            "--no-optional-locks",
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--no-renames",
        )
    except WorktreeError:
        _warn(warnings, "operational_read_failed:candidate_status")
        return []
    if result.returncode != 0:
        _warn(warnings, "operational_read_failed:candidate_status")
        return []
    entries: list[dict[str, object]] = []
    for raw in (item for item in result.stdout.split(b"\0") if item):
        if len(raw) < 4 or raw[2:3] != b" ":
            _warn(warnings, "malformed_candidate_status")
            continue
        status_text = raw[:2].decode("ascii", errors="replace")
        try:
            path = raw[3:].decode("utf-8")
        except UnicodeDecodeError:
            path = raw[3:].decode("utf-8", errors="replace")
            _warn(warnings, "malformed_candidate_path")
        state_hash = hashlib.sha256(
            f"{status_text}\0{path}".encode("utf-8")
        ).hexdigest()
        entries.append(
            {
                "path": path,
                "status": status_text,
                "state_sha256": state_hash,
            }
        )
    return sorted(entries, key=lambda item: (str(item["path"]), str(item["status"])))


def _worktrees_view(
    repository: RepositoryBinding,
    warnings: list[str],
) -> list[dict[str, object]]:
    try:
        raw = _worktrees._git_bytes(repository, "worktree", "list", "--porcelain")
        text = raw.decode("utf-8")
    except (UnicodeDecodeError, WorktreeError):
        _warn(warnings, "operational_read_failed:worktrees")
        return []
    result: list[dict[str, object]] = []
    current: dict[str, object] | None = None
    for line in [*text.splitlines(), ""]:
        if line.startswith("worktree "):
            if current is not None:
                result.append(current)
            current = {
                "path": line.removeprefix("worktree "),
                "head": None,
                "branch": None,
                "detached": False,
            }
        elif not line and current is not None:
            result.append(current)
            current = None
        elif current is not None and line.startswith("HEAD "):
            value = line.removeprefix("HEAD ")
            current["head"] = None if value and set(value) == {"0"} else value
        elif current is not None and line.startswith("branch "):
            current["branch"] = line.removeprefix("branch ").removeprefix(
                "refs/heads/"
            )
        elif current is not None and line == "detached":
            current["detached"] = True
    return sorted(result, key=lambda item: str(item["path"]))


def _optional_git_text(repository: RepositoryBinding, *args: str) -> str | None:
    try:
        result = _worktrees._git_result(repository, *args)
    except WorktreeError:
        return None
    if result.returncode != 0:
        return None
    try:
        return result.stdout.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None


def _load_semantic_document(
    repository: RepositoryBinding,
    head: object,
    path: str,
    warnings: list[str],
) -> _SemanticDocument | None:
    if not isinstance(head, str):
        _warn(warnings, f"semantic_view_unavailable:{path}")
        return None
    try:
        entry = _regular_git_entry(repository, head, path)
        raw = _worktrees._git_bytes(repository, "cat-file", "blob", entry.blob_oid)
        text = raw.decode("utf-8")
        frontmatter, body = _split_frontmatter(text, path)
        _validate_navigation_identity(path, frontmatter)
        sections = _sections(body, path)
    except (ResearchFileError, UnicodeDecodeError, ValueError, WorktreeError):
        if _git_path_exists(repository, head, path):
            _warn(warnings, f"malformed_semantic_view:{path}")
        else:
            _warn(warnings, f"missing_semantic_view:{path}")
        return None
    return _SemanticDocument(
        path=path,
        content_sha256=hashlib.sha256(raw).hexdigest(),
        blob_oid=entry.blob_oid,
        frontmatter=MappingProxyType(dict(frontmatter)),
        sections=MappingProxyType(dict(sections)),
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
    frontier: _SemanticDocument | None,
    warnings: list[str],
    omitted: dict[str, dict[str, object]],
) -> tuple[dict[str, object] | None, _SemanticDocument | None]:
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
    question: _SemanticDocument | None,
    now: _SemanticDocument | None,
    current_model: _SemanticDocument | None,
    warnings: list[str],
    omitted: dict[str, dict[str, object]],
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
    document: _SemanticDocument | None,
    fragment: str,
    omitted: dict[str, dict[str, object]],
) -> list[dict[str, object]]:
    if document is None:
        return []
    return [
        _section_ref(document, heading, omitted)
        for heading in document.sections
        if fragment in heading.casefold()
    ]


def _section_ref(
    document: _SemanticDocument,
    heading: str,
    omitted: dict[str, dict[str, object]],
) -> dict[str, object]:
    content = document.sections[heading]
    excerpt = content[:_MAX_EXCERPT_CHARS]
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    if len(excerpt) != len(content):
        _record_omission(
            omitted,
            "excerpt_characters",
            len(content) - len(excerpt),
            {
                "path": document.path,
                "content_sha256": content_hash,
            },
        )
    return {
        "path": document.path,
        "heading": heading,
        "content_sha256": content_hash,
        "excerpt": excerpt,
    }


def _hypotheses(
    repository: RepositoryBinding,
    head: object,
    current_model: _SemanticDocument | None,
    warnings: list[str],
    omitted: dict[str, dict[str, object]],
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
        "competing": _bounded_items(
            rivals,
            _MAX_RIVALS,
            "hypotheses.competing",
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
) -> tuple[ObservationRecord, ...]:
    try:
        return ObservationCatalog(root).enumerate_terminal()
    except (ObservationError, OSError, RuntimeError):
        _warn(warnings, "operational_read_failed:terminal_observations")
        return ()


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
    root: Path,
    terminal: tuple[ObservationRecord, ...],
    warnings: list[str],
    omitted: dict[str, dict[str, object]],
) -> list[dict[str, object]]:
    terminal_paths = {
        path for record in terminal for path in record.versioned_paths
    }
    pending_evals, linked_runs = _pending_evaluations(
        root,
        terminal_paths,
        warnings,
        omitted,
    )
    pending_runs: list[dict[str, object]] = []
    try:
        run_statuses = RunService(root).list(reconcile=False)
    except (OSError, RuntimeError, RunError, ValueError):
        _warn(warnings, "operational_read_failed:runs")
        run_statuses = []
    for status in run_statuses:
        run_id = status.get("run_id")
        state = status.get("state")
        if not isinstance(run_id, str) or not isinstance(state, str):
            _warn(warnings, "malformed_operational_view:runs")
            continue
        final_ref = f"runs/{run_id}/final.json"
        if final_ref in terminal_paths or run_id in linked_runs:
            continue
        manifest_ref = f"runs/{run_id}/manifest.json"
        try:
            manifest = read_validated_run_manifest(root, run_id)
        except (OSError, RuntimeError, RunError, ValueError):
            _warn(warnings, f"operational_read_failed:run:{run_id}")
            continue
        terminal_missing = state in _RUN_TERMINAL_STATES
        pending_runs.append(
            {
                "kind": "run",
                "ref": manifest_ref,
                "record_sha256": manifest.get("manifest_sha256"),
                "candidate_commit": manifest.get("candidate_commit"),
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
    return _bounded_items(
        combined,
        _MAX_PENDING,
        "pending_measurements",
        omitted,
    )


def _pending_evaluations(
    root: Path,
    terminal_paths: set[str],
    warnings: list[str],
    omitted: dict[str, dict[str, object]],
) -> tuple[list[dict[str, object]], set[str]]:
    eval_ids = _eval_ids(root, warnings)
    if len(eval_ids) > _MAX_EVALS:
        for eval_id in eval_ids[_MAX_EVALS:]:
            _record_omission(
                omitted,
                "pending_eval_inventory",
                1,
                {"ref": f".aros/evaluations/{eval_id}/request.json"},
            )
        eval_ids = eval_ids[:_MAX_EVALS]
    pending: list[dict[str, object]] = []
    linked_runs: set[str] = set()
    try:
        service = EvalService(root)
    except (OSError, RuntimeError, EvalError, WorktreeError, ValueError):
        if eval_ids:
            _warn(warnings, "operational_read_failed:evals")
        return pending, linked_runs
    for eval_id in eval_ids:
        try:
            status = service.status(eval_id)
        except (OSError, RuntimeError, EvalError, ValueError):
            _warn(warnings, f"operational_read_failed:eval:{eval_id}")
            continue
        run_id = status.get("run_id")
        if isinstance(run_id, str):
            linked_runs.add(run_id)
        receipt_ref = f"eval/evaluations/{eval_id}/receipt.json"
        if receipt_ref in terminal_paths:
            continue
        state = status.get("evaluation_state")
        pending.append(
            {
                "kind": "eval",
                "ref": f".aros/evaluations/{eval_id}/request.json",
                "record_sha256": None,
                "candidate_commit": None,
                "measurement_state": status.get("measurement_state"),
                "process_state": status.get("referenced_process_state"),
                "evaluation_state": state,
                "reason": status.get("reason"),
                "versioned_paths": [],
                "retrieval_pointers": [
                    {"path": f".aros/evaluations/{eval_id}/request.json"},
                    {"path": receipt_ref},
                ],
            }
        )
    return pending, linked_runs


def _eval_ids(root: Path, warnings: list[str]) -> list[str]:
    try:
        with AnchoredWorkspaceReader(root) as reader:
            repository = bind_repository(reader.root)
            reader.require_repository(
                repository.root,
                repository.git_dir,
                repository.common_dir,
            )
            if "evaluations" not in reader.listdir(".aros"):
                return []
            metadata = reader.lstat(".aros/evaluations")
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise ValueError("evaluation inventory is not a directory")
            result: list[str] = []
            for name in reader.listdir(".aros/evaluations"):
                if _EVAL_ID.fullmatch(name) is None:
                    continue
                entry = f".aros/evaluations/{name}"
                entry_metadata = reader.lstat(entry)
                if stat.S_ISLNK(entry_metadata.st_mode) or not stat.S_ISDIR(
                    entry_metadata.st_mode
                ):
                    raise ValueError("evaluation identity is not a directory")
                if "request.json" in reader.listdir(entry):
                    reader.require_file(f"{entry}/request.json")
                    result.append(name)
            return result
    except (AnchoredReadError, OSError, RuntimeError, ValueError, WorktreeError):
        _warn(warnings, "operational_read_failed:eval_inventory")
        return []


def _authority_views(
    context: AttentionAuthorityContext | None,
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
    return (
        _plain_mapping(context.authority),
        _plain_mapping(context.remaining_budget),
        [_plain_mapping(item) for item in context.institutional_obligations],
    )


def _plain_mapping(value: Mapping[str, object]) -> dict[str, object]:
    return {key: _plain_value(item) for key, item in value.items()}


def _plain_value(value: object) -> object:
    if isinstance(value, Mapping):
        return _plain_mapping(value)
    if isinstance(value, (list, tuple)):
        return [_plain_value(item) for item in value]
    return value


def _blocked_reasons(
    semantic: list[dict[str, object]],
    pending: list[dict[str, object]],
    authority: dict[str, object],
    remaining_budget: dict[str, object],
) -> list[dict[str, object]]:
    blocked: list[dict[str, object]] = [
        {"layer": "semantic", "ref": item} for item in semantic
    ]
    for item in pending:
        if item.get("process_state") in {"lost", "missing"} or item.get(
            "measurement_state"
        ) == "terminal_observation_missing":
            blocked.append(
                {
                    "layer": "operational",
                    "ref": item["ref"],
                    "reason": item.get("reason")
                    or item.get("measurement_state"),
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


def _bounded_items(
    items: list[dict[str, object]],
    limit: int,
    key: str,
    omitted: dict[str, dict[str, object]],
) -> list[dict[str, object]]:
    for item in items[limit:]:
        _record_omission(omitted, key, 1, _omission_pointer(item))
    return items[:limit]


def _record_omission(
    omitted: dict[str, dict[str, object]],
    key: str,
    count: int,
    pointer: dict[str, object] | None,
) -> None:
    entry = omitted.setdefault(key, {"count": 0, "pointers": []})
    entry["count"] = int(entry["count"]) + count
    pointers = entry["pointers"]
    assert isinstance(pointers, list)
    if pointer and len(pointers) < _MAX_OMITTED_POINTERS and pointer not in pointers:
        pointers.append(pointer)


def _omission_pointer(item: object) -> dict[str, object] | None:
    if not isinstance(item, dict):
        return None
    pointer: dict[str, object] = {}
    for key in ("path", "ref"):
        if isinstance(item.get(key), str):
            pointer[key] = item[key]
            break
    for key in ("content_sha256", "record_sha256", "state_sha256", "blob_oid"):
        if isinstance(item.get(key), str):
            pointer[key] = item[key]
            break
    return pointer or None


def _fit_packet(packet: dict[str, object], max_chars: int) -> None:
    if len(_packet_json(packet)) <= max_chars:
        return
    warnings = packet["warnings"]
    assert isinstance(warnings, list)
    _warn(warnings, "truncated")
    omitted = packet["omitted"]
    assert isinstance(omitted, dict)

    for path in (
        ("hypotheses", "competing"),
        ("snapshot", "candidate", "worktrees"),
    ):
        target = _nested(packet, path)
        while (
            isinstance(target, list)
            and target
            and len(_packet_json(packet)) > max_chars
        ):
            removed = target.pop()
            _record_omission(
                omitted,
                ".".join(path),
                1,
                _omission_pointer(removed),
            )
    if len(_packet_json(packet)) <= max_chars:
        return

    for excerpt_limit in (256, 128, 64, 32, 16, 8, 4, 1, 0):
        _shorten_excerpts(packet, excerpt_limit, omitted)
        if len(_packet_json(packet)) <= max_chars:
            return

    list_paths = (
        ("hypotheses", "competing"),
        ("snapshot", "candidate", "worktrees"),
        ("snapshot", "candidate", "pending_transition_refs"),
        ("snapshot", "candidate", "dirty_paths"),
        ("current_obligations", "institutional"),
        ("pending_measurements",),
        ("unassimilated_returns",),
        ("current_uncertainty",),
        ("blocked_reasons",),
    )
    while len(_packet_json(packet)) > max_chars:
        changed = False
        for path in list_paths:
            target = _nested(packet, path)
            if isinstance(target, list) and target:
                removed = target.pop()
                _record_omission(
                    omitted,
                    ".".join(path),
                    1,
                    _omission_pointer(removed),
                )
                changed = True
                if len(_packet_json(packet)) <= max_chars:
                    return
        if not changed:
            break

    for entry in omitted.values():
        pointers = entry.get("pointers")
        if isinstance(pointers, list):
            del pointers[1:]
    if len(_packet_json(packet)) <= max_chars:
        return

    retained_warnings = [
        warning
        for warning in warnings
        if warning in {"index_incomplete", "truncated"}
    ]
    for warning in warnings:
        if warning not in retained_warnings:
            _record_omission(
                omitted,
                "warnings",
                1,
                {
                    "warning_sha256": hashlib.sha256(
                        warning.encode("utf-8")
                    ).hexdigest()
                },
            )
    warnings[:] = retained_warnings
    if len(_packet_json(packet)) <= max_chars:
        return

    _install_minimal_packet(packet, max_chars)
    if len(_packet_json(packet)) > max_chars:
        raise ValueError("max_chars is too small for the attention packet shape")


def _shorten_excerpts(
    value: object,
    limit: int,
    omitted: dict[str, dict[str, object]],
) -> None:
    if isinstance(value, dict):
        excerpt = value.get("excerpt")
        if isinstance(excerpt, str) and len(excerpt) > limit:
            value["excerpt"] = excerpt[:limit]
            pointer = _omission_pointer(value)
            _record_omission(
                omitted,
                "excerpt_characters",
                len(excerpt) - limit,
                pointer,
            )
        for key, item in tuple(value.items()):
            if key != "omitted":
                _shorten_excerpts(item, limit, omitted)
    elif isinstance(value, list):
        for item in value:
            _shorten_excerpts(item, limit, omitted)


def _nested(packet: dict[str, object], path: tuple[str, ...]) -> object:
    value: object = packet
    for component in path:
        if not isinstance(value, dict):
            return None
        value = value.get(component)
    return value


def _install_minimal_packet(packet: dict[str, object], max_chars: int) -> None:
    snapshot = packet["snapshot"]
    assert isinstance(snapshot, dict)
    canonical = snapshot.get("canonical")
    candidate = snapshot.get("candidate")
    assert isinstance(canonical, dict)
    assert isinstance(candidate, dict)
    omitted_count = _minimal_omitted_count(packet, canonical, candidate)
    packet["snapshot"] = {
        "canonical": {"head": canonical.get("head")},
        "candidate": {},
    }
    packet["active_question"] = None
    packet["current_uncertainty"] = []
    packet["hypotheses"] = {"leading": [], "competing": []}
    packet["pending_measurements"] = []
    packet["unassimilated_returns"] = []
    packet["current_obligations"] = {"scientific": [], "institutional": []}
    authority = packet["authority"]
    budget = packet["remaining_budget"]
    assert isinstance(authority, dict)
    assert isinstance(budget, dict)
    packet["authority"] = {"state": authority.get("state")}
    packet["remaining_budget"] = {"state": budget.get("state")}
    packet["blocked_reasons"] = []
    packet["warnings"] = ["index_incomplete", "truncated"]
    packet["omitted"] = {
        "count": omitted_count,
        "pointers": ["aros boot"],
    }
    if len(_packet_json(packet)) > max_chars:
        packet["snapshot"] = {"head": canonical.get("head")}


def _minimal_omitted_count(
    packet: dict[str, object],
    canonical: dict[str, object],
    candidate: dict[str, object],
) -> int:
    omitted = packet["omitted"]
    assert isinstance(omitted, dict)
    recorded = sum(
        int(entry.get("count", 0))
        for entry in omitted.values()
        if isinstance(entry, dict)
    )
    authority = packet["authority"]
    budget = packet["remaining_budget"]
    hypotheses = packet["hypotheses"]
    obligations = packet["current_obligations"]
    assert isinstance(authority, dict)
    assert isinstance(budget, dict)
    assert isinstance(hypotheses, dict)
    assert isinstance(obligations, dict)
    dropped = (
        _fact_count({key: value for key, value in canonical.items() if key != "head"})
        + _fact_count(candidate)
        + _fact_count(packet["active_question"])
        + _fact_count(packet["current_uncertainty"])
        + _fact_count(hypotheses)
        + _fact_count(packet["pending_measurements"])
        + _fact_count(packet["unassimilated_returns"])
        + _fact_count(obligations)
        + _fact_count(packet["blocked_reasons"])
        + _fact_count({key: value for key, value in authority.items() if key != "state"})
        + _fact_count({key: value for key, value in budget.items() if key != "state"})
    )
    return recorded + dropped


def _fact_count(value: object) -> int:
    if isinstance(value, dict):
        return sum(_fact_count(item) for item in value.values())
    if isinstance(value, list):
        return sum(_fact_count(item) for item in value)
    return 0 if value is None else 1


def _warn(warnings: list[str], warning: str) -> None:
    if warning not in warnings:
        warnings.append(warning)


def _packet_json(packet: dict[str, object]) -> str:
    return json.dumps(
        packet,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


__all__ = [
    "AttentionAuthorityContext",
    "ResearchAttentionService",
]
