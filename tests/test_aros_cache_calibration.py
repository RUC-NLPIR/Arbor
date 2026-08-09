from __future__ import annotations

import hashlib
import json
import platform
import sys
from dataclasses import dataclass, replace
from decimal import Context, Inexact, localcontext
from pathlib import Path

import pytest

from commissioning.cache_campaign import portfolio as portfolio_module
from commissioning.cache_campaign import calibrate as calibration_module
from commissioning.cache_campaign.cachesim import ChildResult
from commissioning.cache_campaign.calibrate import (
    CalibrationError,
    calibrate,
    compare_constraints,
)
from commissioning.cache_campaign.portfolio import evaluate_portfolio
from commissioning.cache_campaign.records import record_sha256, sha256_file
from scripts import calibrate_aros_cache_baselines as calibration_cli
from tests.test_aros_cache_evaluator import (
    PortfolioRun,
    git,
    portfolio_checkout,
    portfolio_manifest,
    portfolio_program,
    portfolio_r0_receipt,
    portfolio_source_receipt,
    write_record,
)


POLICIES = ("LRU", "ARC", "WTinyLFU", "Sieve", "S3FIFO", "BeladySize")


def forbidden_key(value: object) -> str | None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"score", "reward", "objective", "aggregate", "pass"}:
                return key
            nested = forbidden_key(item)
            if nested is not None:
                return nested
    elif isinstance(value, list):
        for item in value:
            nested = forbidden_key(item)
            if nested is not None:
                return nested
    return None


class CalibrationRun(PortfolioRun):
    def __init__(self, throughput: str) -> None:
        super().__init__()
        self.throughput = throughput

    def __call__(
        self,
        argv: list[str],
        output_dir: Path,
        *,
        cwd: Path | None,
        timeout_seconds: float,
        max_output_bytes: int,
    ) -> ChildResult:
        result = super().__call__(
            argv,
            output_dir,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
        )
        if portfolio_program(argv) != "cachesim":
            return result
        actual = argv[2:] if argv[0] == "/usr/bin/env" else argv
        policy = actual[3]
        raw = result.stdout_path.read_text().replace(
            "Sieve cache", f"{policy} cache"
        ).replace("1.25 MQPS", f"{self.throughput} MQPS").encode()
        result.stdout_path.write_bytes(raw)
        side_effect = Path(
            next(item for item in argv if item.startswith("--output=")).split(
                "=", 1
            )[1]
        )
        side_effect.write_bytes(raw)
        return replace(
            result,
            stdout_bytes=len(raw),
            stdout_sha256=hashlib.sha256(raw).hexdigest(),
        )


def normalize_r0(path: Path, checkout: Path, policy: str) -> Path:
    value = json.loads(path.read_text())
    value["policy"] = policy
    value["policy_source_sha256"] = sha256_file(
        checkout / f"libCacheSim/cache/eviction/{policy}.c"
    )
    command = value["commands"][0]
    command["argv"][4] = policy
    command["command_sha256"] = record_sha256(command, "command_sha256")

    binary_path = path.parent / "artifact_snapshots/release_cachesim"
    binary_path.chmod(0o600)
    binary_path.write_bytes(b"#!/bin/sh\nexit 0\n")
    binary_path.chmod(0o400)
    metadata = binary_path.stat()
    binary = value["artifact_snapshots"]["release_cachesim"]
    binary["snapshot_identity"] = {
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
    }
    binary["size_bytes"] = metadata.st_size
    binary["sha256"] = sha256_file(binary_path)
    value["binary_sha256"] = binary["sha256"]
    value["binary_post_run_sha256"] = binary["sha256"]
    evaluator_paths = {
        "evaluate": Path(portfolio_module.__file__).with_name("evaluate.py"),
        "scope": Path(portfolio_module.__file__).with_name("scope.py"),
        "evidence": Path(portfolio_module.__file__).with_name("evidence.py"),
        "r0_probes": Path(portfolio_module.__file__).with_name("r0_probes.py"),
        "cachesim": Path(portfolio_module.__file__).with_name("cachesim.py"),
        "linux_subreaper": Path(portfolio_module.__file__).with_name(
            "linux_subreaper.py"
        ),
    }
    for name, source in evaluator_paths.items():
        artifact_path = path.parent / f"artifact_snapshots/evaluator_{name}"
        artifact_path.chmod(0o600)
        artifact_path.write_bytes(source.read_bytes())
        artifact_path.chmod(0o400)
        artifact_metadata = artifact_path.stat()
        artifact = value["artifact_snapshots"][f"evaluator_{name}"]
        artifact["snapshot_identity"] = {
            "device": artifact_metadata.st_dev,
            "inode": artifact_metadata.st_ino,
        }
        artifact["size_bytes"] = artifact_metadata.st_size
        artifact["sha256"] = sha256_file(artifact_path)
        value["evaluator"][f"{name}_sha256"] = artifact["sha256"]
    value["host"] = {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": sys.version,
    }
    return write_record(path, value, "receipt_sha256")


