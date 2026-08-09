from __future__ import annotations

import hashlib
import os
import platform
import re
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

from .cachesim import run_child
from .calibration_evidence import (
    _R0_EVALUATOR_KEYS,
    BoundCalibration,
    _R0Input,
    _validate_r2,
    load_bound_calibration,
)
from .constraints import compare_transfer_constraints, validate_calibration
from .evidence import (
    BoundJSONObject,
    _strict_parse_json_bytes,
    cleanup_owned,
    read_bound_json_object,
    revalidate_checkout,
)
from .portfolio import (
    _MANIFEST_KEYS,
    Preflight,
    _artifact_binding,
    Run,
    _evaluate_temporal_portfolio,
    _preflight,
    _strict_object,
    _validate_trace_record,
)
from .portfolio_evidence import (
    FileBinding,
    copy_verified_input,
    file_binding,
    revalidate_file,
)
from .records import (
    HEX64,
    ParetoMeasurement,
    canonical_bytes,
    record_sha256,
    write_new_record,
)


_HEX40 = re.compile(r"[0-9a-f]{40}\Z")
_POLICY = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}\Z")
_PACKAGE_KEYS = {
    "schema_version",
    "project",
    "frozen_commit",
    "candidate_commit",
    "policy",
    "candidate_diff_sha256",
    "policy_contract_sha256",
    "claim_ref",
    "preregistration_ref",
    "review_ref",
    "principal_response_ref",
    "reproduction_ref",
    "r0_receipt_sha256",
    "r2_receipt_sha256",
    "calibration_sha256",
    "r3_commitment_sha256",
}
_REF_FIELDS = (
    "claim_ref",
    "preregistration_ref",
    "review_ref",
    "principal_response_ref",
    "reproduction_ref",
)
_PRIVATE_TRACE_FIELDS = (
    "trace_id",
    "organization",
    "application",
    "dataset",
    "provenance_url",
    "license_ref",
    "path",
    "origin_sha256",
    "sha256",
    "diagnostic_sha256",
)
_PARETO_FIELDS = (
    "rung",
    "split",
    "trace_id",
    "policy",
    "cache_fraction",
    "cache_size_bytes",
    "request_count",
    "object_miss_ratio",
    "byte_miss_ratio",
    "simulator_throughput_mqps",
    "cpu_ns_per_request",
    "metadata_bytes_per_object",
    "global_metadata_bytes",
    "metadata_measurement_sha256",
)
_FORBIDDEN_OUTCOME_KEYS = {
    "recommendation",
    "score",
    "reward",
    "objective",
    "aggregate",
    "pass",
}


class SealError(ValueError):
    pass


class LedgerConsumedError(SealError):
    def __init__(
        self,
        message: str,
        *,
        record: dict[str, object],
        intended_file_sha256: str,
    ) -> None:
        super().__init__(message)
        self.record = record
        self.intended_file_sha256 = intended_file_sha256
        self.consumed = True


def _authority_id(package: Mapping[str, object]) -> str:
    raw = (
        str(package["frozen_commit"])
        + str(package["r3_commitment_sha256"])
        + str(package["candidate_commit"])
        + "/"
        + str(package["policy"])
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _canonical_authority_paths(
    package: Mapping[str, object], frozen_package: Path
) -> tuple[Path, Path]:
    authority = _authority_id(package)
    parent = Path(frozen_package).absolute().parent.resolve(strict=True)
    return (
        parent / f"r3-{authority}.consumed.json",
        parent / f"r3-{authority}.receipt.json",
    )


@dataclass(frozen=True)
class ProjectBinding:
    path: Path
    head: str
    tree: str


@dataclass(frozen=True)
class StickyBinding:
    file: FileBinding
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True)
class PrivateInputSnapshot:
    root: Path
    root_identity: tuple[int, int]
    manifest: dict[str, object]
    manifest_binding: FileBinding
    trace_bindings: tuple[FileBinding, ...]
    source_manifest: StickyBinding
    source_traces: tuple[StickyBinding, ...]
    source_receipt: FileBinding | None = None
    r0_receipt: FileBinding | None = None
    r0_root: Path | None = None
    r0_artifacts: dict[str, FileBinding] | None = None
    r0_files: dict[str, FileBinding] | None = None
    source_receipt_original: StickyBinding | None = None
    r0_originals: tuple[StickyBinding, ...] = ()


@dataclass(frozen=True)
class FrozenInputs:
    package: dict[str, object]
    package_binding: FileBinding
    project: ProjectBinding
    ref_sha256s: dict[str, str]
    host_manifest: dict[str, object]
    host_binding: FileBinding
    calibration: BoundCalibration
    reproduction_r2: object
    preflight: Preflight
    trace_bindings: tuple[FileBinding, ...]
    contract: dict[str, object]
    contract_binding: FileBinding
    r3_evaluator_bindings: dict[str, FileBinding]
    private_snapshot: PrivateInputSnapshot
    authority_id: str
    ledger: Path
    final_receipt: Path
    output: Path


def _hash(value: object, label: str, *, sha1: bool = False) -> str:
    pattern = _HEX40 if sha1 else HEX64
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise SealError(f"{label} must be a lowercase cryptographic hash")
    return value


def _string(value: object, label: str) -> str:
    if type(value) is not str or not value or "\x00" in value:
        raise SealError(f"{label} must be a nonempty string")
    return value


