"""Native AROS Principal wiring and filesystem safety tests."""

from __future__ import annotations

import ast
import asyncio
import copy
import subprocess
from pathlib import Path
from typing import Any

import pytest

from arbor.aros.attention import AttentionAuthorityContext
from arbor.aros.principal import (
    build_principal_agent,
    run_principal,
)
from arbor.aros.research_tool import ResearchTool
from arbor.aros.worktrees import bind_repository
from arbor.core.agent import Agent
from arbor.core.llm.base import LLMResponse, TextBlock, ToolUseBlock, Usage
from arbor.core.tools.file_edit import FileEditTool
from arbor.core.tools.file_write import FileWriteTool


class _ScriptedProvider:
    model = "scripted-principal"
    base_url = None

    def __init__(self, responses: list[LLMResponse] | None = None):
        self.responses = list(responses or [])
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> LLMResponse:
        self.calls.append(copy.deepcopy(kwargs))
        if not self.responses:
            raise AssertionError("unexpected LLM call")
        return self.responses.pop(0)

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)


def _workspace(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "principal@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(root), "config", "user.name", "Principal Test"],
        check=True,
    )
    (root / ".gitignore").write_text("/.aros/\n/.worktree/\n", encoding="utf-8")
    (root / "AROS.md").write_text("# AROS\n", encoding="utf-8")
    (root / "memory").mkdir()
    (root / "memory" / "NOW.md").write_text("# Current State\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(root), "add", ".gitignore", "AROS.md", "memory/NOW.md"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(root), "commit", "-qm", "initial principal workspace"],
        check=True,
    )
    return root


def _tool_response(name: str, tool_input: dict[str, Any]) -> LLMResponse:
    return LLMResponse(
        content=[ToolUseBlock(id="tool-1", name=name, input=tool_input)],
        stop_reason="tool_use",
        usage=Usage(),
        raw_content=[{
            "type": "tool_use",
            "id": "tool-1",
            "name": name,
            "input": tool_input,
        }],
    )


def _text_response(text: str) -> LLMResponse:
    return LLMResponse(
        content=[TextBlock(text=text)],
        stop_reason="end_turn",
        usage=Usage(),
        raw_content=[{"type": "text", "text": text}],
    )


def test_build_principal_uses_native_agent_and_exact_default_tools(tmp_path: Path):
    root = _workspace(tmp_path)
    agent = build_principal_agent(
        _ScriptedProvider(),
        root,
        "mission: understand the system",
    )

    assert isinstance(agent, Agent)
    assert set(agent.tools) == {
        "Read",
        "Grep",
        "Glob",
        "Edit",
        "Write",
        "Research",
        "Eval",
        "Run",
        "Task",
    }
    assert agent.tools["Eval"].persist_results is False
    assert agent.tools["Task"].persist_results is False
    assert agent.config.auto_git is False
    assert agent.config.runtime_dir == str(root / ".aros" / "agent")
    assert (root / ".aros" / "agent").is_dir()
    assert not (root / ".arbor").exists()
    assert str(root) in agent.system_prompt
    assert "mission: understand the system" in agent.system_prompt


def test_principal_prompt_states_the_trusted_local_task_boundary(tmp_path: Path):
    root = _workspace(tmp_path)
    agent = build_principal_agent(_ScriptedProvider(), root, "boot")
    prompt = " ".join(agent.system_prompt.lower().split())

    assert "trusted-local and application-scoped, not a security sandbox" in prompt
    assert (
        "network and shell capability flags are audit declarations and are not enforced"
    ) in prompt
    assert "secrets and untrusted adapters are unsupported" in prompt
    assert (
        "daemonizing or new-session descendants that do not drain fail closed as lost "
        "with no terminal receipt"
    ) in prompt


def test_principal_shell_is_opt_in_bounded_and_foreground_only(tmp_path: Path):
    root = _workspace(tmp_path)
    agent = build_principal_agent(
        _ScriptedProvider(),
        root,
        "boot",
        allow_shell=True,
    )

    assert set(agent.tools) == {
        "Read",
        "Grep",
        "Glob",
        "Edit",
        "Write",
        "Research",
        "Eval",
        "Run",
        "Task",
        "Bash",
    }
    bash = agent.tools["Bash"]
    assert "run_in_background" not in bash.input_schema["properties"]
    assert bash.timeout_max == 600
    result = asyncio.run(
        bash.execute(command="printf should-not-run", run_in_background=True)
    )
    assert result.startswith("BLOCKED:")


