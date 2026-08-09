from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, localcontext
from pathlib import Path

from .calibration_evidence import (
    COMPARISON_POLICIES,
    REFERENCE_POLICIES,
    CalibrationError,
    _CALIBRATION_KEYS,
    _R2_EVALUATOR_KEYS,
)
from .calibration_evidence import (
    _canonical_decimal,
    _flat_scientific_hashes,
    _floor_90,
    _forbid_campaign_scalars,
    _hash,
    _hash_mapping,
    _host,
    load_bound_calibration,
)
from .diagnostics import PhaseBin
from .records import canonical_bytes, canonical_decimal, scientific_decimal_context
from .scope import _validate_contract


_AUDIT_STATES = {"accepted", "rejected", "pending_independent_review"}
_PROBE_EVIDENCE_KEYS = {
    "r0_receipt_sha256",
    "metadata_command_sha256",
    "stdout_sha256",
    "metadata_measurement_sha256",
    "metadata_probe_source_sha256",
    "metadata_probe_binary_sha256",
    "metadata_interposer_source_sha256",
    "metadata_interposer_binary_sha256",
}
_REFERENCE_CELL_KEYS = {
    "repetitions",
    "input_receipt_sha256s",
    "measurement_sha256s",
    "object_miss_ratio_values",
    "byte_miss_ratio_values",
    "simulator_throughput_mqps_values",
    "cpu_ns_per_request_values",
    "throughput_median_mqps",
    "throughput_floor_mqps",
}
_COMPARISON_CELL_KEYS = {
    "repetitions",
    "input_receipt_sha256s",
    "measurement_sha256s",
    "object_miss_ratio_values",
    "byte_miss_ratio_values",
    "phase_values",
}
_PHASE_FACT_KEYS = {
    "phase_sha256",
    "request_count",
    "object_misses",
    "request_bytes",
    "byte_misses",
    "bins",
}
_TRANSFER_DERIVATION = "0.90 * minimum(source throughput_median_mqps)"
_TRANSFER_CELL_KEYS = {
    "derivation",
    "source_cells",
    "minimum_throughput_median_mqps",
    "throughput_floor_mqps",
}
_TRANSFER_SOURCE_KEYS = {
    "trace_id",
    "reference_cell_sha256",
    "input_receipt_sha256s",
    "measurement_sha256s",
    "throughput_median_mqps",
}


@dataclass(frozen=True)
class ValidatedCalibration:
    record: Mapping[str, object]
    references: Mapping[str, object]
    transfer_constraints: Mapping[str, object]
    comparisons: Mapping[str, object]
    trace_ids: tuple[str, ...]
    fractions: tuple[str, ...]


def _object(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise CalibrationError(f"{label} must be an object")
    return value


def _hash_list(
    value: object, *, count: int, label: str
) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) != count:
        raise CalibrationError(f"{label} must contain exactly {count} hashes")
    hashes = tuple(_hash(item, label) for item in value)
    if list(hashes) != sorted(hashes) or len(set(hashes)) != count:
        raise CalibrationError(f"{label} hashes must be sorted and unique")
    return hashes


def _decimal_distribution(
    value: object,
    *,
    count: int,
    label: str,
    minimum: Decimal,
    maximum: Decimal | None = None,
    strict_minimum: bool = False,
) -> tuple[Decimal, ...]:
    if not isinstance(value, list) or len(value) != count:
        raise CalibrationError(f"{label} must contain exactly {count} values")
    parsed = tuple(_canonical_decimal(item, label) for item in value)
    if list(parsed) != sorted(parsed):
        raise CalibrationError(f"{label} values must be sorted")
    for item in parsed:
        if (
            item < minimum
            or (strict_minimum and item == minimum)
            or (maximum is not None and item > maximum)
        ):
            raise CalibrationError(f"{label} value is outside its valid range")
    return parsed


