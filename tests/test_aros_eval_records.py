"""Strict visible evaluator record tests."""

from __future__ import annotations

import copy
import inspect
from itertools import product

import pytest

import arbor.aros.eval_records as eval_records_module
from arbor.aros.eval_records import (
    build_measurement_receipt,
    parse_scalar_metric,
    parse_visible_manifest,
    validate_measurement_receipt,
)
from arbor.aros.receipts import record_sha256
from arbor.aros.store import json_sha256


def test_eval_records_exposes_exactly_four_public_functions() -> None:
    assert {
        name
        for name, value in vars(eval_records_module).items()
        if not name.startswith("_") and inspect.isfunction(value)
    } == {
        "parse_visible_manifest",
        "parse_scalar_metric",
        "build_measurement_receipt",
        "validate_measurement_receipt",
    }


_METRIC_CONTRACT = {
    "source": "scorer_stdout",
    "parser": "aros.scalar-metric-v1",
    "metric_name": "quality",
    "minimum": 0,
    "maximum": 1,
    "minimum_samples": 1,
}
_VALID_METRIC_DOCUMENT = b'{"schema_version":1,"metric":0.5,"sample_count":1}'

INVALID_METRIC_DOCUMENTS = [
    pytest.param(
        b'{"schema_version":1,"metric":0.5,"metric":0.6,"sample_count":1}',
        "duplicate",
        id="duplicate-key",
    ),
    pytest.param(
        b'{"schema_version":1,"metric":NaN,"sample_count":1}',
        "finite",
        id="nan",
    ),
    pytest.param(
        b'{"schema_version":1,"metric":Infinity,"sample_count":1}',
        "finite",
        id="positive-infinity",
    ),
    pytest.param(
        b'{"schema_version":1,"metric":-Infinity,"sample_count":1}',
        "finite",
        id="negative-infinity",
    ),
    pytest.param(
        b'{"schema_version":1,"metric":1e400,"sample_count":1}',
        "finite",
        id="float-overflow",
    ),
    pytest.param(
        _VALID_METRIC_DOCUMENT
        + b" " * (65_537 - len(_VALID_METRIC_DOCUMENT)),
        "65,536",
        id="output-too-large",
    ),
    pytest.param(b"\xff", "UTF-8", id="invalid-utf8"),
    pytest.param(b"[]", "object", id="array"),
    pytest.param(b"null", "object", id="null"),
    pytest.param(b"{}{}", "JSON", id="multiple-documents"),
    pytest.param(b"metric=0.5", "JSON", id="prose"),
    pytest.param(
        b'{"schema_version":1,"metric":0.5}',
        "fields",
        id="missing-field",
    ),
    pytest.param(
        b'{"schema_version":1,"metric":0.5,"sample_count":1,"extra":0}',
        "fields",
        id="extra-field",
    ),
    pytest.param(
        b'{"schema_version":true,"metric":0.5,"sample_count":1}',
        "schema_version",
        id="boolean-schema-version",
    ),
    pytest.param(
        b'{"schema_version":2,"metric":0.5,"sample_count":1}',
        "schema_version",
        id="unknown-schema-version",
    ),
    pytest.param(
        b'{"schema_version":1,"metric":true,"sample_count":1}',
        "metric",
        id="boolean-metric",
    ),
    pytest.param(
        b'{"schema_version":1,"metric":"0.5","sample_count":1}',
        "metric",
        id="string-metric",
    ),
    pytest.param(
        b'{"schema_version":1,"metric":-0.1,"sample_count":1}',
        "range",
        id="metric-below-minimum",
    ),
    pytest.param(
        b'{"schema_version":1,"metric":1.1,"sample_count":1}',
        "range",
        id="metric-above-maximum",
    ),
    pytest.param(
        b'{"schema_version":1,"metric":0.5,"sample_count":true}',
        "sample_count",
        id="boolean-sample-count",
    ),
    pytest.param(
        b'{"schema_version":1,"metric":0.5,"sample_count":-1}',
        "sample_count",
        id="negative-sample-count",
    ),
    pytest.param(
        b'{"schema_version":1,"metric":0.5,"sample_count":1.0}',
        "sample_count",
        id="float-sample-count",
    ),
    pytest.param(
        b'{"schema_version":1,"metric":0.5,"sample_count":'
        + b"9" * 129
        + b"}",
        "128",
        id="integer-token-too-long",
    ),
    pytest.param(
        b'{"schema_version":1,"metric":'
        + b"0."
        + b"0" * 127
        + b',"sample_count":1}',
        "128",
        id="float-token-too-long",
    ),
    pytest.param(
        b"[" * 10_000 + b"0" + b"]" * 10_000,
        "nested",
        id="excessive-nesting",
    ),
]


