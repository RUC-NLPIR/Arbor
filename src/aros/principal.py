"""Native Principal Agent for an AROS workspace.

This module deliberately wires the existing general-purpose Agent directly to
the workspace.  It does not import the legacy semantic coordinator or any of
its hypothesis-tree execution machinery.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ..core.agent import Agent
from ..core.config import AgentConfig
from ..core.llm.base import LLMProvider
from ..core.tools.base import PathAuthorizer, Tool
from ..core.tools.bash import BashTool
from ..core.tools.file_edit import FileEditTool
from ..core.tools.file_read import FileReadTool
from ..core.tools.file_write import FileWriteTool
from ..core.tools.glob_tool import GlobTool
from ..core.tools.grep import GrepTool
from .run_tool import RunTool
from .workspace import status_workspace


PRINCIPAL_SYSTEM_PROMPT = """\
You are the Principal Research Agent for this project.

Within the human owner's stated mission, constraints, budget, safety, and
publication boundaries, you are the scientific principal: you integrate the
research state, choose actions, interpret evidence, and may directly revise
the versioned workspace, including its memory, questions, models, ideas, code,
analyses, and project-local skills.

The workspace, not this chat transcript or provider session, is durable project
memory. Re-observe it whenever state may have changed and write material
decisions, evidence links, uncertainties, and obligations back before ending a
material turn. The OS does not prescribe a universal research cycle, idea
quota, belief ladder, or scheduler.

Reality has final veto. Treat command output, tests, evaluators, instruments,
datasets, external sources, and human observations as observations; never
manufacture measurements from your own prose. Inspect current state before
acting. Preserve pre-existing dirty work and do not claim completion without
checking the resulting files or observations. Automatic Git commits are
disabled so that only coherent, intentional workspace snapshots are made.

The following boot context is a finite observation of the durable workspace.
Retrieve source files as needed instead of assuming omitted state:

<workspace_root>
{workspace_root}
</workspace_root>

<boot_context>
{boot_context}
</boot_context>
"""


def _workspace_authorizer(root: Path) -> PathAuthorizer:
    """Allow dedicated file tools to access only *root* and its descendants."""
    canonical_root = os.path.realpath(root)

    def authorize(path: str) -> str | None:
        try:
            if os.path.commonpath((canonical_root, path)) == canonical_root:
                return None
        except (OSError, ValueError):
            pass
        return f"path is outside the AROS workspace: {path}"

    return authorize


class InspectTool(Tool):
    """Return deterministic workspace and Git reality to the Principal."""

    name = "Inspect"
    description = (
        "Inspect the current AROS workspace and Git reality, including "
        "initialization, semantic views, HEAD, branch, dirty paths, and worktrees."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    is_read_only = True
    persist_threshold = float("inf")

    async def execute(self, **kwargs: Any) -> str:
        del kwargs
        return json.dumps(status_workspace(self.cwd), ensure_ascii=False, indent=2)


class ForegroundBashTool(BashTool):
    """Trusted-local shell with a bounded timeout and no background API."""

    description = (
        "Execute a trusted-local Bash command in the AROS workspace and wait "
        "for it to finish. Commands are bounded to 600 seconds; background "
        "execution is unavailable. This tool provides audit-friendly process "
        "control, not a security sandbox."
    )

    def __init__(self, *, cwd: str, **kwargs: Any):
        super().__init__(
            cwd=cwd,
            timeout_default=120,
            timeout_max=600,
            **kwargs,
        )
        self.input_schema["properties"].pop("run_in_background", None)

    async def execute(self, **kwargs: Any) -> str:
        if kwargs.get("run_in_background"):
            return "BLOCKED: AROS Principal Bash only supports foreground commands."
        kwargs.pop("run_in_background", None)
        return await super().execute(**kwargs)


def build_principal_agent(
    provider: LLMProvider,
    root: str | Path,
    boot_context: str,
    *,
    max_turns: int = 100,
    allow_shell: bool = False,
) -> Agent:
    """Build the native single-Principal Agent for *root*.

    Shell access is an explicit trusted-local opt-in. Dedicated filesystem tools
    remain confined to the workspace and all tool-result persistence is disabled
    here so the legacy ``.arbor`` runtime can never be created accidentally.
    """
    workspace_root = Path(root).expanduser().resolve()
    runtime_dir = workspace_root / ".aros" / "agent"
    authorizer = _workspace_authorizer(workspace_root)
    tool_kwargs = {
        "cwd": str(workspace_root),
        "path_authorizer": authorizer,
        "persist_results": False,
    }

    tools: list[Tool] = [
        FileReadTool(**tool_kwargs),
        GrepTool(**tool_kwargs),
        GlobTool(**tool_kwargs),
        FileEditTool(**tool_kwargs),
        FileWriteTool(**tool_kwargs),
        InspectTool(cwd=str(workspace_root), persist_results=False),
        RunTool(cwd=str(workspace_root), persist_results=False),
    ]
    if allow_shell:
        tools.append(
            ForegroundBashTool(
                cwd=str(workspace_root),
                persist_results=False,
            )
        )

    config = AgentConfig(
        cwd=str(workspace_root),
        max_turns=max_turns,
        auto_git=False,
        runtime_dir=str(runtime_dir),
        agent_label="principal",
    )
    return Agent(
        provider=provider,
        tools=tools,
        system_prompt=PRINCIPAL_SYSTEM_PROMPT.format(
            workspace_root=workspace_root,
            boot_context=boot_context,
        ),
        config=config,
    )


async def run_principal(agent: Agent, user_message: str) -> str:
    """Run one Principal turn using the existing native Agent loop."""
    return await agent.run(user_message)
