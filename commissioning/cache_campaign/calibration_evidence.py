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


COMPARISON_POLICIES = ("LRU", "ARC", "WTinyLFU", "Sieve", "S3FIFO", "BeladySize")
REFERENCE_POLICIES = ("Sieve", "S3FIFO")
_FRACTIONS = (Decimal("0.01"), Decimal("0.05"), Decimal("0.10"))
_FORBIDDEN_KEYS = {
    "score",
    "reward",
    "objective",
    "aggregate",
    "pass",
    "rank",
    "ranking",
}
_R0_EVALUATOR_KEYS = {
    "evaluate_sha256",
    "scope_sha256",
    "evidence_sha256",
    "r0_probes_sha256",
    "cachesim_sha256",
    "linux_subreaper_sha256",
}
_R2_EVALUATOR_KEYS = {
    *_R0_EVALUATOR_KEYS,
    "portfolio_sha256",
    "portfolio_evidence_sha256",
    "oracle_sha256",
    "records_sha256",
    "diagnostics_sha256",
    "source_lock_sha256",
    "run_aros_cache_eval_sha256",
}
_R0_SCIENTIFIC_INPUT_KEYS = {
    "fixed_time_interposer",
    "release_archive",
    "release_cmake_cache",
}
_REQUIRED_SCIENTIFIC_HEADERS = {
    "header:libCacheSim/include/libCacheSim.h",
    "header:libCacheSim/bin/cachesim/cache_init.h",
}
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
    "transfer_constraints",
    "comparisons",
    "r0_receipt_sha256s",
    "input_receipt_sha256s",
    "calibration_sha256",
}


class CalibrationError(ValueError):
    pass


@dataclass(frozen=True)
class BoundCalibration:
    path: Path
    identity: tuple[int, int]
    mode: int
    size_bytes: int
    file_sha256: str
    calibration_sha256: str
    record: Mapping[str, object]


def load_bound_calibration(
    path: Path, *, expected_calibration_sha256: str
) -> BoundCalibration:
    if (
        type(expected_calibration_sha256) is not str
        or HEX64.fullmatch(expected_calibration_sha256) is None
    ):
        raise CalibrationError("expected calibration digest must be a lowercase SHA-256")
    try:
        bound = read_bound_json_object(
            Path(path), max_bytes=64 * 1024 * 1024
        )
    except (OSError, TypeError, ValueError) as error:
        raise CalibrationError(f"calibration path binding failed: {error}") from error
    if bound.mode != 0o400:
        raise CalibrationError("calibration record mode must be read-only 0400")
    actual = record_sha256(bound.value, "calibration_sha256")
    if (
        bound.value.get("calibration_sha256") != actual
        or actual != expected_calibration_sha256
    ):
        raise CalibrationError("calibration digest differs from external expected digest")
    return BoundCalibration(
        path=bound.path,
        identity=bound.identity,
        mode=bound.mode,
        size_bytes=bound.size_bytes,
        file_sha256=bound.sha256,
        calibration_sha256=actual,
        record=bound.value,
    )


@dataclass(frozen=True)
class _R0Input:
    path: Path
    receipt: dict[str, object]
    binding: FileBinding
    source: dict[str, object]
    artifacts: dict[str, FileBinding]
    retained: dict[str, FileBinding]
    scientific_input_sha256s: dict[str, str]


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


def _hash_mapping(
    value: object,
    label: str,
    *,
    expected_keys: set[str] | None = None,
) -> dict[str, str]:
    if not isinstance(value, dict) or not value:
        raise CalibrationError(f"{label} hash mapping is missing")
    if expected_keys is not None and set(value) != expected_keys:
        raise CalibrationError(f"{label} hash mapping keys mismatch")
    result: dict[str, str] = {}
    for key, item in sorted(value.items()):
        if type(key) is not str or not key:
            raise CalibrationError(f"{label} hash mapping key is invalid")
        result[key] = _hash(item, f"{label} {key}")
    return result


