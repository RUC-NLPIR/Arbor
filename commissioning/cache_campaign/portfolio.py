from __future__ import annotations

import hashlib
import os
import platform
import re
import secrets
import stat
import struct
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path, PurePosixPath

from .cachesim import ChildResult, parse_cachesim_output, run_child
from .diagnostics import parse_phase_probe_output, phase_probe_source
from .evidence import (
    Binding,
    capture_binding,
    checkout_path,
    cleanup_owned,
    output_path,
    publish_stage,
    regular_bytes,
    regular_identity,
    revalidate_checkout,
    stage_directory,
)
from .records import (
    HEX64,
    NewRecordReceipt,
    ParetoMeasurement,
    canonical_decimal,
    deterministic_decimal_ratio,
    load_candidate_object,
    load_object,
    quarantine_unlink,
    record_sha256,
    sha256_file,
    write_new_record,
)
from .r0_probes import probe_build_flags
from .scope import evaluate_scope


SOURCE_LOCK = load_object(Path(__file__).with_name("source.lock.json"))
Run = Callable[..., ChildResult]
_ORACLE = struct.Struct("<IQIq")
_HEX40 = re.compile(r"[0-9a-f]{40}\Z")
_FRACTIONS = (Decimal("0.01"), Decimal("0.05"), Decimal("0.10"))
_TIMEOUT_SECONDS = 3600.0
_MAX_OUTPUT_BYTES = 64 * 1024 * 1024
_MANIFEST_KEYS = {
    "schema_version",
    "source_commit",
    "cache_fractions",
    "traces",
    "r3_commitment_sha256",
    "manifest_sha256",
}
_TRACE_KEYS = {
    "trace_id",
    "split",
    "organization",
    "application",
    "dataset",
    "provenance_url",
    "license_ref",
    "path",
    "trace_type",
    "origin_sha256",
    "start_request",
    "warmup_seconds",
    "max_requests",
    "working_set_bytes",
    "sha256",
    "size_bytes",
    "diagnostic_sha256",
    "diagnostics",
}
_R0_KEYS = {
    "schema_version",
    "receipt_version",
    "rung",
    "source_receipt_path",
    "source_receipt_sha256",
    "source_receipt_file_sha256",
    "repository_url",
    "base_commit",
    "base_tree",
    "candidate_commit",
    "candidate_tree",
    "candidate_diff_sha256",
    "changed_path_sha256",
    "policy",
    "policy_source_sha256",
    "candidate_test_sha256",
    "contract_sha256",
    "binary",
    "binary_sha256",
    "binary_post_run_sha256",
    "checks",
    "scope",
    "declared_metadata",
    "measured_metadata",
    "complexity_audit",
    "synthetic_trace",
    "simulations",
    "simulator_result",
    "capacity_measurement",
    "commands",
    "artifact_snapshots",
    "probes",
    "evaluator",
    "host",
    "timings",
    "errors",
    "evidence_inventory",
    "unexpected_stage_entries",
    "receipt_sha256",
}
_FORBIDDEN_KEYS = {"score", "reward", "objective", "aggregate", "pass"}


class PortfolioError(ValueError):
    pass


class RecordCollision(PortfolioError):
    def __init__(self, path: Path) -> None:
        super().__init__(f"exclusive record collision: {path.name}")
        self.path = path


class PublicationBindingError(PortfolioError):
    def __init__(self, message: str, *, renamed: bool) -> None:
        super().__init__(message)
        self.renamed = renamed


class PhaseRunError(PortfolioError):
    def __init__(
        self,
        message: str,
        process_sha256: str | None,
        bindings: tuple[FileBinding, ...] = (),
    ) -> None:
        super().__init__(message)
        self.process_sha256 = process_sha256
        self.bindings = bindings


@dataclass(frozen=True)
class FileBinding:
    path: Path
    identity: tuple[int, int]
    size_bytes: int
    sha256: str
    mode: int


@dataclass(frozen=True)
class PublicationFileBinding:
    relative_path: str
    identity: tuple[int, int]
    mode: int
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class PublicationDirectoryBinding:
    relative_path: str
    identity: tuple[int, int]
    mode: int


@dataclass(frozen=True)
class PublicationSnapshot:
    files: tuple[PublicationFileBinding, ...]
    directories: tuple[PublicationDirectoryBinding, ...]


@dataclass(frozen=True)
class TraceFacts:
    record: Mapping[str, object]
    binding: FileBinding
    measured_requests: int
    measured_request_bytes: int


@dataclass(frozen=True)
class Preflight:
    task_root: Path
    manifest_path: Path
    manifest: dict[str, object]
    manifest_binding: FileBinding
    checkout: Path
    checkout_binding: Binding
    candidate: str
    policy: str
    source: dict[str, object]
    source_binding: FileBinding
    r0: dict[str, object]
    r0_binding: FileBinding
    r0_root: Path
    artifact_bindings: dict[str, FileBinding]
    traces: tuple[TraceFacts, ...]
    evaluator_bindings: dict[str, FileBinding]
    output: Path


@dataclass(frozen=True)
class PhaseApparatus:
    source: FileBinding
    binary: FileBinding
    compile_process: dict[str, object]
    include_flags: tuple[str, ...]
    link_flags: tuple[str, ...]
    cmake_cache_sha256: str
    compiler_path: Path
    compiler: FileBinding


@dataclass(frozen=True)
class PhaseRecordEvidence:
    record: dict[str, object]
    bindings: tuple[FileBinding, ...]


