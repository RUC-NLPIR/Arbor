"""Strict pure records for visible AROS evaluation."""

from __future__ import annotations

import hashlib
import json
import math
import posixpath
import re
import sys
from datetime import datetime
from pathlib import PurePosixPath

from .receipts import record_sha256 as _record_sha256
from .store import canonical_json_bytes as _canonical_json_bytes
from .store import json_sha256 as _json_sha256


_MAX_METRIC_BYTES = 65_536
_MAX_NUMBER_TOKEN = 128
_MAX_RECORD_BYTES = 1024 * 1024
_MAX_RECORD_DEPTH = 64
_MAX_RECORD_INTEGER_BITS = _MAX_RECORD_BYTES * 3
_MAX_RECORD_NODES = 10_000
_LOG10_2_UPPER_DENOMINATOR = 100_000
_LOG10_2_UPPER_NUMERATOR = 30_103
_SCORER_LAUNCHERS = {"python", "python3", "bash", "sh", sys.executable}
_METRIC_FIELDS = {"schema_version", "metric", "sample_count"}


def _field_set(fields: str) -> set[str]:
    return set(fields.split())


_VISIBLE_MANIFEST_FIELDS = _field_set(
    "schema_version evaluator_id evaluator_version visibility apparatus_commit "
    "apparatus_paths scorer_argv scorer_cwd inputs environment_ref seed_policy "
    "resource_limits success_exit_codes raw_outputs metric_output "
    "known_limitations calibration_refs"
)
_METRIC_CONTRACT_FIELDS = _field_set(
    "source parser metric_name minimum maximum minimum_samples"
)
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_EVAL_ID = re.compile(r"^EVAL-[0-9a-f]{64}$")
_RUN_ID = re.compile(r"^RUN-[A-Za-z0-9][A-Za-z0-9-]*$")
_START_TOKEN = re.compile(r"^linux-proc-start:[0-9]+$")
_UTC_TIMESTAMP = re.compile(
    r"^\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])"
    r"T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d\.\d{3}Z$"
)
_REQUEST_FIELDS = _field_set(
    "schema_version eval_id evaluator_id evaluator_version descriptor_sha256 "
    "candidate_commit apparatus_commit actor idempotency_key_sha256 created_at "
    "request_sha256"
)
_EXECUTION_FIELDS = _field_set(
    "schema_version eval_id request_sha256 host broker_pid broker_start_token "
    "claimed_at execution_sha256"
)
_RUN_LINK_FIELDS = _field_set(
    "schema_version eval_id request_sha256 execution_sha256 run_id "
    "run_manifest_sha256 bundle_sha256 candidate_commit apparatus_commit "
    "linked_at run_link_sha256"
)
_MEASUREMENT_FIELDS = _field_set(
    "measurement_state metric sample_count metric_name parser"
)
_RECEIPT_FIELDS = _field_set(
    "schema_version eval_id evaluation_state referenced_process_state "
    "measurement_state descriptor_sha256 request_sha256 execution_sha256 run_id "
    "run_manifest_sha256 run_final_sha256 bundle_sha256 candidate_commit "
    "apparatus_commit metric sample_count metric_name parser bundle_cleanup_state "
    "stdout stderr finished_at receipt_sha256"
)
_ALLOWED_STATE_PAIRS = {
    ("completed", "valid"),
    ("completed", "underpowered"),
    ("completed", "invalid_eval"),
    ("failed_process", "not_available"),
    ("timed_out", "not_available"),
    ("cancelled", "not_available"),
}


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_constant(value: str) -> object:
    raise ValueError(f"metric document contains non-finite number: {value}")


def _bounded_int(value: str) -> int:
    if len(value) > _MAX_NUMBER_TOKEN:
        raise ValueError("JSON integer token exceeds 128 characters")
    return int(value)


def _bounded_float(value: str) -> float:
    if len(value) > _MAX_NUMBER_TOKEN:
        raise ValueError("JSON float token exceeds 128 characters")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("metric document contains non-finite number")
    return number