def _phase_fact(value: object) -> Mapping[str, object]:
    fact = _object(value, "calibration phase fact")
    if set(fact) != _PHASE_FACT_KEYS:
        raise CalibrationError("calibration phase fact keys mismatch")
    _hash(fact.get("phase_sha256"), "calibration phase fact")
    bins = fact.get("bins")
    if not isinstance(bins, list) or len(bins) != 16:
        raise CalibrationError("calibration phase fact requires sixteen bins")
    parsed = [PhaseBin.from_record(_object(item, "calibration phase bin")) for item in bins]
    if [item.index for item in parsed] != list(range(16)):
        raise CalibrationError("calibration phase bins are out of order")
    totals = {
        "request_count": sum(item.requests for item in parsed),
        "object_misses": sum(item.object_misses for item in parsed),
        "request_bytes": sum(item.request_bytes for item in parsed),
        "byte_misses": sum(item.byte_misses for item in parsed),
    }
    for name, total in totals.items():
        if type(fact.get(name)) is not int or fact[name] != total:
            raise CalibrationError(
                "calibration phase totals require exact integers matching their bins"
            )
    return fact


def _rounded_phase_ratio(numerator: object, denominator: object) -> Decimal:
    if (
        type(numerator) is not int
        or numerator < 0
        or type(denominator) is not int
        or denominator <= 0
    ):
        raise CalibrationError("calibration phase ratio counters are invalid")
    with localcontext(scientific_decimal_context()):
        return (Decimal(numerator) / Decimal(denominator)).quantize(
            Decimal("0.0001")
        )


def _metadata(
    value: object,
    *,
    policy: str,
    r0_hashes: Mapping[str, object],
) -> Mapping[str, object]:
    metadata = _object(value, "calibration reference metadata")
    if set(metadata) != {
        "bytes_per_object",
        "global_bytes",
        "measurement_sha256",
        "probe_evidence",
        "independent_audit",
    }:
        raise CalibrationError("calibration reference metadata keys mismatch")
    if metadata.get("independent_audit") != "pending_independent_review":
        raise CalibrationError("calibration reference audit must remain pending")
    if _canonical_decimal(metadata.get("bytes_per_object"), "reference metadata") < 0:
        raise CalibrationError("calibration reference object metadata is negative")
    if type(metadata.get("global_bytes")) is not int or metadata["global_bytes"] < 0:
        raise CalibrationError("calibration reference global metadata is invalid")
    measurement = _hash(
        metadata.get("measurement_sha256"), "reference metadata measurement"
    )
    probe = _object(metadata.get("probe_evidence"), "reference probe evidence")
    if set(probe) != _PROBE_EVIDENCE_KEYS:
        raise CalibrationError("calibration reference probe evidence keys mismatch")
    for name in _PROBE_EVIDENCE_KEYS:
        _hash(probe[name], f"reference probe evidence {name}")
    if (
        probe["r0_receipt_sha256"] != r0_hashes[policy]
        or probe["stdout_sha256"] != measurement
        or probe["metadata_measurement_sha256"] != measurement
    ):
        raise CalibrationError("calibration reference probe evidence differs")
    return metadata


