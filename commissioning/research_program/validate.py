from __future__ import annotations

import json
import math
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping


_MAX_CONTRACT_BYTES = 128 * 1024
_TOP_LEVEL_KEYS = {"schema_version", "allowed_tools", "artifacts", "procedures"}
_PROCEDURE_KEYS = {"input", "output", "tools"}
_FORBIDDEN_FIELDS = {
    "score",
    "ranking",
    "pass",
    "reward",
    "objective",
    "aggregate",
    "acceptance_score",
}
_ALLOWED_TOOLS = (
    "Source.read",
    "Source.search",
    "Task.create",
    "Task.start",
    "Task.status",
    "Task.collect",
    "Run.request",
    "Run.status",
    "Eval.run",
    "Receipt.read",
    "Research.observe",
    "Research.checkpoint",
    "Research.petition",
    "Git.read",
)
_ARTIFACTS = {
    "ResearchQuestion": ("question_ref", "scope", "decision_context"),
    "SourcePacket": (
        "query",
        "question_ref",
        "sources",
        "retrieved_at",
        "content_refs",
        "content_sha256s",
        "limitations",
    ),
    "RivalMechanismSet": (
        "root_question_ref",
        "mechanisms",
        "predictions",
        "falsifiers",
        "conflicts",
        "remaining_uncertainty",
    ),
    "ExperimentProposal": (
        "mechanism_refs",
        "decision_uncertainty",
        "prediction",
        "falsifier",
        "controls",
        "run_request",
        "expected_information_gain",
        "cost_bound",
    ),
    "RunEvidence": (
        "run_ref",
        "eval_refs",
        "raw_refs",
        "process_state",
        "budget_used",
    ),
    "ObservationUpdate": (
        "evidence_refs",
        "strengthened",
        "weakened",
        "eliminated",
        "counterexamples",
        "negative_results",
        "remaining_uncertainty",
        "next_action_rationale",
    ),
    "Preregistration": (
        "mechanism_hypothesis",
        "key_predictions",
        "falsifiers",
        "controls",
        "primary_comparisons",
        "transfer_prediction",
        "stopping_rules",
        "evaluator_version",
    ),
    "FrozenEvidencePacket": (
        "task_brief_ref",
        "preregistration_ref",
        "commit",
        "source_refs",
        "raw_refs",
        "reproduction_ref",
    ),
    "ReviewerReport": (
        "reproduction_refs",
        "alternative_explanations",
        "leakage_findings",
        "statistical_findings",
        "scope_objections",
        "fatal_objections",
        "unresolved_objections",
    ),
    "AdjudicatedEvidence": (
        "claim_draft_ref",
        "evidence_refs",
        "review_ref",
        "principal_response_ref",
    ),
    "ClaimPackage": (
        "claim",
        "scope",
        "evidence_refs",
        "counterevidence",
        "reproduction_commands",
        "limitations",
        "remaining_uncertainty",
        "review_objections",
    ),
}
_PROCEDURES = {
    "aros-source-research": (
        "ResearchQuestion",
        "SourcePacket",
        ("Source.read", "Source.search"),
    ),
    "aros-rival-mechanisms": (
        "SourcePacket",
        "RivalMechanismSet",
        ("Git.read", "Receipt.read", "Research.observe", "Research.petition"),
    ),
    "aros-experiment-design": (
        "RivalMechanismSet",
        "ExperimentProposal",
        ("Receipt.read", "Research.observe", "Research.petition"),
    ),
    "aros-evidence-update": (
        "RunEvidence",
        "ObservationUpdate",
        (
            "Run.status",
            "Eval.run",
            "Receipt.read",
            "Research.observe",
            "Research.checkpoint",
        ),
    ),
    "aros-independent-review": (
        "FrozenEvidencePacket",
        "ReviewerReport",
        (
            "Source.read",
            "Run.request",
            "Run.status",
            "Eval.run",
            "Receipt.read",
            "Git.read",
        ),
    ),
    "aros-claim-package": (
        "AdjudicatedEvidence",
        "ClaimPackage",
        ("Source.read", "Receipt.read", "Git.read", "Research.checkpoint"),
    ),
}


@dataclass(frozen=True, slots=True)
class ProcedureContract:
    input: str
    output: str
    tools: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ContractSet:
    schema_version: int
    allowed_tools: tuple[str, ...]
    artifacts: Mapping[str, tuple[str, ...]]
    procedures: Mapping[str, ProcedureContract]


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_constant(value: str) -> object:
    raise ValueError(f"JSON numbers must be finite: {value}")


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"JSON numbers must be finite: {value}")
    return parsed


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        left.st_mode,
        left.st_size,
        left.st_mtime_ns,
        left.st_ctime_ns,
    ) == (
        right.st_dev,
        right.st_ino,
        right.st_mode,
        right.st_size,
        right.st_mtime_ns,
        right.st_ctime_ns,
    )