@dataclass(frozen=True)
class CampaignEvidence:
    manifest: Path
    r0_receipts: dict[str, Path]
    receipts: list[Path]
    trace_ids: list[str]
    root: Path


@pytest.fixture(scope="module")
def campaign_evidence(tmp_path_factory: pytest.TempPathFactory) -> CampaignEvidence:
    root = tmp_path_factory.mktemp("cache-calibration")
    checkout, lock, _candidate, _tree = portfolio_checkout(root)
    for policy in POLICIES:
        source = checkout / f"libCacheSim/cache/eviction/{policy}.c"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(f"/* {policy} baseline */\n")
    git(checkout, "add", ".")
    git(checkout, "commit", "--amend", "-qm", "all comparison baselines")
    candidate = git(checkout, "rev-parse", "HEAD")
    tree = git(checkout, "rev-parse", "HEAD^{tree}")
    lock["commit"] = candidate
    lock["tree"] = tree
    lock["comparison_policies"] = list(POLICIES)

    patcher = pytest.MonkeyPatch()
    patcher.setattr("commissioning.cache_campaign.evaluate.SOURCE_LOCK", lock)
    patcher.setattr("commissioning.cache_campaign.portfolio.SOURCE_LOCK", lock)
    source_receipt = portfolio_source_receipt(root / "source.json", lock)
    task_root = root / "task"
    task_root.mkdir()
    manifest, trace_ids = portfolio_manifest(
        task_root / "manifests/task.json", root / "traces", candidate
    )
    manifest_value = json.loads(manifest.read_text())
    manifest_value["traces"] = manifest_value["traces"][:1]
    manifest = write_record(manifest, manifest_value, "manifest_sha256")
    trace_ids = trace_ids[:1]
    r0_receipts = {
        policy: normalize_r0(
            portfolio_r0_receipt(
                root / "r0" / policy,
                source_receipt,
                candidate,
                tree,
                checkout,
            ),
            checkout,
            policy,
        )
        for policy in POLICIES
    }
    receipts: list[Path] = []
    for policy in POLICIES:
        throughputs = ["18.00", "19.00", "20.00", "21.00", "22.00"]
        if policy not in {"Sieve", "S3FIFO"}:
            throughputs = ["20.00"]
        for repetition, throughput in enumerate(throughputs):
            output = root / "r2" / policy / str(repetition)
            output.parent.mkdir(parents=True, exist_ok=True)
            evaluate_portfolio(
                rung="r2",
                task_root=task_root,
                task_manifest=manifest,
                checkout=checkout,
                candidate=candidate,
                policy=policy,
                source_receipt=source_receipt,
                r0_receipt=r0_receipts[policy],
                output=output,
                run=CalibrationRun(throughput),
            )
            receipts.append(output / "receipt.json")
    yield CampaignEvidence(manifest, r0_receipts, receipts, trace_ids, root)
    patcher.undo()


def candidate_measurement(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "rung": "r2",
        "trace_id": "dev-0",
        "policy": "CandidatePolicy",
        "cache_fraction": "0.01",
        "object_miss_ratio": "0.21",
        "byte_miss_ratio": "0.31",
        "simulator_throughput_mqps": "18",
        "metadata_bytes_per_object": "2",
        "global_metadata_bytes": 24,
        "metadata_measurement_sha256": "a" * 64,
        "phase_diagnostic": {
            "bins": [
                {
                    "index": 0,
                    "requests": 10,
                    "object_misses": 3,
                    "request_bytes": 100,
                    "byte_misses": 30,
                }
            ]
        },
    }
    value.update(changes)
    return value


