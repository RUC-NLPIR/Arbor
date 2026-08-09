from __future__ import annotations

import hashlib
import os
import platform
import re
import stat
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .cachesim import run_child
from .calibration_evidence import BoundCalibration, load_bound_calibration
from .constraints import compare_transfer_constraints, validate_calibration
from .evidence import (
    BoundJSONObject,
    _strict_parse_json_bytes,
    read_bound_json_object,
    revalidate_checkout,
)
from .portfolio import (
    Preflight,
    Run,
    _evaluate_temporal_portfolio,
    _preflight,
)
from .portfolio_evidence import FileBinding, file_binding, revalidate_file
from .records import (
    HEX64,
    ParetoMeasurement,
    canonical_bytes,
    record_sha256,
    write_new_record,
)


_HEX40 = re.compile(r"[0-9a-f]{40}\Z")
_POLICY = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}\Z")
_PACKAGE_KEYS = {
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
_REF_FIELDS = (
    "claim_ref",
    "preregistration_ref",
    "review_ref",
    "principal_response_ref",
    "reproduction_ref",
)
_PRIVATE_TRACE_FIELDS = (
    "trace_id",
    "organization",
    "application",
    "dataset",
    "provenance_url",
    "license_ref",
    "path",
    "origin_sha256",
    "sha256",
    "diagnostic_sha256",
)
_PARETO_FIELDS = (
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
)
_FORBIDDEN_OUTCOME_KEYS = {
    "recommendation",
    "score",
    "reward",
    "objective",
    "aggregate",
    "pass",
}


class SealError(ValueError):
    pass


@dataclass(frozen=True)
class ProjectBinding:
    path: Path
    head: str
    tree: str


@dataclass(frozen=True)
class FrozenInputs:
    package: dict[str, object]
    package_binding: FileBinding
    project: ProjectBinding
    ref_sha256s: dict[str, str]
    host_manifest: dict[str, object]
    host_binding: FileBinding
    calibration: BoundCalibration
    preflight: Preflight
    trace_bindings: tuple[FileBinding, ...]
    contract: dict[str, object]
    contract_binding: FileBinding
    r3_evaluator_bindings: dict[str, FileBinding]
    ledger: Path
    output: Path


def _hash(value: object, label: str, *, sha1: bool = False) -> str:
    pattern = _HEX40 if sha1 else HEX64
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise SealError(f"{label} must be a lowercase cryptographic hash")
    return value


def _string(value: object, label: str) -> str:
    if type(value) is not str or not value or "\x00" in value:
        raise SealError(f"{label} must be a nonempty string")
    return value


def load_frozen_package(value: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != _PACKAGE_KEYS:
        raise SealError("frozen package keys mismatch")
    if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise SealError("frozen package schema_version must be integer 1")
    project = Path(_string(value.get("project"), "project"))
    if not project.is_absolute():
        raise SealError("project must be an absolute task root")
    _hash(value.get("frozen_commit"), "frozen commit", sha1=True)
    _hash(value.get("candidate_commit"), "candidate commit", sha1=True)
    policy = _string(value.get("policy"), "policy")
    if _POLICY.fullmatch(policy) is None:
        raise SealError("policy is not a safe identifier")
    for name in (
        "candidate_diff_sha256",
        "policy_contract_sha256",
        "r0_receipt_sha256",
        "r2_receipt_sha256",
        "calibration_sha256",
        "r3_commitment_sha256",
    ):
        _hash(value.get(name), name.replace("_", " "))
    for name in _REF_FIELDS:
        raw = _string(value.get(name), name)
        reference = PurePosixPath(raw)
        if (
            reference.is_absolute()
            or ".." in reference.parts
            or raw != reference.as_posix()
        ):
            raise SealError(f"{name} must be a safe relative Git reference")
    return dict(value)


def _binding(bound: BoundJSONObject) -> FileBinding:
    return FileBinding(
        bound.path,
        bound.identity,
        bound.size_bytes,
        bound.sha256,
        bound.mode,
    )


def _git(project: Path, *argv: str, binary: bool = False) -> str | bytes:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    environment["GIT_NO_LAZY_FETCH"] = "1"
    result = subprocess.run(
        [
            "git",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.untrackedCache=false",
            "-c",
            "core.fileMode=true",
            "-c",
            "core.trustctime=true",
            *argv,
        ],
        cwd=project,
        capture_output=True,
        check=False,
        env=environment,
    )
    if result.returncode != 0:
        raise SealError(f"Git validation failed: {' '.join(argv)}")
    if binary:
        return result.stdout
    try:
        return result.stdout.decode("utf-8").strip()
    except UnicodeError as error:
        raise SealError("Git validation returned non-UTF-8 text") from error


def _project_binding(project_value: str | Path, frozen_commit: str) -> ProjectBinding:
    raw = Path(project_value)
    try:
        metadata = raw.lstat()
        project = raw.resolve(strict=True)
    except OSError as error:
        raise SealError("project task root is unavailable") from error
    if (
        raw.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or str(project) != str(raw)
    ):
        raise SealError("project must name its canonical real task root")
    top = _git(project, "rev-parse", "--show-toplevel")
    head = _git(project, "rev-parse", "HEAD")
    tree = _git(project, "rev-parse", "HEAD^{tree}")
    status = _git(
        project,
        "status",
        "--porcelain=v2",
        "--untracked-files=all",
        "--ignore-submodules=none",
    )
    if top != str(project):
        raise SealError("project must be the Git task root")
    if head != frozen_commit:
        raise SealError("project HEAD differs from frozen_commit")
    if status:
        raise SealError("project Git worktree is not clean")
    return ProjectBinding(project, str(head), str(tree))


def _git_refs(
    project: ProjectBinding,
    package: Mapping[str, object],
) -> tuple[dict[str, bytes], dict[str, str]]:
    raw_by_field: dict[str, bytes] = {}
    hashes: dict[str, str] = {}
    for field in _REF_FIELDS:
        reference = str(package[field])
        listing = _git(
            project.path,
            "ls-tree",
            "-z",
            project.head,
            "--",
            reference,
            binary=True,
        )
        assert isinstance(listing, bytes)
        records = [item for item in listing.split(b"\0") if item]
        if len(records) != 1:
            raise SealError(f"{field} is not present at frozen_commit")
        try:
            metadata, raw_path = records[0].split(b"\t", 1)
            mode, kind, oid = metadata.split(b" ", 2)
        except ValueError as error:
            raise SealError(f"{field} Git entry is malformed") from error
        if (
            mode not in {b"100644", b"100755"}
            or kind != b"blob"
            or raw_path != os.fsencode(reference)
        ):
            raise SealError(f"{field} must be a regular Git blob")
        raw = _git(project.path, "cat-file", "blob", oid.decode("ascii"), binary=True)
        assert isinstance(raw, bytes)
        raw_by_field[field] = raw
        hashes[field] = hashlib.sha256(raw).hexdigest()
    return raw_by_field, hashes


def _contains_hash(value: object, field: str, expected: str) -> bool:
    if isinstance(value, Mapping):
        if value.get(field) == expected:
            return True
        return any(_contains_hash(item, field, expected) for item in value.values())
    if isinstance(value, list):
        return any(_contains_hash(item, field, expected) for item in value)
    return False


def _validate_reproduction(raw: bytes, expected_r2: str) -> Mapping[str, object]:
    try:
        value = _strict_parse_json_bytes(raw, decimal_numbers=False)
    except (UnicodeError, TypeError, ValueError) as error:
        raise SealError("reproduction_ref must be a JSON Git blob") from error
    if not isinstance(value, dict):
        raise SealError("reproduction_ref must contain a JSON object")
    full_r2 = value.get("rung") == "r2" and value.get("receipt_sha256") == expected_r2
    if full_r2 and record_sha256(value, "receipt_sha256") != expected_r2:
        raise SealError("reproduction R2 receipt self-hash mismatch")
    if not full_r2 and not _contains_hash(value, "r2_receipt_sha256", expected_r2):
        raise SealError("reproduction_ref does not bind the exact R2 receipt")
    return value


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _external_input(path: Path, project: Path, label: str) -> None:
    if _paths_overlap(path, project):
        raise SealError(f"{label} must be outside task_root")


def _new_path(path: Path, project: Path, label: str) -> Path:
    absolute = Path(path).absolute()
    if os.path.lexists(absolute):
        qualifier = "already consumed" if label == "ledger" else "must not preexist"
        raise SealError(f"{label} {qualifier}")
    try:
        parent_metadata = absolute.parent.lstat()
        parent = absolute.parent.resolve(strict=True)
    except OSError as error:
        raise SealError(f"{label} parent is unavailable") from error
    if absolute.parent.is_symlink() or not stat.S_ISDIR(parent_metadata.st_mode):
        raise SealError(f"{label} parent must be a real directory")
    resolved = parent / absolute.name
    _external_input(resolved, project, label)
    return resolved


def _private_identities(manifest: Mapping[str, object]) -> tuple[bytes, ...]:
    traces = manifest.get("traces")
    if not isinstance(traces, list) or not traces:
        raise SealError("host R3 manifest contains no traces")
    identities: set[bytes] = set()
    for trace in traces:
        if not isinstance(trace, dict):
            raise SealError("host R3 trace record is invalid")
        for field in _PRIVATE_TRACE_FIELDS:
            value = trace.get(field)
            if type(value) is not str or not value:
                raise SealError(f"host R3 trace {field} is invalid")
            identities.add(value.encode("utf-8"))
    return tuple(sorted(identities))


def _find_leak(raw: bytes, identities: tuple[bytes, ...]) -> bool:
    return any(identity in raw for identity in identities)


def _scan_task(
    project: ProjectBinding,
    package_raw: bytes,
    identities: tuple[bytes, ...],
) -> None:
    if _find_leak(package_raw, identities):
        raise SealError("frozen package leaks an R3 identity or path")
    for root, directories, files in os.walk(project.path, followlinks=False):
        directories[:] = [name for name in directories if name != ".git"]
        root_path = Path(root)
        for name in [*directories, *files]:
            relative = (root_path / name).relative_to(project.path)
            if _find_leak(os.fsencode(relative), identities):
                raise SealError("task tree leaks an R3 identity or path")
        for name in files:
            path = root_path / name
            try:
                metadata = path.lstat()
                if stat.S_ISLNK(metadata.st_mode):
                    raw = os.fsencode(os.readlink(path))
                elif stat.S_ISREG(metadata.st_mode):
                    raw = path.read_bytes()
                else:
                    continue
            except OSError as error:
                raise SealError("task tree changed during R3 leak scan") from error
            if _find_leak(raw, identities):
                raise SealError("task tree leaks an R3 identity or path")
    listing = _git(project.path, "ls-tree", "-r", "-z", project.head, binary=True)
    assert isinstance(listing, bytes)
    for record in (item for item in listing.split(b"\0") if item):
        try:
            metadata, raw_path = record.split(b"\t", 1)
            mode, kind, oid = metadata.split(b" ", 2)
        except ValueError as error:
            raise SealError("frozen Git tree entry is malformed") from error
        if _find_leak(raw_path, identities):
            raise SealError("frozen Git path leaks an R3 identity or path")
        if kind != b"blob" or mode not in {b"100644", b"100755", b"120000"}:
            continue
        raw = _git(project.path, "cat-file", "blob", oid.decode("ascii"), binary=True)
        assert isinstance(raw, bytes)
        if _find_leak(raw, identities):
            raise SealError("frozen Git blob leaks an R3 identity or path")


def _scientific_hashes(preflight: Preflight) -> dict[str, str]:
    artifacts = preflight.artifact_bindings
    result = {
        "fixed_time_interposer": artifacts[
            "validated_fixed_time_interposer_binary"
        ].sha256,
        "release_archive": artifacts["release_archive"].sha256,
        "release_cmake_cache": artifacts["release_cmake_cache"].sha256,
    }
    result.update(
        {
            f"header:{relative}": digest
            for relative, _mode, digest in preflight.checkout_binding.tracked
            if relative.startswith("libCacheSim/include/")
            or relative == "libCacheSim/bin/cachesim/cache_init.h"
        }
    )
    return dict(sorted(result.items()))


def _validate_calibration(
    bound: BoundCalibration,
    preflight: Preflight,
    reproduction: Mapping[str, object],
) -> None:
    validated = validate_calibration(bound.record)
    del validated
    record = bound.record
    evaluator = {
        name: binding.sha256
        for name, binding in sorted(preflight.evaluator_bindings.items())
    }
    host = {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": sys.version,
    }
    if (
        record.get("source_receipt_sha256") != preflight.source.get("receipt_sha256")
        or record.get("source_commit") != preflight.manifest.get("source_commit")
        or record.get("binary_sha256")
        != preflight.artifact_bindings["release_cachesim"].sha256
        or record.get("evaluator_sha256s") != evaluator
        or record.get("scientific_input_sha256s") != _scientific_hashes(preflight)
        or record.get("host_fingerprint") != host
    ):
        raise SealError("calibration differs from the exact R3 apparatus")
    if "task_manifest_sha256" in reproduction and (
        reproduction["task_manifest_sha256"] != record.get("task_manifest_sha256")
    ):
        raise SealError("R2 reproduction and calibration manifest bindings differ")


def _prevalidate(
    frozen_package: Path,
    host_r3_manifest: Path,
    calibration: Path,
    calibration_sha256: str,
    source_receipt: Path,
    candidate_r0_receipt: Path,
    checkout: Path,
    ledger: Path,
    output: Path,
) -> FrozenInputs:
    try:
        package_bound = read_bound_json_object(frozen_package, max_bytes=1024 * 1024)
        package = load_frozen_package(package_bound.value)
        package_binding = _binding(package_bound)
        project = _project_binding(package["project"], str(package["frozen_commit"]))
        raw_refs, ref_sha256s = _git_refs(project, package)
        reproduction = _validate_reproduction(
            raw_refs["reproduction_ref"], str(package["r2_receipt_sha256"])
        )
        host_bound = read_bound_json_object(
            host_r3_manifest, max_bytes=64 * 1024 * 1024
        )
        host_binding = _binding(host_bound)
        host = host_bound.value
        if set(host) != {
            "schema_version",
            "source_commit",
            "cache_fractions",
            "traces",
            "manifest_sha256",
        }:
            raise SealError("host R3 manifest keys mismatch")
        if type(host.get("schema_version")) is not int or host["schema_version"] != 1:
            raise SealError("host R3 manifest schema_version must be integer 1")
        host_hash = record_sha256(host, "manifest_sha256")
        if (
            host.get("manifest_sha256") != host_hash
            or host_hash != package["r3_commitment_sha256"]
        ):
            raise SealError("host R3 manifest differs from the frozen commitment")
        _external_input(host_binding.path, project.path, "host R3 manifest")
        calibration_expected = _hash(calibration_sha256, "expected calibration")
        if calibration_expected != package["calibration_sha256"]:
            raise SealError("external calibration digest differs from frozen package")
        bound_calibration = load_bound_calibration(
            calibration, expected_calibration_sha256=calibration_expected
        )
        _external_input(bound_calibration.path, project.path, "calibration")
        ledger_path = _new_path(ledger, project.path, "ledger")
        output_path = _new_path(output, project.path, "output")
        if _paths_overlap(ledger_path, output_path):
            raise SealError("ledger and output paths must not overlap")
        preflight = _preflight(
            task_root=project.path,
            task_manifest=host_binding.path,
            checkout=checkout,
            candidate=str(package["candidate_commit"]),
            policy=str(package["policy"]),
            source_receipt=source_receipt,
            r0_receipt=candidate_r0_receipt,
            output=output_path,
            temporal_r3=True,
        )
        os.close(preflight.output_parent.descriptor)
        _external_input(preflight.source_binding.path, project.path, "source receipt")
        _external_input(preflight.r0_binding.path, project.path, "candidate R0 receipt")
        _external_input(preflight.r0_root, project.path, "candidate R0 evidence")
        if (
            type(preflight.source.get("schema_version")) is not int
            or type(preflight.r0.get("schema_version")) is not int
            or type(preflight.r0.get("receipt_version")) is not int
            or preflight.r0.get("receipt_sha256") != package["r0_receipt_sha256"]
            or preflight.r0.get("candidate_diff_sha256")
            != package["candidate_diff_sha256"]
            or preflight.r0.get("contract_sha256") != package["policy_contract_sha256"]
        ):
            raise SealError("candidate R0, diff, or contract binding mismatch")
        contract_path = preflight.checkout / "commissioning/cache_policy_contract.json"
        contract_bound = read_bound_json_object(contract_path, max_bytes=65_536)
        contract_binding = _binding(contract_bound)
        if contract_binding.sha256 != package["policy_contract_sha256"]:
            raise SealError("policy contract bytes differ from frozen package")
        trace_bindings: list[FileBinding] = []
        for trace in preflight.traces:
            trace_binding = file_binding(trace.path)
            _external_input(trace_binding.path, project.path, "R3 trace")
            if trace_binding.sha256 != trace.record.get(
                "sha256"
            ) or trace_binding.size_bytes != trace.record.get("size_bytes"):
                raise SealError("R3 trace bytes differ from host manifest")
            trace_bindings.append(trace_binding)
        protected_paths = (
            host_binding.path,
            bound_calibration.path,
            preflight.source_binding.path,
            preflight.r0_root,
            preflight.checkout,
            *(binding.path for binding in trace_bindings),
        )
        if any(
            _paths_overlap(new_path, protected)
            for new_path in (ledger_path, output_path)
            for protected in protected_paths
        ):
            raise SealError("ledger or output overlaps an immutable R3 input")
        identities = _private_identities(host)
        _scan_task(project, package_bound.raw, identities)
        _validate_calibration(bound_calibration, preflight, reproduction)
        r3_evaluator_bindings = {
            "seal_sha256": file_binding(Path(__file__)),
            "constraints_sha256": file_binding(
                Path(__file__).with_name("constraints.py")
            ),
            "calibration_evidence_sha256": file_binding(
                Path(__file__).with_name("calibration_evidence.py")
            ),
            "run_aros_cache_r3_sha256": file_binding(
                Path(__file__).parents[2] / "scripts/run_aros_cache_r3.py"
            ),
        }
        revalidate_file(package_binding)
        revalidate_file(host_binding)
        revalidate_file(contract_binding)
        revalidate_file(preflight.source_binding)
        revalidate_file(preflight.r0_binding)
        observed_calibration = file_binding(
            bound_calibration.path, expected_mode=bound_calibration.mode
        )
        if (
            observed_calibration.identity != bound_calibration.identity
            or observed_calibration.size_bytes != bound_calibration.size_bytes
            or observed_calibration.sha256 != bound_calibration.file_sha256
        ):
            raise SealError("calibration path binding changed")
        for binding in [
            *preflight.artifact_bindings.values(),
            *preflight.evaluator_bindings.values(),
            *trace_bindings,
            *r3_evaluator_bindings.values(),
        ]:
            revalidate_file(binding)
        revalidate_checkout(preflight.checkout, preflight.checkout_binding)
        _project_binding(project.path, project.head)
        return FrozenInputs(
            package,
            package_binding,
            project,
            ref_sha256s,
            host,
            host_binding,
            bound_calibration,
            preflight,
            tuple(trace_bindings),
            contract_bound.value,
            contract_binding,
            r3_evaluator_bindings,
            ledger_path,
            output_path,
        )
    except SealError:
        raise
    except (OSError, TypeError, ValueError, subprocess.SubprocessError) as error:
        raise SealError(str(error)) from error


def _fsync_parent(path: Path) -> None:
    descriptor = os.open(
        path.parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _consume_ledger(
    path: Path, record: dict[str, object]
) -> tuple[dict[str, object], str]:
    record["ledger_sha256"] = record_sha256(record, "ledger_sha256")
    raw = canonical_bytes(record) + b"\n"
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except FileExistsError as error:
        raise SealError("R3 ledger is already consumed") from error
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    _fsync_parent(path)
    observed = file_binding(path, expected_mode=0o600)
    file_sha256 = hashlib.sha256(raw).hexdigest()
    if observed.size_bytes != len(raw) or observed.sha256 != file_sha256:
        raise SealError("consumed ledger changed during durable publication")
    return record, file_sha256


def _ledger_record(inputs: FrozenInputs) -> dict[str, object]:
    package = inputs.package
    return {
        "schema_version": 1,
        "state": "consumed",
        "requested_at_unix_ns": time.time_ns(),
        "frozen_package_file_sha256": inputs.package_binding.sha256,
        "frozen_commit": package["frozen_commit"],
        "frozen_tree": inputs.project.tree,
        "candidate_commit": package["candidate_commit"],
        "candidate_tree": inputs.preflight.checkout_binding.tree,
        "policy": package["policy"],
        "candidate_diff_sha256": package["candidate_diff_sha256"],
        "policy_contract_sha256": package["policy_contract_sha256"],
        "git_ref_sha256s": inputs.ref_sha256s,
        "host_r3_manifest_sha256": inputs.host_binding.sha256,
        "r3_commitment_sha256": package["r3_commitment_sha256"],
        "calibration_sha256": package["calibration_sha256"],
        "calibration_file_sha256": inputs.calibration.file_sha256,
        "source_receipt_sha256": inputs.preflight.source["receipt_sha256"],
        "source_receipt_file_sha256": inputs.preflight.source_binding.sha256,
        "candidate_r0_receipt_sha256": package["r0_receipt_sha256"],
        "candidate_r0_file_sha256": inputs.preflight.r0_binding.sha256,
        "r2_receipt_sha256": package["r2_receipt_sha256"],
        "binary_sha256": inputs.preflight.artifact_bindings["release_cachesim"].sha256,
        "r3_evaluator_sha256s": {
            name: binding.sha256
            for name, binding in sorted(inputs.r3_evaluator_bindings.items())
        },
        "portfolio_evaluator_sha256s": {
            name: binding.sha256
            for name, binding in sorted(inputs.preflight.evaluator_bindings.items())
        },
        "trace_sha256s": [binding.sha256 for binding in inputs.trace_bindings],
    }


def _assert_factual(value: object) -> None:
    if isinstance(value, Mapping):
        forbidden = set(value) & _FORBIDDEN_OUTCOME_KEYS
        if forbidden:
            raise SealError(
                f"R3 receipt contains forbidden outcome keys: {sorted(forbidden)}"
            )
        for item in value.values():
            _assert_factual(item)
    elif isinstance(value, list):
        for item in value:
            _assert_factual(item)


def _measurement_facts(
    output: Path,
    receipt: Mapping[str, object],
    inputs: FrozenInputs,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    raw_measurements = receipt.get("measurements")
    if not isinstance(raw_measurements, list):
        raise SealError("R3 portfolio measurements are invalid")
    facts: list[dict[str, object]] = []
    constraints: list[dict[str, object]] = []
    for summary in raw_measurements:
        if not isinstance(summary, dict) or type(summary.get("path")) is not str:
            raise SealError("R3 measurement summary is invalid")
        measurement_path = output / "evidence" / str(summary["path"])
        bound = read_bound_json_object(measurement_path, max_bytes=64 * 1024 * 1024)
        measurement = bound.value
        digest = record_sha256(measurement, "measurement_sha256")
        if (
            digest != summary.get("measurement_sha256")
            or measurement.get("measurement_sha256") != digest
        ):
            raise SealError("R3 measurement self-hash mismatch")
        pareto = ParetoMeasurement.from_record(
            {name: measurement[name] for name in _PARETO_FIELDS}
        ).to_record()
        compared = compare_transfer_constraints(
            measurement,
            inputs.preflight.r0,
            inputs.contract,
            inputs.calibration.path,
            inputs.calibration.calibration_sha256,
            None,
        )
        facts.append(
            {
                "cell_index": summary["index"],
                "path": str(Path("evidence") / str(summary["path"])),
                "measurement_sha256": digest,
                "pareto": pareto,
            }
        )
        constraints.append(
            {
                "cell_index": summary["index"],
                "measurement_sha256": digest,
                "facts": compared,
            }
        )
    return facts, constraints


def _write_final_receipt(
    inputs: FrozenInputs,
    ledger: Mapping[str, object],
    ledger_file_sha256: str,
    *,
    started_at: int,
    state: str,
    portfolio_receipt: Mapping[str, object] | None,
    measurements: list[dict[str, object]],
    failures: list[dict[str, object]],
    constraints: list[dict[str, object]],
) -> dict[str, object]:
    ended_at = time.time_ns()
    receipt: dict[str, object] = {
        "schema_version": 1,
        "receipt_version": 1,
        "rung": "r3",
        "state": state,
        "frozen_commit": inputs.package["frozen_commit"],
        "candidate_commit": inputs.package["candidate_commit"],
        "candidate_tree": inputs.preflight.checkout_binding.tree,
        "policy": inputs.package["policy"],
        "ledger_path": str(inputs.ledger),
        "ledger_sha256": ledger["ledger_sha256"],
        "ledger_file_sha256": ledger_file_sha256,
        "r3_commitment_sha256": inputs.package["r3_commitment_sha256"],
        "host_r3_manifest_sha256": inputs.host_binding.sha256,
        "calibration_sha256": inputs.calibration.calibration_sha256,
        "r3_evaluator_sha256s": {
            name: binding.sha256
            for name, binding in sorted(inputs.r3_evaluator_bindings.items())
        },
        "source_receipt_sha256": inputs.preflight.source["receipt_sha256"],
        "r0_receipt_sha256": inputs.package["r0_receipt_sha256"],
        "r2_receipt_sha256": inputs.package["r2_receipt_sha256"],
        "started_at_unix_ns": started_at,
        "ended_at_unix_ns": ended_at,
        "portfolio_receipt_path": (
            "evidence/receipt.json" if portfolio_receipt is not None else None
        ),
        "portfolio_receipt_sha256": (
            portfolio_receipt.get("receipt_sha256")
            if portfolio_receipt is not None
            else None
        ),
        "measurements": measurements,
        "failures": failures,
        "constraints": constraints,
    }
    _assert_factual(receipt)
    write_new_record(inputs.output / "receipt.json", receipt, "receipt_sha256")
    return receipt


def run_r3(
    frozen_package: Path,
    host_r3_manifest: Path,
    calibration: Path,
    calibration_sha256: str,
    source_receipt: Path,
    candidate_r0_receipt: Path,
    checkout: Path,
    ledger: Path,
    output: Path,
    *,
    runner: Run = run_child,
) -> dict[str, object]:
    inputs = _prevalidate(
        frozen_package,
        host_r3_manifest,
        calibration,
        calibration_sha256,
        source_receipt,
        candidate_r0_receipt,
        checkout,
        ledger,
        output,
    )
    ledger_record, ledger_file_sha256 = _consume_ledger(
        inputs.ledger, _ledger_record(inputs)
    )
    started_at = time.time_ns()
    inputs.output.mkdir(mode=0o700)
    _fsync_parent(inputs.output)
    portfolio_receipt: dict[str, object] | None = None
    measurements: list[dict[str, object]] = []
    constraints: list[dict[str, object]] = []
    failures: list[dict[str, object]] = []
    state = "process_failed"
    try:
        portfolio_receipt = _evaluate_temporal_portfolio(
            task_root=inputs.project.path,
            host_manifest=inputs.host_binding.path,
            checkout=inputs.preflight.checkout,
            candidate=str(inputs.package["candidate_commit"]),
            policy=str(inputs.package["policy"]),
            source_receipt=inputs.preflight.source_binding.path,
            r0_receipt=inputs.preflight.r0_binding.path,
            output=inputs.output / "evidence",
            run=runner,
        )
        raw_failures = portfolio_receipt.get("failures")
        if not isinstance(raw_failures, list):
            raise SealError("R3 portfolio failure list is invalid")
        failures = [dict(item) for item in raw_failures if isinstance(item, dict)]
        if len(failures) != len(raw_failures):
            raise SealError("R3 portfolio failure entry is invalid")
        measurements, constraints = _measurement_facts(
            inputs.output, portfolio_receipt, inputs
        )
        expected_cells = inputs.host_manifest.get("traces")
        assert isinstance(expected_cells, list)
        state = (
            "measured"
            if not failures and len(measurements) == len(expected_cells) * 3
            else "process_failed"
        )
    except (
        OSError,
        TypeError,
        ValueError,
        RuntimeError,
        subprocess.SubprocessError,
    ) as error:
        failures.append(
            {
                "kind": "evaluation_failure",
                "state": "process_failed",
                "error": " ".join(str(error).split())[:512],
            }
        )
    _project_binding(inputs.project.path, inputs.project.head)
    receipt = _write_final_receipt(
        inputs,
        ledger_record,
        ledger_file_sha256,
        started_at=started_at,
        state=state,
        portfolio_receipt=portfolio_receipt,
        measurements=measurements,
        failures=failures,
        constraints=constraints,
    )
    _project_binding(inputs.project.path, inputs.project.head)
    return receipt