def load_frozen_package(value: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != _PACKAGE_KEYS:
        raise SealError("frozen package keys mismatch")
    if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise SealError("frozen package schema_version must be integer 1")
    project = Path(_string(value.get("project"), "project"))
    if not project.is_absolute():
        raise SealError("project must be an absolute task root")
    _hash(value.get("frozen_commit"), "frozen commit", sha1=True)
    _hash(value.get("candidate_commit"), "candidate commit", sha1=True)
    policy = _string(value.get("policy"), "policy")
    if _POLICY.fullmatch(policy) is None:
        raise SealError("policy is not a safe identifier")
    for name in (
        "candidate_diff_sha256",
        "policy_contract_sha256",
        "r0_receipt_sha256",
        "r2_receipt_sha256",
        "calibration_sha256",
        "r3_commitment_sha256",
    ):
        _hash(value.get(name), name.replace("_", " "))
    for name in _REF_FIELDS:
        raw = _string(value.get(name), name)
        reference = PurePosixPath(raw)
        if (
            reference.is_absolute()
            or ".." in reference.parts
            or raw != reference.as_posix()
        ):
            raise SealError(f"{name} must be a safe relative Git reference")
    return dict(value)


def _binding(bound: BoundJSONObject) -> FileBinding:
    return FileBinding(
        bound.path,
        bound.identity,
        bound.size_bytes,
        bound.sha256,
        bound.mode,
    )


def _git(project: Path, *argv: str, binary: bool = False) -> str | bytes:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    environment["GIT_NO_LAZY_FETCH"] = "1"
    result = subprocess.run(
        [
            "git",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.untrackedCache=false",
            "-c",
            "core.fileMode=true",
            "-c",
            "core.trustctime=true",
            *argv,
        ],
        cwd=project,
        capture_output=True,
        check=False,
        env=environment,
    )
    if result.returncode != 0:
        raise SealError(f"Git validation failed: {' '.join(argv)}")
    if binary:
        return result.stdout
    try:
        return result.stdout.decode("utf-8").strip()
    except UnicodeError as error:
        raise SealError("Git validation returned non-UTF-8 text") from error


def _project_binding(project_value: str | Path, frozen_commit: str) -> ProjectBinding:
    raw = Path(project_value)
    try:
        metadata = raw.lstat()
        project = raw.resolve(strict=True)
    except OSError as error:
        raise SealError("project task root is unavailable") from error
    if (
        raw.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or str(project) != str(raw)
    ):
        raise SealError("project must name its canonical real task root")
    top = _git(project, "rev-parse", "--show-toplevel")
    head = _git(project, "rev-parse", "HEAD")
    tree = _git(project, "rev-parse", "HEAD^{tree}")
    status = _git(
        project,
        "status",
        "--porcelain=v2",
        "--untracked-files=all",
        "--ignore-submodules=none",
    )
    if top != str(project):
        raise SealError("project must be the Git task root")
    if head != frozen_commit:
        raise SealError("project HEAD differs from frozen_commit")
    if status:
        raise SealError("project Git worktree is not clean")
    return ProjectBinding(project, str(head), str(tree))


def _git_refs(
    project: ProjectBinding,
    package: Mapping[str, object],
) -> tuple[dict[str, bytes], dict[str, str]]:
    raw_by_field: dict[str, bytes] = {}
    hashes: dict[str, str] = {}
    for field in _REF_FIELDS:
        reference = str(package[field])
        listing = _git(
            project.path,
            "ls-tree",
            "-z",
            project.head,
            "--",
            reference,
            binary=True,
        )
        assert isinstance(listing, bytes)
        records = [item for item in listing.split(b"\0") if item]
        if len(records) != 1:
            raise SealError(f"{field} is not present at frozen_commit")
        try:
            metadata, raw_path = records[0].split(b"\t", 1)
            mode, kind, oid = metadata.split(b" ", 2)
        except ValueError as error:
            raise SealError(f"{field} Git entry is malformed") from error
        if (
            mode not in {b"100644", b"100755"}
            or kind != b"blob"
            or raw_path != os.fsencode(reference)
        ):
            raise SealError(f"{field} must be a regular Git blob")
        raw = _git(project.path, "cat-file", "blob", oid.decode("ascii"), binary=True)
        assert isinstance(raw, bytes)
        raw_by_field[field] = raw
        hashes[field] = hashlib.sha256(raw).hexdigest()
    return raw_by_field, hashes


def _require_frozen_worktree_blob(
    project: ProjectBinding, path: Path, binding: FileBinding, label: str
) -> None:
    try:
        relative = binding.path.relative_to(project.path).as_posix()
    except ValueError as error:
        raise SealError(f"{label} must be inside the frozen project") from error
    listing = _git(
        project.path,
        "ls-tree",
        "-z",
        project.head,
        "--",
        relative,
        binary=True,
    )
    assert isinstance(listing, bytes)
    records = [item for item in listing.split(b"\0") if item]
    if len(records) != 1:
        raise SealError(f"{label} is not present at frozen_commit")
    try:
        metadata, raw_path = records[0].split(b"\t", 1)
        mode, kind, oid = metadata.split(b" ", 2)
    except ValueError as error:
        raise SealError(f"{label} Git entry is malformed") from error
    raw = _git(project.path, "cat-file", "blob", oid.decode("ascii"), binary=True)
    assert isinstance(raw, bytes)
    if (
        mode not in {b"100644", b"100755"}
        or kind != b"blob"
        or raw_path != os.fsencode(relative)
        or hashlib.sha256(raw).hexdigest() != binding.sha256
        or len(raw) != binding.size_bytes
    ):
        raise SealError(f"{label} differs from its frozen Git blob")


def _validate_reproduction(raw: bytes, expected_r2: str) -> Mapping[str, object]:
    try:
        value = _strict_parse_json_bytes(raw, decimal_numbers=False)
    except (UnicodeError, TypeError, ValueError) as error:
        raise SealError("reproduction_ref must be a JSON Git blob") from error
    if not isinstance(value, dict):
        raise SealError("reproduction_ref must contain a JSON object")
    if set(value) != {
        "schema_version",
        "r2_receipt_path",
        "r2_receipt_sha256",
    }:
        raise SealError("reproduction_ref descriptor keys mismatch")
    r2_path = value.get("r2_receipt_path")
    if (
        type(value.get("schema_version")) is not int
        or value.get("schema_version") != 1
        or type(r2_path) is not str
        or not Path(r2_path).is_absolute()
        or value.get("r2_receipt_sha256") != expected_r2
    ):
        raise SealError("reproduction_ref descriptor binding mismatch")
    return value


def _validate_candidate_r2(
    descriptor: Mapping[str, object],
    *,
    project: ProjectBinding,
    package: Mapping[str, object],
    preflight: Preflight,
) -> object:
    raw_r2_path = Path(str(descriptor["r2_receipt_path"]))
    r2_path = raw_r2_path.resolve(strict=True)
    if raw_r2_path.is_symlink() or str(r2_path) != str(raw_r2_path):
        raise SealError("candidate R2 receipt path is not canonical")
    _external_input(r2_path, project.path, "candidate R2 evidence")
    receipt, _binding_value, _raw = _strict_object(r2_path, "receipt_sha256")
    if receipt.get("receipt_sha256") != package["r2_receipt_sha256"]:
        raise SealError("candidate R2 receipt differs from frozen package")
    manifest_value = receipt.get("task_manifest_path")
    if type(manifest_value) is not str:
        raise SealError("candidate R2 task manifest path is invalid")
    manifest_path = Path(manifest_value)
    manifest, manifest_binding, _manifest_raw = _strict_object(
        manifest_path, "manifest_sha256"
    )
    _require_frozen_worktree_blob(
        project, manifest_path, manifest_binding, "candidate R2 task manifest"
    )
    if set(manifest) != _MANIFEST_KEYS:
        raise SealError("candidate R2 task manifest keys mismatch")
    if manifest.get("r3_commitment_sha256") != package["r3_commitment_sha256"]:
        raise SealError("candidate R2 task manifest commitment differs")
    raw_traces = manifest.get("traces")
    if not isinstance(raw_traces, list):
        raise SealError("candidate R2 task manifest traces are invalid")
    traces = tuple(
        _validate_trace_record(item, str(manifest["source_commit"]))
        for item in raw_traces
    )
    r0 = _R0Input(
        preflight.r0_binding.path,
        preflight.r0,
        preflight.r0_binding,
        preflight.source,
        preflight.artifact_bindings,
        preflight.artifact_bindings,
        {},
    )
    validated = _validate_r2(
        r2_path,
        manifest=manifest,
        manifest_binding=manifest_binding,
        traces=traces,
        r0=r0,
    )
    evaluator = {
        name: binding.sha256
        for name, binding in sorted(preflight.evaluator_bindings.items())
    }
    if (
        validated.receipt.get("receipt_sha256") != package["r2_receipt_sha256"]
        or validated.receipt.get("task_root") != str(project.path)
        or validated.receipt.get("candidate_commit") != package["candidate_commit"]
        or validated.receipt.get("policy") != package["policy"]
        or validated.receipt.get("r0_receipt_sha256")
        != package["r0_receipt_sha256"]
        or validated.receipt.get("source_receipt_sha256")
        != preflight.source.get("receipt_sha256")
        or validated.receipt.get("evaluator") != evaluator
    ):
        raise SealError("candidate R2 frozen evidence binding mismatch")
    return validated


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _validate_authority_paths(
    ledger: Path, final_receipt: Path, output: Path
) -> None:
    resolved = tuple(
        Path(path).absolute().resolve(strict=False)
        for path in (ledger, final_receipt, output)
    )
    for index, left in enumerate(resolved):
        for right in resolved[index + 1 :]:
            if _paths_overlap(left, right):
                raise SealError(
                    "ledger, final receipt, and output paths must not overlap"
                )


def _sticky_binding(binding: FileBinding) -> StickyBinding:
    metadata = binding.path.stat(follow_symlinks=False)
    if (
        stat.S_ISLNK(metadata.st_mode)
        or (metadata.st_dev, metadata.st_ino) != binding.identity
        or metadata.st_size != binding.size_bytes
    ):
        raise SealError("private source binding changed")
    return StickyBinding(binding, metadata.st_mtime_ns, metadata.st_ctime_ns)


def _revalidate_sticky(binding: StickyBinding) -> None:
    metadata = binding.file.path.stat(follow_symlinks=False)
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or (metadata.st_dev, metadata.st_ino) != binding.file.identity
        or metadata.st_size != binding.file.size_bytes
        or stat.S_IMODE(metadata.st_mode) != binding.file.mode
        or metadata.st_mtime_ns != binding.mtime_ns
        or metadata.st_ctime_ns != binding.ctime_ns
    ):
        raise SealError("private source binding changed after snapshot")


