from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path, PurePosixPath


SOURCE_URL = "https://github.com/1a1a11a/libCacheSim.git"
SOURCE_COMMIT = "da022c2945146e9577d91375a48d53850d7041a3"
SOURCE_TREE = "d59c0319fff072788ab5d5a5c1f204f758082c80"
POLICIES = ("LRU", "ARC", "WTinyLFU", "Sieve", "S3FIFO", "BeladySize")
REFERENCES = ("Sieve", "S3FIFO")
FRACTIONS = ("0.01", "0.05", "0.1")
HEX = set("0123456789abcdef")
FORBIDDEN_OUTCOMES = {
    "aggregate",
    "objective",
    "overall",
    "pass",
    "rank",
    "ranking",
    "reward",
    "score",
}

INDEX_KEYS = {
    "schema_version",
    "checkout",
    "task_root",
    "source_receipt",
    "task_manifest",
    "host_r3_manifest",
    "r0_receipts",
    "r1_receipts",
    "r2_receipts",
    "calibration",
    "calibration_sha256",
    "r3",
}
R3_INDEX_KEYS = {
    "frozen_package",
    "candidate_r0_receipt",
    "candidate_r2_receipt",
    "ledger",
    "receipt",
}
SOURCE_KEYS = {
    "schema_version",
    "repository_url",
    "commit",
    "tree",
    "clean",
    "commands",
    "versions",
    "compilers",
    "interpreter",
    "platform",
    "binary",
    "binary_sha256",
    "receipt_sha256",
}
TASK_MANIFEST_KEYS = {
    "schema_version",
    "source_commit",
    "cache_fractions",
    "traces",
    "r3_commitment_sha256",
    "manifest_sha256",
}
HOST_MANIFEST_KEYS = {
    "schema_version",
    "source_commit",
    "cache_fractions",
    "traces",
    "manifest_sha256",
}
TRACE_KEYS = {
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
DIAGNOSTIC_KEYS = {
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
R0_KEYS = {
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
R0_EVALUATOR_KEYS = {
    "evaluate_sha256",
    "scope_sha256",
    "evidence_sha256",
    "r0_probes_sha256",
    "cachesim_sha256",
    "linux_subreaper_sha256",
}
PORTFOLIO_EVALUATOR_KEYS = {
    *R0_EVALUATOR_KEYS,
    "portfolio_sha256",
    "portfolio_evidence_sha256",
    "oracle_sha256",
    "records_sha256",
    "diagnostics_sha256",
    "source_lock_sha256",
    "run_aros_cache_eval_sha256",
}
PORTFOLIO_KEYS = {
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
MEASUREMENT_BASE_KEYS = {
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
    "measurement_sha256",
}
PHASE_KEYS = {
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
PROCESS_KEYS = {
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
CALIBRATION_KEYS = {
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
PACKAGE_KEYS = {
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
REF_FIELDS = (
    "claim_ref",
    "preregistration_ref",
    "review_ref",
    "principal_response_ref",
    "reproduction_ref",
)
LEDGER_KEYS = {
    "schema_version",
    "state",
    "authority_id",
    "final_receipt_path",
    "requested_at_unix_ns",
    "frozen_package_file_sha256",
    "frozen_commit",
    "frozen_tree",
    "candidate_commit",
    "candidate_tree",
    "policy",
    "candidate_diff_sha256",
    "policy_contract_sha256",
    "git_ref_sha256s",
    "host_r3_manifest_sha256",
    "r3_commitment_sha256",
    "calibration_sha256",
    "calibration_file_sha256",
    "source_receipt_sha256",
    "source_receipt_file_sha256",
    "candidate_r0_receipt_sha256",
    "candidate_r0_file_sha256",
    "r2_receipt_sha256",
    "r2_receipt_file_sha256",
    "binary_sha256",
    "r3_evaluator_sha256s",
    "portfolio_evaluator_sha256s",
    "trace_sha256s",
    "private_snapshot",
    "ledger_sha256",
}
R3_RECEIPT_KEYS = {
    "schema_version",
    "receipt_version",
    "rung",
    "state",
    "authority_id",
    "final_receipt_path",
    "frozen_commit",
    "candidate_commit",
    "candidate_tree",
    "policy",
    "output_path",
    "ledger_path",
    "ledger_sha256",
    "ledger_intended_sha256",
    "ledger_file_sha256",
    "ledger_size_bytes",
    "r3_commitment_sha256",
    "host_r3_manifest_sha256",
    "calibration_sha256",
    "r3_evaluator_sha256s",
    "source_receipt_sha256",
    "r0_receipt_sha256",
    "r2_receipt_sha256",
    "started_at_unix_ns",
    "ended_at_unix_ns",
    "portfolio_receipt_path",
    "portfolio_receipt_sha256",
    "measurements",
    "failures",
    "constraints",
    "receipt_sha256",
}


class VerificationError(ValueError):
    pass


def _unique_object(items):
    value = {}
    for key, item in items:
        if key in value:
            raise VerificationError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _invalid_constant(value):
    raise VerificationError(f"non-finite JSON constant: {value}")


def _finite_float(value):
    parsed = float(value)
    if parsed != parsed or parsed in {float("inf"), float("-inf")}:
        raise VerificationError(f"non-finite JSON number: {value}")
    return parsed


def canonical_bytes(value):
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as error:
        raise VerificationError("value is not canonical JSON") from error


def _record_sha256(value, field):
    projected = {key: item for key, item in value.items() if key != field}
    return hashlib.sha256(canonical_bytes(projected)).hexdigest()


def _sha256_file(path):
    digest = hashlib.sha256()
    try:
        if path.is_symlink() or not path.is_file():
            raise VerificationError(f"regular file required: {path}")
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as error:
        raise VerificationError(f"cannot hash retained file: {path}") from error
    return digest.hexdigest()


def _load_object(path, *, maximum=64 * 1024 * 1024):
    try:
        candidate = Path(path)
        if candidate.is_symlink() or not candidate.is_file():
            raise VerificationError(f"regular JSON file required: {candidate}")
        if candidate.stat().st_size > maximum:
            raise VerificationError(f"JSON file exceeds verifier limit: {candidate}")
        value = json.loads(
            candidate.read_bytes(),
            object_pairs_hook=_unique_object,
            parse_constant=_invalid_constant,
            parse_float=_finite_float,
        )
    except VerificationError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise VerificationError(f"invalid JSON object: {path}") from error
    if not isinstance(value, dict):
        raise VerificationError(f"JSON object required: {path}")
    return value


def _exact(value, keys, label):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise VerificationError(f"{label} keys mismatch")
    return value


def _integer(value, label, minimum=0):
    if type(value) is not int or value < minimum:
        raise VerificationError(f"{label} must be an integer >= {minimum}")
    return value


def _string(value, label):
    if type(value) is not str or not value or "\x00" in value:
        raise VerificationError(f"{label} must be a nonempty string")
    return value


def _hash(value, label, length=64):
    if (
        type(value) is not str
        or len(value) != length
        or any(character not in HEX for character in value)
    ):
        raise VerificationError(f"{label} must be a lowercase hash")
    return value


def _path(value, label, *, exists=True):
    raw = Path(_string(value, label))
    if not raw.is_absolute() or raw.is_symlink():
        raise VerificationError(f"{label} must be an absolute non-symlink path")
    try:
        return raw.resolve(strict=exists)
    except OSError as error:
        raise VerificationError(f"{label} path is unavailable") from error


def _relative(root, value, label):
    relative = PurePosixPath(_string(value, label))
    if relative.is_absolute() or ".." in relative.parts or relative.as_posix() != value:
        raise VerificationError(f"{label} must be a safe relative path")
    candidate = root.joinpath(*relative.parts)
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as error:
        raise VerificationError(f"{label} escapes retained evidence") from error
    if candidate.is_symlink() or not resolved.is_file():
        raise VerificationError(f"{label} must resolve to a regular file")
    return resolved


def _relative_absent(root, value, label):
    relative = PurePosixPath(_string(value, label))
    if relative.is_absolute() or ".." in relative.parts or relative.as_posix() != value:
        raise VerificationError(f"{label} must be a safe relative path")
    candidate = root.joinpath(*relative.parts)
    try:
        candidate.absolute().relative_to(root.absolute())
    except ValueError as error:
        raise VerificationError(f"{label} escapes retained evidence") from error
    if os.path.lexists(candidate):
        raise VerificationError(f"{label} was recorded absent but exists")
    return candidate


def _self_hash(value, field, label):
    digest = _record_sha256(value, field)
    if value.get(field) != digest:
        raise VerificationError(f"{label} self-hash mismatch")
    return digest


def _forbid_outcomes(value, label):
    if isinstance(value, dict):
        forbidden = set(value) & FORBIDDEN_OUTCOMES
        if forbidden:
            raise VerificationError(f"{label} contains outcome interpretation")
        for item in value.values():
            _forbid_outcomes(item, label)
    elif isinstance(value, list):
        for item in value:
            _forbid_outcomes(item, label)


def _normalized_decimal(value, label):
    raw = _string(value, label)
    if raw.startswith("-"):
        sign = "-"
        raw = raw[1:]
    else:
        sign = ""
    if not raw or raw.count(".") > 1 or any(character not in "0123456789." for character in raw):
        raise VerificationError(f"{label} must be a finite decimal string")
    integer, dot, fraction = raw.partition(".")
    if not integer or (dot and not fraction):
        raise VerificationError(f"{label} must be a finite decimal string")
    integer = integer.lstrip("0") or "0"
    fraction = fraction.rstrip("0")
    result = integer + ("." + fraction if fraction else "")
    if result == "0":
        sign = ""
    return sign + result


def _decimal_key(value):
    normalized = _normalized_decimal(value, "decimal")
    sign = -1 if normalized.startswith("-") else 1
    raw = normalized.lstrip("-")
    integer, _, fraction = raw.partition(".")
    return sign, int(integer + fraction), len(fraction)


def _decimal_compare(left, right):
    left_sign, left_value, left_scale = _decimal_key(left)
    right_sign, right_value, right_scale = _decimal_key(right)
    scale = max(left_scale, right_scale)
    left_integer = left_sign * left_value * 10 ** (scale - left_scale)
    right_integer = right_sign * right_value * 10 ** (scale - right_scale)
    return (left_integer > right_integer) - (left_integer < right_integer)


def _decimal_sort(values):
    ordered = []
    for value in values:
        normalized = _normalized_decimal(value, "measurement decimal")
        position = 0
        while position < len(ordered) and _decimal_compare(ordered[position], normalized) <= 0:
            position += 1
        ordered.insert(position, normalized)
    return ordered


def _times_nine_tenths(value):
    normalized = _normalized_decimal(value, "throughput median")
    if normalized.startswith("-"):
        raise VerificationError("throughput median must be nonnegative")
    integer, _, fraction = normalized.partition(".")
    numerator = int(integer + fraction) * 9
    scale = len(fraction) + 1
    digits = str(numerator).rjust(scale + 1, "0")
    raw = digits[:-scale] + "." + digits[-scale:]
    return _normalized_decimal(raw, "throughput floor")


def _git(repository, *arguments, binary=False, allowed=(0,)):
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    environment["GIT_NO_LAZY_FETCH"] = "1"
    command = [
        "git",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.untrackedCache=false",
        "-c",
        "core.fileMode=true",
        "-c",
        "core.trustctime=true",
        "-c",
        "core.checkStat=default",
        *arguments,
    ]
    try:
        result = subprocess.run(
            command,
            cwd=repository,
            capture_output=True,
            check=False,
            env=environment,
        )
    except OSError as error:
        raise VerificationError("Git validation could not start") from error
    if result.returncode not in allowed:
        raise VerificationError(f"Git validation failed: {' '.join(arguments)}")
    if binary:
        return result.stdout
    try:
        return result.stdout.decode("utf-8").strip()
    except UnicodeError as error:
        raise VerificationError("Git returned non-UTF-8 text") from error


def _normalize_url(value):
    normalized = value.strip().rstrip("/")
    if normalized.endswith(".git"):
        normalized = normalized[:-4]
    return normalized.rstrip("/")


def _raw_checkout(checkout):
    top = Path(_git(checkout, "rev-parse", "--show-toplevel")).resolve(strict=True)
    if top != checkout:
        raise VerificationError("source checkout is not its Git top level")
    head = _git(checkout, "rev-parse", "HEAD")
    tree = _git(checkout, "rev-parse", "HEAD^{tree}")
    object_format = _git(checkout, "rev-parse", "--show-object-format")
    oid_length = {"sha1": 40, "sha256": 64}.get(object_format)
    if oid_length is None:
        raise VerificationError("unsupported Git object format")
    tree_entries = {}
    raw_tree = _git(checkout, "ls-tree", "-r", "-z", "--full-tree", "HEAD", binary=True)
    for record in raw_tree.split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_path = record.split(b"\t", 1)
            mode, kind, oid = metadata.decode("ascii").split(" ")
            path = raw_path.decode("utf-8")
        except (UnicodeError, ValueError) as error:
            raise VerificationError("malformed Git tree entry") from error
        if kind != "blob" or mode not in {"100644", "100755", "120000"}:
            raise VerificationError("unsupported Git tree entry")
        if len(oid) != oid_length or any(character not in HEX for character in oid):
            raise VerificationError("invalid Git object ID")
        relative = PurePosixPath(path)
        if relative.is_absolute() or ".." in relative.parts or path in tree_entries:
            raise VerificationError("unsafe Git tree path")
        tree_entries[path] = (mode, oid)
    index_entries = {}
    for record in _git(checkout, "ls-files", "--stage", "-z", binary=True).split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_path = record.split(b"\t", 1)
            mode, oid, stage = metadata.decode("ascii").split(" ")
            path = raw_path.decode("utf-8")
        except (UnicodeError, ValueError) as error:
            raise VerificationError("malformed Git index entry") from error
        if stage != "0" or path in index_entries:
            raise VerificationError("Git index contains conflict stages")
        index_entries[path] = (mode, oid)
    if index_entries != tree_entries:
        raise VerificationError("Git index does not exactly match HEAD")
    tracked_directories = set()
    for relative in tree_entries:
        parent = PurePosixPath(relative).parent
        while parent.as_posix() != ".":
            tracked_directories.add(parent.as_posix())
            parent = parent.parent
    observed = set()
    pending = [(checkout, "")]
    while pending:
        directory, prefix = pending.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError as error:
            raise VerificationError("source checkout cannot be scanned") from error
        for entry in entries:
            if not prefix and entry.name == ".git":
                continue
            relative = f"{prefix}/{entry.name}" if prefix else entry.name
            if not prefix and entry.name.startswith("_build"):
                continue
            if entry.is_dir(follow_symlinks=False):
                if relative not in tracked_directories:
                    raise VerificationError(f"untracked source directory: {relative}")
                pending.append((Path(entry.path), relative))
            else:
                observed.add(relative)
    if observed != set(tree_entries):
        raise VerificationError("source worktree files do not exactly match HEAD")
    for relative, (mode, oid) in tree_entries.items():
        path = checkout.joinpath(*PurePosixPath(relative).parts)
        if mode == "120000":
            if not path.is_symlink():
                raise VerificationError("tracked symlink type changed")
            raw = os.fsencode(os.readlink(path))
        else:
            if path.is_symlink() or not path.is_file():
                raise VerificationError("tracked regular file type changed")
            raw = path.read_bytes()
            executable = bool(path.stat().st_mode & 0o100)
            if executable is not (mode == "100755"):
                raise VerificationError("tracked executable mode changed")
        constructor = hashlib.sha1 if object_format == "sha1" else hashlib.sha256
        actual = constructor(f"blob {len(raw)}\0".encode("ascii") + raw).hexdigest()
        if actual != oid:
            raise VerificationError(f"raw checkout bytes differ: {relative}")
    return head, tree


def _verify_inventory(root, records):
    if not isinstance(records, list):
        raise VerificationError("evidence inventory must be an array")
    observed_paths = []
    for item in records:
        _exact(item, {"path", "identity", "mode", "size_bytes", "sha256"}, "inventory")
        path = _relative(root, item["path"], "inventory path")
        metadata = path.stat()
        identity = _exact(item["identity"], {"device", "inode"}, "inventory identity")
        if (
            identity != {"device": metadata.st_dev, "inode": metadata.st_ino}
            or item["mode"] != metadata.st_mode & 0o777
            or item["size_bytes"] != metadata.st_size
            or item["sha256"] != _sha256_file(path)
        ):
            raise VerificationError("evidence inventory binding mismatch")
        observed_paths.append(item["path"])
    actual_paths = sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink() and path.name != "receipt.json"
    )
    if observed_paths != actual_paths:
        raise VerificationError("evidence inventory is incomplete or unordered")


def _verify_r0_inventory(root, records):
    if not isinstance(records, list):
        raise VerificationError("R0 evidence inventory must be an array")
    paths = []
    expected_keys = {
        "path",
        "identity",
        "size_bytes",
        "sha256",
        "present",
        "observed_identity",
        "observed_size_bytes",
        "observed_sha256",
        "binding_intact",
    }
    for item in records:
        _exact(item, expected_keys, "R0 evidence inventory")
        if item["present"] is False:
            _relative_absent(root, item["path"], "R0 absent evidence path")
            if any(
                item[field] is not None
                for field in (
                    "identity", "size_bytes", "sha256", "observed_identity",
                    "observed_size_bytes", "observed_sha256",
                )
            ) or item["binding_intact"] is not False:
                raise VerificationError("R0 absent evidence facts are inconsistent")
            continue
        path = _relative(root, item["path"], "R0 evidence path")
        metadata = path.stat()
        identity = {"device": metadata.st_dev, "inode": metadata.st_ino}
        digest = _sha256_file(path)
        if (
            item["identity"] != identity
            or item["observed_identity"] != identity
            or item["size_bytes"] != metadata.st_size
            or item["observed_size_bytes"] != metadata.st_size
            or item["sha256"] != digest
            or item["observed_sha256"] != digest
            or item["present"] is not True
            or item["binding_intact"] is not True
        ):
            raise VerificationError("R0 evidence inventory binding mismatch")
        paths.append(item["path"])
    actual = sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink() and path.name != "receipt.json"
    )
    if paths != actual:
        raise VerificationError("R0 evidence inventory is incomplete or unordered")


def _verify_source(path, checkout):
    receipt = _exact(_load_object(path), SOURCE_KEYS, "source receipt")
    digest = _self_hash(receipt, "receipt_sha256", "source receipt")
    if (
        receipt["schema_version"] != 1
        or receipt["repository_url"] != SOURCE_URL
        or receipt["commit"] != SOURCE_COMMIT
        or receipt["tree"] != SOURCE_TREE
        or receipt["clean"] is not True
    ):
        raise VerificationError("source receipt differs from the approved pin")
    expected_commands = (
        [
            "cmake", "-S", ".", "-B", "_build", "-G", "Ninja",
            "-DCMAKE_BUILD_TYPE=Release", "-DENABLE_TESTS=ON",
        ],
        ["cmake", "--build", "_build", "-j", "8"],
        ["ctest", "--test-dir", "_build", "--output-on-failure"],
    )
    commands = receipt["commands"]
    if not isinstance(commands, list) or len(commands) != 3:
        raise VerificationError("source command receipt count mismatch")
    for item, argv in zip(commands, expected_commands):
        _exact(item, {"argv", "returncode", "stdout_sha256", "stderr_sha256"}, "source command")
        if item["argv"] != argv or item["returncode"] != 0:
            raise VerificationError("source command receipt mismatch")
        _hash(item["stdout_sha256"], "source stdout")
        _hash(item["stderr_sha256"], "source stderr")
    _exact(receipt["versions"], {"cmake", "ninja"}, "source versions")
    compilers = _exact(receipt["compilers"], {"c", "cxx"}, "source compilers")
    for compiler in compilers.values():
        _exact(compiler, {"path", "version"}, "source compiler")
        if not Path(_string(compiler["path"], "compiler path")).is_absolute():
            raise VerificationError("compiler path must be absolute")
        _string(compiler["version"], "compiler version")
    head, tree = _raw_checkout(checkout)
    if _git(checkout, "rev-parse", f"{SOURCE_COMMIT}^{{tree}}") != SOURCE_TREE:
        raise VerificationError("pinned source commit/tree object mismatch")
    urls = _git(checkout, "remote", "get-url", "--all", "origin").splitlines()
    if len(urls) != 1 or _normalize_url(urls[0]) != _normalize_url(SOURCE_URL):
        raise VerificationError("source origin URL mismatch")
    push = _git(
        checkout,
        "config",
        "--get-regexp",
        r"^remote\.origin\.pushurl$",
        allowed=(0, 1),
    )
    if push:
        raise VerificationError("source checkout has an explicit push URL")
    relative = PurePosixPath(_string(receipt["binary"], "source binary"))
    if relative.is_absolute() or ".." in relative.parts:
        raise VerificationError("source binary path is unsafe")
    binary = checkout.joinpath(*relative.parts)
    if _sha256_file(binary) != receipt["binary_sha256"]:
        raise VerificationError("source binary hash mismatch")
    return receipt, {
        "state": "verified",
        "commit": SOURCE_COMMIT,
        "tree": SOURCE_TREE,
        "checkout_head": head,
        "checkout_tree": tree,
        "receipt_sha256": digest,
        "binary_sha256": receipt["binary_sha256"],
    }


def _verify_diagnostic(value, trace):
    _exact(value, DIAGNOSTIC_KEYS, "trace diagnostic")
    _self_hash(value, "diagnostic_sha256", "trace diagnostic")
    if (
        value["schema_version"] != 1
        or value["trace_id"] != trace["trace_id"]
        or value["request_count"] != trace["max_requests"]
        or value["working_set_bytes"] != trace["working_set_bytes"]
    ):
        raise VerificationError("trace diagnostic binding mismatch")
    unique = _integer(value["unique_object_count"], "unique object count", 1)
    object_fraction = _exact(
        value["one_hit_object_fraction"], {"numerator", "denominator"}, "one-hit object fraction"
    )
    request_fraction = _exact(
        value["one_hit_request_fraction"], {"numerator", "denominator"}, "one-hit request fraction"
    )
    if (
        object_fraction["denominator"] != unique
        or request_fraction["denominator"] != trace["max_requests"]
        or request_fraction["numerator"] != object_fraction["numerator"]
        or type(object_fraction["numerator"]) is not int
        or not 0 <= object_fraction["numerator"] <= unique
    ):
        raise VerificationError("one-hit diagnostic is invalid")
    reuse = _exact(
        value["reuse_distance"], {"bin_convention", "counts", "no_next_count"}, "reuse distance"
    )
    _string(reuse["bin_convention"], "reuse bin convention")
    if not isinstance(reuse["counts"], dict):
        raise VerificationError("reuse counts must be an object")
    count = _integer(reuse["no_next_count"], "no-next count")
    for key, item in reuse["counts"].items():
        if not key.isdigit() or (key.startswith("0") and key != "0"):
            raise VerificationError("reuse-distance bin is not canonical")
        count += _integer(item, "reuse-distance count")
    if count != trace["max_requests"]:
        raise VerificationError("reuse-distance counts are incomplete")


def _verify_trace(value, allowed_splits):
    trace = _exact(value, TRACE_KEYS, "trace")
    if trace["split"] not in allowed_splits or trace["trace_type"] != "oracleGeneral":
        raise VerificationError("trace split or type mismatch")
    for field in (
        "trace_id", "organization", "application", "dataset", "provenance_url", "license_ref"
    ):
        _string(trace[field], f"trace {field}")
    _hash(trace["origin_sha256"], "trace origin")
    _hash(trace["sha256"], "trace hash")
    _hash(trace["diagnostic_sha256"], "trace diagnostic hash")
    _integer(trace["start_request"], "trace start")
    _integer(trace["warmup_seconds"], "trace warmup", 1)
    maximum = _integer(trace["max_requests"], "trace maximum", 1)
    _integer(trace["working_set_bytes"], "working set", 1)
    size = _integer(trace["size_bytes"], "trace size", 1)
    if size != maximum * 24:
        raise VerificationError("OracleGeneral size does not match max_requests")
    path = _path(trace["path"], "trace path")
    if path.stat().st_size != size or _sha256_file(path) != trace["sha256"]:
        raise VerificationError("trace byte binding mismatch")
    _verify_diagnostic(trace["diagnostics"], trace)
    if trace["diagnostic_sha256"] != trace["diagnostics"]["diagnostic_sha256"]:
        raise VerificationError("trace diagnostic hash projection mismatch")
    return trace


def _split_constraints(public, private):
    all_traces = [*public, *private]
    identifiers = [item["trace_id"] for item in all_traces]
    file_hashes = [item["sha256"] for item in all_traces]
    if len(set(identifiers)) != len(identifiers) or len(set(file_hashes)) != len(file_hashes):
        raise VerificationError("trace identities or physical bytes overlap")
    origins = {}
    for trace in all_traces:
        start = trace["start_request"]
        end = start + trace["max_requests"]
        for previous_start, previous_end in origins.setdefault(trace["origin_sha256"], []):
            if not (end <= previous_start or previous_end <= start):
                raise VerificationError("trace origin intervals overlap")
        origins[trace["origin_sha256"]].append((start, end))
    dev = [item for item in public if item["split"] == "dev"]
    visible = [item for item in public if item["split"] == "visible"]
    if len(dev) < 3 or len({(item["organization"], item["application"]) for item in dev}) < 2:
        raise VerificationError("dev split is incomplete")
    if not visible or not private:
        raise VerificationError("visible and R3 splits are required")
    for item in visible:
        for dev_item in dev:
            if item["application"] != dev_item["application"]:
                continue
            if item["origin_sha256"] != dev_item["origin_sha256"]:
                raise VerificationError("visible identity reuses a dev application")
            left = (item["start_request"], item["start_request"] + item["max_requests"])
            right = (dev_item["start_request"], dev_item["start_request"] + dev_item["max_requests"])
            if not (left[1] <= right[0] or right[1] <= left[0]):
                raise VerificationError("visible interval overlaps dev")
    organizations = {item["organization"] for item in public}
    if any(item["organization"] in organizations for item in private):
        raise VerificationError("R3 organization is not unseen")


def _task_visible_files(task_root):
    files = []
    for path in task_root.rglob("*"):
        try:
            relative = path.relative_to(task_root)
        except ValueError:
            continue
        if relative.parts and relative.parts[0] == ".git":
            continue
        if path.is_symlink():
            raise VerificationError("task-visible tree contains a symlink")
        if path.is_file():
            files.append(path)
    return files


def _private_needles(public, private):
    public_values = set()
    for trace in public:
        for value in trace.values():
            if type(value) is str:
                public_values.add(value.encode())
    needles = set()
    for trace in private:
        for field in (
            "trace_id", "organization", "application", "dataset", "provenance_url",
            "license_ref", "path", "origin_sha256", "sha256", "diagnostic_sha256",
        ):
            raw = str(trace[field]).encode()
            if len(raw) >= 4 and raw not in public_values:
                needles.add(raw)
        needles.add(Path(trace["path"]).read_bytes())
    return needles


def _verify_no_task_leak(task_root, public, private, extra_blobs=()):
    needles = _private_needles(public, private)
    for path in _task_visible_files(task_root):
        raw = path.read_bytes()
        if any(needle and needle in raw for needle in needles):
            raise VerificationError(f"private R3 bytes leak into task root: {path}")
    for raw in extra_blobs:
        if any(needle and needle in raw for needle in needles):
            raise VerificationError("private R3 bytes leak into frozen Git blobs")


def _verify_manifests(task_path, host_path, task_root):
    task = _exact(_load_object(task_path), TASK_MANIFEST_KEYS, "task manifest")
    host = _exact(_load_object(host_path), HOST_MANIFEST_KEYS, "host R3 manifest")
    task_digest = _self_hash(task, "manifest_sha256", "task manifest")
    host_digest = _self_hash(host, "manifest_sha256", "host R3 manifest")
    if (
        task["schema_version"] != 1
        or host["schema_version"] != 1
        or task["source_commit"] != SOURCE_COMMIT
        or host["source_commit"] != SOURCE_COMMIT
        or task["cache_fractions"] != [0.01, 0.05, 0.1]
        or host["cache_fractions"] != [0.01, 0.05, 0.1]
        or task["r3_commitment_sha256"] != host_digest
    ):
        raise VerificationError("task/private manifest binding mismatch")
    try:
        Path(task_path).resolve(strict=True).relative_to(task_root)
    except ValueError as error:
        raise VerificationError("task manifest is outside task_root") from error
    if not isinstance(task["traces"], list) or not isinstance(host["traces"], list):
        raise VerificationError("manifest traces must be arrays")
    public = [_verify_trace(item, {"dev", "visible"}) for item in task["traces"]]
    private = [_verify_trace(item, {"r3"}) for item in host["traces"]]
    _split_constraints(public, private)
    _verify_no_task_leak(task_root, public, private)
    return task, host, public, private, {
        "state": "verified",
        "task_manifest_sha256": task_digest,
        "r3_commitment_sha256": host_digest,
        "dev_traces": sum(item["split"] == "dev" for item in public),
        "visible_traces": sum(item["split"] == "visible" for item in public),
        "sealed_r3_traces": len(private),
    }


def _git_blob(checkout, commit, relative):
    value = _git(checkout, "show", f"{commit}:{relative}", binary=True)
    return value


def _git_diff(checkout, base, candidate):
    return _git(
        checkout,
        "diff",
        "--binary",
        "--full-index",
        "--no-renames",
        f"{base}..{candidate}",
        binary=True,
    )


def _verify_raw_record(root, value, label):
    _exact(value, {"path", "size_bytes", "sha256", "identity"}, label)
    path = _relative(root, value["path"], label)
    identity = _exact(value["identity"], {"device", "inode"}, f"{label} identity")
    metadata = path.stat()
    if (
        identity != {"device": metadata.st_dev, "inode": metadata.st_ino}
        or value["size_bytes"] != metadata.st_size
        or value["sha256"] != _sha256_file(path)
    ):
        raise VerificationError(f"{label} binding mismatch")
    return path


def _verify_process(root, value, label):
    _exact(value, PROCESS_KEYS, label)
    _self_hash(value, "process_sha256", label)
    if (
        not isinstance(value["argv"], list)
        or not value["argv"]
        or any(type(item) is not str or not item for item in value["argv"])
        or value["returncode"] != 0
        or type(value["timeout_seconds"]) not in {int, float}
        or value["timeout_seconds"] <= 0
        or type(value["max_output_bytes"]) is not int
        or value["max_output_bytes"] <= 0
    ):
        raise VerificationError(f"{label} process was not successful")
    _integer(value["wall_ns"], f"{label} wall")
    _integer(value["cpu_ns"], f"{label} CPU")
    stdout = _verify_raw_record(root, value["stdout"], f"{label} stdout")
    stderr = _verify_raw_record(root, value["stderr"], f"{label} stderr")
    if stdout.name != "stdout.raw" or stderr.name != "stderr.raw":
        raise VerificationError(f"{label} raw filenames mismatch")
    return stdout, stderr


def _verify_artifact(root, value, label):
    _exact(
        value,
        {
            "source_path", "source_identity", "size_bytes", "sha256",
            "snapshot_path", "snapshot_identity", "binding_intact",
        },
        label,
    )
    snapshot = _relative(root, value["snapshot_path"], f"{label} snapshot")
    metadata = snapshot.stat()
    if (
        value["binding_intact"] is not True
        or value["size_bytes"] != metadata.st_size
        or value["sha256"] != _sha256_file(snapshot)
        or value["snapshot_identity"]
        != {"device": metadata.st_dev, "inode": metadata.st_ino}
    ):
        raise VerificationError(f"{label} snapshot binding mismatch")
    source = Path(_string(value["source_path"], f"{label} source"))
    if not source.is_absolute():
        raise VerificationError(f"{label} source path must be absolute")
    return snapshot


def _verify_r0(path, checkout, source, *, expected_policy=None):
    root = Path(path).resolve(strict=True).parent
    receipt = _exact(_load_object(path), R0_KEYS, "R0 receipt")
    digest = _self_hash(receipt, "receipt_sha256", "R0 receipt")
    if (
        receipt["schema_version"] != 1
        or receipt["receipt_version"] != 1
        or receipt["rung"] != "r0"
        or receipt["repository_url"] != SOURCE_URL
        or receipt["base_commit"] != SOURCE_COMMIT
        or receipt["base_tree"] != SOURCE_TREE
        or receipt["source_receipt_sha256"] != source["receipt_sha256"]
        or receipt["source_receipt_file_sha256"]
        != _sha256_file(_path(receipt["source_receipt_path"], "R0 source receipt"))
    ):
        raise VerificationError("R0 source binding mismatch")
    policy = _string(receipt["policy"], "R0 policy")
    if expected_policy is not None and policy != expected_policy:
        raise VerificationError("R0 policy order mismatch")
    candidate = _hash(receipt["candidate_commit"], "R0 candidate", 40)
    candidate_tree = _git(checkout, "rev-parse", f"{candidate}^{{tree}}")
    if candidate_tree != receipt["candidate_tree"]:
        raise VerificationError("R0 candidate tree mismatch")
    _git(checkout, "merge-base", "--is-ancestor", SOURCE_COMMIT, candidate)
    diff = _git_diff(checkout, SOURCE_COMMIT, candidate)
    diff_hash = hashlib.sha256(diff).hexdigest()
    if diff_hash != receipt["candidate_diff_sha256"]:
        raise VerificationError("R0 candidate diff hash mismatch")
    changed = _git(
        checkout, "diff", "--name-only", "--no-renames", f"{SOURCE_COMMIT}..{candidate}"
    ).splitlines()
    scope = _exact(
        receipt["scope"],
        {
            "allowed_paths", "baseline_unchanged", "additive_wiring_only",
            "contract_bound", "changed_paths", "diff_sha256",
        },
        "R0 scope",
    )
    if (
        scope["allowed_paths"] is not True
        or scope["baseline_unchanged"] is not True
        or scope["additive_wiring_only"] is not True
        or scope["changed_paths"] != sorted(changed)
        or scope["diff_sha256"] != diff_hash
    ):
        raise VerificationError("R0 scope facts mismatch")
    baseline = candidate == SOURCE_COMMIT
    if scope["contract_bound"] is not (None if baseline else True):
        raise VerificationError("R0 contract scope state mismatch")
    changed_hashes = receipt["changed_path_sha256"]
    if not isinstance(changed_hashes, dict) or set(changed_hashes) != set(changed):
        raise VerificationError("R0 changed-path projection mismatch")
    for relative, expected in changed_hashes.items():
        if hashlib.sha256(_git_blob(checkout, candidate, relative)).hexdigest() != expected:
            raise VerificationError("R0 changed Git blob hash mismatch")
    source_blob = _git_blob(checkout, candidate, f"libCacheSim/cache/eviction/{policy}.c")
    if hashlib.sha256(source_blob).hexdigest() != receipt["policy_source_sha256"]:
        raise VerificationError("R0 policy source hash mismatch")
    if baseline:
        if receipt["contract_sha256"] is not None or receipt["candidate_test_sha256"] is not None:
            raise VerificationError("baseline R0 unexpectedly binds candidate files")
    else:
        contract = _git_blob(checkout, candidate, "commissioning/cache_policy_contract.json")
        test = _git_blob(checkout, candidate, f"test/test_{policy}.c")
        if (
            hashlib.sha256(contract).hexdigest() != receipt["contract_sha256"]
            or hashlib.sha256(test).hexdigest() != receipt["candidate_test_sha256"]
        ):
            raise VerificationError("candidate contract/test binding mismatch")
    checks = _exact(
        receipt["checks"],
        {
            "source_binding", "evidence_binding", "build", "full_tests",
            "candidate_test", "sanitizer", "deterministic", "capacity", "metadata_probe",
        },
        "R0 checks",
    )
    for name in (
        "source_binding", "evidence_binding", "build", "full_tests",
        "sanitizer", "deterministic", "capacity", "metadata_probe",
    ):
        if checks[name] is not True:
            raise VerificationError(f"R0 {name} check is not true")
    if checks["candidate_test"] is not (None if baseline else True):
        raise VerificationError("R0 candidate test state mismatch")
    if receipt["complexity_audit"] != "pending_independent_review":
        raise VerificationError("R0 complexity audit was overclaimed")
    metadata = _exact(
        receipt["measured_metadata"],
        {"bytes_per_object", "global_bytes", "measurement_sha256", "within_budget"},
        "R0 metadata",
    )
    _normalized_decimal(metadata["bytes_per_object"], "R0 object metadata")
    _integer(metadata["global_bytes"], "R0 global metadata")
    _hash(metadata["measurement_sha256"], "R0 metadata measurement")
    if metadata["within_budget"] is not None:
        raise VerificationError("R0 metadata budget was claimed before calibration")
    commands = receipt["commands"]
    if not isinstance(commands, list) or not commands:
        raise VerificationError("R0 command receipts are missing")
    metadata_commands = []
    for command in commands:
        if not isinstance(command, dict) or command.get("command_sha256") != _record_sha256(command, "command_sha256"):
            raise VerificationError("R0 command receipt hash mismatch")
        for field in ("stdout", "stderr"):
            raw = command.get(field)
            if not isinstance(raw, dict) or set(raw) != {
                "path", "size_bytes", "sha256", "binding_intact"
            }:
                raise VerificationError("R0 command raw receipt mismatch")
            if raw.get("size_bytes") is not None:
                if raw["binding_intact"] is not True:
                    raise VerificationError("R0 command raw binding is not intact")
                raw_path = _relative(root, raw["path"], "R0 command raw path")
                if raw_path.stat().st_size != raw["size_bytes"] or _sha256_file(raw_path) != raw["sha256"]:
                    raise VerificationError("R0 command raw bytes mismatch")
            elif raw["sha256"] is not None or raw["binding_intact"] is not False:
                raise VerificationError("R0 absent command raw facts are inconsistent")
        if command.get("label") == "metadata-run":
            metadata_commands.append(command)
    if len(metadata_commands) != 1:
        raise VerificationError("R0 must contain one metadata-run command")
    if metadata_commands[0]["stdout"]["sha256"] != metadata["measurement_sha256"]:
        raise VerificationError("R0 metadata command/output binding mismatch")
    artifacts = receipt["artifact_snapshots"]
    if not isinstance(artifacts, dict):
        raise VerificationError("R0 artifact snapshots are missing")
    bound_artifacts = {
        name: _verify_artifact(root, artifacts[name], f"R0 artifact {name}")
        for name in ("release_cachesim", "release_archive", "release_cmake_cache")
    }
    binary_hash = _sha256_file(bound_artifacts["release_cachesim"])
    if receipt["binary_sha256"] != binary_hash or receipt["binary_post_run_sha256"] != binary_hash:
        raise VerificationError("R0 binary snapshot binding mismatch")
    probes = _exact(
        receipt["probes"],
        {"fixed_time", "release_cmake_cache_sha256", "include_flags", "link_flags", "capacity", "metadata"},
        "R0 probes",
    )
    for name in ("fixed_time", "capacity", "metadata"):
        probe = probes[name]
        required = {"source_path", "source_sha256", "binary"}
        if name == "fixed_time":
            required.add("environment")
        if name == "metadata":
            required.update(
                {"interposer_source_path", "interposer_source_sha256", "interposer_binary", "accounting_scope"}
            )
        _exact(probe, required, f"R0 {name} probe")
        source_path = _relative(root, probe["source_path"], f"R0 {name} source")
        if _sha256_file(source_path) != probe["source_sha256"]:
            raise VerificationError("R0 probe source hash mismatch")
        binary = probe["binary"]
        if not isinstance(binary, dict) or set(binary) != {
            "path", "size_bytes", "sha256", "binding_intact"
        }:
            raise VerificationError("R0 probe binary receipt mismatch")
        if binary["binding_intact"] is not True:
            raise VerificationError("R0 probe binary binding is not intact")
        binary_path = _relative(root, binary["path"], f"R0 {name} binary")
        if binary_path.stat().st_size != binary["size_bytes"] or _sha256_file(binary_path) != binary["sha256"]:
            raise VerificationError("R0 probe binary hash mismatch")
    interposer = probes["metadata"]
    interposer_source = _relative(root, interposer["interposer_source_path"], "R0 interposer source")
    if _sha256_file(interposer_source) != interposer["interposer_source_sha256"]:
        raise VerificationError("R0 interposer source hash mismatch")
    interposer_binary = interposer["interposer_binary"]
    interposer_path = _relative(root, interposer_binary["path"], "R0 interposer binary")
    if _sha256_file(interposer_path) != interposer_binary["sha256"]:
        raise VerificationError("R0 interposer binary hash mismatch")
    evaluator = receipt["evaluator"]
    if not isinstance(evaluator, dict) or set(evaluator) != R0_EVALUATOR_KEYS:
        raise VerificationError("R0 evaluator dependency map mismatch")
    for value in evaluator.values():
        _hash(value, "R0 evaluator hash")
    if receipt["errors"] != [] or receipt["unexpected_stage_entries"] != []:
        raise VerificationError("R0 retains an operational failure")
    _verify_r0_inventory(root, receipt["evidence_inventory"])
    return {
        "path": Path(path).resolve(strict=True),
        "root": root,
        "record": receipt,
        "receipt_sha256": digest,
        "file_sha256": _sha256_file(Path(path)),
        "artifacts": artifacts,
    }


def _parse_result(raw):
    try:
        text = raw.decode("ascii")
    except UnicodeError as error:
        raise VerificationError("simulator stdout is not ASCII") from error
    matches = []
    for line in text.splitlines():
        if not line:
            continue
        marker = " cache size "
        if marker not in line:
            raise VerificationError("simulator stdout contains an unknown line")
        prefix, remainder = line.split(marker, 1)
        if not prefix.startswith("/"):
            raise VerificationError("simulator result trace path is not absolute")
        fields = remainder.split(",")
        if len(fields) != 5:
            raise VerificationError("simulator result field count mismatch")
        requests = fields[1].strip().split(" ")
        object_ratio = fields[2].strip().removeprefix("miss ratio ")
        byte_ratio = fields[3].strip().removeprefix("byte miss ratio ")
        throughput = fields[4].strip().removeprefix("throughput ").removesuffix(" MQPS")
        if len(requests) != 2 or requests[1] != "req" or not requests[0].isdigit():
            raise VerificationError("simulator request count is malformed")
        parsed_object = _normalized_decimal(object_ratio, "object miss ratio")
        parsed_byte = _normalized_decimal(byte_ratio, "byte miss ratio")
        parsed_throughput = _normalized_decimal(throughput, "throughput")
        if (
            int(requests[0]) <= 0
            or _decimal_compare(parsed_object, "0") < 0
            or _decimal_compare(parsed_object, "1") > 0
            or _decimal_compare(parsed_byte, "0") < 0
            or _decimal_compare(parsed_byte, "1") > 0
            or _decimal_compare(parsed_throughput, "0") <= 0
        ):
            raise VerificationError("simulator result values are out of range")
        matches.append(
            {
                "request_count": int(requests[0]),
                "object_miss_ratio": parsed_object,
                "byte_miss_ratio": parsed_byte,
                "simulator_throughput_mqps": parsed_throughput,
            }
        )
    if len(matches) != 1:
        raise VerificationError("simulator stdout must contain exactly one result")
    return matches[0]


def _verify_phase(root, value, measurement, trace):
    _exact(value, PHASE_KEYS, "phase diagnostic")
    _self_hash(value, "phase_sha256", "phase diagnostic")
    bins = value["bins"]
    if not isinstance(bins, list) or len(bins) != 16:
        raise VerificationError("phase diagnostic must contain sixteen bins")
    totals = {"request_count": 0, "object_misses": 0, "request_bytes": 0, "byte_misses": 0}
    for index, item in enumerate(bins):
        _exact(item, {"index", "requests", "object_misses", "request_bytes", "byte_misses"}, "phase bin")
        if item["index"] != index:
            raise VerificationError("phase bins are out of order")
        requests = _integer(item["requests"], "phase requests", 1)
        misses = _integer(item["object_misses"], "phase object misses")
        request_bytes = _integer(item["request_bytes"], "phase request bytes", 1)
        byte_misses = _integer(item["byte_misses"], "phase byte misses")
        if misses > requests or byte_misses > request_bytes:
            raise VerificationError("phase miss counts exceed requests")
        totals["request_count"] += requests
        totals["object_misses"] += misses
        totals["request_bytes"] += request_bytes
        totals["byte_misses"] += byte_misses
    if any(value[key] != total for key, total in totals.items()):
        raise VerificationError("phase totals differ from bins")
    if (
        value["trace_id"] != measurement["trace_id"]
        or value["trace_sha256"] != trace["sha256"]
        or value["frozen_trace_diagnostic_sha256"] != trace["diagnostic_sha256"]
        or value["policy"] != measurement["policy"]
        or value["cache_fraction"] != measurement["cache_fraction"]
        or value["cache_size_bytes"] != measurement["cache_size_bytes"]
        or value["request_count"] != measurement["request_count"]
    ):
        raise VerificationError("phase diagnostic cell binding mismatch")
    _verify_process(root, value["process"], "phase process")
    return {
        "phase_sha256": value["phase_sha256"],
        **totals,
        "bins": bins,
    }


def _verify_measurement(root, summary, receipt, r0, trace):
    _exact(
        summary,
        {"index", "trace_id", "split", "cache_fraction", "cache_size_bytes", "path", "measurement_sha256"},
        "measurement summary",
    )
    index = _integer(summary["index"], "measurement index")
    if summary["path"] != f"measurements/{index:04d}/measurement.json":
        raise VerificationError("measurement summary path mismatch")
    path = _relative(root, summary["path"], "measurement")
    measurement = _load_object(path)
    expected_keys = set(MEASUREMENT_BASE_KEYS)
    if receipt["rung"] in {"r2", "r3"}:
        expected_keys.update({"phase_diagnostic", "frozen_trace_diagnostic"})
    _exact(measurement, expected_keys, "measurement")
    digest = _self_hash(measurement, "measurement_sha256", "measurement")
    if digest != summary["measurement_sha256"]:
        raise VerificationError("measurement summary hash mismatch")
    expected_summary = {
        "index": index,
        "trace_id": measurement["trace_id"],
        "split": measurement["split"],
        "cache_fraction": measurement["cache_fraction"],
        "cache_size_bytes": measurement["cache_size_bytes"],
        "path": summary["path"],
        "measurement_sha256": digest,
    }
    if summary != expected_summary:
        raise VerificationError("measurement summary projection mismatch")
    metadata = r0["record"]["measured_metadata"]
    if (
        measurement["schema_version"] != 1
        or measurement["receipt_version"] != 1
        or measurement["rung"] != receipt["rung"]
        or measurement["trace_id"] != trace["trace_id"]
        or measurement["split"] != trace["split"]
        or measurement["policy"] != receipt["policy"]
        or measurement["trace_sha256"] != trace["sha256"]
        or measurement["trace_diagnostic_sha256"] != trace["diagnostic_sha256"]
        or measurement["source_receipt_sha256"] != receipt["source_receipt_sha256"]
        or measurement["r0_receipt_sha256"] != r0["receipt_sha256"]
        or measurement["candidate_commit"] != receipt["candidate_commit"]
        or measurement["candidate_tree"] != receipt["candidate_tree"]
        or measurement["binary_sha256"] != receipt["binary_snapshot_sha256"]
        or measurement["metadata_bytes_per_object"] != metadata["bytes_per_object"]
        or measurement["global_metadata_bytes"] != metadata["global_bytes"]
        or measurement["metadata_measurement_sha256"] != metadata["measurement_sha256"]
    ):
        raise VerificationError("measurement provenance binding mismatch")
    for field in (
        "object_miss_ratio", "byte_miss_ratio", "simulator_throughput_mqps",
        "cpu_ns_per_request", "metadata_bytes_per_object",
    ):
        if _normalized_decimal(measurement[field], field) != measurement[field]:
            raise VerificationError(f"measurement {field} is not canonical")
    process = measurement["process"]
    stdout, _stderr = _verify_process(root, process, "simulator process")
    process_record = _load_object(path.parent / "process.json")
    if process_record != process or measurement["argv"] != process["argv"]:
        raise VerificationError("measurement process record differs")
    parsed = _parse_result(stdout.read_bytes())
    for field in ("request_count", "object_miss_ratio", "byte_miss_ratio", "simulator_throughput_mqps"):
        observed = measurement[field]
        expected = parsed[field]
        if field != "request_count":
            observed = _normalized_decimal(observed, field)
        if observed != expected:
            raise VerificationError(f"raw simulator {field} differs from measurement")
    simulator = _exact(
        measurement["simulator_output"],
        {"requested_path", "path", "identity", "size_bytes", "sha256"},
        "simulator output",
    )
    simulator_path = _relative(root, simulator["path"], "simulator side effect")
    if (
        simulator_path.stat().st_size != simulator["size_bytes"]
        or _sha256_file(simulator_path) != simulator["sha256"]
        or simulator["sha256"] != process["stdout"]["sha256"]
    ):
        raise VerificationError("simulator side-effect binding mismatch")
    request = _load_object(path.parent / "request.json")
    _self_hash(request, "request_sha256", "measurement request")
    for field in ("rung", "trace_id", "policy", "cache_fraction", "cache_size_bytes", "argv"):
        expected = measurement["argv"] if field == "argv" else measurement[field]
        if request.get(field) != expected:
            raise VerificationError("measurement request binding mismatch")
    phase_projection = None
    if receipt["rung"] in {"r2", "r3"}:
        if measurement["frozen_trace_diagnostic"] != trace["diagnostics"]:
            raise VerificationError("measurement frozen diagnostic differs")
        phase_projection = _verify_phase(root, measurement["phase_diagnostic"], measurement, trace)
        if _load_object(path.parent / "phase.json") != measurement["phase_diagnostic"]:
            raise VerificationError("phase record differs from measurement")
    measurement["_phase_projection"] = phase_projection
    measurement["_receipt_sha256"] = receipt["receipt_sha256"]
    return measurement


def _scientific_projection(receipt, root=None):
    scientific = receipt["scientific_inputs"]
    _exact(scientific, {"fixed_time_interposer", "release_archive", "release_cmake_cache", "headers"}, "scientific inputs")
    projection = {}
    for name in ("fixed_time_interposer", "release_archive", "release_cmake_cache"):
        item = _exact(scientific[name], {"path", "sha256"}, f"scientific input {name}")
        projection[name] = _hash(item["sha256"], f"scientific input {name}")
        if root is not None and _sha256_file(
            _relative(root, item["path"], f"scientific input {name}")
        ) != projection[name]:
            raise VerificationError("scientific input snapshot hash mismatch")
    if not isinstance(scientific["headers"], dict):
        raise VerificationError("scientific headers must be an object")
    for name, item in scientific["headers"].items():
        _exact(item, {"path", "sha256"}, "scientific header")
        projection[f"header:{name}"] = _hash(item["sha256"], "scientific header")
        if root is not None and _sha256_file(
            _relative(root, item["path"], "scientific header")
        ) != projection[f"header:{name}"]:
            raise VerificationError("scientific header snapshot hash mismatch")
    return dict(sorted(projection.items()))


def _verify_portfolio(path, expected_rung, task_root, manifest, source, r0_by_hash, checkout):
    root = Path(path).resolve(strict=True).parent
    receipt = _exact(_load_object(path), PORTFOLIO_KEYS, f"{expected_rung} receipt")
    digest = _self_hash(receipt, "receipt_sha256", f"{expected_rung} receipt")
    _forbid_outcomes(receipt, f"{expected_rung} receipt")
    if (
        receipt["schema_version"] != 1
        or receipt["receipt_version"] != 1
        or receipt["rung"] != expected_rung
        or receipt["task_root"] != str(task_root)
        or receipt["task_manifest_sha256"] != manifest["manifest_sha256"]
        or receipt["source_receipt_sha256"] != source["receipt_sha256"]
        or receipt["source_receipt_file_sha256"]
        != r0_by_hash.get(receipt["r0_receipt_sha256"], {}).get("record", {}).get(
            "source_receipt_file_sha256"
        )
    ):
        raise VerificationError(f"{expected_rung} root provenance mismatch")
    manifest_path = _path(receipt["task_manifest_path"], "portfolio manifest")
    if (
        _sha256_file(manifest_path) != receipt["task_manifest_file_sha256"]
        or _load_object(manifest_path) != manifest
    ):
        raise VerificationError("portfolio manifest file hash mismatch")
    r0 = r0_by_hash.get(receipt["r0_receipt_sha256"])
    if r0 is None or r0["file_sha256"] != receipt["r0_receipt_file_sha256"]:
        raise VerificationError("portfolio R0 binding mismatch")
    if (
        receipt["policy"] != r0["record"]["policy"]
        or receipt["candidate_commit"] != r0["record"]["candidate_commit"]
        or receipt["candidate_tree"] != r0["record"]["candidate_tree"]
        or receipt["policy_source_sha256"] != r0["record"]["policy_source_sha256"]
        or receipt["binary_snapshot_sha256"] != r0["record"]["binary_sha256"]
        or receipt["r0_artifact_snapshots"] != r0["record"]["artifact_snapshots"]
    ):
        raise VerificationError("portfolio candidate/R0 projection mismatch")
    if _git(checkout, "rev-parse", f"{receipt['candidate_commit']}^{{tree}}") != receipt["candidate_tree"]:
        raise VerificationError("portfolio candidate tree mismatch")
    traces = list(manifest["traces"])
    selected_traces = (
        [item for item in traces if item["split"] == "dev"][:3]
        if expected_rung == "r1"
        else traces
    )
    expected_cells = []
    for trace in selected_traces:
        for fraction in FRACTIONS:
            numerator, denominator = {
                "0.01": (1, 100),
                "0.05": (5, 100),
                "0.1": (1, 10),
            }[fraction]
            expected_cells.append(
                {
                    "index": len(expected_cells),
                    "trace_id": trace["trace_id"],
                    "split": trace["split"],
                    "cache_fraction": fraction,
                    "cache_size_bytes": trace["working_set_bytes"] * numerator // denominator,
                }
            )
    if receipt["selected_cells"] != expected_cells:
        raise VerificationError(f"{expected_rung} selected cell inventory mismatch")
    if receipt["failures"] != [] or receipt["failure_hashes"] != []:
        raise VerificationError(f"{expected_rung} retains failed measurements")
    if receipt["provenance"] != {"final_binding_intact": True}:
        raise VerificationError(f"{expected_rung} final binding is not intact")
    summaries = receipt["measurements"]
    if not isinstance(summaries, list) or len(summaries) != len(expected_cells):
        raise VerificationError(f"{expected_rung} measurement inventory incomplete")
    if receipt["measurement_hashes"] != [item["measurement_sha256"] for item in summaries]:
        raise VerificationError(f"{expected_rung} measurement hash order mismatch")
    by_trace = {item["trace_id"]: item for item in selected_traces}
    measurements = [
        _verify_measurement(root, summary, receipt, r0, by_trace[summary["trace_id"]])
        for summary in summaries
    ]
    if expected_rung == "r1":
        if receipt["phase_probe"] is not None or receipt["frozen_trace_diagnostics"] != []:
            raise VerificationError("R1 unexpectedly contains phase apparatus")
    else:
        phase_probe = receipt["phase_probe"]
        if not isinstance(phase_probe, dict):
            raise VerificationError("R2/R3 phase probe facts are missing")
        _exact(
            phase_probe,
            {
                "source_path", "source_sha256", "binary_path", "binary_sha256",
                "compile_process_sha256", "release_archive_sha256",
                "release_cmake_cache_sha256", "compiler_path",
                "compiler_resolved_path", "compiler_sha256", "include_flags",
                "link_flags",
            },
            "portfolio phase probe",
        )
        phase_source = _relative(root, phase_probe["source_path"], "phase probe source")
        phase_binary = _relative(root, phase_probe["binary_path"], "phase probe binary")
        if (
            _sha256_file(phase_source) != phase_probe["source_sha256"]
            or _sha256_file(phase_binary) != phase_probe["binary_sha256"]
            or phase_probe["release_archive_sha256"]
            != r0["record"]["artifact_snapshots"]["release_archive"]["sha256"]
            or phase_probe["release_cmake_cache_sha256"]
            != r0["record"]["artifact_snapshots"]["release_cmake_cache"]["sha256"]
        ):
            raise VerificationError("portfolio phase apparatus binding mismatch")
        expected_diagnostics = [
            {
                "trace_id": trace["trace_id"],
                "diagnostic_sha256": trace["diagnostic_sha256"],
                "diagnostics": trace["diagnostics"],
            }
            for trace in selected_traces
        ]
        if receipt["frozen_trace_diagnostics"] != expected_diagnostics:
            raise VerificationError("portfolio frozen diagnostic inventory mismatch")
    evaluator = receipt["evaluator"]
    if not isinstance(evaluator, dict) or set(evaluator) != PORTFOLIO_EVALUATOR_KEYS:
        raise VerificationError("portfolio evaluator map is incomplete")
    for value in evaluator.values():
        _hash(value, "portfolio evaluator")
    scientific = _scientific_projection(receipt, root)
    for measurement in measurements:
        if measurement["evaluator"] != evaluator or measurement["scientific_inputs"] != scientific:
            raise VerificationError("measurement apparatus projection mismatch")
    snapshots = receipt["evaluator_snapshots"]
    if not isinstance(snapshots, dict) or set(snapshots) != PORTFOLIO_EVALUATOR_KEYS:
        raise VerificationError("portfolio evaluator snapshot map mismatch")
    for name, item in snapshots.items():
        _exact(item, {"path", "identity", "size_bytes", "sha256"}, "evaluator snapshot")
        path_value = _relative(root, item["path"], "evaluator snapshot path")
        metadata = path_value.stat()
        if (
            item["identity"] != {"device": metadata.st_dev, "inode": metadata.st_ino}
            or item["size_bytes"] != metadata.st_size
            or item["sha256"] != evaluator[name]
            or _sha256_file(path_value) != item["sha256"]
        ):
            raise VerificationError("portfolio evaluator snapshot binding mismatch")
    trace_snapshots = receipt["trace_snapshots"]
    if not isinstance(trace_snapshots, list) or len(trace_snapshots) != len(selected_traces):
        raise VerificationError("portfolio trace snapshot inventory mismatch")
    for trace, item in zip(selected_traces, trace_snapshots):
        _exact(
            item,
            {
                "trace_id", "source_path", "source_identity", "source_size_bytes",
                "source_sha256", "snapshot_path", "snapshot_identity",
                "snapshot_size_bytes", "snapshot_sha256", "audit_path",
                "audit_identity", "audit_sha256",
            },
            "trace snapshot",
        )
        source_path = _path(item["source_path"], "trace snapshot source")
        snapshot_path = _relative(root, item["snapshot_path"], "trace snapshot")
        audit_path = _relative(root, item["audit_path"], "trace audit")
        source_metadata = source_path.stat()
        snapshot_metadata = snapshot_path.stat()
        audit_metadata = audit_path.stat()
        if (
            item["trace_id"] != trace["trace_id"]
            or item["source_identity"]
            != {"device": source_metadata.st_dev, "inode": source_metadata.st_ino}
            or item["source_size_bytes"] != trace["size_bytes"]
            or item["source_sha256"] != trace["sha256"]
            or _sha256_file(source_path) != trace["sha256"]
            or item["snapshot_identity"]
            != {"device": snapshot_metadata.st_dev, "inode": snapshot_metadata.st_ino}
            or item["snapshot_size_bytes"] != trace["size_bytes"]
            or item["snapshot_sha256"] != trace["sha256"]
            or _sha256_file(snapshot_path) != trace["sha256"]
            or item["audit_identity"]
            != {"device": audit_metadata.st_dev, "inode": audit_metadata.st_ino}
            or item["audit_sha256"] != trace["diagnostic_sha256"]
            or _load_object(audit_path) != trace["diagnostics"]
        ):
            raise VerificationError("portfolio trace snapshot binding mismatch")
    execution = receipt["execution_copy"]
    if not isinstance(execution, dict):
        raise VerificationError("portfolio execution copy is missing")
    execution_path = _relative(root, execution["path"], "portfolio execution copy")
    if _sha256_file(execution_path) != execution["sha256"]:
        raise VerificationError("portfolio execution-copy hash mismatch")
    _verify_inventory(root, receipt["evidence_inventory"])
    return {
        "path": Path(path).resolve(strict=True),
        "record": receipt,
        "receipt_sha256": digest,
        "file_sha256": _sha256_file(Path(path)),
        "measurements": measurements,
        "scientific": scientific,
    }


def _phase_projection(measurement):
    return measurement["_phase_projection"]


def _verify_calibration(path, expected_digest, task, source, r0s, r2s):
    calibration_path = Path(path).resolve(strict=True)
    record = _exact(_load_object(calibration_path), CALIBRATION_KEYS, "calibration")
    digest = _self_hash(record, "calibration_sha256", "calibration")
    if digest != expected_digest:
        raise VerificationError("calibration differs from external expected digest")
    if calibration_path.stat().st_mode & 0o777 != 0o400:
        raise VerificationError("calibration must be read-only mode 0400")
    _forbid_outcomes(record, "calibration")
    r0_by_policy = {item["record"]["policy"]: item for item in r0s}
    if set(r0_by_policy) != set(POLICIES) or set(record["r0_receipt_sha256s"]) != set(POLICIES):
        raise VerificationError("calibration R0 policy map mismatch")
    for policy in POLICIES:
        if record["r0_receipt_sha256s"][policy] != r0_by_policy[policy]["receipt_sha256"]:
            raise VerificationError("calibration R0 receipt projection mismatch")
    if (
        record["schema_version"] != 1
        or record["task_manifest_sha256"] != task["manifest_sha256"]
        or record["source_receipt_sha256"] != source["receipt_sha256"]
        or record["source_commit"] != SOURCE_COMMIT
        or record["repetitions"] != 5
        or record["cache_fractions"] != list(FRACTIONS)
        or set(record["references"]) != set(REFERENCES)
        or set(record["transfer_constraints"]) != set(REFERENCES)
        or set(record["comparisons"]) != set(POLICIES)
    ):
        raise VerificationError("calibration root projection mismatch")
    if record["input_receipt_sha256s"] != sorted(item["receipt_sha256"] for item in r2s):
        raise VerificationError("calibration input receipt projection mismatch")
    first = r2s[0]["record"]
    if (
        record["binary_sha256"] != first["binary_snapshot_sha256"]
        or record["evaluator_sha256s"] != first["evaluator"]
        or record["scientific_input_sha256s"] != r2s[0]["scientific"]
        or record["host_fingerprint"] != first["host"]
    ):
        raise VerificationError("calibration apparatus projection mismatch")
    grouped = {}
    for receipt in r2s:
        policy = receipt["record"]["policy"]
        for measurement in receipt["measurements"]:
            key = (policy, measurement["trace_id"], measurement["cache_fraction"])
            grouped.setdefault(key, []).append(measurement)
    for policy in POLICIES:
        expected_repetitions = 5 if policy in REFERENCES else 1
        policy_receipts = [item for item in r2s if item["record"]["policy"] == policy]
        if len(policy_receipts) != expected_repetitions:
            raise VerificationError("calibration policy receipt count mismatch")
        for trace in task["traces"]:
            trace_id = trace["trace_id"]
            for fraction in FRACTIONS:
                values = grouped.get((policy, trace_id, fraction), [])
                if len(values) != expected_repetitions:
                    raise VerificationError("calibration cell repetitions are incomplete")
                comparison = record["comparisons"][policy][trace_id][fraction]
                expected_comparison = {
                    "repetitions": expected_repetitions,
                    "input_receipt_sha256s": sorted(item["_receipt_sha256"] for item in values),
                    "measurement_sha256s": sorted(item["measurement_sha256"] for item in values),
                    "object_miss_ratio_values": _decimal_sort(item["object_miss_ratio"] for item in values),
                    "byte_miss_ratio_values": _decimal_sort(item["byte_miss_ratio"] for item in values),
                    "phase_values": sorted(
                        (_phase_projection(item) for item in values),
                        key=lambda item: item["phase_sha256"],
                    ),
                }
                if comparison != expected_comparison:
                    raise VerificationError("calibration comparison cell mismatch")
                if policy in REFERENCES:
                    throughputs = _decimal_sort(item["simulator_throughput_mqps"] for item in values)
                    cpu_values = _decimal_sort(item["cpu_ns_per_request"] for item in values)
                    median = throughputs[2]
                    reference = record["references"][policy][trace_id][fraction]
                    expected_reference = {
                        "repetitions": 5,
                        "input_receipt_sha256s": expected_comparison["input_receipt_sha256s"],
                        "measurement_sha256s": expected_comparison["measurement_sha256s"],
                        "object_miss_ratio_values": expected_comparison["object_miss_ratio_values"],
                        "byte_miss_ratio_values": expected_comparison["byte_miss_ratio_values"],
                        "simulator_throughput_mqps_values": throughputs,
                        "cpu_ns_per_request_values": cpu_values,
                        "throughput_median_mqps": median,
                        "throughput_floor_mqps": _times_nine_tenths(median),
                    }
                    if reference != expected_reference:
                        raise VerificationError("calibration reference median/floor mismatch")
    for policy in REFERENCES:
        r0 = r0_by_policy[policy]["record"]
        metadata = r0["measured_metadata"]
        command = next(item for item in r0["commands"] if item["label"] == "metadata-run")
        probe = r0["probes"]["metadata"]
        expected_metadata = {
            "bytes_per_object": metadata["bytes_per_object"],
            "global_bytes": metadata["global_bytes"],
            "measurement_sha256": metadata["measurement_sha256"],
            "probe_evidence": {
                "r0_receipt_sha256": r0["receipt_sha256"],
                "metadata_command_sha256": command["command_sha256"],
                "stdout_sha256": command["stdout"]["sha256"],
                "metadata_measurement_sha256": metadata["measurement_sha256"],
                "metadata_probe_source_sha256": probe["source_sha256"],
                "metadata_probe_binary_sha256": probe["binary"]["sha256"],
                "metadata_interposer_source_sha256": probe["interposer_source_sha256"],
                "metadata_interposer_binary_sha256": probe["interposer_binary"]["sha256"],
            },
            "independent_audit": "pending_independent_review",
        }
        if record["references"][policy]["metadata"] != expected_metadata:
            raise VerificationError("calibration metadata/probe projection mismatch")
        transfer = record["transfer_constraints"][policy]
        if transfer.get("metadata") != expected_metadata:
            raise VerificationError("transfer metadata projection mismatch")
        for fraction in FRACTIONS:
            source_cells = []
            medians = []
            for trace in sorted(task["traces"], key=lambda item: item["trace_id"]):
                cell = record["references"][policy][trace["trace_id"]][fraction]
                medians.append(cell["throughput_median_mqps"])
                source_cells.append(
                    {
                        "trace_id": trace["trace_id"],
                        "reference_cell_sha256": hashlib.sha256(canonical_bytes(cell)).hexdigest(),
                        "input_receipt_sha256s": cell["input_receipt_sha256s"],
                        "measurement_sha256s": cell["measurement_sha256s"],
                        "throughput_median_mqps": cell["throughput_median_mqps"],
                    }
                )
            minimum = medians[0]
            for candidate in medians[1:]:
                if _decimal_compare(candidate, minimum) < 0:
                    minimum = candidate
            expected_transfer = {
                "derivation": "0.90 * minimum(source throughput_median_mqps)",
                "source_cells": source_cells,
                "minimum_throughput_median_mqps": minimum,
                "throughput_floor_mqps": _times_nine_tenths(minimum),
            }
            if transfer.get(fraction) != expected_transfer:
                raise VerificationError("transfer constraint projection mismatch")
    return record, {
        "state": "verified",
        "calibration_sha256": digest,
        "r0_policy_count": len(POLICIES),
        "r2_receipt_count": len(r2s),
        "repetitions_per_reference_cell": 5,
    }


def _host_authority_root():
    uid = os.getuid()
    try:
        for line in Path("/etc/passwd").read_text(encoding="utf-8").splitlines():
            fields = line.split(":")
            if len(fields) >= 6 and fields[2].isdigit() and int(fields[2]) == uid:
                home = Path(fields[5])
                if not home.is_absolute():
                    break
                return home / ".local/state/aros/cache-campaign-r3"
    except OSError as error:
        raise VerificationError("passwd authority lookup failed") from error
    raise VerificationError("passwd authority home is unavailable")


def _authority_id(package):
    raw = (
        package["frozen_commit"]
        + package["r3_commitment_sha256"]
        + package["candidate_commit"]
        + "/"
        + package["policy"]
    ).encode()
    return hashlib.sha256(raw).hexdigest()


def _project_blobs(project, commit):
    blobs = []
    raw_tree = _git(project, "ls-tree", "-r", "-z", "--full-tree", commit, binary=True)
    for record in raw_tree.split(b"\0"):
        if not record:
            continue
        metadata, _path_value = record.split(b"\t", 1)
        mode, kind, oid = metadata.decode("ascii").split(" ")
        if kind == "blob" and mode in {"100644", "100755"}:
            blobs.append(_git(project, "cat-file", "blob", oid, binary=True))
    return blobs


def _verify_r3(
    index,
    task_root,
    checkout,
    task,
    host,
    public,
    private,
    source,
    calibration,
    r0_by_hash,
):
    r3_index = index["r3"]
    if r3_index is None:
        return None
    _exact(r3_index, R3_INDEX_KEYS, "R3 index")
    package_path = _path(r3_index["frozen_package"], "frozen package")
    package = _exact(_load_object(package_path), PACKAGE_KEYS, "frozen package")
    if package["schema_version"] != 1 or package["project"] != str(task_root):
        raise VerificationError("frozen package task root mismatch")
    frozen = _hash(package["frozen_commit"], "frozen commit", 40)
    candidate = _hash(package["candidate_commit"], "candidate commit", 40)
    if _git(task_root, "rev-parse", "HEAD") != frozen:
        raise VerificationError("task project HEAD differs from frozen commit")
    if _git(task_root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise VerificationError("task project is dirty after freeze")
    frozen_tree = _git(task_root, "rev-parse", f"{frozen}^{{tree}}")
    raw_refs = {}
    ref_hashes = {}
    for field in REF_FIELDS:
        relative = PurePosixPath(_string(package[field], field))
        if relative.is_absolute() or ".." in relative.parts or relative.as_posix() != package[field]:
            raise VerificationError("frozen package reference is unsafe")
        listing = _git(task_root, "ls-tree", "-z", frozen, "--", package[field], binary=True)
        records = [item for item in listing.split(b"\0") if item]
        if len(records) != 1:
            raise VerificationError(f"frozen Git ref is missing: {field}")
        metadata, raw_path = records[0].split(b"\t", 1)
        mode, kind, oid = metadata.decode("ascii").split(" ")
        if mode not in {"100644", "100755"} or kind != "blob" or raw_path.decode() != package[field]:
            raise VerificationError("frozen package ref is not a regular Git blob")
        raw = _git(task_root, "cat-file", "blob", oid, binary=True)
        raw_refs[field] = raw
        ref_hashes[field] = hashlib.sha256(raw).hexdigest()
    _verify_no_task_leak(task_root, public, private, _project_blobs(task_root, frozen))
    try:
        descriptor = json.loads(
            raw_refs["reproduction_ref"],
            object_pairs_hook=_unique_object,
            parse_constant=_invalid_constant,
        )
    except (UnicodeError, json.JSONDecodeError, VerificationError) as error:
        raise VerificationError("reproduction descriptor is invalid JSON") from error
    _exact(
        descriptor,
        {"schema_version", "r2_receipt_path", "r2_receipt_sha256"},
        "reproduction descriptor",
    )
    candidate_r0_path = _path(r3_index["candidate_r0_receipt"], "candidate R0 receipt")
    candidate_r0 = _verify_r0(
        candidate_r0_path,
        checkout,
        source,
        expected_policy=package["policy"],
    )
    r0_by_hash[candidate_r0["receipt_sha256"]] = candidate_r0
    candidate_r2_path = _path(r3_index["candidate_r2_receipt"], "candidate R2 receipt")
    if (
        descriptor["schema_version"] != 1
        or descriptor["r2_receipt_path"] != str(candidate_r2_path)
        or descriptor["r2_receipt_sha256"] != package["r2_receipt_sha256"]
    ):
        raise VerificationError("candidate R2 reproduction descriptor mismatch")
    if (
        package["candidate_commit"] != candidate_r0["record"]["candidate_commit"]
        or package["r0_receipt_sha256"] != candidate_r0["receipt_sha256"]
        or package["policy_contract_sha256"] != candidate_r0["record"]["contract_sha256"]
        or package["candidate_diff_sha256"] != candidate_r0["record"]["candidate_diff_sha256"]
        or package["calibration_sha256"] != calibration["calibration_sha256"]
        or package["r3_commitment_sha256"] != host["manifest_sha256"]
    ):
        raise VerificationError("frozen package evidence binding mismatch")
    candidate_r2 = _verify_portfolio(
        candidate_r2_path,
        "r2",
        task_root,
        task,
        source,
        r0_by_hash,
        checkout,
    )
    candidate_manifest_path = Path(candidate_r2["record"]["task_manifest_path"])
    try:
        candidate_manifest_ref = candidate_manifest_path.relative_to(task_root).as_posix()
    except ValueError as error:
        raise VerificationError("candidate R2 task manifest is outside frozen project") from error
    if (
        _git(task_root, "show", f"{frozen}:{candidate_manifest_ref}", binary=True)
        != candidate_manifest_path.read_bytes()
    ):
        raise VerificationError("candidate R2 task manifest differs from frozen Git blob")
    if candidate_r2["receipt_sha256"] != package["r2_receipt_sha256"]:
        raise VerificationError("candidate R2 receipt binding mismatch")
    diff = hashlib.sha256(_git_diff(checkout, SOURCE_COMMIT, candidate)).hexdigest()
    if diff != package["candidate_diff_sha256"]:
        raise VerificationError("frozen candidate diff mismatch")
    authority = _authority_id(package)
    authority_root = _host_authority_root().absolute()
    authority_metadata = authority_root.lstat()
    if (
        authority_root.is_symlink()
        or not authority_root.is_dir()
        or authority_metadata.st_uid != os.getuid()
        or authority_metadata.st_mode & 0o777 != 0o700
    ):
        raise VerificationError("R3 authority root is not owned mode 0700")
    ledger_path = _path(r3_index["ledger"], "R3 ledger")
    final_path = _path(r3_index["receipt"], "R3 final receipt")
    if (
        ledger_path != authority_root / f"r3-{authority}.consumed.json"
        or final_path != authority_root / f"r3-{authority}.receipt.json"
    ):
        raise VerificationError("R3 ledger/final path is not canonical per UID")
    consumed = list(authority_root.glob(f"r3-{authority}.consumed.json"))
    if consumed != [ledger_path]:
        raise VerificationError("R3 authority does not show exactly one consumption")
    ledger = _exact(_load_object(ledger_path), LEDGER_KEYS, "R3 ledger")
    if ledger_path.stat().st_mode & 0o777 != 0o600:
        raise VerificationError("R3 ledger mode must be 0600")
    ledger_digest = _self_hash(ledger, "ledger_sha256", "R3 ledger")
    if (
        ledger["schema_version"] != 1
        or ledger["state"] != "consumed"
        or ledger["authority_id"] != authority
        or ledger["final_receipt_path"] != str(final_path)
        or ledger["frozen_package_file_sha256"] != _sha256_file(package_path)
        or ledger["frozen_commit"] != frozen
        or ledger["frozen_tree"] != frozen_tree
        or ledger["candidate_commit"] != candidate
        or ledger["candidate_tree"] != candidate_r0["record"]["candidate_tree"]
        or ledger["candidate_diff_sha256"] != package["candidate_diff_sha256"]
        or ledger["policy_contract_sha256"] != package["policy_contract_sha256"]
        or ledger["git_ref_sha256s"] != ref_hashes
        or ledger["host_r3_manifest_sha256"] != _sha256_file(_path(index["host_r3_manifest"], "host R3 manifest"))
        or ledger["r3_commitment_sha256"] != host["manifest_sha256"]
        or ledger["calibration_sha256"] != calibration["calibration_sha256"]
        or ledger["calibration_file_sha256"] != _sha256_file(_path(index["calibration"], "calibration"))
        or ledger["source_receipt_sha256"] != source["receipt_sha256"]
        or ledger["source_receipt_file_sha256"] != _sha256_file(_path(index["source_receipt"], "source receipt"))
        or ledger["candidate_r0_receipt_sha256"] != candidate_r0["receipt_sha256"]
        or ledger["candidate_r0_file_sha256"] != candidate_r0["file_sha256"]
        or ledger["r2_receipt_sha256"] != candidate_r2["receipt_sha256"]
        or ledger["r2_receipt_file_sha256"] != candidate_r2["file_sha256"]
        or ledger["binary_sha256"] != candidate_r0["record"]["binary_sha256"]
        or ledger["portfolio_evaluator_sha256s"] != candidate_r2["record"]["evaluator"]
        or ledger["trace_sha256s"] != [item["sha256"] for item in private]
    ):
        raise VerificationError("R3 consumed ledger binding mismatch")
    snapshot = _exact(
        ledger["private_snapshot"],
        {
            "root", "manifest_sha256", "trace_sha256s", "source_receipt_sha256",
            "r0_receipt_sha256", "r0_artifact_sha256s", "r0_evidence_sha256s",
        },
        "R3 private snapshot",
    )
    if (
        snapshot["manifest_sha256"] != _sha256_file(_path(index["host_r3_manifest"], "host R3 manifest"))
        or snapshot["trace_sha256s"] != [item["sha256"] for item in private]
        or snapshot["source_receipt_sha256"] != _sha256_file(_path(index["source_receipt"], "source receipt"))
        or snapshot["r0_receipt_sha256"] != candidate_r0["file_sha256"]
    ):
        raise VerificationError("R3 private snapshot lineage mismatch")
    final = _exact(_load_object(final_path), R3_RECEIPT_KEYS, "R3 final receipt")
    final_digest = _self_hash(final, "receipt_sha256", "R3 final receipt")
    _forbid_outcomes(final, "R3 final receipt")
    ledger_file_sha = _sha256_file(ledger_path)
    if (
        final["schema_version"] != 1
        or final["receipt_version"] != 1
        or final["rung"] != "r3"
        or final["state"] != "measured"
        or final["authority_id"] != authority
        or final["final_receipt_path"] != str(final_path)
        or final["frozen_commit"] != frozen
        or final["candidate_commit"] != candidate
        or final["candidate_tree"] != candidate_r0["record"]["candidate_tree"]
        or final["policy"] != package["policy"]
        or final["ledger_path"] != str(ledger_path)
        or final["ledger_sha256"] != ledger_digest
        or final["ledger_intended_sha256"] != ledger_file_sha
        or final["ledger_file_sha256"] != ledger_file_sha
        or final["ledger_size_bytes"] != ledger_path.stat().st_size
        or final["r3_commitment_sha256"] != host["manifest_sha256"]
        or final["host_r3_manifest_sha256"] != _sha256_file(_path(index["host_r3_manifest"], "host R3 manifest"))
        or final["calibration_sha256"] != calibration["calibration_sha256"]
        or final["source_receipt_sha256"] != source["receipt_sha256"]
        or final["r0_receipt_sha256"] != candidate_r0["receipt_sha256"]
        or final["r2_receipt_sha256"] != candidate_r2["receipt_sha256"]
        or final["failures"] != []
    ):
        raise VerificationError("R3 final receipt binding mismatch")
    requested = _integer(ledger["requested_at_unix_ns"], "R3 request time", 1)
    started = _integer(final["started_at_unix_ns"], "R3 start time", 1)
    ended = _integer(final["ended_at_unix_ns"], "R3 end time", 1)
    commit_seconds = int(_git(task_root, "show", "-s", "--format=%ct", frozen))
    if not commit_seconds * 1_000_000_000 <= requested <= started <= ended:
        raise VerificationError("R3 chronology is invalid")
    output = _path(final["output_path"], "R3 output")
    portfolio_path = output / _string(final["portfolio_receipt_path"], "R3 portfolio receipt")
    r3_portfolio = _verify_portfolio(
        portfolio_path,
        "r3",
        task_root,
        host,
        source,
        r0_by_hash,
        checkout,
    )
    if (
        r3_portfolio["receipt_sha256"] != final["portfolio_receipt_sha256"]
        or final["portfolio_receipt_sha256"] != _load_object(portfolio_path)["receipt_sha256"]
    ):
        raise VerificationError("R3 portfolio receipt binding mismatch")
    raw_paths = []
    for measurement in r3_portfolio["measurements"]:
        raw_paths.append(_relative(output, measurement["process"]["stdout"]["path"], "R3 stdout"))
        raw_paths.append(_relative(output, measurement["process"]["stderr"]["path"], "R3 stderr"))
    if any(ledger_path.stat().st_mtime_ns > path.stat().st_mtime_ns for path in raw_paths):
        raise VerificationError("R3 ledger does not predate raw outputs")
    expected_facts = []
    for summary, measurement in zip(final["measurements"], r3_portfolio["measurements"]):
        _exact(summary, {"cell_index", "path", "measurement_sha256", "pareto"}, "R3 measurement fact")
        pareto = {
            field: measurement[field]
            for field in (
                "rung", "split", "trace_id", "policy", "cache_fraction",
                "cache_size_bytes", "request_count", "object_miss_ratio",
                "byte_miss_ratio", "simulator_throughput_mqps", "cpu_ns_per_request",
                "metadata_bytes_per_object", "global_metadata_bytes", "metadata_measurement_sha256",
            )
        }
        expected_facts.append(
            {
                "cell_index": len(expected_facts),
                "path": r3_portfolio["record"]["measurements"][len(expected_facts)]["path"],
                "measurement_sha256": measurement["measurement_sha256"],
                "pareto": pareto,
            }
        )
    if final["measurements"] != expected_facts:
        raise VerificationError("R3 factual measurement projection mismatch")
    if not isinstance(final["constraints"], list) or len(final["constraints"]) != len(expected_facts):
        raise VerificationError("R3 constraint fact inventory mismatch")
    for index_value, constraint in enumerate(final["constraints"]):
        _exact(constraint, {"cell_index", "measurement_sha256", "facts"}, "R3 constraint")
        if (
            constraint["cell_index"] != index_value
            or constraint["measurement_sha256"] != expected_facts[index_value]["measurement_sha256"]
            or not isinstance(constraint["facts"], dict)
        ):
            raise VerificationError("R3 constraint binding mismatch")
    return {
        "state": "verified",
        "execution_state": "measured",
        "authority_id": authority,
        "ledger_sha256": ledger_digest,
        "receipt_sha256": final_digest,
        "measurement_count": len(expected_facts),
    }


def verify(index_path):
    index_file = _path(str(Path(index_path).absolute()), "retained index")
    index = _exact(_load_object(index_file), INDEX_KEYS, "retained index")
    if index["schema_version"] != 1:
        raise VerificationError("retained index schema_version must be 1")
    checkout = _path(index["checkout"], "source checkout")
    if not checkout.is_dir():
        raise VerificationError("source checkout must be a directory")
    task_root = _path(index["task_root"], "task root")
    if not task_root.is_dir():
        raise VerificationError("task root must be a directory")
    source_path = _path(index["source_receipt"], "source receipt")
    source, source_state = _verify_source(source_path, checkout)
    task_path = _path(index["task_manifest"], "task manifest")
    host_path = _path(index["host_r3_manifest"], "host R3 manifest")
    task, host, public, private, data_state = _verify_manifests(
        task_path, host_path, task_root
    )
    raw_r0 = index["r0_receipts"]
    if not isinstance(raw_r0, list) or len(raw_r0) != 6:
        raise VerificationError("retained index requires exactly six R0 receipts")
    r0s = [
        _verify_r0(
            _path(path, "R0 receipt"),
            checkout,
            source,
            expected_policy=policy,
        )
        for path, policy in zip(raw_r0, POLICIES)
    ]
    r0_by_hash = {item["receipt_sha256"]: item for item in r0s}
    if len(r0_by_hash) != 6:
        raise VerificationError("R0 receipt hashes are not unique")
    raw_r1 = index["r1_receipts"]
    if not isinstance(raw_r1, list) or not raw_r1:
        raise VerificationError("retained index requires at least one R1 receipt")
    r1s = [
        _verify_portfolio(
            _path(path, "R1 receipt"),
            "r1",
            task_root,
            task,
            source,
            r0_by_hash,
            checkout,
        )
        for path in raw_r1
    ]
    raw_r2 = index["r2_receipts"]
    if not isinstance(raw_r2, list) or len(raw_r2) != 14:
        raise VerificationError("retained index requires fourteen calibration R2 receipts")
    r2s = [
        _verify_portfolio(
            _path(path, "R2 receipt"),
            "r2",
            task_root,
            task,
            source,
            r0_by_hash,
            checkout,
        )
        for path in raw_r2
    ]
    calibration_path = _path(index["calibration"], "calibration")
    expected_calibration = _hash(index["calibration_sha256"], "external calibration digest")
    calibration, calibration_state = _verify_calibration(
        calibration_path,
        expected_calibration,
        task,
        source,
        r0s,
        r2s,
    )
    r3_state = _verify_r3(
        index,
        task_root,
        checkout,
        task,
        host,
        public,
        private,
        source,
        calibration,
        r0_by_hash,
    )
    return {
        "source": source_state,
        "data_boundary": data_state,
        "r0": {
            "state": "verified",
            "policies": list(POLICIES),
            "receipt_sha256s": [item["receipt_sha256"] for item in r0s],
        },
        "r1": {
            "state": "verified",
            "receipt_count": len(r1s),
            "measurement_count": sum(len(item["measurements"]) for item in r1s),
        },
        "r2": {
            "state": "verified",
            "receipt_count": len(r2s),
            "measurement_count": sum(len(item["measurements"]) for item in r2s),
        },
        "calibration": calibration_state,
        "r3": r3_state,
        "unresolved_audit": [
            "metadata_allocation_coverage",
            "amortized_o1_complexity",
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("index", type=Path)
    try:
        arguments = parser.parse_args(argv)
        result = verify(arguments.index)
        sys.stdout.buffer.write(canonical_bytes(result) + b"\n")
        return 0
    except (OSError, TypeError, ValueError, subprocess.SubprocessError):
        print("error: substrate verification failed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
