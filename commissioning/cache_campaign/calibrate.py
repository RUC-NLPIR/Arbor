from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, DecimalException, localcontext
from pathlib import Path
from pathlib import PurePosixPath

from . import portfolio as portfolio_module
from .diagnostics import PhaseBin
from .evaluate import _source_receipt, validate_r0_metadata_evidence
from .evidence import read_bound_json_object
from .portfolio import (
    _MANIFEST_KEYS,
    _R0_KEYS,
    _artifact_binding,
    _cache_size,
    _strict_object,
    _validate_trace_record,
)
from .portfolio_evidence import FileBinding, file_binding, verify_root
from .records import (
    HEX64,
    ParetoMeasurement,
    canonical_bytes,
    canonical_decimal,
    quarantine_unlink,
    record_sha256,
    scientific_decimal_context,
)
from .scope import _validate_contract


COMPARISON_POLICIES = ("LRU", "ARC", "WTinyLFU", "Sieve", "S3FIFO", "BeladySize")
REFERENCE_POLICIES = ("Sieve", "S3FIFO")
_AUDIT_STATES = {"accepted", "rejected", "pending_independent_review"}
_FRACTIONS = (Decimal("0.01"), Decimal("0.05"), Decimal("0.10"))
_FORBIDDEN_KEYS = {"score", "reward", "objective", "aggregate", "pass"}
_R2_KEYS = {
    "schema_version",
    "receipt_version",
    "rung",
    "task_root",
    "task_manifest_path",
    "task_manifest_sha256",
    "task_manifest_file_sha256",
    "source_receipt_sha256",
    "source_receipt_file_sha256",
    "r0_receipt_sha256",
    "r0_receipt_file_sha256",
    "candidate_commit",
    "candidate_tree",
    "policy",
    "policy_source_sha256",
    "binary_snapshot_sha256",
    "r0_artifact_snapshots",
    "execution_copy",
    "phase_probe",
    "frozen_trace_diagnostics",
    "trace_snapshots",
    "evaluator",
    "evaluator_snapshots",
    "scientific_inputs",
    "host",
    "selected_cells",
    "measurements",
    "measurement_hashes",
    "failures",
    "failure_hashes",
    "provenance",
    "evidence_inventory",
    "timings",
    "receipt_sha256",
}
_MEASUREMENT_KEYS = {
    "schema_version",
    "receipt_version",
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
    "trace_sha256",
    "trace_diagnostic_sha256",
    "source_receipt_sha256",
    "r0_receipt_sha256",
    "candidate_commit",
    "candidate_tree",
    "binary_sha256",
    "evaluator",
    "evaluator_snapshots",
    "scientific_inputs",
    "argv",
    "process",
    "simulator_output",
    "phase_diagnostic",
    "frozen_trace_diagnostic",
    "measurement_sha256",
}
_PARETO_KEYS = {
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
}
_PHASE_KEYS = {
    "schema_version",
    "trace_id",
    "trace_sha256",
    "frozen_trace_diagnostic_sha256",
    "policy",
    "cache_fraction",
    "cache_size_bytes",
    "request_count",
    "object_misses",
    "request_bytes",
    "byte_misses",
    "bins",
    "process",
    "phase_sha256",
}
_CALIBRATION_KEYS = {
    "schema_version",
    "task_manifest_sha256",
    "source_receipt_sha256",
    "source_commit",
    "binary_sha256",
    "evaluator_sha256s",
    "scientific_input_sha256s",
    "host_fingerprint",
    "repetitions",
    "cache_fractions",
    "references",
    "comparisons",
    "r0_receipt_sha256s",
    "input_receipt_sha256s",
    "calibration_sha256",
}


class CalibrationError(ValueError):
    pass


@dataclass(frozen=True)
class _R0Input:
    path: Path
    receipt: dict[str, object]
    binding: FileBinding
    source: dict[str, object]
    artifacts: dict[str, FileBinding]
    retained: dict[str, FileBinding]


@dataclass(frozen=True)
class _R2Input:
    path: Path
    receipt: dict[str, object]
    binding: FileBinding
    measurements: tuple[dict[str, object], ...]
    scientific_input_sha256s: dict[str, str]
    apparatus: dict[str, object]


@dataclass(frozen=True)
class _Inputs:
    manifest: dict[str, object]
    manifest_binding: FileBinding
    traces: tuple[object, ...]
    r0: dict[str, _R0Input]
    r2: tuple[_R2Input, ...]


def _forbid_campaign_scalars(value: object) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in _FORBIDDEN_KEYS:
                raise CalibrationError(f"forbidden campaign scalar key: {key}")
            _forbid_campaign_scalars(item)
    elif isinstance(value, list | tuple):
        for item in value:
            _forbid_campaign_scalars(item)


def _hash(value: object, label: str) -> str:
    if type(value) is not str or HEX64.fullmatch(value) is None:
        raise CalibrationError(f"{label} must be a lowercase SHA-256")
    return value


def _host(value: object, label: str) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {
        "platform",
        "machine",
        "python",
    }:
        raise CalibrationError(f"{label} host fingerprint is invalid")
    if any(type(item) is not str or not item for item in value.values()):
        raise CalibrationError(f"{label} host fingerprint is invalid")
    return {key: value[key] for key in ("machine", "platform", "python")}


def _hash_mapping(value: object, label: str) -> dict[str, str]:
    if not isinstance(value, dict) or not value:
        raise CalibrationError(f"{label} hash mapping is missing")
    result: dict[str, str] = {}
    for key, item in sorted(value.items()):
        if type(key) is not str or not key:
            raise CalibrationError(f"{label} hash mapping key is invalid")
        result[key] = _hash(item, f"{label} {key}")
    return result