def _read_contract(path: Path) -> str:
    candidate = Path(path)
    before = candidate.lstat()
    if stat.S_ISLNK(before.st_mode):
        raise ValueError("contract path must not be a symlink")
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("contract path must be a regular file")
    if before.st_size > _MAX_CONTRACT_BYTES:
        raise ValueError("contract file must not exceed 128 KiB")

    descriptor = os.open(
        candidate,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK,
    )
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError("contract path must be a regular file")
        if not _same_file(before, opened):
            raise ValueError("contract path identity changed before reading")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            raw = stream.read(_MAX_CONTRACT_BYTES + 1)
            after_read = os.fstat(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    if len(raw) > _MAX_CONTRACT_BYTES:
        raise ValueError("contract file must not exceed 128 KiB")
    after_path = candidate.lstat()
    if (
        not _same_file(opened, after_read)
        or not _same_file(opened, after_path)
        or len(raw) != opened.st_size
    ):
        raise ValueError("contract path identity or contents changed while reading")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("contract file must be UTF-8") from error


def _exact_keys(value: object, expected: set[str], label: str) -> dict[str, object]:
    if type(value) is not dict:
        raise ValueError(f"{label} must be an object")
    actual = set(value)
    unknown = actual - expected
    if unknown:
        raise ValueError(f"{label} has unknown fields: {sorted(unknown)}")
    missing = expected - actual
    if missing:
        raise ValueError(f"{label} is missing fields: {sorted(missing)}")
    return value


def _string_list(value: object, label: str) -> tuple[str, ...]:
    if type(value) is not list:
        raise ValueError(f"{label} must be a list")
    if any(type(item) is not str for item in value):
        raise ValueError(f"{label} must contain only strings")
    result = tuple(value)
    if len(result) != len(set(result)):
        raise ValueError(f"{label} contains duplicate values")
    return result


def _reject_forbidden_fields(value: object) -> None:
    if type(value) is dict:
        for key, item in value.items():
            if key.casefold() in _FORBIDDEN_FIELDS:
                raise ValueError(f"forbidden contract field: {key}")
            _reject_forbidden_fields(item)
    elif type(value) is list:
        for item in value:
            _reject_forbidden_fields(item)


def load_contracts(path: Path) -> ContractSet:
    value = json.loads(
        _read_contract(path),
        object_pairs_hook=_strict_object,
        parse_constant=_reject_constant,
        parse_float=_finite_float,
    )
    _reject_forbidden_fields(value)
    root = _exact_keys(value, _TOP_LEVEL_KEYS, "contract")
    if type(root["schema_version"]) is not int or root["schema_version"] != 1:
        raise ValueError("schema_version must be the plain integer 1")

    allowed_tools = _string_list(root["allowed_tools"], "allowed_tools")
    if allowed_tools != _ALLOWED_TOOLS:
        unknown = set(allowed_tools) - set(_ALLOWED_TOOLS)
        if unknown:
            raise ValueError(f"allowed_tools has unknown tools: {sorted(unknown)}")
        raise ValueError("allowed_tools must match the canonical ordered list")

    artifacts_value = _exact_keys(root["artifacts"], set(_ARTIFACTS), "artifacts")
    artifacts: dict[str, tuple[str, ...]] = {}
    for name, expected_fields in _ARTIFACTS.items():
        fields = _string_list(artifacts_value[name], f"artifact {name}")
        forbidden = set(fields) & _FORBIDDEN_FIELDS
        if forbidden:
            raise ValueError(f"artifact {name} has forbidden fields: {sorted(forbidden)}")
        if fields != expected_fields:
            raise ValueError(f"artifact {name} fields must match the canonical list")
        artifacts[name] = fields

    procedures_value = _exact_keys(
        root["procedures"], set(_PROCEDURES), "procedures"
    )
    procedures: dict[str, ProcedureContract] = {}
    for name, expected in _PROCEDURES.items():
        procedure = _exact_keys(
            procedures_value[name], _PROCEDURE_KEYS, f"procedure {name}"
        )
        input_name = procedure["input"]
        output_name = procedure["output"]
        if type(input_name) is not str or input_name not in artifacts:
            raise ValueError(f"procedure {name} has unknown input artifact reference")
        if type(output_name) is not str or output_name not in artifacts:
            raise ValueError(f"procedure {name} has unknown output artifact reference")
        tools = _string_list(procedure["tools"], f"procedure {name} tools")
        unknown_tools = set(tools) - set(allowed_tools)
        if unknown_tools:
            raise ValueError(f"procedure {name} has unknown tools: {sorted(unknown_tools)}")
        if (input_name, output_name, tools) != expected:
            raise ValueError(f"procedure {name} must match the canonical contract")
        procedures[name] = ProcedureContract(input_name, output_name, tools)

    return ContractSet(
        schema_version=1,
        allowed_tools=allowed_tools,
        artifacts=MappingProxyType(artifacts),
        procedures=MappingProxyType(procedures),
    )