def _flat_scientific_hashes(value: object, label: str) -> dict[str, str]:
    result = _hash_mapping(value, label)
    if not _R0_SCIENTIFIC_INPUT_KEYS < set(result):
        raise CalibrationError(f"{label} hash mapping is incomplete")
    header_keys = set(result) - _R0_SCIENTIFIC_INPUT_KEYS
    if not _REQUIRED_SCIENTIFIC_HEADERS <= header_keys:
        raise CalibrationError(f"{label} required header hashes are missing")
    for key in header_keys:
        relative = key.removeprefix("header:")
        pure = PurePosixPath(relative)
        if (
            not key.startswith("header:")
            or pure.is_absolute()
            or ".." in pure.parts
            or not (
                relative.startswith("libCacheSim/include/")
                or relative == "libCacheSim/bin/cachesim/cache_init.h"
            )
        ):
            raise CalibrationError(f"{label} header key is invalid")
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
    return _flat_scientific_hashes(
        dict(sorted(result.items())), "R2 scientific input"
    )


def _r0_scientific_hashes(
    artifacts: Mapping[str, FileBinding],
) -> dict[str, str]:
    names = {
        "fixed_time_interposer": "fixed_time_interposer_binary",
        "release_archive": "release_archive",
        "release_cmake_cache": "release_cmake_cache",
    }
    if not set(names.values()) <= set(artifacts):
        raise CalibrationError("R0 scientific input hash mapping is incomplete")
    return {
        name: artifacts[artifact].sha256
        for name, artifact in sorted(names.items())
    }


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
        scientific = _r0_scientific_hashes(bindings)
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
        evaluator = _hash_mapping(
            receipt.get("evaluator"),
            "R0 evaluator",
            expected_keys=_R0_EVALUATOR_KEYS,
        )
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
        return _R0Input(
            binding.path,
            receipt,
            binding,
            source,
            bindings,
            retained,
            scientific,
        )
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
    measurement["_calibration_input_receipt_sha256"] = receipt["receipt_sha256"]
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
        evaluator = _hash_mapping(
            receipt.get("evaluator"),
            "R2 evaluator",
            expected_keys=_R2_EVALUATOR_KEYS,
        )
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
            _hash_mapping(
                item.receipt["evaluator"],
                "R0 evaluator",
                expected_keys=_R0_EVALUATOR_KEYS,
            )
            for item in r0_inputs
        ]
        r0_scientific = [
            item.scientific_input_sha256s for item in r0_inputs
        ]
        if (
            len(source_hashes) != 1
            or len(candidates) != 1
            or len(binaries) != 1
            or len(r0_hosts) != 1
            or any(value != r0_evaluators[0] for value in r0_evaluators[1:])
            or any(value != r0_scientific[0] for value in r0_scientific[1:])
        ):
            raise CalibrationError(
                "R0 source, candidate, binary, host, evaluator, or scientific input is mixed"
            )
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
        evaluator = _hash_mapping(
            first.receipt["evaluator"],
            "R2 evaluator",
            expected_keys=_R2_EVALUATOR_KEYS,
        )
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
            if _hash_mapping(
                item.receipt["evaluator"],
                "R2 evaluator",
                expected_keys=_R2_EVALUATOR_KEYS,
            ) != evaluator:
                raise CalibrationError("R2 evaluator version is mixed")
            if item.scientific_input_sha256s != scientific:
                raise CalibrationError("R2 scientific input hashes are mixed")
            if item.apparatus != apparatus:
                raise CalibrationError("R2 phase apparatus is mixed")
            if _host(item.receipt["host"], "R2") != host:
                raise CalibrationError("R2 host fingerprint is mixed")
        if canonical_bytes(host) not in r0_hosts:
            raise CalibrationError("R0 and R2 host fingerprints differ")
        evaluator_projection = {
            name: evaluator[name] for name in sorted(_R0_EVALUATOR_KEYS)
        }
        if any(value != evaluator_projection for value in r0_evaluators):
            raise CalibrationError("R0 and R2 evaluator dependency maps differ")
        scientific_projection = {
            name: scientific[name]
            for name in sorted(_R0_SCIENTIFIC_INPUT_KEYS)
        }
        if any(value != scientific_projection for value in r0_scientific):
            raise CalibrationError("R0 and R2 scientific input maps differ")
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