def _snapshot_private_inputs(
    *,
    authority: str,
    host_parent: Path,
    manifest: Mapping[str, object],
    manifest_binding: FileBinding,
    traces: tuple[object, ...],
) -> PrivateInputSnapshot:
    root = Path(
        tempfile.mkdtemp(prefix=f".r3-{authority}-inputs-", dir=host_parent)
    )
    metadata = root.lstat()
    root_identity = (metadata.st_dev, metadata.st_ino)
    try:
        trace_root = root / "traces"
        trace_root.mkdir(mode=0o700)
        snapshots: list[FileBinding] = []
        snapshot_records: list[dict[str, object]] = []
        sticky_traces: list[StickyBinding] = []
        for index, trace in enumerate(traces):
            record = trace.record  # type: ignore[attr-defined]
            copied = copy_verified_input(
                trace.path,  # type: ignore[attr-defined]
                trace_root / f"{index:04d}.oracleGeneral",
                expected_sha256=str(record["sha256"]),
                expected_size=int(record["size_bytes"]),
                destination_mode=0o400,
            )
            snapshots.append(copied.snapshot)
            sticky_traces.append(_sticky_binding(copied.source))
            snapshot_record = dict(record)
            snapshot_record["path"] = str(copied.snapshot.path)
            snapshot_records.append(snapshot_record)
        snapshot_manifest: dict[str, object] = {
            "schema_version": manifest["schema_version"],
            "source_commit": manifest["source_commit"],
            "cache_fractions": manifest["cache_fractions"],
            "traces": snapshot_records,
        }
        write_new_record(
            root / "r3.json", snapshot_manifest, "manifest_sha256"
        )
        snapshot_manifest_binding = file_binding(root / "r3.json")
        return PrivateInputSnapshot(
            root,
            root_identity,
            snapshot_manifest,
            snapshot_manifest_binding,
            tuple(snapshots),
            _sticky_binding(manifest_binding),
            tuple(sticky_traces),
        )
    except BaseException:
        cleanup_owned(root, root_identity)
        raise


