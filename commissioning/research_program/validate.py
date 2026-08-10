from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import marshal
import math
import os
import re
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import CodeType, MappingProxyType
from typing import Mapping


_MAX_CONTRACT_BYTES = 128 * 1024
_MAX_PROCEDURE_BYTES = 128 * 1024
_TOP_LEVEL_KEYS = {"schema_version", "allowed_tools", "artifacts", "procedures"}
_PROCEDURE_KEYS = {"input", "output", "tools"}
_SOURCE_TOP_LEVEL_KEYS = {"schema_version", "sources"}
_SOURCE_KEYS = {
    "id",
    "repository",
    "commit",
    "license",
    "selected_paths",
    "adaptation",
}
_FRONTMATTER_KEYS = ("name", "source_ids", "input", "output", "tools")
_PROCEDURE_HEADINGS = (
    "Purpose",
    "Inputs",
    "Method",
    "Output",
    "Completion",
    "Forbidden",
)
_PROGRAM_FILES = {"SOURCES.json", "__init__.py", "validate.py"}
_PROGRAM_DIRECTORIES = {"contracts", "procedures"}
_BYTECODE_MAGIC_BY_TAG = {
    f"cpython-3{minor}": magic.to_bytes(2, "little") + b"\r\n"
    for minor, magic in {
        10: 3439,
        11: 3495,
        12: 3531,
        13: 3571,
        14: 3627,
    }.items()
}
_BYTECODE_MAGIC_BY_TAG.update(
    {
        f"pypy3{minor}": magic.to_bytes(2, "little") + b"\r\n"
        for minor, magic in {10: 384, 11: 432, 12: 448}.items()
    }
)
if (sys.implementation.cache_tag or "").startswith("pypy3"):
    _BYTECODE_MAGIC_BY_TAG[sys.implementation.cache_tag] = importlib.util.MAGIC_NUMBER