def candidate_r0(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "policy": "CandidatePolicy",
        "checks": {
            "capacity": True,
            "deterministic": True,
            "sanitizer": True,
        },
        "measured_metadata": {
            "bytes_per_object": "2",
            "global_bytes": 24,
            "measurement_sha256": "a" * 64,
        },
        "declared_metadata": {
            "policy": "CandidatePolicy",
            "reference_policy": "Sieve",
            "policy_source": "libCacheSim/cache/eviction/CandidatePolicy.c",
            "object_metadata_bytes": 2,
            "global_metadata_bytes": 24,
            "global_metadata_evidence": [
                {
                    "source": "libCacheSim/cache/eviction/CandidatePolicy.c",
                    "line": 10,
                    "expression": "sizeof(CandidatePolicy_params_t)",
                }
            ],
            "update_complexity": "amortized O(1)",
        },
    }
    value.update(changes)
    return value


def contract(**changes: object) -> dict[str, object]:
    value = dict(candidate_r0()["declared_metadata"])  # type: ignore[arg-type]
    value["schema_version"] = 1
    value.update(changes)
    return value


def calibration() -> dict[str, object]:
    comparisons = {
        policy: {
            "dev-0": {
                "0.01": {
                    "repetitions": 5 if policy in {"Sieve", "S3FIFO"} else 1,
                    "object_miss_ratio_values": ["0.2"]
                    * (5 if policy in {"Sieve", "S3FIFO"} else 1),
                    "byte_miss_ratio_values": ["0.3"]
                    * (5 if policy in {"Sieve", "S3FIFO"} else 1),
                    "phase_values": [
                        {
                            "bins": [
                                {
                                    "index": 0,
                                    "requests": 10,
                                    "object_misses": 2,
                                    "request_bytes": 100,
                                    "byte_misses": 20,
                                }
                            ]
                        }
                    ]
                    * (5 if policy in {"Sieve", "S3FIFO"} else 1),
                }
            }
        }
        for policy in POLICIES
    }
    reference = {
        "metadata": {
            "bytes_per_object": "2",
            "global_bytes": 24,
            "measurement_sha256": "b" * 64,
            "probe_evidence": {
                "r0_receipt_sha256": "a" * 64,
                "metadata_command_sha256": "d" * 64,
                "stdout_sha256": "b" * 64,
                "metadata_measurement_sha256": "b" * 64,
                "metadata_probe_source_sha256": "e" * 64,
                "metadata_probe_binary_sha256": "f" * 64,
                "metadata_interposer_source_sha256": "0" * 64,
                "metadata_interposer_binary_sha256": "1" * 64,
            },
            "independent_audit": "pending_independent_review",
        },
        "dev-0": {
            "0.01": {
                "repetitions": 5,
                "object_miss_ratio_values": ["0.2"] * 5,
                "byte_miss_ratio_values": ["0.3"] * 5,
                "simulator_throughput_mqps_values": [
                    "18",
                    "19",
                    "20",
                    "21",
                    "22",
                ],
                "cpu_ns_per_request_values": ["6.25"] * 5,
                "throughput_median_mqps": "20",
                "throughput_floor_mqps": "18",
            }
        },
    }
    value: dict[str, object] = {
        "schema_version": 1,
        "task_manifest_sha256": "1" * 64,
        "source_receipt_sha256": "2" * 64,
        "source_commit": "3" * 40,
        "binary_sha256": "4" * 64,
        "evaluator_sha256s": {
            name: "5" * 64
            for name in (
                "cachesim_sha256",
                "diagnostics_sha256",
                "evaluate_sha256",
                "evidence_sha256",
                "linux_subreaper_sha256",
                "oracle_sha256",
                "portfolio_evidence_sha256",
                "portfolio_sha256",
                "r0_probes_sha256",
                "records_sha256",
                "run_aros_cache_eval_sha256",
                "scope_sha256",
                "source_lock_sha256",
            )
        },
        "scientific_input_sha256s": {
            "fixed_time_interposer": "6" * 64,
            "release_archive": "6" * 64,
            "release_cmake_cache": "6" * 64,
            "header:libCacheSim/include/libCacheSim.h": "6" * 64,
            "header:libCacheSim/bin/cachesim/cache_init.h": "6" * 64,
        },
        "host_fingerprint": {
            "machine": "test",
            "platform": "test",
            "python": "test",
        },
        "repetitions": 5,
        "cache_fractions": ["0.01", "0.05", "0.1"],
        "references": {"Sieve": reference, "S3FIFO": reference},
        "comparisons": comparisons,
        "r0_receipt_sha256s": {
            policy: f"{index + 7:x}" * 64
            for index, policy in enumerate(POLICIES)
        },
        "input_receipt_sha256s": [f"{index + 1:x}" * 64 for index in range(14)],
    }
    value["calibration_sha256"] = record_sha256(value, "calibration_sha256")
    return value


