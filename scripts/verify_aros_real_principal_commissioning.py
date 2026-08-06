from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")


class VerificationError(ValueError):
    pass


def _object(path: Path) -> dict[str, object]:
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
        detail = (result.stderr or result.stdout).decode(errors="replace").strip()
        raise VerificationError(detail or f"Git command failed: {' '.join(args)}")
    return result.stdout


def _git_text(project: Path, *args: str) -> str:
    return _git(project, *args).decode("utf-8").strip()


def _tool_uses(primary: dict[str, object]) -> list[dict[str, object]]:
    value = primary.get("tool_uses")
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise VerificationError("primary tool_uses are invalid")
    return value  # type: ignore[return-value]


def _tool_identity(item: dict[str, object]) -> tuple[str, str | None]:
    name = item.get("name")
    tool_input = item.get("input")
    if not isinstance(name, str) or not isinstance(tool_input, dict):
        raise VerificationError("tool use is invalid")
    selector = tool_input.get("action") or tool_input.get("file_path")
    return name, selector if isinstance(selector, str) else None


def _validate_primary_budget(primary: dict[str, object]) -> None:
    if primary.get("provider") != "openai-responses":
        raise VerificationError("primary provider differs")
    if primary.get("model") != "gpt-5.6-luna":
        raise VerificationError("primary model differs")
    if primary.get("reasoning_effort") != "max":
        raise VerificationError("primary reasoning effort differs")
    turns = primary.get("turns")
    if isinstance(turns, bool) or not isinstance(turns, int) or not 1 <= turns <= 40:
        raise VerificationError("primary turn budget differs")
    for field in ("input_tokens", "output_tokens"):
        value = primary.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise VerificationError(f"primary {field} is invalid")
    if primary.get("input_tokens") == 0:
        raise VerificationError("primary contains no real provider usage")
    identities = [_tool_identity(item) for item in _tool_uses(primary)]
    if identities.count(("Task", "create")) != 1:
        raise VerificationError("primary must create exactly one Task")
    if identities.count(("Task", "collect")) != 1:
        raise VerificationError("primary must collect exactly one Task")
    if identities.count(("Eval", "register")) != 1:
        raise VerificationError("primary must register exactly one Eval")
    if identities.count(("Eval", "run")) != 1:
        raise VerificationError("primary must run exactly one Eval")
    if identities.count(("Research", "checkpoint")) != 2:
        raise VerificationError("primary must perform exactly two checkpoints")
    if identities.count(("Research", "transition_audit")) != 2:
        raise VerificationError("primary must perform exactly two audits")
    if any(name in {"Bash", "Run"} for name, _ in identities):
        raise VerificationError("primary used a forbidden direct execution tool")


def _semantic_writes(
    tool_uses: list[dict[str, object]],
) -> dict[str, list[bytes]]:
    writes: dict[str, list[bytes]] = {}
    for item in tool_uses:
        name = item.get("name")
        if name == "Edit":
            raise VerificationError("semantic Edit provenance is not accepted")
        if name != "Write":
            continue
        tool_input = item.get("input")
        if not isinstance(tool_input, dict):
            raise VerificationError("Write input is invalid")
        path = tool_input.get("file_path")
        content = tool_input.get("content")
        if not isinstance(path, str) or not isinstance(content, str):
            raise VerificationError("Write payload is invalid")
        writes.setdefault(path, []).append(content.encode("utf-8"))
    return writes


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
    ).encode("utf-8")
    if hashlib.sha256(raw).hexdigest() != observed:
        raise VerificationError(f"{field} mismatch")
    return observed


def _single_path(paths: list[str], pattern: re.Pattern[str], label: str) -> str:
    matches = [path for path in paths if pattern.fullmatch(path)]
    if len(matches) != 1:
        raise VerificationError(f"expected one {label}, found {len(matches)}")
    return matches[0]


def _require_headings(raw: bytes, headings: tuple[str, ...], label: str) -> str:
    text = raw.decode("utf-8")
    for heading in headings:
        if f"## {heading}" not in text:
            raise VerificationError(f"{label} lacks heading {heading}")
    if "Not yet assessed" in text:
        raise VerificationError(f"{label} retains placeholder meaning")
    return text


