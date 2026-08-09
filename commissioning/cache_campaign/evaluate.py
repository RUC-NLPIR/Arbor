from __future__ import annotations

import hashlib
import os
import platform
import re
import stat
import struct
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from decimal import Decimal, localcontext
from pathlib import Path, PurePosixPath

from .cachesim import ChildResult, parse_cachesim_output, run_child
from .portfolio import evaluate_portfolio
from .evidence import (
    ArtifactRegistry,
    Binding as _Binding,
    EvidenceError,
    Invocation as _Invocation,
    capture_binding as _binding,
    capture_executable as _capture_executable,
    capture_expected_evidence as _capture_expected_evidence,
    checkout_path as _checkout_path,
    cleanup_owned as _cleanup_owned,
    command_evidence_expectations as _command_evidence_expectations,
    command_record as _command_record,
    directory_identity as _directory_identity,
    discover_static_archive,
    evidence_inventory as _evidence_inventory,
    executable_hash as _executable_hash,
    output_path as _output_path,
    publish_stage as _publish_stage,
    refresh_file_record as _refresh_file_record,
    regular_bytes as _regular_bytes,
    regular_identity as _regular_identity,
    revalidate_checkout as _post_binding,
    revalidate_command_evidence as _revalidate_command_evidence,
    skipped_command_record as _skipped_command_record,
    stage_directory as _stage_directory,
    unexpected_stage_entries as _unexpected_stage_entries,
    verify_final_stage as _verify_final_stage,
)
from .records import (
    canonical_decimal,
    load_object,
    record_sha256,
    scientific_decimal_context,
    sha256_file,
    write_new_record,
)
from .r0_probes import (
    ProbeError,
    allocator_compile_argv,
    allocator_interposer_source,
    capacity_compile_argv,
    capacity_probe_source,
    metadata_compile_argv,
    metadata_probe_source,
    metadata_run_argv,
    parse_capacity_probe as _parse_capacity_probe,
    parse_metadata_probe as _parse_metadata_probe,
    probe_build_flags,
)
from .scope import PolicyContract, ScopeFacts, evaluate_scope
from .source import validate_source


__all__ = ["evaluate_portfolio", "evaluate_r0", "validate_r0_metadata_evidence"]