def test_compare_constraints_keeps_audit_gated_facts_separate() -> None:
    accepted = {"metadata": "accepted", "complexity": "accepted"}
    result = compare_constraints(
        candidate_measurement(), candidate_r0(), contract(), calibration(), accepted
    )
    assert result["throughput"] is True
    assert result["object_metadata"] is True
    assert result["global_metadata"] is True
    assert result["declared_metadata_consistency"] is True
    assert result["complexity"] is True
    assert result["capacity"] is True
    assert result["determinism"] is True
    assert result["sanitizer"] is True
    assert result["object_miss_gaps"]["LRU"] == ["0.01"]  # type: ignore[index]
    assert result["byte_miss_gaps"]["LRU"] == ["0.01"]  # type: ignore[index]
    assert result["phase_gaps"]["LRU"] == [  # type: ignore[index]
        {
            "bins": [
                {
                    "index": 0,
                    "object_miss_ratio_gap": "0.1",
                    "byte_miss_ratio_gap": "0.1",
                }
            ]
        }
    ]


@pytest.mark.parametrize(
    ("audit", "metadata", "complexity"),
    [
        (None, None, None),
        (
            {
                "metadata": "pending_independent_review",
                "complexity": "pending_independent_review",
            },
            None,
            None,
        ),
        ({"metadata": "rejected", "complexity": "rejected"}, False, False),
    ],
)
def test_compare_constraints_never_promotes_missing_or_rejected_audit(
    audit: dict[str, str] | None,
    metadata: bool | None,
    complexity: bool | None,
) -> None:
    result = compare_constraints(
        candidate_measurement(), candidate_r0(), contract(), calibration(), audit
    )
    assert result["object_metadata"] is metadata
    assert result["global_metadata"] is metadata
    assert result["declared_metadata_consistency"] is metadata
    assert result["complexity"] is complexity


def test_compare_constraints_reports_observed_failures_without_audit() -> None:
    measurement = candidate_measurement(
        metadata_bytes_per_object="2.1", global_metadata_bytes=25
    )
    r0 = candidate_r0(
        measured_metadata={
            "bytes_per_object": "2.1",
            "global_bytes": 25,
            "measurement_sha256": "a" * 64,
        }
    )
    result = compare_constraints(
        measurement, r0, contract(), calibration(), None
    )
    assert result["object_metadata"] is False
    assert result["global_metadata"] is False


def test_compare_constraints_requires_exact_metadata_probe_binding() -> None:
    with pytest.raises(CalibrationError, match="metadata binding"):
        compare_constraints(
            candidate_measurement(metadata_measurement_sha256="c" * 64),
            candidate_r0(),
            contract(),
            calibration(),
            None,
        )


def test_compare_constraints_rejects_integer_as_boolean_check() -> None:
    r0 = candidate_r0(
        checks={"capacity": 1, "deterministic": True, "sanitizer": True}
    )
    with pytest.raises(CalibrationError, match="capacity"):
        compare_constraints(
            candidate_measurement(), r0, contract(), calibration(), None
        )


def test_declared_metadata_consistency_compares_declared_and_measured_values() -> None:
    declared = contract(object_metadata_bytes=1, global_metadata_bytes=23)
    r0_declaration = dict(declared)
    del r0_declaration["schema_version"]
    r0 = candidate_r0(declared_metadata=r0_declaration)
    result = compare_constraints(
        candidate_measurement(),
        r0,
        declared,
        calibration(),
        {"metadata": "accepted", "complexity": "accepted"},
    )
    assert result["declared_metadata_consistency"] is False


@pytest.mark.parametrize(
    "audit",
    [
        {},
        {"metadata": "accepted"},
        {"metadata": "accepted", "complexity": "accepted", "extra": "accepted"},
        {"metadata": True, "complexity": "accepted"},
        {"metadata": "unknown", "complexity": "accepted"},
    ],
)
def test_compare_constraints_rejects_malformed_audit(audit: object) -> None:
    with pytest.raises(CalibrationError, match="audit"):
        compare_constraints(
            candidate_measurement(),
            candidate_r0(),
            contract(),
            calibration(),
            audit,  # type: ignore[arg-type]
        )