def _render_human_review(
    *,
    evidence: dict[str, object],
    question: str,
    prereg_commit: str,
    final_commit: str,
    task_ref: str,
    eval_ref: str,
    model: str,
    idea: str,
    claim: str,
    now: str,
    restart_text: str,
) -> str:
    def clip(value: str, limit: int = 4000) -> str:
        return value if len(value) <= limit else value[:limit] + "\n[truncated]\n"

    primary = evidence["primary"]
    assert isinstance(primary, dict)
    return (
        "# AROS Real Principal Human Review\n\n"
        "## Mechanical result\n\n"
        "- verifier: `verified`\n"
        "- enforcement: `cooperative`\n"
        f"- model: `{primary['model']}`\n"
        f"- reasoning effort: `{primary['reasoning_effort']}`\n"
        f"- primary turns: `{primary['turns']}`\n"
        f"- tokens: input `{primary['input_tokens']}`, output `{primary['output_tokens']}`\n"
        f"- preregistration commit: `{prereg_commit}`\n"
        f"- final commit: `{final_commit}`\n"
        f"- Task observation: `{task_ref}`\n"
        f"- Eval observation: `{eval_ref}`\n\n"
        "## Human Question\n\n"
        f"{question}\n\n"
        "## Final ScientificModel\n\n```markdown\n"
        f"{clip(model)}\n```\n\n"
        "## Final Idea\n\n```markdown\n"
        f"{clip(idea)}\n```\n\n"
        "## Final Claim\n\n```markdown\n"
        f"{clip(claim)}\n```\n\n"
        "## Final NOW\n\n```markdown\n"
        f"{clip(now)}\n```\n\n"
        "## Fresh Principal explanation\n\n"
        f"{clip(restart_text)}\n\n"
        "## Human decision\n\n"
        "- [ ] accept scientific coherence and scope\n"
        "- [ ] reject\n\n"
        "Reason:\n"
    )


