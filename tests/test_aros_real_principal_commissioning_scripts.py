"""Real external Principal commissioning boundaries."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DRIVER = ROOT / "scripts/commission_aros_real_principal.py"
VERIFIER = ROOT / "scripts/verify_aros_real_principal_commissioning.py"


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
