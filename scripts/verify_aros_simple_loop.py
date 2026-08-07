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
_SEMANTIC_PATHS = [
    "ideas/I-E2E.md",
    "knowledge/claims/C-0001.md",
    "memory/NOW.md",
    "model/CURRENT.md",
    "questions/Q-0001/question.md",
]


class VerificationError(ValueError):
    pass


def _json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise VerificationError(f"object required: {path}")
    return value


def _git(project: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(project), *args],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise VerificationError(f"Git failed: {' '.join(args)}")
    return result.stdout


def _git_text(project: Path, *args: str) -> str:
    return _git(project, *args).decode("utf-8").strip()


def _record_hash(value: dict[str, object], field: str) -> str:
    observed = value.get(field)
    if not isinstance(observed, str) or _SHA256.fullmatch(observed) is None:
        raise VerificationError(f"invalid {field}")
    payload = {key: item for key, item in value.items() if key != field}
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    if hashlib.sha256(raw).hexdigest() != observed:
        raise VerificationError(f"{field} mismatch")
    return observed


def _tools(section: dict[str, object]) -> list[dict[str, object]]:
    value = section.get("tool_uses")
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise VerificationError("tool uses are invalid")
    return value  # type: ignore[return-value]


def _tool(item: dict[str, object]) -> tuple[str, dict[str, object]]:
    name = item.get("name")
    tool_input = item.get("input")
    if not isinstance(name, str) or not isinstance(tool_input, dict):
        raise VerificationError("tool use is invalid")
    return name, tool_input


def _primary_writes(tools: list[dict[str, object]]) -> dict[str, bytes]:
    cursor = 0
    writes: dict[str, bytes] = {}

    def require(name: str, action: str | None = None) -> dict[str, object]:
        nonlocal cursor
        if cursor >= len(tools):
            raise VerificationError(f"missing tool {name}")
        actual_name, tool_input = _tool(tools[cursor])
        cursor += 1
        if actual_name != name or (
            action is not None and tool_input.get("action") != action
        ):
            raise VerificationError(f"expected {name}/{action}, got {actual_name}")
        return tool_input

    require("Attention")
    for path in ("model/CURRENT.md", "ideas/I-E2E.md"):
        value = require("Write")
        if value.get("file_path") != path:
            raise VerificationError("preregistration Write path differs")
    prereg = require("Checkpoint")
    if prereg != {
        "message": "Preregister deterministic mechanism and test.",
        "paths": ["ideas/I-E2E.md", "model/CURRENT.md"],
    }:
        raise VerificationError("preregistration checkpoint input differs")
    require("Task", "create")
    require("Task", "start")
    require("Task", "status")
    while cursor < len(tools):
        name, value = _tool(tools[cursor])
        if name != "Task" or value.get("action") != "status":
            break
        cursor += 1
    require("Task", "collect")
    require("Eval", "run")
    require("Attention")
    for path in (
        "questions/Q-0001/question.md",
        "model/CURRENT.md",
        "ideas/I-E2E.md",
        "knowledge/claims/C-0001.md",
        "memory/NOW.md",
    ):
        value = require("Write")
        content = value.get("content")
        if value.get("file_path") != path or not isinstance(content, str):
            raise VerificationError("final semantic Write differs")
        writes[path] = content.encode()
    final = require("Checkpoint")
    if final != {
        "message": "Interpret deterministic Task return and measurement.",
        "paths": _SEMANTIC_PATHS,
    }:
        raise VerificationError("final checkpoint input differs")
    if cursor != len(tools):
        raise VerificationError("unexpected extra tool use")
    return writes


