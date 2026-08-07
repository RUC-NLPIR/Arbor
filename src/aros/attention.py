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
from .eval import EvalError, read_eval_inventory
from .observations import ObservationCatalog, ObservationError, ObservationRecord
from .observed import ObservedRefError, validate_observed_ref
from .research_files import (
    ResearchFileError,
    SemanticDocument,
    parse_semantic_document_bytes,
)
from .runs import RunError, read_run_inventory
from .store import AnchoredReadError, AnchoredWorkspaceReader
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
    "unread_returns",
    "current_obligations",
    "remaining_budget",
    "blocked_reasons",
    "authority",
    "warnings",
    "omitted",
}
_QUESTION_ID = re.compile(r"^Q-[A-Za-z0-9][A-Za-z0-9-]*$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_QUESTION_HEADINGS = (
    "Question",
    "Current best answer",
    "Current uncertainty",
    "Resolution criterion",
    "Stop / pivot criterion",
    "Expected information gain",
)
_TERMINAL_RUN_STATES = {"completed", "failed_process", "timed_out", "cancelled"}
_AUTHORITY_STATES = {"available", "unavailable", "blocked", "denied", "expired"}
_BUDGET_STATES = {
    "available", "unavailable", "not_configured", "exhausted", "blocked", "denied"
}
_MAX_EXCERPT_CHARS = 768
_MAX_DIRTY_PATHS = 48
_MAX_WORKTREES = 16
_MAX_RIVALS = 16
_MAX_PENDING = 32
_MAX_RETURNS = 32


@dataclass(frozen=True)
class AttentionAuthorityContext:
    authority: Mapping[str, object]
    remaining_budget: Mapping[str, object]
    institutional_obligations: tuple[Mapping[str, object], ...]

    def __post_init__(self) -> None:
        authority = freeze_json(self.authority, "authority")
        budget = freeze_json(self.remaining_budget, "remaining_budget")
        if not isinstance(authority, Mapping) or not isinstance(budget, Mapping):
            raise TypeError("authority and remaining_budget must be mappings")
        _require_context_state(authority, "authority", _AUTHORITY_STATES)
        _require_context_state(budget, "remaining_budget", _BUDGET_STATES)
        if not isinstance(self.institutional_obligations, tuple) or any(
            not isinstance(item, Mapping) for item in self.institutional_obligations
        ):
            raise TypeError("institutional_obligations must be a tuple of mappings")
        obligations = tuple(
            freeze_json(item, "institutional_obligations")
            for item in self.institutional_obligations
        )
        object.__setattr__(self, "authority", authority)
        object.__setattr__(self, "remaining_budget", budget)
        object.__setattr__(self, "institutional_obligations", obligations)


def _require_context_state(
    value: Mapping[str, object],
    field: str,
    allowed: set[str],
) -> None:
    state = value.get("state")
    if not isinstance(state, str):
        raise TypeError(f"{field}.state must be a string")
    if state not in allowed:
        raise ValueError(f"{field}.state is unknown: {state!r}")


@dataclass(frozen=True)
class _Document:
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