def test_principal_eval_tool_states_measurement_interpretation_and_no_retry(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    agent = build_principal_agent(_ScriptedProvider(), root, "boot")
    description = " ".join(agent.tools["Eval"].description.lower().split())

    assert "apparatus produces factual measurements" in description
    assert "principal interprets" in description
    assert "lost evaluations are never retried" in description
    for unavailable in ("admit", "protected", "administrator", "mcp"):
        assert unavailable not in description


def test_principal_research_defaults_to_attached_workspace_without_authority(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    binding = bind_repository(root)

    agent = build_principal_agent(_ScriptedProvider(), root, "boot")

    research = agent.tools["Research"]
    assert isinstance(research, ResearchTool)
    assert research.candidate_root == root
    assert research.canonical_repository == binding
    assert research.canonical_ref == "refs/heads/main"
    assert research.attention_context is None
    assert research.checkpoint_service.gateway is None
    assert agent.tools["Task"].operational_admission is None


def test_principal_injects_explicit_research_host_context_without_schema_change(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    binding = bind_repository(root)
    gateway = object()
    context = AttentionAuthorityContext(
        authority={"state": "available"},
        remaining_budget={"state": "available"},
        institutional_obligations=(),
    )
    expected_schema = copy.deepcopy(ResearchTool.input_schema)

    agent = build_principal_agent(
        _ScriptedProvider(),
        root,
        "boot",
        canonical_repository=binding,
        canonical_ref="refs/heads/main",
        admission_gateway=gateway,
        attention_context=context,
    )

    research = agent.tools["Research"]
    assert isinstance(research, ResearchTool)
    assert research.canonical_repository is binding
    assert research.canonical_ref == "refs/heads/main"
    assert research.attention_context is context
    assert research.checkpoint_service.gateway is gateway
    assert research.input_schema == expected_schema
    task = agent.tools["Task"]
    assert callable(task.operational_admission)
    assert task.operational_admission.__self__ is research.checkpoint_service
    assert task.operational_admission.__func__.__name__ == "checkpoint_operational"


def test_principal_directly_writes_workspace_without_legacy_runtime(tmp_path: Path):
    root = _workspace(tmp_path)
    target = root / "artifact.txt"
    provider = _ScriptedProvider([
        _tool_response(
            "Write",
            {"file_path": str(target), "content": "measured observation\n"},
        ),
        _text_response("Artifact written."),
    ])

    agent = build_principal_agent(
        provider,
        root,
        "mission: preserve evidence",
        max_turns=3,
    )
    result = asyncio.run(run_principal(agent, "Write the requested artifact."))

    assert result == "Artifact written."
    assert target.read_text(encoding="utf-8") == "measured observation\n"
    assert not (root / ".arbor").exists()


def test_principal_file_tools_block_outside_workspace_and_symlink_escape(tmp_path: Path):
    root = tmp_path / "project"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    _workspace(root)
    outside_file = outside / "outside.txt"
    outside_file.write_text("unchanged", encoding="utf-8")
    inside = root / "inside.txt"
    inside.write_text("old", encoding="utf-8")
    escape = root / "escape"
    escape.symlink_to(outside, target_is_directory=True)
    agent = build_principal_agent(_ScriptedProvider(), root, "boot")

    write_outside = asyncio.run(
        agent.tools["Write"].execute(
            file_path=str(outside / "new.txt"), content="forbidden"
        )
    )
    edit_outside = asyncio.run(
        agent.tools["Edit"].execute(
            file_path=str(outside_file), old_string="unchanged", new_string="changed"
        )
    )
    write_escape = asyncio.run(
        agent.tools["Write"].execute(
            file_path=str(escape / "escaped.txt"), content="forbidden"
        )
    )

    assert write_outside.startswith("BLOCKED:")
    assert edit_outside.startswith("BLOCKED:")
    assert write_escape.startswith("BLOCKED:")
    assert outside_file.read_text(encoding="utf-8") == "unchanged"
    assert not (outside / "new.txt").exists()
    assert not (outside / "escaped.txt").exists()


def test_write_and_edit_authorize_before_any_filesystem_io(tmp_path: Path, monkeypatch):
    def blocked(_path: str) -> str:
        return "test scope denies this path"

    write = FileWriteTool(cwd=str(tmp_path), path_authorizer=blocked)
    edit = FileEditTool(cwd=str(tmp_path), path_authorizer=blocked)

    def fail(*_args: Any, **_kwargs: Any):
        raise AssertionError("filesystem IO happened before authorization")

    monkeypatch.setattr("arbor.core.tools.file_write.os.makedirs", fail)
    monkeypatch.setattr("arbor.core.tools.file_write.os.path.exists", fail)
    monkeypatch.setattr("arbor.core.tools.file_edit.os.path.exists", fail)

    write_result = asyncio.run(
        write.execute(file_path="blocked/new.txt", content="content")
    )
    edit_result = asyncio.run(
        edit.execute(
            file_path="blocked/current.txt", old_string="old", new_string="new"
        )
    )

    assert write_result.startswith("BLOCKED:")
    assert edit_result.startswith("BLOCKED:")


def test_principal_module_has_no_legacy_control_plane_imports():
    source = Path("src/aros/principal.py").read_text(encoding="utf-8")
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

    forbidden = (
        "coordinator",
        "idea_tree",
        "executor",
        "mcp",
        "semantic_pipeline",
        "auto_git",
    )
    assert not any(part in module for module in imported for part in forbidden)


def test_principal_rejects_detached_default_research_context(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    subprocess.run(
        ["git", "-C", str(root), "checkout", "-q", "--detach"],
        check=True,
    )

    with pytest.raises(ValueError, match="attached canonical Git branch"):
        build_principal_agent(_ScriptedProvider(), root, "boot")