def _validate_transfer_constraints(
    value: object,
    *,
    references: Mapping[str, object],
    trace_ids: tuple[str, ...],
    fractions: tuple[str, ...],
) -> Mapping[str, object]:
    transfer = _object(value, "calibration transfer constraints")
    if set(transfer) != set(REFERENCE_POLICIES):
        raise CalibrationError("calibration transfer policies are incomplete")
    expected_policy_keys = {"metadata", *fractions}
    for policy in REFERENCE_POLICIES:
        policy_transfer = _object(transfer[policy], "calibration transfer policy")
        reference = _object(references[policy], "calibration reference policy")
        if set(policy_transfer) != expected_policy_keys:
            raise CalibrationError("calibration transfer fractions are incomplete")
        if policy_transfer.get("metadata") != reference.get("metadata"):
            raise CalibrationError("calibration transfer metadata projection differs")
        for fraction in fractions:
            cell = _object(policy_transfer[fraction], "calibration transfer cell")
            if (
                set(cell) != _TRANSFER_CELL_KEYS
                or cell.get("derivation") != _TRANSFER_DERIVATION
            ):
                raise CalibrationError("calibration transfer cell keys mismatch")
            raw_sources = cell.get("source_cells")
            if not isinstance(raw_sources, list) or len(raw_sources) != len(trace_ids):
                raise CalibrationError(
                    "calibration transfer source cells are incomplete"
                )
            medians: list[Decimal] = []
            observed_trace_ids: list[str] = []
            for source in raw_sources:
                projected = _object(source, "calibration transfer source cell")
                if set(projected) != _TRANSFER_SOURCE_KEYS:
                    raise CalibrationError("calibration transfer source keys mismatch")
                trace_id = projected.get("trace_id")
                if type(trace_id) is not str or trace_id not in trace_ids:
                    raise CalibrationError(
                        "calibration transfer source trace is invalid"
                    )
                observed_trace_ids.append(trace_id)
                reference_trace = _object(
                    reference[trace_id], "calibration reference trace"
                )
                reference_cell = _object(
                    reference_trace[fraction], "calibration reference cell"
                )
                expected_cell_hash = hashlib.sha256(
                    canonical_bytes(reference_cell)
                ).hexdigest()
                if (
                    _hash(
                        projected.get("reference_cell_sha256"),
                        "calibration transfer source cell",
                    )
                    != expected_cell_hash
                    or projected.get("input_receipt_sha256s")
                    != reference_cell.get("input_receipt_sha256s")
                    or projected.get("measurement_sha256s")
                    != reference_cell.get("measurement_sha256s")
                    or projected.get("throughput_median_mqps")
                    != reference_cell.get("throughput_median_mqps")
                ):
                    raise CalibrationError(
                        "calibration transfer source projection differs"
                    )
                medians.append(
                    _canonical_decimal(
                        projected.get("throughput_median_mqps"),
                        "calibration transfer source median",
                    )
                )
            if observed_trace_ids != list(trace_ids):
                raise CalibrationError("calibration transfer sources are out of order")
            minimum = min(medians)
            if cell.get("minimum_throughput_median_mqps") != canonical_decimal(
                minimum
            ) or cell.get("throughput_floor_mqps") != _floor_90(minimum):
                raise CalibrationError("calibration transfer arithmetic differs")
    return transfer