class ResearchAttentionService:
    def __init__(
        self,
        candidate_root: str | Path,
        *,
        canonical_repository: RepositoryBinding | None = None,
    ) -> None:
        try:
            self.candidate_repository = bind_repository(candidate_root)
        except WorktreeError as error:
            root = Path(candidate_root).expanduser().resolve()
            raise ValueError(
                f"workspace is not initialized; run `aros start` at the Git root: {root}"
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
        _validate_max_chars(max_chars)
        if context is not None and not isinstance(context, AttentionAuthorityContext):
            raise TypeError("context must be an AttentionAuthorityContext or None")
        _require_workspace(self.candidate_repository)

        warnings: list[str] = []
        omitted: dict[str, int] = {}
        canonical = read_repository_snapshot(self.canonical_repository)
        candidate = read_repository_snapshot(self.candidate_repository)
        head = canonical.get("head")
        documents: dict[str, _Document | None] = {}

        def document(path: str) -> _Document | None:
            if path not in documents:
                documents[path] = _load_document(
                    self.canonical_repository,
                    head,
                    path,
                    warnings,
                )
            return documents[path]

        frontier = document("questions/FRONTIER.md")
        active_question, question = _active_question(
            self.canonical_repository,
            head,
            frontier,
            warnings,
            omitted,
        )
        now = document("memory/NOW.md")
        model = document("model/CURRENT.md")
        terminal = _terminal_observations(self.candidate_repository.root, warnings)
        runs = _runs(self.candidate_repository.root, warnings)
        evals = _evals(self.candidate_repository.root, warnings)
        observed, recent = _observed_git_history(
            self.canonical_repository,
            warnings,
        )
        unread = bound_items(
            [_observation_item(item) for item in terminal if item.ref not in observed],
            _MAX_RETURNS,
            "terminal observations",
            omitted,
        )
        pending = _pending_measurements(runs, evals, terminal, omitted)
        authority, budget, institutional = _authority_views(context, omitted)
        semantic_blockers = _matching_sections(now, "blocker", omitted)

        packet: dict[str, object] = {
            "schema_version": 1,
            "snapshot": {
                "canonical": head,
                "canonical_ref": canonical.get("ref"),
                "canonical_branch": canonical.get("branch"),
                "candidate": {
                    "head": candidate.get("head"),
                    "branch": candidate.get("branch"),
                    **_candidate_state(self.candidate_repository, warnings, omitted),
                },
            },
            "active_question": active_question,
            "current_uncertainty": _uncertainty(
                question,
                now,
                model,
                warnings=warnings,
                omitted=omitted,
            ),
            "recent_evidence_delta": recent,
            "hypotheses": _hypotheses(
                self.canonical_repository,
                head,
                model,
                warnings,
                omitted,
            ),
            "pending_measurements": pending,
            "unread_returns": unread,
            "current_obligations": {
                "scientific": _matching_sections(now, "obligation", omitted),
                "institutional": institutional,
            },
            "remaining_budget": budget,
            "blocked_reasons": _blocked_reasons(
                semantic_blockers,
                pending,
                authority,
                budget,
            ),
            "authority": authority,
            "warnings": warnings,
            "omitted": omitted,
        }
        _fit(packet, max_chars)
        if (
            read_repository_snapshot(self.canonical_repository) != canonical
            or read_repository_snapshot(self.candidate_repository) != candidate
        ):
            raise ValueError("repository snapshot changed while building attention")
        return packet

    @staticmethod
    def render_text(packet: dict[str, object]) -> str:
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
            f"max_chars must be {MIN_ATTENTION_MAX_CHARS} through {MAX_ATTENTION_MAX_CHARS}"
        )


def _require_workspace(repository: RepositoryBinding) -> None:
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
            f"workspace is not initialized; run `aros start` at the Git root: {repository.root}"
        ) from error


def _candidate_state(
    repository: RepositoryBinding,
    warnings: list[str],
    omitted: dict[str, int],
) -> dict[str, object]:
    try:
        status = read_candidate_status(repository)
        dirty = status.get("dirty_paths")
        if isinstance(dirty, list):
            status["dirty_paths"] = bound_items(
                dirty,
                _MAX_DIRTY_PATHS,
                "git status",
                omitted,
            )
    except (OSError, RuntimeError, ValueError, WorktreeError):
        _warn(warnings, "candidate_status_unavailable")
        status = {"state": "unavailable"}
    try:
        worktrees = {
            "state": "available",
            "items": bound_items(
                [dict(item) for item in read_worktree_inventory(repository)],
                _MAX_WORKTREES,
                "worktrees",
                omitted,
            ),
        }
    except (OSError, RuntimeError, ValueError, WorktreeError):
        _warn(warnings, "worktree_inventory_unavailable")
        worktrees = {"state": "unavailable", "items": []}
    return {"git_status": status, "worktrees": worktrees}


def _load_document(
    repository: RepositoryBinding,
    head: object,
    path: str,
    warnings: list[str],
) -> _Document | None:
    if not isinstance(head, str):
        _warn(warnings, f"semantic_view_unavailable:{path}")
        return None
    try:
        raw_entry = _worktrees._git_bytes(
            repository,
            "--literal-pathspecs",
            "ls-tree",
            "-z",
            "--full-tree",
            head,
            "--",
            path,
        )
        records = [item for item in raw_entry.split(b"\0") if item]
        if len(records) != 1:
            raise ValueError("missing semantic path")
        header, separator, raw_path = records[0].partition(b"\t")
        fields = header.split(b" ")
        if (
            separator != b"\t"
            or raw_path != path.encode()
            or len(fields) != 3
            or fields[0] not in {b"100644", b"100755"}
            or fields[1] != b"blob"
        ):
            raise ValueError("semantic path is not a regular blob")
        blob_oid = fields[2].decode("ascii")
        raw = _worktrees._git_bytes(repository, "cat-file", "blob", blob_oid)
        semantic = parse_semantic_document_bytes(path, raw)
    except (ResearchFileError, UnicodeError, ValueError, WorktreeError):
        _warn(warnings, f"semantic_view_unavailable:{path}")
        return None
    return _Document(semantic, hashlib.sha256(raw).hexdigest(), blob_oid)


