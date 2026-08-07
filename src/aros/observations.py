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
from .store import AnchoredReadError, AnchoredWorkspaceReader, json_sha256
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
        object.__setattr__(
            self,
            "payload",
            MappingProxyType(
                {key: _freeze_json(value) for key, value in self.payload.items()}
            ),
        )


class ObservationCatalog:
    """Resolve and enumerate versioned terminal observations without writes."""

    def __init__(self, root: str | Path):
        supplied = Path(root).expanduser()
        candidate = supplied if supplied.is_absolute() else Path.cwd() / supplied
        try:
            with AnchoredWorkspaceReader(candidate) as reader:
                repository = bind_repository(reader.root)
                reader.require_repository(
                    repository.root,
                    repository.git_dir,
                    repository.common_dir,
                )
                self.root = reader.root
        except (AnchoredReadError, OSError, WorktreeError) as error:
            raise ObservationError(f"invalid observation workspace: {root}") from error

    def resolve(
        self,
        observation_ref: str,
        *,
        reader: AnchoredWorkspaceReader | None = None,
    ) -> ObservationRecord:
        if reader is not None:
            try:
                repository = bind_repository(reader.root)
                if repository.root != self.root:
                    raise ObservationError(
                        "observation reader root differs from catalog root"
                    )
                reader.require_repository(
                    repository.root,
                    repository.git_dir,
                    repository.common_dir,
                )
                record = self._resolve_record(observation_ref, reader)
                if bind_repository(reader.root) != repository:
                    raise WorktreeError(
                        f"repository binding changed: {reader.root}"
                    )
                return record
            except ObservationError:
                raise
            except (
                AnchoredReadError,
                EvalError,
                OSError,
                RunError,
                TaskError,
                WorktreeError,
            ) as error:
                raise ObservationError(
                    f"invalid observation lineage for {observation_ref}: {error}"
                ) from error
        try:
            with AnchoredWorkspaceReader(self.root) as reader:
                repository = bind_repository(reader.root)
                reader.require_repository(
                    repository.root,
                    repository.git_dir,
                    repository.common_dir,
                )
                record = self._resolve_record(observation_ref, reader)
                if bind_repository(reader.root) != repository:
                    raise WorktreeError(
                        f"repository binding changed: {reader.root}"
                    )
                return record
        except ObservationError:
            raise
        except (
            AnchoredReadError,
            EvalError,
            OSError,
            RunError,
            TaskError,
            WorktreeError,
        ) as error:
            raise ObservationError(
                f"invalid observation lineage for {observation_ref}: {error}"
            ) from error

    def enumerate_terminal(self) -> tuple[ObservationRecord, ...]:
        try:
            with AnchoredWorkspaceReader(self.root) as reader:
                repository = bind_repository(reader.root)
                reader.require_repository(
                    repository.root,
                    repository.git_dir,
                    repository.common_dir,
                )
                records = [
                    self._resolve_record(ref, reader)
                    for ref in self._candidate_refs(reader)
                ]
                linked_run_finals = {
                    (
                        str(record.payload["run_final_ref"])
                        if record.kind == "task_return"
                        else f"runs/{record.payload['run_id']}/final.json"
                    )
                    for record in records
                    if record.kind in {"task_return", "measurement", "eval_outcome"}
                }
                result = tuple(
                    record
                    for record in records
                    if not (
                        record.kind == "run_final"
                        and record.ref in linked_run_finals
                    )
                )
                if bind_repository(reader.root) != repository:
                    raise WorktreeError(
                        f"repository binding changed: {reader.root}"
                    )
                return result
        except ObservationError:
            raise
        except (
            AnchoredReadError,
            EvalError,
            OSError,
            RunError,
            TaskError,
            WorktreeError,
        ) as error:
            raise ObservationError(f"invalid observation catalog: {error}") from error

    def _resolve_record(
        self,
        observation_ref: str,
        reader: AnchoredWorkspaceReader,
    ) -> ObservationRecord:
        owner, record_id = _parse_observation_ref(observation_ref)
        reader.require_file(observation_ref)
        if owner == "task":
            return self._resolve_task(observation_ref, record_id, reader)
        if owner == "run":
            return self._resolve_run(observation_ref, record_id, reader)
        return self._resolve_eval(observation_ref, record_id, reader)

    def _resolve_task(
        self,
        ref: str,
        task_id: str,
        reader: AnchoredWorkspaceReader,
    ) -> ObservationRecord:
        brief_ref = f"tasks/{task_id}/brief.json"
        reader.require_file(brief_ref)
        collected = read_validated_task_collection(
            self.root,
            task_id,
            reader=reader,
        )
        run_id = collected.get("run_id")
        manifest_ref = collected.get("run_manifest_ref")
        final_ref = collected.get("run_final_ref")
        if (
            not isinstance(run_id, str)
            or not isinstance(manifest_ref, str)
            or not isinstance(final_ref, str)
            or manifest_ref != f"runs/{run_id}/manifest.json"
            or final_ref != f"runs/{run_id}/final.json"
        ):
            raise ObservationError(f"invalid Task Run lineage: {ref}")
        reader.require_file(manifest_ref)
        reader.require_file(final_ref)
        manifest = read_validated_run_manifest(self.root, run_id, reader=reader)
        final = read_validated_run_final(self.root, run_id, reader=reader)
        if (
            manifest.get("run_id") != run_id
            or manifest.get("manifest_sha256")
            != collected.get("run_manifest_sha256")
            or final.get("run_id") != run_id
            or final.get("manifest_sha256")
            != collected.get("run_manifest_sha256")
            or final.get("state") != collected.get("final_state")
            or json_sha256(final) != collected.get("run_final_sha256")
        ):
            raise ObservationError(f"Task Run lineage mismatch: {ref}")
        child_commit = collected["child_commit"]
        if not isinstance(child_commit, str) or _COMMIT.fullmatch(child_commit) is None:
            raise ObservationError(f"invalid Task candidate commit: {ref}")
        return ObservationRecord(
            ref=ref,
            kind="task_return",
            record_sha256=str(collected["collected_sha256"]),
            versioned_paths=(brief_ref, manifest_ref, final_ref, ref),
            candidate_commit=child_commit,
            measurement_state=None,
            payload=collected,
        )

    def _resolve_run(
        self,
        ref: str,
        run_id: str,
        reader: AnchoredWorkspaceReader,
    ) -> ObservationRecord:
        manifest_ref = f"runs/{run_id}/manifest.json"
        reader.require_file(manifest_ref)
        reader.require_file(f".aros/receipts/{run_id}-prelaunch.json")
        manifest = read_validated_run_manifest(self.root, run_id, reader=reader)
        final = read_validated_run_final(self.root, run_id, reader=reader)
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

    def _resolve_eval(
        self,
        ref: str,
        eval_id: str,
        reader: AnchoredWorkspaceReader,
    ) -> ObservationRecord:
        receipt = read_validated_eval_receipt(self.root, eval_id, reader=reader)
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
            reader.require_file(path)
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

    def _candidate_refs(
        self,
        reader: AnchoredWorkspaceReader,
    ) -> tuple[str, ...]:
        candidates: list[str] = []
        candidates.extend(
            self._records_under(
                reader,
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
                reader,
                Path("runs"),
                re.compile(r"^RUN-[A-Za-z0-9][A-Za-z0-9-]*$"),
                "final.json",
            )
        )
        candidates.extend(
            self._records_under(
                reader,
                Path("eval/evaluations"),
                re.compile(r"^EVAL-[0-9a-f]{64}$"),
                "receipt.json",
            )
        )
        return tuple(sorted(candidates))

    def _records_under(
        self,
        reader: AnchoredWorkspaceReader,
        relative_root: Path,
        identity: re.Pattern[str],
        filename: str,
        *,
        ignored_entries: frozenset[str] = frozenset(),
    ) -> list[str]:
        parent = Path(".")
        for part in relative_root.parts:
            try:
                parent_entries = reader.listdir(parent)
            except FileNotFoundError:
                return []
            if part not in parent_entries:
                return []
            parent = parent / part
        try:
            metadata = reader.lstat(relative_root)
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
            entries = reader.listdir(relative_root)
        except OSError as error:
            raise ObservationError(
                f"unable to enumerate observations: {relative_root.as_posix()}"
            ) from error
        refs: list[str] = []
        for name in entries:
            entry = relative_root / name
            try:
                entry_metadata = reader.lstat(entry)
            except OSError as error:
                raise ObservationError(
                    f"unable to inspect observation identity: {entry}"
                ) from error
            if identity.fullmatch(name) is None:
                if name in ignored_entries:
                    continue
                if stat.S_ISLNK(entry_metadata.st_mode):
                    raise ObservationError(
                        f"terminal observation has invalid identity: {entry}"
                    )
                if stat.S_ISDIR(entry_metadata.st_mode):
                    try:
                        child_entries = reader.listdir(entry)
                    except OSError as error:
                        raise ObservationError(
                            f"unable to inspect terminal observation: {entry}"
                        ) from error
                    if filename in child_entries:
                        raise ObservationError(
                            "terminal observation has invalid identity: "
                            f"{entry / filename}"
                        )
                continue
            if stat.S_ISLNK(entry_metadata.st_mode) or not stat.S_ISDIR(
                entry_metadata.st_mode
            ):
                raise ObservationError(f"observation identity must be a directory: {entry}")
            try:
                child_entries = reader.listdir(entry)
            except OSError as error:
                raise ObservationError(
                    f"unable to inspect observation record: {entry}"
                ) from error
            if filename in child_entries:
                refs.append((entry / filename).as_posix())
        return refs

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


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    return value
