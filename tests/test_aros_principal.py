"""Native Agent-centric Principal wiring."""

from __future__ import annotations

import asyncio
import copy
import subprocess
from pathlib import Path
from typing import Any

from arbor.aros.attention_tool import AttentionTool
from arbor.aros.checkpoint_tool import CheckpointTool
from arbor.aros.observed import ObservedRefs
from arbor.aros.principal import (
    AROS_DEFAULT_MODEL,
    AROS_DEFAULT_PROVIDER,
    AROS_DEFAULT_REASONING_EFFORT,
    build_principal_agent,
    run_principal,
)
from arbor.aros.workspace import init_workspace
from arbor.core.agent import Agent
from arbor.core.llm.base import LLMResponse, TextBlock, Usage


class _Provider:
    model = "scripted-principal"
    base_url = None

    def __init__(self, responses: list[LLMResponse] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> LLMResponse:
        self.calls.append(copy.deepcopy(kwargs))
        return self.responses.pop(0)

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _workspace(root: Path) -> Path:
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "Principal Test")
    _git(root, "config", "user.email", "principal@example.invalid")
    init_workspace(root, "Understand the load-bearing mechanism.")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "Initialize AROS")
    return root


def test_principal_default_model_triple() -> None:
    assert AROS_DEFAULT_PROVIDER == "openai-responses"
    assert AROS_DEFAULT_MODEL == "gpt-5.6-luna"
    assert AROS_DEFAULT_REASONING_EFFORT == "max"


def test_principal_uses_native_agent_and_five_aros_tools(tmp_path: Path) -> None:
    root = _workspace(tmp_path)

    agent = build_principal_agent(_Provider(), root, "active question: Q-0001")

    assert isinstance(agent, Agent)
    assert set(agent.tools) == {
        "Read",
        "Grep",
        "Glob",
        "Edit",
        "Write",
        "Attention",
        "Task",
        "Run",
        "Eval",
    }
    assert isinstance(agent.tools["Attention"], AttentionTool)
    assert "Checkpoint" not in agent.tools
    assert agent.config.auto_git is False
    assert agent.config.runtime_dir == str(root / ".aros/agent")
    assert "active question: Q-0001" in agent.system_prompt


def test_host_granted_checkpoint_shares_observations_and_git_service(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)

    agent = build_principal_agent(
        _Provider(),
        root,
        "boot",
        allow_checkpoint=True,
    )

    checkpoint_tool = agent.tools["Checkpoint"]
    assert isinstance(checkpoint_tool, CheckpointTool)
    observed = checkpoint_tool.observed
    assert isinstance(observed, ObservedRefs)
    for name in ("Task", "Run", "Eval"):
        tool = agent.tools[name]
        assert tool.record_observation.__self__ is observed
        assert tool.commit_paths.__self__ is checkpoint_tool.checkpoint
        assert tool.commit_paths.__func__.__name__ == "commit_paths"


def test_shell_is_explicit_bounded_and_foreground_only(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    agent = build_principal_agent(_Provider(), root, "boot", allow_shell=True)

    bash = agent.tools["Bash"]
    assert "run_in_background" not in bash.input_schema["properties"]
    assert bash.timeout_max == 600
    result = asyncio.run(
        bash.execute(command="printf should-not-run", run_in_background=True)
    )
    assert result.startswith("BLOCKED:")


def test_run_principal_uses_the_existing_agent_loop(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    response = LLMResponse(
        content=[TextBlock(text="Continue from durable state.")],
        stop_reason="end_turn",
        usage=Usage(),
        raw_content=[{"type": "text", "text": "Continue from durable state."}],
    )
    agent = build_principal_agent(_Provider([response]), root, "boot")

    assert asyncio.run(run_principal(agent, "continue")) == "Continue from durable state."


def test_principal_source_has_no_removed_control_plane() -> None:
    source = Path("src/aros/principal.py").read_text(encoding="utf-8")
    for removed in (
        "AdmissionGateway",
        "ResearchTool",
        "OperationalIntent",
        "transition",
        "assimilation",
    ):
        assert removed not in source