def test_compare_constraints_rejects_malformed_policy_contract() -> None:
    malformed = contract(extra=True)
    with pytest.raises(CalibrationError, match="contract"):
        compare_constraints(
            candidate_measurement(),
            candidate_r0(),
            malformed,
            calibration(),
            {"metadata": "accepted", "complexity": "accepted"},
        )


def test_compare_constraints_rejects_rehashed_calibration_mutation() -> None:
    changed = calibration()
    changed["references"]["Sieve"]["dev-0"]["0.01"][  # type: ignore[index]
        "throughput_floor_mqps"
    ] = "17"
    changed["calibration_sha256"] = record_sha256(
        changed, "calibration_sha256"
    )
    with pytest.raises(CalibrationError, match="calibration"):
        compare_constraints(
            candidate_measurement(),
            candidate_r0(),
            contract(),
            changed,
            {"metadata": "accepted", "complexity": "accepted"},
        )


def test_compare_constraints_decimal_math_ignores_ambient_traps() -> None:
    hostile = Context(prec=2)
    hostile.traps[Inexact] = True
    with localcontext(hostile):
        result = compare_constraints(
            candidate_measurement(),
            candidate_r0(),
            contract(),
            calibration(),
            {"metadata": "accepted", "complexity": "accepted"},
        )
    assert result["object_miss_gaps"]["Sieve"] == ["0.01"] * 5  # type: ignore[index]


def test_calibrate_rejects_wrong_input_counts_before_reading(tmp_path: Path) -> None:
    with pytest.raises(CalibrationError, match="six R0"):
        calibrate(
            tmp_path / "task.json",
            [tmp_path / "r0.json"],
            [],
            tmp_path / "calibration.json",
        )


def test_calibration_public_api_requires_paths(tmp_path: Path) -> None:
    with pytest.raises(CalibrationError):
        calibrate(
            tmp_path / "missing-task.json",
            [tmp_path / f"{policy}.json" for policy in POLICIES],
            [],
            tmp_path / "calibration.json",
        )