def _bounded(value: object, limit: int = 512) -> str:
    message = " ".join(str(value).split()) or value.__class__.__name__
    return message if len(message) <= limit else message[: limit - 3] + "..."


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _file_binding(path: Path, *, expected_mode: int | None = None) -> FileBinding:
    raw_path = Path(path).absolute()
    descriptor = os.open(raw_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    digest = hashlib.sha256()
    size = 0
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise PortfolioError(f"expected a regular file: {raw_path}")
        mode = stat.S_IMODE(before.st_mode)
        if expected_mode is not None and mode != expected_mode:
            raise PortfolioError(f"file mode binding mismatch: {raw_path.name}")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
                size += len(block)
        after = raw_path.stat(follow_symlinks=False)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    if (
        stat.S_ISLNK(after.st_mode)
        or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or before.st_ctime_ns != after.st_ctime_ns
        or size != before.st_size
    ):
        raise PortfolioError(f"file changed while hashing: {raw_path.name}")
    return FileBinding(
        raw_path.resolve(strict=True),
        (before.st_dev, before.st_ino),
        size,
        digest.hexdigest(),
        mode,
    )


def _revalidate_file(expected: FileBinding) -> None:
    observed = _file_binding(expected.path, expected_mode=expected.mode)
    if observed != expected:
        raise PortfolioError(f"bound file changed: {expected.path.name}")


def _require_process_output_inventory(path: Path) -> None:
    try:
        entries = set(os.listdir(path))
    except OSError as error:
        raise PortfolioError("process output directory is unavailable") from error
    if entries != {"stdout.raw", "stderr.raw"}:
        raise PortfolioError("process output inventory mismatch")


def _exact_string(value: object, label: str) -> str:
    if type(value) is not str or not value or "\x00" in value:
        raise PortfolioError(f"{label} must be a nonempty string")
    return value


def _exact_integer(value: object, label: str, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise PortfolioError(f"{label} must be an integer >= {minimum}")
    return value


def _hash(value: object, label: str, *, sha1: bool = False) -> str:
    pattern = _HEX40 if sha1 else HEX64
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise PortfolioError(f"{label} is not a lowercase cryptographic hash")
    return value


def _strict_object(path: Path, hash_field: str) -> tuple[dict[str, object], FileBinding]:
    binding = _file_binding(path)
    value = load_object(binding.path)
    digest = value.get(hash_field)
    if (
        type(digest) is not str
        or HEX64.fullmatch(digest) is None
        or digest != record_sha256(value, hash_field)
    ):
        raise PortfolioError(f"{path.name} self-hash mismatch")
    _revalidate_file(binding)
    return value, binding


def _validate_trace_record(value: object, source_commit: str) -> TraceFacts:
    del source_commit
    if not isinstance(value, dict) or set(value) != _TRACE_KEYS:
        raise PortfolioError("task trace record keys mismatch")
    split = value.get("split")
    if split not in {"dev", "visible"}:
        raise PortfolioError("Task 5 cannot access R3 trace records")
    for name in (
        "trace_id",
        "organization",
        "application",
        "dataset",
        "provenance_url",
        "license_ref",
    ):
        _exact_string(value.get(name), name)
    if value.get("trace_type") != "oracleGeneral":
        raise PortfolioError("trace_type must be oracleGeneral")
    _hash(value.get("origin_sha256"), "trace origin SHA-256")
    _hash(value.get("sha256"), "trace SHA-256")
    _hash(value.get("diagnostic_sha256"), "trace diagnostic SHA-256")
    _exact_integer(value.get("start_request"), "start_request", 0)
    warmup = _exact_integer(value.get("warmup_seconds"), "warmup_seconds", 1)
    maximum = _exact_integer(value.get("max_requests"), "max_requests", 1)
    if warmup > 2_147_483_647 or maximum > 2_147_483_647:
        raise PortfolioError("trace limits exceed the pinned CLI integer range")
    working_set = _exact_integer(value.get("working_set_bytes"), "working_set_bytes", 1)
    size_bytes = _exact_integer(value.get("size_bytes"), "size_bytes", 1)
    diagnostics = value.get("diagnostics")
    diagnostic_keys = {
        "schema_version",
        "trace_id",
        "request_count",
        "unique_object_count",
        "working_set_bytes",
        "one_hit_object_fraction",
        "one_hit_request_fraction",
        "reuse_distance",
        "diagnostic_sha256",
    }
    if not isinstance(diagnostics, dict) or set(diagnostics) != diagnostic_keys:
        raise PortfolioError("trace diagnostics must be an object")
    if diagnostics.get("diagnostic_sha256") != value.get("diagnostic_sha256"):
        raise PortfolioError("trace diagnostic hash binding mismatch")
    if diagnostics.get("diagnostic_sha256") != record_sha256(
        diagnostics, "diagnostic_sha256"
    ):
        raise PortfolioError("trace diagnostic self-hash mismatch")
    if (
        diagnostics.get("trace_id") != value.get("trace_id")
        or diagnostics.get("request_count") != maximum
        or diagnostics.get("working_set_bytes") != working_set
    ):
        raise PortfolioError("trace diagnostic facts mismatch")
    raw_path = Path(_exact_string(value.get("path"), "trace path"))
    if not raw_path.is_absolute():
        raise PortfolioError("trace path must be absolute")
    binding = _file_binding(raw_path)
    if binding.size_bytes != size_bytes or binding.sha256 != value.get("sha256"):
        raise PortfolioError("trace byte binding mismatch")
    if size_bytes != maximum * _ORACLE.size:
        raise PortfolioError("trace size does not equal max_requests records")
    raw = regular_bytes(binding.path)
    first_timestamp: int | None = None
    previous_timestamp: int | None = None
    measured_requests = 0
    measured_request_bytes = 0
    objects: dict[int, tuple[int, int]] = {}
    reuse_counts: dict[str, int] = {}
    no_next_count = 0
    start_request = int(value["start_request"])
    for index, offset in enumerate(range(0, len(raw), _ORACLE.size)):
        timestamp, object_id, object_size, next_access = _ORACLE.unpack_from(raw, offset)
        if object_size <= 0:
            raise PortfolioError("trace contains a nonpositive object size")
        if previous_timestamp is not None and timestamp < previous_timestamp:
            raise PortfolioError("trace timestamps are not monotonic")
        if first_timestamp is None:
            first_timestamp = timestamp
        previous_timestamp = timestamp
        count, first_size = objects.get(object_id, (0, object_size))
        if first_size != object_size:
            raise PortfolioError("trace changes an object's size inside a window")
        objects[object_id] = (count + 1, first_size)
        current_vtime = start_request + index + 1
        if next_access in {-1, (1 << 63) - 1}:
            no_next_count += 1
        else:
            if next_access <= current_vtime:
                raise PortfolioError("trace contains a backward next-access vtime")
            bucket = str((next_access - current_vtime).bit_length() - 1)
            reuse_counts[bucket] = reuse_counts.get(bucket, 0) + 1
        if timestamp - first_timestamp > warmup:
            measured_requests += 1
            measured_request_bytes += object_size
    if measured_requests <= 0 or measured_request_bytes <= 0:
        raise PortfolioError("trace warmup leaves no measured requests")
    observed_working_set = sum(item[1] for item in objects.values())
    one_hit = sum(item[0] == 1 for item in objects.values())
    if (
        len(objects) != diagnostics.get("unique_object_count")
        or observed_working_set != working_set
    ):
        raise PortfolioError("trace working-set diagnostics do not match bytes")
    object_fraction = diagnostics.get("one_hit_object_fraction")
    request_fraction = diagnostics.get("one_hit_request_fraction")
    if (
        not isinstance(object_fraction, dict)
        or set(object_fraction) != {"numerator", "denominator"}
        or object_fraction.get("numerator") != one_hit
        or object_fraction.get("denominator") != len(objects)
        or not isinstance(request_fraction, dict)
        or set(request_fraction) != {"numerator", "denominator"}
        or request_fraction.get("numerator") != one_hit
        or request_fraction.get("denominator") != maximum
    ):
        raise PortfolioError("trace one-hit diagnostics do not match bytes")
    reuse = diagnostics.get("reuse_distance")
    if (
        not isinstance(reuse, dict)
        or set(reuse) != {"bin_convention", "counts", "no_next_count"}
        or type(reuse.get("bin_convention")) is not str
        or not reuse["bin_convention"]
        or reuse.get("counts") != {
            key: reuse_counts[key]
            for key in sorted(reuse_counts, key=int)
        }
        or reuse.get("no_next_count") != no_next_count
    ):
        raise PortfolioError("trace reuse-distance diagnostics do not match bytes")
    _revalidate_file(binding)
    return TraceFacts(value, binding, measured_requests, measured_request_bytes)


def _artifact_binding(
    r0_root: Path, artifacts: Mapping[str, object], name: str
) -> FileBinding:
    record = artifacts.get(name)
    expected_keys = {
        "source_path",
        "source_identity",
        "size_bytes",
        "sha256",
        "snapshot_path",
        "snapshot_identity",
        "binding_intact",
    }
    if not isinstance(record, dict) or set(record) != expected_keys:
        raise PortfolioError(f"R0 {name} artifact receipt is invalid")
    expected_relative = f"artifact_snapshots/{name}"
    if record.get("snapshot_path") != expected_relative or record.get("binding_intact") is not True:
        raise PortfolioError(f"R0 {name} artifact is not an intact snapshot")
    pure = PurePosixPath(expected_relative)
    if pure.is_absolute() or ".." in pure.parts:
        raise PortfolioError("R0 artifact path escapes its receipt")
    binding = _file_binding(r0_root.joinpath(*pure.parts), expected_mode=0o400)
    identity = record.get("snapshot_identity")
    if (
        not isinstance(identity, dict)
        or set(identity) != {"device", "inode"}
        or identity.get("device") != binding.identity[0]
        or identity.get("inode") != binding.identity[1]
        or record.get("size_bytes") != binding.size_bytes
        or record.get("sha256") != binding.sha256
    ):
        raise PortfolioError(f"R0 {name} artifact snapshot binding mismatch")
    return binding


def _validate_r0(
    path: Path,
    *,
    source: Mapping[str, object],
    source_binding: FileBinding,
    checkout: Path,
    candidate: str,
    policy: str,
) -> tuple[dict[str, object], FileBinding, Path, dict[str, FileBinding]]:
    receipt, binding = _strict_object(path, "receipt_sha256")
    if set(receipt) != _R0_KEYS:
        raise PortfolioError("R0 receipt keys mismatch")
    if (
        receipt.get("schema_version") != 1
        or receipt.get("receipt_version") != 1
        or receipt.get("rung") != "r0"
        or receipt.get("candidate_commit") != candidate
        or receipt.get("policy") != policy
        or receipt.get("source_receipt_sha256") != source.get("receipt_sha256")
        or receipt.get("source_receipt_file_sha256") != source_binding.sha256
        or receipt.get("source_receipt_path") != str(source_binding.path)
        or receipt.get("repository_url") != SOURCE_LOCK.get("repository_url")
        or receipt.get("base_commit") != SOURCE_LOCK.get("commit")
        or receipt.get("base_tree") != SOURCE_LOCK.get("tree")
    ):
        raise PortfolioError("R0 source, candidate, or policy binding mismatch")
    candidate_tree = subprocess.run(
        ["git", "rev-parse", "HEAD^{tree}"],
        cwd=checkout,
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()
    if receipt.get("candidate_tree") != candidate_tree:
        raise PortfolioError("R0 candidate tree binding mismatch")
    checks = receipt.get("checks")
    if not isinstance(checks, dict) or set(checks) != {
        "source_binding",
        "evidence_binding",
        "build",
        "full_tests",
        "candidate_test",
        "sanitizer",
        "deterministic",
        "capacity",
        "metadata_probe",
    }:
        raise PortfolioError("R0 checks are missing")
    required_true = {
        "source_binding",
        "evidence_binding",
        "build",
        "full_tests",
        "sanitizer",
        "deterministic",
        "capacity",
        "metadata_probe",
    }
    if any(checks.get(name) is not True for name in required_true):
        raise PortfolioError("R0 operational checks are not successful")
    baseline = candidate == receipt.get("base_commit")
    if checks.get("candidate_test") is not (None if baseline else True):
        raise PortfolioError("R0 candidate test state is invalid")
    scope = receipt.get("scope")
    if not isinstance(scope, dict) or set(scope) != {
        "allowed_paths",
        "baseline_unchanged",
        "additive_wiring_only",
        "contract_bound",
        "changed_paths",
        "diff_sha256",
    } or any(
        scope.get(name) is not True
        for name in ("allowed_paths", "baseline_unchanged", "additive_wiring_only")
    ):
        raise PortfolioError("R0 scope facts are not successful")
    if scope.get("contract_bound") is not (None if baseline else True):
        raise PortfolioError("R0 policy contract fact is invalid")
    if baseline and (scope.get("changed_paths") != [] or receipt.get("changed_path_sha256") != {}):
        raise PortfolioError("baseline R0 records candidate changes")
    observed_scope, observed_contract = evaluate_scope(
        checkout,
        base=str(receipt["base_commit"]),
        candidate=candidate,
        policy=policy,
    )
    if (
        scope.get("allowed_paths") != observed_scope.allowed_paths
        or scope.get("baseline_unchanged") != observed_scope.baseline_unchanged
        or scope.get("additive_wiring_only") != observed_scope.additive_wiring_only
        or scope.get("contract_bound") != observed_scope.contract_bound
        or scope.get("changed_paths") != list(observed_scope.changed_paths)
        or scope.get("diff_sha256") != observed_scope.diff_sha256
        or receipt.get("candidate_diff_sha256") != observed_scope.diff_sha256
    ):
        raise PortfolioError("R0 scope receipt differs from the exact candidate")
    changed_hashes = receipt.get("changed_path_sha256")
    if not isinstance(changed_hashes, dict) or set(changed_hashes) != set(
        observed_scope.changed_paths
    ):
        raise PortfolioError("R0 changed-path hash set is invalid")
    for relative, digest in changed_hashes.items():
        _hash(digest, f"R0 changed path {relative}")
        if sha256_file(checkout / relative) != digest:
            raise PortfolioError("R0 changed-path hash differs from the candidate")
    if baseline:
        if observed_contract is not None or receipt.get("contract_sha256") is not None:
            raise PortfolioError("baseline R0 unexpectedly binds a policy contract")
    else:
        contract_sha256 = receipt.get("contract_sha256")
        _hash(contract_sha256, "R0 policy contract")
        if (
            observed_contract is None
            or sha256_file(checkout / "commissioning/cache_policy_contract.json")
            != contract_sha256
        ):
            raise PortfolioError("R0 policy contract hash differs from the candidate")
    if receipt.get("errors") != [] or receipt.get("unexpected_stage_entries") != []:
        raise PortfolioError("R0 receipt contains retained failures")
    metadata = receipt.get("measured_metadata")
    if not isinstance(metadata, dict) or set(metadata) != {
        "bytes_per_object",
        "global_bytes",
        "measurement_sha256",
        "within_budget",
    }:
        raise PortfolioError("R0 measured metadata is missing")
    try:
        bytes_per_object = Decimal(str(metadata["bytes_per_object"]))
    except Exception as error:
        raise PortfolioError("R0 metadata Decimal is invalid") from error
    if not bytes_per_object.is_finite() or bytes_per_object < 0:
        raise PortfolioError("R0 metadata Decimal is invalid")
    _exact_integer(metadata.get("global_bytes"), "R0 global metadata", 0)
    _hash(metadata.get("measurement_sha256"), "R0 metadata measurement")
    policy_source = checkout / f"libCacheSim/cache/eviction/{policy}.c"
    if sha256_file(policy_source) != receipt.get("policy_source_sha256"):
        raise PortfolioError("R0 policy source binding mismatch")
    r0_root = binding.path.parent
    from .evaluate import validate_r0_metadata_evidence

    validated_evidence = validate_r0_metadata_evidence(
        receipt,
        r0_root,
        candidate=candidate,
        policy=policy,
        source_receipt_sha256=str(source["receipt_sha256"]),
    )
    artifacts = receipt.get("artifact_snapshots")
    if not isinstance(artifacts, dict):
        raise PortfolioError("R0 artifact snapshots are missing")
    bound = {
        name: _artifact_binding(r0_root, artifacts, name)
        for name in (
            "release_cachesim",
            "release_archive",
            "release_cmake_cache",
        )
    }
    for validated in validated_evidence.files:
        observed = _file_binding(validated.path, expected_mode=validated.mode)
        if (
            observed.identity != validated.identity
            or observed.size_bytes != validated.size_bytes
            or observed.sha256 != validated.sha256
        ):
            raise PortfolioError(
                f"validated R0 evidence changed before binding: {validated.name}"
            )
        name = (
            "metadata_measurement_stdout"
            if validated == validated_evidence.stdout
            else "validated_" + validated.name.replace("/", "_")
        )
        if name in bound:
            raise PortfolioError("duplicate validated R0 evidence binding")
        bound[name] = observed
    if (
        receipt.get("binary_sha256") != bound["release_cachesim"].sha256
        or receipt.get("binary_post_run_sha256") != bound["release_cachesim"].sha256
    ):
        raise PortfolioError("R0 binary hash does not bind its retained snapshot")
    return receipt, binding, r0_root, bound


def _evaluator_bindings() -> dict[str, FileBinding]:
    paths = {
        "portfolio_sha256": Path(__file__),
        "evaluate_sha256": Path(__file__).with_name("evaluate.py"),
        "records_sha256": Path(__file__).with_name("records.py"),
        "diagnostics_sha256": Path(__file__).with_name("diagnostics.py"),
        "cachesim_sha256": Path(__file__).with_name("cachesim.py"),
        "evidence_sha256": Path(__file__).with_name("evidence.py"),
        "linux_subreaper_sha256": Path(__file__).with_name("linux_subreaper.py"),
        "r0_probes_sha256": Path(__file__).with_name("r0_probes.py"),
        "scope_sha256": Path(__file__).with_name("scope.py"),
        "source_lock_sha256": Path(__file__).with_name("source.lock.json"),
        "run_aros_cache_eval_sha256": Path(__file__).parents[2]
        / "scripts/run_aros_cache_eval.py",
    }
    return {name: _file_binding(path) for name, path in paths.items()}


def _preflight(
    *,
    task_root: Path,
    task_manifest: Path,
    checkout: Path,
    candidate: str,
    policy: str,
    source_receipt: Path,
    r0_receipt: Path,
    output: Path,
) -> Preflight:
    raw_task_root = Path(task_root).absolute()
    task_metadata = raw_task_root.lstat()
    if raw_task_root.is_symlink() or not stat.S_ISDIR(task_metadata.st_mode):
        raise PortfolioError("task_root must be a real directory")
    resolved_task_root = raw_task_root.resolve(strict=True)
    manifest, manifest_binding = _strict_object(task_manifest, "manifest_sha256")
    if set(manifest) != _MANIFEST_KEYS:
        raise PortfolioError("task manifest keys mismatch")
    if resolved_task_root not in manifest_binding.path.parents:
        raise PortfolioError("task manifest must be beneath task_root")
    if manifest.get("schema_version") != 1 or manifest.get("source_commit") != SOURCE_LOCK.get("commit"):
        raise PortfolioError("task manifest source binding mismatch")
    _hash(manifest.get("r3_commitment_sha256"), "R3 commitment")
    decimal_manifest = load_candidate_object(manifest_binding.path)
    _revalidate_file(manifest_binding)
    fractions = decimal_manifest.get("cache_fractions")
    if not isinstance(fractions, list) or tuple(fractions) != _FRACTIONS:
        raise PortfolioError("task manifest cache fractions mismatch")
    raw_traces = manifest.get("traces")
    if not isinstance(raw_traces, list):
        raise PortfolioError("task manifest traces must be an array")
    traces = tuple(
        _validate_trace_record(item, str(manifest["source_commit"]))
        for item in raw_traces
    )
    if len({item.record["trace_id"] for item in traces}) != len(traces):
        raise PortfolioError("task manifest trace IDs are not unique")
    root = checkout_path(Path(checkout))
    if _HEX40.fullmatch(candidate) is None:
        raise PortfolioError("candidate must be a lowercase SHA-1 commit")
    binding = capture_binding(root)
    if binding.head != candidate:
        raise PortfolioError("candidate checkout HEAD mismatch")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", policy) is None:
        raise PortfolioError("policy name is invalid")
    revalidate_checkout(root, binding)
    from .evaluate import _source_receipt

    source = _source_receipt(Path(source_receipt), SOURCE_LOCK)
    source_binding = _file_binding(Path(str(source["_resolved_path"])))
    if source_binding.sha256 != source.get("_file_sha256"):
        raise PortfolioError("source receipt file binding mismatch")
    r0, r0_binding, r0_root, artifacts = _validate_r0(
        Path(r0_receipt),
        source=source,
        source_binding=source_binding,
        checkout=root,
        candidate=candidate,
        policy=policy,
    )
    final = output_path(Path(output), root)
    if _paths_overlap(final, resolved_task_root):
        raise PortfolioError("portfolio output must be outside task_root")
    if _paths_overlap(final, r0_root):
        raise PortfolioError("portfolio output must be outside the R0 evidence root")
    if any(_paths_overlap(final, item.binding.path) for item in traces):
        raise PortfolioError("portfolio output must not overlap trace inputs")
    evaluator = _evaluator_bindings()
    return Preflight(
        resolved_task_root,
        manifest_binding.path,
        manifest,
        manifest_binding,
        root,
        binding,
        candidate,
        policy,
        source,
        source_binding,
        r0,
        r0_binding,
        r0_root,
        artifacts,
        traces,
        evaluator,
        final,
    )


def _revalidate_preflight(preflight: Preflight, execution: FileBinding | None) -> None:
    revalidate_checkout(preflight.checkout, preflight.checkout_binding)
    _revalidate_file(preflight.manifest_binding)
    _revalidate_file(preflight.source_binding)
    _revalidate_file(preflight.r0_binding)
    for binding in preflight.artifact_bindings.values():
        _revalidate_file(binding)
    for binding in preflight.evaluator_bindings.values():
        _revalidate_file(binding)
    for trace in preflight.traces:
        _revalidate_file(trace.binding)
    if execution is not None:
        _revalidate_file(execution)
        if execution.sha256 != preflight.artifact_bindings["release_cachesim"].sha256:
            raise PortfolioError("execution copy differs from the R0 binary snapshot")


def _copy_execution(stage: Path, source: FileBinding) -> FileBinding:
    apparatus = stage / "apparatus"
    apparatus.mkdir(mode=0o700)
    destination = apparatus / "cachesim"
    descriptor = os.open(
        destination,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o500,
    )
    try:
        raw = regular_bytes(source.path)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    destination.chmod(0o500)
    binding = _file_binding(destination, expected_mode=0o500)
    if binding.size_bytes != source.size_bytes or binding.sha256 != source.sha256:
        raise PortfolioError("execution copy differs from the R0 binary snapshot")
    return binding


def _write_raw(path: Path, raw: bytes, mode: int) -> FileBinding:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    path.chmod(mode)
    return _file_binding(path, expected_mode=mode)


def _write_owned_record(
    path: Path, value: dict[str, object], hash_field: str
) -> FileBinding:
    if os.path.lexists(path):
        raise RecordCollision(path)
    try:
        receipt = write_new_record(path, value, hash_field)
    except (OSError, ValueError) as error:
        if os.path.lexists(path):
            raise RecordCollision(path) from error
        raise
    if not isinstance(receipt, NewRecordReceipt):
        raise PortfolioError("record publisher did not return an ownership receipt")
    try:
        observed = _file_binding(path, expected_mode=receipt.mode)
    except (OSError, ValueError) as error:
        if os.path.lexists(path):
            raise RecordCollision(path) from error
        raise PortfolioError("published record disappeared before binding") from error
    if (
        observed.identity != receipt.identity
        or observed.size_bytes != receipt.size_bytes
        or observed.sha256 != receipt.sha256
    ):
        raise RecordCollision(path)
    return observed


def _revalidate_owned_record(
    path: Path, receipt: NewRecordReceipt, label: str
) -> None:
    try:
        observed = _file_binding(path, expected_mode=receipt.mode)
    except (OSError, ValueError) as error:
        if os.path.lexists(path):
            raise RecordCollision(path) from error
        raise PortfolioError(f"{label} disappeared before publication") from error
    if (
        observed.identity != receipt.identity
        or observed.size_bytes != receipt.size_bytes
        or observed.sha256 != receipt.sha256
    ):
        raise RecordCollision(path)


def _write_failure_record(
    stage: Path,
    preferred: Path,
    failure: dict[str, object],
) -> tuple[Path, FileBinding, dict[str, object]]:
    try:
        binding = _write_owned_record(preferred, failure, "failure_sha256")
        return preferred, binding, failure
    except RecordCollision:
        fallback_failure = {
            key: item for key, item in failure.items() if key != "failure_sha256"
        }
        fallback_failure["record_collision"] = {
            "path": str(preferred.relative_to(stage)),
            "state": "foreign_preserved",
        }
        for _attempt in range(16):
            path = stage / f"failure-{secrets.token_hex(16)}.json"
            try:
                binding = _write_owned_record(
                    path, fallback_failure, "failure_sha256"
                )
                return path, binding, fallback_failure
            except RecordCollision:
                continue
        raise PortfolioError("cannot allocate a private failure receipt")


def _compiler_binding(path: Path) -> FileBinding:
    raw = Path(path)
    if not raw.is_absolute():
        raise PortfolioError("compiler path must be absolute")
    try:
        resolved = raw.resolve(strict=True)
    except OSError as error:
        raise PortfolioError("compiler path is unavailable") from error
    return _file_binding(resolved)


def _revalidate_compiler(path: Path, binding: FileBinding) -> None:
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise PortfolioError("compiler path changed") from error
    if resolved != binding.path:
        raise PortfolioError("compiler path binding changed")
    _revalidate_file(binding)


def _compile_phase_probe(
    *,
    stage: Path,
    preflight: Preflight,
    execution: FileBinding,
    run: Run,
) -> PhaseApparatus:
    source_raw = phase_probe_source()
    source = _write_raw(stage / "apparatus/phase_probe.c", source_raw, 0o400)
    cmake_cache = preflight.artifact_bindings["release_cmake_cache"]
    archive = preflight.artifact_bindings["release_archive"]
    include_flags, link_flags, cmake_cache_sha256 = probe_build_flags(
        cmake_cache.path, preflight.source
    )
    compiler = preflight.source["compilers"]["c"]["path"]
    if type(compiler) is not str or not Path(compiler).is_absolute():
        raise PortfolioError("source receipt C compiler binding is invalid")
    compiler_path = Path(compiler)
    compiler_snapshot = _compiler_binding(compiler_path)
    binary_path = stage / "apparatus/phase-probe"
    argv = [
        compiler,
        "-std=c11",
        "-O2",
        "-I",
        str(preflight.checkout / "libCacheSim/include"),
        "-I",
        str(preflight.checkout / "libCacheSim/bin/cachesim"),
        *include_flags,
        "-o",
        str(binary_path),
        str(source.path),
        str(archive.path),
        *link_flags,
    ]
    _revalidate_preflight(preflight, execution)
    _revalidate_compiler(compiler_path, compiler_snapshot)
    process_output = stage / "phase-compile-process"
    result: ChildResult | None = None
    process_binding: FileBinding | None = None
    try:
        result = run(
            argv,
            process_output,
            cwd=stage,
            timeout_seconds=300.0,
            max_output_bytes=16 * 1024 * 1024,
        )
        if not isinstance(result, ChildResult) or result.argv != tuple(argv):
            raise PortfolioError("phase compiler returned an invalid process receipt")
        _require_process_output_inventory(process_output)
        _revalidate_preflight(preflight, execution)
        _revalidate_file(source)
        _revalidate_compiler(compiler_path, compiler_snapshot)
        process = _process_record(
            result,
            stage=stage,
            label="phase-compile",
            timeout_seconds=300.0,
            max_output_bytes=16 * 1024 * 1024,
        )
        process_binding = _write_owned_record(
            process_output / "process.json", process, "process_sha256"
        )
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        if not process_output.exists():
            process_output.mkdir(mode=0o700)
        process = _failed_process_record(
            label="phase-compile",
            argv=argv,
            result=result if isinstance(result, ChildResult) else None,
            error=error,
            timeout_seconds=300.0,
            max_output_bytes=16 * 1024 * 1024,
        )
        process_path = process_output / "process.json"
        if process_binding is None and not (
            isinstance(error, RecordCollision) and error.path == process_path
        ):
            try:
                _write_owned_record(process_path, process, "process_sha256")
            except RecordCollision:
                pass
        raise
    if result.returncode != 0:
        raise PortfolioError("phase probe compilation failed")
    raw_binary = _file_binding(binary_path)
    if raw_binary.mode & 0o111 == 0:
        raise PortfolioError("phase compiler did not create an executable")
    binary_path.chmod(0o500)
    binary = _file_binding(binary_path, expected_mode=0o500)
    _revalidate_preflight(preflight, execution)
    _revalidate_file(source)
    _revalidate_file(binary)
    _revalidate_compiler(compiler_path, compiler_snapshot)
    return PhaseApparatus(
        source,
        binary,
        process,
        tuple(include_flags),
        tuple(link_flags),
        cmake_cache_sha256,
        compiler_path,
        compiler_snapshot,
    )


def _phase_record(
    *,
    stage: Path,
    cell_directory: Path,
    trace: TraceFacts,
    cell: Mapping[str, object],
    policy: str,
    parsed_object_miss_ratio: Decimal,
    parsed_byte_miss_ratio: Decimal,
    apparatus: PhaseApparatus,
    run: Run,
    guard: Callable[[], None],
) -> PhaseRecordEvidence:
    argv = [
        str(apparatus.binary.path),
        str(trace.binding.path),
        policy,
        str(cell["cache_size_bytes"]),
        str(trace.record["max_requests"]),
        str(trace.record["warmup_seconds"]),
        str(trace.measured_requests),
    ]
    guard()
    _revalidate_file(apparatus.source)
    _revalidate_file(apparatus.binary)
    output = cell_directory / "phase-process"
    result: ChildResult | None = None
    process_binding: FileBinding | None = None
    owned_bindings: list[FileBinding] = []
    try:
        result = run(
            argv,
            output,
            cwd=stage,
            timeout_seconds=_TIMEOUT_SECONDS,
            max_output_bytes=_MAX_OUTPUT_BYTES,
        )
        if not isinstance(result, ChildResult) or result.argv != tuple(argv):
            raise PortfolioError("phase runner returned an invalid process receipt")
        _require_process_output_inventory(output)
        guard()
        process = _process_record(
            result,
            stage=stage,
            label="phase-probe",
            timeout_seconds=_TIMEOUT_SECONDS,
            max_output_bytes=_MAX_OUTPUT_BYTES,
        )
        owned_bindings.extend(
            (_file_binding(result.stdout_path), _file_binding(result.stderr_path))
        )
        process_binding = _write_owned_record(
            output / "process.json", process, "process_sha256"
        )
        owned_bindings.append(process_binding)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        if not output.exists():
            output.mkdir(mode=0o700)
        process = _failed_process_record(
            label="phase-probe",
            argv=argv,
            result=result if isinstance(result, ChildResult) else None,
            error=error,
            timeout_seconds=_TIMEOUT_SECONDS,
            max_output_bytes=_MAX_OUTPUT_BYTES,
        )
        process_path = output / "process.json"
        if process_binding is None and not (
            isinstance(error, RecordCollision) and error.path == process_path
        ):
            try:
                process_binding = _write_owned_record(
                    process_path, process, "process_sha256"
                )
                owned_bindings.append(process_binding)
            except RecordCollision:
                process_binding = None
        process_sha256 = (
            str(process["process_sha256"])
            if process_binding is not None and "process_sha256" in process
            else None
        )
        raise PhaseRunError(
            _bounded(error), process_sha256, tuple(owned_bindings)
        ) from error
    if result.returncode != 0:
        raise PhaseRunError(
            "phase probe process failed",
            str(process["process_sha256"]),
            tuple(owned_bindings),
        )
    try:
        bins = parse_phase_probe_output(
            regular_bytes(result.stdout_path).decode("ascii"),
            expected_request_count=trace.measured_requests,
            expected_request_bytes=trace.measured_request_bytes,
            expected_object_miss_ratio=parsed_object_miss_ratio,
            expected_byte_miss_ratio=parsed_byte_miss_ratio,
        )
    except (UnicodeError, ValueError) as error:
        raise PhaseRunError(
            _bounded(error),
            str(process["process_sha256"]),
            tuple(owned_bindings),
        ) from error
    guard()
    _revalidate_file(apparatus.source)
    _revalidate_file(apparatus.binary)
    record: dict[str, object] = {
        "schema_version": 1,
        "trace_id": trace.record["trace_id"],
        "trace_sha256": trace.binding.sha256,
        "frozen_trace_diagnostic_sha256": trace.record["diagnostic_sha256"],
        "policy": policy,
        "cache_fraction": cell["cache_fraction"],
        "cache_size_bytes": cell["cache_size_bytes"],
        "request_count": sum(item.requests for item in bins),
        "object_misses": sum(item.object_misses for item in bins),
        "request_bytes": sum(item.request_bytes for item in bins),
        "byte_misses": sum(item.byte_misses for item in bins),
        "bins": [item.to_record() for item in bins],
        "process": process,
    }
    try:
        phase_binding = _write_owned_record(
            cell_directory / "phase.json", record, "phase_sha256"
        )
    except RecordCollision as error:
        raise PhaseRunError(
            _bounded(error),
            str(process["process_sha256"]),
            tuple(owned_bindings),
        ) from error
    owned_bindings.append(phase_binding)
    return PhaseRecordEvidence(record, tuple(owned_bindings))


def _selected_traces(rung: str, traces: Sequence[TraceFacts]) -> tuple[TraceFacts, ...]:
    if rung == "r1":
        dev = tuple(item for item in traces if item.record["split"] == "dev")
        if len(dev) < 3:
            raise PortfolioError("R1 requires at least three dev traces")
        return dev[:3]
    return tuple(traces)


def _cache_size(working_set_bytes: int, fraction: Decimal) -> int:
    if type(working_set_bytes) is not int or working_set_bytes <= 0:
        raise PortfolioError("working set must be a positive integer")
    if type(fraction) is not Decimal or not fraction.is_finite() or fraction <= 0:
        raise PortfolioError("cache fraction must be a positive finite Decimal")
    numerator, denominator = fraction.as_integer_ratio()
    size = working_set_bytes * numerator // denominator
    if size <= 0:
        raise PortfolioError("cache fraction produces a nonpositive cache size")
    return size


def _cpu_ns_per_request(cpu_ns: int, request_count: int) -> Decimal:
    if type(cpu_ns) is not int or cpu_ns <= 0:
        raise PortfolioError("CPU nanoseconds must be a positive integer")
    if type(request_count) is not int or request_count <= 0:
        raise PortfolioError("request count must be a positive integer")
    return deterministic_decimal_ratio(cpu_ns, request_count)


def _write_request(path: Path, request: dict[str, object]) -> FileBinding:
    return _write_owned_record(path, request, "request_sha256")


def _retain_side_effect(
    source: Path, destination: Path, expected_raw: bytes
) -> FileBinding:
    source_binding = _file_binding(source)
    if regular_bytes(source) != expected_raw:
        raise PortfolioError("simulator side-effect output differs from stdout")
    try:
        os.link(source, destination, follow_symlinks=False)
    except FileExistsError as error:
        raise PortfolioError("refusing to replace simulator side-effect evidence") from error
    destination_binding = _file_binding(destination)
    if destination_binding != FileBinding(
        destination_binding.path,
        source_binding.identity,
        source_binding.size_bytes,
        source_binding.sha256,
        source_binding.mode,
    ):
        raise PortfolioError("simulator side-effect publication binding mismatch")
    directory = os.open(
        source.parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        quarantine_unlink(
            directory,
            source.name,
            source_binding.identity,
            sha256=source_binding.sha256,
        )
    finally:
        os.close(directory)
    _revalidate_file(destination_binding)
    return destination_binding


def _process_record(
    result: ChildResult,
    *,
    stage: Path,
    label: str,
    timeout_seconds: float,
    max_output_bytes: int,
) -> dict[str, object]:
    stdout = regular_bytes(result.stdout_path)
    stderr = regular_bytes(result.stderr_path)
    if (
        result.stdout_path != result.stderr_path.parent / "stdout.raw"
        or result.stderr_path.name != "stderr.raw"
        or len(stdout) != result.stdout_bytes
        or len(stderr) != result.stderr_bytes
        or hashlib.sha256(stdout).hexdigest() != result.stdout_sha256
        or hashlib.sha256(stderr).hexdigest() != result.stderr_sha256
    ):
        raise PortfolioError("child process raw evidence receipt mismatch")
    return {
        "label": label,
        "argv": list(result.argv),
        "timeout_seconds": timeout_seconds,
        "max_output_bytes": max_output_bytes,
        "returncode": result.returncode,
        "wall_ns": result.wall_ns,
        "cpu_ns": result.cpu_ns,
        "stdout": {
            "path": str(result.stdout_path.relative_to(stage)),
            "size_bytes": result.stdout_bytes,
            "sha256": result.stdout_sha256,
            "identity": {
                "device": regular_identity(result.stdout_path)[0],
                "inode": regular_identity(result.stdout_path)[1],
            },
        },
        "stderr": {
            "path": str(result.stderr_path.relative_to(stage)),
            "size_bytes": result.stderr_bytes,
            "sha256": result.stderr_sha256,
            "identity": {
                "device": regular_identity(result.stderr_path)[0],
                "inode": regular_identity(result.stderr_path)[1],
            },
        },
    }


def _failed_process_record(
    *,
    label: str,
    argv: Sequence[str],
    result: ChildResult | None,
    error: BaseException,
    timeout_seconds: float,
    max_output_bytes: int,
) -> dict[str, object]:
    def raw_record(path: Path | None) -> dict[str, object]:
        if path is None:
            return {"path": None, "size_bytes": None, "sha256": None}
        try:
            binding = _file_binding(path)
        except (OSError, ValueError):
            return {"path": str(path), "size_bytes": None, "sha256": None}
        return {
            "path": str(path),
            "size_bytes": binding.size_bytes,
            "sha256": binding.sha256,
            "identity": {
                "device": binding.identity[0],
                "inode": binding.identity[1],
            },
        }

    return {
        "label": label,
        "argv": list(argv),
        "timeout_seconds": timeout_seconds,
        "max_output_bytes": max_output_bytes,
        "returncode": result.returncode if result is not None else None,
        "wall_ns": result.wall_ns if result is not None else None,
        "cpu_ns": result.cpu_ns if result is not None else None,
        "error": _bounded(error),
        "stdout": raw_record(result.stdout_path if result is not None else None),
        "stderr": raw_record(result.stderr_path if result is not None else None),
    }


def _assert_no_forbidden_keys(value: object) -> None:
    if isinstance(value, dict):
        forbidden = set(value) & _FORBIDDEN_KEYS
        if forbidden:
            raise PortfolioError(f"forbidden scientific interpretation keys: {sorted(forbidden)}")
        for item in value.values():
            _assert_no_forbidden_keys(item)
    elif isinstance(value, list):
        for item in value:
            _assert_no_forbidden_keys(item)


def _inventory(stage: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for path in sorted(stage.rglob("*")):
        relative = path.relative_to(stage).as_posix()
        metadata = path.lstat()
        if stat.S_ISDIR(metadata.st_mode):
            continue
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise PortfolioError(f"unexpected non-regular evidence entry: {relative}")
        if relative == "receipt.json":
            continue
        binding = _file_binding(path)
        records.append(
            {
                "path": relative,
                "identity": {
                    "device": binding.identity[0],
                    "inode": binding.identity[1],
                },
                "mode": binding.mode,
                "size_bytes": binding.size_bytes,
                "sha256": binding.sha256,
            }
        )
    return records


def _inventory_record(stage: Path, binding: FileBinding) -> dict[str, object]:
    return {
        "path": binding.path.relative_to(stage).as_posix(),
        "identity": {
            "device": binding.identity[0],
            "inode": binding.identity[1],
        },
        "mode": binding.mode,
        "size_bytes": binding.size_bytes,
        "sha256": binding.sha256,
    }


def _tree_bindings(root: Path) -> list[FileBinding]:
    bindings: list[FileBinding] = []
    for path in sorted(root.rglob("*")):
        metadata = path.lstat()
        if stat.S_ISDIR(metadata.st_mode):
            continue
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise PortfolioError("retained evidence contains a non-regular entry")
        bindings.append(_file_binding(path))
    return bindings


def _verify_publication(stage: Path, receipt: dict[str, object]) -> None:
    if _inventory(stage) != receipt["evidence_inventory"]:
        raise PortfolioError("portfolio evidence inventory changed before publication")
    observed = load_object(stage / "receipt.json")
    if observed != receipt or receipt.get("receipt_sha256") != record_sha256(
        receipt, "receipt_sha256"
    ):
        raise PortfolioError("portfolio root receipt changed before publication")


def _publication_snapshot(
    stage: Path,
    stage_identity: tuple[int, int],
    inventory: Sequence[Mapping[str, object]],
    root_receipt: NewRecordReceipt,
) -> PublicationSnapshot:
    files: list[PublicationFileBinding] = []
    for item in inventory:
        if set(item) != {"path", "identity", "mode", "size_bytes", "sha256"}:
            raise PortfolioError("publication inventory record is invalid")
        relative = item.get("path")
        identity = item.get("identity")
        if (
            type(relative) is not str
            or not isinstance(identity, dict)
            or set(identity) != {"device", "inode"}
            or type(identity.get("device")) is not int
            or type(identity.get("inode")) is not int
            or type(item.get("mode")) is not int
            or type(item.get("size_bytes")) is not int
            or type(item.get("sha256")) is not str
            or HEX64.fullmatch(str(item["sha256"])) is None
        ):
            raise PortfolioError("publication inventory binding is invalid")
        pure = PurePosixPath(relative)
        if pure.is_absolute() or ".." in pure.parts or relative == "receipt.json":
            raise PortfolioError("publication inventory path is invalid")
        files.append(
            PublicationFileBinding(
                relative,
                (int(identity["device"]), int(identity["inode"])),
                int(item["mode"]),
                int(item["size_bytes"]),
                str(item["sha256"]),
            )
        )
    files.append(
        PublicationFileBinding(
            "receipt.json",
            root_receipt.identity,
            root_receipt.mode,
            root_receipt.size_bytes,
            root_receipt.sha256,
        )
    )
    if len({item.relative_path for item in files}) != len(files):
        raise PortfolioError("publication inventory contains duplicate files")

    root_metadata = stage.lstat()
    if (
        stage.is_symlink()
        or not stat.S_ISDIR(root_metadata.st_mode)
        or (root_metadata.st_dev, root_metadata.st_ino) != stage_identity
    ):
        raise PortfolioError("publication root directory binding changed")
    directories = [
        PublicationDirectoryBinding(
            ".",
            stage_identity,
            stat.S_IMODE(root_metadata.st_mode),
        )
    ]
    for path in sorted(stage.rglob("*")):
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise PortfolioError("publication contains a symlink")
        if stat.S_ISDIR(metadata.st_mode):
            directories.append(
                PublicationDirectoryBinding(
                    path.relative_to(stage).as_posix(),
                    (metadata.st_dev, metadata.st_ino),
                    stat.S_IMODE(metadata.st_mode),
                )
            )
    return PublicationSnapshot(
        tuple(sorted(files, key=lambda item: item.relative_path)),
        tuple(sorted(directories, key=lambda item: item.relative_path)),
    )


def _revalidate_publication_snapshot(
    root: Path, snapshot: PublicationSnapshot
) -> None:
    observed_files: set[str] = set()
    observed_directories = {"."}
    root_metadata = root.lstat()
    if root.is_symlink() or not stat.S_ISDIR(root_metadata.st_mode):
        raise PortfolioError("publication root is not a real directory")
    for path in sorted(root.rglob("*")):
        metadata = path.lstat()
        relative = path.relative_to(root).as_posix()
        if stat.S_ISLNK(metadata.st_mode):
            raise PortfolioError("publication gained a symlink")
        if stat.S_ISDIR(metadata.st_mode):
            observed_directories.add(relative)
        elif stat.S_ISREG(metadata.st_mode):
            observed_files.add(relative)
        else:
            raise PortfolioError("publication gained a non-regular entry")
    if observed_files != {item.relative_path for item in snapshot.files}:
        raise PortfolioError("publication file inventory changed")
    if observed_directories != {
        item.relative_path for item in snapshot.directories
    }:
        raise PortfolioError("publication directory inventory changed")
    for expected in snapshot.files:
        observed = _file_binding(
            root.joinpath(*PurePosixPath(expected.relative_path).parts),
            expected_mode=expected.mode,
        )
        if (
            observed.identity != expected.identity
            or observed.size_bytes != expected.size_bytes
            or observed.sha256 != expected.sha256
        ):
            raise PortfolioError(
                f"publication file binding changed: {expected.relative_path}"
            )
    for expected in snapshot.directories:
        path = root if expected.relative_path == "." else root.joinpath(
            *PurePosixPath(expected.relative_path).parts
        )
        metadata = path.lstat()
        if (
            path.is_symlink()
            or not stat.S_ISDIR(metadata.st_mode)
            or (metadata.st_dev, metadata.st_ino) != expected.identity
            or stat.S_IMODE(metadata.st_mode) != expected.mode
        ):
            raise PortfolioError(
                f"publication directory binding changed: {expected.relative_path}"
            )


def _publish_and_verify(
    *,
    stage: Path,
    stage_identity: tuple[int, int],
    output: Path,
    receipt: dict[str, object],
    inventory: Sequence[Mapping[str, object]],
    root_receipt: NewRecordReceipt,
) -> None:
    renamed = False
    try:
        _verify_publication(stage, receipt)
        _revalidate_owned_record(stage / "receipt.json", root_receipt, "root receipt")
        snapshot = _publication_snapshot(
            stage, stage_identity, inventory, root_receipt
        )
        _revalidate_publication_snapshot(stage, snapshot)
        publish_stage(stage, stage_identity, output)
        renamed = True
        _revalidate_publication_snapshot(output, snapshot)
        _revalidate_owned_record(
            output / "receipt.json", root_receipt, "published root receipt"
        )
        _revalidate_publication_snapshot(output, snapshot)
    except (OSError, ValueError) as error:
        raise PublicationBindingError(
            f"publication binding verification failed: {_bounded(error)}",
            renamed=renamed,
        ) from error


def _publish_preflight_failure(
    *,
    rung: str,
    task_root: Path,
    task_manifest: Path,
    checkout: Path,
    candidate: str,
    policy: str,
    source_receipt: Path,
    r0_receipt: Path,
    output: Path,
    error: BaseException,
    started: int,
) -> None:
    try:
        root = checkout_path(Path(checkout))
        final = output_path(Path(output), root)
        resolved_task = Path(task_root).resolve(strict=True)
        if _paths_overlap(final, resolved_task):
            return
        r0_path = Path(r0_receipt).resolve(strict=True)
        if _paths_overlap(final, r0_path.parent):
            return
        stage, stage_identity = stage_directory(final)
    except (OSError, ValueError):
        return
    published = False
    preserve_stage = False
    try:
        failure: dict[str, object] = {
            "schema_version": 1,
            "kind": "preflight_failure",
            "error": _bounded(error),
        }
        failures = stage / "failures"
        failures.mkdir(mode=0o700)
        failure_binding = _write_owned_record(
            failures / "preflight.json", failure, "failure_sha256"
        )
        _revalidate_file(failure_binding)
        inventory = [_inventory_record(stage, failure_binding)]
        receipt: dict[str, object] = {
            "schema_version": 1,
            "receipt_version": 1,
            "rung": rung,
            "task_root": str(resolved_task),
            "task_manifest_path": str(Path(task_manifest).absolute()),
            "source_receipt_path": str(Path(source_receipt).absolute()),
            "r0_receipt_path": str(Path(r0_receipt).absolute()),
            "candidate_commit": candidate,
            "policy": policy,
            "selected_cells": [],
            "measurements": [],
            "measurement_hashes": [],
            "failures": [
                {
                    "path": "failures/preflight.json",
                    "failure_sha256": failure["failure_sha256"],
                }
            ],
            "failure_hashes": [failure["failure_sha256"]],
            "provenance": {"final_binding_intact": False},
            "evidence_inventory": inventory,
            "timings": {"total_wall_ns": max(0, time.monotonic_ns() - started)},
        }
        _assert_no_forbidden_keys(receipt)
        root_receipt = write_new_record(
            stage / "receipt.json", receipt, "receipt_sha256"
        )
        if not isinstance(root_receipt, NewRecordReceipt):
            raise PortfolioError(
                "preflight root receipt publisher returned no ownership receipt"
            )
        try:
            _publish_and_verify(
                stage=stage,
                stage_identity=stage_identity,
                output=final,
                receipt=receipt,
                inventory=inventory,
                root_receipt=root_receipt,
            )
        except PublicationBindingError as publication_error:
            preserve_stage = not publication_error.renamed
            raise
        published = True
    finally:
        if not published and not preserve_stage and os.path.lexists(stage):
            cleanup_owned(stage, stage_identity)


def evaluate_portfolio(
    *,
    rung: str,
    task_root: Path,
    task_manifest: Path,
    checkout: Path,
    candidate: str,
    policy: str,
    source_receipt: Path,
    r0_receipt: Path,
    output: Path,
    run: Run = run_child,
) -> dict[str, object]:
    if rung == "r3":
        raise PortfolioError("R3 is reserved for the temporal-seal evaluator")
    if rung not in {"r1", "r2"}:
        raise PortfolioError("portfolio rung must be r1 or r2")
    started = time.monotonic_ns()
    try:
        preflight = _preflight(
            task_root=task_root,
            task_manifest=task_manifest,
            checkout=checkout,
            candidate=candidate,
            policy=policy,
            source_receipt=source_receipt,
            r0_receipt=r0_receipt,
            output=output,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        _publish_preflight_failure(
            rung=rung,
            task_root=task_root,
            task_manifest=task_manifest,
            checkout=checkout,
            candidate=candidate,
            policy=policy,
            source_receipt=source_receipt,
            r0_receipt=r0_receipt,
            output=output,
            error=error,
            started=started,
        )
        raise PortfolioError(_bounded(error)) from error
    selected_traces = _selected_traces(rung, preflight.traces)
    selected_cells = [
        {
            "index": index,
            "trace_id": trace.record["trace_id"],
            "split": trace.record["split"],
            "cache_fraction": canonical_decimal(fraction),
            "cache_size_bytes": _cache_size(int(trace.record["working_set_bytes"]), fraction),
        }
        for index, (trace, fraction) in enumerate(
            (item, fraction) for item in selected_traces for fraction in _FRACTIONS
        )
    ]
    stage, stage_identity = stage_directory(preflight.output)
    published = False
    preserve_stage = False
    try:
        execution = _copy_execution(
            stage, preflight.artifact_bindings["release_cachesim"]
        )
        _revalidate_preflight(preflight, execution)
        measurements: list[dict[str, object]] = []
        failures: list[dict[str, object]] = []
        provenance_intact = True
        phase_apparatus: PhaseApparatus | None = None
        phase_compile_failed = False
        if rung == "r2":
            try:
                phase_apparatus = _compile_phase_probe(
                    stage=stage,
                    preflight=preflight,
                    execution=execution,
                    run=run,
                )
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                phase_compile_failed = True
                compile_integrity_failure = (
                    isinstance(error, RecordCollision)
                    or "collision" in str(error).lower()
                    or "binding" in str(error).lower()
                    or "changed" in str(error).lower()
                    or "mismatch" in str(error).lower()
                    or "differs" in str(error).lower()
                )
                if compile_integrity_failure:
                    provenance_intact = False
                process_path = stage / "phase-compile-process/process.json"
                process = (
                    load_object(process_path)
                    if process_path.exists() and not isinstance(error, RecordCollision)
                    else None
                )
                failure = {
                    "schema_version": 1,
                    "kind": (
                        "binding_failure"
                        if compile_integrity_failure
                        else "process_failure"
                    ),
                    "label": "phase-compile",
                    "error": _bounded(error),
                    "remaining_cell_indices": list(range(len(selected_cells))),
                }
                if isinstance(process, dict) and type(process.get("process_sha256")) is str:
                    failure["process_sha256"] = process["process_sha256"]
                failure_path, _failure_binding, retained_failure = _write_failure_record(
                    stage,
                    stage / "phase-compile-failure.json",
                    failure,
                )
                failures.append(
                    {
                        "path": str(failure_path.relative_to(stage)),
                        "failure_sha256": retained_failure["failure_sha256"],
                    }
                )
        metadata = preflight.r0["measured_metadata"]
        assert isinstance(metadata, dict)
        (stage / "measurements").mkdir(mode=0o700)
        side_effect_root = stage / "simulator-side-effects"
        side_effect_root.mkdir(mode=0o700)
        retained_bindings: list[FileBinding] = (
            [
                *_tree_bindings(stage / "phase-compile-process"),
                *_tree_bindings(stage / "apparatus"),
            ]
            if phase_apparatus is not None
            else []
        )
        invalid_retained: set[tuple[Path, tuple[int, int]]] = set()

        def guard() -> None:
            _revalidate_preflight(preflight, execution)
            if phase_apparatus is not None:
                _revalidate_compiler(
                    phase_apparatus.compiler_path, phase_apparatus.compiler
                )
            for retained in retained_bindings:
                try:
                    _revalidate_file(retained)
                except (OSError, ValueError):
                    invalid_retained.add((retained.path, retained.identity))
                    raise

        def retain_owned(bindings: Sequence[FileBinding]) -> bool:
            intact = True
            known = {(item.path, item.identity) for item in retained_bindings}
            for owned in bindings:
                try:
                    _revalidate_file(owned)
                except (OSError, ValueError):
                    intact = False
                    invalid_retained.add((owned.path, owned.identity))
                    continue
                key = (owned.path, owned.identity)
                if key not in known:
                    retained_bindings.append(owned)
                    known.add(key)
            return intact

        for cell in (() if phase_compile_failed else selected_cells):
            trace = next(
                item
                for item in selected_traces
                if item.record["trace_id"] == cell["trace_id"]
            )
            cell_directory = stage / "measurements" / f"{cell['index']:04d}"
            private_side_effect = side_effect_root / f"{cell['index']:04d}.cachesim"
            retained_side_effect = cell_directory / "simulator.cachesim"
            argv = [
                str(execution.path),
                str(trace.binding.path),
                "oracleGeneral",
                policy,
                str(cell["cache_size_bytes"]),
                "--num-thread=1",
                f"--num-req={trace.record['max_requests']}",
                f"--warmup-sec={trace.record['warmup_seconds']}",
                "--consider-obj-metadata=true",
                "--print-head-req=false",
                f"--output={private_side_effect}",
            ]
            request: dict[str, object] = {
                "schema_version": 1,
                "rung": rung,
                "cell_index": cell["index"],
                "trace_id": cell["trace_id"],
                "split": cell["split"],
                "trace_sha256": trace.binding.sha256,
                "trace_diagnostic_sha256": trace.record["diagnostic_sha256"],
                "policy": policy,
                "cache_fraction": cell["cache_fraction"],
                "cache_size_bytes": cell["cache_size_bytes"],
                "expected_measured_requests": trace.measured_requests,
                "expected_measured_request_bytes": trace.measured_request_bytes,
                "argv": argv,
            }
            attempted = False
            result: ChildResult | None = None
            process: dict[str, object] | None = None
            request_binding: FileBinding | None = None
            process_binding: FileBinding | None = None
            current_owned: list[FileBinding] = []
            try:
                guard()
                attempted = True
                result = run(
                    argv,
                    cell_directory,
                    cwd=stage,
                    timeout_seconds=_TIMEOUT_SECONDS,
                    max_output_bytes=_MAX_OUTPUT_BYTES,
                )
                if not isinstance(result, ChildResult) or result.argv != tuple(argv):
                    raise PortfolioError("command runner returned an invalid process receipt")
                _require_process_output_inventory(cell_directory)
                guard()
                process = _process_record(
                    result,
                    stage=stage,
                    label="cachesim",
                    timeout_seconds=_TIMEOUT_SECONDS,
                    max_output_bytes=_MAX_OUTPUT_BYTES,
                )
                current_owned.extend(
                    (_file_binding(result.stdout_path), _file_binding(result.stderr_path))
                )
                request_binding = _write_request(
                    cell_directory / "request.json", request
                )
                current_owned.append(request_binding)
                process_binding = _write_owned_record(
                    cell_directory / "process.json", process, "process_sha256"
                )
                current_owned.append(process_binding)
                stdout = regular_bytes(result.stdout_path)
                side_binding = (
                    _retain_side_effect(
                        private_side_effect,
                        retained_side_effect,
                        stdout,
                    )
                    if private_side_effect.exists()
                    else None
                )
                if side_binding is not None:
                    current_owned.append(side_binding)
                if result.returncode != 0:
                    failure: dict[str, object] = {
                        "schema_version": 1,
                        "cell_index": cell["index"],
                        "kind": "process_failure",
                        "process_sha256": process["process_sha256"],
                        "returncode": result.returncode,
                    }
                    (
                        failure_path,
                        _failure_binding,
                        retained_failure,
                    ) = _write_failure_record(
                        stage,
                        cell_directory / "failure.json",
                        failure,
                    )
                    current_owned.append(_failure_binding)
                    failures.append(
                        {
                            "cell_index": cell["index"],
                            "path": str(failure_path.relative_to(stage)),
                            "failure_sha256": retained_failure["failure_sha256"],
                        }
                    )
                    guard()
                    if not retain_owned(current_owned):
                        raise PortfolioError(
                            "current-cell owned evidence changed before retention"
                        )
                    continue
                if side_binding is None:
                    raise PortfolioError("simulator side-effect output is missing")
                try:
                    parsed = parse_cachesim_output(stdout.decode("ascii"))
                except (UnicodeError, ValueError) as error:
                    raise PortfolioError(_bounded(error)) from error
                if parsed.request_count != trace.measured_requests:
                    raise PortfolioError(
                        "simulator request count does not match the warmup convention"
                    )
                guard()
                phase_evidence = (
                    _phase_record(
                        stage=stage,
                        cell_directory=cell_directory,
                        trace=trace,
                        cell=cell,
                        policy=policy,
                        parsed_object_miss_ratio=parsed.object_miss_ratio,
                        parsed_byte_miss_ratio=parsed.byte_miss_ratio,
                        apparatus=phase_apparatus,
                        run=run,
                        guard=guard,
                    )
                    if phase_apparatus is not None
                    else None
                )
                phase = phase_evidence.record if phase_evidence is not None else None
                if phase_evidence is not None:
                    current_owned.extend(phase_evidence.bindings)
                cpu_per_request = _cpu_ns_per_request(
                    result.cpu_ns, parsed.request_count
                )
                pareto = ParetoMeasurement(
                    rung=rung,  # type: ignore[arg-type]
                    split=str(cell["split"]),  # type: ignore[arg-type]
                    trace_id=str(cell["trace_id"]),
                    policy=policy,
                    cache_fraction=Decimal(str(cell["cache_fraction"])),
                    cache_size_bytes=int(cell["cache_size_bytes"]),
                    request_count=parsed.request_count,
                    object_miss_ratio=parsed.object_miss_ratio,
                    byte_miss_ratio=parsed.byte_miss_ratio,
                    simulator_throughput_mqps=parsed.simulator_throughput_mqps,
                    cpu_ns_per_request=cpu_per_request,
                    metadata_bytes_per_object=Decimal(str(metadata["bytes_per_object"])),
                    global_metadata_bytes=int(metadata["global_bytes"]),
                    metadata_measurement_sha256=str(metadata["measurement_sha256"]),
                )
                measurement: dict[str, object] = {
                    "schema_version": 1,
                    "receipt_version": 1,
                    **pareto.to_record(),
                    "trace_sha256": trace.binding.sha256,
                    "trace_diagnostic_sha256": trace.record["diagnostic_sha256"],
                    "source_receipt_sha256": preflight.source["receipt_sha256"],
                    "r0_receipt_sha256": preflight.r0["receipt_sha256"],
                    "candidate_commit": candidate,
                    "candidate_tree": preflight.checkout_binding.tree,
                    "binary_sha256": execution.sha256,
                    "evaluator": {
                        name: binding.sha256
                        for name, binding in sorted(preflight.evaluator_bindings.items())
                    },
                    "argv": argv,
                    "process": process,
                    "simulator_output": {
                        "requested_path": str(private_side_effect.relative_to(stage)),
                        "path": str(retained_side_effect.relative_to(stage)),
                        "identity": {
                            "device": side_binding.identity[0],
                            "inode": side_binding.identity[1],
                        },
                        "size_bytes": side_binding.size_bytes,
                        "sha256": side_binding.sha256,
                    },
                }
                if phase is not None:
                    measurement["phase_diagnostic"] = phase
                    measurement["frozen_trace_diagnostic"] = trace.record[
                        "diagnostics"
                    ]
                _assert_no_forbidden_keys(measurement)
                measurement_binding = _write_owned_record(
                    cell_directory / "measurement.json",
                    measurement,
                    "measurement_sha256",
                )
                current_owned.append(measurement_binding)
                if not retain_owned(current_owned):
                    raise PortfolioError(
                        "current-cell owned evidence changed before retention"
                    )
                measurements.append(
                    {
                        **cell,
                        "path": str(
                            (cell_directory / "measurement.json").relative_to(stage)
                        ),
                        "measurement_sha256": measurement["measurement_sha256"],
                    }
                )
                guard()
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                if isinstance(error, PhaseRunError):
                    current_owned.extend(error.bindings)
                if not cell_directory.exists():
                    cell_directory.mkdir(parents=True, mode=0o700)
                collision = error if isinstance(error, RecordCollision) else None
                request_path = cell_directory / "request.json"
                if request_binding is None and (
                    collision is None or collision.path != request_path
                ):
                    try:
                        request_binding = _write_request(request_path, request)
                        current_owned.append(request_binding)
                    except RecordCollision as record_error:
                        collision = record_error
                process_path = cell_directory / "process.json"
                if process is None:
                    process = _failed_process_record(
                        label="cachesim",
                        argv=argv,
                        result=result if isinstance(result, ChildResult) else None,
                        error=error,
                        timeout_seconds=_TIMEOUT_SECONDS,
                        max_output_bytes=_MAX_OUTPUT_BYTES,
                    )
                if process_binding is None and (
                    collision is None or collision.path != process_path
                ):
                    try:
                        process_binding = _write_owned_record(
                            process_path, process, "process_sha256"
                        )
                        current_owned.append(process_binding)
                    except RecordCollision as record_error:
                        collision = record_error
                integrity_failure = (
                    collision is not None
                    or isinstance(error, RecordCollision)
                    or not attempted
                    or "collision" in str(error).lower()
                    or "binding" in str(error).lower()
                    or "changed" in str(error).lower()
                    or "mismatch" in str(error).lower()
                    or "differs" in str(error).lower()
                )
                failure = {
                    "schema_version": 1,
                    "cell_index": cell["index"],
                    "kind": "binding_failure" if integrity_failure else "launch_failure",
                    "error": _bounded(collision or error),
                }
                if isinstance(error, PhaseRunError):
                    if error.process_sha256 is not None:
                        failure["process_sha256"] = error.process_sha256
                elif process_binding is not None and "process_sha256" in process:
                    failure["process_sha256"] = process["process_sha256"]
                if integrity_failure:
                    failure["remaining_cell_indices"] = list(
                        range(int(cell["index"]) + 1, len(selected_cells))
                    )
                (
                    failure_path,
                    _failure_binding,
                    retained_failure,
                ) = _write_failure_record(
                    stage,
                    cell_directory / "failure.json",
                    failure,
                )
                current_owned.append(_failure_binding)
                if not retain_owned(current_owned):
                    integrity_failure = True
                failures.append(
                    {
                        "cell_index": cell["index"],
                        "path": str(failure_path.relative_to(stage)),
                        "failure_sha256": retained_failure["failure_sha256"],
                    }
                )
                if integrity_failure:
                    provenance_intact = False
                    break
        if not any(side_effect_root.iterdir()):
            side_effect_root.rmdir()
        if provenance_intact:
            guard()
            if phase_apparatus is not None:
                _revalidate_file(phase_apparatus.source)
                _revalidate_file(phase_apparatus.binary)
        else:
            for retained in retained_bindings:
                if (retained.path, retained.identity) not in invalid_retained:
                    _revalidate_file(retained)
        inventory = _inventory(stage)
        receipt: dict[str, object] = {
            "schema_version": 1,
            "receipt_version": 1,
            "rung": rung,
            "task_root": str(preflight.task_root),
            "task_manifest_path": str(preflight.manifest_path),
            "task_manifest_sha256": preflight.manifest["manifest_sha256"],
            "task_manifest_file_sha256": preflight.manifest_binding.sha256,
            "source_receipt_sha256": preflight.source["receipt_sha256"],
            "source_receipt_file_sha256": preflight.source_binding.sha256,
            "r0_receipt_sha256": preflight.r0["receipt_sha256"],
            "r0_receipt_file_sha256": preflight.r0_binding.sha256,
            "candidate_commit": candidate,
            "candidate_tree": preflight.checkout_binding.tree,
            "policy": policy,
            "policy_source_sha256": preflight.r0["policy_source_sha256"],
            "binary_snapshot_sha256": preflight.artifact_bindings[
                "release_cachesim"
            ].sha256,
            "r0_artifact_snapshots": {
                name: {
                    "path": str(binding.path),
                    "identity": {
                        "device": binding.identity[0],
                        "inode": binding.identity[1],
                    },
                    "mode": binding.mode,
                    "size_bytes": binding.size_bytes,
                    "sha256": binding.sha256,
                }
                for name, binding in sorted(preflight.artifact_bindings.items())
            },
            "execution_copy": {
                "path": str(execution.path.relative_to(stage)),
                "identity": {
                    "device": execution.identity[0],
                    "inode": execution.identity[1],
                },
                "mode": execution.mode,
                "size_bytes": execution.size_bytes,
                "sha256": execution.sha256,
            },
            "phase_probe": (
                {
                    "source_path": str(phase_apparatus.source.path.relative_to(stage)),
                    "source_sha256": phase_apparatus.source.sha256,
                    "binary_path": str(phase_apparatus.binary.path.relative_to(stage)),
                    "binary_sha256": phase_apparatus.binary.sha256,
                    "compile_process_sha256": phase_apparatus.compile_process[
                        "process_sha256"
                    ],
                    "release_archive_sha256": preflight.artifact_bindings[
                        "release_archive"
                    ].sha256,
                    "release_cmake_cache_sha256": phase_apparatus.cmake_cache_sha256,
                    "compiler_path": str(phase_apparatus.compiler_path),
                    "compiler_resolved_path": str(phase_apparatus.compiler.path),
                    "compiler_sha256": phase_apparatus.compiler.sha256,
                    "include_flags": list(phase_apparatus.include_flags),
                    "link_flags": list(phase_apparatus.link_flags),
                }
                if phase_apparatus is not None
                else None
            ),
            "frozen_trace_diagnostics": (
                [
                    {
                        "trace_id": trace.record["trace_id"],
                        "diagnostic_sha256": trace.record[
                            "diagnostic_sha256"
                        ],
                        "diagnostics": trace.record["diagnostics"],
                    }
                    for trace in selected_traces
                ]
                if rung == "r2"
                else []
            ),
            "evaluator": {
                name: binding.sha256
                for name, binding in sorted(preflight.evaluator_bindings.items())
            },
            "host": {
                "platform": platform.platform(),
                "machine": platform.machine(),
                "python": sys.version,
            },
            "selected_cells": selected_cells,
            "measurements": measurements,
            "measurement_hashes": [
                item["measurement_sha256"] for item in measurements
            ],
            "failures": failures,
            "failure_hashes": [item["failure_sha256"] for item in failures],
            "provenance": {"final_binding_intact": provenance_intact},
            "evidence_inventory": inventory,
            "timings": {"total_wall_ns": max(0, time.monotonic_ns() - started)},
        }
        _assert_no_forbidden_keys(receipt)
        root_receipt_path = stage / "receipt.json"
        root_receipt_binding = write_new_record(
            root_receipt_path, receipt, "receipt_sha256"
        )
        if not isinstance(root_receipt_binding, NewRecordReceipt):
            raise PortfolioError("root receipt publisher returned no ownership receipt")
        if provenance_intact:
            guard()
        try:
            _publish_and_verify(
                stage=stage,
                stage_identity=stage_identity,
                output=preflight.output,
                receipt=receipt,
                inventory=inventory,
                root_receipt=root_receipt_binding,
            )
        except PublicationBindingError as publication_error:
            preserve_stage = not publication_error.renamed
            raise
        published = True
        return receipt
    finally:
        if not published and not preserve_stage and os.path.lexists(stage):
            cleanup_owned(stage, stage_identity)
