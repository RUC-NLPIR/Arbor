from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Literal

from .records import ContractError


_POLICY = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}\Z")
_COMPARISON_POLICIES = {
    "LRU",
    "ARC",
    "WTinyLFU",
    "Sieve",
    "S3FIFO",
    "BeladySize",
}
_REFERENCE_POLICIES = {"Sieve", "S3FIFO"}
_CONTRACT_PATH = "commissioning/cache_policy_contract.json"
_CONTRACT_KEYS = {
    "schema_version",
    "policy",
    "reference_policy",
    "policy_source",
    "object_metadata_bytes",
    "global_metadata_bytes",
    "global_metadata_evidence",
    "update_complexity",
}
_EVIDENCE_KEYS = {"source", "line", "expression"}
_WIRING_PATHS = {
    "libCacheSim/include/libCacheSim/evictionAlgo.h",
    "libCacheSim/cache/CMakeLists.txt",
    "libCacheSim/bin/cachesim/cache_init.h",
    "test/CMakeLists.txt",
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


@dataclass(frozen=True)
class ScopeFacts:
    allowed_paths: bool
    baseline_unchanged: bool
    additive_wiring_only: bool
    contract_bound: bool | None
    changed_paths: tuple[str, ...]
    diff_sha256: str


@dataclass(frozen=True)
class ConstraintFacts:
    measured_metadata_bytes_per_object: Decimal | None
    measured_global_metadata_bytes: int | None
    metadata_measurement_sha256: str | None
    metadata_within_budget: bool | None
    complexity_audit: Literal[
        "pending_independent_review", "accepted", "rejected"
    ]
    capacity_conserved: bool | None
    deterministic: bool | None
    sanitizer_clean: bool | None


@dataclass(frozen=True)
class PolicyContract:
    schema_version: int
    policy: str
    reference_policy: str
    policy_source: str
    object_metadata_bytes: int
    global_metadata_bytes: int
    global_metadata_evidence: tuple[tuple[str, int, str], ...]
    update_complexity: str


def _unique_object(items: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in items:
        if key in value:
            raise ContractError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _invalid_constant(value: str) -> object:
    raise ContractError(f"non-finite JSON constant is forbidden: {value}")


def _finite_decimal(value: str) -> Decimal:
    parsed = Decimal(value)
    if not parsed.is_finite():
        raise ContractError(f"non-finite JSON number is forbidden: {value}")
    return parsed


def _contract_object(raw: bytes, label: str) -> dict[str, object]:
    if len(raw) > 65_536:
        raise ContractError(f"policy contract is too large: {label}")
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=_invalid_constant,
            parse_float=_finite_decimal,
        )
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ContractError(f"invalid policy contract JSON: {label}") from error
    if not isinstance(value, dict):
        raise ContractError(f"policy contract must be an object: {label}")
    return value


def _exact_keys(value: Mapping[str, object], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise ContractError(
            f"{label} keys mismatch: missing={sorted(expected - set(value))}, "
            f"unknown={sorted(set(value) - expected)}"
        )


def _policy_identifier(value: object, label: str) -> str:
    if type(value) is not str or _POLICY.fullmatch(value) is None:
        raise ContractError(f"{label} must be a safe policy identifier")
    return value


def _metadata_integer(value: object, label: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ContractError(f"{label} must be an integer >= {minimum}")
    return value


def _validate_contract(
    candidate: Mapping[str, object], *, expected_policy: str
) -> PolicyContract:
    policy = _policy_identifier(expected_policy, "expected policy")
    _exact_keys(candidate, _CONTRACT_KEYS, "policy contract")
    if type(candidate["schema_version"]) is not int or candidate["schema_version"] != 1:
        raise ContractError("policy contract schema_version must be integer 1")
    if candidate["policy"] != policy:
        raise ContractError("policy contract policy does not match the candidate policy")
    reference = candidate["reference_policy"]
    if type(reference) is not str or reference not in _REFERENCE_POLICIES:
        raise ContractError("reference_policy must be Sieve or S3FIFO")
    source = f"libCacheSim/cache/eviction/{policy}.c"
    if candidate["policy_source"] != source:
        raise ContractError("policy_source does not exactly match the candidate policy")
    object_bytes = _metadata_integer(
        candidate["object_metadata_bytes"], "object_metadata_bytes"
    )
    global_bytes = _metadata_integer(
        candidate["global_metadata_bytes"], "global_metadata_bytes"
    )
    raw_evidence = candidate["global_metadata_evidence"]
    if not isinstance(raw_evidence, list) or not raw_evidence:
        raise ContractError("global_metadata_evidence must be a nonempty array")
    evidence: list[tuple[str, int, str]] = []
    for index, item in enumerate(raw_evidence):
        if not isinstance(item, dict):
            raise ContractError(f"global_metadata_evidence[{index}] must be an object")
        _exact_keys(item, _EVIDENCE_KEYS, f"global_metadata_evidence[{index}]")
        if item["source"] != source:
            raise ContractError("metadata evidence must be inside the candidate source")
        line = _metadata_integer(item["line"], "metadata evidence line", minimum=1)
        expression = item["expression"]
        if (
            type(expression) is not str
            or not expression.strip()
            or len(expression) > 256
            or any(not 0x20 <= ord(character) <= 0x7E for character in expression)
        ):
            raise ContractError(
                "metadata evidence expression must be nonempty bounded printable ASCII"
            )
        evidence.append((source, line, expression))
    if candidate["update_complexity"] != "amortized O(1)":
        raise ContractError("update_complexity must be exactly amortized O(1)")
    return PolicyContract(
        schema_version=1,
        policy=policy,
        reference_policy=reference,
        policy_source=source,
        object_metadata_bytes=object_bytes,
        global_metadata_bytes=global_bytes,
        global_metadata_evidence=tuple(evidence),
        update_complexity="amortized O(1)",
    )


def load_policy_contract(path: Path, *, expected_policy: str) -> PolicyContract:
    candidate = Path(path)
    try:
        metadata = candidate.lstat()
        if candidate.is_symlink() or not candidate.is_file():
            raise ContractError("policy contract must be a regular non-symlink file")
        if metadata.st_size > 65_536:
            raise ContractError("policy contract is too large")
        descriptor = os.open(candidate, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = -1
                raw = stream.read(65_537)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    except ContractError:
        raise
    except OSError as error:
        raise ContractError(f"cannot read policy contract: {candidate}") from error
    return _validate_contract(
        _contract_object(raw, str(candidate)), expected_policy=expected_policy
    )


def _git_bytes(checkout: Path, *argv: str) -> bytes:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    environment["GIT_NO_LAZY_FETCH"] = "1"
    result = subprocess.run(
        ["git", *_GIT_CONFIG, *argv],
        cwd=checkout,
        capture_output=True,
        check=False,
        env=environment,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace")
        raise ContractError(
            f"Git scope command failed ({' '.join(argv)}): "
            f"{' '.join(stderr.split())[:300]}"
        )
    return result.stdout


def _name_status(raw: bytes) -> list[tuple[str, str]]:
    try:
        text = raw.decode("utf-8")
    except UnicodeError as error:
        raise ContractError("scope diff paths must be UTF-8") from error
    entries: list[tuple[str, str]] = []
    for line in text.splitlines():
        fields = line.split("\t")
        if len(fields) != 2 or not fields[0] or not fields[1]:
            raise ContractError("malformed Git name-status output")
        status, path = fields
        if (
            status not in {"A", "M", "D", "T"}
            or path.startswith("/")
            or ".." in Path(path).parts
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in path)
        ):
            raise ContractError("unsafe Git name-status entry")
        entries.append((status, path))
    if len({path for _status, path in entries}) != len(entries):
        raise ContractError("duplicate path in Git name-status output")
    return entries


def _added_wiring_line_is_necessary(path: str, line: str, policy: str) -> bool:
    stripped = line.strip()
    if (
        not stripped
        or len(stripped) > 512
        or policy not in stripped
        or stripped.startswith(("#", "//", "/*", "message("))
    ):
        return False
    if path.endswith("evictionAlgo.h"):
        return re.fullmatch(
            rf"cache_t\s*\*\s*{re.escape(policy)}_init\s*\([^;]{{1,400}}\);",
            stripped,
        ) is not None
    if path == "libCacheSim/cache/CMakeLists.txt":
        return stripped == f"eviction/{policy}.c"
    if path.endswith("cache_init.h"):
        return re.fullmatch(
            rf'\{{\s*"{re.escape(policy)}"\s*,\s*{re.escape(policy)}_init\s*\}},?',
            stripped,
        ) is not None
    if path == "test/CMakeLists.txt":
        escaped = re.escape(policy)
        return re.fullmatch(
            rf"add_test_executable\(test_{escaped}\s+test_{escaped}\.c\)",
            stripped,
        ) is not None or re.fullmatch(
            rf"add_test\(NAME\s+test_{escaped}\s+COMMAND\s+test_{escaped}\s+"
            rf"WORKING_DIRECTORY\s+\.\)",
            stripped,
        ) is not None
    return False


def _wiring_is_additive(checkout: Path, base: str, candidate: str, path: str, policy: str) -> bool:
    raw = _git_bytes(
        checkout,
        "diff",
        "--unified=0",
        f"{base}..{candidate}",
        "--",
        path,
    )
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeError:
        return False
    additions = 0
    for line in lines:
        if line.startswith("---") or line.startswith("+++"):
            continue
        if line.startswith("-"):
            return False
        if line.startswith("+"):
            additions += 1
            if not _added_wiring_line_is_necessary(path, line[1:], policy):
                return False
    return additions > 0


def _regular_candidate_blob(checkout: Path, candidate: str, path: str) -> bool:
    raw = _git_bytes(checkout, "ls-tree", candidate, "--", path)
    records = [record for record in raw.splitlines() if record]
    if len(records) != 1:
        return False
    try:
        metadata, raw_path = records[0].split(b"\t", 1)
        mode, entry_type, _oid = metadata.split(b" ", 2)
    except ValueError:
        return False
    return mode == b"100644" and entry_type == b"blob" and raw_path == os.fsencode(path)


def evaluate_scope(
    checkout: Path,
    *,
    base: str,
    candidate: str,
    policy: str,
) -> tuple[ScopeFacts, PolicyContract | None]:
    policy = _policy_identifier(policy, "policy")
    raw_diff = _git_bytes(
        checkout,
        "diff",
        "--binary",
        "--full-index",
        "--no-renames",
        f"{base}..{candidate}",
    )
    diff_sha256 = hashlib.sha256(raw_diff).hexdigest()
    if candidate == base:
        if policy not in _COMPARISON_POLICIES:
            raise ContractError(
                "unchanged baseline policy must be in the locked comparison policies"
            )
        status = _name_status(
            _git_bytes(
                checkout,
                "diff",
                "--name-status",
                "--no-renames",
                f"{base}..{candidate}",
            )
        )
        if status:
            raise ContractError("unchanged baseline unexpectedly has a commit diff")
        return (
            ScopeFacts(True, True, True, None, (), diff_sha256),
            None,
        )

    source = f"libCacheSim/cache/eviction/{policy}.c"
    test = f"test/test_{policy}.c"
    allowed = {source, test, _CONTRACT_PATH, *_WIRING_PATHS}
    entries = _name_status(
        _git_bytes(
            checkout,
            "diff",
            "--name-status",
            "--no-renames",
            f"{base}..{candidate}",
        )
    )
    statuses = {path: status for status, path in entries}
    changed_paths = tuple(sorted(statuses))
    required_additions = {source, test, _CONTRACT_PATH}
    allowed_paths = (
        set(statuses) <= allowed
        and all(statuses.get(path) == "A" for path in required_additions)
        and all(statuses.get(path) == "M" for path in _WIRING_PATHS)
        and all(
            _regular_candidate_blob(checkout, candidate, path) for path in statuses
        )
    )
    additive = all(path in statuses for path in _WIRING_PATHS) and all(
        _wiring_is_additive(checkout, base, candidate, path, policy)
        for path in _WIRING_PATHS
    )
    baseline_unchanged = allowed_paths and additive and not any(
        status in {"D", "T"} for status in statuses.values()
    )
    contract: PolicyContract | None = None
    contract_bound = False
    try:
        raw_contract = _git_bytes(checkout, "show", f"{candidate}:{_CONTRACT_PATH}")
        contract = _validate_contract(
            _contract_object(raw_contract, _CONTRACT_PATH), expected_policy=policy
        )
        contract_bound = statuses.get(_CONTRACT_PATH) == "A"
    except ContractError:
        contract = None
        contract_bound = False
    return (
        ScopeFacts(
            allowed_paths=allowed_paths,
            baseline_unchanged=baseline_unchanged,
            additive_wiring_only=additive,
            contract_bound=contract_bound,
            changed_paths=changed_paths,
            diff_sha256=diff_sha256,
        ),
        contract,
    )
