"""Pure, service-owned terminal observation lineage."""

from __future__ import annotations

import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Literal

from .eval import EvalError, read_validated_eval_receipt
from .runs import (
    RunError,
    read_validated_run_final,
    read_validated_run_manifest,
)
from .store import json_sha256
from .tasks import TaskError, read_validated_task_collection
from .worktrees import WorktreeError, bind_repository


_ObservationKind = Literal[
    "task_return",
    "run_final",
    "measurement",
    "eval_outcome",
]
_TASK_REF = re.compile(
    r"^tasks/(TASK-[0-9]{8}-[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)/"
    r"collected\.json$"
)
_RUN_REF = re.compile(r"^runs/(RUN-[A-Za-z0-9][A-Za-z0-9-]*)/final\.json$")
_EVAL_REF = re.compile(
    r"^eval/evaluations/(EVAL-[0-9a-f]{64})/receipt\.json$"
)
_COMMIT = re.compile(r"^[0-9a-f]{40}$")


class ObservationError(ValueError):
    """Raised when a stable observation reference or lineage is invalid."""


@dataclass(frozen=True)
class ObservationRecord:
    """One immutable outer view of a service-validated observation."""

    ref: str
    kind: _ObservationKind
    record_sha256: str
    versioned_paths: tuple[str, ...]
    candidate_commit: str | None
    measurement_state: str | None
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


class ObservationCatalog:
    """Resolve and enumerate versioned terminal observations without writes."""

    def __init__(self, root: str | Path):
        try:
            self.root = bind_repository(root).root
        except WorktreeError as error:
            raise ObservationError(f"invalid observation workspace: {root}") from error

    def resolve(self, observation_ref: str) -> ObservationRecord:
        owner, record_id = _parse_observation_ref(observation_ref)
        self._require_plain_path(observation_ref)
        try:
            if owner == "task":
                return self._resolve_task(observation_ref, record_id)
            if owner == "run":
                return self._resolve_run(observation_ref, record_id)
            return self._resolve_eval(observation_ref, record_id)
        except ObservationError:
            raise
        except (EvalError, OSError, RunError, TaskError, ValueError) as error:
            raise ObservationError(
                f"invalid observation lineage for {observation_ref}: {error}"
            ) from error

    def enumerate_terminal(self) -> tuple[ObservationRecord, ...]:
        records = [self.resolve(ref) for ref in self._candidate_refs()]
        linked_run_finals = {
            f"runs/{record.payload['run_id']}/final.json"
            for record in records
            if record.kind in {"measurement", "eval_outcome"}
        }
        return tuple(
            record
            for record in records
            if not (
                record.kind == "run_final" and record.ref in linked_run_finals
            )
        )

    def _resolve_task(self, ref: str, task_id: str) -> ObservationRecord:
        brief_ref = f"tasks/{task_id}/brief.json"
        self._require_plain_path(brief_ref)
        collected = read_validated_task_collection(self.root, task_id)
        child_commit = collected["child_commit"]
        if not isinstance(child_commit, str) or _COMMIT.fullmatch(child_commit) is None:
            raise ObservationError(f"invalid Task candidate commit: {ref}")
        return ObservationRecord(
            ref=ref,
            kind="task_return",
            record_sha256=str(collected["collected_sha256"]),
            versioned_paths=(brief_ref, ref),
            candidate_commit=child_commit,
            measurement_state=None,
            payload=collected,
        )

    def _resolve_run(self, ref: str, run_id: str) -> ObservationRecord:
        manifest_ref = f"runs/{run_id}/manifest.json"
        self._require_plain_path(manifest_ref)
        self._require_plain_path(f".aros/receipts/{run_id}-prelaunch.json")
        manifest = read_validated_run_manifest(self.root, run_id)
        final = read_validated_run_final(self.root, run_id)
        candidate_commit = final.get("candidate_commit", manifest.get("candidate_commit"))
        if candidate_commit is not None and (
            not isinstance(candidate_commit, str)
            or _COMMIT.fullmatch(candidate_commit) is None
        ):
            raise ObservationError(f"invalid Run candidate commit: {ref}")
        return ObservationRecord(
            ref=ref,
            kind="run_final",
            record_sha256=json_sha256(final),
            versioned_paths=(manifest_ref, ref),
            candidate_commit=candidate_commit,
            measurement_state=None,
            payload=final,
        )

    def _resolve_eval(self, ref: str, eval_id: str) -> ObservationRecord:
        receipt = read_validated_eval_receipt(self.root, eval_id)
        run_id = str(receipt["run_id"])
        manifest_ref = f"runs/{run_id}/manifest.json"
        final_ref = f"runs/{run_id}/final.json"
        for path in (
            f".aros/evaluations/{eval_id}/request.json",
            f".aros/evaluations/{eval_id}/execution.json",
            f".aros/evaluations/{eval_id}/run.json",
            f".aros/receipts/{run_id}-prelaunch.json",
            f".aros/runs/{run_id}/stdout.log",
            f".aros/runs/{run_id}/stderr.log",
            manifest_ref,
            final_ref,
        ):
            self._require_plain_path(path)
        measurement_state = str(receipt["measurement_state"])
        kind: _ObservationKind = (
            "measurement"
            if measurement_state in {"valid", "underpowered"}
            else "eval_outcome"
        )
        return ObservationRecord(
            ref=ref,
            kind=kind,
            record_sha256=str(receipt["receipt_sha256"]),
            versioned_paths=(ref, manifest_ref, final_ref),
            candidate_commit=str(receipt["candidate_commit"]),
            measurement_state=measurement_state,
            payload=receipt,
        )

    def _candidate_refs(self) -> tuple[str, ...]:
        candidates: list[str] = []
        candidates.extend(
            self._records_under(
                Path("tasks"),
                re.compile(
                    r"^TASK-[0-9]{8}-[A-Za-z0-9]"
                    r"(?:[A-Za-z0-9-]*[A-Za-z0-9])?$"
                ),
                "collected.json",
                ignored_entries=frozenset({".staging"}),
            )
        )
        candidates.extend(
            self._records_under(
                Path("runs"),
                re.compile(r"^RUN-[A-Za-z0-9][A-Za-z0-9-]*$"),
                "final.json",
            )
        )
        candidates.extend(
            self._records_under(
                Path("eval/evaluations"),
                re.compile(r"^EVAL-[0-9a-f]{64}$"),
                "receipt.json",
            )
        )
        return tuple(sorted(candidates))

    def _records_under(
        self,
        relative_root: Path,
        identity: re.Pattern[str],
        filename: str,
        *,
        ignored_entries: frozenset[str] = frozenset(),
    ) -> list[str]:
        directory = self.root / relative_root
        try:
            metadata = directory.lstat()
        except FileNotFoundError:
            return []
        except OSError as error:
            raise ObservationError(
                f"unable to inspect observation directory: {relative_root.as_posix()}"
            ) from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ObservationError(
                f"observation directory has invalid identity: {relative_root.as_posix()}"
            )
        try:
            entries = sorted(directory.iterdir(), key=lambda path: path.name)
        except OSError as error:
            raise ObservationError(
                f"unable to enumerate observations: {relative_root.as_posix()}"
            ) from error
        refs: list[str] = []
        for entry in entries:
            try:
                entry_metadata = entry.lstat()
            except OSError as error:
                raise ObservationError(
                    f"unable to inspect observation identity: {entry}"
                ) from error
            if identity.fullmatch(entry.name) is None:
                if entry.name in ignored_entries:
                    continue
                if stat.S_ISLNK(entry_metadata.st_mode):
                    raise ObservationError(
                        f"terminal observation has invalid identity: {entry}"
                    )
                if stat.S_ISDIR(entry_metadata.st_mode):
                    candidate = entry / filename
                    try:
                        candidate.lstat()
                    except FileNotFoundError:
                        continue
                    except OSError as error:
                        raise ObservationError(
                            f"unable to inspect terminal observation: {candidate}"
                        ) from error
                    raise ObservationError(
                        f"terminal observation has invalid identity: {candidate}"
                    )
                continue
            if stat.S_ISLNK(entry_metadata.st_mode) or not stat.S_ISDIR(
                entry_metadata.st_mode
            ):
                raise ObservationError(f"observation identity must be a directory: {entry}")
            path = entry / filename
            try:
                path.lstat()
            except FileNotFoundError:
                continue
            except OSError as error:
                raise ObservationError(f"unable to inspect observation record: {path}") from error
            refs.append(path.relative_to(self.root).as_posix())
        return refs

    def _require_plain_path(self, relative: str) -> None:
        current = self.root
        parts = PurePosixPath(relative).parts
        for index, part in enumerate(parts):
            current /= part
            try:
                metadata = current.lstat()
            except FileNotFoundError as error:
                raise ObservationError(f"observation path does not exist: {relative}") from error
            except OSError as error:
                raise ObservationError(f"unable to inspect observation path: {relative}") from error
            if stat.S_ISLNK(metadata.st_mode):
                raise ObservationError(f"observation path must not contain a symlink: {relative}")
            if index < len(parts) - 1:
                if not stat.S_ISDIR(metadata.st_mode):
                    raise ObservationError(
                        f"observation path parent must be a directory: {relative}"
                    )
            elif not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise ObservationError(
                    f"observation path must be a single-link plain file: {relative}"
                )