def _snapshot_candidate_evidence(
    snapshot: PrivateInputSnapshot, preflight: Preflight
) -> tuple[PrivateInputSnapshot, Preflight]:
    source_copy = copy_verified_input(
        preflight.source_binding.path,
        snapshot.root / "candidate-evidence/source-receipt.json",
        expected_sha256=preflight.source_binding.sha256,
        expected_identity=preflight.source_binding.identity,
        expected_size=preflight.source_binding.size_bytes,
        expected_mode=preflight.source_binding.mode,
        destination_mode=0o400,
    )
    original_r0_root = preflight.r0_root
    snapshot_r0_root = snapshot.root / "candidate-evidence/r0"
    copied_by_relative: dict[Path, FileBinding] = {}
    sticky_originals: list[StickyBinding] = []
    for path in sorted(original_r0_root.rglob("*")):
        metadata = path.lstat()
        if stat.S_ISDIR(metadata.st_mode):
            continue
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise SealError("candidate R0 evidence tree contains a non-regular file")
        original = file_binding(path)
        relative = path.relative_to(original_r0_root)
        copied = copy_verified_input(
            original.path,
            snapshot_r0_root / relative,
            expected_sha256=original.sha256,
            expected_identity=original.identity,
            expected_size=original.size_bytes,
            expected_mode=original.mode,
            destination_mode=original.mode,
        )
        copied_by_relative[relative] = copied.snapshot
        sticky_originals.append(_sticky_binding(copied.source))
    try:
        r0_receipt_relative = preflight.r0_binding.path.relative_to(original_r0_root)
        r0_receipt = copied_by_relative[r0_receipt_relative]
        artifacts = {
            name: copied_by_relative[binding.path.relative_to(original_r0_root)]
            for name, binding in preflight.artifact_bindings.items()
        }
    except (KeyError, ValueError) as error:
        raise SealError("candidate R0 snapshot closure is incomplete") from error
    retained = replace(
        snapshot,
        source_receipt=source_copy.snapshot,
        r0_receipt=r0_receipt,
        r0_root=snapshot_r0_root,
        r0_artifacts=artifacts,
        r0_files={
            relative.as_posix(): binding
            for relative, binding in sorted(copied_by_relative.items())
        },
        source_receipt_original=_sticky_binding(source_copy.source),
        r0_originals=tuple(sticky_originals),
    )
    prepared = replace(
        preflight,
        source_binding=source_copy.snapshot,
        r0_binding=r0_receipt,
        r0_root=snapshot_r0_root,
        artifact_bindings=artifacts,
    )
    return retained, prepared


def _external_input(path: Path, project: Path, label: str) -> None:
    if _paths_overlap(path, project):
        raise SealError(f"{label} must be outside task_root")


def _new_path(path: Path, project: Path, label: str) -> Path:
    absolute = Path(path).absolute()
    if os.path.lexists(absolute):
        qualifier = "already consumed" if label == "ledger" else "must not preexist"
        raise SealError(f"{label} {qualifier}")
    try:
        parent_metadata = absolute.parent.lstat()
        parent = absolute.parent.resolve(strict=True)
    except OSError as error:
        raise SealError(f"{label} parent is unavailable") from error
    if absolute.parent.is_symlink() or not stat.S_ISDIR(parent_metadata.st_mode):
        raise SealError(f"{label} parent must be a real directory")
    resolved = parent / absolute.name
    _external_input(resolved, project, label)
    return resolved


def _private_identities(manifest: Mapping[str, object]) -> tuple[bytes, ...]:
    traces = manifest.get("traces")
    if not isinstance(traces, list) or not traces:
        raise SealError("host R3 manifest contains no traces")
    identities: set[bytes] = set()
    for trace in traces:
        if not isinstance(trace, dict):
            raise SealError("host R3 trace record is invalid")
        for field in _PRIVATE_TRACE_FIELDS:
            value = trace.get(field)
            if type(value) is not str or not value:
                raise SealError(f"host R3 trace {field} is invalid")
            identities.add(value.encode("utf-8"))
    return tuple(sorted(identities))


def _find_leak(raw: bytes, identities: tuple[bytes, ...]) -> bool:
    return any(identity in raw for identity in identities)


def _scan_task(
    project: ProjectBinding,
    package_raw: bytes,
    identities: tuple[bytes, ...],
    private_blobs: tuple[FileBinding, ...] = (),
) -> None:
    private_hashes = {
        (binding.sha256, binding.size_bytes) for binding in private_blobs
    }

    def exact_private_blob(raw: bytes) -> bool:
        return (hashlib.sha256(raw).hexdigest(), len(raw)) in private_hashes

    if _find_leak(package_raw, identities):
        raise SealError("frozen package leaks an R3 identity or path")
    for root, directories, files in os.walk(project.path, followlinks=False):
        directories[:] = [name for name in directories if name != ".git"]
        root_path = Path(root)
        for name in [*directories, *files]:
            relative = (root_path / name).relative_to(project.path)
            if _find_leak(os.fsencode(relative), identities):
                raise SealError("task tree leaks an R3 identity or path")
        for name in files:
            path = root_path / name
            try:
                metadata = path.lstat()
                if stat.S_ISLNK(metadata.st_mode):
                    raw = os.fsencode(os.readlink(path))
                elif stat.S_ISREG(metadata.st_mode):
                    raw = path.read_bytes()
                else:
                    continue
            except OSError as error:
                raise SealError("task tree changed during R3 leak scan") from error
            if _find_leak(raw, identities) or exact_private_blob(raw):
                raise SealError("task tree leaks an R3 identity or path")
    listing = _git(project.path, "ls-tree", "-r", "-z", project.head, binary=True)
    assert isinstance(listing, bytes)
    for record in (item for item in listing.split(b"\0") if item):
        try:
            metadata, raw_path = record.split(b"\t", 1)
            mode, kind, oid = metadata.split(b" ", 2)
        except ValueError as error:
            raise SealError("frozen Git tree entry is malformed") from error
        if _find_leak(raw_path, identities):
            raise SealError("frozen Git path leaks an R3 identity or path")
        if kind != b"blob" or mode not in {b"100644", b"100755", b"120000"}:
            continue
        raw = _git(project.path, "cat-file", "blob", oid.decode("ascii"), binary=True)
        assert isinstance(raw, bytes)
        if _find_leak(raw, identities) or exact_private_blob(raw):
            raise SealError("frozen Git blob leaks an R3 identity or path")