def _active_question(
    repository: RepositoryBinding,
    head: object,
    frontier: _Document | None,
    warnings: list[str],
    omitted: dict[str, int],
) -> tuple[dict[str, object] | None, _Document | None]:
    if frontier is None:
        return None, None
    focus = frontier.frontmatter.get("focus_question")
    if focus in {None, ""}:
        return None, None
    if not isinstance(focus, str) or _QUESTION_ID.fullmatch(focus) is None:
        _warn(warnings, "malformed_focus_question")
        return None, None
    path = f"questions/{focus}/question.md"
    question = _load_document(repository, head, path, warnings)
    sections = (
        [
            _section(question, heading, omitted)
            for heading in _QUESTION_HEADINGS
            if heading in question.sections
        ]
        if question is not None
        else []
    )
    return {
        "id": focus,
        "path": path,
        "content_sha256": question.content_sha256 if question else None,
        "sections": sections,
    }, question


def _uncertainty(
    *documents: _Document | None,
    warnings: list[str],
    omitted: dict[str, int],
) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for document in documents:
        if document is None:
            continue
        headings = [name for name in document.sections if "uncertaint" in name.casefold()]
        if not headings:
            _warn(warnings, f"missing_uncertainty:{document.path}")
        result.extend(_section(document, heading, omitted) for heading in headings)
    return result


def _matching_sections(
    document: _Document | None,
    fragment: str,
    omitted: dict[str, int],
) -> list[dict[str, object]]:
    if document is None:
        return []
    return [
        _section(document, heading, omitted)
        for heading in document.sections
        if fragment in heading.casefold()
    ]


def _section(
    document: _Document,
    heading: str,
    omitted: dict[str, int],
) -> dict[str, object]:
    content = document.sections[heading]
    excerpt = content[:_MAX_EXCERPT_CHARS]
    if len(excerpt) < len(content):
        add_omission(omitted, f"{document.path}#{heading}")
    return {
        "path": document.path,
        "heading": heading,
        "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
        "excerpt": excerpt,
    }


def _hypotheses(
    repository: RepositoryBinding,
    head: object,
    model: _Document | None,
    warnings: list[str],
    omitted: dict[str, int],
) -> dict[str, list[dict[str, object]]]:
    leading = (
        [{"path": model.path, "content_sha256": model.content_sha256, "blob_oid": model.blob_oid}]
        if model is not None
        else []
    )
    rivals: list[dict[str, object]] = []
    if isinstance(head, str):
        try:
            raw = _worktrees._git_bytes(
                repository,
                "ls-tree",
                "-r",
                "-z",
                "--full-tree",
                head,
                "--",
                "model/rivals",
            )
            for record in (item for item in raw.split(b"\0") if item):
                header, _, raw_path = record.partition(b"\t")
                fields = header.split(b" ")
                path = raw_path.decode("utf-8")
                if len(fields) == 3 and fields[1] == b"blob" and path.endswith(".md"):
                    rivals.append({"path": path, "blob_oid": fields[2].decode("ascii")})
        except (UnicodeError, WorktreeError):
            _warn(warnings, "rival_inventory_unavailable")
    return {
        "leading": leading,
        "competing": bound_items(sorted(rivals, key=lambda item: str(item["path"])), _MAX_RIVALS, "model/rivals", omitted),
    }


def _terminal_observations(root: Path, warnings: list[str]) -> tuple[ObservationRecord, ...]:
    try:
        return ObservationCatalog(root).enumerate_terminal()
    except (ObservationError, OSError, RuntimeError):
        _warn(warnings, "terminal_observations_unavailable")
        return ()


def _runs(root: Path, warnings: list[str]) -> tuple[dict[str, object], ...]:
    try:
        return read_run_inventory(root)
    except (OSError, RuntimeError, RunError, ValueError):
        _warn(warnings, "run_inventory_unavailable")
        return ()


def _evals(root: Path, warnings: list[str]) -> tuple[dict[str, object], ...]:
    try:
        return read_eval_inventory(root)
    except (OSError, RuntimeError, EvalError, ValueError):
        _warn(warnings, "eval_inventory_unavailable")
        return ()