def validate_calibration(value: Mapping[str, object]) -> ValidatedCalibration:
    if set(value) != _CALIBRATION_KEYS:
        raise CalibrationError("calibration record keys mismatch")
    if (
        type(value.get("schema_version")) is not int
        or value.get("schema_version") != 1
        or type(value.get("repetitions")) is not int
        or value.get("repetitions") != 5
        or value.get("cache_fractions") != ["0.01", "0.05", "0.1"]
    ):
        raise CalibrationError("calibration record identity is invalid")
    for name in ("task_manifest_sha256", "source_receipt_sha256", "binary_sha256"):
        _hash(value.get(name), f"calibration {name}")
    source_commit = value.get("source_commit")
    if (
        type(source_commit) is not str
        or len(source_commit) != 40
        or any(character not in "0123456789abcdef" for character in source_commit)
    ):
        raise CalibrationError("calibration source commit is invalid")
    _hash_mapping(
        value.get("evaluator_sha256s"),
        "calibration evaluator",
        expected_keys=_R2_EVALUATOR_KEYS,
    )
    _flat_scientific_hashes(
        value.get("scientific_input_sha256s"), "calibration scientific input"
    )
    _host(value.get("host_fingerprint"), "calibration")
    r0_hashes = _object(value.get("r0_receipt_sha256s"), "calibration R0 hashes")
    if set(r0_hashes) != set(COMPARISON_POLICIES):
        raise CalibrationError("calibration R0 policy hashes are incomplete")
    for policy in COMPARISON_POLICIES:
        _hash(r0_hashes[policy], f"calibration R0 {policy}")
    if len(set(r0_hashes.values())) != len(COMPARISON_POLICIES):
        raise CalibrationError("calibration R0 receipt hashes are duplicated")
    input_receipts = _hash_list(
        value.get("input_receipt_sha256s"),
        count=14,
        label="calibration input receipt",
    )
    references = _object(value.get("references"), "calibration references")
    comparisons = _object(value.get("comparisons"), "calibration comparisons")
    if set(references) != set(REFERENCE_POLICIES):
        raise CalibrationError("calibration reference policies are incomplete")
    if set(comparisons) != set(COMPARISON_POLICIES):
        raise CalibrationError("calibration comparison policies are incomplete")
    trace_sets: list[set[str]] = []
    for policy in REFERENCE_POLICIES:
        reference = _object(references[policy], "calibration reference policy")
        if "metadata" not in reference:
            raise CalibrationError("calibration reference metadata is missing")
        _metadata(reference["metadata"], policy=policy, r0_hashes=r0_hashes)
        trace_sets.append(set(reference) - {"metadata"})
    if not trace_sets[0] or any(traces != trace_sets[0] for traces in trace_sets[1:]):
        raise CalibrationError("calibration reference trace sets differ")
    trace_ids = tuple(sorted(trace_sets[0]))
    if any(type(trace_id) is not str or not trace_id for trace_id in trace_ids):
        raise CalibrationError("calibration trace ID is invalid")
    if any(set(_object(comparisons[policy], "comparison policy")) != set(trace_ids) for policy in COMPARISON_POLICIES):
        raise CalibrationError("calibration comparison trace sets differ")
    fractions = ("0.01", "0.05", "0.1")
    policy_receipts: dict[str, tuple[str, ...]] = {}
    measurement_hashes_seen: set[str] = set()
    phase_hashes_seen: set[str] = set()
    for policy in COMPARISON_POLICIES:
        repetitions = 5 if policy in REFERENCE_POLICIES else 1
        policy_record = _object(comparisons[policy], "comparison policy")
        projected_receipts: tuple[str, ...] | None = None
        for trace_id in trace_ids:
            trace = _object(policy_record[trace_id], "comparison trace")
            if set(trace) != set(fractions):
                raise CalibrationError("calibration comparison fractions are incomplete")
            for fraction in fractions:
                cell = _object(trace[fraction], "comparison cell")
                if (
                    set(cell) != _COMPARISON_CELL_KEYS
                    or type(cell.get("repetitions")) is not int
                    or cell.get("repetitions") != repetitions
                ):
                    raise CalibrationError("calibration comparison cell keys mismatch")
                receipts = _hash_list(
                    cell.get("input_receipt_sha256s"),
                    count=repetitions,
                    label="comparison input receipt",
                )
                measurements = _hash_list(
                    cell.get("measurement_sha256s"),
                    count=repetitions,
                    label="comparison measurement",
                )
                if projected_receipts is None:
                    projected_receipts = receipts
                elif receipts != projected_receipts:
                    raise CalibrationError("comparison receipt projection differs by cell")
                if not set(receipts) <= set(input_receipts):
                    raise CalibrationError("comparison receipt projection is forged")
                if measurement_hashes_seen.intersection(measurements):
                    raise CalibrationError("comparison measurement hash is reused")
                measurement_hashes_seen.update(measurements)
                object_distribution = _decimal_distribution(
                    cell.get("object_miss_ratio_values"),
                    count=repetitions,
                    label="comparison object miss ratio",
                    minimum=Decimal(0),
                    maximum=Decimal(1),
                )
                byte_distribution = _decimal_distribution(
                    cell.get("byte_miss_ratio_values"),
                    count=repetitions,
                    label="comparison byte miss ratio",
                    minimum=Decimal(0),
                    maximum=Decimal(1),
                )
                phases = cell.get("phase_values")
                if not isinstance(phases, list) or len(phases) != repetitions:
                    raise CalibrationError("calibration phase distribution is incomplete")
                phase_facts = tuple(_phase_fact(item) for item in phases)
                phase_hashes = tuple(
                    str(item["phase_sha256"]) for item in phase_facts
                )
                if list(phase_hashes) != sorted(phase_hashes):
                    raise CalibrationError("calibration phase facts are unsorted")
                if phase_hashes_seen.intersection(phase_hashes):
                    raise CalibrationError("calibration phase hash is reused")
                phase_hashes_seen.update(phase_hashes)
                phase_object_distribution = tuple(
                    sorted(
                        _rounded_phase_ratio(
                            item["object_misses"], item["request_count"]
                        )
                        for item in phase_facts
                    )
                )
                phase_byte_distribution = tuple(
                    sorted(
                        _rounded_phase_ratio(
                            item["byte_misses"], item["request_bytes"]
                        )
                        for item in phase_facts
                    )
                )
                if (
                    phase_object_distribution != object_distribution
                    or phase_byte_distribution != byte_distribution
                ):
                    raise CalibrationError(
                        "calibration phase ratios contradict primary distributions"
                    )
        assert projected_receipts is not None
        policy_receipts[policy] = projected_receipts
    if set().union(*(set(items) for items in policy_receipts.values())) != set(input_receipts):
        raise CalibrationError("calibration receipt projections do not cover inputs")
    for policy in REFERENCE_POLICIES:
        reference = _object(references[policy], "reference policy")
        comparison = _object(comparisons[policy], "comparison policy")
        for trace_id in trace_ids:
            reference_trace = _object(reference[trace_id], "reference trace")
            comparison_trace = _object(comparison[trace_id], "comparison trace")
            if set(reference_trace) != set(fractions):
                raise CalibrationError("calibration reference fractions are incomplete")
            for fraction in fractions:
                cell = _object(reference_trace[fraction], "reference cell")
                comparison_cell = _object(comparison_trace[fraction], "comparison cell")
                if (
                    set(cell) != _REFERENCE_CELL_KEYS
                    or type(cell.get("repetitions")) is not int
                    or cell.get("repetitions") != 5
                ):
                    raise CalibrationError("calibration reference cell keys mismatch")
                receipts = _hash_list(
                    cell.get("input_receipt_sha256s"),
                    count=5,
                    label="reference input receipt",
                )
                measurements = _hash_list(
                    cell.get("measurement_sha256s"),
                    count=5,
                    label="reference measurement",
                )
                if (
                    receipts != tuple(comparison_cell["input_receipt_sha256s"])
                    or measurements != tuple(comparison_cell["measurement_sha256s"])
                    or cell.get("object_miss_ratio_values")
                    != comparison_cell.get("object_miss_ratio_values")
                    or cell.get("byte_miss_ratio_values")
                    != comparison_cell.get("byte_miss_ratio_values")
                ):
                    raise CalibrationError("reference and comparison projections differ")
                throughputs = _decimal_distribution(
                    cell.get("simulator_throughput_mqps_values"),
                    count=5,
                    label="reference throughput",
                    minimum=Decimal(0),
                    strict_minimum=True,
                )
                _decimal_distribution(
                    cell.get("cpu_ns_per_request_values"),
                    count=5,
                    label="reference CPU per request",
                    minimum=Decimal(0),
                    strict_minimum=True,
                )
                median = throughputs[2]
                if (
                    cell.get("throughput_median_mqps") != canonical_decimal(median)
                    or cell.get("throughput_floor_mqps") != _floor_90(median)
                ):
                    raise CalibrationError("calibration throughput threshold is inconsistent")
    transfer_constraints = _validate_transfer_constraints(
        value.get("transfer_constraints"),
        references=references,
        trace_ids=trace_ids,
        fractions=fractions,
    )
    _forbid_campaign_scalars(value)
    return ValidatedCalibration(
        value,
        references,
        transfer_constraints,
        comparisons,
        trace_ids,
        fractions,
    )


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