def verify(evidence_path: Path, human_review: Path) -> dict[str, object]:
    evidence = _object(evidence_path)
    if evidence.get("schema_version") != 1:
        raise VerificationError("evidence schema_version must be 1")
    if evidence.get("enforcement_class") != "cooperative":
        raise VerificationError("real Principal evidence must be cooperative")
    primary = evidence.get("primary")
    restart = evidence.get("restart")
    if not isinstance(primary, dict) or not isinstance(restart, dict):
        raise VerificationError("Agent evidence sections are incomplete")
    if primary.get("class") != "arbor.core.agent.Agent":
        raise VerificationError("primary is not the native Agent")
    if primary.get("initial_message_count") != 0 or primary.get("stop_reason") != "finished":
        raise VerificationError("primary session boundary is invalid")
    _validate_primary_budget(primary)
    tool_uses = _tool_uses(primary)
    writes = _semantic_writes(tool_uses)

    project_value = evidence.get("project")
    if not isinstance(project_value, str):
        raise VerificationError("project path is missing")
    project = Path(project_value).resolve(strict=True)
    final_commit = evidence.get("final_commit")
    if not isinstance(final_commit, str) or _COMMIT.fullmatch(final_commit) is None:
        raise VerificationError("final commit is invalid")
    if _git_text(project, "rev-parse", "HEAD") != final_commit:
        raise VerificationError("final commit differs from HEAD")
    checkpoints = primary.get("checkpoint_commits")
    if (
        not isinstance(checkpoints, list)
        or len(checkpoints) != 2
        or any(not isinstance(item, str) or _COMMIT.fullmatch(item) is None for item in checkpoints)
    ):
        raise VerificationError("primary checkpoint commits are invalid")
    prereg_commit, observed_final = checkpoints
    assert isinstance(prereg_commit, str) and isinstance(observed_final, str)
    if observed_final != final_commit:
        raise VerificationError("final checkpoint differs from canonical HEAD")
    if subprocess.run(
        ["git", "-C", str(project), "merge-base", "--is-ancestor", prereg_commit, final_commit],
        check=False,
    ).returncode != 0:
        raise VerificationError("preregistration is not ancestral to final commit")

    tree_paths = _git_text(project, "ls-tree", "-r", "--name-only", final_commit).splitlines()
    task_ref = _single_path(
        tree_paths,
        re.compile(r"tasks/TASK-[^/]+/collected\.json"),
        "Task collection",
    )
    eval_ref = _single_path(
        tree_paths,
        re.compile(r"eval/evaluations/EVAL-[0-9a-f]{64}/receipt\.json"),
        "Eval receipt",
    )
    collected = json.loads(_git(project, "show", f"{final_commit}:{task_ref}"))
    receipt = json.loads(_git(project, "show", f"{final_commit}:{eval_ref}"))
    if not isinstance(collected, dict) or not isinstance(receipt, dict):
        raise VerificationError("Task/Eval records are invalid")
    _record_hash(collected, "collected_sha256")
    _record_hash(receipt, "receipt_sha256")
    if collected.get("child_commit") != receipt.get("candidate_commit"):
        raise VerificationError("Task C differs from measurement candidate")
    if receipt.get("measurement_state") != "valid" or receipt.get("metric") != 1.0:
        raise VerificationError("measurement is not the exact valid fixture metric")

    required_paths = (
        "questions/Q-0001/question.md",
        "model/CURRENT.md",
        "ideas/I-0001-real-principal.md",
        "knowledge/claims/C-0001.md",
        "memory/NOW.md",
        "transitions/T-REAL-ASSIMILATE/proposal.json",
    )
    for path in required_paths:
        payloads = writes.get(path)
        if not payloads:
            raise VerificationError(f"missing Agent Write provenance: {path}")
        if _git(project, "show", f"{final_commit}:{path}") != payloads[-1]:
            raise VerificationError(f"final Git blob differs from Agent Write: {path}")
    for path in ("model/CURRENT.md", "ideas/I-0001-real-principal.md"):
        payloads = writes.get(path)
        assert payloads
        if _git(project, "show", f"{prereg_commit}:{path}") != payloads[0]:
            raise VerificationError(f"preregistration differs from Agent Write: {path}")

    question_text = _require_headings(
        _git(project, "show", f"{final_commit}:questions/Q-0001/question.md"),
        (
            "Current best answer",
            "Current uncertainty",
            "Evidence that would change the answer",
            "Resolution criterion",
            "Stop / pivot criterion",
        ),
        "Question",
    )
    model_text = _require_headings(
        _git(project, "show", f"{final_commit}:model/CURRENT.md"),
        (
            "Scope and boundary",
            "Proposed mechanism",
            "Rival or null",
            "Premeasurement prediction",
            "Observed result and apparatus limits",
            "Remaining uncertainty",
            "Evidence and artifact refs",
        ),
        "ScientificModel",
    )
    idea_text = _require_headings(
        _git(project, "show", f"{final_commit}:ideas/I-0001-real-principal.md"),
        (
            "Target question",
            "Why worth testing",
            "Expected observations under focal and rival",
            "Minimal controls and evaluator",
            "What failure would teach",
            "Task and Eval links",
        ),
        "Idea",
    )
    claim_text = _require_headings(
        _git(project, "show", f"{final_commit}:knowledge/claims/C-0001.md"),
        ("Statement and scope", "Evidence links", "Counterevidence", "Assumptions", "Uncertainty and alternatives"),
        "Claim",
    )
    now_text = _git(project, "show", f"{final_commit}:memory/NOW.md").decode("utf-8")
    for text, label in (
        (model_text, "Model"),
        (idea_text, "Idea"),
        (claim_text, "Claim"),
        (now_text, "NOW"),
    ):
        if task_ref not in text and eval_ref not in text:
            raise VerificationError(f"{label} lacks Task/Eval refs")
    if eval_ref not in claim_text or '"relation"' not in claim_text:
        raise VerificationError("Claim lacks strict measurement EvidenceLink")

    proposal = json.loads(_git(project, "show", f"{final_commit}:transitions/T-REAL-ASSIMILATE/proposal.json"))
    if not isinstance(proposal, dict):
        raise VerificationError("final proposal is invalid")
    assimilations = proposal.get("assimilations")
    if not isinstance(assimilations, list) or len(assimilations) != 2:
        raise VerificationError("final proposal does not contain two assimilations")
    refs = {item.get("observation_ref") for item in assimilations if isinstance(item, dict)}
    if refs != {task_ref, eval_ref}:
        raise VerificationError("final assimilation refs differ")

    if restart.get("initial_message_count") != 0:
        raise VerificationError("restart reused primary messages")
    if restart.get("provider") != "openai-responses" or restart.get("model") != "gpt-5.6-luna" or restart.get("reasoning_effort") != "max":
        raise VerificationError("restart model triple differs")
    turns = restart.get("turns")
    if isinstance(turns, bool) or not isinstance(turns, int) or not 1 <= turns <= 6:
        raise VerificationError("restart turn budget differs")
    restart_tools = restart.get("tool_uses")
    if not isinstance(restart_tools, list):
        raise VerificationError("restart tools are invalid")
    for item in restart_tools:
        if not isinstance(item, dict):
            raise VerificationError("restart tool use is invalid")
        name, selector = _tool_identity(item)
        if name == "Research" and selector == "attention":
            continue
        if name == "Read":
            continue
        raise VerificationError("restart used a mutating or disallowed tool")
    assistant_texts = restart.get("assistant_texts")
    if not isinstance(assistant_texts, list) or not assistant_texts:
        raise VerificationError("restart explanation is missing")
    restart_text = "\n".join(str(item) for item in assistant_texts)
    lowered = restart_text.lower()
    for term in ("question", "model", "evidence", "uncertainty", "next"):
        if term not in lowered:
            raise VerificationError(f"restart explanation lacks {term}")

    review = _render_human_review(
        evidence=evidence,
        question=question_text,
        prereg_commit=prereg_commit,
        final_commit=final_commit,
        task_ref=task_ref,
        eval_ref=eval_ref,
        model=model_text,
        idea=idea_text,
        claim=claim_text,
        now=now_text,
        restart_text=restart_text,
    )
    human_review.parent.mkdir(parents=True, exist_ok=True)
    human_review.write_text(review, encoding="utf-8")
    return {
        "schema_version": 1,
        "state": "verified",
        "enforcement_class": "cooperative",
        "model": "gpt-5.6-luna",
        "reasoning_effort": "max",
        "preregistration_commit": prereg_commit,
        "commit": final_commit,
        "task_id": collected.get("task_id"),
        "eval_id": receipt.get("eval_id"),
        "human_review": str(human_review),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--human-review", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = verify(args.evidence, args.human_review)
    except (OSError, ValueError, json.JSONDecodeError, UnicodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