def _validate_calibration(
    bound: BoundCalibration,
    preflight: Preflight,
    reproduction_r2: object,
) -> None:
    validated = validate_calibration(bound.record)
    del validated
    record = bound.record
    evaluator = {
        name: binding.sha256
        for name, binding in sorted(preflight.evaluator_bindings.items())
    }
    calibration_evaluator = record.get("evaluator_sha256s")
    candidate_evaluator = preflight.r0.get("evaluator")
    if not isinstance(calibration_evaluator, dict) or not isinstance(
        candidate_evaluator, dict
    ) or set(candidate_evaluator) != _R0_EVALUATOR_KEYS:
        raise SealError("candidate/calibration evaluator projection is missing")
    for name, digest in candidate_evaluator.items():
        if (
            name not in evaluator
            or digest != evaluator[name]
            or calibration_evaluator.get(name) != digest
        ):
            raise SealError("candidate/calibration evaluator projection differs")
        artifacts = preflight.r0.get("artifact_snapshots")
        if not isinstance(artifacts, dict):
            raise SealError("candidate R0 evaluator artifacts are missing")
        artifact = _artifact_binding(
            preflight.r0_root,
            artifacts,
            f"evaluator_{name.removesuffix('_sha256')}",
        )
        if artifact.sha256 != digest:
            raise SealError("candidate R0 evaluator artifact differs")
    host = {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": sys.version,
    }
    if (
        record.get("source_receipt_sha256") != preflight.source.get("receipt_sha256")
        or record.get("source_commit") != preflight.manifest.get("source_commit")
        or calibration_evaluator != evaluator
        or record.get("host_fingerprint") != host
    ):
        raise SealError("calibration differs from the exact R3 apparatus")
    r2_receipt = reproduction_r2.receipt  # type: ignore[attr-defined]
    if (
        r2_receipt.get("task_manifest_sha256")
        != record.get("task_manifest_sha256")
        or r2_receipt.get("host") != record.get("host_fingerprint")
    ):
        raise SealError("R2 reproduction and calibration bindings differ")