SOURCE_LOCK = load_object(Path(__file__).with_name("source.lock.json"))
Run = Callable[..., ChildResult]
_ORACLE = struct.Struct("<IQIq")
_REQUEST_COUNT = 10_000
_SEED = 0xA205_2026
_SANITIZER = re.compile(
    rb"(?:AddressSanitizer|UndefinedBehaviorSanitizer|LeakSanitizer|"
    rb"runtime error:|SUMMARY: [^\r\n]*Sanitizer)",
    re.IGNORECASE,
)
_HEX40 = re.compile(r"[0-9a-f]{40}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_MIB = 1024 * 1024
_COMMAND_LIMITS: dict[str, tuple[float, int]] = {
    "release-configure": (600.0, 16 * _MIB),
    "release-build": (1800.0, 64 * _MIB),
    "release-full-tests": (1800.0, 64 * _MIB),
    "candidate-test": (600.0, 16 * _MIB),
    "sanitize-configure": (600.0, 16 * _MIB),
    "sanitize-build": (1800.0, 64 * _MIB),
    "sanitize-full-tests": (1800.0, 64 * _MIB),
    "determinism-run-1": (120.0, 16 * _MIB),
    "determinism-run-2": (120.0, 16 * _MIB),
    "capacity-compile": (300.0, 16 * _MIB),
    "capacity-run": (120.0, 16 * _MIB),
    "metadata-interposer-compile": (300.0, 16 * _MIB),
    "metadata-compile": (300.0, 16 * _MIB),
    "metadata-run": (120.0, 16 * _MIB),
}
_GIT_CONFIG = [
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
]


EvaluationError = EvidenceError


@dataclass(frozen=True)
class ValidatedR0File:
    name: str
    path: Path
    identity: tuple[int, int]
    mode: int
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class ValidatedR0MetadataEvidence:
    files: tuple[ValidatedR0File, ...]
    stdout: ValidatedR0File


def _capture_r0_file(name: str, path: Path) -> tuple[ValidatedR0File, bytes]:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    digest = hashlib.sha256()
    raw = bytearray()
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise EvaluationError(f"R0 retained evidence is not regular: {name}")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
                raw.extend(block)
        after = path.stat(follow_symlinks=False)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if (
        stat.S_ISLNK(after.st_mode)
        or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or before.st_ctime_ns != after.st_ctime_ns
        or len(raw) != before.st_size
    ):
        raise EvaluationError(f"R0 retained evidence changed while reading: {name}")
    return (
        ValidatedR0File(
            name=name,
            path=path.resolve(strict=True),
            identity=(before.st_dev, before.st_ino),
            mode=stat.S_IMODE(before.st_mode),
            size_bytes=len(raw),
            sha256=digest.hexdigest(),
        ),
        bytes(raw),
    )


def _revalidate_r0_file(expected: ValidatedR0File) -> None:
    observed, _raw = _capture_r0_file(expected.name, expected.path)
    if observed != expected:
        raise EvaluationError(f"R0 retained evidence binding changed: {expected.name}")

def _command_outcome(invocation: _Invocation) -> bool | None:
    if invocation.result is None:
        return None
    return invocation.result.returncode == 0


def _combined_outcome(*values: bool | None) -> bool | None:
    if any(value is False for value in values):
        return False
    if any(value is None for value in values):
        return None
    return True


def _clear_unavailable_checks(checks: dict[str, bool | None]) -> None:
    for key in (
        "build",
        "full_tests",
        "candidate_test",
        "sanitizer",
        "deterministic",
        "capacity",
        "metadata_probe",
    ):
        checks[key] = None


def _bounded(value: object, limit: int = 512) -> str:
    message = " ".join(str(value).split()) or value.__class__.__name__
    return message if len(message) <= limit else message[: limit - 3] + "..."


def _git_result(checkout: Path, *argv: str) -> subprocess.CompletedProcess[bytes]:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    environment["GIT_NO_LAZY_FETCH"] = "1"
    return subprocess.run(
        ["git", *_GIT_CONFIG, *argv],
        cwd=checkout,
        capture_output=True,
        check=False,
        env=environment,
    )


def _git_bytes(checkout: Path, *argv: str) -> bytes:
    result = _git_result(checkout, *argv)
    if result.returncode != 0:
        raise EvaluationError(
            f"Git command failed ({' '.join(argv)}): "
            f"{_bounded(result.stderr.decode('utf-8', errors='replace'), 300)}"
        )
    return result.stdout


def _git(checkout: Path, *argv: str) -> str:
    try:
        return _git_bytes(checkout, *argv).decode("utf-8").strip()
    except UnicodeError as error:
        raise EvaluationError("Git binding output is not UTF-8") from error


def _write_private(path: Path, raw: bytes, mode: int = 0o600) -> None:
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


def generate_synthetic_trace(path: Path) -> dict[str, object]:
    """Create the fixed apparatus-owned 10,000-record OracleGeneral trace."""
    state = _SEED
    objects: list[int] = []
    for _index in range(_REQUEST_COUNT):
        state = (1_664_525 * state + 1_013_904_223) & 0xFFFF_FFFF
        objects.append(1 + ((state >> 8) % 512))
    next_access = [-1] * _REQUEST_COUNT
    following: dict[int, int] = {}
    for index in range(_REQUEST_COUNT - 1, -1, -1):
        object_id = objects[index]
        next_access[index] = following.get(object_id, -1)
        following[object_id] = index + 1
    raw = bytearray()
    sizes: dict[int, int] = {}
    for index, object_id in enumerate(objects, start=1):
        size = 64 * (1 + object_id % 4)
        sizes[object_id] = size
        raw.extend(_ORACLE.pack(index - 1, object_id, size, next_access[index - 1]))
    _write_private(Path(path), bytes(raw))
    return {
        "classification": "pre_registered_synthetic_unit_data",
        "record_layout": "<IQIq",
        "request_count": _REQUEST_COUNT,
        "seed": _SEED,
        "generator": "lcg32-numerical-recipes",
        "distribution": "object_id=1+((state>>8)%512); size=64*(1+object_id%4)",
        "next_access_vtime": "one_based_future_request_or_minus_one",
        "working_set_bytes": sum(sizes.values()),
        "size_bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def parse_capacity_probe(output: str) -> dict[str, int | bool]:
    try:
        return _parse_capacity_probe(output)
    except ProbeError as error:
        raise EvaluationError(str(error)) from error


def parse_metadata_probe(output: str) -> tuple[Decimal, int]:
    try:
        with localcontext(scientific_decimal_context()):
            return _parse_metadata_probe(output)
    except ProbeError as error:
        raise EvaluationError(str(error)) from error


def _retained_r0_artifact(
    receipt: Mapping[str, object], r0_root: Path, name: str
) -> tuple[dict[str, object], ValidatedR0File, bytes]:
    artifacts = receipt.get("artifact_snapshots")
    record = artifacts.get(name) if isinstance(artifacts, dict) else None
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
        raise EvaluationError(f"R0 artifact receipt is invalid: {name}")
    relative = record.get("snapshot_path")
    if type(relative) is not str:
        raise EvaluationError(f"R0 artifact path is invalid: {name}")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts:
        raise EvaluationError(f"R0 artifact path escapes its receipt: {name}")
    path = r0_root.joinpath(*pure.parts)
    file_receipt, raw = _capture_r0_file(name, path)
    recorded_identity = record.get("snapshot_identity")
    if (
        record.get("binding_intact") is not True
        or not isinstance(recorded_identity, dict)
        or set(recorded_identity) != {"device", "inode"}
        or recorded_identity.get("device") != file_receipt.identity[0]
        or recorded_identity.get("inode") != file_receipt.identity[1]
        or record.get("size_bytes") != file_receipt.size_bytes
        or record.get("sha256") != file_receipt.sha256
    ):
        raise EvaluationError(f"R0 artifact snapshot binding mismatch: {name}")
    _revalidate_r0_file(file_receipt)
    return record, file_receipt, raw


def _retained_r0_raw_evidence(
    receipt: Mapping[str, object],
    r0_root: Path,
    raw_record: object,
    expected_path: str,
) -> tuple[bytes, ValidatedR0File]:
    if not isinstance(raw_record, dict) or set(raw_record) != {
        "path",
        "size_bytes",
        "sha256",
        "binding_intact",
    }:
        raise EvaluationError("R0 metadata command raw receipt is invalid")
    if raw_record.get("path") != expected_path or raw_record.get("binding_intact") is not True:
        raise EvaluationError("R0 metadata command raw path binding mismatch")
    pure = PurePosixPath(expected_path)
    if pure.is_absolute() or ".." in pure.parts:
        raise EvaluationError("R0 metadata command raw path escapes its receipt")
    path = r0_root.joinpath(*pure.parts)
    file_receipt, raw = _capture_r0_file(expected_path, path)
    identity = file_receipt.identity
    digest = file_receipt.sha256
    if raw_record.get("size_bytes") != len(raw) or raw_record.get("sha256") != digest:
        raise EvaluationError("R0 metadata command raw hash mismatch")
    inventory = receipt.get("evidence_inventory")
    matches = [
        item
        for item in inventory
        if isinstance(item, dict) and item.get("path") == expected_path
    ] if isinstance(inventory, list) else []
    if len(matches) != 1:
        raise EvaluationError("R0 metadata command inventory entry is missing")
    item = matches[0]
    expected_identity = {"device": identity[0], "inode": identity[1]}
    if (
        set(item) != {
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
        or item.get("identity") != expected_identity
        or item.get("observed_identity") != expected_identity
        or item.get("size_bytes") != len(raw)
        or item.get("observed_size_bytes") != len(raw)
        or item.get("sha256") != digest
        or item.get("observed_sha256") != digest
        or item.get("present") is not True
        or item.get("binding_intact") is not True
    ):
        raise EvaluationError("R0 metadata command inventory binding mismatch")
    _revalidate_r0_file(file_receipt)
    return raw, file_receipt


def validate_r0_metadata_evidence(
    receipt: Mapping[str, object],
    r0_root: Path,
    *,
    candidate: str,
    policy: str,
    source_receipt_sha256: str,
) -> ValidatedR0MetadataEvidence:
    """Validate and return every retained Task 4 metadata dependency."""
    validated_files: list[ValidatedR0File] = []
    if (
        receipt.get("candidate_commit") != candidate
        or receipt.get("policy") != policy
        or receipt.get("source_receipt_sha256") != source_receipt_sha256
    ):
        raise EvaluationError("R0 metadata command candidate/source binding mismatch")
    evaluator = receipt.get("evaluator")
    expected_evaluator_names = (
        "evaluate",
        "scope",
        "evidence",
        "r0_probes",
        "cachesim",
        "linux_subreaper",
    )
    if not isinstance(evaluator, dict) or set(evaluator) != {
        f"{name}_sha256" for name in expected_evaluator_names
    }:
        raise EvaluationError("R0 evaluator dependency binding is invalid")
    for name in expected_evaluator_names:
        artifact, file_receipt, _raw = _retained_r0_artifact(
            receipt, r0_root, f"evaluator_{name}"
        )
        validated_files.append(file_receipt)
        if evaluator.get(f"{name}_sha256") != artifact.get("sha256"):
            raise EvaluationError("R0 evaluator dependency hash mismatch")

    metadata_probe, metadata_probe_file, _probe_raw = _retained_r0_artifact(
        receipt, r0_root, "metadata_probe_binary"
    )
    interposer, interposer_file, _interposer_raw = _retained_r0_artifact(
        receipt, r0_root, "metadata_interposer_binary"
    )
    metadata_source, metadata_source_file, _metadata_source_raw = _retained_r0_artifact(
        receipt, r0_root, "metadata_probe_source"
    )
    interposer_source, interposer_source_file, _interposer_source_raw = (
        _retained_r0_artifact(receipt, r0_root, "metadata_interposer_source")
    )
    synthetic, synthetic_file, _synthetic_raw = _retained_r0_artifact(
        receipt, r0_root, "synthetic_trace"
    )
    validated_files.extend(
        (
            metadata_probe_file,
            interposer_file,
            metadata_source_file,
            interposer_source_file,
            synthetic_file,
        )
    )
    probes = receipt.get("probes")
    metadata_probe_record = probes.get("metadata") if isinstance(probes, dict) else None
    if (
        not isinstance(metadata_probe_record, dict)
        or metadata_probe_record.get("accounting_scope") != "process_wide_ld_preload"
        or metadata_probe_record.get("source_sha256") != metadata_source.get("sha256")
        or not isinstance(metadata_probe_record.get("binary"), dict)
        or metadata_probe_record["binary"].get("sha256") != metadata_probe.get("sha256")
        or metadata_probe_record.get("interposer_source_sha256")
        != interposer_source.get("sha256")
        or not isinstance(metadata_probe_record.get("interposer_binary"), dict)
        or metadata_probe_record["interposer_binary"].get("sha256")
        != interposer.get("sha256")
    ):
        raise EvaluationError("R0 metadata probe artifact binding is invalid")
    synthetic_record = receipt.get("synthetic_trace")
    if not isinstance(synthetic_record, dict):
        raise EvaluationError("R0 synthetic trace binding is invalid")
    cache_size = synthetic_record.get("cache_size_bytes")
    if type(cache_size) is not int or cache_size <= 0:
        raise EvaluationError("R0 synthetic cache size binding is invalid")

    commands = receipt.get("commands")
    matches = [
        item
        for item in commands
        if isinstance(item, dict) and item.get("label") == "metadata-run"
    ] if isinstance(commands, list) else []
    if len(matches) != 1:
        raise EvaluationError("R0 metadata command receipt is missing")
    command = matches[0]
    expected_index = 13 if candidate == receipt.get("base_commit") else 14
    expected_command_keys = {
        "index",
        "label",
        "argv",
        "cwd",
        "timeout_seconds",
        "max_output_bytes",
        "returncode",
        "wall_ns",
        "cpu_ns",
        "stdout",
        "stderr",
        "command_sha256",
    }
    command_hash = command.get("command_sha256")
    if (
        set(command) != expected_command_keys
        or type(command_hash) is not str
        or _HEX64.fullmatch(command_hash) is None
        or command_hash != record_sha256(command, "command_sha256")
    ):
        raise EvaluationError("R0 metadata command self-hash mismatch")
    source_paths = [
        interposer.get("source_path"),
        metadata_probe.get("source_path"),
        synthetic.get("source_path"),
    ]
    if any(type(item) is not str or not Path(item).is_absolute() for item in source_paths):
        raise EvaluationError("R0 metadata command artifact source path is invalid")
    expected_cwd = str(Path(str(metadata_probe["source_path"])).parent)
    if any(str(Path(str(item)).parent) != expected_cwd for item in source_paths):
        raise EvaluationError("R0 metadata command artifact roots differ")
    expected_argv = [
        "/usr/bin/env",
        f"LD_PRELOAD={interposer['source_path']}",
        metadata_probe["source_path"],
        synthetic["source_path"],
        policy,
        str(cache_size),
    ]
    if (
        command.get("index") != expected_index
        or command.get("argv") != expected_argv
        or command.get("cwd") != expected_cwd
        or command.get("timeout_seconds") != _COMMAND_LIMITS["metadata-run"][0]
        or command.get("max_output_bytes") != _COMMAND_LIMITS["metadata-run"][1]
        or command.get("returncode") != 0
        or type(command.get("wall_ns")) is not int
        or command["wall_ns"] < 0
        or type(command.get("cpu_ns")) is not int
        or command["cpu_ns"] < 0
    ):
        raise EvaluationError("R0 metadata command process binding mismatch")
    prefix = f"commands/{expected_index:02d}-metadata-run"
    stdout_raw, stdout_file = _retained_r0_raw_evidence(
        receipt, r0_root, command.get("stdout"), f"{prefix}/stdout.raw"
    )
    stderr_raw, stderr_file = _retained_r0_raw_evidence(
        receipt, r0_root, command.get("stderr"), f"{prefix}/stderr.raw"
    )
    validated_files.extend((stdout_file, stderr_file))
    if stderr_raw:
        raise EvaluationError("R0 metadata command stderr is not empty")
    measured = receipt.get("measured_metadata")
    if not isinstance(measured, dict):
        raise EvaluationError("R0 measured metadata is missing")
    try:
        measured_bytes, measured_global = parse_metadata_probe(stdout_raw.decode("ascii"))
    except (UnicodeError, ValueError) as error:
        raise EvaluationError("R0 metadata command stdout is invalid") from error
    if (
        canonical_decimal(measured_bytes) != measured.get("bytes_per_object")
        or measured_global != measured.get("global_bytes")
        or measured.get("measurement_sha256") != hashlib.sha256(stdout_raw).hexdigest()
    ):
        raise EvaluationError("R0 metadata facts differ from exact process evidence")
    for file_receipt in validated_files:
        _revalidate_r0_file(file_receipt)
    ordered = tuple(sorted(validated_files, key=lambda item: item.name))
    return ValidatedR0MetadataEvidence(ordered, stdout_file)


def _candidate_ctest_passed(invocation: _Invocation, policy: str) -> bool:
    if not invocation.ok:
        return False
    try:
        output = invocation.stdout.decode("ascii")
    except UnicodeError:
        return False
    test_name = f"test_{policy}"
    return (
        "No tests were found" not in output
        and re.search(rf"\bStart\s+[0-9]+:\s+{re.escape(test_name)}\b", output)
        is not None
        and re.search(
            rf"\b1/1\s+Test\s+#[0-9]+:\s+{re.escape(test_name)}\s+.*\bPassed\b",
            output,
        )
        is not None
        and "100% tests passed, 0 tests failed out of 1" in output
    )


def _source_receipt(path: Path, lock: Mapping[str, object]) -> dict[str, object]:
    raw_path = Path(path)
    try:
        metadata = raw_path.lstat()
        if raw_path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise EvaluationError("source receipt must be a regular non-symlink file")
        resolved = raw_path.resolve(strict=True)
    except EvaluationError:
        raise
    except OSError as error:
        raise EvaluationError("source receipt is missing") from error
    receipt = load_object(resolved)
    expected_keys = {
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
    if set(receipt) != expected_keys:
        raise EvaluationError("source receipt keys do not match the Task 1 receipt")
    digest = receipt.get("receipt_sha256")
    if (
        type(digest) is not str
        or _HEX64.fullmatch(digest) is None
        or digest != record_sha256(receipt, "receipt_sha256")
    ):
        raise EvaluationError("source receipt self-hash mismatch")
    exact = {
        "schema_version": 1,
        "repository_url": lock.get("repository_url"),
        "commit": lock.get("commit"),
        "tree": lock.get("tree"),
        "binary": lock.get("binary"),
    }
    if any(receipt.get(key) != value for key, value in exact.items()):
        raise EvaluationError("source receipt does not match the pinned source lock")
    if receipt.get("clean") is not True:
        raise EvaluationError("source receipt does not record a clean source")
    binary_sha256 = receipt.get("binary_sha256")
    if type(binary_sha256) is not str or _HEX64.fullmatch(binary_sha256) is None:
        raise EvaluationError("source receipt binary binding is invalid")
    commands = receipt.get("commands")
    expected_argv = [
        lock.get("configure_argv"),
        lock.get("build_argv"),
        lock.get("test_argv"),
    ]
    if not isinstance(commands, list) or len(commands) != 3:
        raise EvaluationError("source receipt command binding is invalid")
    for item, argv in zip(commands, expected_argv, strict=True):
        if (
            not isinstance(item, dict)
            or set(item) != {"argv", "returncode", "stdout_sha256", "stderr_sha256"}
            or item.get("argv") != argv
            or type(item.get("returncode")) is not int
            or item.get("returncode") != 0
            or type(item.get("stdout_sha256")) is not str
            or _HEX64.fullmatch(item["stdout_sha256"]) is None
            or type(item.get("stderr_sha256")) is not str
            or _HEX64.fullmatch(item["stderr_sha256"]) is None
        ):
            raise EvaluationError("source receipt command binding is invalid")
    compilers = receipt.get("compilers")
    if not isinstance(compilers, dict) or set(compilers) != {"c", "cxx"}:
        raise EvaluationError("source receipt C compiler binding is missing")
    for language in ("c", "cxx"):
        compiler_record = compilers.get(language)
        if not isinstance(compiler_record, dict) or set(compiler_record) != {
            "path",
            "version",
        }:
            raise EvaluationError("source receipt compiler binding is invalid")
        compiler_path = compiler_record.get("path")
        compiler_version = compiler_record.get("version")
        if (
            type(compiler_path) is not str
            or not Path(compiler_path).is_absolute()
            or type(compiler_version) is not str
            or not compiler_version
        ):
            raise EvaluationError("source receipt compiler binding is invalid")
    versions = receipt.get("versions")
    if (
        not isinstance(versions, dict)
        or set(versions) != {"cmake", "ninja"}
        or any(type(value) is not str or not value for value in versions.values())
        or type(receipt.get("interpreter")) is not str
        or not receipt["interpreter"]
        or type(receipt.get("platform")) is not str
        or not receipt["platform"]
    ):
        raise EvaluationError("source receipt tool binding is invalid")
    receipt["_resolved_path"] = str(resolved)
    receipt["_file_sha256"] = sha256_file(resolved)
    return receipt


def _preflight(
    checkout: Path,
    base: str,
    candidate: str,
    policy: str,
    source_receipt: Path,
    progress: dict[str, object],
) -> tuple[
    dict[str, object],
    str,
    ScopeFacts,
    PolicyContract | None,
    _Binding,
]:
    try:
        root = Path(checkout).resolve(strict=True)
        if Path(checkout).is_symlink() or not root.is_dir():
            raise EvaluationError("checkout must be a real directory")
    except EvaluationError:
        raise
    except OSError as error:
        raise EvaluationError("checkout must exist") from error
    locked_base = SOURCE_LOCK.get("commit")
    locked_tree = SOURCE_LOCK.get("tree")
    if base != locked_base or _HEX40.fullmatch(base) is None:
        raise EvaluationError("base must equal the exact pinned source commit")
    if candidate != _git(root, "rev-parse", "HEAD"):
        raise EvaluationError("candidate checkout HEAD mismatch")
    if _HEX40.fullmatch(candidate) is None:
        raise EvaluationError("candidate must be a lowercase SHA-1 commit")
    if _git(root, "rev-parse", f"{base}^{{tree}}") != locked_tree:
        raise EvaluationError("base tree does not equal the pinned source tree")
    ancestry = _git_result(root, "merge-base", "--is-ancestor", base, candidate)
    if ancestry.returncode != 0:
        raise EvaluationError("candidate is not a descendant of the pinned base")
    tree = _git(root, "rev-parse", "HEAD^{tree}")
    progress["candidate_tree"] = tree
    candidate_lock = {
        "commit": candidate,
        "tree": tree,
        "repository_url": SOURCE_LOCK.get("repository_url"),
    }
    try:
        validate_source(root, candidate_lock)
    except (OSError, ValueError) as error:
        raise EvaluationError(_bounded(error)) from error
    for name in ("_build-release", "_build-sanitize"):
        if os.path.lexists(root / name):
            raise EvaluationError(f"clean evaluation build directory must be absent: {name}")
    source = _source_receipt(source_receipt, SOURCE_LOCK)
    progress["source_binding"] = True
    progress["source_receipt"] = source
    facts, contract = evaluate_scope(
        root,
        base=base,
        candidate=candidate,
        policy=policy,
    )
    progress["scope"] = facts
    progress["contract"] = contract
    structurally_valid = (
        facts.allowed_paths
        and facts.baseline_unchanged
        and facts.additive_wiring_only
        and (facts.contract_bound is True if candidate != base else facts.contract_bound is None)
    )
    if not structurally_valid:
        raise EvaluationError("candidate scope or policy contract is invalid")
    return source, tree, facts, contract, _binding(root)


def _contract_receipt(contract: PolicyContract | None) -> dict[str, object] | None:
    if contract is None:
        return None
    return {
        "policy": contract.policy,
        "reference_policy": contract.reference_policy,
        "policy_source": contract.policy_source,
        "object_metadata_bytes": contract.object_metadata_bytes,
        "global_metadata_bytes": contract.global_metadata_bytes,
        "global_metadata_evidence": [
            {"source": source, "line": line, "expression": expression}
            for source, line, expression in contract.global_metadata_evidence
        ],
        "update_complexity": contract.update_complexity,
    }


def _failure_receipt(
    *,
    base: str,
    candidate: str,
    policy: str,
    source_receipt: Path,
    error: BaseException,
    started: int,
    progress: Mapping[str, object],
) -> dict[str, object]:
    checks = {
        "source_binding": progress.get("source_binding", False),
        "evidence_binding": None,
        "build": None,
        "full_tests": None,
        "candidate_test": None,
        "sanitizer": None,
        "deterministic": None,
        "capacity": None,
        "metadata_probe": None,
    }
    source = progress.get("source_receipt")
    source_hash = source.get("receipt_sha256") if isinstance(source, dict) else None
    scope = progress.get("scope")
    scope_record = (
        {**asdict(scope), "changed_paths": list(scope.changed_paths)}
        if isinstance(scope, ScopeFacts)
        else None
    )
    contract = progress.get("contract")
    return {
        "schema_version": 1,
        "receipt_version": 1,
        "rung": "r0",
        "base_commit": base,
        "candidate_commit": candidate,
        "policy": policy,
        "source_receipt_path": str(source_receipt),
        "source_receipt_sha256": source_hash,
        "checks": checks,
        "candidate_tree": progress.get("candidate_tree"),
        "scope": scope_record,
        "declared_metadata": (
            _contract_receipt(contract)
            if isinstance(contract, PolicyContract)
            else None
        ),
        "measured_metadata": None,
        "complexity_audit": "pending_independent_review",
        "synthetic_trace": None,
        "commands": [],
        "probes": None,
        "errors": [_bounded(error)],
        "timings": {"total_wall_ns": max(0, time.monotonic_ns() - started)},
    }


def evaluate_r0(
    *,
    checkout: Path,
    base: str,
    candidate: str,
    policy: str,
    source_receipt: Path,
    output: Path,
    run: Run = run_child,
) -> dict[str, object]:
    started = time.monotonic_ns()
    checkout_root = _checkout_path(Path(checkout))
    final = _output_path(Path(output), checkout_root)
    preflight_progress: dict[str, object] = {}
    try:
        source, candidate_tree, scope, contract, binding = _preflight(
            checkout_root,
            base,
            candidate,
            policy,
            Path(source_receipt),
            preflight_progress,
        )
        changed_path_sha256 = {
            path: hashlib.sha256(
                _git_bytes(checkout_root, "show", f"{candidate}:{path}")
            ).hexdigest()
            for path in scope.changed_paths
        }
        policy_source = f"libCacheSim/cache/eviction/{policy}.c"
        policy_source_sha256 = hashlib.sha256(
            _git_bytes(checkout_root, "show", f"{candidate}:{policy_source}")
        ).hexdigest()
        candidate_test_sha256 = (
            changed_path_sha256[f"test/test_{policy}.c"] if candidate != base else None
        )
        contract_sha256 = (
            changed_path_sha256["commissioning/cache_policy_contract.json"]
            if contract is not None
            else None
        )
        evaluator_paths = {
            "evaluate_sha256": Path(__file__),
            "scope_sha256": Path(__file__).with_name("scope.py"),
            "evidence_sha256": Path(__file__).with_name("evidence.py"),
            "r0_probes_sha256": Path(__file__).with_name("r0_probes.py"),
            "cachesim_sha256": Path(__file__).with_name("cachesim.py"),
            "linux_subreaper_sha256": Path(__file__).with_name(
                "linux_subreaper.py"
            ),
        }
        evaluator_hashes = {
            key: sha256_file(path) for key, path in evaluator_paths.items()
        }
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        stage, stage_identity = _stage_directory(final)
        published = False
        try:
            failure = _failure_receipt(
                base=base,
                candidate=candidate,
                policy=policy,
                source_receipt=Path(source_receipt),
                error=error,
                started=started,
                progress=preflight_progress,
            )
            write_new_record(stage / "receipt.json", failure, "receipt_sha256")
            _publish_stage(stage, stage_identity, final)
            published = True
        finally:
            if not published and os.path.lexists(stage):
                _cleanup_owned(stage, stage_identity)
        raise EvaluationError(_bounded(error)) from error

    root = Path(checkout).resolve(strict=True)
    stage, stage_identity = _stage_directory(final)
    published = False
    build_owners: dict[Path, tuple[int, int]] = {}
    errors: list[str] = []
    commands: list[_Invocation] = []
    artifacts = ArtifactRegistry(stage)
    provenance_valid = True
    source_binding_valid = True
    allowed_build_roots: set[Path] = set()

    def provenance_guard(boundary: str) -> bool:
        nonlocal provenance_valid, source_binding_valid
        if not provenance_valid:
            return False
        sanitizer_root = root / "_build-sanitize"
        if sanitizer_root not in allowed_build_roots and os.path.lexists(
            sanitizer_root
        ):
            source_binding_valid = False
            provenance_valid = False
            errors.append(f"{boundary} unexpected sanitizer build directory")
            return False
        for build_root in allowed_build_roots:
            try:
                if _directory_identity(build_root) != build_owners[build_root]:
                    raise EvaluationError("build directory identity changed")
            except (OSError, ValueError) as error:
                source_binding_valid = False
                provenance_valid = False
                errors.append(
                    f"{boundary} build directory mutation: {_bounded(error)}"
                )
                return False
        try:
            _post_binding(
                root,
                binding,
                allowed_roots=tuple(sorted(allowed_build_roots)),
            )
        except (OSError, ValueError) as error:
            source_binding_valid = False
            provenance_valid = False
            errors.append(f"{boundary} source binding mutation: {_bounded(error)}")
        mutated = artifacts.revalidate()
        if mutated:
            provenance_valid = False
            errors.append(
                f"{boundary} artifact binding mutation: {', '.join(mutated)}"
            )
        return provenance_valid

    def allow_configured_build_root(label: str) -> None:
        nonlocal provenance_valid, source_binding_valid
        build_root = root / (
            "_build-release" if label == "release-configure" else "_build-sanitize"
        )
        try:
            identity = _directory_identity(build_root)
        except (OSError, ValueError) as error:
            provenance_valid = False
            source_binding_valid = False
            errors.append(f"{label} build directory invalid: {_bounded(error)}")
            return
        build_owners[build_root] = identity
        allowed_build_roots.add(build_root)

    def capture_artifact(name: str, path: Path) -> None:
        nonlocal provenance_valid
        if not provenance_valid:
            return
        try:
            artifacts.capture(name, path)
        except (OSError, ValueError) as error:
            provenance_valid = False
            errors.append(f"artifact snapshot failed for {name}: {_bounded(error)}")

    for dependency_key, dependency_path in evaluator_paths.items():
        capture_artifact(
            "evaluator_" + dependency_key.removesuffix("_sha256"),
            dependency_path,
        )

    def invoke(
        label: str, argv: Sequence[str], *, command_cwd: Path = root
    ) -> _Invocation:
        timeout_seconds, max_output_bytes = _COMMAND_LIMITS[label]
        attempted = provenance_guard(f"before {label}")
        if attempted:
            item = _command_record(
                stage,
                len(commands) + 1,
                label,
                argv,
                command_cwd,
                run,
                timeout_seconds,
                max_output_bytes,
            )
        else:
            item = _skipped_command_record(
                stage,
                len(commands) + 1,
                label,
                argv,
                command_cwd,
                "skipped after provenance mutation",
                timeout_seconds,
                max_output_bytes,
            )
        commands.append(item)
        if not item.ok:
            errors.append(f"{label}: command did not complete successfully")
        if attempted and item.ok and label in {
            "release-configure",
            "sanitize-configure",
        }:
            allow_configured_build_root(label)
        if attempted:
            provenance_guard(f"after {label}")
        return item

    try:
        trace_path = stage / "synthetic.oracleGeneral.bin"
        synthetic = generate_synthetic_trace(trace_path)
        capture_artifact("synthetic_trace", trace_path)
        trace_evidence = _capture_expected_evidence(trace_path, stage)
        cache_bytes = int(synthetic["working_set_bytes"]) // 10

        release_config = invoke(
            "release-configure",
            [
                "cmake",
                "-S",
                ".",
                "-B",
                "_build-release",
                "-G",
                "Ninja",
                "-DCMAKE_BUILD_TYPE=Release",
                "-DENABLE_TESTS=ON",
            ],
        )
        release_root = root / "_build-release"
        if release_config.ok:
            capture_artifact(
                "release_cmake_cache", release_root / "CMakeCache.txt"
            )
        release_build = invoke(
            "release-build", ["cmake", "--build", "_build-release", "-j", "8"]
        )
        release_archive = release_root / "liblibCacheSim.a"
        if release_build.ok and provenance_valid:
            try:
                release_archive = discover_static_archive(release_root)
            except (OSError, ValueError) as error:
                provenance_valid = False
                errors.append(f"release archive discovery failed: {_bounded(error)}")
            if provenance_valid:
                capture_artifact("release_cachesim", release_root / "bin/cachesim")
                capture_artifact("release_archive", release_archive)
        release_tests = invoke(
            "release-full-tests",
            ["ctest", "--test-dir", "_build-release", "--output-on-failure"],
        )
        candidate_test: _Invocation | None = None
        if candidate != base:
            candidate_test = invoke(
                "candidate-test",
                [
                    "ctest",
                    "--test-dir",
                    "_build-release",
                    "--output-on-failure",
                    "-R",
                    f"^test_{policy}$",
                    "--no-tests=error",
                ],
            )
            if not _candidate_ctest_passed(candidate_test, policy):
                errors.append("candidate CTest did not run the exact registered test")
        sanitize_config = invoke(
            "sanitize-configure",
            [
                "cmake",
                "-S",
                ".",
                "-B",
                "_build-sanitize",
                "-G",
                "Ninja",
                "-DCMAKE_BUILD_TYPE=RelWithDebInfo",
                "-DENABLE_TESTS=ON",
                "-DCMAKE_C_FLAGS=-fsanitize=address,undefined -fno-omit-frame-pointer",
                "-DCMAKE_CXX_FLAGS=-fsanitize=address,undefined -fno-omit-frame-pointer",
                "-DCMAKE_EXE_LINKER_FLAGS=-fsanitize=address,undefined",
            ],
        )
        sanitize_root = root / "_build-sanitize"
        if sanitize_config.ok:
            capture_artifact(
                "sanitize_cmake_cache", sanitize_root / "CMakeCache.txt"
            )
        sanitize_build = invoke(
            "sanitize-build", ["cmake", "--build", "_build-sanitize", "-j", "8"]
        )
        if sanitize_build.ok and provenance_valid:
            try:
                sanitize_archive = discover_static_archive(sanitize_root)
            except (OSError, ValueError) as error:
                provenance_valid = False
                errors.append(f"sanitize archive discovery failed: {_bounded(error)}")
            if provenance_valid:
                capture_artifact(
                    "sanitize_cachesim", sanitize_root / "bin/cachesim"
                )
                capture_artifact("sanitize_archive", sanitize_archive)
        sanitize_tests = invoke(
            "sanitize-full-tests",
            ["ctest", "--test-dir", "_build-sanitize", "--output-on-failure"],
        )

        binary = release_root / "bin/cachesim"
        binary_sha256 = _executable_hash(binary)
        release_commands_state = _combined_outcome(
            _command_outcome(release_config),
            _command_outcome(release_build),
        )
        build_state = (
            binary_sha256 is not None
            if release_commands_state is True
            else release_commands_state
        )
        full_tests_state = (
            _command_outcome(release_tests) if build_state is True else None
        )
        candidate_test_state = None
        if build_state is True and candidate_test is not None:
            candidate_test_outcome = _command_outcome(candidate_test)
            if candidate_test_outcome is not None:
                candidate_test_state = _candidate_ctest_passed(candidate_test, policy)
        simulator_result_path = stage / "simulator-results.cachesim"
        simulation_argv = [
            str(binary),
            str(trace_path),
            "oracleGeneral",
            policy,
            str(cache_bytes),
            "--num-thread=1",
            f"--num-req={_REQUEST_COUNT}",
            "--warmup-sec=0",
            "--consider-obj-metadata=true",
            "--print-head-req=false",
            f"--output={simulator_result_path}",
        ]
        simulation_one = invoke(
            "determinism-run-1", simulation_argv, command_cwd=stage
        )
        simulation_two = invoke(
            "determinism-run-2", simulation_argv, command_cwd=stage
        )
        deterministic: bool | None = None
        simulation_receipts: list[dict[str, object]] = []
        if build_state is True and simulation_one.ok and simulation_two.ok:
            try:
                parsed_one = parse_cachesim_output(simulation_one.stdout.decode("ascii"))
                parsed_two = parse_cachesim_output(simulation_two.stdout.decode("ascii"))
                scientific_one = (
                    parsed_one.request_count,
                    parsed_one.object_miss_ratio,
                    parsed_one.byte_miss_ratio,
                )
                scientific_two = (
                    parsed_two.request_count,
                    parsed_two.object_miss_ratio,
                    parsed_two.byte_miss_ratio,
                )
                deterministic = (
                    scientific_one == scientific_two
                    and parsed_one.request_count == _REQUEST_COUNT - 1
                )
                for parsed in (parsed_one, parsed_two):
                    simulation_receipts.append(
                        {
                            "request_count": parsed.request_count,
                            "object_miss_ratio": str(parsed.object_miss_ratio),
                            "byte_miss_ratio": str(parsed.byte_miss_ratio),
                            "simulator_throughput_mqps": str(
                                parsed.simulator_throughput_mqps
                            ),
                        }
                    )
            except (UnicodeError, ValueError) as error:
                errors.append(f"determinism parser: {_bounded(error)}")
        simulator_result_receipt: dict[str, object] | None = None
        simulator_result_identity: tuple[int, int] | None = None
        try:
            simulator_result_raw = _regular_bytes(simulator_result_path)
            simulator_result_identity = _regular_identity(simulator_result_path)
            simulator_result_receipt = {
                "path": str(simulator_result_path.relative_to(stage)),
                "size_bytes": len(simulator_result_raw),
                "sha256": hashlib.sha256(simulator_result_raw).hexdigest(),
            }
            if simulator_result_raw != simulation_one.stdout + simulation_two.stdout:
                raise EvaluationError("simulator result file differs from raw stdout")
        except (OSError, ValueError) as error:
            errors.append(f"simulator result binding: {_bounded(error)}")
            deterministic = None
        simulator_result_evidence = _capture_expected_evidence(
            simulator_result_path, stage
        )

        compiler = source["compilers"]["c"]["path"]
        probe_toolchain_clean = True
        probe_include_flags: list[str] = []
        probe_link_flags: list[str] = ["-lm", "-ldl", "-pthread"]
        release_cache_sha256: str | None = None
        if provenance_valid:
            try:
                (
                    probe_include_flags,
                    probe_link_flags,
                    release_cache_sha256,
                ) = probe_build_flags(release_root / "CMakeCache.txt", source)
            except (OSError, ValueError) as error:
                errors.append(f"probe toolchain binding: {_bounded(error)}")
                probe_toolchain_clean = False
        else:
            probe_toolchain_clean = False

        capacity_source = stage / "capacity_probe.c"
        capacity_source_raw = capacity_probe_source(policy)
        _write_private(capacity_source, capacity_source_raw)
        capture_artifact("capacity_probe_source", capacity_source)
        capacity_source_evidence = _capture_expected_evidence(capacity_source, stage)
        capacity_binary = stage / "capacity-probe"
        capacity_compile = invoke(
            "capacity-compile",
            capacity_compile_argv(
                compiler,
                root,
                capacity_binary,
                capacity_source,
                release_archive,
                probe_include_flags,
                probe_link_flags,
            ),
        )
        capacity_binary_receipt, capacity_binary_identity = _capture_executable(
            capacity_binary, stage
        )
        if capacity_compile.ok:
            capture_artifact("capacity_probe_binary", capacity_binary)
        capacity_binary_evidence = _capture_expected_evidence(capacity_binary, stage)
        capacity_run = invoke(
            "capacity-run",
            [str(capacity_binary), str(trace_path), policy, str(cache_bytes)],
            command_cwd=stage,
        )
        capacity: bool | None = None
        capacity_values: dict[str, int | bool] | None = None
        capacity_prerequisites = (
            build_state is True
            and probe_toolchain_clean
            and capacity_compile.ok
            and capacity_binary_identity is not None
        )
        capacity_diagnostic = capacity_run.stdout + capacity_run.stderr
        if capacity_prerequisites and b"capacity violation" in capacity_diagnostic.lower():
            capacity = False
        elif capacity_prerequisites and capacity_run.ok:
            try:
                capacity_values = parse_capacity_probe(capacity_run.stdout.decode("ascii"))
                capacity = capacity_values["cache_size_bytes"] == cache_bytes
            except (UnicodeError, ValueError) as error:
                errors.append(f"capacity probe: {_bounded(error)}")
                if "capacity violation" in str(error):
                    capacity = False

        interposer_source = stage / "allocator_interposer.c"
        interposer_source_raw = allocator_interposer_source()
        _write_private(interposer_source, interposer_source_raw)
        capture_artifact("metadata_interposer_source", interposer_source)
        interposer_source_evidence = _capture_expected_evidence(
            interposer_source, stage
        )
        interposer_binary = stage / "allocator-interposer.so"
        interposer_compile = invoke(
            "metadata-interposer-compile",
            allocator_compile_argv(compiler, interposer_binary, interposer_source),
        )
        interposer_binary_receipt, interposer_binary_identity = _capture_executable(
            interposer_binary, stage
        )
        if interposer_compile.ok:
            capture_artifact("metadata_interposer_binary", interposer_binary)
        interposer_binary_evidence = _capture_expected_evidence(
            interposer_binary, stage
        )

        metadata_source = stage / "metadata_probe.c"
        metadata_source_raw = metadata_probe_source(policy)
        _write_private(metadata_source, metadata_source_raw)
        capture_artifact("metadata_probe_source", metadata_source)
        metadata_source_evidence = _capture_expected_evidence(metadata_source, stage)
        metadata_binary = stage / "metadata-probe"
        metadata_compile = invoke(
            "metadata-compile",
            metadata_compile_argv(
                compiler,
                root,
                metadata_binary,
                metadata_source,
                release_archive,
                probe_include_flags,
                probe_link_flags,
            ),
        )
        metadata_binary_receipt, metadata_binary_identity = _capture_executable(
            metadata_binary, stage
        )
        if metadata_compile.ok:
            capture_artifact("metadata_probe_binary", metadata_binary)
        metadata_binary_evidence = _capture_expected_evidence(metadata_binary, stage)
        metadata_run = invoke(
            "metadata-run",
            metadata_run_argv(
                interposer_binary,
                metadata_binary,
                trace_path,
                policy,
                cache_bytes,
            ),
            command_cwd=stage,
        )
        measured: tuple[Decimal, int] | None = None
        metadata_state: bool | None = None
        metadata_prerequisites = (
            build_state is True
            and probe_toolchain_clean
            and interposer_compile.ok
            and interposer_binary_identity is not None
            and metadata_compile.ok
            and metadata_binary_identity is not None
        )
        if metadata_prerequisites and metadata_run.result is not None:
            try:
                parsed_metadata = parse_metadata_probe(
                    metadata_run.stdout.decode("ascii")
                )
                if metadata_run.result.returncode == 0:
                    measured = parsed_metadata
                    metadata_state = True
            except (UnicodeError, ValueError) as error:
                errors.append(f"metadata probe: {_bounded(error)}")
                if metadata_run.result.returncode == 0 or metadata_run.stdout.strip():
                    metadata_state = False

        expected_evidence = [
            trace_evidence,
            simulator_result_evidence,
            capacity_source_evidence,
            capacity_binary_evidence,
            metadata_source_evidence,
            metadata_binary_evidence,
            interposer_source_evidence,
            interposer_binary_evidence,
            *(
                _capture_expected_evidence(path, stage)
                for path in artifacts.snapshot_paths()
            ),
            *_command_evidence_expectations(commands),
        ]

        final_artifact_mutations = artifacts.revalidate()
        if final_artifact_mutations:
            errors.append(
                "final artifact binding mutation: "
                + ", ".join(final_artifact_mutations)
            )
        artifact_receipts = artifacts.receipt()
        if not artifacts.valid and not final_artifact_mutations:
            errors.append("artifact binding changed during final receipt capture")
        evidence_binding_clean = (
            _revalidate_command_evidence(stage, commands)
            and provenance_valid
            and artifacts.valid
            and not final_artifact_mutations
        )
        if simulator_result_receipt is not None and simulator_result_identity is not None:
            evidence_binding_clean = (
                _refresh_file_record(
                    simulator_result_path,
                    simulator_result_receipt,
                    simulator_result_identity,
                )
                and evidence_binding_clean
            )
        if capacity_binary_identity is not None:
            evidence_binding_clean = (
                _refresh_file_record(
                    capacity_binary,
                    capacity_binary_receipt,
                    capacity_binary_identity,
                )
                and evidence_binding_clean
            )
        if metadata_binary_identity is not None:
            evidence_binding_clean = (
                _refresh_file_record(
                    metadata_binary,
                    metadata_binary_receipt,
                    metadata_binary_identity,
                )
                and evidence_binding_clean
            )
        if interposer_binary_identity is not None:
            evidence_binding_clean = (
                _refresh_file_record(
                    interposer_binary,
                    interposer_binary_receipt,
                    interposer_binary_identity,
                )
                and evidence_binding_clean
            )
        if release_cache_sha256 is not None:
            try:
                evidence_binding_clean = (
                    sha256_file(release_root / "CMakeCache.txt")
                    == release_cache_sha256
                    and evidence_binding_clean
                )
            except OSError:
                evidence_binding_clean = False
        unexpected_stage_entries = _unexpected_stage_entries(
            stage, commands, artifacts.snapshot_paths()
        )
        if unexpected_stage_entries:
            evidence_binding_clean = False
            errors.append("candidate created an unregistered stage entry")
        if not evidence_binding_clean:
            errors.append("retained process or binary evidence changed after capture")

        sanitizer_finding = any(
            _SANITIZER.search(item.stdout) is not None
            or _SANITIZER.search(item.stderr) is not None
            for item in (sanitize_config, sanitize_build, sanitize_tests)
        )
        sanitizer_commands_state = _combined_outcome(
            _command_outcome(sanitize_config),
            _command_outcome(sanitize_build),
            _command_outcome(sanitize_tests),
        )
        sanitizer_state = (
            False
            if sanitizer_finding
            else True
            if sanitizer_commands_state is True
            else None
        )
        checks: dict[str, bool | None] = {
            "source_binding": source_binding_valid,
            "evidence_binding": evidence_binding_clean,
            "build": build_state,
            "full_tests": full_tests_state,
            "candidate_test": candidate_test_state,
            "sanitizer": sanitizer_state,
            "deterministic": deterministic,
            "capacity": capacity,
            "metadata_probe": metadata_state,
        }
        if not probe_toolchain_clean:
            checks["capacity"] = None
            checks["metadata_probe"] = None
            measured = None
        if not evidence_binding_clean:
            _clear_unavailable_checks(checks)
            measured = None
        binary_post_run_sha256 = _executable_hash(binary)
        if binary_post_run_sha256 != binary_sha256:
            errors.append("candidate binary binding mutated during evaluation")
            checks["evidence_binding"] = False
            _clear_unavailable_checks(checks)
            measured = None
        try:
            if _regular_bytes(capacity_source) != capacity_source_raw:
                raise EvaluationError("capacity probe source changed")
        except (OSError, ValueError) as error:
            errors.append(f"capacity probe binding: {_bounded(error)}")
            checks["evidence_binding"] = False
            checks["capacity"] = None
        try:
            if _regular_bytes(metadata_source) != metadata_source_raw:
                raise EvaluationError("metadata probe source changed")
        except (OSError, ValueError) as error:
            errors.append(f"metadata probe binding: {_bounded(error)}")
            checks["evidence_binding"] = False
            checks["metadata_probe"] = None
            measured = None
        apparatus_binding_clean = True
        try:
            if sha256_file(Path(source["_resolved_path"])) != source["_file_sha256"]:
                raise EvaluationError("source receipt changed")
            if any(
                sha256_file(path) != evaluator_hashes[key]
                for key, path in evaluator_paths.items()
            ):
                raise EvaluationError("R0 evaluator apparatus changed")
        except (OSError, ValueError) as error:
            errors.append(f"apparatus binding: {_bounded(error)}")
            apparatus_binding_clean = False
        if not apparatus_binding_clean:
            checks["source_binding"] = False
            _clear_unavailable_checks(checks)
            measured = None
        try:
            observed_trace_sha256 = sha256_file(trace_path)
        except OSError as error:
            observed_trace_sha256 = None
            errors.append(f"synthetic trace revalidation: {_bounded(error)}")
        synthetic["post_run_sha256"] = observed_trace_sha256
        if observed_trace_sha256 != synthetic["sha256"]:
            errors.append("synthetic trace binding mutated during evaluation")
            checks["evidence_binding"] = False
            checks["deterministic"] = None
            checks["capacity"] = None
            checks["metadata_probe"] = None
            measured = None

        cleanup_clean = True
        for path, identity in build_owners.items():
            if not os.path.lexists(path):
                errors.append(f"apparatus build directory disappeared: {path.name}")
                cleanup_clean = False
                continue
            try:
                _cleanup_owned(path, identity)
            except (OSError, ValueError) as error:
                errors.append(_bounded(error))
                cleanup_clean = False
        try:
            _post_binding(root, binding, allowed_roots=(stage,))
        except (OSError, ValueError) as error:
            errors.append(_bounded(error))
            cleanup_clean = False
        if not cleanup_clean:
            checks["source_binding"] = False
            _clear_unavailable_checks(checks)
            measured = None

        evidence_inventory, final_inventory_intact = _evidence_inventory(
            stage, expected_evidence
        )
        if not final_inventory_intact:
            errors.append("expected evidence inventory is missing or changed")
            checks["evidence_binding"] = False
            if any(
                item["identity"] is not None and item["binding_intact"] is False
                for item in evidence_inventory
            ):
                _clear_unavailable_checks(checks)
            measured = None

        measured_receipt = (
            {
                "bytes_per_object": canonical_decimal(measured[0]),
                "global_bytes": measured[1],
                "measurement_sha256": metadata_run.result.stdout_sha256,
                "within_budget": None,
            }
            if measured is not None and metadata_run.result is not None
            else None
        )
        synthetic["path"] = str(trace_path.relative_to(stage))
        synthetic["cache_fraction"] = "0.10"
        synthetic["cache_size_bytes"] = cache_bytes
        for invocation in commands:
            invocation.record["command_sha256"] = record_sha256(
                invocation.record, "command_sha256"
            )
        receipt: dict[str, object] = {
            "schema_version": 1,
            "receipt_version": 1,
            "rung": "r0",
            "source_receipt_path": str(source["_resolved_path"]),
            "source_receipt_sha256": source["receipt_sha256"],
            "source_receipt_file_sha256": source["_file_sha256"],
            "repository_url": SOURCE_LOCK["repository_url"],
            "base_commit": base,
            "base_tree": SOURCE_LOCK["tree"],
            "candidate_commit": candidate,
            "candidate_tree": candidate_tree,
            "candidate_diff_sha256": scope.diff_sha256,
            "changed_path_sha256": changed_path_sha256,
            "policy": policy,
            "policy_source_sha256": policy_source_sha256,
            "candidate_test_sha256": candidate_test_sha256,
            "contract_sha256": contract_sha256,
            "binary": str(binary.relative_to(root)),
            "binary_sha256": binary_sha256,
            "binary_post_run_sha256": binary_post_run_sha256,
            "checks": checks,
            "scope": {**asdict(scope), "changed_paths": list(scope.changed_paths)},
            "declared_metadata": _contract_receipt(contract),
            "measured_metadata": measured_receipt,
            "complexity_audit": "pending_independent_review",
            "synthetic_trace": synthetic,
            "simulations": simulation_receipts,
            "simulator_result": simulator_result_receipt,
            "capacity_measurement": capacity_values,
            "commands": [item.record for item in commands],
            "artifact_snapshots": artifact_receipts,
            "probes": {
                "release_cmake_cache_sha256": release_cache_sha256,
                "include_flags": probe_include_flags,
                "link_flags": probe_link_flags,
                "capacity": {
                    "source_path": str(capacity_source.relative_to(stage)),
                    "source_sha256": hashlib.sha256(capacity_source_raw).hexdigest(),
                    "binary": capacity_binary_receipt,
                },
                "metadata": {
                    "source_path": str(metadata_source.relative_to(stage)),
                    "source_sha256": hashlib.sha256(metadata_source_raw).hexdigest(),
                    "binary": metadata_binary_receipt,
                    "interposer_source_path": str(
                        interposer_source.relative_to(stage)
                    ),
                    "interposer_source_sha256": hashlib.sha256(
                        interposer_source_raw
                    ).hexdigest(),
                    "interposer_binary": interposer_binary_receipt,
                    "accounting_scope": "process_wide_ld_preload",
                },
            },
            "evaluator": evaluator_hashes,
            "host": {
                "platform": platform.platform(),
                "machine": platform.machine(),
                "python": sys.version,
            },
            "timings": {
                "total_wall_ns": max(0, time.monotonic_ns() - started),
                "command_wall_ns": sum(
                    item.result.wall_ns for item in commands if item.result is not None
                ),
                "command_cpu_ns": sum(
                    item.result.cpu_ns for item in commands if item.result is not None
                ),
            },
            "errors": errors,
            "evidence_inventory": evidence_inventory,
            "unexpected_stage_entries": unexpected_stage_entries,
        }
        write_new_record(stage / "receipt.json", receipt, "receipt_sha256")
        _verify_final_stage(stage, expected_evidence, evidence_inventory, receipt)
        _publish_stage(stage, stage_identity, final)
        published = True
        return receipt
    except BaseException:
        for path, identity in build_owners.items():
            if os.path.lexists(path):
                try:
                    _cleanup_owned(path, identity)
                except (OSError, ValueError):
                    pass
        raise
    finally:
        if not published and os.path.lexists(stage):
            _cleanup_owned(stage, stage_identity)