def _difference(left: Decimal, right: Decimal) -> str:
    context = scientific_decimal_context()
    least_exponent = min(left.as_tuple().exponent, right.as_tuple().exponent)
    greatest_digit = max(left.adjusted(), right.adjusted())
    context.prec = max(context.prec, greatest_digit - least_exponent + 3)
    with localcontext(context):
        return canonical_decimal(left - right)


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
        values = (
            candidate_value.get("object_misses"),
            candidate_value.get("byte_misses"),
            reference_value.get("object_misses"),
            reference_value.get("byte_misses"),
        )
        if any(type(item) is not int or item < 0 for item in values):
            raise CalibrationError("phase miss facts are invalid")
        candidate_object_misses, candidate_byte_misses, reference_object_misses, reference_byte_misses = (int(item) for item in values)
        if (
            candidate_object_misses > requests
            or candidate_byte_misses > request_bytes
            or reference_object_misses > reference_value["requests"]
            or reference_byte_misses > reference_value["request_bytes"]
        ):
            raise CalibrationError("phase miss facts exceed their denominators")
        with localcontext(scientific_decimal_context()):
            object_gap = Decimal(candidate_object_misses) / Decimal(requests) - (
                Decimal(reference_object_misses) / Decimal(reference_value["requests"])
            )
            byte_gap = Decimal(candidate_byte_misses) / Decimal(request_bytes) - (
                Decimal(reference_byte_misses) / Decimal(reference_value["request_bytes"])
            )
        gaps.append(
            {
                "index": phase,
                "object_miss_ratio_gap": canonical_decimal(object_gap),
                "byte_miss_ratio_gap": canonical_decimal(byte_gap),
            }
        )
    return {"bins": gaps}