def _prevalidate(
    frozen_package: Path,
    host_r3_manifest: Path,
    calibration: Path,
    calibration_sha256: str,
    source_receipt: Path,
    candidate_r0_receipt: Path,
    checkout: Path,
    ledger: Path,
    output: Path,
) -> FrozenInputs:
    private_snapshot: PrivateInputSnapshot | None = None
    preflight: Preflight | None = None
    try:
        package_bound = read_bound_json_object(frozen_package, max_bytes=1024 * 1024)
        package = load_frozen_package(package_bound.value)
        package_binding = _binding(package_bound)
        project = _project_binding(package["project"], str(package["frozen_commit"]))
        raw_package = Path(frozen_package).absolute()
        if (
            raw_package.parent.is_symlink()
            or raw_package.resolve(strict=True) != package_binding.path
            or raw_package.parent.resolve(strict=True) != raw_package.parent
        ):
            raise SealError("frozen package authority path is not canonical")
        _external_input(package_binding.path, project.path, "frozen package")
        raw_refs, ref_sha256s = _git_refs(project, package)
        reproduction = _validate_reproduction(
            raw_refs["reproduction_ref"], str(package["r2_receipt_sha256"])
        )
        host_bound = read_bound_json_object(
            host_r3_manifest, max_bytes=64 * 1024 * 1024
        )
        host_binding = _binding(host_bound)
        host = host_bound.value
        if set(host) != {
            "schema_version",
            "source_commit",
            "cache_fractions",
            "traces",
            "manifest_sha256",
        }:
            raise SealError("host R3 manifest keys mismatch")
        if type(host.get("schema_version")) is not int or host["schema_version"] != 1:
            raise SealError("host R3 manifest schema_version must be integer 1")
        host_hash = record_sha256(host, "manifest_sha256")
        if (
            host.get("manifest_sha256") != host_hash
            or host_hash != package["r3_commitment_sha256"]
        ):
            raise SealError("host R3 manifest differs from the frozen commitment")
        _external_input(host_binding.path, project.path, "host R3 manifest")
        calibration_expected = _hash(calibration_sha256, "expected calibration")
        if calibration_expected != package["calibration_sha256"]:
            raise SealError("external calibration digest differs from frozen package")
        bound_calibration = load_bound_calibration(
            calibration, expected_calibration_sha256=calibration_expected
        )
        _external_input(bound_calibration.path, project.path, "calibration")
        authority = _authority_id(package)
        canonical_ledger, final_receipt = _canonical_authority_paths(
            package, package_bound.path
        )
        supplied_ledger = Path(ledger).absolute().resolve(strict=False)
        if os.path.lexists(canonical_ledger):
            raise SealError("R3 ledger is already consumed")
        if supplied_ledger != canonical_ledger:
            raise SealError("ledger path does not match the canonical R3 authority")
        ledger_path = _new_path(canonical_ledger, project.path, "ledger")
        final_receipt = _new_path(final_receipt, project.path, "final receipt")
        output_path = _new_path(output, project.path, "output")
        _validate_authority_paths(ledger_path, final_receipt, output_path)
        raw_traces = host.get("traces")
        if not isinstance(raw_traces, list):
            raise SealError("host R3 manifest traces are invalid")
        host_traces = tuple(
            _validate_trace_record(
                item,
                str(host["source_commit"]),
                allowed_splits=frozenset({"r3"}),
            )
            for item in raw_traces
        )
        if not host_traces or len(
            {item.record["trace_id"] for item in host_traces}
        ) != len(host_traces):
            raise SealError("host R3 manifest trace IDs are invalid")
        private_snapshot = _snapshot_private_inputs(
            authority=authority,
            host_parent=ledger_path.parent,
            manifest=host,
            manifest_binding=host_binding,
            traces=host_traces,
        )
        trace_bindings = [
            sticky.file for sticky in private_snapshot.source_traces
        ]
        preflight = _preflight(
            task_root=project.path,
            task_manifest=private_snapshot.manifest_binding.path,
            checkout=checkout,
            candidate=str(package["candidate_commit"]),
            policy=str(package["policy"]),
            source_receipt=source_receipt,
            r0_receipt=candidate_r0_receipt,
            output=output_path,
            temporal_r3=True,
        )
        _external_input(preflight.source_binding.path, project.path, "source receipt")
        _external_input(preflight.r0_binding.path, project.path, "candidate R0 receipt")
        _external_input(preflight.r0_root, project.path, "candidate R0 evidence")
        if (
            type(preflight.source.get("schema_version")) is not int
            or type(preflight.r0.get("schema_version")) is not int
            or type(preflight.r0.get("receipt_version")) is not int
            or preflight.r0.get("receipt_sha256") != package["r0_receipt_sha256"]
            or preflight.r0.get("candidate_diff_sha256")
            != package["candidate_diff_sha256"]
            or preflight.r0.get("contract_sha256") != package["policy_contract_sha256"]
        ):
            raise SealError("candidate R0, diff, or contract binding mismatch")
        _git(
            preflight.checkout,
            "merge-base",
            "--is-ancestor",
            str(preflight.r0["base_commit"]),
            str(package["candidate_commit"]),
        )
        reproduction_r2 = _validate_candidate_r2(
            reproduction,
            project=project,
            package=package,
            preflight=preflight,
        )
        contract_path = preflight.checkout / "commissioning/cache_policy_contract.json"
        contract_bound = read_bound_json_object(contract_path, max_bytes=65_536)
        contract_binding = _binding(contract_bound)
        if contract_binding.sha256 != package["policy_contract_sha256"]:
            raise SealError("policy contract bytes differ from frozen package")
        for trace_binding in trace_bindings:
            _external_input(trace_binding.path, project.path, "R3 trace")
        protected_paths = (
            host_binding.path,
            bound_calibration.path,
            preflight.source_binding.path,
            preflight.r0_root,
            reproduction_r2.binding.path.parent,  # type: ignore[attr-defined]
            preflight.checkout,
            *(binding.path for binding in trace_bindings),
        )
        if any(
            _paths_overlap(new_path, protected)
            for new_path in (ledger_path, output_path)
            for protected in protected_paths
        ):
            raise SealError("ledger or output overlaps an immutable R3 input")
        observed_calibration = file_binding(
            bound_calibration.path, expected_mode=bound_calibration.mode
        )
        if (
            observed_calibration.identity != bound_calibration.identity
            or observed_calibration.size_bytes != bound_calibration.size_bytes
            or observed_calibration.sha256 != bound_calibration.file_sha256
        ):
            raise SealError("calibration path binding changed")
        identities = _private_identities(host)
        _scan_task(
            project,
            package_bound.raw,
            identities,
            (
                host_binding,
                observed_calibration,
                preflight.source_binding,
                preflight.r0_binding,
                reproduction_r2.binding,  # type: ignore[attr-defined]
                *preflight.artifact_bindings.values(),
                *trace_bindings,
            ),
        )
        _validate_calibration(bound_calibration, preflight, reproduction_r2)
        r3_evaluator_bindings = {
            "seal_sha256": file_binding(Path(__file__)),
            "constraints_sha256": file_binding(
                Path(__file__).with_name("constraints.py")
            ),
            "calibration_evidence_sha256": file_binding(
                Path(__file__).with_name("calibration_evidence.py")
            ),
            "run_aros_cache_r3_sha256": file_binding(
                Path(__file__).parents[2] / "scripts/run_aros_cache_r3.py"
            ),
        }
        revalidate_file(package_binding)
        revalidate_file(contract_binding)
        revalidate_file(preflight.source_binding)
        revalidate_file(preflight.r0_binding)
        revalidate_file(reproduction_r2.binding)  # type: ignore[attr-defined]
        for binding in [
            *preflight.artifact_bindings.values(),
            *preflight.evaluator_bindings.values(),
            private_snapshot.manifest_binding,
            *private_snapshot.trace_bindings,
            *r3_evaluator_bindings.values(),
        ]:
            revalidate_file(binding)
        revalidate_checkout(preflight.checkout, preflight.checkout_binding)
        _project_binding(project.path, project.head)
        private_snapshot, preflight = _snapshot_candidate_evidence(
            private_snapshot, preflight
        )
        _revalidate_sticky(private_snapshot.source_manifest)
        for sticky in private_snapshot.source_traces:
            _revalidate_sticky(sticky)
        assert private_snapshot.source_receipt_original is not None
        _revalidate_sticky(private_snapshot.source_receipt_original)
        for sticky in private_snapshot.r0_originals:
            _revalidate_sticky(sticky)
        _project_binding(project.path, project.head)
        return FrozenInputs(
            package,
            package_binding,
            project,
            ref_sha256s,
            host,
            host_binding,
            bound_calibration,
            reproduction_r2,
            preflight,
            tuple(trace_bindings),
            contract_bound.value,
            contract_binding,
            r3_evaluator_bindings,
            private_snapshot,
            authority,
            ledger_path,
            final_receipt,
            output_path,
        )
    except SealError:
        if preflight is not None:
            try:
                os.close(preflight.output_parent.descriptor)
            except OSError:
                pass
        if private_snapshot is not None and os.path.lexists(private_snapshot.root):
            cleanup_owned(private_snapshot.root, private_snapshot.root_identity)
        raise
    except (OSError, TypeError, ValueError, subprocess.SubprocessError) as error:
        if preflight is not None:
            try:
                os.close(preflight.output_parent.descriptor)
            except OSError:
                pass
        if private_snapshot is not None and os.path.lexists(private_snapshot.root):
            cleanup_owned(private_snapshot.root, private_snapshot.root_identity)
        raise SealError(str(error)) from error