def test_calibration_freezes_all_policy_evidence_and_exact_medians(
    campaign_evidence: CampaignEvidence, tmp_path: Path
) -> None:
    output = tmp_path / "baseline-calibration.json"
    frozen = calibrate(
        campaign_evidence.manifest,
        list(campaign_evidence.r0_receipts.values()),
        campaign_evidence.receipts,
        output,
    )
    assert set(frozen) == {
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
    assert frozen == json.loads(output.read_text())
    assert output.stat().st_mode & 0o777 == 0o400
    assert frozen["calibration_sha256"] == record_sha256(
        frozen, "calibration_sha256"
    )
    assert set(frozen["references"]) == {"Sieve", "S3FIFO"}  # type: ignore[arg-type]
    assert set(frozen["comparisons"]) == set(POLICIES)  # type: ignore[arg-type]
    assert set(frozen["r0_receipt_sha256s"]) == set(POLICIES)  # type: ignore[arg-type]
    assert frozen["repetitions"] == 5
    assert frozen["cache_fractions"] == ["0.01", "0.05", "0.1"]
    references = frozen["references"]
    assert isinstance(references, dict)
    cell = references["Sieve"][campaign_evidence.trace_ids[0]]["0.01"]
    assert cell == {
        "repetitions": 5,
        "object_miss_ratio_values": ["0.0625"] * 5,
        "byte_miss_ratio_values": ["0.0625"] * 5,
        "simulator_throughput_mqps_values": ["18", "19", "20", "21", "22"],
        "cpu_ns_per_request_values": ["6.25"] * 5,
        "throughput_median_mqps": "20",
        "throughput_floor_mqps": "18",
    }
    sieve_r0 = json.loads(campaign_evidence.r0_receipts["Sieve"].read_text())
    metadata_command = sieve_r0["commands"][0]
    metadata_probe = sieve_r0["probes"]["metadata"]
    assert references["Sieve"]["metadata"] == {
        "bytes_per_object": "3",
        "global_bytes": 24,
        "measurement_sha256": sieve_r0["measured_metadata"][
            "measurement_sha256"
        ],
        "probe_evidence": {
            "r0_receipt_sha256": sieve_r0["receipt_sha256"],
            "metadata_command_sha256": metadata_command["command_sha256"],
            "stdout_sha256": metadata_command["stdout"]["sha256"],
            "metadata_measurement_sha256": sieve_r0["measured_metadata"][
                "measurement_sha256"
            ],
            "metadata_probe_source_sha256": metadata_probe["source_sha256"],
            "metadata_probe_binary_sha256": metadata_probe["binary"]["sha256"],
            "metadata_interposer_source_sha256": metadata_probe[
                "interposer_source_sha256"
            ],
            "metadata_interposer_binary_sha256": metadata_probe[
                "interposer_binary"
            ]["sha256"],
        },
        "independent_audit": "pending_independent_review",
    }
    comparisons = frozen["comparisons"]
    assert isinstance(comparisons, dict)
    assert len(
        comparisons["S3FIFO"][campaign_evidence.trace_ids[0]]["0.01"][
            "phase_values"
        ]
    ) == 5
    assert len(
        comparisons["LRU"][campaign_evidence.trace_ids[0]]["0.01"][
            "phase_values"
        ]
    ) == 1
    assert forbidden_key(frozen) is None

    source = json.loads(campaign_evidence.receipts[0].read_text())
    assert frozen["binary_sha256"] == source["binary_snapshot_sha256"]
    assert frozen["evaluator_sha256s"] == source["evaluator"]
    expected_scientific = {
        "fixed_time_interposer": source["scientific_inputs"][
            "fixed_time_interposer"
        ]["sha256"],
        "release_archive": source["scientific_inputs"]["release_archive"][
            "sha256"
        ],
        "release_cmake_cache": source["scientific_inputs"][
            "release_cmake_cache"
        ]["sha256"],
        **{
            f"header:{name}": value["sha256"]
            for name, value in source["scientific_inputs"]["headers"].items()
        },
    }
    assert frozen["scientific_input_sha256s"] == dict(
        sorted(expected_scientific.items())
    )


@pytest.mark.parametrize(
    ("kind", "match"),
    [
        ("duplicate", "duplicate"),
        ("missing", "five"),
        ("host", "host"),
        ("evaluator_extra", "evaluator"),
        ("evaluator_missing", "evaluator"),
        ("evaluator_changed", "evaluator"),
        ("scientific_extra", "scientific"),
        ("scientific_missing", "scientific"),
        ("scientific_changed", "scientific"),
        ("r0map", "R0 evidence"),
        ("candidate", "candidate"),
        ("failure", "failure"),
        ("cell", "cell"),
        ("r3", "R2"),
        ("selfhash", "hash"),
    ],
)
def test_calibration_rejects_invalid_or_mixed_evidence(
    campaign_evidence: CampaignEvidence,
    tmp_path: Path,
    kind: str,
    match: str,
) -> None:
    receipts = list(campaign_evidence.receipts)
    changed_path: Path | None = None
    original: bytes | None = None
    if kind == "duplicate":
        receipts[-1] = receipts[0]
    elif kind == "missing":
        receipts = receipts[:-1]
    else:
        changed_path = receipts[0]
        original = changed_path.read_bytes()
        changed = json.loads(original)
        if kind == "host":
            changed["host"]["machine"] = "different-host"
        elif kind == "evaluator_extra":
            changed["evaluator"]["extra_sha256"] = "0" * 64
        elif kind == "evaluator_missing":
            changed["evaluator"].pop("portfolio_sha256")
        elif kind == "evaluator_changed":
            changed["evaluator"]["portfolio_sha256"] = "0" * 64
        elif kind == "scientific_extra":
            changed["scientific_inputs"]["extra"] = {
                "path": "scientific-inputs/extra",
                "sha256": "0" * 64,
            }
        elif kind == "scientific_missing":
            changed["scientific_inputs"].pop("release_archive")
        elif kind == "scientific_changed":
            changed["scientific_inputs"]["release_archive"]["sha256"] = "0" * 64
        elif kind == "r0map":
            name, value = changed["r0_artifact_snapshots"].popitem()
            changed["r0_artifact_snapshots"][f"arbitrary-{name}"] = value
        elif kind == "candidate":
            changed["candidate_commit"] = "0" * 40
        elif kind == "failure":
            changed["failures"] = [
                {"path": "failure.json", "failure_sha256": "0" * 64}
            ]
            changed["failure_hashes"] = ["0" * 64]
        elif kind == "cell":
            changed["measurements"] = changed["measurements"][:-1]
            changed["measurement_hashes"] = changed["measurement_hashes"][:-1]
        elif kind == "r3":
            changed["rung"] = "r3"
        elif kind == "selfhash":
            changed["host"]["machine"] = "unhashed-host"
            changed_path.write_text(json.dumps(changed, sort_keys=True) + "\n")
        if kind != "selfhash":
            write_record(changed_path, changed, "receipt_sha256")
    try:
        with pytest.raises(CalibrationError, match=match):
            calibrate(
                campaign_evidence.manifest,
                list(campaign_evidence.r0_receipts.values()),
                receipts,
                tmp_path / f"{kind}.json",
            )
    finally:
        if changed_path is not None and original is not None:
            changed_path.write_bytes(original)


def test_calibration_rejects_r0_failure_and_output_collision(
    campaign_evidence: CampaignEvidence, tmp_path: Path
) -> None:
    r0_receipts = list(campaign_evidence.r0_receipts.values())
    changed_path = r0_receipts[0]
    original = changed_path.read_bytes()
    changed = json.loads(original)
    changed["checks"]["deterministic"] = False
    write_record(changed_path, changed, "receipt_sha256")
    try:
        with pytest.raises(CalibrationError, match="R0"):
            calibrate(
                campaign_evidence.manifest,
                r0_receipts,
                campaign_evidence.receipts,
                tmp_path / "failed-r0.json",
            )
    finally:
        changed_path.write_bytes(original)

    output = tmp_path / "collision.json"
    output.write_bytes(b"foreign\n")
    with pytest.raises(CalibrationError, match="replace|exist"):
        calibrate(
            campaign_evidence.manifest,
            r0_receipts,
            campaign_evidence.receipts,
            output,
        )
    assert output.read_bytes() == b"foreign\n"


@pytest.mark.parametrize(
    "kind",
    [
        "evaluator_extra",
        "evaluator_missing",
        "evaluator_changed",
        "scientific_extra",
        "scientific_missing",
        "scientific_changed",
    ],
)
def test_calibration_rejects_r0_map_mutations(
    campaign_evidence: CampaignEvidence, tmp_path: Path, kind: str
) -> None:
    path = campaign_evidence.r0_receipts["LRU"]
    original = path.read_bytes()
    changed = json.loads(original)
    if kind == "evaluator_extra":
        changed["evaluator"]["extra_sha256"] = "0" * 64
    elif kind == "evaluator_missing":
        changed["evaluator"].pop("evaluate_sha256")
    elif kind == "evaluator_changed":
        changed["evaluator"]["evaluate_sha256"] = "0" * 64
    elif kind == "scientific_extra":
        changed["artifact_snapshots"]["scientific_extra"] = dict(
            changed["artifact_snapshots"]["release_archive"]
        )
    elif kind == "scientific_missing":
        changed["artifact_snapshots"].pop("release_archive")
    elif kind == "scientific_changed":
        changed["artifact_snapshots"]["release_archive"]["sha256"] = "0" * 64
    write_record(path, changed, "receipt_sha256")
    try:
        with pytest.raises(CalibrationError, match="R0|evaluator|scientific"):
            calibrate(
                campaign_evidence.manifest,
                list(campaign_evidence.r0_receipts.values()),
                campaign_evidence.receipts,
                tmp_path / f"r0-{kind}.json",
            )
    finally:
        path.write_bytes(original)


@pytest.mark.parametrize("kind", ["process", "side_effect"])
def test_calibration_rejects_rehashed_incomplete_process_facts(
    campaign_evidence: CampaignEvidence, tmp_path: Path, kind: str
) -> None:
    root_path = campaign_evidence.receipts[0]
    root_raw = root_path.read_bytes()
    root = json.loads(root_raw)
    summary = root["measurements"][0]
    measurement_path = root_path.parent / summary["path"]
    measurement_raw = measurement_path.read_bytes()
    measurement = json.loads(measurement_raw)
    if kind == "process":
        del measurement["process"]
    else:
        measurement["simulator_output"]["identity"]["inode"] += 1
    write_record(measurement_path, measurement, "measurement_sha256")
    summary["measurement_sha256"] = measurement["measurement_sha256"]
    root["measurement_hashes"][0] = measurement["measurement_sha256"]
    observed = measurement_path.stat()
    inventory = next(
        item for item in root["evidence_inventory"] if item["path"] == summary["path"]
    )
    inventory["size_bytes"] = observed.st_size
    inventory["sha256"] = sha256_file(measurement_path)
    write_record(root_path, root, "receipt_sha256")
    try:
        with pytest.raises(
            CalibrationError, match="measurement keys|process|side-effect"
        ):
            calibrate(
                campaign_evidence.manifest,
                list(campaign_evidence.r0_receipts.values()),
                campaign_evidence.receipts,
                tmp_path / f"incomplete-{kind}.json",
            )
    finally:
        measurement_path.write_bytes(measurement_raw)
        root_path.write_bytes(root_raw)


def test_calibration_median_ignores_ambient_decimal_context(
    campaign_evidence: CampaignEvidence, tmp_path: Path
) -> None:
    hostile = Context(prec=2)
    hostile.traps[Inexact] = True
    with localcontext(hostile):
        frozen = calibrate(
            campaign_evidence.manifest,
            list(campaign_evidence.r0_receipts.values()),
            campaign_evidence.receipts,
            tmp_path / "decimal-context.json",
        )
    references = frozen["references"]
    assert isinstance(references, dict)
    assert references["Sieve"][campaign_evidence.trace_ids[0]]["0.01"][
        "throughput_floor_mqps"
    ] == "18"


@pytest.mark.parametrize("mutation_call", [2, 3])
def test_calibration_rejects_input_replacement_before_and_after_publication(
    campaign_evidence: CampaignEvidence,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation_call: int,
) -> None:
    path = campaign_evidence.receipts[0]
    original = path.read_bytes()
    real_load = calibration_module._load_inputs
    calls = 0

    def replace_before_revalidation(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        if calls == mutation_call:
            path.write_bytes(original + b" \n")
        return real_load(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(calibration_module, "_load_inputs", replace_before_revalidation)
    output = tmp_path / f"binding-{mutation_call}.json"
    try:
        with pytest.raises(CalibrationError, match="binding"):
            calibrate(
                campaign_evidence.manifest,
                list(campaign_evidence.r0_receipts.values()),
                campaign_evidence.receipts,
                output,
            )
    finally:
        path.write_bytes(original)
    assert not output.exists()


def test_calibration_parent_swap_cannot_redirect_publication(
    campaign_evidence: CampaignEvidence,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    displaced = tmp_path / "displaced"
    output = parent / "calibration.json"
    real_load = calibration_module._load_inputs
    calls = 0

    def swap_before_publication(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        observed = real_load(*args, **kwargs)  # type: ignore[arg-type]
        if calls == 2:
            parent.rename(displaced)
            parent.mkdir()
        return observed

    monkeypatch.setattr(calibration_module, "_load_inputs", swap_before_publication)
    with pytest.raises(CalibrationError, match="parent"):
        calibrate(
            campaign_evidence.manifest,
            list(campaign_evidence.r0_receipts.values()),
            campaign_evidence.receipts,
            output,
        )
    assert not output.exists()
    assert not (displaced / "calibration.json").exists()


def test_cli_requires_exact_counts_and_prints_only_path_and_hash(
    campaign_evidence: CampaignEvidence,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "cli-calibration.json"
    argv = ["--task-manifest", str(campaign_evidence.manifest)]
    for path in campaign_evidence.r0_receipts.values():
        argv.extend(("--r0-receipt", str(path)))
    for path in campaign_evidence.receipts:
        argv.extend(("--receipt", str(path)))
    argv.extend(("--output", str(output)))
    assert calibration_cli.main(argv) == 0
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result == {
        "calibration_path": str(output.resolve()),
        "calibration_sha256": json.loads(output.read_text())["calibration_sha256"],
    }
    assert captured.err == ""

    assert calibration_cli.main(
        [
            "--task-manifest",
            str(campaign_evidence.manifest),
            "--r0-receipt",
            str(next(iter(campaign_evidence.r0_receipts.values()))),
            "--receipt",
            str(campaign_evidence.receipts[0]),
            "--output",
            str(tmp_path / "invalid.json"),
        ]
    ) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: invalid command line\n"


@pytest.mark.parametrize("repeated", ["--task-manifest", "--output"])
def test_cli_rejects_repeated_singular_flags(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    repeated: str,
) -> None:
    argv = [
        "--task-manifest",
        str(tmp_path / "task.json"),
        "--output",
        str(tmp_path / "output.json"),
    ]
    argv.extend((repeated, str(tmp_path / "duplicate")))
    for index in range(6):
        argv.extend(("--r0-receipt", str(tmp_path / f"r0-{index}.json")))
    for index in range(14):
        argv.extend(("--receipt", str(tmp_path / f"r2-{index}.json")))
    assert calibration_cli.main(argv) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: invalid command line\n"
