"""Real external Principal commissioning boundaries."""

from __future__ import annotations

import ast
import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType


ROOT = Path(__file__).resolve().parent.parent
DRIVER = ROOT / "scripts/commission_aros_real_principal.py"
VERIFIER = ROOT / "scripts/verify_aros_real_principal_commissioning.py"


def _module(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_real_principal_driver_uses_real_provider_and_one_attempt() -> None:
    source = DRIVER.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imports.update(
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    )

    assert "NativeStartProvider" not in source
    assert "PrincipalLoopProvider" not in source
    assert "create_provider =" not in source
    assert "TaskTool" not in source
    assert "EvalTool" not in source
    assert "ResearchTool" not in source
    assert source.count("runner.invoke(") == 2
    assert '"--model",\n            "gpt-5.6-luna"' in source
    assert '"--reasoning-effort",\n            "max"' in source
    assert '"--max-turns",\n            "40"' in source
    assert '"--max-turns",\n            "6"' in source
    assert "while True" not in source
    assert "for attempt" not in source
    assert not imports & {
        "commissioning.native_start.provider",
        "commissioning.principal_loop.provider",
    }


def test_real_principal_driver_does_not_mutate_project_after_agent_start() -> None:
    source = DRIVER.read_text(encoding="utf-8")
    before, after = source.split("principal_started = True", maxsplit=1)

    assert "initialize_knowledge_bank" in before
    assert "eval/suites/real-principal/1/manifest.json" in before
    assert "write_text(" not in after
    assert "write_bytes(" not in after
    assert "git add" not in after
    assert "git commit" not in after


def test_real_principal_driver_reuses_current_task_and_evaluator_fixtures() -> None:
    source = DRIVER.read_text(encoding="utf-8")

    assert "commissioning/principal_loop/task_adapter.py" in source
    assert "commissioning/principal_loop/evaluation/score.py" in source
    assert "commissioning/real_principal/task_adapter.py" in source
    assert "commissioning/real_principal/evaluation/score.py" in source


def test_real_principal_driver_exposes_bounded_cli() -> None:
    result = subprocess.run(
        [sys.executable, str(DRIVER), "--help"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "--aros" in result.stdout
    assert "--runtime" in result.stdout
    assert "--human-review" in result.stdout
    assert "retry" not in result.stdout.lower()


def _valid_tool_uses() -> list[dict[str, object]]:
    return [
        {"name": "Research", "input": {"action": "attention"}},
        {"name": "Read", "input": {"file_path": "questions/Q-0001/question.md"}},
        {"name": "Write", "input": {"file_path": "model/CURRENT.md", "content": "model"}},
        {
            "name": "Write",
            "input": {"file_path": "ideas/I-0001-real-principal.md", "content": "idea"},
        },
        {"name": "Research", "input": {"action": "transition_audit"}},
        {"name": "Research", "input": {"action": "checkpoint"}},
        {"name": "Task", "input": {"action": "create"}},
        {"name": "Task", "input": {"action": "start"}},
        {"name": "Task", "input": {"action": "status"}},
        {"name": "Task", "input": {"action": "collect"}},
        {"name": "Eval", "input": {"action": "register"}},
        {"name": "Eval", "input": {"action": "run"}},
        {"name": "Research", "input": {"action": "attention"}},
        {"name": "Write", "input": {"file_path": "questions/Q-0001/question.md", "content": "q"}},
        {"name": "Write", "input": {"file_path": "model/CURRENT.md", "content": "model2"}},
        {
            "name": "Write",
            "input": {"file_path": "ideas/I-0001-real-principal.md", "content": "idea2"},
        },
        {"name": "Write", "input": {"file_path": "knowledge/claims/C-0001.md", "content": "claim"}},
        {"name": "Write", "input": {"file_path": "memory/NOW.md", "content": "now"}},
        {
            "name": "Write",
            "input": {"file_path": "transitions/T-REAL-ASSIMILATE/proposal.json", "content": "{}"},
        },
        {"name": "Research", "input": {"action": "transition_audit"}},
        {"name": "Research", "input": {"action": "checkpoint"}},
    ]


def test_real_principal_verifier_requires_exact_model_budget_and_tool_counts() -> None:
    module = _module(VERIFIER, "aros_real_principal_verifier")
    primary = {
        "provider": "openai-responses",
        "model": "gpt-5.6-luna",
        "reasoning_effort": "max",
        "turns": 21,
        "input_tokens": 100,
        "output_tokens": 20,
        "tool_uses": _valid_tool_uses(),
    }

    module._validate_primary_budget(primary)

    primary["model"] = "fallback-model"
    try:
        module._validate_primary_budget(primary)
    except module.VerificationError as error:
        assert "model" in str(error)
    else:
        raise AssertionError("verifier accepted a fallback model")


def test_real_principal_verifier_extracts_complete_write_payloads_only() -> None:
    module = _module(VERIFIER, "aros_real_principal_verifier_writes")

    writes = module._semantic_writes(_valid_tool_uses())

    assert writes["model/CURRENT.md"] == [b"model", b"model2"]
    assert writes["knowledge/claims/C-0001.md"] == [b"claim"]
    with_edit = [*_valid_tool_uses(), {"name": "Edit", "input": {}}]
    try:
        module._semantic_writes(with_edit)
    except module.VerificationError as error:
        assert "Edit" in str(error)
    else:
        raise AssertionError("verifier accepted semantic Edit provenance")


def test_real_principal_verifier_cli_exposes_evidence_and_human_review() -> None:
    result = subprocess.run(
        [sys.executable, str(VERIFIER), "--help"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "evidence" in result.stdout.lower()
    assert "--human-review" in result.stdout