@pytest.mark.parametrize(("raw", "message"), INVALID_METRIC_DOCUMENTS)
def test_scalar_metric_parser_rejects_non_contract_output(
    raw: bytes,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        parse_scalar_metric(raw, _METRIC_CONTRACT)


@pytest.mark.parametrize(
    ("sample_count", "measurement_state"),
    ((0, "underpowered"), (1, "valid"), (20, "valid")),
)
def test_scalar_metric_parser_returns_valid_or_underpowered(
    sample_count: int,
    measurement_state: str,
) -> None:
    raw = (
        b'{"schema_version":1,"metric":0.73,"sample_count":'
        + str(sample_count).encode("ascii")
        + b"}"
    )

    assert parse_scalar_metric(raw, _METRIC_CONTRACT) == {
        "measurement_state": measurement_state,
        "metric": 0.73,
        "sample_count": sample_count,
        "metric_name": "quality",
        "parser": "aros.scalar-metric-v1",
    }


@pytest.mark.parametrize(
    "case",
    (
        "not-object",
        "missing-field",
        "extra-field",
        "wrong-source",
        "parser-plugin",
        "unsafe-metric-name",
        "boolean-minimum",
        "nonfinite-maximum",
        "huge-maximum",
        "reversed-range",
        "boolean-minimum-samples",
        "zero-minimum-samples",
    ),
)
def test_scalar_metric_parser_rejects_invalid_contract(case: str) -> None:
    contract: object = dict(_METRIC_CONTRACT)
    if case == "not-object":
        contract = []
    else:
        assert isinstance(contract, dict)
        if case == "missing-field":
            del contract["minimum"]
        elif case == "extra-field":
            contract["threshold"] = 0.5
        elif case == "wrong-source":
            contract["source"] = "stderr"
        elif case == "parser-plugin":
            contract["parser"] = "custom-parser"
        elif case == "unsafe-metric-name":
            contract["metric_name"] = "../quality"
        elif case == "boolean-minimum":
            contract["minimum"] = False
        elif case == "nonfinite-maximum":
            contract["maximum"] = float("inf")
        elif case == "huge-maximum":
            contract["maximum"] = 10**1000
        elif case == "reversed-range":
            contract["minimum"] = 2
        elif case == "boolean-minimum-samples":
            contract["minimum_samples"] = True
        elif case == "zero-minimum-samples":
            contract["minimum_samples"] = 0
        else:
            raise AssertionError(f"unknown test case: {case}")

    with pytest.raises(ValueError):
        parse_scalar_metric(_VALID_METRIC_DOCUMENT, contract)  # type: ignore[arg-type]


def test_scalar_metric_parser_requires_bytes() -> None:
    with pytest.raises(ValueError):
        parse_scalar_metric(  # type: ignore[arg-type]
            '{"schema_version":1,"metric":0.5,"sample_count":1}',
            _METRIC_CONTRACT,
        )


def _visible_manifest() -> dict[str, object]:
    return {
        "schema_version": 1,
        "evaluator_id": "quality",
        "evaluator_version": "1",
        "visibility": "visible",
        "apparatus_commit": "a" * 40,
        "apparatus_paths": [
            {"path": "evaluation/score.py", "blob_sha256": "b" * 64}
        ],
        "scorer_argv": ["python", "../apparatus/evaluation/score.py"],
        "scorer_cwd": ".",
        "inputs": [],
        "environment_ref": "isolated-evaluator-v1",
        "seed_policy": {"kind": "fixed", "seed": 7},
        "resource_limits": {"timeout_seconds": 300},
        "success_exit_codes": [0],
        "raw_outputs": ["stdout", "stderr"],
        "metric_output": {
            "source": "scorer_stdout",
            "parser": "aros.scalar-metric-v1",
            "metric_name": "quality",
            "minimum": 0,
            "maximum": 1,
            "minimum_samples": 1,
        },
        "known_limitations": [],
        "calibration_refs": [],
    }


def _invalid_visible_manifest(case: str) -> dict[str, object]:
    manifest = _visible_manifest()
    if case == "missing-field":
        del manifest["visibility"]
    elif case == "extra-field":
        manifest["direction"] = "maximize"
    elif case == "boolean-schema-version":
        manifest["schema_version"] = True
    elif case == "unsafe-evaluator-id":
        manifest["evaluator_id"] = "../quality"
    elif case == "unsafe-evaluator-version":
        manifest["evaluator_version"] = "/1"
    elif case == "short-commit":
        manifest["apparatus_commit"] = "a" * 39
    elif case == "uppercase-commit":
        manifest["apparatus_commit"] = "A" * 40
    elif case == "unsafe-apparatus-path":
        manifest["apparatus_paths"] = [
            {"path": "../score.py", "blob_sha256": "b" * 64}
        ]
    elif case == "invalid-apparatus-blob":
        manifest["apparatus_paths"] = [
            {"path": "evaluation/score.py", "blob_sha256": "B" * 64}
        ]
    elif case == "extra-apparatus-path-field":
        manifest["apparatus_paths"] = [
            {
                "path": "evaluation/score.py",
                "blob_sha256": "b" * 64,
                "mode": "100755",
            }
        ]
    elif case == "empty-argv":
        manifest["scorer_argv"] = []
    elif case == "nul-argv":
        manifest["scorer_argv"] = ["python\x00", "../apparatus/evaluation/score.py"]
    elif case == "unbound-apparatus-argv":
        manifest["scorer_argv"] = ["python", "score.py"]
    elif case == "unsafe-cwd":
        manifest["scorer_cwd"] = "../apparatus"
    elif case == "protected-visibility":
        manifest["visibility"] = "protected"
    elif case == "non-list-inputs":
        manifest["inputs"] = {}
    elif case == "unknown-environment":
        manifest["environment_ref"] = "host"
    elif case == "boolean-seed":
        manifest["seed_policy"] = {"kind": "fixed", "seed": True}
    elif case == "extra-seed-policy-field":
        manifest["seed_policy"] = {"kind": "fixed", "seed": 7, "salt": "x"}
    elif case == "nonfinite-timeout":
        manifest["resource_limits"] = {"timeout_seconds": float("inf")}
    elif case == "duplicate-exit-code":
        manifest["success_exit_codes"] = [0, 0]
    elif case == "boolean-exit-code":
        manifest["success_exit_codes"] = [False]
    elif case == "raw-output-order":
        manifest["raw_outputs"] = ["stderr", "stdout"]
    elif case == "metric-extra-field":
        metric_output = manifest["metric_output"]
        assert isinstance(metric_output, dict)
        metric_output["threshold"] = 0.5
    elif case == "metric-plugin":
        metric_output = manifest["metric_output"]
        assert isinstance(metric_output, dict)
        metric_output["parser"] = "custom-parser"
    elif case == "boolean-metric-bound":
        metric_output = manifest["metric_output"]
        assert isinstance(metric_output, dict)
        metric_output["minimum"] = False
    elif case == "reversed-metric-range":
        metric_output = manifest["metric_output"]
        assert isinstance(metric_output, dict)
        metric_output["minimum"] = 2
    elif case == "huge-metric-bound":
        metric_output = manifest["metric_output"]
        assert isinstance(metric_output, dict)
        metric_output["maximum"] = 10**1000
    elif case == "zero-minimum-samples":
        metric_output = manifest["metric_output"]
        assert isinstance(metric_output, dict)
        metric_output["minimum_samples"] = 0
    elif case == "invalid-limitation-text":
        manifest["known_limitations"] = ["\ud800"]
    else:
        raise AssertionError(f"unknown test case: {case}")
    return manifest


def test_visible_manifest_is_strict_and_self_hashed() -> None:
    manifest = _visible_manifest()

    parsed = parse_visible_manifest(manifest)

    assert parsed == manifest
    assert parsed is not manifest
    assert parsed["apparatus_paths"] is not manifest["apparatus_paths"]
    assert parsed["metric_output"] is not manifest["metric_output"]
    assert "manifest_sha256" not in parsed
    assert json_sha256(parsed) == json_sha256(manifest)


def test_visible_manifest_rejects_decoy_scorer_entry() -> None:
    manifest = _visible_manifest()
    manifest["scorer_argv"] = [
        "python",
        "untrusted.py",
        "../apparatus/evaluation/score.py",
    ]

    with pytest.raises(ValueError):
        parse_visible_manifest(manifest)


def test_visible_manifest_rejects_undeclared_explicit_apparatus_path() -> None:
    manifest = _visible_manifest()
    manifest["scorer_argv"] = [
        "python",
        "../apparatus/evaluation/score.py",
        "../apparatus/evaluation/undeclared.json",
    ]

    with pytest.raises(ValueError):
        parse_visible_manifest(manifest)


def test_visible_manifest_accepts_direct_declared_apparatus_entry() -> None:
    manifest = _visible_manifest()
    manifest["scorer_argv"] = ["../apparatus/evaluation/score.py"]

    assert parse_visible_manifest(manifest)["scorer_argv"] == [
        "../apparatus/evaluation/score.py"
    ]


@pytest.mark.parametrize(
    "case",
    (
        "missing-field",
        "extra-field",
        "boolean-schema-version",
        "unsafe-evaluator-id",
        "unsafe-evaluator-version",
        "short-commit",
        "uppercase-commit",
        "unsafe-apparatus-path",
        "invalid-apparatus-blob",
        "extra-apparatus-path-field",
        "empty-argv",
        "nul-argv",
        "unbound-apparatus-argv",
        "unsafe-cwd",
        "protected-visibility",
        "non-list-inputs",
        "unknown-environment",
        "boolean-seed",
        "extra-seed-policy-field",
        "nonfinite-timeout",
        "duplicate-exit-code",
        "boolean-exit-code",
        "raw-output-order",
        "metric-extra-field",
        "metric-plugin",
        "boolean-metric-bound",
        "reversed-metric-range",
        "huge-metric-bound",
        "zero-minimum-samples",
        "invalid-limitation-text",
    ),
)
def test_visible_manifest_rejects_non_contract_values(case: str) -> None:
    with pytest.raises(ValueError):
        parse_visible_manifest(_invalid_visible_manifest(case))


def _lineage_records(
    process_state: str = "completed",
) -> tuple[
    dict[str, object],
    dict[str, object],
    dict[str, object],
    dict[str, object],
]:
    idempotency_key_sha256 = "1" * 64
    eval_id = f"EVAL-{idempotency_key_sha256}"
    candidate_commit = "a" * 40
    apparatus_commit = "b" * 40
    request: dict[str, object] = {
        "schema_version": 1,
        "eval_id": eval_id,
        "evaluator_id": "quality",
        "evaluator_version": "1",
        "descriptor_sha256": "2" * 64,
        "candidate_commit": candidate_commit,
        "apparatus_commit": apparatus_commit,
        "actor": "principal",
        "idempotency_key_sha256": idempotency_key_sha256,
        "created_at": "2026-08-03T12:00:00.000Z",
    }
    request["request_sha256"] = record_sha256(request, "request_sha256")
    execution: dict[str, object] = {
        "schema_version": 1,
        "eval_id": eval_id,
        "request_sha256": request["request_sha256"],
        "host": "test-host",
        "broker_pid": 1234,
        "broker_start_token": "linux-proc-start:99",
        "claimed_at": "2026-08-03T12:00:01.000Z",
    }
    execution["execution_sha256"] = record_sha256(
        execution,
        "execution_sha256",
    )
    portable_bundle = {
        "candidate": {
            "path": "candidate",
            "commit": candidate_commit,
            "tree": "c" * 40,
        },
        "apparatus": {
            "path": "apparatus",
            "commit": apparatus_commit,
            "tree": "d" * 40,
        },
        "temp": "tmp",
    }
    bundle_sha256 = json_sha256(portable_bundle)
    run_id = "RUN-eval-quality"
    run_link: dict[str, object] = {
        "schema_version": 1,
        "eval_id": eval_id,
        "request_sha256": request["request_sha256"],
        "execution_sha256": execution["execution_sha256"],
        "run_id": run_id,
        "run_manifest_sha256": "3" * 64,
        "bundle_sha256": bundle_sha256,
        "candidate_commit": candidate_commit,
        "apparatus_commit": apparatus_commit,
        "linked_at": "2026-08-03T12:00:02.000Z",
    }
    run_link["run_link_sha256"] = record_sha256(
        run_link,
        "run_link_sha256",
    )
    run_final = {
        "schema_version": 1,
        "run_id": run_id,
        "manifest_sha256": run_link["run_manifest_sha256"],
        "state": process_state,
        "candidate_commit": candidate_commit,
        "execution_bundle": {
            **portable_bundle,
            "bundle_sha256": bundle_sha256,
        },
        "stdout": {
            "path": f".aros/runs/{run_id}/stdout.log",
            "bytes": 51,
            "sha256": "4" * 64,
        },
        "stderr": {
            "path": f".aros/runs/{run_id}/stderr.log",
            "bytes": 0,
            "sha256": "5" * 64,
        },
        "finished_at": "2026-08-03T12:00:03.000Z",
    }
    return request, execution, run_link, run_final


def _measurement(measurement_state: str) -> dict[str, object]:
    has_metric = measurement_state in {"valid", "underpowered"}
    return {
        "measurement_state": measurement_state,
        "metric": 0.73 if has_metric else None,
        "sample_count": (0 if measurement_state == "underpowered" else 20)
        if has_metric
        else None,
        "metric_name": "quality",
        "parser": "aros.scalar-metric-v1",
    }


def _expected_receipt(
    request: dict[str, object],
    execution: dict[str, object],
    run_link: dict[str, object],
    run_final: dict[str, object],
    measurement_state: str,
    measurement: dict[str, object],
    bundle_cleanup_state: str,
) -> dict[str, object]:
    receipt: dict[str, object] = {
        "schema_version": 1,
        "eval_id": request["eval_id"],
        "evaluation_state": "completed",
        "referenced_process_state": run_final["state"],
        "measurement_state": measurement_state,
        "descriptor_sha256": request["descriptor_sha256"],
        "request_sha256": request["request_sha256"],
        "execution_sha256": execution["execution_sha256"],
        "run_id": run_link["run_id"],
        "run_manifest_sha256": run_link["run_manifest_sha256"],
        "run_final_sha256": json_sha256(run_final),
        "bundle_sha256": run_link["bundle_sha256"],
        "candidate_commit": run_link["candidate_commit"],
        "apparatus_commit": run_link["apparatus_commit"],
        "metric": measurement["metric"],
        "sample_count": measurement["sample_count"],
        "metric_name": measurement["metric_name"],
        "parser": measurement["parser"],
        "bundle_cleanup_state": bundle_cleanup_state,
        "stdout": run_final["stdout"],
        "stderr": run_final["stderr"],
        "finished_at": run_final["finished_at"],
    }
    receipt["receipt_sha256"] = record_sha256(receipt, "receipt_sha256")
    return receipt


def test_build_measurement_receipt_has_exact_lineage_and_self_hash() -> None:
    request, execution, run_link, run_final = _lineage_records()
    measurement = _measurement("valid")
    inputs = copy.deepcopy(
        (request, execution, run_link, run_final, measurement)
    )

    receipt = build_measurement_receipt(
        request,
        execution,
        run_link,
        run_final,
        "valid",
        measurement,
        "removed",
    )

    assert receipt == _expected_receipt(
        request,
        execution,
        run_link,
        run_final,
        "valid",
        measurement,
        "removed",
    )
    assert set(receipt) == {
        "schema_version",
        "eval_id",
        "evaluation_state",
        "referenced_process_state",
        "measurement_state",
        "descriptor_sha256",
        "request_sha256",
        "execution_sha256",
        "run_id",
        "run_manifest_sha256",
        "run_final_sha256",
        "bundle_sha256",
        "candidate_commit",
        "apparatus_commit",
        "metric",
        "sample_count",
        "metric_name",
        "parser",
        "bundle_cleanup_state",
        "stdout",
        "stderr",
        "finished_at",
        "receipt_sha256",
    }
    assert "run_link_sha256" not in receipt
    assert receipt["stdout"] is not run_final["stdout"]
    assert receipt["stderr"] is not run_final["stderr"]
    assert (request, execution, run_link, run_final, measurement) == inputs


def test_build_measurement_receipt_rejects_nan_in_run_final_extra() -> None:
    request, execution, run_link, run_final = _lineage_records()
    run_final["arbitrary_extra"] = float("nan")

    with pytest.raises(ValueError):
        build_measurement_receipt(
            request,
            execution,
            run_link,
            run_final,
            "valid",
            _measurement("valid"),
            "removed",
        )


def test_build_measurement_receipt_normalizes_deep_run_final_error() -> None:
    request, execution, run_link, run_final = _lineage_records()
    nested: object = None
    for _depth in range(10_000):
        nested = [nested]
    run_final["arbitrary_extra"] = nested

    with pytest.raises(ValueError):
        build_measurement_receipt(
            request,
            execution,
            run_link,
            run_final,
            "valid",
            _measurement("valid"),
            "removed",
        )


@pytest.mark.parametrize(
    "extra",
    (
        pytest.param(float("inf"), id="infinity"),
        pytest.param(("tuple",), id="tuple"),
        pytest.param({1: "non-string-key"}, id="non-string-key"),
    ),
)
def test_build_measurement_receipt_rejects_noncanonical_run_final_extra(
    extra: object,
) -> None:
    request, execution, run_link, run_final = _lineage_records()
    run_final["arbitrary_extra"] = extra

    with pytest.raises(ValueError):
        build_measurement_receipt(
            request,
            execution,
            run_link,
            run_final,
            "valid",
            _measurement("valid"),
            "removed",
        )


def test_build_measurement_receipt_rejects_excessive_run_final_nodes() -> None:
    request, execution, run_link, run_final = _lineage_records()
    run_final["arbitrary_extra"] = [None] * 10_001

    with pytest.raises(ValueError):
        build_measurement_receipt(
            request,
            execution,
            run_link,
            run_final,
            "valid",
            _measurement("valid"),
            "removed",
        )


def test_build_measurement_receipt_rejects_oversized_run_final() -> None:
    request, execution, run_link, run_final = _lineage_records()
    run_final["arbitrary_extra"] = "x" * (1024 * 1024)

    with pytest.raises(ValueError):
        build_measurement_receipt(
            request,
            execution,
            run_link,
            run_final,
            "valid",
            _measurement("valid"),
            "removed",
        )


@pytest.mark.parametrize(
    "case",
    (
        "request-extra-field",
        "request-hash",
        "request-eval-id",
        "execution-request-binding",
        "execution-start-token",
        "execution-hash",
        "run-link-execution-binding",
        "run-link-hash",
        "run-link-commit-binding",
        "run-final-run-id",
        "run-final-manifest",
        "run-final-candidate",
        "run-final-apparatus",
        "run-final-bundle-hash",
        "run-final-bundle-payload",
        "run-final-output-path",
        "measurement-state-binding",
        "measurement-extra-field",
        "metric-null-valid",
        "zero-sample-valid",
        "metric-present-invalid",
        "unknown-cleanup-state",
    ),
)
def test_build_measurement_receipt_rejects_invalid_lineage(case: str) -> None:
    request, execution, run_link, run_final = _lineage_records()
    measurement_state = "valid"
    measurement = _measurement(measurement_state)
    cleanup_state = "removed"
    if case == "request-extra-field":
        request["threshold"] = 0.5
        request["request_sha256"] = record_sha256(request, "request_sha256")
    elif case == "request-hash":
        request["request_sha256"] = "f" * 64
    elif case == "request-eval-id":
        request["eval_id"] = f"EVAL-{'e' * 64}"
        request["idempotency_key_sha256"] = "e" * 64
        request["request_sha256"] = record_sha256(request, "request_sha256")
    elif case == "execution-request-binding":
        execution["request_sha256"] = "e" * 64
        execution["execution_sha256"] = record_sha256(
            execution,
            "execution_sha256",
        )
    elif case == "execution-start-token":
        execution["broker_start_token"] = "not-a-linux-start-token"
        execution["execution_sha256"] = record_sha256(
            execution,
            "execution_sha256",
        )
        run_link["execution_sha256"] = execution["execution_sha256"]
        run_link["run_link_sha256"] = record_sha256(
            run_link,
            "run_link_sha256",
        )
    elif case == "execution-hash":
        execution["execution_sha256"] = "e" * 64
    elif case == "run-link-execution-binding":
        run_link["execution_sha256"] = "e" * 64
        run_link["run_link_sha256"] = record_sha256(
            run_link,
            "run_link_sha256",
        )
    elif case == "run-link-hash":
        run_link["run_link_sha256"] = "e" * 64
    elif case == "run-link-commit-binding":
        run_link["candidate_commit"] = "e" * 40
        run_link["run_link_sha256"] = record_sha256(
            run_link,
            "run_link_sha256",
        )
    elif case == "run-final-run-id":
        run_final["run_id"] = "RUN-other"
    elif case == "run-final-manifest":
        run_final["manifest_sha256"] = "e" * 64
    elif case == "run-final-candidate":
        run_final["candidate_commit"] = "e" * 40
    elif case == "run-final-apparatus":
        bundle = run_final["execution_bundle"]
        assert isinstance(bundle, dict)
        apparatus = bundle["apparatus"]
        assert isinstance(apparatus, dict)
        apparatus["commit"] = "e" * 40
    elif case == "run-final-bundle-hash":
        bundle = run_final["execution_bundle"]
        assert isinstance(bundle, dict)
        bundle["bundle_sha256"] = "e" * 64
    elif case == "run-final-bundle-payload":
        bundle = run_final["execution_bundle"]
        assert isinstance(bundle, dict)
        candidate = bundle["candidate"]
        assert isinstance(candidate, dict)
        candidate["tree"] = "e" * 40
    elif case == "run-final-output-path":
        stdout = run_final["stdout"]
        assert isinstance(stdout, dict)
        stdout["path"] = ".aros/runs/RUN-other/stdout.log"
    elif case == "measurement-state-binding":
        measurement["measurement_state"] = "underpowered"
    elif case == "measurement-extra-field":
        measurement["verdict"] = "pass"
    elif case == "metric-null-valid":
        measurement["metric"] = None
    elif case == "zero-sample-valid":
        measurement["sample_count"] = 0
    elif case == "metric-present-invalid":
        measurement_state = "invalid_eval"
        measurement = _measurement(measurement_state)
        measurement["metric"] = 0.73
    elif case == "unknown-cleanup-state":
        cleanup_state = "forced"
    else:
        raise AssertionError(f"unknown test case: {case}")

    with pytest.raises(ValueError):
        build_measurement_receipt(
            request,
            execution,
            run_link,
            run_final,
            measurement_state,
            measurement,
            cleanup_state,
        )


_ALLOWED_STATE_PAIRS = {
    ("completed", "valid"),
    ("completed", "underpowered"),
    ("completed", "invalid_eval"),
    ("failed_process", "not_available"),
    ("timed_out", "not_available"),
    ("cancelled", "not_available"),
}


@pytest.mark.parametrize(
    ("process_state", "measurement_state"),
    tuple(
        product(
            ("completed", "failed_process", "timed_out", "cancelled", "lost"),
            ("valid", "underpowered", "invalid_eval", "not_available"),
        )
    ),
)
def test_process_and_measurement_states_have_only_declared_pairings(
    process_state: str,
    measurement_state: str,
) -> None:
    request, execution, run_link, run_final = _lineage_records(process_state)
    arguments = (
        request,
        execution,
        run_link,
        run_final,
        measurement_state,
        _measurement(measurement_state),
        "preserved",
    )
    if (process_state, measurement_state) in _ALLOWED_STATE_PAIRS:
        receipt = build_measurement_receipt(*arguments)
        assert receipt["referenced_process_state"] == process_state
        assert receipt["measurement_state"] == measurement_state
    else:
        with pytest.raises(ValueError):
            build_measurement_receipt(*arguments)


def _valid_receipt(
    process_state: str = "completed",
    measurement_state: str = "valid",
) -> dict[str, object]:
    request, execution, run_link, run_final = _lineage_records(process_state)
    return build_measurement_receipt(
        request,
        execution,
        run_link,
        run_final,
        measurement_state,
        _measurement(measurement_state),
        "removed",
    )


def test_validate_measurement_receipt_returns_fresh_exact_record() -> None:
    receipt = _valid_receipt()

    validated = validate_measurement_receipt(receipt)

    assert validated == receipt
    assert validated is not receipt
    assert validated["stdout"] is not receipt["stdout"]
    assert validated["stderr"] is not receipt["stderr"]


@pytest.mark.parametrize(
    ("process_state", "measurement_state"),
    tuple(_ALLOWED_STATE_PAIRS),
)
def test_validate_measurement_receipt_accepts_declared_pairings(
    process_state: str,
    measurement_state: str,
) -> None:
    assert validate_measurement_receipt(
        _valid_receipt(process_state, measurement_state)
    )["measurement_state"] == measurement_state


@pytest.mark.parametrize(
    "case",
    (
        "missing-field",
        "extra-threshold",
        "boolean-schema-version",
        "invalid-eval-id",
        "lost-evaluation-state",
        "lost-process-state",
        "non-string-process-state",
        "non-string-measurement-state",
        "invalid-state-pair",
        "invalid-lineage-hash",
        "invalid-run-id",
        "invalid-commit",
        "boolean-metric",
        "nonfinite-metric",
        "null-valid-metric",
        "boolean-sample-count",
        "negative-sample-count",
        "zero-sample-valid",
        "metric-present-invalid",
        "unsafe-metric-name",
        "parser-plugin",
        "unknown-cleanup-state",
        "non-string-cleanup-state",
        "stdout-extra-field",
        "stdout-path-mismatch",
        "stdout-boolean-bytes",
        "stdout-invalid-hash",
        "invalid-finished-at",
        "receipt-hash-mismatch",
    ),
)
def test_validate_measurement_receipt_rejects_non_contract_values(
    case: str,
) -> None:
    receipt = _valid_receipt()
    rehash = True
    if case == "missing-field":
        del receipt["bundle_sha256"]
    elif case == "extra-threshold":
        receipt["threshold"] = 0.5
    elif case == "boolean-schema-version":
        receipt["schema_version"] = True
    elif case == "invalid-eval-id":
        receipt["eval_id"] = "EVAL-short"
    elif case == "lost-evaluation-state":
        receipt["evaluation_state"] = "lost"
    elif case == "lost-process-state":
        receipt["referenced_process_state"] = "lost"
        receipt["measurement_state"] = "not_available"
        receipt["metric"] = None
        receipt["sample_count"] = None
    elif case == "non-string-process-state":
        receipt["referenced_process_state"] = []
    elif case == "non-string-measurement-state":
        receipt["measurement_state"] = []
    elif case == "invalid-state-pair":
        receipt["measurement_state"] = "not_available"
        receipt["metric"] = None
        receipt["sample_count"] = None
    elif case == "invalid-lineage-hash":
        receipt["run_manifest_sha256"] = "A" * 64
    elif case == "invalid-run-id":
        receipt["run_id"] = "../RUN-bad"
    elif case == "invalid-commit":
        receipt["candidate_commit"] = "A" * 40
    elif case == "boolean-metric":
        receipt["metric"] = True
    elif case == "nonfinite-metric":
        receipt["metric"] = float("inf")
    elif case == "null-valid-metric":
        receipt["metric"] = None
    elif case == "boolean-sample-count":
        receipt["sample_count"] = False
    elif case == "negative-sample-count":
        receipt["sample_count"] = -1
    elif case == "zero-sample-valid":
        receipt["sample_count"] = 0
    elif case == "metric-present-invalid":
        receipt["measurement_state"] = "invalid_eval"
    elif case == "unsafe-metric-name":
        receipt["metric_name"] = "../quality"
    elif case == "parser-plugin":
        receipt["parser"] = "custom-parser"
    elif case == "unknown-cleanup-state":
        receipt["bundle_cleanup_state"] = "forced"
    elif case == "non-string-cleanup-state":
        receipt["bundle_cleanup_state"] = []
    elif case == "stdout-extra-field":
        stdout = receipt["stdout"]
        assert isinstance(stdout, dict)
        stdout["encoding"] = "utf-8"
    elif case == "stdout-path-mismatch":
        stdout = receipt["stdout"]
        assert isinstance(stdout, dict)
        stdout["path"] = ".aros/runs/RUN-other/stdout.log"
    elif case == "stdout-boolean-bytes":
        stdout = receipt["stdout"]
        assert isinstance(stdout, dict)
        stdout["bytes"] = False
    elif case == "stdout-invalid-hash":
        stdout = receipt["stdout"]
        assert isinstance(stdout, dict)
        stdout["sha256"] = "A" * 64
    elif case == "invalid-finished-at":
        receipt["finished_at"] = "2026-08-03"
    elif case == "receipt-hash-mismatch":
        receipt["receipt_sha256"] = "e" * 64
        rehash = False
    else:
        raise AssertionError(f"unknown test case: {case}")
    if rehash:
        receipt["receipt_sha256"] = record_sha256(receipt, "receipt_sha256")

    with pytest.raises(ValueError):
        validate_measurement_receipt(receipt)
