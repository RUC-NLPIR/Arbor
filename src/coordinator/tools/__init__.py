"""Coordinator tool registry."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ...core.tools.base import Tool
from ...core.tools.bash import BashTool
from ...core.tools.file_read import FileReadTool
from ...core.tools.grep import GrepTool
from ...core.tools.glob_tool import GlobTool
from ...core.tools.skill import LoadSkillTool
from ...core.skill_registry import build_default_registry

from .tree_ops import TreeViewTool, TreeAddNodeTool, TreeUpdateNodeTool, TreePruneTool, TreeSetMetaTool, TreePropagateTool
from .executor_run import RunExecutorTool, RunExecutorParallelTool, ResumeExecutorTool
from .git_ops import GitMergeBranchTool
from .search_ctx import SearchIdeaContextTool, SearchIdeaContextParallelTool, SearchStatusTool
from .research_ctx import ResearchSearchTool
from .ask_user import AskUserTool
from .record_finding import RecordFindingTool
from ..convergence import ConvergenceDetector, ConvergenceConfig

if TYPE_CHECKING:
    from ..config import CoordinatorConfig
    from ..idea_tree import IdeaTree
    from ...core.llm.base import LLMProvider

log = logging.getLogger(__name__)


def get_coordinator_tools(
    tree: "IdeaTree",
    config: "CoordinatorConfig",
    provider: "LLMProvider",
) -> list[Tool]:
    """Return all tools available to the coordinator."""
    cwd = config.cwd
    wdir = config.workspace_dir

    # Create convergence detector — merge plugin config with CoordinatorConfig defaults
    convergence_detector: ConvergenceDetector | None = None
    conv_config = config.convergence
    # Plugin convergence block overrides defaults if present
    if config.plugin and config.plugin.convergence:
        conv_config = ConvergenceConfig.from_dict(config.plugin.convergence)
    if conv_config.enabled:
        convergence_detector = ConvergenceDetector(tree, conv_config)

    skill_registry = (
        build_default_registry(cwd, disabled=set(config.disabled_skills))
        if config.skills_enabled else None
    )

    tools: list[Tool] = [
        # Tree operations
        TreeViewTool(cwd=cwd, tree=tree, workspace_dir=wdir),
        TreeAddNodeTool(cwd=cwd, tree=tree, config=config, provider=provider, workspace_dir=wdir),
        TreeUpdateNodeTool(cwd=cwd, tree=tree, workspace_dir=wdir),
        TreePruneTool(cwd=cwd, tree=tree, workspace_dir=wdir),
        TreeSetMetaTool(cwd=cwd, tree=tree, config=config, workspace_dir=wdir),
        TreePropagateTool(cwd=cwd, tree=tree, provider=provider, workspace_dir=wdir),
        # Executor dispatch (with convergence detection)
        RunExecutorTool(
            cwd=cwd, tree=tree, config=config, provider=provider,
            workspace_dir=wdir, convergence_detector=convergence_detector,
        ),
        RunExecutorParallelTool(
            cwd=cwd, tree=tree, config=config, provider=provider,
            workspace_dir=wdir, convergence_detector=convergence_detector,
        ),
        ResumeExecutorTool(
            cwd=cwd, tree=tree, config=config, provider=provider,
            workspace_dir=wdir, convergence_detector=convergence_detector,
        ),
        # Git
        GitMergeBranchTool(cwd=cwd, config=config, tree=tree, provider=provider, workspace_dir=wdir),
        # Read-only codebase tools (coordinator never writes code directly)
        BashTool(
            cwd=cwd,
            workspace_dir=wdir,
            timeout_default=config.bash_timeout_default,
            timeout_max=config.bash_timeout_max,
        ),
        FileReadTool(cwd=cwd, workspace_dir=wdir),
        GrepTool(cwd=cwd, workspace_dir=wdir),
        GlobTool(cwd=cwd, workspace_dir=wdir),
        RecordFindingTool(cwd=cwd, workspace_dir=wdir),
    ]
    if skill_registry is not None:
        # Skills (read-only reference docs loaded on demand)
        tools.append(LoadSkillTool(cwd=cwd, registry=skill_registry))

    # ── Ask-back (#10): only when the operator allows agent questions ──
    interaction_mode = (getattr(config.ui, "interaction_mode", "auto") or "auto").lower()
    if config.ui.allow_agent_questions or interaction_mode in ("direction", "collaborative"):
        tools.append(AskUserTool(cwd=cwd, tree=tree, config=config, workspace_dir=wdir))

    # ── Web search / browse — only registered if enabled and configured ──
    sc = getattr(config, "search", None)
    if sc is not None and sc.enabled and sc.has_backend:
        from ...core.tools.web.backends import resolve_backend_names
        log.info(
            "search enabled — backends: %s | visit: %s | grounded_ideation: %s",
            ", ".join(resolve_backend_names(sc)) or "(none)",
            (sc.visit_backend or "auto"),
            "on" if sc.grounded_ideation else "off",
        )
        mode = (sc.mode or "executor").lower()
        if mode == "inline":
            # Phase-1 surface: coordinator calls the web tools itself. Backend
            # selection (alphaXiv / Jina / Serper / Exa / endpoint + keyless
            # visit) is centralized in the web-tools factory.
            from ...core.tools.web.factory import (
                build_web_search_tool,
                build_web_visit_tool,
            )
            search_tool = build_web_search_tool(sc, cwd=cwd, workspace_dir=wdir)
            if search_tool is not None:
                tools.append(search_tool)
            visit_tool = build_web_visit_tool(sc, cwd=cwd, workspace_dir=wdir)
            if visit_tool is not None:
                tools.append(visit_tool)
        else:
            # Phase-2 surface (default): coordinator dispatches a SearchAgent.
            # Raw web tools are NOT registered — the SearchAgent owns that
            # toolset internally so SERP / page text never enters the
            # coordinator's context.
            tools.append(
                SearchIdeaContextTool(
                    cwd=cwd, tree=tree, config=config, provider=provider,
                    workspace_dir=wdir,
                )
            )
            tools.append(
                SearchIdeaContextParallelTool(
                    cwd=cwd, tree=tree, config=config, provider=provider,
                    workspace_dir=wdir,
                )
            )
            tools.append(SearchStatusTool(cwd=cwd, workspace_dir=wdir))

        # ── Grounded ideation (roadmap 1.1) — own lane, independent of `mode` ──
        # When on, the coordinator gets a ResearchSearch tool: an on-demand
        # external-knowledge assistant (related-work / survey / lookup /
        # explore) it can call any time to inform its work. Separate from the
        # novelty-audit surface above.
        if sc.grounded_ideation:
            tools.append(
                ResearchSearchTool(
                    cwd=cwd, config=config, provider=provider, workspace_dir=wdir,
                )
            )

    return tools

