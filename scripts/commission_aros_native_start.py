from __future__ import annotations

import argparse
import gc
import hashlib
import importlib.util
import json
import subprocess
import sys
import weakref
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
PROVIDER = ROOT / "commissioning/native_start/provider.py"
VERIFIER = ROOT / "scripts/verify_aros_native_start_commissioning.py"


class CommissioningError(RuntimeError):
    pass


def _provider_class() -> type[Any]:
    spec = importlib.util.spec_from_file_location("aros_native_start_provider", PROVIDER)
    if spec is None or spec.loader is None:
        raise CommissioningError("cannot load native-start provider")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    value = getattr(module, "NativeStartProvider", None)
    if not isinstance(value, type):
        raise CommissioningError("native-start provider is invalid")
    return value


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise CommissioningError((result.stderr or result.stdout).strip())
    return result.stdout.strip()


def _json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise CommissioningError(f"JSON object required: {path}")
    return value


def _message_sha256(messages: object) -> str:
    raw = json.dumps(
        messages,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def commission(aros: Path, runtime: Path) -> Path:
    aros = aros.resolve(strict=True)
    if Path(sys.executable).resolve().parent != aros.parent.resolve():
        raise CommissioningError("driver must run under the clean-wheel interpreter")
    arbor = aros.with_name("arbor")
    arbor.resolve(strict=True)
    runtime = runtime.absolute()
    if runtime.exists():
        raise CommissioningError(f"runtime must not already exist: {runtime}")
    runtime.mkdir(parents=True)
    inputs = runtime / "inputs"
    inputs.mkdir()
    paper = inputs / "paper.md"
    question = "What mechanism explains the deterministic local observation?"
    paper_content = "# Local observation\n\nThe deterministic mediator changed the output.\n"
    paper.write_text(paper_content, encoding="utf-8")
    digest = hashlib.sha256(paper_content.encode("utf-8")).hexdigest()
    source_id = f"SRC-{digest[:16]}"
    source_ref = f"sources/papers/{source_id}/extracted.md"
    project = runtime / "project"

    from typer.testing import CliRunner

    from arbor.aros.principal import build_principal_agent as native_build
    from arbor.cli.commands import aros_cmd

    provider_type = _provider_class()
    provider_queue = [
        provider_type(source_ref=source_ref),
        provider_type(source_ref=source_ref, restart=True),
    ]
    agents: list[tuple[object, int]] = []

    def create_provider(config: object) -> object:
        del config
        if not provider_queue:
            raise CommissioningError("unexpected provider creation")
        return provider_queue.pop(0)

    def capture_build(*args: object, **kwargs: object) -> object:
        agent = native_build(*args, **kwargs)
        agents.append((agent, len(agent.messages)))
        return agent

    aros_cmd.llm_defaults = lambda: {}
    aros_cmd.create_provider = create_provider
    aros_cmd.build_principal_agent = capture_build
    runner = CliRunner()
    primary_result = runner.invoke(
        aros_cmd.aros_app,
        [
            "start",
            "Inspect the exact local Question and source.",
            "--cwd",
            str(project),
            "--question",
            question,
            "--material",
            str(paper),
            "--cooperative-human-direct",
            "--max-turns",
            "8",
        ],
    )
    if primary_result.exit_code != 0 or len(agents) != 1:
        raise CommissioningError(
            primary_result.output or f"primary start exited {primary_result.exit_code}"
        )
    primary_agent, primary_initial_messages = agents.pop()
    primary_evidence = {
        "class": f"{type(primary_agent).__module__}.{type(primary_agent).__qualname__}",
        "initial_message_count": primary_initial_messages,
        "stop_reason": primary_agent.stop_reason,
        "tool_uses": json.loads(json.dumps(primary_agent.tool_uses)),
        "message_sha256": _message_sha256(primary_agent.messages),
    }
    primary_ref = weakref.ref(primary_agent)
    del primary_agent
    gc.collect()
    if primary_ref() is not None:
        raise CommissioningError("primary Agent survived destruction")

    restart_result = runner.invoke(
        aros_cmd.aros_app,
        [
            "start",
            "Recover the initialized research state.",
            "--cwd",
            str(project),
            "--cooperative-human-direct",
            "--max-turns",
            "4",
        ],
    )
    if restart_result.exit_code != 0 or len(agents) != 1:
        raise CommissioningError(
            restart_result.output or f"restart exited {restart_result.exit_code}"
        )
    restart_agent, restart_initial_messages = agents.pop()
    restart_evidence = {
        "initial_message_count": restart_initial_messages,
        "stop_reason": restart_agent.stop_reason,
        "tool_uses": json.loads(json.dumps(restart_agent.tool_uses)),
        "message_sha256": _message_sha256(restart_agent.messages),
    }

    commit = _git(project, "rev-parse", "HEAD")
    metadata_ref = f"sources/papers/{source_id}/metadata.json"
    metadata = _json(project / metadata_ref)
    evidence = {
        "schema_version": 1,
        "project": str(project),
        "commit": commit,
        "question": question,
        "recorded_question": question,
        "question_path": "questions/Q-0001/question.md",
        "source": {
            "source_id": source_id,
            "content_sha256": digest,
            "original_ref": f"sources/papers/{source_id}/original.md",
            "extracted_ref": source_ref,
            "metadata_ref": metadata_ref,
            "metadata": metadata,
        },
        "agent": primary_evidence,
        "restart": restart_evidence,
        "aros_executable": str(aros),
        "arbor_executable": str(arbor),
        "commands": [
            {"name": "primary", "returncode": primary_result.exit_code},
            {"name": "restart", "returncode": restart_result.exit_code},
        ],
    }
    evidence_path = runtime / "evidence.json"
    evidence_path.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    verification = subprocess.run(
        [sys.executable, str(VERIFIER), str(evidence_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if verification.returncode != 0:
        raise CommissioningError(verification.stderr.strip())
    print(verification.stdout.strip())
    return evidence_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--aros", required=True, type=Path)
    parser.add_argument("--runtime", required=True, type=Path)
    args = parser.parse_args()
    try:
        evidence = commission(args.aros, args.runtime)
    except (OSError, ValueError, CommissioningError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(json.dumps({"state": "commissioned", "evidence": str(evidence)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
