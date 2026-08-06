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


def _tool_uses(section: dict[str, object]) -> list[dict[str, object]]:
    value = section.get("tool_uses")
    if not isinstance(value, list) or any(
        not isinstance(item, dict) for item in value
    ):
        raise VerificationError("Agent tool_uses are invalid")
    return value  # type: ignore[return-value]


def _live_agent_sections(
    evidence: dict[str, object],
) -> tuple[
    dict[str, object],
    dict[str, object],
    list[dict[str, object]],
    list[dict[str, object]],
]:
    agent = evidence.get("agent")
    restart = evidence.get("restart")
    if not isinstance(agent, dict):
        raise VerificationError("live agent section is missing")
    if not isinstance(restart, dict):
        raise VerificationError("restart section is missing")
    if agent.get("class") != "arbor.core.agent.Agent":
        raise VerificationError("commissioning did not use the native Agent")
    if agent.get("destroyed_before_restart") is not True:
        raise VerificationError("primary Agent was not destroyed before restart")
    if agent.get("stop_reason") != "finished" or restart.get("stop_reason") != "finished":
        raise VerificationError("an Agent did not finish normally")
    if restart.get("initial_message_count") != 0:
        raise VerificationError("restart Agent reused prior messages")
    primary_agent = agent.get("instance")
    primary_provider = agent.get("provider_instance")
    restart_agent = restart.get("agent_instance")
    restart_provider = restart.get("provider_instance")
    if any(
        isinstance(value, bool) or not isinstance(value, int)
        for value in (
            primary_agent,
            primary_provider,
            restart_agent,
            restart_provider,
        )
    ):
        raise VerificationError("Agent/provider identity is invalid")
    if primary_agent == restart_agent or primary_provider == restart_provider:
        raise VerificationError("restart did not use a fresh Agent/provider")
    for section in (agent, restart):
        digest = section.get("message_sha256")
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise VerificationError("Agent message hash is invalid")
        if not isinstance(section.get("result"), str):
            raise VerificationError("Agent terminal result is invalid")
    return agent, restart, _tool_uses(agent), _tool_uses(restart)


def _tool_identity(item: dict[str, object]) -> tuple[str, str]:
    name = item.get("name")
    tool_input = item.get("input")
    if not isinstance(name, str) or not isinstance(tool_input, dict):
        raise VerificationError("Agent tool use is invalid")
    selector = (
        tool_input.get("action")
        if name in {"Research", "Task", "Eval"}
        else tool_input.get("file_path")
    )
    if not isinstance(selector, str):
        raise VerificationError("Agent tool selector is invalid")
    return name, selector


def _validate_primary_tool_sequence(
    tool_uses: list[dict[str, object]],
) -> dict[str, bytes]:
    identities = [_tool_identity(item) for item in tool_uses]
    cursor = 0

    def require(expected: tuple[str, str]) -> None:
        nonlocal cursor
        if cursor >= len(identities) or identities[cursor] != expected:
            raise VerificationError(
                f"Agent tool sequence expected {expected!r} at position {cursor}"
            )
        cursor += 1

    require(("Research", "attention"))
    require(("Task", "create"))
    require(("Task", "start"))
    require(("Task", "status"))
    while cursor < len(identities) and identities[cursor] == ("Task", "status"):
        cursor += 1
    require(("Task", "collect"))
    require(("Eval", "run"))
    require(("Research", "attention"))
    require(("Read", "knowledge/claims/C-0001.md"))
    require(("Read", "memory/NOW.md"))
    write_paths = (
        "knowledge/claims/C-0001.md",
        "memory/NOW.md",
        "transitions/T-E2E-ASSIMILATE/proposal.json",
    )
    payloads: dict[str, bytes] = {}
    for path in write_paths:
        require(("Write", path))
        tool_input = tool_uses[cursor - 1]["input"]
        assert isinstance(tool_input, dict)
        content = tool_input.get("content")
        if not isinstance(content, str):
            raise VerificationError("semantic Write payload is invalid")
        payloads[path] = content.encode("utf-8")
    require(("Research", "transition_audit"))
    require(("Research", "checkpoint"))
    if cursor != len(identities):
        raise VerificationError("Agent tool sequence contains extra calls")
    return payloads


def verify(evidence_path: Path) -> dict[str, object]:
    evidence = _json(evidence_path)
    if evidence.get("schema_version") != 1:
        raise VerificationError("evidence schema_version must be 1")
    if evidence.get("enforcement_class") != "cooperative":
        raise VerificationError("commissioning must be explicitly cooperative")
    task = evidence.get("task")
    evaluation = evidence.get("eval")
    checkpoint = evidence.get("checkpoint")
    if not all(isinstance(item, dict) for item in (task, evaluation, checkpoint)):
        raise VerificationError("evidence sections are incomplete")
    assert isinstance(task, dict)
    assert isinstance(evaluation, dict)
    assert isinstance(checkpoint, dict)
    if task.get("child_commit") != evaluation.get("candidate_commit"):
        raise VerificationError("Task and Eval candidate_commit mismatch")
    _, restart, primary_tools, restart_tools = _live_agent_sections(evidence)
    semantic_payloads = _validate_primary_tool_sequence(primary_tools)
    if [_tool_identity(item) for item in restart_tools] != [
        ("Research", "attention")
    ]:
        raise VerificationError("restart Agent tool sequence is not one attention")
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
    for path, expected in semantic_payloads.items():
        if _git(project, "show", f"{final_commit}:{path}") != expected:
            raise VerificationError(f"Git blob differs from Agent Write: {path}")

    proposal_raw = semantic_payloads[
        "transitions/T-E2E-ASSIMILATE/proposal.json"
    ]
    proposal = json.loads(proposal_raw)
    if not isinstance(proposal, dict):
        raise VerificationError("Agent proposal is not an object")
    if proposal.get("base_commit") != base_commit:
        raise VerificationError("Agent proposal base_commit differs")
    if proposal.get("workspace_paths") != [
        "knowledge/claims/C-0001.md",
        "memory/NOW.md",
    ]:
        raise VerificationError("Agent proposal workspace_paths differ")

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

    assimilations = proposal.get("assimilations")
    if not isinstance(assimilations, list) or len(assimilations) != 2:
        raise VerificationError("Agent proposal requires two assimilations")
    observed_refs = {
        item.get("observation_ref")
        for item in assimilations
        if isinstance(item, dict)
    }
    if observed_refs != {collected_ref, receipt_ref}:
        raise VerificationError("Agent proposal assimilation refs differ")

    packet = restart.get("packet")
    if not isinstance(packet, dict):
        raise VerificationError("restart packet is incomplete")
    if _refs(packet):
        raise VerificationError("assimilated observations remain pending after restart")
    recent = packet.get("recent_evidence_delta")
    if (
        not isinstance(recent, list)
        or not recent
        or not isinstance(recent[0], dict)
        or recent[0].get("transition_id") != transition_id
    ):
        raise VerificationError("restart packet lacks latest evidence transition")
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