def compare_constraints(
    candidate_measurement: Mapping[str, object],
    candidate_r0: Mapping[str, object],
    contract: Mapping[str, object],
    calibration_path: Path,
    expected_calibration_sha256: str,
    independent_audit: Mapping[str, object] | None,
) -> dict[str, object]:
    bound = load_bound_calibration(
        calibration_path,
        expected_calibration_sha256=expected_calibration_sha256,
    )
    calibration = validate_calibration(bound.record)
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
    reference = _object(calibration.references[reference_policy], "reference policy")
    reference_trace = _object(reference.get(trace_id), "reference trace")
    reference_cell = _object(reference_trace.get(fraction), "reference cell")
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
        or measured.get("bytes_per_object") != measurement.get("metadata_bytes_per_object")
        or measured.get("global_bytes") != candidate_global
        or measured.get("measurement_sha256") != measurement.get("metadata_measurement_sha256")
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
        and candidate_object <= Decimal(validated_contract.object_metadata_bytes)
        and candidate_global <= validated_contract.global_metadata_bytes
    )
    checks = _object(r0.get("checks"), "candidate R0 checks")
    operational: dict[str, bool | None] = {}
    for result_name, check_name in (
        ("capacity", "capacity"),
        ("determinism", "deterministic"),
        ("sanitizer", "sanitizer"),
    ):
        item = checks.get(check_name)
        if item is not True and item is not False and item is not None:
            raise CalibrationError(f"candidate R0 {check_name} fact is invalid")
        operational[result_name] = item
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
        policy_record = _object(calibration.comparisons[comparison_policy], "comparison policy")
        trace_record = _object(policy_record.get(trace_id), "comparison trace")
        cell = _object(trace_record.get(fraction), "comparison cell")
        object_values = cell["object_miss_ratio_values"]
        byte_values = cell["byte_miss_ratio_values"]
        phase_values = cell["phase_values"]
        assert isinstance(object_values, list)
        assert isinstance(byte_values, list)
        assert isinstance(phase_values, list)
        object_gaps[comparison_policy] = [
            _difference(candidate_object_miss, _canonical_decimal(item, "comparison object miss ratio"))
            for item in object_values
        ]
        byte_gaps[comparison_policy] = [
            _difference(candidate_byte_miss, _canonical_decimal(item, "comparison byte miss ratio"))
            for item in byte_values
        ]
        phase_gaps[comparison_policy] = [
            _phase_gaps(candidate_phase, _object(item, "comparison phase facts"))
            for item in phase_values
        ]
    return {
        "throughput": throughput >= floor,
        "object_metadata": _audit_gated(candidate_object <= object_limit, metadata_audit),
        "global_metadata": _audit_gated(candidate_global <= global_limit, metadata_audit),
        "declared_metadata_consistency": _audit_gated(declared_consistent, metadata_audit),
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


def compare_transfer_constraints(
    candidate_measurement: Mapping[str, object],
    candidate_r0: Mapping[str, object],
    contract: Mapping[str, object],
    calibration_path: Path,
    expected_calibration_sha256: str,
    independent_audit: Mapping[str, object] | None,
) -> dict[str, object]:
    bound = load_bound_calibration(
        calibration_path,
        expected_calibration_sha256=expected_calibration_sha256,
    )
    calibration = validate_calibration(bound.record)
    metadata_audit, complexity_audit = _audit_states(independent_audit)
    measurement = _object(candidate_measurement, "candidate transfer measurement")
    r0 = _object(candidate_r0, "candidate R0")
    declared = _object(contract, "policy contract")
    policy = measurement.get("policy")
    trace_id = measurement.get("trace_id")
    fraction = measurement.get("cache_fraction")
    if (
        type(policy) is not str
        or r0.get("policy") != policy
        or type(trace_id) is not str
        or not trace_id
        or type(fraction) is not str
    ):
        raise CalibrationError("candidate transfer policy or cell binding mismatch")
    try:
        validated_contract = _validate_contract(declared, expected_policy=policy)
    except ValueError as error:
        raise CalibrationError(f"policy contract is invalid: {error}") from error
    reference_policy = validated_contract.reference_policy
    transfer_policy = _object(
        calibration.transfer_constraints[reference_policy],
        "transfer reference policy",
    )
    transfer_cell = _object(
        transfer_policy.get(fraction), "transfer reference fraction"
    )
    reference_metadata = _object(
        transfer_policy.get("metadata"), "transfer reference metadata"
    )
    throughput = _canonical_decimal(
        measurement.get("simulator_throughput_mqps"), "candidate throughput"
    )
    floor = _canonical_decimal(
        transfer_cell.get("throughput_floor_mqps"),
        "transfer throughput floor",
    )
    object_limit = _canonical_decimal(
        reference_metadata.get("bytes_per_object"), "reference object metadata"
    )
    global_limit = reference_metadata.get("global_bytes")
    candidate_object = _canonical_decimal(
        measurement.get("metadata_bytes_per_object"), "candidate object metadata"
    )
    candidate_object_miss = _canonical_decimal(
        measurement.get("object_miss_ratio"), "candidate object miss ratio"
    )
    candidate_byte_miss = _canonical_decimal(
        measurement.get("byte_miss_ratio"), "candidate byte miss ratio"
    )
    candidate_global = measurement.get("global_metadata_bytes")
    measured = _object(r0.get("measured_metadata"), "candidate R0 metadata")
    if (
        measurement.get("rung") != "r3"
        or throughput <= 0
        or floor <= 0
        or object_limit < 0
        or candidate_object < 0
        or not Decimal(0) <= candidate_object_miss <= Decimal(1)
        or not Decimal(0) <= candidate_byte_miss <= Decimal(1)
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
        raise CalibrationError("candidate transfer metadata binding mismatch")
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
        and candidate_object <= Decimal(validated_contract.object_metadata_bytes)
        and candidate_global <= validated_contract.global_metadata_bytes
    )
    checks = _object(r0.get("checks"), "candidate R0 checks")
    operational: dict[str, bool | None] = {}
    for result_name, check_name in (
        ("capacity", "capacity"),
        ("determinism", "deterministic"),
        ("sanitizer", "sanitizer"),
    ):
        item = checks.get(check_name)
        if item is not True and item is not False and item is not None:
            raise CalibrationError(f"candidate R0 {check_name} fact is invalid")
        operational[result_name] = item
    return {
        "reference_policy": reference_policy,
        "cache_fraction": fraction,
        "transfer_constraint": dict(transfer_cell),
        "reference_metadata": dict(reference_metadata),
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
        "object_miss_gaps": None,
        "byte_miss_gaps": None,
        "phase_gaps": None,
    }
