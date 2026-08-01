"""Configuration for the research (sub-)agent.

``AgentConfig`` is a typed pydantic model composed of the shared subgroups
defined in :mod:`arbor.core.config_schema` (``llm`` / ``timeout`` /
``context``) plus the fields specific to a single agent run. Legacy flat
keyword construction and ``config.<flat_name>`` reads keep working via the
``ProxyModel`` machinery, so existing call sites are untouched.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, ClassVar

from pydantic import Field

from .config_schema import (
    SHARED_FLAT,
    ContextConfig,
    LLMConfig,
    ProxyModel,
    TimeoutConfig,
)


class AgentConfig(ProxyModel):
    """All runtime configuration for a single agent."""

    # Flat read proxies (provider, api_key, bash_timeout_default, ...) resolve
    # through the shared subgroup map.
    PROXY: ClassVar[dict[str, tuple[str, str]]] = SHARED_FLAT

    # ── Shared subgroups ─────────────────────────────────────────────
    llm: LLMConfig = Field(default_factory=LLMConfig)
    timeout: TimeoutConfig = Field(default_factory=TimeoutConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)

    # ── Target codebase ──────────────────────────────────────────────
    cwd: str = "."

    # ── Agent loop (per-spawn, not shared) ───────────────────────────
    max_turns: int = 100
    max_tool_concurrency: int = 10
    # Interactive frontends need a real user-turn boundary. When enabled, any
    # visible assistant text ends the current run; tool calls returned beside
    # that text are discarded rather than executed behind a displayed question.
    yield_on_text: bool = False
    # Autonomous agents benefit from a nudge when they promise work without
    # calling a tool. Interactive chat agents must disable this: ordinary
    # phrases such as "接下来" or "I will" often introduce a question.
    premature_stop_nudges: bool = True

    # ── Experiment settings ──────────────────────────────────────────
    experiment_cmd: str | None = None
    run_training_stage_timeouts: dict[str, int] = Field(default_factory=dict)
    budget_policy_summary: str | None = None
    auto_git: bool = True  # auto-commit after file changes
    git_branch_prefix: str = "arbor"
    idea: str = ""  # the research idea (used for branch naming)

    # ── Logging ──────────────────────────────────────────────────────
    log_dir: str | None = None
    verbose: bool = False

    # ── Persistence — keep agent state out of the target codebase ────
    workspace_dir: str | None = None
    # Exact runtime directory override for clients with their own state root.
    # Legacy Arbor callers leave this unset and continue using ``.arbor``.
    runtime_dir: str | None = None

    # ── Runtime-only handles (never serialized) ──────────────────────
    # Event attribution: which tree node / agent these emissions belong to.
    node_id: str = Field(default="", exclude=True, repr=False)
    agent_label: str = Field(default="agent", exclude=True, repr=False)
    # Callback drained between turns; returns fresh user messages to inject
    # (used by the live dashboard to forward typed-in questions).
    inter_turn_user_messages: Callable[[], list[str]] | None = Field(
        default=None, exclude=True, repr=False
    )
    # Called once per turn at a clean message boundary with (messages, turn).
    # The coordinator orchestrator uses this to persist a resume checkpoint (#1).
    checkpoint_hook: Callable[[list[dict[str, Any]], int], None] | None = Field(
        default=None, exclude=True, repr=False
    )
    # Optional EventBus-like object for LLM usage/error telemetry.
    event_bus: Any | None = Field(default=None, exclude=True, repr=False)
    # When False, this agent's LLM calls are NOT recorded in the process-wide
    # AgentStats counter. Set False for read-only side agents (e.g. the live
    # dashboard's Q&A companion) so their tokens/cache don't pollute the main
    # run's stats (#13 cache_hit_rate, the dashboard token counter, run_stats).
    track_stats: bool = Field(default=True, exclude=True, repr=False)

    # Token-level trace sink (self-evolution line 1). When set, each LLM call's
    # messages + output are appended to this jsonl for SFT/RL. None = off.
    token_trace_path: str | None = Field(default=None, exclude=True, repr=False)

    # ── Derived paths ────────────────────────────────────────────────

    @property
    def cwd_path(self) -> Path:
        return Path(self.cwd).resolve()

    @property
    def agent_dir(self) -> Path:
        """Directory for agent runtime artifacts (legacy default: ``.arbor``)."""
        from .._app import CONFIG_DIR_NAME

        if self.runtime_dir:
            d = Path(self.runtime_dir)
        elif self.workspace_dir:
            d = Path(self.workspace_dir) / CONFIG_DIR_NAME
        else:
            d = self.cwd_path / CONFIG_DIR_NAME
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def log_path(self) -> Path:
        p = Path(self.log_dir) if self.log_dir else self.agent_dir
        p.mkdir(parents=True, exist_ok=True)
        return p
