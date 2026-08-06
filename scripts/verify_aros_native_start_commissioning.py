from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path


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
        raise VerificationError(f"Git command failed: {' '.join(args)}")
    return result.stdout


def _help(executable: Path) -> str:
    result = subprocess.run(
        [str(executable), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise VerificationError(f"help command failed: {executable}")
    return result.stdout


def _validate_navigation(frontier: bytes, now: bytes, metadata_ref: str) -> None:
    if (
        b"focus_question: Q-0001" not in frontier
        or b"questions/Q-0001/question.md" not in now
        or metadata_ref.encode("utf-8") not in now
    ):
        raise VerificationError("KnowledgeBank navigation is incomplete")


def verify(evidence_path: Path) -> dict[str, object]:
    evidence = _object(evidence_path)
    if evidence.get("schema_version") != 1:
        raise VerificationError("evidence schema_version must be 1")
    question = evidence.get("question")
    if not isinstance(question, str) or evidence.get("recorded_question") != question:
        raise VerificationError("recorded question differs from expected question")
    project_value = evidence.get("project")
    if not isinstance(project_value, str):
        raise VerificationError("project path is missing")
    project = Path(project_value).resolve(strict=True)
    commit = evidence.get("commit")
    if not isinstance(commit, str) or _git(project, "rev-parse", "HEAD").decode().strip() != commit:
        raise VerificationError("initialization commit differs from HEAD")
    author = _git(project, "log", "-1", "--format=%an <%ae>").decode().strip()
    if author != "AROS Intake <aros-intake@local.invalid>":
        raise VerificationError("initialization commit author is invalid")

    question_ref = evidence.get("question_path")
    if not isinstance(question_ref, str):
        raise VerificationError("question path is invalid")
    question_bytes = _git(project, "show", f"{commit}:{question_ref}")
    if question.encode("utf-8") not in question_bytes:
        raise VerificationError("question bytes are absent from canonical Question")

    source = evidence.get("source")
    if not isinstance(source, dict):
        raise VerificationError("source section is missing")
    for field in ("original_ref", "extracted_ref", "metadata_ref", "content_sha256"):
        if not isinstance(source.get(field), str):
            raise VerificationError(f"source {field} is invalid")
    original = _git(project, "show", f"{commit}:{source['original_ref']}")
    if hashlib.sha256(original).hexdigest() != source["content_sha256"]:
        raise VerificationError("source original hash differs")
    metadata = json.loads(_git(project, "show", f"{commit}:{source['metadata_ref']}"))
    if not isinstance(metadata, dict) or metadata.get("content_sha256") != source["content_sha256"]:
        raise VerificationError("source metadata hash differs")
    extracted = _git(project, "show", f"{commit}:{source['extracted_ref']}")
    if not extracted.strip():
        raise VerificationError("source extraction is empty")
    _validate_navigation(
        _git(project, "show", f"{commit}:questions/FRONTIER.md"),
        _git(project, "show", f"{commit}:memory/NOW.md"),
        str(source["metadata_ref"]),
    )
    model = _git(project, "show", f"{commit}:model/CURRENT.md")
    if b"No explanatory model has been admitted" not in model:
        raise VerificationError("bootstrap model contains invented meaning")

    tree = _git(project, "ls-tree", "-r", "--name-only", commit).decode().splitlines()
    if any(path.startswith("ideas/I-") or path.startswith("knowledge/claims/C-") for path in tree):
        raise VerificationError("bootstrap invented an Idea or Claim")

    primary = evidence.get("agent")
    restart = evidence.get("restart")
    if not isinstance(primary, dict) or not isinstance(restart, dict):
        raise VerificationError("Agent evidence is incomplete")
    if primary.get("class") != "arbor.core.agent.Agent":
        raise VerificationError("native Agent class is invalid")
    expected_reads = [question_ref, source["extracted_ref"]]
    reads = [
        item.get("input", {}).get("file_path")
        for item in primary.get("tool_uses", [])
        if isinstance(item, dict) and item.get("name") == "Read"
    ]
    if reads != expected_reads:
        raise VerificationError("Agent did not read exact Question and source")
    if restart.get("initial_message_count") != 0:
        raise VerificationError("restart reused prior messages")
    if restart.get("tool_uses") != [
        {"name": "Research", "input": {"action": "attention"}}
    ]:
        raise VerificationError("restart did not recover through Attention")

    aros_value = evidence.get("aros_executable")
    arbor_value = evidence.get("arbor_executable")
    if not isinstance(aros_value, str) or not isinstance(arbor_value, str):
        raise VerificationError("entry executable paths are missing")
    aros_help = _help(Path(aros_value))
    if "start" not in aros_help or "init" in {
        line.strip().split()[0] for line in aros_help.splitlines() if line.strip()
    }:
        raise VerificationError("direct AROS command surface is invalid")
    if "aros" in _help(Path(arbor_value)).split():
        raise VerificationError("legacy arbor still exposes AROS")
    return {
        "schema_version": 1,
        "state": "verified",
        "commit": commit,
        "question_id": "Q-0001",
        "source_id": source.get("source_id"),
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
