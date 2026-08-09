from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from .calibration_evidence import (
    COMPARISON_POLICIES,
    REFERENCE_POLICIES,
    CalibrationError,
    _FRACTIONS,
    _Inputs,
    _R0Input,
    _R2_EVALUATOR_KEYS,
    _canonical_decimal,
    _floor_90,
    _forbid_campaign_scalars,
    _hash,
    _hash_mapping,
    _host,
    _input_signature,
    _load_inputs,
    _write_calibration,
)
from .records import canonical_decimal, record_sha256


def _probe_evidence(r0: _R0Input) -> dict[str, str]:
    receipt = r0.receipt
    commands = receipt.get("commands")
    matches = (
        [
            item
            for item in commands
            if isinstance(item, dict) and item.get("label") == "metadata-run"
        ]
        if isinstance(commands, list)
        else []
    )
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
        "r0_receipt_sha256": _hash(receipt.get("receipt_sha256"), "R0 receipt"),
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
    grouped: dict[tuple[str, str, str], list[dict[str, object]]] = {}
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
                measurement_hashes = sorted(
                    str(item["measurement_sha256"]) for item in values
                )
                receipt_hashes = sorted(
                    str(item["_calibration_input_receipt_sha256"])
                    for item in values
                )
                if (
                    len(set(measurement_hashes)) != expected_repetitions
                    or len(set(receipt_hashes)) != expected_repetitions
                ):
                    raise CalibrationError("calibration cell evidence hashes are duplicated")
                object_values = sorted(
                    _canonical_decimal(item["object_miss_ratio"], "object miss ratio")
                    for item in values
                )
                byte_values = sorted(
                    _canonical_decimal(item["byte_miss_ratio"], "byte miss ratio")
                    for item in values
                )
                phases = sorted(
                    (item["_calibration_phase_facts"] for item in values),
                    key=lambda item: str(item["phase_sha256"]),  # type: ignore[index]
                )
                comparison_cells[fraction] = {
                    "repetitions": expected_repetitions,
                    "input_receipt_sha256s": receipt_hashes,
                    "measurement_sha256s": measurement_hashes,
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
                        _canonical_decimal(item["cpu_ns_per_request"], "CPU per request")
                        for item in values
                    )
                    median = throughputs[2]
                    reference_cells[fraction] = {
                        "repetitions": 5,
                        "input_receipt_sha256s": receipt_hashes,
                        "measurement_sha256s": measurement_hashes,
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
            first.receipt["evaluator"],
            "R2 evaluator",
            expected_keys=_R2_EVALUATOR_KEYS,
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
