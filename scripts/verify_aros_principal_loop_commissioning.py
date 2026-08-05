from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path


_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class VerificationError(ValueError):
    pass


def _json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise VerificationError(f"JSON object required: {path}")
    return value


def _git(project: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(project), *args],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise VerificationError(f"Git command failed: {' '.join(args)}")
    return result.stdout


def _git_text(project: Path, *args: str) -> str:
    try:
        return _git(project, *args).decode("utf-8").strip()
    except UnicodeError as error:
        raise VerificationError("Git output is not UTF-8") from error


def _canonical_hash(value: dict[str, object], hash_field: str) -> str:
    payload = {key: item for key, item in value.items() if key != hash_field}
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _record_hash(value: dict[str, object], hash_field: str) -> str:
    observed = value.get(hash_field)
    if not isinstance(observed, str) or _SHA256.fullmatch(observed) is None:
        raise VerificationError(f"invalid {hash_field}")
    if observed != _canonical_hash(value, hash_field):
        raise VerificationError(f"{hash_field} mismatch")
    return observed


def _refs(packet: dict[str, object]) -> set[str]:
    values = packet.get("unassimilated_returns")
    if not isinstance(values, list):
        raise VerificationError("packet unassimilated_returns is invalid")
    refs: set[str] = set()
    for item in values:
        if not isinstance(item, dict) or not isinstance(item.get("ref"), str):
            raise VerificationError("packet observation ref is invalid")
        refs.add(str(item["ref"]))
    return refs


def verify(evidence_path: Path) -> dict[str, object]:
    evidence = _json(evidence_path)
    if evidence.get("schema_version") != 1:
        raise VerificationError("evidence schema_version must be 1")
    if evidence.get("enforcement_class") != "cooperative":
        raise VerificationError("commissioning must be explicitly cooperative")
    task = evidence.get("task")
    evaluation = evidence.get("eval")
    checkpoint = evidence.get("checkpoint")
    restart = evidence.get("restart")
    if not all(isinstance(item, dict) for item in (task, evaluation, checkpoint, restart)):
        raise VerificationError("evidence sections are incomplete")
    assert isinstance(task, dict)
    assert isinstance(evaluation, dict)
    assert isinstance(checkpoint, dict)
    assert isinstance(restart, dict)
    if task.get("child_commit") != evaluation.get("candidate_commit"):
        raise VerificationError("Task and Eval candidate_commit mismatch")

    project_raw = evidence.get("project")
    if not isinstance(project_raw, str):
        raise VerificationError("project path is missing")
    project = Path(project_raw).resolve(strict=True)
    final_commit = checkpoint.get("commit")
    base_commit = checkpoint.get("base_commit")
    transition_id = checkpoint.get("transition_id")
    if (
        not isinstance(final_commit, str)
        or _COMMIT.fullmatch(final_commit) is None
        or not isinstance(base_commit, str)
        or _COMMIT.fullmatch(base_commit) is None
        or not isinstance(transition_id, str)
    ):
        raise VerificationError("checkpoint commit identity is invalid")
    if _git_text(project, "rev-parse", "HEAD") != final_commit:
        raise VerificationError("canonical HEAD differs from checkpoint commit")
    if _git_text(project, "rev-parse", f"{final_commit}^") != base_commit:
        raise VerificationError("checkpoint sole parent differs from base_commit")

    collected_ref = str(task.get("collected_ref"))
    receipt_ref = str(evaluation.get("receipt_ref"))
    collected = json.loads(_git(project, "show", f"{final_commit}:{collected_ref}"))
    receipt = json.loads(_git(project, "show", f"{final_commit}:{receipt_ref}"))
    if not isinstance(collected, dict) or not isinstance(receipt, dict):
        raise VerificationError("versioned Task/Eval receipt is invalid")
    if _record_hash(collected, "collected_sha256") != task.get("collected_sha256"):
        raise VerificationError("Task collected_sha256 differs")
    if _record_hash(receipt, "receipt_sha256") != evaluation.get("receipt_sha256"):
        raise VerificationError("Eval receipt_sha256 differs")
    if collected.get("child_commit") != receipt.get("candidate_commit"):
        raise VerificationError("versioned Task/Eval candidate_commit mismatch")
    if receipt.get("measurement_state") != "valid" or receipt.get("metric") != 1.0:
        raise VerificationError("expected valid metric 1.0")

    admission_ref = f"transitions/{transition_id}/admission.json"
    admission = json.loads(_git(project, "show", f"{final_commit}:{admission_ref}"))
    if not isinstance(admission, dict):
        raise VerificationError("checkpoint admission is invalid")
    if (
        admission.get("receipt_kind") != "human_direct"
        or admission.get("enforcement_class") != "cooperative"
        or admission.get("issuer") != "human-direct"
    ):
        raise VerificationError("checkpoint admission is not cooperative human-direct")
    if _record_hash(admission, "receipt_sha256") != checkpoint.get("receipt_sha256"):
        raise VerificationError("checkpoint receipt_sha256 differs")

    complete = restart.get("complete_packet")
    missing = restart.get("missing_cache_packet")
    rebuilt = restart.get("rebuilt_packet")
    if not all(isinstance(item, dict) for item in (complete, missing, rebuilt)):
        raise VerificationError("restart packets are incomplete")
    assert isinstance(complete, dict)
    assert isinstance(missing, dict)
    assert isinstance(rebuilt, dict)
    expected_refs = {collected_ref, receipt_ref}
    if _refs(complete) or _refs(rebuilt):
        raise VerificationError("assimilated observations remain pending after restart")
    if _refs(missing) != expected_refs:
        raise VerificationError("missing cache did not conservatively redisplay observations")
    warnings = missing.get("warnings")
    if not isinstance(warnings, list) or "index_incomplete" not in warnings:
        raise VerificationError("missing cache packet lacks index_incomplete")
    recent = rebuilt.get("recent_evidence_delta")
    if (
        not isinstance(recent, list)
        or not recent
        or not isinstance(recent[0], dict)
        or recent[0].get("transition_id") != transition_id
    ):
        raise VerificationError("rebuilt packet lacks latest evidence transition")
    commands = evidence.get("commands")
    if not isinstance(commands, list) or not commands:
        raise VerificationError("commissioning command receipts are missing")
    for item in commands:
        if not isinstance(item, dict):
            raise VerificationError("a commissioning command receipt is invalid")
        if item.get("returncode") != 0:
            raise VerificationError("a commissioning command failed")
    return {
        "schema_version": 1,
        "state": "verified",
        "enforcement_class": "cooperative",
        "commit": final_commit,
        "task_id": task.get("task_id"),
        "eval_id": evaluation.get("eval_id"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("evidence", type=Path)
    args = parser.parse_args()
    try:
        result = verify(args.evidence)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