def _fsync_parent(path: Path) -> None:
    descriptor = os.open(
        path.parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_ledger_stream(stream: object, raw: bytes) -> None:
    stream.write(raw)  # type: ignore[attr-defined]


def _consume_ledger(
    path: Path, record: dict[str, object]
) -> tuple[dict[str, object], str]:
    record["ledger_sha256"] = record_sha256(record, "ledger_sha256")
    raw = canonical_bytes(record) + b"\n"
    file_sha256 = hashlib.sha256(raw).hexdigest()
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except FileExistsError as error:
        raise SealError("R3 ledger is already consumed") from error
    try:
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                _write_ledger_stream(stream, raw)
                stream.flush()
                os.fsync(stream.fileno())
            _fsync_parent(path)
            observed = file_binding(path, expected_mode=0o600)
            if observed.size_bytes != len(raw) or observed.sha256 != file_sha256:
                raise SealError("consumed ledger changed during durable publication")
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    except BaseException as error:
        raise LedgerConsumedError(
            "R3 ledger authority was consumed before durability failed: "
            + " ".join(str(error).split())[:384],
            record=record,
            intended_file_sha256=file_sha256,
        ) from error
    return record, file_sha256


def _ledger_record(inputs: FrozenInputs) -> dict[str, object]:
    package = inputs.package
    return {
        "schema_version": 1,
        "state": "consumed",
        "authority_id": inputs.authority_id,
        "final_receipt_path": str(inputs.final_receipt),
        "requested_at_unix_ns": time.time_ns(),
        "frozen_package_file_sha256": inputs.package_binding.sha256,
        "frozen_commit": package["frozen_commit"],
        "frozen_tree": inputs.project.tree,
        "candidate_commit": package["candidate_commit"],
        "candidate_tree": inputs.preflight.checkout_binding.tree,
        "policy": package["policy"],
        "candidate_diff_sha256": package["candidate_diff_sha256"],
        "policy_contract_sha256": package["policy_contract_sha256"],
        "git_ref_sha256s": inputs.ref_sha256s,
        "host_r3_manifest_sha256": inputs.host_binding.sha256,
        "r3_commitment_sha256": package["r3_commitment_sha256"],
        "calibration_sha256": package["calibration_sha256"],
        "calibration_file_sha256": inputs.calibration.file_sha256,
        "source_receipt_sha256": inputs.preflight.source["receipt_sha256"],
        "source_receipt_file_sha256": inputs.preflight.source_binding.sha256,
        "candidate_r0_receipt_sha256": package["r0_receipt_sha256"],
        "candidate_r0_file_sha256": inputs.preflight.r0_binding.sha256,
        "r2_receipt_sha256": package["r2_receipt_sha256"],
        "r2_receipt_file_sha256": inputs.reproduction_r2.binding.sha256,  # type: ignore[attr-defined]
        "binary_sha256": inputs.preflight.artifact_bindings["release_cachesim"].sha256,
        "r3_evaluator_sha256s": {
            name: binding.sha256
            for name, binding in sorted(inputs.r3_evaluator_bindings.items())
        },
        "portfolio_evaluator_sha256s": {
            name: binding.sha256
            for name, binding in sorted(inputs.preflight.evaluator_bindings.items())
        },
        "trace_sha256s": [binding.sha256 for binding in inputs.trace_bindings],
        "private_snapshot": {
            "root": str(inputs.private_snapshot.root),
            "manifest_sha256": inputs.private_snapshot.manifest_binding.sha256,
            "trace_sha256s": [
                binding.sha256
                for binding in inputs.private_snapshot.trace_bindings
            ],
            "source_receipt_sha256": (
                inputs.private_snapshot.source_receipt.sha256
                if inputs.private_snapshot.source_receipt is not None
                else None
            ),
            "r0_receipt_sha256": (
                inputs.private_snapshot.r0_receipt.sha256
                if inputs.private_snapshot.r0_receipt is not None
                else None
            ),
            "r0_artifact_sha256s": {
                name: binding.sha256
                for name, binding in sorted(
                    (inputs.private_snapshot.r0_artifacts or {}).items()
                )
            },
            "r0_evidence_sha256s": {
                name: binding.sha256
                for name, binding in sorted(
                    (inputs.private_snapshot.r0_files or {}).items()
                )
            },
        },
    }


def _assert_factual(value: object) -> None:
    if isinstance(value, Mapping):
        forbidden = set(value) & _FORBIDDEN_OUTCOME_KEYS
        if forbidden:
            raise SealError(
                f"R3 receipt contains forbidden outcome keys: {sorted(forbidden)}"
            )
        for item in value.values():
            _assert_factual(item)
    elif isinstance(value, list):
        for item in value:
            _assert_factual(item)


def _measurement_facts(
    output: Path,
    receipt: Mapping[str, object],
    inputs: FrozenInputs,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    raw_measurements = receipt.get("measurements")
    if not isinstance(raw_measurements, list):
        raise SealError("R3 portfolio measurements are invalid")
    facts: list[dict[str, object]] = []
    constraints: list[dict[str, object]] = []
    for summary in raw_measurements:
        if not isinstance(summary, dict) or type(summary.get("path")) is not str:
            raise SealError("R3 measurement summary is invalid")
        measurement_path = output / str(summary["path"])
        bound = read_bound_json_object(measurement_path, max_bytes=64 * 1024 * 1024)
        measurement = bound.value
        digest = record_sha256(measurement, "measurement_sha256")
        if (
            digest != summary.get("measurement_sha256")
            or measurement.get("measurement_sha256") != digest
        ):
            raise SealError("R3 measurement self-hash mismatch")
        pareto = ParetoMeasurement.from_record(
            {name: measurement[name] for name in _PARETO_FIELDS}
        ).to_record()
        compared = compare_transfer_constraints(
            measurement,
            inputs.preflight.r0,
            inputs.contract,
            inputs.calibration.path,
            inputs.calibration.calibration_sha256,
            None,
        )
        facts.append(
            {
                "cell_index": summary["index"],
                "path": str(summary["path"]),
                "measurement_sha256": digest,
                "pareto": pareto,
            }
        )
        constraints.append(
            {
                "cell_index": summary["index"],
                "measurement_sha256": digest,
                "facts": compared,
            }
        )
    return facts, constraints


def _write_final_receipt(
    inputs: FrozenInputs,
    ledger: Mapping[str, object],
    ledger_file_sha256: str,
    *,
    started_at: int,
    state: str,
    portfolio_receipt: Mapping[str, object] | None,
    measurements: list[dict[str, object]],
    failures: list[dict[str, object]],
    constraints: list[dict[str, object]],
) -> dict[str, object]:
    ended_at = time.time_ns()
    receipt: dict[str, object] = {
        "schema_version": 1,
        "receipt_version": 1,
        "rung": "r3",
        "state": state,
        "authority_id": inputs.authority_id,
        "final_receipt_path": str(inputs.final_receipt),
        "frozen_commit": inputs.package["frozen_commit"],
        "candidate_commit": inputs.package["candidate_commit"],
        "candidate_tree": inputs.preflight.checkout_binding.tree,
        "policy": inputs.package["policy"],
        "output_path": str(inputs.output),
        "ledger_path": str(inputs.ledger),
        "ledger_sha256": ledger["ledger_sha256"],
        "ledger_file_sha256": ledger_file_sha256,
        "r3_commitment_sha256": inputs.package["r3_commitment_sha256"],
        "host_r3_manifest_sha256": inputs.host_binding.sha256,
        "calibration_sha256": inputs.calibration.calibration_sha256,
        "r3_evaluator_sha256s": {
            name: binding.sha256
            for name, binding in sorted(inputs.r3_evaluator_bindings.items())
        },
        "source_receipt_sha256": inputs.preflight.source["receipt_sha256"],
        "r0_receipt_sha256": inputs.package["r0_receipt_sha256"],
        "r2_receipt_sha256": inputs.package["r2_receipt_sha256"],
        "started_at_unix_ns": started_at,
        "ended_at_unix_ns": ended_at,
        "portfolio_receipt_path": (
            "receipt.json" if portfolio_receipt is not None else None
        ),
        "portfolio_receipt_sha256": (
            portfolio_receipt.get("receipt_sha256")
            if portfolio_receipt is not None
            else None
        ),
        "measurements": measurements,
        "failures": failures,
        "constraints": constraints,
    }
    _assert_factual(receipt)
    write_new_record(inputs.final_receipt, receipt, "receipt_sha256")
    return receipt


def run_r3(
    frozen_package: Path,
    host_r3_manifest: Path,
    calibration: Path,
    calibration_sha256: str,
    source_receipt: Path,
    candidate_r0_receipt: Path,
    checkout: Path,
    ledger: Path,
    output: Path,
    *,
    runner: Run = run_child,
) -> dict[str, object]:
    inputs = _prevalidate(
        frozen_package,
        host_r3_manifest,
        calibration,
        calibration_sha256,
        source_receipt,
        candidate_r0_receipt,
        checkout,
        ledger,
        output,
    )
    try:
        ledger_record, ledger_file_sha256 = _consume_ledger(
            inputs.ledger, _ledger_record(inputs)
        )
    except LedgerConsumedError as error:
        try:
            os.close(inputs.preflight.output_parent.descriptor)
        except (AttributeError, OSError):
            pass
        return _write_final_receipt(
            inputs,
            error.record,
            error.intended_file_sha256,
            started_at=time.time_ns(),
            state="process_failed",
            portfolio_receipt=None,
            measurements=[],
            failures=[
                {
                    "kind": "ledger_consumption_failure",
                    "state": "process_failed",
                    "error": " ".join(str(error).split())[:512],
                }
            ],
            constraints=[],
        )
    except BaseException:
        try:
            os.close(inputs.preflight.output_parent.descriptor)
        except (AttributeError, OSError):
            pass
        if os.path.lexists(inputs.private_snapshot.root):
            cleanup_owned(
                inputs.private_snapshot.root,
                inputs.private_snapshot.root_identity,
            )
        raise
    started_at = time.time_ns()
    portfolio_receipt: dict[str, object] | None = None
    measurements: list[dict[str, object]] = []
    constraints: list[dict[str, object]] = []
    failures: list[dict[str, object]] = []
    state = "process_failed"
    try:
        portfolio_receipt = _evaluate_temporal_portfolio(
            task_root=inputs.project.path,
            host_manifest=inputs.private_snapshot.manifest_binding.path,
            checkout=inputs.preflight.checkout,
            candidate=str(inputs.package["candidate_commit"]),
            policy=str(inputs.package["policy"]),
            source_receipt=inputs.preflight.source_binding.path,
            r0_receipt=inputs.preflight.r0_binding.path,
            output=inputs.output,
            run=runner,
            prepared=inputs.preflight,
        )
        raw_failures = portfolio_receipt.get("failures")
        if not isinstance(raw_failures, list):
            raise SealError("R3 portfolio failure list is invalid")
        failures = [dict(item) for item in raw_failures if isinstance(item, dict)]
        if len(failures) != len(raw_failures):
            raise SealError("R3 portfolio failure entry is invalid")
        measurements, constraints = _measurement_facts(
            inputs.output, portfolio_receipt, inputs
        )
        expected_cells = inputs.host_manifest.get("traces")
        assert isinstance(expected_cells, list)
        state = (
            "measured"
            if not failures and len(measurements) == len(expected_cells) * 3
            else "process_failed"
        )
        _revalidate_sticky(inputs.private_snapshot.source_manifest)
        for sticky in inputs.private_snapshot.source_traces:
            _revalidate_sticky(sticky)
        assert inputs.private_snapshot.source_receipt_original is not None
        _revalidate_sticky(inputs.private_snapshot.source_receipt_original)
        for sticky in inputs.private_snapshot.r0_originals:
            _revalidate_sticky(sticky)
        _project_binding(inputs.project.path, inputs.project.head)
    except BaseException as error:
        failures.append(
            {
                "kind": "evaluation_failure",
                "state": "process_failed",
                "error": " ".join(str(error).split())[:512],
            }
        )
        state = "process_failed"
    try:
        return _write_final_receipt(
            inputs,
            ledger_record,
            ledger_file_sha256,
            started_at=started_at,
            state=state,
            portfolio_receipt=portfolio_receipt,
            measurements=measurements,
            failures=failures,
            constraints=constraints,
        )
    except BaseException as error:
        failures.append(
            {
                "kind": "receipt_publication_failure",
                "state": "process_failed",
                "error": " ".join(str(error).split())[:512],
            }
        )
        return _write_final_receipt(
            inputs,
            ledger_record,
            ledger_file_sha256,
            started_at=started_at,
            state="process_failed",
            portfolio_receipt=portfolio_receipt,
            measurements=measurements,
            failures=failures,
            constraints=constraints,
        )