def _utf8(value: object, field: str, *, nonempty: bool = False) -> str:
    if not isinstance(value, str) or (nonempty and not value):
        raise ValueError(f"{field} must be a string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError(f"{field} must be valid UTF-8") from error
    return value


def _identifier(value: object, field: str) -> str:
    text = _utf8(value, field, nonempty=True)
    if _IDENTIFIER.fullmatch(text) is None:
        raise ValueError(f"{field} must be a safe path component")
    return text


def _relative_path(value: object, field: str, *, allow_dot: bool) -> str:
    text = _utf8(value, field, nonempty=True)
    path = PurePosixPath(text)
    if (
        "\x00" in text
        or "\\" in text
        or path.is_absolute()
        or text != path.as_posix()
        or any(part == ".." for part in path.parts)
        or (not allow_dot and text == ".")
    ):
        raise ValueError(f"{field} must be a canonical safe relative path")
    return text


def _string_list(value: object, field: str) -> list[str]:
    if type(value) is not list:
        raise ValueError(f"{field} must be a list of strings")
    return [_utf8(item, field) for item in value]


def _finite_number(value: object, field: str) -> int | float:
    if type(value) not in {int, float}:
        raise ValueError(f"{field} must be a finite number")
    try:
        finite = math.isfinite(value)
    except OverflowError as error:
        raise ValueError(f"{field} must be a finite number") from error
    if not finite:
        raise ValueError(f"{field} must be a finite number")
    return value


def _bounded_utf8_size(value: str, description: str, remaining: int) -> int:
    if len(value) > _MAX_RECORD_BYTES:
        raise ValueError(f"{description} exceeds {_MAX_RECORD_BYTES} characters")
    if len(value) > remaining:
        raise ValueError(f"{description} exceeds the cumulative UTF-8 byte limit")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise ValueError(f"{description} must be valid UTF-8") from error
    if size > remaining:
        raise ValueError(f"{description} exceeds the cumulative UTF-8 byte limit")
    return size


def _bounded_canonical_sha256(value: object, description: str) -> str:
    stack = [(value, 0)]
    node_count = 0
    estimated_bytes = 0
    while stack:
        item, depth = stack.pop()
        node_count += 1
        if node_count > _MAX_RECORD_NODES:
            raise ValueError(f"{description} exceeds {_MAX_RECORD_NODES} nodes")
        if depth > _MAX_RECORD_DEPTH:
            raise ValueError(f"{description} exceeds depth {_MAX_RECORD_DEPTH}")
        item_type = type(item)
        if item_type is dict:
            if node_count + len(stack) + len(item) > _MAX_RECORD_NODES:
                raise ValueError(f"{description} exceeds {_MAX_RECORD_NODES} nodes")
            for key in item:
                if type(key) is not str:
                    raise ValueError(f"{description} keys must be strings")
                estimated_bytes += _bounded_utf8_size(
                    key,
                    f"{description} key",
                    _MAX_RECORD_BYTES - estimated_bytes,
                )
            stack.extend((child, depth + 1) for child in item.values())
        elif item_type is list:
            if node_count + len(stack) + len(item) > _MAX_RECORD_NODES:
                raise ValueError(f"{description} exceeds {_MAX_RECORD_NODES} nodes")
            stack.extend((child, depth + 1) for child in item)
        elif item_type is str:
            estimated_bytes += _bounded_utf8_size(
                item,
                description,
                _MAX_RECORD_BYTES - estimated_bytes,
            )
        elif item_type is float:
            if not math.isfinite(item):
                raise ValueError(f"{description} contains a non-finite number")
        elif item_type is int:
            bit_length = item.bit_length()
            if bit_length > _MAX_RECORD_INTEGER_BITS:
                raise ValueError(f"{description} contains an oversized integer")
            integer_bytes = max(
                1,
                (
                    bit_length * _LOG10_2_UPPER_NUMERATOR
                    + _LOG10_2_UPPER_DENOMINATOR
                    - 1
                )
                // _LOG10_2_UPPER_DENOMINATOR,
            ) + int(item < 0)
            if integer_bytes > _MAX_RECORD_BYTES - estimated_bytes:
                raise ValueError(
                    f"{description} exceeds the cumulative encoded byte limit"
                )
            estimated_bytes += integer_bytes
        elif item is not None and item_type is not bool:
            raise ValueError(f"{description} contains a non-JSON value")
    try:
        payload = _canonical_json_bytes(value)
    except (RecursionError, TypeError, ValueError, UnicodeError) as error:
        raise ValueError(f"{description} is not canonical JSON") from error
    if len(payload) > _MAX_RECORD_BYTES:
        raise ValueError(f"{description} exceeds {_MAX_RECORD_BYTES} encoded bytes")
    return hashlib.sha256(payload).hexdigest()


def parse_visible_manifest(value: object) -> dict[str, object]:
    """Validate and copy one exact visible evaluator manifest."""
    if type(value) is not dict or set(value) != _VISIBLE_MANIFEST_FIELDS:
        raise ValueError("visible evaluator manifest has invalid fields")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("visible evaluator manifest has invalid schema_version")
    evaluator_id = _identifier(value["evaluator_id"], "evaluator_id")
    evaluator_version = _identifier(value["evaluator_version"], "evaluator_version")
    if value["visibility"] != "visible":
        raise ValueError("visible evaluator manifest visibility must be visible")
    apparatus_commit = value["apparatus_commit"]
    if not isinstance(apparatus_commit, str) or _COMMIT.fullmatch(apparatus_commit) is None:
        raise ValueError("apparatus_commit must be a full lowercase Git commit")

    raw_apparatus_paths = value["apparatus_paths"]
    if type(raw_apparatus_paths) is not list or not raw_apparatus_paths:
        raise ValueError("apparatus_paths must be a non-empty list")
    apparatus_paths: list[dict[str, object]] = []
    seen_paths: set[str] = set()
    for item in raw_apparatus_paths:
        if type(item) is not dict or set(item) != {"path", "blob_sha256"}:
            raise ValueError("apparatus_paths entries have invalid fields")
        path = _relative_path(item["path"], "apparatus path", allow_dot=False)
        blob_sha256 = item["blob_sha256"]
        if not isinstance(blob_sha256, str) or _SHA256.fullmatch(blob_sha256) is None:
            raise ValueError("apparatus blob_sha256 must be lowercase 64-hex")
        if path in seen_paths:
            raise ValueError("apparatus_paths must not contain duplicate paths")
        seen_paths.add(path)
        apparatus_paths.append({"path": path, "blob_sha256": blob_sha256})

    raw_argv = value["scorer_argv"]
    if type(raw_argv) is not list or not raw_argv:
        raise ValueError("scorer_argv must be a non-empty list")
    scorer_argv: list[str] = []
    for item in raw_argv:
        argument = _utf8(item, "scorer_argv", nonempty=True)
        if "\x00" in argument:
            raise ValueError("scorer_argv arguments must not contain NUL")
        scorer_argv.append(argument)
    scorer_cwd = _relative_path(value["scorer_cwd"], "scorer_cwd", allow_dot=True)
    scorer_start = posixpath.join("candidate", scorer_cwd)
    normalized_argv = [
        posixpath.normpath(argument)
        if "/" in argument or argument.startswith(".")
        else argument
        for argument in scorer_argv
    ]
    apparatus_arguments = {
        posixpath.relpath(posixpath.join("apparatus", str(item["path"])), scorer_start)
        for item in apparatus_paths
    }
    direct_entry = normalized_argv[0] in apparatus_arguments
    launched_entry = (
        len(normalized_argv) > 1
        and scorer_argv[0] in _SCORER_LAUNCHERS
        and normalized_argv[1] in apparatus_arguments
    )
    if direct_entry:
        entry_index = 0
    elif launched_entry:
        entry_index = 1
    else:
        raise ValueError("scorer_argv must execute one declared apparatus entry")
    if any(
        "/" in argument or "\\" in argument
        for argument in scorer_argv[entry_index + 1 :]
    ):
        raise ValueError("scorer_argv arguments after the entry must not contain paths")

    inputs = _string_list(value["inputs"], "inputs")
    environment_ref = _utf8(value["environment_ref"], "environment_ref", nonempty=True)
    if environment_ref != "isolated-evaluator-v1":
        raise ValueError("environment_ref must be isolated-evaluator-v1")

    seed_policy = value["seed_policy"]
    if type(seed_policy) is not dict or set(seed_policy) != {"kind", "seed"}:
        raise ValueError("seed_policy has invalid fields")
    seed = seed_policy["seed"]
    if seed_policy["kind"] != "fixed" or type(seed) is not int or seed < 0:
        raise ValueError("seed_policy must contain a fixed nonnegative plain integer seed")

    resource_limits = value["resource_limits"]
    if type(resource_limits) is not dict or set(resource_limits) != {"timeout_seconds"}:
        raise ValueError("resource_limits has invalid fields")
    timeout_seconds = _finite_number(resource_limits["timeout_seconds"], "timeout_seconds")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")

    success_exit_codes = value["success_exit_codes"]
    if (
        type(success_exit_codes) is not list
        or not success_exit_codes
        or any(type(code) is not int for code in success_exit_codes)
        or len(set(success_exit_codes)) != len(success_exit_codes)
    ):
        raise ValueError("success_exit_codes must be unique plain integers")
    exit_codes = list(success_exit_codes)

    if type(value["raw_outputs"]) is not list or value["raw_outputs"] != [
        "stdout",
        "stderr",
    ]:
        raise ValueError("raw_outputs must be exactly stdout and stderr")

    metric_output = _validate_metric_contract(value["metric_output"])
    known_limitations = _string_list(value["known_limitations"], "known_limitations")
    calibration_refs = _string_list(value["calibration_refs"], "calibration_refs")
    return {
        **value,
        "evaluator_id": evaluator_id,
        "evaluator_version": evaluator_version,
        "apparatus_commit": apparatus_commit,
        "apparatus_paths": apparatus_paths,
        "scorer_argv": scorer_argv,
        "scorer_cwd": scorer_cwd,
        "inputs": inputs,
        "environment_ref": environment_ref,
        "seed_policy": {"kind": "fixed", "seed": seed},
        "resource_limits": {"timeout_seconds": timeout_seconds},
        "success_exit_codes": exit_codes,
        "raw_outputs": ["stdout", "stderr"],
        "metric_output": metric_output,
        "known_limitations": known_limitations,
        "calibration_refs": calibration_refs,
    }


def _exact_object(
    value: object,
    fields: set[str],
    description: str,
) -> dict[str, object]:
    if type(value) is not dict or set(value) != fields:
        raise ValueError(f"{description} has invalid fields")
    return value


def _validate_metric_contract(value: object) -> dict[str, object]:
    contract = _exact_object(value, _METRIC_CONTRACT_FIELDS, "metric_output")
    if contract["source"] != "scorer_stdout":
        raise ValueError("metric_output source must be scorer_stdout")
    if contract["parser"] != "aros.scalar-metric-v1":
        raise ValueError("metric_output parser must be aros.scalar-metric-v1")
    metric_name = _identifier(contract["metric_name"], "metric_name")
    minimum = _finite_number(contract["minimum"], "metric minimum")
    maximum = _finite_number(contract["maximum"], "metric maximum")
    if minimum > maximum:
        raise ValueError("metric minimum must not exceed maximum")
    minimum_samples = contract["minimum_samples"]
    if type(minimum_samples) is not int or minimum_samples <= 0:
        raise ValueError("minimum_samples must be a positive plain integer")
    return {
        "source": "scorer_stdout",
        "parser": "aros.scalar-metric-v1",
        "metric_name": metric_name,
        "minimum": minimum,
        "maximum": maximum,
        "minimum_samples": minimum_samples,
    }


def _schema_one(record: dict[str, object], description: str) -> None:
    if type(record["schema_version"]) is not int or record["schema_version"] != 1:
        raise ValueError(f"{description} has invalid schema_version")


def _hash(value: object, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{field} must be lowercase 64-hex")
    return value


def _commit(value: object, field: str) -> str:
    if not isinstance(value, str) or _COMMIT.fullmatch(value) is None:
        raise ValueError(f"{field} must be a full lowercase Git commit")
    return value


def _timestamp(value: object, field: str) -> str:
    if not isinstance(value, str) or _UTC_TIMESTAMP.fullmatch(value) is None:
        raise ValueError(f"{field} must be a millisecond UTC timestamp")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError as error:
        raise ValueError(f"{field} must be a valid UTC timestamp") from error
    return value


def _self_hash(record: dict[str, object], field: str, description: str) -> str:
    value = _hash(record[field], field)
    if value != _record_sha256(record, field):
        raise ValueError(f"{description} self-hash mismatch")
    return value


def _validate_request(value: object) -> dict[str, object]:
    request = _exact_object(value, _REQUEST_FIELDS, "evaluation request")
    _schema_one(request, "evaluation request")
    eval_id = request["eval_id"]
    if not isinstance(eval_id, str) or _EVAL_ID.fullmatch(eval_id) is None:
        raise ValueError("evaluation request has invalid eval_id")
    key_hash = _hash(request["idempotency_key_sha256"], "idempotency_key_sha256")
    if eval_id != f"EVAL-{key_hash}":
        raise ValueError("evaluation request identity does not match idempotency key")
    _identifier(request["evaluator_id"], "evaluator_id")
    _identifier(request["evaluator_version"], "evaluator_version")
    _hash(request["descriptor_sha256"], "descriptor_sha256")
    _commit(request["candidate_commit"], "candidate_commit")
    _commit(request["apparatus_commit"], "apparatus_commit")
    _utf8(request["actor"], "actor", nonempty=True)
    _timestamp(request["created_at"], "created_at")
    _self_hash(request, "request_sha256", "evaluation request")
    return request


def _validate_execution(
    value: object,
    request: dict[str, object],
) -> dict[str, object]:
    execution = _exact_object(value, _EXECUTION_FIELDS, "evaluation execution")
    _schema_one(execution, "evaluation execution")
    if (
        execution["eval_id"] != request["eval_id"]
        or execution["request_sha256"] != request["request_sha256"]
    ):
        raise ValueError("evaluation execution lineage mismatch")
    _utf8(execution["host"], "host", nonempty=True)
    if type(execution["broker_pid"]) is not int or execution["broker_pid"] <= 1:
        raise ValueError("broker_pid must be a plain process ID")
    start_token = _utf8(
        execution["broker_start_token"],
        "broker_start_token",
        nonempty=True,
    )
    if _START_TOKEN.fullmatch(start_token) is None:
        raise ValueError("broker_start_token must be a Linux process start token")
    _timestamp(execution["claimed_at"], "claimed_at")
    _self_hash(execution, "execution_sha256", "evaluation execution")
    return execution


def _validate_run_link(
    value: object,
    request: dict[str, object],
    execution: dict[str, object],
) -> dict[str, object]:
    run_link = _exact_object(value, _RUN_LINK_FIELDS, "evaluation run link")
    _schema_one(run_link, "evaluation run link")
    if (
        run_link["eval_id"] != request["eval_id"]
        or run_link["request_sha256"] != request["request_sha256"]
        or run_link["execution_sha256"] != execution["execution_sha256"]
        or run_link["candidate_commit"] != request["candidate_commit"]
        or run_link["apparatus_commit"] != request["apparatus_commit"]
    ):
        raise ValueError("evaluation run link lineage mismatch")
    run_id = run_link["run_id"]
    if not isinstance(run_id, str) or _RUN_ID.fullmatch(run_id) is None:
        raise ValueError("evaluation run link has invalid run_id")
    for field in ("run_manifest_sha256", "bundle_sha256"):
        _hash(run_link[field], field)
    _commit(run_link["candidate_commit"], "candidate_commit")
    _commit(run_link["apparatus_commit"], "apparatus_commit")
    _timestamp(run_link["linked_at"], "linked_at")
    _self_hash(run_link, "run_link_sha256", "evaluation run link")
    return run_link


def _validate_content_receipt(
    value: object,
    run_id: str,
    stream: str,
) -> dict[str, object]:
    receipt = _exact_object(value, {"path", "bytes", "sha256"}, f"{stream} receipt")
    if receipt["path"] != f".aros/runs/{run_id}/{stream}.log":
        raise ValueError(f"{stream} receipt path mismatch")
    if type(receipt["bytes"]) is not int or receipt["bytes"] < 0:
        raise ValueError(f"{stream} receipt bytes must be a nonnegative plain integer")
    _hash(receipt["sha256"], f"{stream} sha256")
    return {
        "path": receipt["path"],
        "bytes": receipt["bytes"],
        "sha256": receipt["sha256"],
    }


def _validate_run_final(
    value: object,
    request: dict[str, object],
    run_link: dict[str, object],
) -> tuple[str, dict[str, object], dict[str, object], str]:
    if type(value) is not dict:
        raise ValueError("Run final must be a JSON object")
    required = {
        "schema_version",
        "run_id",
        "manifest_sha256",
        "state",
        "candidate_commit",
        "execution_bundle",
        "stdout",
        "stderr",
        "finished_at",
    }
    if not required.issubset(value):
        raise ValueError("Run final is missing evaluation lineage")
    _schema_one(value, "Run final")
    if (
        value["run_id"] != run_link["run_id"]
        or value["manifest_sha256"] != run_link["run_manifest_sha256"]
        or value["candidate_commit"] != run_link["candidate_commit"]
    ):
        raise ValueError("Run final lineage mismatch")
    process_state = value["state"]
    if not isinstance(process_state, str):
        raise ValueError("Run final state must be a string")

    bundle = _exact_object(
        value["execution_bundle"],
        {"candidate", "apparatus", "temp", "bundle_sha256"},
        "Run execution bundle",
    )
    portable: dict[str, object] = {}
    for name in ("candidate", "apparatus"):
        checkout = _exact_object(
            bundle[name],
            {"path", "commit", "tree"},
            f"Run {name} checkout",
        )
        if checkout["path"] != name:
            raise ValueError(f"Run {name} checkout path mismatch")
        portable[name] = {
            "path": name,
            "commit": _commit(checkout["commit"], f"{name} commit"),
            "tree": _commit(checkout["tree"], f"{name} tree"),
        }
    if bundle["temp"] != "tmp":
        raise ValueError("Run execution bundle temp path mismatch")
    portable["temp"] = "tmp"
    bundle_hash = _hash(bundle["bundle_sha256"], "bundle_sha256")
    if bundle_hash != _json_sha256(portable) or bundle_hash != run_link["bundle_sha256"]:
        raise ValueError("Run execution bundle hash mismatch")
    candidate = portable["candidate"]
    apparatus = portable["apparatus"]
    assert isinstance(candidate, dict) and isinstance(apparatus, dict)
    if (
        candidate["commit"] != request["candidate_commit"]
        or apparatus["commit"] != request["apparatus_commit"]
    ):
        raise ValueError("Run execution bundle commit lineage mismatch")
    run_id = str(run_link["run_id"])
    stdout = _validate_content_receipt(value["stdout"], run_id, "stdout")
    stderr = _validate_content_receipt(value["stderr"], run_id, "stderr")
    finished_at = _timestamp(value["finished_at"], "Run finished_at")
    return process_state, stdout, stderr, finished_at


def _validate_measurement(
    value: object,
    measurement_state: object,
) -> dict[str, object]:
    measurement = _exact_object(value, _MEASUREMENT_FIELDS, "measurement")
    if not isinstance(measurement_state, str) or measurement_state not in {
        "valid",
        "underpowered",
        "invalid_eval",
        "not_available",
    }:
        raise ValueError("invalid measurement_state")
    if measurement["measurement_state"] != measurement_state:
        raise ValueError("measurement state mismatch")
    metric_name = _identifier(measurement["metric_name"], "metric_name")
    if measurement["parser"] != "aros.scalar-metric-v1":
        raise ValueError("measurement parser mismatch")
    metric = measurement["metric"]
    sample_count = measurement["sample_count"]
    if measurement_state in {"valid", "underpowered"}:
        _finite_number(metric, "metric")
        if type(sample_count) is not int or sample_count < 0:
            raise ValueError("sample_count must be a plain nonnegative integer")
        if measurement_state == "valid" and sample_count == 0:
            raise ValueError("valid measurement must contain at least one sample")
    elif metric is not None or sample_count is not None:
        raise ValueError("unavailable measurement must have null metric and sample_count")
    return {
        "measurement_state": measurement_state,
        "metric": metric,
        "sample_count": sample_count,
        "metric_name": metric_name,
        "parser": "aros.scalar-metric-v1",
    }


def build_measurement_receipt(
    request: dict[str, object],
    execution: dict[str, object],
    run_link: dict[str, object],
    run_final: dict[str, object],
    measurement_state: str,
    measurement: dict[str, object],
    bundle_cleanup_state: str,
) -> dict[str, object]:
    """Build one exact visible terminal measurement receipt."""
    validated_request = _validate_request(request)
    validated_execution = _validate_execution(execution, validated_request)
    validated_link = _validate_run_link(
        run_link,
        validated_request,
        validated_execution,
    )
    process_state, stdout, stderr, finished_at = _validate_run_final(
        run_final,
        validated_request,
        validated_link,
    )
    normalized_measurement = _validate_measurement(measurement, measurement_state)
    if (process_state, measurement_state) not in _ALLOWED_STATE_PAIRS:
        raise ValueError("invalid process and measurement state pairing")
    if not isinstance(bundle_cleanup_state, str) or bundle_cleanup_state not in {
        "removed",
        "preserved",
    }:
        raise ValueError("invalid bundle_cleanup_state")
    run_final_sha256 = _bounded_canonical_sha256(run_final, "Run final")
    receipt: dict[str, object] = {
        "schema_version": 1,
        "eval_id": validated_request["eval_id"],
        "evaluation_state": "completed",
        "referenced_process_state": process_state,
        "measurement_state": measurement_state,
        "descriptor_sha256": validated_request["descriptor_sha256"],
        "request_sha256": validated_request["request_sha256"],
        "execution_sha256": validated_execution["execution_sha256"],
        "run_id": validated_link["run_id"],
        "run_manifest_sha256": validated_link["run_manifest_sha256"],
        "run_final_sha256": run_final_sha256,
        "bundle_sha256": validated_link["bundle_sha256"],
        "candidate_commit": validated_link["candidate_commit"],
        "apparatus_commit": validated_link["apparatus_commit"],
        "metric": normalized_measurement["metric"],
        "sample_count": normalized_measurement["sample_count"],
        "metric_name": normalized_measurement["metric_name"],
        "parser": normalized_measurement["parser"],
        "bundle_cleanup_state": bundle_cleanup_state,
        "stdout": stdout,
        "stderr": stderr,
        "finished_at": finished_at,
    }
    receipt["receipt_sha256"] = _record_sha256(receipt, "receipt_sha256")
    return receipt


def validate_measurement_receipt(value: object) -> dict[str, object]:
    """Validate and copy one exact visible terminal measurement receipt."""
    receipt = _exact_object(value, _RECEIPT_FIELDS, "measurement receipt")
    _schema_one(receipt, "measurement receipt")
    eval_id = receipt["eval_id"]
    if not isinstance(eval_id, str) or _EVAL_ID.fullmatch(eval_id) is None:
        raise ValueError("measurement receipt has invalid eval_id")
    if receipt["evaluation_state"] != "completed":
        raise ValueError("terminal measurement receipt must be completed")
    process_state = receipt["referenced_process_state"]
    measurement_state = receipt["measurement_state"]
    measurement = _validate_measurement(
        {
            "measurement_state": measurement_state,
            "metric": receipt["metric"],
            "sample_count": receipt["sample_count"],
            "metric_name": receipt["metric_name"],
            "parser": receipt["parser"],
        },
        measurement_state,
    )
    if not isinstance(process_state, str) or (
        process_state,
        measurement_state,
    ) not in _ALLOWED_STATE_PAIRS:
        raise ValueError("invalid process and measurement state pairing")
    for field in (
        "descriptor_sha256",
        "request_sha256",
        "execution_sha256",
        "run_manifest_sha256",
        "run_final_sha256",
        "bundle_sha256",
    ):
        _hash(receipt[field], field)
    run_id = receipt["run_id"]
    if not isinstance(run_id, str) or _RUN_ID.fullmatch(run_id) is None:
        raise ValueError("measurement receipt has invalid run_id")
    _commit(receipt["candidate_commit"], "candidate_commit")
    _commit(receipt["apparatus_commit"], "apparatus_commit")
    cleanup_state = receipt["bundle_cleanup_state"]
    if not isinstance(cleanup_state, str) or cleanup_state not in {
        "removed",
        "preserved",
    }:
        raise ValueError("invalid bundle_cleanup_state")
    stdout = _validate_content_receipt(receipt["stdout"], run_id, "stdout")
    stderr = _validate_content_receipt(receipt["stderr"], run_id, "stderr")
    _timestamp(receipt["finished_at"], "finished_at")
    _self_hash(receipt, "receipt_sha256", "measurement receipt")
    validated = dict(receipt)
    validated.update(
        {
            "metric": measurement["metric"],
            "sample_count": measurement["sample_count"],
            "metric_name": measurement["metric_name"],
            "parser": measurement["parser"],
            "stdout": stdout,
            "stderr": stderr,
        }
    )
    return validated


def parse_scalar_metric(
    raw: bytes,
    contract: dict[str, object],
) -> dict[str, object]:
    """Parse one bounded JSON scalar metric document."""
    if type(raw) is not bytes:
        raise ValueError("metric output must be bytes")
    normalized_contract = _validate_metric_contract(contract)
    if len(raw) > _MAX_METRIC_BYTES:
        raise ValueError("metric output exceeds 65,536 bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("metric output must be strict UTF-8") from error
    try:
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
            parse_int=_bounded_int,
            parse_float=_bounded_float,
        )
    except RecursionError as error:
        raise ValueError("metric JSON is too deeply nested") from error
    except json.JSONDecodeError as error:
        raise ValueError("metric output must contain exactly one JSON document") from error
    if not isinstance(value, dict):
        raise ValueError("metric document must be a JSON object")
    if set(value) != _METRIC_FIELDS:
        raise ValueError("metric document has invalid fields")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("metric document has invalid schema_version")

    metric = value["metric"]
    if type(metric) not in {int, float}:
        raise ValueError("metric must be a JSON number")
    minimum = normalized_contract["minimum"]
    maximum = normalized_contract["maximum"]
    if metric < minimum or metric > maximum:  # type: ignore[operator]
        raise ValueError("metric is outside the declared range")

    sample_count = value["sample_count"]
    if type(sample_count) is not int or sample_count < 0:
        raise ValueError("sample_count must be a plain nonnegative integer")
    minimum_samples = normalized_contract["minimum_samples"]
    measurement_state = (
        "underpowered" if sample_count < minimum_samples else "valid"  # type: ignore[operator]
    )
    return {
        "measurement_state": measurement_state,
        "metric": metric,
        "sample_count": sample_count,
        "metric_name": normalized_contract["metric_name"],
        "parser": normalized_contract["parser"],
    }