def _observed_git_history(
    repository: RepositoryBinding,
    warnings: list[str],
) -> tuple[set[str], list[dict[str, object]]]:
    try:
        raw = _worktrees._git_bytes(repository, "log", "--format=%H%x00%B%x00")
    except WorktreeError:
        _warn(warnings, "observed_history_unavailable")
        return set(), []
    fields = raw.rstrip(b"\n").split(b"\0")
    if fields and not fields[-1]:
        fields.pop()
    if len(fields) % 2:
        _warn(warnings, "observed_history_malformed")
        return set(), []
    observed: set[str] = set()
    recent: list[dict[str, object]] = []
    for raw_commit, raw_message in zip(fields[::2], fields[1::2], strict=True):
        try:
            commit = raw_commit.lstrip(b"\n").decode("ascii")
            message = raw_message.decode("utf-8")
        except UnicodeDecodeError:
            _warn(warnings, "observed_history_malformed")
            continue
        if _COMMIT.fullmatch(commit) is None:
            _warn(warnings, "observed_history_malformed")
            continue
        refs: list[str] = []
        for line in message.splitlines():
            if not line.startswith("AROS-Observed:"):
                continue
            try:
                refs.append(validate_observed_ref(line.split(":", 1)[1].strip()))
            except ObservedRefError:
                _warn(warnings, f"invalid_observed_trailer:{commit}")
        observed.update(refs)
        if refs and not recent:
            try:
                paths = _worktrees._git_text(
                    repository,
                    "diff-tree",
                    "--root",
                    "--no-commit-id",
                    "--name-only",
                    "-r",
                    commit,
                ).splitlines()
            except WorktreeError:
                paths = []
                _warn(warnings, f"observed_delta_unavailable:{commit}")
            recent.append(
                {
                    "commit": commit,
                    "observed_refs": sorted(set(refs)),
                    "paths": sorted(paths),
                }
            )
    return observed, recent


def _observation_item(record: ObservationRecord) -> dict[str, object]:
    return {
        "kind": record.kind,
        "ref": record.ref,
        "record_sha256": record.record_sha256,
        "candidate_commit": record.candidate_commit,
        "measurement_state": record.measurement_state,
        "versioned_paths": list(record.versioned_paths),
    }


def _pending_measurements(
    runs: tuple[dict[str, object], ...],
    evals: tuple[dict[str, object], ...],
    terminal: tuple[ObservationRecord, ...],
    omitted: dict[str, int],
) -> list[dict[str, object]]:
    terminal_paths = {path for item in terminal for path in item.versioned_paths}
    linked_runs = {str(item["run_id"]) for item in evals if item.get("run_id")}
    pending: list[dict[str, object]] = []
    for item in evals:
        eval_id = str(item["eval_id"])
        receipt = f"eval/evaluations/{eval_id}/receipt.json"
        if receipt not in terminal_paths:
            pending.append(
                {
                    "kind": "eval",
                    "ref": f".aros/evaluations/{eval_id}/request.json",
                    "evaluation_state": item.get("evaluation_state"),
                    "measurement_state": item.get("measurement_state"),
                    "process_state": item.get("referenced_process_state"),
                    "run_id": item.get("run_id"),
                    "reason": item.get("reason"),
                }
            )
    for item in runs:
        run_id = str(item["run_id"])
        final = f"runs/{run_id}/final.json"
        if final not in terminal_paths and run_id not in linked_runs:
            pending.append(
                {
                    "kind": "run",
                    "ref": f"runs/{run_id}/manifest.json",
                    "process_state": item.get("state"),
                    "reason": item.get("reason"),
                }
            )
    return bound_items(
        sorted(pending, key=lambda item: (str(item["kind"]), str(item["ref"]))),
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
            {"state": "unavailable", "enforcement_class": "unavailable"},
            {"state": "not_configured", "enforcement_class": "unavailable"},
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
    budget: dict[str, object],
) -> list[dict[str, object]]:
    blocked = [{"layer": "semantic", "ref": item} for item in semantic]
    for item in pending:
        if item.get("process_state") in {"lost", "missing"}:
            blocked.append({"layer": "operational", "ref": item["ref"], "reason": item.get("reason")})
    if authority.get("state") in {"blocked", "denied", "expired"}:
        blocked.append({"layer": "authority", "reason": authority.get("reason") or authority["state"]})
    if budget.get("state") in {"exhausted", "blocked", "denied"}:
        blocked.append({"layer": "budget", "reason": budget.get("reason") or budget["state"]})
    return blocked


def _fit(packet: dict[str, object], max_chars: int) -> None:
    try:
        fit_packet(packet, max_chars)
    except ValueError:
        recent = packet["recent_evidence_delta"]
        if not isinstance(recent, list) or not recent:
            raise
        commit = recent[0].get("commit")
        packet["recent_evidence_delta"] = [{"commit": commit}]
        try:
            fit_packet(packet, max_chars)
        except ValueError:
            packet["recent_evidence_delta"] = []
            fit_packet(packet, max_chars)


def _warn(warnings: list[str], warning: str) -> None:
    if warning not in warnings:
        warnings.append(warning)


__all__ = [
    "AttentionAuthorityContext",
    "DEFAULT_ATTENTION_MAX_CHARS",
    "MAX_ATTENTION_MAX_CHARS",
    "MIN_ATTENTION_MAX_CHARS",
    "ResearchAttentionService",
]