def _scientific_hashes(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {
        "fixed_time_interposer",
        "release_archive",
        "release_cmake_cache",
        "headers",
    }:
        raise CalibrationError("R2 scientific input mapping is incomplete")
    result: dict[str, str] = {}
    for name in ("fixed_time_interposer", "release_archive", "release_cmake_cache"):
        item = value[name]
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            raise CalibrationError("R2 scientific input receipt is invalid")
        if type(item["path"]) is not str:
            raise CalibrationError("R2 scientific input path is invalid")
        result[name] = _hash(item["sha256"], f"R2 scientific input {name}")
    headers = value["headers"]
    if not isinstance(headers, dict) or not headers:
        raise CalibrationError("R2 scientific header mapping is missing")
    for name, item in sorted(headers.items()):
        if (
            type(name) is not str
            or not isinstance(item, dict)
            or set(item) != {"path", "sha256"}
            or type(item["path"]) is not str
        ):
            raise CalibrationError("R2 scientific header receipt is invalid")
        result[f"header:{name}"] = _hash(
            item["sha256"], f"R2 scientific header {name}"
        )
    return dict(sorted(result.items()))


def _validate_r0(path: Path) -> _R0Input:
    try:
        receipt, binding, _raw = _strict_object(Path(path), "receipt_sha256")
        if set(receipt) != _R0_KEYS:
            raise CalibrationError("R0 receipt keys mismatch")
        policy = receipt.get("policy")
        lock = portfolio_module.SOURCE_LOCK
        if (
            receipt.get("schema_version") != 1
            or receipt.get("receipt_version") != 1
            or receipt.get("rung") != "r0"
            or policy not in COMPARISON_POLICIES
            or receipt.get("repository_url") != lock.get("repository_url")
            or receipt.get("base_commit") != lock.get("commit")
            or receipt.get("candidate_commit") != lock.get("commit")
            or receipt.get("base_tree") != lock.get("tree")
            or receipt.get("candidate_tree") != lock.get("tree")
            or receipt.get("candidate_diff_sha256")
            != hashlib.sha256(b"").hexdigest()
        ):
            raise CalibrationError("R0 source, candidate, or policy binding mismatch")
        source_path = receipt.get("source_receipt_path")
        if type(source_path) is not str:
            raise CalibrationError("R0 source receipt path is invalid")
        source = _source_receipt(Path(source_path), lock)
        if (
            receipt.get("source_receipt_sha256") != source.get("receipt_sha256")
            or receipt.get("source_receipt_file_sha256") != source.get("_file_sha256")
            or str(Path(source_path).resolve(strict=True)) != source_path
        ):
            raise CalibrationError("R0 source receipt binding mismatch")
        checks = receipt.get("checks")
        expected_checks = {
            "source_binding",
            "evidence_binding",
            "build",
            "full_tests",
            "candidate_test",
            "sanitizer",
            "deterministic",
            "capacity",
            "metadata_probe",
        }
        if (
            not isinstance(checks, dict)
            or set(checks) != expected_checks
            or checks.get("candidate_test") is not None
            or any(
                checks.get(name) is not True
                for name in expected_checks - {"candidate_test"}
            )
        ):
            raise CalibrationError("R0 operational checks are not successful")
        scope = receipt.get("scope")
        if (
            not isinstance(scope, dict)
            or set(scope)
            != {
                "allowed_paths",
                "baseline_unchanged",
                "additive_wiring_only",
                "contract_bound",
                "changed_paths",
                "diff_sha256",
            }
            or scope.get("allowed_paths") is not True
            or scope.get("baseline_unchanged") is not True
            or scope.get("additive_wiring_only") is not True
            or scope.get("contract_bound") is not None
            or scope.get("changed_paths") != []
            or scope.get("diff_sha256") != hashlib.sha256(b"").hexdigest()
            or receipt.get("changed_path_sha256") != {}
            or receipt.get("contract_sha256") is not None
            or receipt.get("candidate_test_sha256") is not None
            or receipt.get("declared_metadata") is not None
        ):
            raise CalibrationError("R0 baseline scope is invalid")
        metadata = receipt.get("measured_metadata")
        if not isinstance(metadata, dict) or set(metadata) != {
            "bytes_per_object",
            "global_bytes",
            "measurement_sha256",
            "within_budget",
        }:
            raise CalibrationError("R0 measured metadata is invalid")
        measured_decimal = _canonical_decimal(
            metadata.get("bytes_per_object"), "R0 metadata bytes per object"
        )
        if (
            measured_decimal < 0
            or type(metadata.get("global_bytes")) is not int
            or int(metadata["global_bytes"]) < 0
            or metadata.get("within_budget") is not None
        ):
            raise CalibrationError("R0 measured metadata is invalid")
        _hash(metadata.get("measurement_sha256"), "R0 metadata measurement")
        if (
            receipt.get("complexity_audit") != "pending_independent_review"
            or receipt.get("errors") != []
            or receipt.get("unexpected_stage_entries") != []
        ):
            raise CalibrationError("R0 contains a failure or invented audit result")
        _host(receipt.get("host"), "R0")
        artifacts = receipt.get("artifact_snapshots")
        if not isinstance(artifacts, dict) or not artifacts:
            raise CalibrationError("R0 artifact snapshots are missing")
        bindings = {
            name: _artifact_binding(binding.path.parent, artifacts, name)
            for name in sorted(artifacts)
        }
        validated_evidence = validate_r0_metadata_evidence(
            receipt,
            binding.path.parent,
            candidate=str(receipt["candidate_commit"]),
            policy=str(policy),
            source_receipt_sha256=str(source["receipt_sha256"]),
        )
        release = bindings.get("release_cachesim")
        if (
            release is None
            or receipt.get("binary_sha256") != release.sha256
            or receipt.get("binary_post_run_sha256") != release.sha256
        ):
            raise CalibrationError("R0 binary snapshot binding mismatch")
        evaluator = _hash_mapping(receipt.get("evaluator"), "R0 evaluator")
        for name, digest in evaluator.items():
            artifact = bindings.get(f"evaluator_{name.removesuffix('_sha256')}")
            if artifact is None or artifact.sha256 != digest:
                raise CalibrationError("R0 evaluator evidence binding mismatch")
        retained = {
            name: bindings[name]
            for name in (
                "release_cachesim",
                "release_archive",
                "release_cmake_cache",
            )
        }
        for validated in validated_evidence.files:
            observed = file_binding(validated.path)
            if (
                observed.identity != validated.identity
                or observed.mode != validated.mode
                or observed.size_bytes != validated.size_bytes
                or observed.sha256 != validated.sha256
            ):
                raise CalibrationError("validated R0 evidence binding mismatch")
            retained_name = (
                "metadata_measurement_stdout"
                if validated == validated_evidence.stdout
                else "validated_" + validated.name.replace("/", "_")
            )
            if retained_name in retained:
                raise CalibrationError("duplicate validated R0 evidence name")
            retained[retained_name] = observed
        _forbid_campaign_scalars(receipt)
        return _R0Input(binding.path, receipt, binding, source, bindings, retained)
    except CalibrationError:
        raise
    except (OSError, ValueError) as error:
        raise CalibrationError(f"R0 evidence validation failed: {error}") from error


def _relative_path(root: Path, value: object, label: str) -> Path:
    if type(value) is not str:
        raise CalibrationError(f"{label} path is invalid")
    pure = PurePosixPath(value)
    if pure.is_absolute() or not pure.parts or ".." in pure.parts:
        raise CalibrationError(f"{label} path escapes its evidence root")
    return root.joinpath(*pure.parts)


def _validate_raw_file(
    root: Path, value: object, expected_name: str, label: str
) -> FileBinding:
    if not isinstance(value, dict) or set(value) != {
        "path",
        "size_bytes",
        "sha256",
        "identity",
    }:
        raise CalibrationError(f"{label} raw-file receipt is invalid")
    path = _relative_path(root, value.get("path"), label)
    if path.name != expected_name:
        raise CalibrationError(f"{label} raw-file name is invalid")
    observed = file_binding(path)
    identity = value.get("identity")
    if (
        not isinstance(identity, dict)
        or set(identity) != {"device", "inode"}
        or identity.get("device") != observed.identity[0]
        or identity.get("inode") != observed.identity[1]
        or value.get("size_bytes") != observed.size_bytes
        or value.get("sha256") != observed.sha256
    ):
        raise CalibrationError(f"{label} raw-file binding mismatch")
    return observed


def _validate_process(root: Path, value: object, label: str) -> dict[str, object]:
    expected = {
        "label",
        "argv",
        "timeout_seconds",
        "max_output_bytes",
        "returncode",
        "wall_ns",
        "cpu_ns",
        "stdout",
        "stderr",
        "process_sha256",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise CalibrationError(f"{label} process receipt keys mismatch")
    if (
        type(value.get("label")) is not str
        or not isinstance(value.get("argv"), list)
        or not value["argv"]
        or any(type(item) is not str or not item for item in value["argv"])
        or type(value.get("timeout_seconds")) not in {int, float}
        or value["timeout_seconds"] <= 0
        or type(value.get("max_output_bytes")) is not int
        or value["max_output_bytes"] <= 0
        or value.get("returncode") != 0
        or type(value.get("wall_ns")) is not int
        or value["wall_ns"] < 0
        or type(value.get("cpu_ns")) is not int
        or value["cpu_ns"] < 0
        or value.get("process_sha256") != record_sha256(value, "process_sha256")
    ):
        raise CalibrationError(f"{label} process receipt is unsuccessful")
    _validate_raw_file(root, value.get("stdout"), "stdout.raw", label)
    _validate_raw_file(root, value.get("stderr"), "stderr.raw", label)
    return value


def _phase_facts(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != _PHASE_KEYS:
        raise CalibrationError("R2 phase diagnostic keys mismatch")
    if (
        value.get("schema_version") != 1
        or value.get("phase_sha256") != record_sha256(value, "phase_sha256")
    ):
        raise CalibrationError("R2 phase diagnostic self-hash mismatch")
    bins = value.get("bins")
    if not isinstance(bins, list) or len(bins) != 16:
        raise CalibrationError("R2 phase diagnostics are incomplete")
    parsed = [PhaseBin.from_record(_object(item, "phase bin")) for item in bins]
    if [item.index for item in parsed] != list(range(16)):
        raise CalibrationError("R2 phase diagnostics are out of order")
    totals = {
        "request_count": sum(item.requests for item in parsed),
        "object_misses": sum(item.object_misses for item in parsed),
        "request_bytes": sum(item.request_bytes for item in parsed),
        "byte_misses": sum(item.byte_misses for item in parsed),
    }
    if any(value.get(key) != expected for key, expected in totals.items()):
        raise CalibrationError("R2 phase totals differ from their bins")
    return {
        "phase_sha256": value["phase_sha256"],
        **totals,
        "bins": [item.to_record() for item in parsed],
    }


def _validate_measurement(
    root: Path,
    summary: Mapping[str, object],
    *,
    receipt: Mapping[str, object],
    r0: _R0Input,
    manifest_trace: Mapping[str, object],
    evaluator: Mapping[str, str],
    scientific: Mapping[str, str],
) -> dict[str, object]:
    path = _relative_path(root, summary.get("path"), "R2 measurement")
    cell_index = summary.get("index")
    if (
        type(cell_index) is not int
        or summary.get("path")
        != f"measurements/{cell_index:04d}/measurement.json"
    ):
        raise CalibrationError("R2 measurement path binding is invalid")
    measurement, _binding, _raw = _strict_object(path, "measurement_sha256")
    if set(measurement) != _MEASUREMENT_KEYS:
        raise CalibrationError("R2 measurement keys mismatch")
    pareto = ParetoMeasurement.from_record(
        {key: measurement[key] for key in _PARETO_KEYS}
    )
    expected_summary = {
        "index": summary.get("index"),
        "trace_id": pareto.trace_id,
        "split": pareto.split,
        "cache_fraction": canonical_decimal(pareto.cache_fraction),
        "cache_size_bytes": pareto.cache_size_bytes,
        "path": summary.get("path"),
        "measurement_sha256": measurement["measurement_sha256"],
    }
    if dict(summary) != expected_summary:
        raise CalibrationError("R2 measurement summary differs from its receipt")
    r0_metadata = r0.receipt["measured_metadata"]
    assert isinstance(r0_metadata, dict)
    if (
        pareto.rung != "r2"
        or pareto.policy != receipt.get("policy")
        or pareto.trace_id != manifest_trace.get("trace_id")
        or pareto.split != manifest_trace.get("split")
        or measurement.get("trace_sha256") != manifest_trace.get("sha256")
        or measurement.get("trace_diagnostic_sha256")
        != manifest_trace.get("diagnostic_sha256")
        or measurement.get("source_receipt_sha256")
        != receipt.get("source_receipt_sha256")
        or measurement.get("r0_receipt_sha256") != r0.receipt["receipt_sha256"]
        or measurement.get("candidate_commit") != receipt.get("candidate_commit")
        or measurement.get("candidate_tree") != receipt.get("candidate_tree")
        or measurement.get("binary_sha256") != receipt.get("binary_snapshot_sha256")
        or measurement.get("evaluator") != dict(evaluator)
        or measurement.get("scientific_inputs") != dict(scientific)
        or pareto.metadata_bytes_per_object
        != _canonical_decimal(r0_metadata["bytes_per_object"], "R0 metadata")
        or pareto.global_metadata_bytes != r0_metadata.get("global_bytes")
        or pareto.metadata_measurement_sha256
        != r0_metadata.get("measurement_sha256")
        or measurement.get("frozen_trace_diagnostic")
        != manifest_trace.get("diagnostics")
    ):
        raise CalibrationError("R2 measurement provenance binding mismatch")
    process = _validate_process(root, measurement.get("process"), "R2 simulator")
    process_path = path.parent / "process.json"
    process_record, _process_binding, _process_raw = _strict_object(
        process_path, "process_sha256"
    )
    if process_record != process or measurement.get("argv") != process.get("argv"):
        raise CalibrationError("R2 simulator process evidence differs")
    request, _request_binding, _request_raw = _strict_object(
        path.parent / "request.json", "request_sha256"
    )
    if (
        request.get("rung") != "r2"
        or request.get("trace_id") != pareto.trace_id
        or request.get("policy") != pareto.policy
        or request.get("cache_fraction") != canonical_decimal(pareto.cache_fraction)
        or request.get("cache_size_bytes") != pareto.cache_size_bytes
        or request.get("argv") != process.get("argv")
    ):
        raise CalibrationError("R2 simulator request evidence differs")
    simulator_output = measurement.get("simulator_output")
    if not isinstance(simulator_output, dict) or set(simulator_output) != {
        "requested_path",
        "path",
        "identity",
        "size_bytes",
        "sha256",
    }:
        raise CalibrationError("R2 simulator side-effect receipt is invalid")
    side_effect = _relative_path(
        root, simulator_output.get("path"), "R2 simulator side effect"
    )
    requested_side_effect = _relative_path(
        root,
        simulator_output.get("requested_path"),
        "R2 requested simulator side effect",
    )
    side_binding = file_binding(side_effect)
    stdout = process["stdout"]
    assert isinstance(stdout, dict)
    identity = simulator_output.get("identity")
    if (
        simulator_output.get("requested_path")
        != f"simulator-side-effects/{cell_index:04d}.cachesim"
        or simulator_output.get("path")
        != f"measurements/{cell_index:04d}/simulator.cachesim"
        or os.path.lexists(requested_side_effect)
        or not isinstance(identity, dict)
        or identity
        != {
            "device": side_binding.identity[0],
            "inode": side_binding.identity[1],
        }
        or simulator_output.get("size_bytes") != side_binding.size_bytes
        or simulator_output.get("sha256") != side_binding.sha256
        or side_binding.sha256 != stdout.get("sha256")
    ):
        raise CalibrationError("R2 simulator side-effect binding mismatch")
    phase = measurement.get("phase_diagnostic")
    facts = _phase_facts(phase)
    phase_record, _phase_binding, _phase_raw = _strict_object(
        path.parent / "phase.json", "phase_sha256"
    )
    if phase_record != phase:
        raise CalibrationError("R2 phase receipt differs from its measurement")
    assert isinstance(phase, dict)
    phase_process = _validate_process(root, phase.get("process"), "R2 phase probe")
    phase_process_record, _phase_process_binding, _phase_process_raw = _strict_object(
        path.parent / "phase-process/process.json", "process_sha256"
    )
    if phase_process_record != phase_process:
        raise CalibrationError("R2 phase process evidence differs")
    if (
        phase.get("trace_id") != pareto.trace_id
        or phase.get("trace_sha256") != manifest_trace.get("sha256")
        or phase.get("frozen_trace_diagnostic_sha256")
        != manifest_trace.get("diagnostic_sha256")
        or phase.get("policy") != pareto.policy
        or phase.get("cache_fraction") != canonical_decimal(pareto.cache_fraction)
        or phase.get("cache_size_bytes") != pareto.cache_size_bytes
        or phase.get("request_count") != pareto.request_count
    ):
        raise CalibrationError("R2 phase cell binding mismatch")
    measurement["_calibration_phase_facts"] = facts
    return measurement


def _validate_r2(
    path: Path,
    *,
    manifest: Mapping[str, object],
    manifest_binding: FileBinding,
    traces: Sequence[object],
    r0: _R0Input,
) -> _R2Input:
    try:
        receipt, binding, _raw = _strict_object(Path(path), "receipt_sha256")
        if set(receipt) != _R2_KEYS:
            raise CalibrationError("R2 receipt keys mismatch")
        root = binding.path.parent
        verify_root(root, receipt)
        policy = r0.receipt["policy"]
        task_root_value = receipt.get("task_root")
        if type(task_root_value) is not str:
            raise CalibrationError("R2 task root binding is invalid")
        task_root = Path(task_root_value).resolve(strict=True)
        if (
            str(task_root) != task_root_value
            or task_root not in manifest_binding.path.parents
        ):
            raise CalibrationError("R2 task root binding is invalid")
        if (
            receipt.get("schema_version") != 1
            or receipt.get("receipt_version") != 1
            or receipt.get("rung") != "r2"
            or receipt.get("task_manifest_path") != str(manifest_binding.path)
            or receipt.get("task_manifest_sha256") != manifest.get("manifest_sha256")
            or receipt.get("task_manifest_file_sha256") != manifest_binding.sha256
            or receipt.get("source_receipt_sha256")
            != r0.receipt.get("source_receipt_sha256")
            or receipt.get("source_receipt_file_sha256")
            != r0.receipt.get("source_receipt_file_sha256")
            or receipt.get("r0_receipt_sha256") != r0.receipt.get("receipt_sha256")
            or receipt.get("r0_receipt_file_sha256") != r0.binding.sha256
            or receipt.get("candidate_commit") != r0.receipt.get("candidate_commit")
            or receipt.get("candidate_tree") != r0.receipt.get("candidate_tree")
            or receipt.get("policy") != policy
            or receipt.get("policy_source_sha256")
            != r0.receipt.get("policy_source_sha256")
            or receipt.get("binary_snapshot_sha256")
            != r0.receipt.get("binary_sha256")
            or receipt.get("failures") != []
            or receipt.get("failure_hashes") != []
            or receipt.get("provenance") != {"final_binding_intact": True}
        ):
            raise CalibrationError("R2 task/source/candidate binding or failure state is invalid")
        _host(receipt.get("host"), "R2")
        evaluator = _hash_mapping(receipt.get("evaluator"), "R2 evaluator")
        snapshots = receipt.get("evaluator_snapshots")
        if not isinstance(snapshots, dict) or set(snapshots) != set(evaluator):
            raise CalibrationError("R2 evaluator snapshot mapping is incomplete")
        for name, digest in evaluator.items():
            snapshot = snapshots[name]
            if not isinstance(snapshot, dict) or set(snapshot) != {
                "path",
                "identity",
                "size_bytes",
                "sha256",
            }:
                raise CalibrationError("R2 evaluator snapshot receipt is invalid")
            observed = file_binding(
                _relative_path(root, snapshot.get("path"), "R2 evaluator snapshot")
            )
            identity = snapshot.get("identity")
            if (
                not isinstance(identity, dict)
                or identity
                != {"device": observed.identity[0], "inode": observed.identity[1]}
                or snapshot.get("size_bytes") != observed.size_bytes
                or snapshot.get("sha256") != observed.sha256
                or observed.sha256 != digest
            ):
                raise CalibrationError("R2 evaluator snapshot binding mismatch")
        scientific = _scientific_hashes(receipt.get("scientific_inputs"))
        scientific_records = receipt["scientific_inputs"]
        assert isinstance(scientific_records, dict)
        for name in ("fixed_time_interposer", "release_archive", "release_cmake_cache"):
            item = scientific_records[name]
            assert isinstance(item, dict)
            observed = file_binding(
                _relative_path(root, item.get("path"), "R2 scientific input")
            )
            if observed.sha256 != scientific[name]:
                raise CalibrationError("R2 scientific input snapshot binding mismatch")
        headers = scientific_records["headers"]
        assert isinstance(headers, dict)
        for name, item in headers.items():
            assert isinstance(item, dict)
            observed = file_binding(
                _relative_path(root, item.get("path"), "R2 scientific header")
            )
            if observed.sha256 != scientific[f"header:{name}"]:
                raise CalibrationError("R2 scientific header snapshot binding mismatch")
        execution = receipt.get("execution_copy")
        if not isinstance(execution, dict) or set(execution) != {
            "path",
            "identity",
            "mode",
            "size_bytes",
            "sha256",
        }:
            raise CalibrationError("R2 binary execution copy is invalid")
        execution_binding = file_binding(
            _relative_path(root, execution.get("path"), "R2 binary execution")
        )
        if (
            execution.get("identity")
            != {
                "device": execution_binding.identity[0],
                "inode": execution_binding.identity[1],
            }
            or execution.get("mode") != execution_binding.mode
            or execution.get("size_bytes") != execution_binding.size_bytes
            or execution.get("sha256") != execution_binding.sha256
            or execution_binding.sha256 != receipt.get("binary_snapshot_sha256")
        ):
            raise CalibrationError("R2 binary execution binding mismatch")
        r0_snapshots = receipt.get("r0_artifact_snapshots")
        if not isinstance(r0_snapshots, dict) or set(r0_snapshots) != set(
            r0.retained
        ):
            raise CalibrationError("R2 retained R0 evidence mapping is missing")
        for name, snapshot in r0_snapshots.items():
            if not isinstance(snapshot, dict) or set(snapshot) != {
                "path",
                "identity",
                "mode",
                "size_bytes",
                "sha256",
            }:
                raise CalibrationError("R2 retained R0 evidence receipt is invalid")
            expected = r0.retained[name]
            if (
                snapshot.get("path") != str(expected.path)
                or snapshot.get("identity")
                != {
                    "device": expected.identity[0],
                    "inode": expected.identity[1],
                }
                or snapshot.get("mode") != expected.mode
                or snapshot.get("size_bytes") != expected.size_bytes
                or snapshot.get("sha256") != expected.sha256
            ):
                raise CalibrationError("R2 retained R0 evidence binding mismatch")
            observed = file_binding(expected.path)
            if observed != expected:
                raise CalibrationError("R2 retained R0 evidence changed")
        phase_probe = receipt.get("phase_probe")
        phase_keys = {
            "source_path",
            "source_sha256",
            "binary_path",
            "binary_sha256",
            "compile_process_sha256",
            "release_archive_sha256",
            "release_cmake_cache_sha256",
            "compiler_path",
            "compiler_resolved_path",
            "compiler_sha256",
            "include_flags",
            "link_flags",
        }
        if not isinstance(phase_probe, dict) or set(phase_probe) != phase_keys:
            raise CalibrationError("R2 phase apparatus is incomplete")
        for name in (
            "source_sha256",
            "binary_sha256",
            "compile_process_sha256",
            "release_archive_sha256",
            "release_cmake_cache_sha256",
            "compiler_sha256",
        ):
            _hash(phase_probe[name], f"R2 phase apparatus {name}")
        for name in ("source_path", "binary_path"):
            item = file_binding(
                _relative_path(root, phase_probe[name], "R2 phase apparatus")
            )
            expected_hash = phase_probe[f"{name.removesuffix('_path')}_sha256"]
            if item.sha256 != expected_hash:
                raise CalibrationError("R2 phase apparatus snapshot binding mismatch")
        if (
            phase_probe.get("release_archive_sha256")
            != scientific["release_archive"]
            or phase_probe.get("release_cmake_cache_sha256")
            != scientific["release_cmake_cache"]
            or type(phase_probe.get("compiler_path")) is not str
            or type(phase_probe.get("compiler_resolved_path")) is not str
            or not isinstance(phase_probe.get("include_flags"), list)
            or not isinstance(phase_probe.get("link_flags"), list)
        ):
            raise CalibrationError("R2 phase apparatus dependency binding mismatch")
        compiler = file_binding(Path(str(phase_probe["compiler_resolved_path"])))
        if compiler.sha256 != phase_probe.get("compiler_sha256"):
            raise CalibrationError("R2 phase compiler binding mismatch")
        compile_process, _compile_binding, _compile_raw = _strict_object(
            root / "phase-compile-process/process.json", "process_sha256"
        )
        _validate_process(root, compile_process, "R2 phase compiler")
        if compile_process.get("process_sha256") != phase_probe.get(
            "compile_process_sha256"
        ):
            raise CalibrationError("R2 phase compile process binding mismatch")
        apparatus = {
            name: phase_probe[name]
            for name in (
                "source_sha256",
                "binary_sha256",
                "release_archive_sha256",
                "release_cmake_cache_sha256",
                "compiler_sha256",
                "compiler_path",
                "compiler_resolved_path",
                "include_flags",
                "link_flags",
            )
        }
        manifest_traces = [item.record for item in traces]  # type: ignore[attr-defined]
        frozen = receipt.get("frozen_trace_diagnostics")
        expected_frozen = [
            {
                "trace_id": item["trace_id"],
                "diagnostic_sha256": item["diagnostic_sha256"],
                "diagnostics": item["diagnostics"],
            }
            for item in manifest_traces
        ]
        if frozen != expected_frozen:
            raise CalibrationError("R2 frozen trace diagnostics differ from the manifest")
        snapshots_value = receipt.get("trace_snapshots")
        if not isinstance(snapshots_value, list) or len(snapshots_value) != len(
            manifest_traces
        ):
            raise CalibrationError("R2 trace snapshots are incomplete")
        for snapshot, trace in zip(snapshots_value, manifest_traces, strict=True):
            if not isinstance(snapshot, dict) or set(snapshot) != {
                "trace_id",
                "source_path",
                "source_identity",
                "source_size_bytes",
                "source_sha256",
                "snapshot_path",
                "snapshot_identity",
                "snapshot_size_bytes",
                "snapshot_sha256",
                "audit_path",
                "audit_identity",
                "audit_sha256",
            }:
                raise CalibrationError("R2 trace snapshot receipt is invalid")
            snapshot_binding = file_binding(
                _relative_path(root, snapshot["snapshot_path"], "R2 trace snapshot")
            )
            audit_binding = file_binding(
                _relative_path(root, snapshot["audit_path"], "R2 trace audit")
            )
            source_metadata = Path(str(trace["path"])).stat(follow_symlinks=False)
            if (
                snapshot.get("trace_id") != trace["trace_id"]
                or snapshot.get("source_path") != trace["path"]
                or snapshot.get("source_identity")
                != {
                    "device": source_metadata.st_dev,
                    "inode": source_metadata.st_ino,
                }
                or snapshot.get("source_size_bytes") != trace["size_bytes"]
                or snapshot.get("source_sha256") != trace["sha256"]
                or snapshot.get("snapshot_identity")
                != {
                    "device": snapshot_binding.identity[0],
                    "inode": snapshot_binding.identity[1],
                }
                or snapshot.get("snapshot_size_bytes") != snapshot_binding.size_bytes
                or snapshot.get("snapshot_sha256") != snapshot_binding.sha256
                or snapshot_binding.sha256 != trace["sha256"]
                or snapshot.get("audit_identity")
                != {
                    "device": audit_binding.identity[0],
                    "inode": audit_binding.identity[1],
                }
                or snapshot.get("audit_sha256") != trace["diagnostic_sha256"]
                or audit_binding.sha256
                != hashlib.sha256(
                    (
                        json.dumps(
                            trace["diagnostics"],
                            ensure_ascii=False,
                            sort_keys=True,
                            indent=2,
                        )
                        + "\n"
                    ).encode("utf-8")
                ).hexdigest()
            ):
                raise CalibrationError("R2 trace snapshot binding mismatch")
        selected_cells = receipt.get("selected_cells")
        expected_cells = [
            {
                "index": index,
                "trace_id": trace["trace_id"],
                "split": trace["split"],
                "cache_fraction": canonical_decimal(fraction),
                "cache_size_bytes": _cache_size(trace["working_set_bytes"], fraction),
            }
            for index, (trace, fraction) in enumerate(
                (item, fraction)
                for item in manifest_traces
                for fraction in _FRACTIONS
            )
        ]
        if selected_cells != expected_cells:
            raise CalibrationError("R2 selected cell set is incomplete or inconsistent")
        summaries = receipt.get("measurements")
        hashes = receipt.get("measurement_hashes")
        if (
            not isinstance(summaries, list)
            or len(summaries) != len(expected_cells)
            or not isinstance(hashes, list)
            or hashes != [item.get("measurement_sha256") for item in summaries if isinstance(item, dict)]
            or len(set(hashes)) != len(hashes)
        ):
            raise CalibrationError("R2 measurement cell set is incomplete")
        measurements: list[dict[str, object]] = []
        for summary, cell in zip(summaries, expected_cells, strict=True):
            if not isinstance(summary, dict) or set(summary) != {
                "index",
                "trace_id",
                "split",
                "cache_fraction",
                "cache_size_bytes",
                "path",
                "measurement_sha256",
            } or any(summary.get(key) != value for key, value in cell.items()):
                raise CalibrationError("R2 measurement cell summary is invalid")
            trace = next(
                item for item in manifest_traces if item["trace_id"] == cell["trace_id"]
            )
            measurement = _validate_measurement(
                root,
                summary,
                receipt=receipt,
                r0=r0,
                manifest_trace=trace,
                evaluator=evaluator,
                scientific=scientific,
            )
            expected_snapshot_hashes = {
                name: item["sha256"] for name, item in snapshots.items()
            }
            if measurement.get("evaluator_snapshots") != expected_snapshot_hashes:
                raise CalibrationError("R2 measurement evaluator snapshot binding mismatch")
            measurements.append(measurement)
        _forbid_campaign_scalars(receipt)
        return _R2Input(
            binding.path,
            receipt,
            binding,
            tuple(measurements),
            scientific,
            apparatus,
        )
    except CalibrationError:
        raise
    except (OSError, ValueError) as error:
        raise CalibrationError(f"R2 evidence validation failed: {error}") from error


def _load_inputs(
    task_manifest: Path,
    r0_receipts: Sequence[Path],
    receipts: Sequence[Path],
) -> _Inputs:
    if (
        isinstance(r0_receipts, (str, bytes))
        or not isinstance(r0_receipts, Sequence)
        or len(r0_receipts) != 6
    ):
        raise CalibrationError("calibration requires exactly six R0 receipts")
    if (
        isinstance(receipts, (str, bytes))
        or not isinstance(receipts, Sequence)
        or len(receipts) != 14
    ):
        raise CalibrationError(
            "calibration requires fourteen R2 receipts: five per reference policy"
        )
    try:
        if (
            portfolio_module.SOURCE_LOCK.get("comparison_policies")
            != list(COMPARISON_POLICIES)
            or portfolio_module.SOURCE_LOCK.get("baseline_policies")
            != list(REFERENCE_POLICIES)
        ):
            raise CalibrationError("source lock calibration policies mismatch")
        manifest, manifest_binding, _manifest_raw = _strict_object(
            Path(task_manifest), "manifest_sha256"
        )
        if set(manifest) != _MANIFEST_KEYS:
            raise CalibrationError("task manifest keys mismatch")
        decimal_manifest = read_bound_json_object(
            manifest_binding.path,
            max_bytes=64 * 1024 * 1024,
            decimal_numbers=True,
        ).value
        if (
            manifest.get("schema_version") != 1
            or manifest.get("source_commit")
            != portfolio_module.SOURCE_LOCK.get("commit")
            or tuple(decimal_manifest.get("cache_fractions", ())) != _FRACTIONS
        ):
            raise CalibrationError("task manifest source or fractions mismatch")
        if not isinstance(manifest.get("traces"), list) or not manifest["traces"]:
            raise CalibrationError("task manifest traces are missing")
        traces = tuple(
            _validate_trace_record(item, str(manifest["source_commit"]))
            for item in manifest["traces"]
        )
        if len({item.record["trace_id"] for item in traces}) != len(traces):
            raise CalibrationError("task manifest trace IDs are duplicated")
        resolved_r0 = [Path(path).resolve(strict=True) for path in r0_receipts]
        if len(set(resolved_r0)) != len(resolved_r0):
            raise CalibrationError("duplicate R0 receipt paths are forbidden")
        r0_inputs = [_validate_r0(path) for path in resolved_r0]
        by_policy: dict[str, _R0Input] = {}
        for item in r0_inputs:
            policy = str(item.receipt["policy"])
            if policy in by_policy:
                raise CalibrationError("duplicate R0 policy receipts are forbidden")
            by_policy[policy] = item
        if set(by_policy) != set(COMPARISON_POLICIES):
            raise CalibrationError("R0 receipts must cover all six comparison policies")
        r0_hashes = [str(item.receipt["receipt_sha256"]) for item in r0_inputs]
        if len(set(r0_hashes)) != len(r0_hashes):
            raise CalibrationError("duplicate R0 receipt hashes are forbidden")
        source_hashes = {
            item.receipt["source_receipt_sha256"] for item in r0_inputs
        }
        candidates = {item.receipt["candidate_commit"] for item in r0_inputs}
        binaries = {item.receipt["binary_sha256"] for item in r0_inputs}
        r0_hosts = {
            canonical_bytes(_host(item.receipt["host"], "R0"))
            for item in r0_inputs
        }
        r0_evaluators = [
            _hash_mapping(item.receipt["evaluator"], "R0 evaluator")
            for item in r0_inputs
        ]
        if (
            len(source_hashes) != 1
            or len(candidates) != 1
            or len(binaries) != 1
            or len(r0_hosts) != 1
            or any(value != r0_evaluators[0] for value in r0_evaluators[1:])
        ):
            raise CalibrationError("R0 source, candidate, binary, host, or evaluator is mixed")
        resolved_r2 = [Path(path).resolve(strict=True) for path in receipts]
        if len(set(resolved_r2)) != len(resolved_r2):
            raise CalibrationError("duplicate R2 receipt paths are forbidden")
        r2_inputs: list[_R2Input] = []
        for path in resolved_r2:
            preview, _preview_binding, _preview_raw = _strict_object(
                path, "receipt_sha256"
            )
            policy = preview.get("policy")
            if type(policy) is not str or policy not in by_policy:
                raise CalibrationError("R2 policy has no exact R0 receipt")
            r2_inputs.append(
                _validate_r2(
                    path,
                    manifest=manifest,
                    manifest_binding=manifest_binding,
                    traces=traces,
                    r0=by_policy[policy],
                )
            )
        receipt_hashes = [
            str(item.receipt["receipt_sha256"]) for item in r2_inputs
        ]
        if len(set(receipt_hashes)) != len(receipt_hashes):
            raise CalibrationError("duplicate R2 receipt hashes are forbidden")
        policy_counts = {
            policy: sum(item.receipt["policy"] == policy for item in r2_inputs)
            for policy in COMPARISON_POLICIES
        }
        if any(policy_counts[policy] != 5 for policy in REFERENCE_POLICIES):
            raise CalibrationError("each constraint baseline requires five R2 receipts")
        if any(
            policy_counts[policy] != 1
            for policy in set(COMPARISON_POLICIES) - set(REFERENCE_POLICIES)
        ):
            raise CalibrationError("each non-reference policy requires one R2 receipt")
        first = r2_inputs[0]
        evaluator = _hash_mapping(first.receipt["evaluator"], "R2 evaluator")
        scientific = first.scientific_input_sha256s
        host = _host(first.receipt["host"], "R2")
        apparatus = first.apparatus
        for item in r2_inputs:
            if item.receipt.get("task_manifest_sha256") != manifest["manifest_sha256"]:
                raise CalibrationError("R2 task manifest binding is mixed")
            if item.receipt.get("source_receipt_sha256") not in source_hashes:
                raise CalibrationError("R2 source receipt binding is mixed")
            if item.receipt.get("candidate_commit") not in candidates:
                raise CalibrationError("R2 candidate binding is mixed")
            if item.receipt.get("binary_snapshot_sha256") not in binaries:
                raise CalibrationError("R2 binary apparatus is mixed")
            if _hash_mapping(item.receipt["evaluator"], "R2 evaluator") != evaluator:
                raise CalibrationError("R2 evaluator version is mixed")
            if item.scientific_input_sha256s != scientific:
                raise CalibrationError("R2 scientific input hashes are mixed")
            if item.apparatus != apparatus:
                raise CalibrationError("R2 phase apparatus is mixed")
            if _host(item.receipt["host"], "R2") != host:
                raise CalibrationError("R2 host fingerprint is mixed")
        if canonical_bytes(host) not in r0_hosts:
            raise CalibrationError("R0 and R2 host fingerprints differ")
        for name, digest in r0_evaluators[0].items():
            if evaluator.get(name) != digest:
                raise CalibrationError("R0 and R2 evaluator dependency hashes differ")
        return _Inputs(
            manifest,
            manifest_binding,
            traces,
            {policy: by_policy[policy] for policy in COMPARISON_POLICIES},
            tuple(r2_inputs),
        )
    except CalibrationError:
        raise
    except (OSError, TypeError, ValueError) as error:
        raise CalibrationError(f"calibration input validation failed: {error}") from error


def _input_signature(inputs: _Inputs) -> tuple[object, ...]:
    manifest = (
        str(inputs.manifest_binding.path),
        inputs.manifest_binding.identity,
        inputs.manifest_binding.sha256,
        inputs.manifest["manifest_sha256"],
    )
    r0 = tuple(
        (
            policy,
            str(item.path),
            item.binding.identity,
            item.binding.sha256,
            item.receipt["receipt_sha256"],
            item.source["_file_identity"],
            item.source["_file_sha256"],
            tuple(
                (name, binding.identity, binding.sha256)
                for name, binding in sorted(item.artifacts.items())
            ),
            tuple(
                (name, binding.identity, binding.sha256)
                for name, binding in sorted(item.retained.items())
            ),
        )
        for policy, item in inputs.r0.items()
    )
    r2 = tuple(
        (
            str(item.path),
            item.binding.identity,
            item.binding.sha256,
            item.receipt["receipt_sha256"],
        )
        for item in inputs.r2
    )
    traces = tuple(
        (
            str(item.path),  # type: ignore[attr-defined]
            item.record["sha256"],  # type: ignore[attr-defined]
            item.record["size_bytes"],  # type: ignore[attr-defined]
        )
        for item in inputs.traces
    )
    return manifest, r0, r2, traces


def _floor_90(value: Decimal) -> str:
    context = scientific_decimal_context()
    context.prec = max(context.prec, len(value.as_tuple().digits) + 2)
    with localcontext(context):
        return canonical_decimal(value * Decimal(9) / Decimal(10))


def _probe_evidence(r0: _R0Input) -> dict[str, str]:
    receipt = r0.receipt
    commands = receipt.get("commands")
    matches = [
        item
        for item in commands
        if isinstance(item, dict) and item.get("label") == "metadata-run"
    ] if isinstance(commands, list) else []
    if len(matches) != 1:
        raise CalibrationError("R0 metadata command evidence is missing")
    command = matches[0]
    stdout = command.get("stdout")
    metadata = receipt.get("measured_metadata")
    probes = receipt.get("probes")
    probe = probes.get("metadata") if isinstance(probes, dict) else None
    inventory = receipt.get("evidence_inventory")
    if (
        not isinstance(stdout, dict)
        or not isinstance(metadata, dict)
        or not isinstance(probe, dict)
        or not isinstance(probe.get("binary"), dict)
        or not isinstance(probe.get("interposer_binary"), dict)
        or not isinstance(inventory, list)
    ):
        raise CalibrationError("R0 metadata probe evidence is malformed")
    inventory_matches = [
        item
        for item in inventory
        if isinstance(item, dict) and item.get("path") == stdout.get("path")
    ]
    stdout_sha256 = _hash(stdout.get("sha256"), "R0 metadata stdout")
    measurement_sha256 = _hash(
        metadata.get("measurement_sha256"), "R0 metadata measurement"
    )
    if (
        len(inventory_matches) != 1
        or inventory_matches[0].get("sha256") != stdout_sha256
        or inventory_matches[0].get("observed_sha256") != stdout_sha256
        or inventory_matches[0].get("binding_intact") is not True
        or measurement_sha256 != stdout_sha256
    ):
        raise CalibrationError("R0 metadata stdout inventory binding mismatch")
    return {
        "r0_receipt_sha256": _hash(
            receipt.get("receipt_sha256"), "R0 receipt"
        ),
        "metadata_command_sha256": _hash(
            command.get("command_sha256"), "R0 metadata command"
        ),
        "stdout_sha256": stdout_sha256,
        "metadata_measurement_sha256": measurement_sha256,
        "metadata_probe_source_sha256": _hash(
            probe.get("source_sha256"), "R0 metadata probe source"
        ),
        "metadata_probe_binary_sha256": _hash(
            probe["binary"].get("sha256"), "R0 metadata probe binary"
        ),
        "metadata_interposer_source_sha256": _hash(
            probe.get("interposer_source_sha256"),
            "R0 metadata interposer source",
        ),
        "metadata_interposer_binary_sha256": _hash(
            probe["interposer_binary"].get("sha256"),
            "R0 metadata interposer binary",
        ),
    }


def _freeze(inputs: _Inputs) -> dict[str, object]:
    grouped: dict[
        tuple[str, str, str], list[dict[str, object]]
    ] = {}
    all_measurement_hashes: set[str] = set()
    for receipt in inputs.r2:
        for measurement in receipt.measurements:
            measurement_hash = str(measurement["measurement_sha256"])
            if measurement_hash in all_measurement_hashes:
                raise CalibrationError("duplicate R2 measurement hashes are forbidden")
            all_measurement_hashes.add(measurement_hash)
            key = (
                str(measurement["policy"]),
                str(measurement["trace_id"]),
                str(measurement["cache_fraction"]),
            )
            grouped.setdefault(key, []).append(measurement)
    references: dict[str, object] = {}
    comparisons: dict[str, object] = {}
    manifest_traces = [item.record for item in inputs.traces]  # type: ignore[attr-defined]
    fractions = [canonical_decimal(item) for item in _FRACTIONS]
    for policy in COMPARISON_POLICIES:
        policy_comparisons: dict[str, object] = {}
        expected_repetitions = 5 if policy in REFERENCE_POLICIES else 1
        reference: dict[str, object] | None = None
        if policy in REFERENCE_POLICIES:
            metadata = inputs.r0[policy].receipt["measured_metadata"]
            assert isinstance(metadata, dict)
            reference = {
                "metadata": {
                    "bytes_per_object": metadata["bytes_per_object"],
                    "global_bytes": metadata["global_bytes"],
                    "measurement_sha256": metadata["measurement_sha256"],
                    "probe_evidence": _probe_evidence(inputs.r0[policy]),
                    "independent_audit": "pending_independent_review",
                }
            }
        for trace in manifest_traces:
            trace_id = str(trace["trace_id"])
            comparison_cells: dict[str, object] = {}
            reference_cells: dict[str, object] = {}
            for fraction in fractions:
                values = grouped.get((policy, trace_id, fraction), [])
                if len(values) != expected_repetitions:
                    raise CalibrationError("calibration cell repetitions are incomplete")
                measurement_hashes = [
                    str(item["measurement_sha256"]) for item in values
                ]
                if len(set(measurement_hashes)) != expected_repetitions:
                    raise CalibrationError("calibration cell measurement hashes are duplicated")
                object_values = sorted(
                    _canonical_decimal(item["object_miss_ratio"], "object miss ratio")
                    for item in values
                )
                byte_values = sorted(
                    _canonical_decimal(item["byte_miss_ratio"], "byte miss ratio")
                    for item in values
                )
                phases = sorted(
                    (
                        item["_calibration_phase_facts"]
                        for item in values
                    ),
                    key=lambda item: str(item["phase_sha256"]),  # type: ignore[index]
                )
                comparison_cells[fraction] = {
                    "repetitions": expected_repetitions,
                    "object_miss_ratio_values": [
                        canonical_decimal(item) for item in object_values
                    ],
                    "byte_miss_ratio_values": [
                        canonical_decimal(item) for item in byte_values
                    ],
                    "phase_values": phases,
                }
                if reference is not None:
                    throughputs = sorted(
                        _canonical_decimal(
                            item["simulator_throughput_mqps"], "simulator throughput"
                        )
                        for item in values
                    )
                    cpu_values = sorted(
                        _canonical_decimal(
                            item["cpu_ns_per_request"], "CPU per request"
                        )
                        for item in values
                    )
                    median = throughputs[2]
                    reference_cells[fraction] = {
                        "repetitions": 5,
                        "object_miss_ratio_values": [
                            canonical_decimal(item) for item in object_values
                        ],
                        "byte_miss_ratio_values": [
                            canonical_decimal(item) for item in byte_values
                        ],
                        "simulator_throughput_mqps_values": [
                            canonical_decimal(item) for item in throughputs
                        ],
                        "cpu_ns_per_request_values": [
                            canonical_decimal(item) for item in cpu_values
                        ],
                        "throughput_median_mqps": canonical_decimal(median),
                        "throughput_floor_mqps": _floor_90(median),
                    }
            policy_comparisons[trace_id] = comparison_cells
            if reference is not None:
                reference[trace_id] = reference_cells
        comparisons[policy] = policy_comparisons
        if reference is not None:
            references[policy] = reference
    first = inputs.r2[0]
    record: dict[str, object] = {
        "schema_version": 1,
        "task_manifest_sha256": inputs.manifest["manifest_sha256"],
        "source_receipt_sha256": first.receipt["source_receipt_sha256"],
        "source_commit": inputs.manifest["source_commit"],
        "binary_sha256": first.receipt["binary_snapshot_sha256"],
        "evaluator_sha256s": _hash_mapping(
            first.receipt["evaluator"], "R2 evaluator"
        ),
        "scientific_input_sha256s": first.scientific_input_sha256s,
        "host_fingerprint": _host(first.receipt["host"], "R2"),
        "repetitions": 5,
        "cache_fractions": fractions,
        "references": references,
        "comparisons": comparisons,
        "r0_receipt_sha256s": {
            policy: inputs.r0[policy].receipt["receipt_sha256"]
            for policy in COMPARISON_POLICIES
        },
        "input_receipt_sha256s": sorted(
            str(item.receipt["receipt_sha256"]) for item in inputs.r2
        ),
    }
    _forbid_campaign_scalars(record)
    record["calibration_sha256"] = record_sha256(record, "calibration_sha256")
    return record


def _write_calibration(
    output: Path,
    record: dict[str, object],
    revalidate: Callable[[], None],
) -> Path:
    candidate = Path(output).absolute()
    if candidate.name in {"", ".", ".."}:
        raise CalibrationError("calibration output must name a new file")
    parent = candidate.parent
    try:
        parent_metadata = parent.lstat()
        if parent.is_symlink() or not stat.S_ISDIR(parent_metadata.st_mode):
            raise CalibrationError("calibration output parent must be a real directory")
        descriptor = os.open(
            parent,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    except CalibrationError:
        raise
    except OSError as error:
        raise CalibrationError("calibration output parent must exist") from error
    temporary_name: str | None = None
    temporary_identity: tuple[int, int] | None = None
    published = False
    complete = False
    output_identity: tuple[int, int] | None = None
    serialized = (
        json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    serialized_sha256 = hashlib.sha256(serialized).hexdigest()
    try:
        retained = os.fstat(descriptor)
        canonical_raw = os.readlink(f"/proc/self/fd/{descriptor}")
        if canonical_raw.endswith(" (deleted)"):
            raise CalibrationError("calibration output parent was removed")
        canonical_parent = Path(canonical_raw).resolve(strict=True)
        path_metadata = parent.lstat()
        if (
            parent.is_symlink()
            or (retained.st_dev, retained.st_ino)
            != (path_metadata.st_dev, path_metadata.st_ino)
            or parent.resolve(strict=True) != canonical_parent
        ):
            raise CalibrationError("calibration output parent binding is invalid")
        try:
            os.stat(candidate.name, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise CalibrationError("refusing to replace existing calibration output")
        for _attempt in range(16):
            name = f".{candidate.name}.{secrets.token_hex(16)}.tmp"
            try:
                temporary_descriptor = os.open(
                    name,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=descriptor,
                )
            except FileExistsError:
                continue
            temporary_name = name
            break
        else:
            raise CalibrationError("cannot allocate calibration staging file")
        try:
            with os.fdopen(temporary_descriptor, "wb") as stream:
                stream.write(serialized)
                stream.flush()
                os.fchmod(stream.fileno(), 0o400)
                os.fsync(stream.fileno())
                temporary_metadata = os.fstat(stream.fileno())
        except BaseException:
            raise
        temporary_identity = (temporary_metadata.st_dev, temporary_metadata.st_ino)
        revalidate()
        path_metadata = parent.lstat()
        if (
            parent.is_symlink()
            or (path_metadata.st_dev, path_metadata.st_ino)
            != (retained.st_dev, retained.st_ino)
        ):
            raise CalibrationError("calibration output parent changed before publication")
        try:
            os.link(
                temporary_name,
                candidate.name,
                src_dir_fd=descriptor,
                dst_dir_fd=descriptor,
                follow_symlinks=False,
            )
        except FileExistsError as error:
            raise CalibrationError(
                "refusing to replace existing calibration output"
            ) from error
        published = True
        output_identity = temporary_identity
        os.fsync(descriptor)
        output_descriptor = os.open(
            candidate.name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=descriptor,
        )
        try:
            output_metadata = os.fstat(output_descriptor)
            with os.fdopen(output_descriptor, "rb") as stream:
                output_descriptor = -1
                observed = stream.read()
        finally:
            if output_descriptor >= 0:
                os.close(output_descriptor)
        if (
            (output_metadata.st_dev, output_metadata.st_ino) != output_identity
            or stat.S_IMODE(output_metadata.st_mode) != 0o400
            or observed != serialized
        ):
            raise CalibrationError("immutable calibration output changed during publication")
        revalidate()
        path_parent = parent.lstat()
        public = candidate.lstat()
        if (
            parent.is_symlink()
            or candidate.is_symlink()
            or (path_parent.st_dev, path_parent.st_ino)
            != (retained.st_dev, retained.st_ino)
            or (public.st_dev, public.st_ino) != output_identity
            or stat.S_IMODE(public.st_mode) != 0o400
            or candidate.resolve(strict=True) != canonical_parent / candidate.name
        ):
            raise CalibrationError("calibration publication parent binding changed")
        complete = True
        return candidate
    except CalibrationError:
        raise
    except (OSError, ValueError) as error:
        raise CalibrationError(f"calibration publication failed: {error}") from error
    finally:
        if published and not complete and output_identity is not None:
            try:
                quarantine_unlink(
                    descriptor,
                    candidate.name,
                    output_identity,
                    sha256=serialized_sha256,
                    raw=serialized,
                )
            except (OSError, ValueError):
                pass
        if temporary_name is not None and temporary_identity is not None:
            try:
                temporary = os.stat(
                    temporary_name, dir_fd=descriptor, follow_symlinks=False
                )
                if (temporary.st_dev, temporary.st_ino) == temporary_identity:
                    os.unlink(temporary_name, dir_fd=descriptor)
            except FileNotFoundError:
                pass
        os.close(descriptor)


def _object(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise CalibrationError(f"{label} must be an object")
    return value


def _canonical_decimal(value: object, label: str) -> Decimal:
    if type(value) is not str:
        raise CalibrationError(f"{label} must be a canonical Decimal string")
    try:
        parsed = Decimal(value)
    except DecimalException as error:
        raise CalibrationError(f"{label} must be a canonical Decimal string") from error
    if not parsed.is_finite() or canonical_decimal(parsed) != value:
        raise CalibrationError(f"{label} must be a canonical Decimal string")
    return parsed


def _difference(left: Decimal, right: Decimal) -> str:
    context = scientific_decimal_context()
    least_exponent = min(left.as_tuple().exponent, right.as_tuple().exponent)
    greatest_digit = max(left.adjusted(), right.adjusted())
    context.prec = max(context.prec, greatest_digit - least_exponent + 3)
    with localcontext(context):
        return canonical_decimal(left - right)


def _audit_states(
    independent_audit: Mapping[str, object] | None,
) -> tuple[str | None, str | None]:
    if independent_audit is None:
        return None, None
    if not isinstance(independent_audit, Mapping) or set(independent_audit) != {
        "metadata",
        "complexity",
    }:
        raise CalibrationError("independent audit keys mismatch")
    metadata = independent_audit["metadata"]
    complexity = independent_audit["complexity"]
    if (
        type(metadata) is not str
        or metadata not in _AUDIT_STATES
        or type(complexity) is not str
        or complexity not in _AUDIT_STATES
    ):
        raise CalibrationError("independent audit state is invalid")
    return metadata, complexity


def _audit_gated(observed: bool, state: str | None) -> bool | None:
    if not observed or state == "rejected":
        return False
    if state == "accepted":
        return True
    return None


def _phase_gaps(
    candidate: Mapping[str, object], reference: Mapping[str, object]
) -> dict[str, object]:
    candidate_bins = candidate.get("bins")
    reference_bins = reference.get("bins")
    if not isinstance(candidate_bins, list) or not isinstance(reference_bins, list):
        raise CalibrationError("phase facts must contain bins")
    if len(candidate_bins) != len(reference_bins):
        raise CalibrationError("candidate and reference phase bins differ")
    gaps: list[dict[str, object]] = []
    for candidate_bin, reference_bin in zip(candidate_bins, reference_bins, strict=True):
        candidate_value = _object(candidate_bin, "candidate phase bin")
        reference_value = _object(reference_bin, "reference phase bin")
        phase = candidate_value.get("index")
        requests = candidate_value.get("requests")
        request_bytes = candidate_value.get("request_bytes")
        if (
            type(phase) is not int
            or type(requests) is not int
            or requests <= 0
            or type(request_bytes) is not int
            or request_bytes <= 0
            or reference_value.get("index") != phase
            or type(reference_value.get("requests")) is not int
            or int(reference_value["requests"]) <= 0
            or type(reference_value.get("request_bytes")) is not int
            or int(reference_value["request_bytes"]) <= 0
        ):
            raise CalibrationError("candidate and reference phase facts differ")
        candidate_object_misses = candidate_value.get("object_misses")
        candidate_byte_misses = candidate_value.get("byte_misses")
        reference_object_misses = reference_value.get("object_misses")
        reference_byte_misses = reference_value.get("byte_misses")
        if any(
            type(value) is not int or value < 0
            for value in (
                candidate_object_misses,
                candidate_byte_misses,
                reference_object_misses,
                reference_byte_misses,
            )
        ):
            raise CalibrationError("phase miss facts are invalid")
        candidate_object_misses = int(candidate_object_misses)
        candidate_byte_misses = int(candidate_byte_misses)
        reference_object_misses = int(reference_object_misses)
        reference_byte_misses = int(reference_byte_misses)
        if (
            candidate_object_misses > requests
            or candidate_byte_misses > request_bytes
            or reference_object_misses
            > reference_value["requests"]
            or reference_byte_misses
            > reference_value["request_bytes"]
        ):
            raise CalibrationError("phase miss facts exceed their denominators")
        with localcontext(scientific_decimal_context()):
            object_gap = Decimal(candidate_object_misses) / Decimal(requests) - (
                Decimal(reference_object_misses)
                / Decimal(reference_value["requests"])
            )
            byte_gap = Decimal(candidate_byte_misses) / Decimal(request_bytes) - (
                Decimal(reference_byte_misses)
                / Decimal(reference_value["request_bytes"])
            )
        gaps.append(
            {
                "index": phase,
                "object_miss_ratio_gap": canonical_decimal(object_gap),
                "byte_miss_ratio_gap": canonical_decimal(byte_gap),
            }
        )
    return {"bins": gaps}


def _validated_calibration_cell(
    calibration: Mapping[str, object],
    *,
    reference_policy: str,
    trace_id: str,
    fraction: str,
) -> tuple[Mapping[str, object], Mapping[str, object]]:
    if set(calibration) != _CALIBRATION_KEYS:
        raise CalibrationError("calibration record keys mismatch")
    if (
        calibration.get("schema_version") != 1
        or calibration.get("repetitions") != 5
        or calibration.get("cache_fractions") != ["0.01", "0.05", "0.1"]
        or calibration.get("calibration_sha256")
        != record_sha256(calibration, "calibration_sha256")
    ):
        raise CalibrationError("calibration record identity is invalid")
    for name in (
        "task_manifest_sha256",
        "source_receipt_sha256",
        "binary_sha256",
    ):
        _hash(calibration.get(name), f"calibration {name}")
    source_commit = calibration.get("source_commit")
    if (
        type(source_commit) is not str
        or len(source_commit) != 40
        or any(character not in "0123456789abcdef" for character in source_commit)
    ):
        raise CalibrationError("calibration source commit is invalid")
    _hash_mapping(calibration.get("evaluator_sha256s"), "calibration evaluator")
    _hash_mapping(
        calibration.get("scientific_input_sha256s"),
        "calibration scientific input",
    )
    _host(calibration.get("host_fingerprint"), "calibration")
    r0_hashes = calibration.get("r0_receipt_sha256s")
    if not isinstance(r0_hashes, dict) or set(r0_hashes) != set(
        COMPARISON_POLICIES
    ):
        raise CalibrationError("calibration R0 policy hashes are incomplete")
    for policy, digest in r0_hashes.items():
        _hash(digest, f"calibration R0 {policy}")
    input_hashes = calibration.get("input_receipt_sha256s")
    if (
        not isinstance(input_hashes, list)
        or len(input_hashes) != 14
        or input_hashes != sorted(input_hashes)
        or len(set(input_hashes)) != len(input_hashes)
    ):
        raise CalibrationError("calibration input receipt hashes are invalid")
    for digest in input_hashes:
        _hash(digest, "calibration input receipt")
    references = _object(calibration.get("references"), "calibration references")
    if set(references) != set(REFERENCE_POLICIES):
        raise CalibrationError("calibration reference policies are incomplete")
    reference = _object(references.get(reference_policy), "reference policy")
    metadata = _object(reference.get("metadata"), "reference metadata")
    if set(metadata) != {
        "bytes_per_object",
        "global_bytes",
        "measurement_sha256",
        "probe_evidence",
        "independent_audit",
    } or metadata.get("independent_audit") != "pending_independent_review":
        raise CalibrationError("calibration reference metadata is invalid")
    _canonical_decimal(metadata.get("bytes_per_object"), "reference metadata")
    if type(metadata.get("global_bytes")) is not int or metadata["global_bytes"] < 0:
        raise CalibrationError("calibration reference global metadata is invalid")
    measurement_hash = _hash(
        metadata.get("measurement_sha256"), "reference metadata measurement"
    )
    probe = _object(metadata.get("probe_evidence"), "reference probe evidence")
    probe_keys = {
        "r0_receipt_sha256",
        "metadata_command_sha256",
        "stdout_sha256",
        "metadata_measurement_sha256",
        "metadata_probe_source_sha256",
        "metadata_probe_binary_sha256",
        "metadata_interposer_source_sha256",
        "metadata_interposer_binary_sha256",
    }
    if set(probe) != probe_keys:
        raise CalibrationError("calibration reference probe evidence is invalid")
    for name in probe_keys:
        _hash(probe[name], f"reference probe evidence {name}")
    if (
        probe["r0_receipt_sha256"] != r0_hashes[reference_policy]
        or probe["stdout_sha256"] != measurement_hash
        or probe["metadata_measurement_sha256"] != measurement_hash
    ):
        raise CalibrationError("calibration reference probe evidence differs")
    reference_trace = _object(reference.get(trace_id), "reference trace")
    reference_cell = _object(reference_trace.get(fraction), "reference cell")
    reference_cell_keys = {
        "repetitions",
        "object_miss_ratio_values",
        "byte_miss_ratio_values",
        "simulator_throughput_mqps_values",
        "cpu_ns_per_request_values",
        "throughput_median_mqps",
        "throughput_floor_mqps",
    }
    if set(reference_cell) != reference_cell_keys or reference_cell.get(
        "repetitions"
    ) != 5:
        raise CalibrationError("calibration reference cell is invalid")
    parsed_distributions: dict[str, list[Decimal]] = {}
    for name in (
        "object_miss_ratio_values",
        "byte_miss_ratio_values",
        "simulator_throughput_mqps_values",
        "cpu_ns_per_request_values",
    ):
        values = reference_cell.get(name)
        if not isinstance(values, list) or len(values) != 5:
            raise CalibrationError("calibration reference distribution is incomplete")
        parsed = [_canonical_decimal(item, f"reference {name}") for item in values]
        if parsed != sorted(parsed):
            raise CalibrationError("calibration reference distribution is unsorted")
        parsed_distributions[name] = parsed
    median = parsed_distributions["simulator_throughput_mqps_values"][2]
    if (
        reference_cell.get("throughput_median_mqps")
        != canonical_decimal(median)
        or reference_cell.get("throughput_floor_mqps") != _floor_90(median)
    ):
        raise CalibrationError("calibration throughput median or floor is inconsistent")
    comparisons = _object(
        calibration.get("comparisons"), "calibration comparisons"
    )
    if set(comparisons) != set(COMPARISON_POLICIES):
        raise CalibrationError("calibration comparison policies mismatch")
    for policy in COMPARISON_POLICIES:
        policy_record = _object(comparisons[policy], "comparison policy")
        trace_record = _object(policy_record.get(trace_id), "comparison trace")
        cell = _object(trace_record.get(fraction), "comparison cell")
        repetitions = 5 if policy in REFERENCE_POLICIES else 1
        if set(cell) != {
            "repetitions",
            "object_miss_ratio_values",
            "byte_miss_ratio_values",
            "phase_values",
        } or cell.get("repetitions") != repetitions:
            raise CalibrationError("calibration comparison cell is invalid")
        for name in ("object_miss_ratio_values", "byte_miss_ratio_values"):
            values = cell.get(name)
            if not isinstance(values, list) or len(values) != repetitions:
                raise CalibrationError("calibration comparison distribution is incomplete")
            parsed = [_canonical_decimal(item, f"comparison {name}") for item in values]
            if parsed != sorted(parsed):
                raise CalibrationError("calibration comparison distribution is unsorted")
        phases = cell.get("phase_values")
        if not isinstance(phases, list) or len(phases) != repetitions:
            raise CalibrationError("calibration phase comparison is incomplete")
    _forbid_campaign_scalars(calibration)
    return reference, reference_cell


def compare_constraints(
    candidate_measurement: Mapping[str, object],
    candidate_r0: Mapping[str, object],
    contract: Mapping[str, object],
    calibration: Mapping[str, object],
    independent_audit: Mapping[str, object] | None,
) -> dict[str, object]:
    metadata_audit, complexity_audit = _audit_states(independent_audit)
    measurement = _object(candidate_measurement, "candidate measurement")
    r0 = _object(candidate_r0, "candidate R0")
    declared = _object(contract, "policy contract")
    policy = measurement.get("policy")
    trace_id = measurement.get("trace_id")
    fraction = measurement.get("cache_fraction")
    if (
        type(policy) is not str
        or r0.get("policy") != policy
        or type(trace_id) is not str
        or type(fraction) is not str
    ):
        raise CalibrationError("candidate policy or cell binding mismatch")
    try:
        validated_contract = _validate_contract(declared, expected_policy=policy)
    except ValueError as error:
        raise CalibrationError(f"policy contract is invalid: {error}") from error
    reference_policy = validated_contract.reference_policy
    reference, reference_cell = _validated_calibration_cell(
        calibration,
        reference_policy=reference_policy,
        trace_id=trace_id,
        fraction=fraction,
    )
    throughput = _canonical_decimal(
        measurement.get("simulator_throughput_mqps"), "candidate throughput"
    )
    floor = _canonical_decimal(
        reference_cell.get("throughput_floor_mqps"), "reference throughput floor"
    )

    reference_metadata = _object(reference.get("metadata"), "reference metadata")
    object_limit = _canonical_decimal(
        reference_metadata.get("bytes_per_object"), "reference object metadata"
    )
    global_limit = reference_metadata.get("global_bytes")
    candidate_object = _canonical_decimal(
        measurement.get("metadata_bytes_per_object"), "candidate object metadata"
    )
    candidate_global = measurement.get("global_metadata_bytes")
    measured = _object(r0.get("measured_metadata"), "candidate R0 metadata")
    if (
        measurement.get("rung") not in {"r2", "r3"}
        or throughput <= 0
        or object_limit < 0
        or candidate_object < 0
        or type(global_limit) is not int
        or global_limit < 0
        or type(candidate_global) is not int
        or candidate_global < 0
        or measured.get("bytes_per_object")
        != measurement.get("metadata_bytes_per_object")
        or measured.get("global_bytes") != candidate_global
        or measured.get("measurement_sha256")
        != measurement.get("metadata_measurement_sha256")
    ):
        raise CalibrationError("candidate metadata binding mismatch")
    normalized_contract = {
        "policy": validated_contract.policy,
        "reference_policy": validated_contract.reference_policy,
        "policy_source": validated_contract.policy_source,
        "object_metadata_bytes": validated_contract.object_metadata_bytes,
        "global_metadata_bytes": validated_contract.global_metadata_bytes,
        "global_metadata_evidence": [
            {"source": source, "line": line, "expression": expression}
            for source, line, expression in validated_contract.global_metadata_evidence
        ],
        "update_complexity": validated_contract.update_complexity,
    }
    r0_declared = r0.get("declared_metadata")
    declared_consistent = (
        isinstance(r0_declared, Mapping)
        and dict(r0_declared) == normalized_contract
        and candidate_object
        <= Decimal(validated_contract.object_metadata_bytes)
        and candidate_global <= validated_contract.global_metadata_bytes
    )

    checks = _object(r0.get("checks"), "candidate R0 checks")
    operational: dict[str, bool | None] = {}
    for result_name, check_name in (
        ("capacity", "capacity"),
        ("determinism", "deterministic"),
        ("sanitizer", "sanitizer"),
    ):
        value = checks.get(check_name)
        if value is not True and value is not False and value is not None:
            raise CalibrationError(f"candidate R0 {check_name} fact is invalid")
        operational[result_name] = value

    comparisons = _object(calibration.get("comparisons"), "calibration comparisons")
    if set(comparisons) != set(COMPARISON_POLICIES):
        raise CalibrationError("calibration comparison policies mismatch")
    object_gaps: dict[str, list[str]] = {}
    byte_gaps: dict[str, list[str]] = {}
    phase_gaps: dict[str, list[dict[str, object]]] = {}
    candidate_object_miss = _canonical_decimal(
        measurement.get("object_miss_ratio"), "candidate object miss ratio"
    )
    candidate_byte_miss = _canonical_decimal(
        measurement.get("byte_miss_ratio"), "candidate byte miss ratio"
    )
    if not (
        Decimal(0) <= candidate_object_miss <= Decimal(1)
        and Decimal(0) <= candidate_byte_miss <= Decimal(1)
    ):
        raise CalibrationError("candidate miss ratios are outside [0, 1]")
    candidate_phase = _object(
        measurement.get("phase_diagnostic"), "candidate phase facts"
    )
    for comparison_policy in COMPARISON_POLICIES:
        policy_record = _object(comparisons[comparison_policy], "comparison policy")
        trace_record = _object(policy_record.get(trace_id), "comparison trace")
        cell = _object(trace_record.get(fraction), "comparison cell")
        object_values = cell.get("object_miss_ratio_values")
        byte_values = cell.get("byte_miss_ratio_values")
        phase_values = cell.get("phase_values")
        if (
            not isinstance(object_values, list)
            or not isinstance(byte_values, list)
            or not isinstance(phase_values, list)
            or not object_values
            or len(object_values) != len(byte_values)
            or len(object_values) != len(phase_values)
        ):
            raise CalibrationError("comparison distributions are incomplete")
        object_gaps[comparison_policy] = [
            _difference(
                candidate_object_miss,
                _canonical_decimal(value, "comparison object miss ratio"),
            )
            for value in object_values
        ]
        byte_gaps[comparison_policy] = [
            _difference(
                candidate_byte_miss,
                _canonical_decimal(value, "comparison byte miss ratio"),
            )
            for value in byte_values
        ]
        phase_gaps[comparison_policy] = [
            _phase_gaps(candidate_phase, _object(value, "comparison phase facts"))
            for value in phase_values
        ]

    return {
        "throughput": throughput >= floor,
        "object_metadata": _audit_gated(
            candidate_object <= object_limit, metadata_audit
        ),
        "global_metadata": _audit_gated(
            candidate_global <= global_limit, metadata_audit
        ),
        "declared_metadata_consistency": _audit_gated(
            declared_consistent, metadata_audit
        ),
        "complexity": (
            True
            if complexity_audit == "accepted"
            else False
            if complexity_audit == "rejected"
            else None
        ),
        **operational,
        "object_miss_gaps": object_gaps,
        "byte_miss_gaps": byte_gaps,
        "phase_gaps": phase_gaps,
    }


def calibrate(
    task_manifest: Path,
    r0_receipts: Sequence[Path],
    receipts: Sequence[Path],
    output: Path,
) -> dict[str, object]:
    inputs = _load_inputs(task_manifest, r0_receipts, receipts)
    signature = _input_signature(inputs)
    record = _freeze(inputs)

    def revalidate() -> None:
        observed = _load_inputs(task_manifest, r0_receipts, receipts)
        if _input_signature(observed) != signature or _freeze(observed) != record:
            raise CalibrationError("calibration input binding changed")

    _write_calibration(output, record, revalidate)
    return record
