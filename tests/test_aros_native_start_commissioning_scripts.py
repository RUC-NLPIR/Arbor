"""Clean-wheel native `aros start` commissioning boundaries."""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parent.parent
PROVIDER = ROOT / "commissioning/native_start/provider.py"
DRIVER = ROOT / "scripts/commission_aros_native_start.py"
VERIFIER = ROOT / "scripts/verify_aros_native_start_commissioning.py"


def _module(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _with_result(
    messages: list[dict[str, object]],
    response: object,
    content: str,
) -> list[dict[str, object]]:
    call = response.get_tool_calls()[0]
    return [
        *messages,
        {"role": "assistant", "content": response.raw_content},
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": call.id,
                    "content": content,
                }
            ],
        },
    ]


def test_native_start_provider_is_reality_blind_and_reads_exact_inputs() -> None:
    source = PROVIDER.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported.update(
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    )
    assert not imported & {
        "os",
        "pathlib",
        "subprocess",
        "requests",
        "httpx",
        "arbor.aros",
    }

    module = _module(PROVIDER, "aros_native_start_provider")
    provider = module.NativeStartProvider(
        source_ref="sources/papers/SRC-test/extracted.md"
    )
    messages: list[dict[str, object]] = [{"role": "user", "content": "start"}]
    first = asyncio.run(provider.create(system="boot", messages=messages))
    assert first.get_tool_calls()[0].input == {
        "file_path": "questions/Q-0001/question.md"
    }
    messages = _with_result(messages, first, "question bytes")
    second = asyncio.run(provider.create(system="boot", messages=messages))
    assert second.get_tool_calls()[0].input == {
        "file_path": "sources/papers/SRC-test/extracted.md"
    }
    messages = _with_result(messages, second, "source bytes")
    final = asyncio.run(provider.create(system="boot", messages=messages))
    assert final.get_tool_calls() == []
    assert "Question and local source observed" in final.get_text()


def test_native_start_driver_uses_public_start_without_direct_kb_mutation() -> None:
    source = DRIVER.read_text(encoding="utf-8")
    tree = ast.parse(source)
    called_names = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    assert "CliRunner" in source
    assert '"start"' in source
    assert '"--question"' in source
    assert '"--material"' in source
    assert "initialize_knowledge_bank" not in source
    assert "init_workspace" not in source
    assert "write_text" not in called_names
    assert "write_bytes" not in called_names


def test_native_start_driver_compares_venv_entry_paths_without_resolving_symlinks(
    tmp_path: Path,
) -> None:
    module = _module(DRIVER, "aros_native_start_driver")
    bin_dir = tmp_path / "venv/bin"
    bin_dir.mkdir(parents=True)

    module._require_clean_wheel_interpreter(
        bin_dir / "aros",
        bin_dir / "python",
    )

    try:
        module._require_clean_wheel_interpreter(
            bin_dir / "aros",
            tmp_path / "other/bin/python",
        )
    except module.CommissioningError as error:
        assert "clean-wheel interpreter" in str(error)
    else:
        raise AssertionError("driver accepted a different interpreter directory")


def test_native_start_scripts_expose_one_runtime_and_independent_verifier() -> None:
    driver = subprocess.run(
        [sys.executable, str(DRIVER), "--help"],
        capture_output=True,
        text=True,
    )
    verifier = subprocess.run(
        [sys.executable, str(VERIFIER), "--help"],
        capture_output=True,
        text=True,
    )

    assert driver.returncode == 0
    assert "--runtime" in driver.stdout
    assert verifier.returncode == 0
    assert "evidence" in verifier.stdout.lower()


def test_native_start_verifier_rejects_wrong_question_before_repository_io(
    tmp_path: Path,
) -> None:
    evidence = {
        "schema_version": 1,
        "project": str(tmp_path / "missing"),
        "question": "Expected question?",
        "question_path": "questions/Q-0001/question.md",
        "recorded_question": "Different question?",
    }
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(evidence), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(VERIFIER), str(path)],
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "question" in result.stderr.lower()


def test_native_start_verifier_requires_question_and_source_navigation() -> None:
    module = _module(VERIFIER, "aros_native_start_verifier")
    metadata_ref = "sources/papers/SRC-test/metadata.json"

    module._validate_navigation(
        b"---\nfocus_question: Q-0001\n---\n",
        f"questions/Q-0001/question.md\n{metadata_ref}\n".encode(),
        metadata_ref,
    )

    try:
        module._validate_navigation(
            b"---\nfocus_question:\n---\n",
            b"questions/Q-0001/question.md\n",
            metadata_ref,
        )
    except module.VerificationError as error:
        assert "navigation" in str(error)
    else:
        raise AssertionError("verifier accepted missing KB navigation")