def verify(evidence_path: Path) -> dict[str, object]:
    evidence = _json(evidence_path)
    if evidence.get("schema_version") != 1:
        raise VerificationError("evidence version differs")
    if evidence.get("enforcement_class") != "cooperative":
        raise VerificationError("boundary must be cooperative")
    project_value = evidence.get("project")
    if not isinstance(project_value, str):
        raise VerificationError("project is missing")
    project = Path(project_value).resolve(strict=True)
    task = evidence.get("task")
    evaluation = evidence.get("eval")
    checkpoint = evidence.get("checkpoint")
    agent = evidence.get("agent")
    restart = evidence.get("restart")
    if not all(isinstance(item, dict) for item in (task, evaluation, checkpoint, agent, restart)):
        raise VerificationError("evidence sections are incomplete")
    assert isinstance(task, dict)
    assert isinstance(evaluation, dict)
    assert isinstance(checkpoint, dict)
    assert isinstance(agent, dict)
    assert isinstance(restart, dict)
    if agent.get("class") != "arbor.core.agent.Agent":
        raise VerificationError("primary was not the native Agent")
    if agent.get("destroyed_before_restart") is not True:
        raise VerificationError("primary was not destroyed before restart")
    if agent.get("stop_reason") != "finished" or restart.get("stop_reason") != "finished":
        raise VerificationError("an Agent did not finish")
    if restart.get("initial_message_count") != 0:
        raise VerificationError("restart reused messages")
    if agent.get("instance") == restart.get("agent_instance"):
        raise VerificationError("restart reused Agent identity")
    if agent.get("provider_instance") == restart.get("provider_instance"):
        raise VerificationError("restart reused provider identity")
    writes = _primary_writes(_tools(agent))
    restart_tools = [_tool(item) for item in _tools(restart)]
    if restart_tools != [("Attention", {})]:
        raise VerificationError("restart did not perform exactly one Attention")

    final_commit = checkpoint.get("final_commit")
    final_parent = checkpoint.get("final_parent")
    prereg_commit = checkpoint.get("preregistration_commit")
    if any(not isinstance(item, str) or _COMMIT.fullmatch(item) is None for item in (final_commit, final_parent, prereg_commit)):
        raise VerificationError("checkpoint identity is invalid")
    assert isinstance(final_commit, str)
    assert isinstance(final_parent, str)
    assert isinstance(prereg_commit, str)
    if _git_text(project, "rev-parse", "HEAD") != final_commit:
        raise VerificationError("HEAD differs from final checkpoint")
    if _git_text(project, "rev-parse", "HEAD^") != final_parent:
        raise VerificationError("final parent differs")
    if _git_text(project, "status", "--porcelain=v1", "--untracked-files=all"):
        raise VerificationError("project is dirty after final checkpoint")
    if sorted(_git_text(project, "diff-tree", "--no-commit-id", "--name-only", "-r", final_commit).splitlines()) != _SEMANTIC_PATHS:
        raise VerificationError("final changed paths differ")
    if sorted(_git_text(project, "diff-tree", "--no-commit-id", "--name-only", "-r", prereg_commit).splitlines()) != ["ideas/I-E2E.md", "model/CURRENT.md"]:
        raise VerificationError("preregistration changed paths differ")
    for path, expected in writes.items():
        if _git(project, "show", f"{final_commit}:{path}") != expected:
            raise VerificationError(f"final Git blob differs: {path}")

    collected_ref = task.get("collected_ref")
    receipt_ref = evaluation.get("receipt_ref")
    if not isinstance(collected_ref, str) or not isinstance(receipt_ref, str):
        raise VerificationError("return refs are invalid")
    collected = json.loads(_git(project, "show", f"{final_commit}:{collected_ref}"))
    receipt = json.loads(_git(project, "show", f"{final_commit}:{receipt_ref}"))
    if not isinstance(collected, dict) or not isinstance(receipt, dict):
        raise VerificationError("return records are invalid")
    if _record_hash(collected, "collected_sha256") != task.get("collected_sha256"):
        raise VerificationError("Task hash differs")
    if _record_hash(receipt, "receipt_sha256") != evaluation.get("receipt_sha256"):
        raise VerificationError("Eval hash differs")
    if collected.get("child_commit") != receipt.get("candidate_commit"):
        raise VerificationError("Task and Eval candidates differ")
    if receipt.get("measurement_state") != "valid" or receipt.get("metric") != 1.0:
        raise VerificationError("measurement differs")

    message = _git_text(project, "log", "-1", "--format=%B", final_commit)
    trailers = sorted(
        line.split(": ", 1)[1]
        for line in message.splitlines()
        if line.startswith("AROS-Observed: ")
    )
    expected_refs = sorted([collected_ref, receipt_ref])
    if trailers != expected_refs:
        raise VerificationError("automatic observed trailers differ")
    packet = restart.get("packet")
    if not isinstance(packet, dict) or packet.get("unread_returns") != []:
        raise VerificationError("restart has unread returns")
    recent = packet.get("recent_evidence_delta")
    if not isinstance(recent, list) or not recent or not isinstance(recent[0], dict):
        raise VerificationError("restart lacks recent checkpoint")
    if recent[0].get("commit") != final_commit:
        raise VerificationError("restart recent commit differs")
    if recent[0].get("observed_refs") != expected_refs:
        raise VerificationError("restart observed refs differ")
    if recent[0].get("paths") != _SEMANTIC_PATHS:
        raise VerificationError("restart recent paths differ")

    tree_paths = _git_text(project, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
    forbidden_files = {"proposal" + ".json", "admission" + ".json"}
    if any(path.startswith("transitions/") or Path(path).name in forbidden_files for path in tree_paths):
        raise VerificationError("removed research-control artifact exists")
    if len(list((project / "tasks").glob("TASK-*/collected.json"))) != 1:
        raise VerificationError("Task count differs")
    if len(list((project / "eval/evaluations").glob("EVAL-*/receipt.json"))) != 1:
        raise VerificationError("Eval count differs")
    package_value = evidence.get("package_root")
    if not isinstance(package_value, str):
        raise VerificationError("package root is missing")
    package = Path(package_value)
    removed_modules = [
        "transitions.py",
        "transition_" + "index.py",
        "checkpoint_" + "bridge.py",
        "operational.py",
        "research_" + "tool.py",
    ]
    if any((package / name).exists() for name in removed_modules):
        raise VerificationError("removed module exists in installed package")
    commands = evidence.get("commands")
    if not isinstance(commands, list) or not commands:
        raise VerificationError("command receipts are missing")
    if any(not isinstance(item, dict) or item.get("returncode") != 0 for item in commands):
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
    except (OSError, ValueError, UnicodeError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