def validate_task_measurement_lineage(
    task_record: ObservationRecord,
    measurement_record: ObservationRecord,
) -> None:
    """Require one Task return and measurement to name the same candidate."""
    if task_record.kind != "task_return":
        raise ObservationError("joint lineage requires a task_return observation")
    if measurement_record.kind != "measurement":
        raise ObservationError("joint lineage requires a measurement observation")
    child_commit = task_record.payload.get("child_commit")
    candidate_commit = measurement_record.payload.get("candidate_commit")
    if (
        not isinstance(child_commit, str)
        or _COMMIT.fullmatch(child_commit) is None
        or not isinstance(candidate_commit, str)
        or _COMMIT.fullmatch(candidate_commit) is None
        or child_commit != task_record.candidate_commit
        or candidate_commit != measurement_record.candidate_commit
        or child_commit != candidate_commit
    ):
        raise ObservationError(
            "Task collection and measurement candidate commit mismatch"
        )


def _parse_observation_ref(observation_ref: str) -> tuple[str, str]:
    if not isinstance(observation_ref, str):
        raise ObservationError("observation reference must be a string")
    path = PurePosixPath(observation_ref)
    if (
        not observation_ref
        or "\x00" in observation_ref
        or "\\" in observation_ref
        or path.is_absolute()
        or path.as_posix() != observation_ref
        or any(part in {".", ".."} for part in path.parts)
        or path.parts[:1] == (".aros",)
    ):
        raise ObservationError(f"invalid observation reference path: {observation_ref!r}")
    for owner, pattern in (
        ("task", _TASK_REF),
        ("run", _RUN_REF),
        ("eval", _EVAL_REF),
    ):
        match = pattern.fullmatch(observation_ref)
        if match is not None:
            return owner, match.group(1)
    raise ObservationError(f"unsupported observation reference: {observation_ref!r}")