_FOREIGN_BYTECODE_SCRIPT = r"""
import base64
import importlib.util
import io
import json
import marshal
import struct
import sys
from pathlib import PurePosixPath
from types import CodeType

LIMIT = 512 * 1024
ATTRS = (
    "co_argcount", "co_posonlyargcount", "co_kwonlyargcount", "co_nlocals",
    "co_stacksize", "co_flags", "co_code", "co_names", "co_varnames",
    "co_filename", "co_name", "co_qualname", "co_firstlineno", "co_lnotab",
    "co_linetable", "co_exceptiontable", "co_freevars", "co_cellvars",
)

def same_const(left, right):
    if type(left) is not type(right):
        return False
    if type(left) is CodeType:
        return same_code(left, right)
    if type(left) is tuple:
        return len(left) == len(right) and all(
            same_const(left_item, right_item)
            for left_item, right_item in zip(left, right)
        )
    if type(left) is frozenset:
        remaining = list(right)
        for left_item in left:
            for index, right_item in enumerate(remaining):
                if same_const(left_item, right_item):
                    remaining.pop(index)
                    break
            else:
                return False
        return not remaining
    if type(left) is float:
        return struct.pack("!d", left) == struct.pack("!d", right)
    if type(left) is complex:
        return (
            struct.pack("!d", left.real) == struct.pack("!d", right.real)
            and struct.pack("!d", left.imag) == struct.pack("!d", right.imag)
        )
    return left == right

def same_code(left, right):
    if type(left) is not CodeType or type(right) is not CodeType:
        return False
    for name in ATTRS:
        if hasattr(left, name) != hasattr(right, name):
            return False
        if hasattr(left, name) and getattr(left, name) != getattr(right, name):
            return False
    if len(left.co_consts) != len(right.co_consts):
        return False
    for left_item, right_item in zip(left.co_consts, right.co_consts):
        if not same_const(left_item, right_item):
            return False
    return True

def code_filenames_match(value, filename):
    if type(value) is CodeType:
        return value.co_filename == filename and all(
            code_filenames_match(item, filename) for item in value.co_consts
        )
    if type(value) in (tuple, frozenset):
        return all(code_filenames_match(item, filename) for item in value)
    return True

def validated_filename(code, absolute, relative):
    if type(code) is not CodeType or type(code.co_filename) is not str:
        return None
    filename = code.co_filename
    path = PurePosixPath(filename)
    if path.is_absolute():
        valid = filename == absolute
    else:
        normalized = filename[2:] if filename.startswith("./") else filename
        valid = (
            normalized == relative
            and "\\" not in filename
            and "\x00" not in filename
            and ":" not in filename
        )
    if not valid or not code_filenames_match(code, filename):
        return None
    return filename

def main():
    encoded = sys.stdin.buffer.read(LIMIT + 1)
    if len(encoded) > LIMIT:
        return False
    payload = json.loads(encoded)
    if sys.implementation.cache_tag != payload["tag"]:
        return False
    pyc = base64.b64decode(payload["pyc"], validate=True)
    source = base64.b64decode(payload["source"], validate=True)
    if len(pyc) < 16 or pyc[:4] != importlib.util.MAGIC_NUMBER:
        return False
    flags = int.from_bytes(pyc[4:8], "little")
    if flags not in (0, 1, 3):
        return False
    if flags == 0:
        if int.from_bytes(pyc[8:12], "little") != payload["mtime"]:
            return False
        if int.from_bytes(pyc[12:16], "little") != payload["size"]:
            return False
    elif pyc[8:16] != importlib.util.source_hash(source):
        return False
    stream = io.BytesIO(pyc[16:])
    actual = marshal.load(stream)
    if stream.read(1):
        return False
    filename = validated_filename(
        actual,
        payload["filename"],
        payload["relative_filename"],
    )
    if filename is None:
        return False
    expected = compile(
        source,
        filename,
        "exec",
        dont_inherit=True,
        optimize=payload["optimization"],
    )
    return same_code(actual, expected)

try:
    valid = main()
except BaseException:
    valid = False
raise SystemExit(0 if valid else 1)
"""
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
        "rival_mechanism_set_ref",
        "mechanism_refs",
        "experiment_proposal_ref",
        "prediction_ref",
        "falsifier_ref",
        "preregistration_ref",
    ),
    "ObservationUpdate": (
        "evidence_refs",
        "classifications",
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
        "candidate_commit",
        "data_manifest_refs",
        "environment_sha256",
        "output_schema_sha256",
        "analysis_boundaries",
        "rerun_rules",
        "experiment_proposal_ref",
        "mechanism_refs",
        "prediction_ref",
        "falsifier_ref",
    ),
    "FrozenEvidencePacket": (
        "task_brief_ref",
        "preregistration_ref",
        "candidate_commit",
        "source_refs",
        "raw_refs",
        "reproduction_ref",
        "root_question_ref",
        "claim_draft_ref",
        "researcher_model_id",
        "researcher_model_family",
        "researcher_session_refs",
        "researcher_worktree_ref",
        "packet_sha256",
        "review_session_receipt_ref",
        "reviewer_session_id",
    ),
    "ReviewerReport": (
        "reproduction_refs",
        "alternative_explanations",
        "leakage_findings",
        "statistical_findings",
        "scope_objections",
        "fatal_objections",
        "unresolved_objections",
        "reviewer_model_id",
        "reviewer_model_family",
        "independence_evidence_ref",
        "packet_sha256",
        "claim_draft_ref",
        "candidate_commit",
        "reviewer_session_id",
    ),
    "AdjudicatedEvidence": (
        "claim_draft_ref",
        "evidence_refs",
        "review_ref",
        "principal_response_ref",
        "root_question_ref",
        "candidate_commit",
        "preregistration_ref",
        "reproduction_ref",
        "principal_actor_ref",
        "principal_checkpoint_ref",
        "disposition",
        "principal_decision_receipt_ref",
        "authority_class",
        "principal_authority_ref",
        "checkpoint_reservation_ref",
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
        "disposition",
        "root_question_ref",
        "candidate_commit",
        "preregistration_ref",
        "review_ref",
        "principal_response_ref",
        "reproduction_ref",
        "environment_ref",
        "checkpoint_ref",
        "principal_decision_receipt_ref",
        "authority_class",
        "principal_authority_ref",
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
            "Receipt.read",
            "Git.read",
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
            "Research.petition",
        ),
    ),
    "aros-claim-package": (
        "AdjudicatedEvidence",
        "ClaimPackage",
        ("Source.read", "Receipt.read", "Git.read", "Research.checkpoint"),
    ),
}
_PROCEDURE_SHA256 = {
    "aros-claim-package": "b6f661a42c2e18aca8cf0a1a2a49956bf04c7c166ad6f24f9aaee0e00c39737e",
    "aros-evidence-update": "2d28d7003eea2b11efafcfce8c96fef292fbc9a7a3a54ba31771952b875d7776",
    "aros-experiment-design": "a40ff958ebe3fd87ed88b869ade75063f09d6e96969761911ae447057a27727c",
    "aros-independent-review": "7eac1ab5f835a0a630b796103a4b767db35a1a0cc3d6ad51cf83c1625a548e06",
    "aros-rival-mechanisms": "d38f66fe7631900769b5d7a37595e1522c5275e01fe5b50e56e609fea54cd989",
    "aros-source-research": "2e9aa3126854944234ad7e99550918fc91f228b1da780bbd87550681184b9dec",
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


@dataclass(frozen=True, slots=True)
class SourceRecord:
    id: str
    repository: str
    commit: str
    license: str
    selected_paths: tuple[str, ...]
    adaptation: str


@dataclass(frozen=True, slots=True)
class SourceSet:
    schema_version: int
    sources: tuple[SourceRecord, ...]


@dataclass(frozen=True, slots=True)
class FileBinding:
    dev: int
    ino: int
    size: int
    mtime_ns: int
    ctime_ns: int
    sha256: str
    raw: bytes


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _reject_constant(value: str) -> object:
    raise ValueError("JSON numbers must be finite")


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("JSON numbers must be finite")
    return parsed


def frozen_evidence_packet_sha256(packet: Mapping[str, object]) -> str:
    canonical = dict(packet)
    canonical.pop("packet_sha256", None)
    encoded = json.dumps(
        canonical,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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
    _, text = _read_regular_utf8(
        path, label="contract path", limit=_MAX_CONTRACT_BYTES
    )
    return text


def _read_bound_regular_bytes(
    path: Path, *, label: str, limit: int
) -> tuple[bytes, FileBinding]:
    candidate = Path(path)
    try:
        before = candidate.lstat()
    except OSError as error:
        raise ValueError(f"{label} is not readable") from error
    if stat.S_ISLNK(before.st_mode):
        raise ValueError(f"{label} must not be a symlink")
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f"{label} must be a regular file")
    if before.st_size > limit:
        raise ValueError(f"{label} must not exceed 128 KiB")

    try:
        descriptor = os.open(
            candidate,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK,
        )
    except OSError as error:
        raise ValueError(f"{label} is not readable") from error
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError(f"{label} must be a regular file")
        if not _same_file(before, opened):
            raise ValueError(f"{label} identity changed before reading")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            raw = stream.read(limit + 1)
            after_read = os.fstat(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    if len(raw) > limit:
        raise ValueError(f"{label} must not exceed 128 KiB")
    try:
        after_path = candidate.lstat()
    except OSError as error:
        raise ValueError(f"{label} identity changed while reading") from error
    if (
        not _same_file(opened, after_read)
        or not _same_file(opened, after_path)
        or len(raw) != opened.st_size
    ):
        raise ValueError(f"{label} identity or contents changed while reading")
    return raw, FileBinding(
        dev=after_read.st_dev,
        ino=after_read.st_ino,
        size=after_read.st_size,
        mtime_ns=after_read.st_mtime_ns,
        ctime_ns=after_read.st_ctime_ns,
        sha256=hashlib.sha256(raw).hexdigest(),
        raw=raw,
    )


def _read_regular_bytes(path: Path, *, label: str, limit: int) -> bytes:
    raw, _ = _read_bound_regular_bytes(path, label=label, limit=limit)
    return raw


def _decode_utf8(raw: bytes, label: str) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"{label} must be UTF-8") from error


def _read_regular_utf8(
    path: Path, *, label: str, limit: int
) -> tuple[bytes, str]:
    raw = _read_regular_bytes(path, label=label, limit=limit)
    return raw, _decode_utf8(raw, label)


def _exact_keys(value: object, expected: set[str], label: str) -> dict[str, object]:
    if type(value) is not dict:
        raise ValueError(f"{label} must be an object")
    actual = set(value)
    unknown = actual - expected
    if unknown:
        raise ValueError(f"{label} has {len(unknown)} unknown field(s)")
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
            folded_key = key.casefold()
            if folded_key in _FORBIDDEN_FIELDS:
                raise ValueError(f"forbidden contract field: {folded_key}")
            _reject_forbidden_fields(item)
    elif type(value) is list:
        for item in value:
            _reject_forbidden_fields(item)


def _parse_contracts(text: str) -> ContractSet:
    value = json.loads(
        text,
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
            raise ValueError("allowed_tools has unknown tools")
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
            raise ValueError(f"procedure {name} has unknown tools")
        if (input_name, output_name, tools) != expected:
            raise ValueError(f"procedure {name} must match the canonical contract")
        procedures[name] = ProcedureContract(input_name, output_name, tools)

    return ContractSet(
        schema_version=1,
        allowed_tools=allowed_tools,
        artifacts=MappingProxyType(artifacts),
        procedures=MappingProxyType(procedures),
    )


def load_contracts(path: Path) -> ContractSet:
    return _parse_contracts(_read_contract(path))


def _git(repository: Path, *arguments: str) -> str:
    environment = {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "HOME": os.devnull,
        "LC_ALL": "C",
        "PATH": os.defpath,
    }
    try:
        completed = subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=False,
            capture_output=True,
            env=environment,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError("source Git inspection failed") from error
    if completed.returncode != 0:
        raise ValueError("source Git inspection failed")
    return completed.stdout.strip()


def _parse_sources(raw: bytes) -> SourceSet:
    text = _decode_utf8(raw, "source record")
    try:
        value = json.loads(
            text,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
        )
    except (json.JSONDecodeError, TypeError) as error:
        raise ValueError("source record must be strict JSON") from error
    root = _exact_keys(value, _SOURCE_TOP_LEVEL_KEYS, "source record")
    if type(root["schema_version"]) is not int or root["schema_version"] != 1:
        raise ValueError("source schema_version must be the plain integer 1")
    sources_value = root["sources"]
    if type(sources_value) is not list or not sources_value:
        raise ValueError("sources must be a non-empty list")

    records: list[SourceRecord] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(sources_value):
        source = _exact_keys(item, _SOURCE_KEYS, f"source {index}")
        source_id = source["id"]
        if (
            type(source_id) is not str
            or len(source_id) > 64
            or re.fullmatch(r"source-[1-9][0-9]*", source_id) is None
        ):
            raise ValueError(f"source {index} has an invalid id")
        if source_id in seen_ids:
            raise ValueError(f"duplicate source id: {source_id}")
        seen_ids.add(source_id)

        repository_value = source["repository"]
        if type(repository_value) is not str or not repository_value:
            raise ValueError(f"source {source_id} repository must be a string")
        repository = Path(repository_value)
        if not repository.is_absolute():
            raise ValueError(f"source {source_id} repository must be absolute")
        if _git(repository, "rev-parse", "--is-inside-work-tree") != "true":
            raise ValueError(f"source {source_id} repository is not a Git worktree")

        commit = source["commit"]
        if type(commit) is not str or re.fullmatch(r"[0-9a-f]{40}", commit) is None:
            raise ValueError(f"source {source_id} commit must be a full object id")
        if _git(repository, "rev-parse", "--verify", f"{commit}^{{commit}}") != commit:
            raise ValueError(f"source {source_id} commit is not canonical")
        if _git(repository, "cat-file", "-t", commit) != "commit":
            raise ValueError(f"source {source_id} object must be a commit")

        license_value = source["license"]
        if (
            type(license_value) is not str
            or license_value != license_value.strip()
            or not license_value
            or len(license_value) > 128
        ):
            raise ValueError(f"source {source_id} license must be a bounded string")
        selected_paths = _string_list(
            source["selected_paths"], f"source {source_id} selected_paths"
        )
        if not selected_paths:
            raise ValueError(f"source {source_id} selected_paths must not be empty")
        for selected_path in selected_paths:
            parsed_path = PurePosixPath(selected_path)
            if (
                selected_path != parsed_path.as_posix()
                or parsed_path.is_absolute()
                or not parsed_path.parts
                or any(part in {"", ".", ".."} for part in parsed_path.parts)
                or "\\" in selected_path
                or "\x00" in selected_path
            ):
                raise ValueError(f"source {source_id} has an invalid selected path")
            if _git(repository, "cat-file", "-t", f"{commit}:{selected_path}") != "blob":
                raise ValueError(f"source {source_id} selected path must name a blob")

        adaptation = source["adaptation"]
        if (
            type(adaptation) is not str
            or adaptation != adaptation.strip()
            or not 1 <= len(adaptation) <= 256
        ):
            raise ValueError(f"source {source_id} adaptation must be a bounded string")
        records.append(
            SourceRecord(
                id=source_id,
                repository=repository_value,
                commit=commit,
                license=license_value,
                selected_paths=selected_paths,
                adaptation=adaptation,
            )
        )
    return SourceSet(schema_version=1, sources=tuple(records))


def load_sources(path: Path) -> SourceSet:
    raw = _read_regular_bytes(
        path, label="source record", limit=_MAX_CONTRACT_BYTES
    )
    return _parse_sources(raw)


def _require_directory(path: Path, label: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ValueError(f"{label} is not readable") from error
    if stat.S_ISLNK(metadata.st_mode):
        raise ValueError(f"{label} must not be a symlink")
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"{label} must be a directory")


def _bytecode_name(path: Path) -> re.Match[str]:
    match = re.fullmatch(
        r"(?P<module>__init__|validate)\.(?P<tag>[A-Za-z0-9-]+)"
        r"(?:\.opt-[12])?\.pyc",
        path.name,
    )
    if match is None or match.group("tag") not in _BYTECODE_MAGIC_BY_TAG:
        raise ValueError("program filesystem inventory contains invalid bytecode")
    return match


def _load_bytecode(raw: bytes) -> CodeType:
    stream = io.BytesIO(raw[16:])
    try:
        code = marshal.load(stream)
    except (EOFError, TypeError, ValueError) as error:
        raise ValueError(
            "program filesystem inventory contains invalid bytecode"
        ) from error
    if type(code) is not CodeType or stream.read(1) != b"":
        raise ValueError("program filesystem inventory contains invalid bytecode")
    return code


def _validate_bytecode_envelope(path: Path, binding: FileBinding) -> None:
    match = _bytecode_name(path)
    raw = binding.raw
    current_tag = sys.implementation.cache_tag
    if (
        len(raw) < 16
        or int.from_bytes(raw[4:8], "little") not in {0, 1, 3}
        or raw[:4] != _BYTECODE_MAGIC_BY_TAG[match.group("tag")]
    ):
        raise ValueError("program filesystem inventory contains invalid bytecode")
    if match.group("tag") != current_tag:
        return
    code = _load_bytecode(raw)
    source_name = "__init__.py" if match.group("module") == "__init__" else "validate.py"
    if Path(code.co_filename).name != source_name:
        raise ValueError("program filesystem inventory contains invalid bytecode")


def _bytecode_filenames_match(value: object, filename: str) -> bool:
    if type(value) is CodeType:
        return value.co_filename == filename and all(
            _bytecode_filenames_match(item, filename) for item in value.co_consts
        )
    if type(value) in (tuple, frozenset):
        return all(_bytecode_filenames_match(item, filename) for item in value)
    return True


def _validated_bytecode_filename(code: object, source_path: Path) -> str:
    if type(code) is not CodeType or type(code.co_filename) is not str:
        raise ValueError("program filesystem inventory contains invalid bytecode")
    filename = code.co_filename
    path = PurePosixPath(filename)
    if path.is_absolute():
        valid = filename == str(source_path)
    else:
        relative = PurePosixPath(
            "commissioning", "research_program", source_path.name
        ).as_posix()
        normalized = filename[2:] if filename.startswith("./") else filename
        valid = (
            normalized == relative
            and "\\" not in filename
            and "\x00" not in filename
            and ":" not in filename
        )
    if not valid or not _bytecode_filenames_match(code, filename):
        raise ValueError("program filesystem inventory contains invalid bytecode")
    return filename


def _validate_bytecode_source(
    path: Path,
    binding: FileBinding,
    source_path: Path,
    source_binding: FileBinding,
) -> None:
    match = _bytecode_name(path)
    optimization_match = re.search(r"\.opt-([0-9]+)\.pyc$", path.name)
    optimization = (
        int(optimization_match.group(1))
        if optimization_match is not None
        else sys.flags.optimize
    )
    if optimization not in {0, 1, 2}:
        raise ValueError("program filesystem inventory contains invalid bytecode")
    flags = int.from_bytes(binding.raw[4:8], "little")
    if flags == 0:
        header_matches = (
            int.from_bytes(binding.raw[8:12], "little")
            == ((source_binding.mtime_ns // 1_000_000_000) & 0xFFFFFFFF)
            and int.from_bytes(binding.raw[12:16], "little")
            == (source_binding.size & 0xFFFFFFFF)
        )
    else:
        header_matches = (
            binding.raw[8:16] == importlib.util.source_hash(source_binding.raw)
        )
    actual_code = _load_bytecode(binding.raw)
    filename = _validated_bytecode_filename(actual_code, source_path)
    expected_code = compile(
        source_binding.raw,
        filename,
        "exec",
        dont_inherit=True,
        optimize=optimization,
    )
    source_name = "__init__" if match.group("module") == "__init__" else "validate"
    if (
        source_path.stem != source_name
        or not header_matches
        or actual_code != expected_code
    ):
        raise ValueError("program filesystem inventory contains invalid bytecode")


def _foreign_bytecode_interpreter(tag: str) -> str:
    if tag.startswith("cpython-"):
        digits = tag.removeprefix("cpython-")
        candidates = (f"python{digits[0]}.{digits[1:]}",)
    elif tag.startswith("pypy"):
        digits = tag.removeprefix("pypy")
        candidates = (f"pypy{digits[0]}.{digits[1:]}", "pypy3")
    else:
        candidates = ()
    for candidate in candidates:
        executable = shutil.which(candidate, path=os.defpath)
        if executable is not None:
            return executable
    raise ValueError("foreign bytecode interpreter is unavailable")


def _validate_foreign_bytecode_source(
    path: Path,
    binding: FileBinding,
    source_path: Path,
    source_binding: FileBinding,
) -> None:
    match = _bytecode_name(path)
    tag = match.group("tag")
    executable = _foreign_bytecode_interpreter(tag)
    optimization_match = re.search(r"\.opt-([0-9]+)\.pyc$", path.name)
    optimization = (
        int(optimization_match.group(1))
        if optimization_match is not None
        else 0
    )
    payload = json.dumps(
        {
            "filename": str(source_path),
            "mtime": (source_binding.mtime_ns // 1_000_000_000) & 0xFFFFFFFF,
            "optimization": optimization,
            "pyc": base64.b64encode(binding.raw).decode("ascii"),
            "relative_filename": (
                f"commissioning/research_program/{source_path.name}"
            ),
            "size": source_binding.size & 0xFFFFFFFF,
            "source": base64.b64encode(source_binding.raw).decode("ascii"),
            "tag": tag,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    environment = {
        "HOME": os.devnull,
        "LC_ALL": "C",
        "PATH": os.defpath,
    }
    try:
        completed = subprocess.run(
            [executable, "-I", "-S", "-c", _FOREIGN_BYTECODE_SCRIPT],
            check=False,
            close_fds=True,
            env=environment,
            input=payload,
            stderr=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError("foreign bytecode validation failed") from error
    if completed.returncode != 0:
        raise ValueError("foreign bytecode validation failed")


def _validate_program_inventory(root: Path) -> Mapping[str, FileBinding]:
    expected_files = {
        *_PROGRAM_FILES,
        "contracts/procedure_contracts.json",
        *(f"procedures/{name}.md" for name in _PROCEDURES),
    }
    expected_directories = set(_PROGRAM_DIRECTORIES)
    seen_files: set[str] = set()
    seen_directories: set[str] = set()
    bindings: dict[str, FileBinding] = {}
    cache_present = False
    cache_files = 0
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = tuple(directory.iterdir())
        except OSError as error:
            raise ValueError("program filesystem inventory is not readable") from error
        for path in entries:
            relative = path.relative_to(root).as_posix()
            try:
                metadata = path.lstat()
            except OSError as error:
                raise ValueError("program filesystem inventory changed") from error
            if stat.S_ISLNK(metadata.st_mode):
                raise ValueError("program filesystem inventory contains a symlink")
            if stat.S_ISDIR(metadata.st_mode):
                if relative in expected_directories:
                    seen_directories.add(relative)
                    pending.append(path)
                    continue
                if relative == "__pycache__":
                    cache_present = True
                    pending.append(path)
                    continue
                raise ValueError("program filesystem inventory contains an unknown entry")
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("program filesystem inventory contains a non-file entry")
            if relative.startswith("__pycache__/"):
                _, binding = _read_bound_regular_bytes(
                    path, label="program inventory file", limit=_MAX_PROCEDURE_BYTES
                )
                _validate_bytecode_envelope(path, binding)
                bindings[relative] = binding
                cache_files += 1
                continue
            if relative not in expected_files:
                raise ValueError("program filesystem inventory contains an unknown entry")
            _, bindings[relative] = _read_bound_regular_bytes(
                path, label="program inventory file", limit=_MAX_PROCEDURE_BYTES
            )
            seen_files.add(relative)
    if seen_files != expected_files or seen_directories != expected_directories:
        raise ValueError("program filesystem inventory is incomplete")
    if cache_present and cache_files == 0:
        raise ValueError("program filesystem inventory contains empty bytecode cache")
    return MappingProxyType(bindings)


def _parse_frontmatter(text: str, name: str) -> tuple[dict[str, object], str]:
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise ValueError(f"procedure {name} must start with frontmatter")
    try:
        closing = lines.index("---", 1)
    except ValueError as error:
        raise ValueError(f"procedure {name} frontmatter is not closed") from error

    metadata: dict[str, object] = {}
    active_list: list[str] | None = None
    for line in lines[1:closing]:
        field = re.fullmatch(r"([a-z_]+):(.*)", line)
        if field is not None:
            key, raw_value = field.groups()
            if key in metadata:
                raise ValueError(f"procedure {name} has duplicate frontmatter key")
            value = raw_value.strip()
            if value:
                metadata[key] = value
                active_list = None
            else:
                active_list = []
                metadata[key] = active_list
            continue
        item = re.fullmatch(r"  - ([A-Za-z0-9.-]+)", line)
        if item is None or active_list is None:
            raise ValueError(f"procedure {name} has invalid frontmatter")
        active_list.append(item.group(1))

    if tuple(metadata) != _FRONTMATTER_KEYS:
        raise ValueError(f"procedure {name} frontmatter must have exact ordered keys")
    if any(type(metadata[key]) is not str for key in ("name", "input", "output")):
        raise ValueError(f"procedure {name} frontmatter scalar types are invalid")
    for key in ("source_ids", "tools"):
        values = metadata[key]
        if type(values) is not list or not values or len(values) != len(set(values)):
            raise ValueError(f"procedure {name} frontmatter {key} is invalid")
    return metadata, "\n".join(lines[closing + 1 :])


def _sections(body: str, name: str) -> dict[str, str]:
    headings = tuple(re.findall(r"(?m)^## ([^\n]+)$", body))
    if headings != _PROCEDURE_HEADINGS:
        raise ValueError(f"procedure {name} headings must match the canonical order")
    first_heading = body.find("## Purpose\n")
    if first_heading < 0 or body[:first_heading].strip():
        raise ValueError(f"procedure {name} has content outside required headings")
    sections: dict[str, str] = {}
    for heading in headings:
        match = re.search(
            rf"(?ms)^## {re.escape(heading)}\n(?P<section>.*?)(?=^## |\Z)", body
        )
        if match is None or not match.group("section").strip():
            raise ValueError(f"procedure {name} has an empty {heading} section")
        sections[heading] = match.group("section")
    return sections


def _normative_units(section: str) -> tuple[str, ...]:
    units: list[str] = []
    current: list[str] = []
    for raw_line in section.strip().splitlines():
        line = raw_line.strip()
        if not line:
            if current:
                units.append(" ".join(current))
                current = []
            continue
        if re.match(r"(?:- |[0-9]+\. )", line) and current:
            units.append(" ".join(current))
            current = []
        current.append(re.sub(r"^(?:- |[0-9]+\. )", "", line))
    if current:
        units.append(" ".join(current))
    return tuple(units)


_RUNTIME_ACTION_PATTERNS = (
    r"\bshell\b",
    r"\bbash\b",
    r"\b(?:ba|z)?sh\s+-c\b",
    r"\bsubprocess\b",
    r"\bssh\b",
    r"\bremote\b",
    r"\bqueue\b",
    r"\bupload(?:s|ed|ing)?\b",
    r"\bnotification(?:s)?\b",
    r"\bnotif(?:y|ies|ied|ying)\b",
    r"\bpaper\b",
    r"\bposter\b",
    r"\bslide deck\b",
    r"\bpublication\b",
    r"\bpublish(?:ed|es|ing)?\b",
    r"\bsubmission\b",
    r"\bmerge(?:s|d|ing)?\b",
    r"\bscheduler\b",
    r"\bschedul(?:e|es|ed|ing)\b",
    r"\bscore threshold\b",
    r"\bscore(?:s|d|ing)?\b",
    r"\brank(?:s|ed|ing)?\b",
    r"\bpass threshold\b",
    r"\bfixed[- ]round\b",
    r"\b(?:[0-9]+|zero|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|"
    r"nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|"
    r"thousand)\s+(?:rounds?|iterations?|cycles?)\b",
    r"\bwinner\b",
    r"\b(?:auto|automatic|automatically)[- ]+(?:choose|chooses|chose|chosen|"
    r"choosing|select|selects|selected|selecting|pick|picks|picked|picking)\b",
    r"\b(?:choose|chooses|chose|chosen|choosing|select|selects|selected|selecting|"
    r"pick|picks|picked|picking)\b.{0,80}\b(?:auto|automatic|automatically)\b",
    r"\b(?:execute|run|launch|start|request)(?:s|ed|ing)? "
    r"(?:an? |the )?(?:experiment|evaluation)\b",
    r"```(?:sh|zsh|fish|powershell|console)\b",
    r"\bpython(?:3)? [^ ]+\.py\b",
    r"\bgit (?:commit|push|merge)\b",
)
_DIRECT_PROHIBITION = re.compile(
    r"(?:\b(?:do|must) not\s*$|\bnever\s*$|"
    r"\b(?:do|must) not (?:accept|admit|allow|authorize|be|call|choose|create|"
    r"emit|execute|invoke|launch|merge|notify|perform|permit|produce|publish|"
    r"queue|rank|request|run|schedule|select|start|submit|upload|use)\b|"
    r"\bnever (?:accept|admit|allow|authorize|be|call|choose|create|emit|execute|"
    r"invoke|launch|merge|notify|perform|permit|produce|publish|queue|rank|"
    r"request|run|schedule|select|start|submit|upload|use)\b)"
)
_WITHOUT_POLARITY = re.compile(
    r"\bwithout(?:\s+(?:an?|the|directly|using|invoking|running|executing|"
    r"requesting))*\s*$"
)
_REVERSED_PROHIBITION = re.compile(
    r"\b(?:do|must) not (?:avoid|decline|delay|deny|fail|forbid|forget|hesitate|"
    r"omit|prevent|prohibit|refuse|stop)\b|"
    r"\bnot (?:an? )?(?:error|problem|violation|wrong)\b|"
    r"\bnot (?:disallowed|forbidden|prevented|prohibited)\b"
)
_POSITIVE_AUTHORITY = re.compile(
    r"\b(?:is|are|be|become|becomes|remain|remains) "
    r"(?:explicitly )?(?:allowed|authorized|permitted)\b"
)


def _validate_runtime_actions(sections: Mapping[str, str], name: str) -> None:
    forbidden_units = _normative_units(sections["Forbidden"])
    if not forbidden_units or any(not unit.startswith("Do not ") for unit in forbidden_units):
        raise ValueError(f"procedure {name} Forbidden rules must be prohibitions")
    for heading, section in sections.items():
        for unit in _normative_units(section):
            folded = unit.casefold()
            for pattern in _RUNTIME_ACTION_PATTERNS:
                for match in re.finditer(pattern, folded):
                    if _REVERSED_PROHIBITION.search(
                        folded
                    ) or _POSITIVE_AUTHORITY.search(folded):
                        raise ValueError(
                            f"procedure {name} grants forbidden runtime authority"
                        )
                    prefix = folded[max(0, match.start() - 120) : match.start()]
                    prefix = re.split(
                        r"(?:[.;:]|\band\b|\bthen\b|\bbut\b|\bhowever\b)", prefix
                    )[-1]
                    if (
                        _DIRECT_PROHIBITION.search(prefix) is None
                        and _WITHOUT_POLARITY.search(prefix) is None
                    ):
                        raise ValueError(
                            f"procedure {name} grants forbidden runtime authority in {heading}"
                        )


def _is_source_record_path(relative: Path) -> bool:
    if relative.as_posix() in {"SOURCES.json", "procedures/aros-source-research.md"}:
        return False
    if relative.suffix.casefold() not in {
        ".json",
        ".jsonl",
        ".md",
        ".toml",
        ".yaml",
        ".yml",
    }:
        return False
    candidates = (*relative.parts[:-1], relative.stem)
    for candidate in candidates:
        tokens = {
            token for token in re.split(r"[^a-z0-9]+", candidate.casefold()) if token
        }
        if tokens & {"source", "sources", "provenance"}:
            return True
    return False


def _contains_source_detail(raw: bytes, source_details: tuple[bytes, ...]) -> bool:
    folded = raw.lower()
    for detail in source_details:
        folded_detail = detail.lower()
        if len(detail) >= 8:
            if detail in raw or folded_detail in folded:
                return True
        else:
            for content, candidate in (
                (raw, detail),
                (folded, folded_detail),
            ):
                if re.search(
                    rb"(?<![A-Za-z0-9])"
                    + re.escape(candidate)
                    + rb"(?![A-Za-z0-9])",
                    content,
                ) is not None:
                    return True
    return False


def _validate_source_isolation(
    root: Path,
    sources_path: Path,
    sources: SourceSet,
    validated_content: Mapping[Path, bytes],
) -> None:
    upstream_names = (("clau" + "de").encode(), ("gem" + "ini").encode())
    detail_markers = (
        ("repository" + ":").encode(),
        ("license" + ":").encode(),
        ("/work" + "space/").encode(),
    )
    source_details = tuple(
        value.encode("utf-8")
        for source in sources.sources
        for value in (
            source.repository,
            source.commit,
            source.license,
            *source.selected_paths,
            source.adaptation,
        )
    )
    bytecode_value_details = tuple(
        value.encode("utf-8")
        for source in sources.sources
        for value in (
            source.commit,
            source.license,
            *source.selected_paths,
            source.adaptation,
        )
    )
    bytecode_source_details = (
        validated_content[sources_path],
        *(source.id.encode("utf-8") for source in sources.sources),
        *bytecode_value_details,
    )
    for path, raw in validated_content.items():
        relative = path.relative_to(root)
        folded_parts = tuple(part.casefold() for part in relative.parts)
        if "__pycache__" in folded_parts:
            if _contains_source_detail(raw, bytecode_source_details):
                raise ValueError(
                    "source details are permitted only in SOURCES.json"
                )
            continue
        if _is_source_record_path(relative):
            raise ValueError("unexpected source or provenance record")
        relative_name = relative.as_posix().casefold().encode()
        if any(product in relative_name for product in upstream_names):
            raise ValueError("upstream product name is forbidden at runtime")
        if path == sources_path:
            continue
        folded = raw.lower()
        if any(product in folded for product in upstream_names):
            raise ValueError("upstream product name is forbidden at runtime")
        if relative.suffix.casefold() != ".py" and any(
            marker in folded for marker in detail_markers
        ):
            raise ValueError("source details are permitted only in SOURCES.json")
        if _contains_source_detail(raw, source_details):
            raise ValueError("source details are permitted only in SOURCES.json")


def validate_program(root: Path) -> dict[str, object]:
    candidate_root = Path(root)
    _require_directory(candidate_root, "program root")
    program_root = candidate_root.resolve(strict=True)
    initial_bindings = _validate_program_inventory(program_root)
    sources_path = program_root / "SOURCES.json"

    sources = _parse_sources(initial_bindings["SOURCES.json"].raw)
    contract_raw = initial_bindings["contracts/procedure_contracts.json"].raw
    contract_text = _decode_utf8(contract_raw, "contract file")
    contracts = _parse_contracts(contract_text)

    expected_filenames = {f"{name}.md" for name in contracts.procedures}
    source_ids = tuple(source.id for source in sources.sources)
    procedure_results: list[dict[str, object]] = []
    validated_content = {
        program_root / relative: binding.raw
        for relative, binding in initial_bindings.items()
    }
    for filename in sorted(expected_filenames):
        name = filename.removesuffix(".md")
        raw = initial_bindings[f"procedures/{name}.md"].raw
        text = _decode_utf8(raw, f"procedure {name}")
        metadata, body = _parse_frontmatter(text, name)
        sections = _sections(body, name)
        contract = contracts.procedures[name]
        if metadata["name"] != name:
            raise ValueError(f"procedure {name} frontmatter name does not match its file")
        if tuple(metadata["source_ids"]) != source_ids:
            raise ValueError(f"procedure {name} source_ids do not match SOURCES.json")
        if (
            metadata["input"] != contract.input
            or metadata["output"] != contract.output
            or tuple(metadata["tools"]) != contract.tools
        ):
            raise ValueError(f"procedure {name} frontmatter does not match its contract")
        _validate_runtime_actions(sections, name)
        procedure_results.append(
            {
                "name": name,
                "tools": list(contract.tools),
            }
        )

    _validate_source_isolation(
        program_root, sources_path, sources, validated_content
    )
    for relative, binding in initial_bindings.items():
        if not relative.startswith("__pycache__/"):
            continue
        bytecode_path = program_root / relative
        bytecode_name = _bytecode_name(bytecode_path)
        module_name = bytecode_name.group("module")
        source_relative = "__init__.py" if module_name == "__init__" else "validate.py"
        if bytecode_name.group("tag") == sys.implementation.cache_tag:
            _validate_bytecode_source(
                bytecode_path,
                binding,
                program_root / source_relative,
                initial_bindings[source_relative],
            )
        else:
            _validate_foreign_bytecode_source(
                bytecode_path,
                binding,
                program_root / source_relative,
                initial_bindings[source_relative],
            )
    final_bindings = _validate_program_inventory(program_root)
    if final_bindings != initial_bindings:
        raise ValueError("program files changed during validation")
    for procedure in procedure_results:
        name = procedure["name"]
        procedure_sha256 = final_bindings[f"procedures/{name}.md"].sha256
        if procedure_sha256 != _PROCEDURE_SHA256[name]:
            raise ValueError(f"procedure {name} does not match approved SHA-256")
        procedure["sha256"] = procedure_sha256
    return {
        "schema_version": 1,
        "state": "valid",
        "sources": [
            {"id": source.id, "commit": source.commit}
            for source in sorted(sources.sources, key=lambda source: source.id)
        ],
        "contract_sha256": final_bindings[
            "contracts/procedure_contracts.json"
        ].sha256,
        "procedures": procedure_results,
    }
