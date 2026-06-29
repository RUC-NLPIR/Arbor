"""Configuration for the Coordinator orchestrator."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from pydantic import BaseModel
from pydantic import Field as PydField
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ..core.config_schema import (
    SHARED_FLAT,
    ContextConfig,
    LLMConfig,
    ProxyModel,
    TimeoutConfig,
    UIConfig,
)

if TYPE_CHECKING:
    from ..core.config import AgentConfig

from .convergence import ConvergenceConfig


class BudgetStage(BaseModel):
    """One fidelity stage in an adaptive research budget."""

    walltime: int
    description: str = ""
    data_fraction: float | None = None
    steps: int | None = None
    promotion_gate: str | None = None

    @classmethod
    def from_value(cls, name: str, value: object) -> "BudgetStage":
        """Parse a stage from YAML-friendly scalar or mapping values."""
        if isinstance(value, BudgetStage):
            return value
        if isinstance(value, int):
            return cls(walltime=max(1, value), description=name)
        if not isinstance(value, dict):
            raise TypeError(f"budget stage {name!r} must be an int or mapping")

        walltime = (
            value.get("walltime")
            or value.get("timeout")
            or value.get("seconds")
            or value.get("time_budget")
        )
        if walltime is None:
            raise ValueError(f"budget stage {name!r} is missing walltime/timeout")

        return cls(
            walltime=max(1, int(walltime)),
            description=str(value.get("description") or name),
            data_fraction=_optional_float(value.get("data_fraction")),
            steps=_optional_int(value.get("steps")),
            promotion_gate=value.get("promotion_gate") or value.get("promote_if"),
        )


class BudgetPolicy(BaseModel):
    """Optional runtime policy shared by the coordinator and executors.

    The legacy scalar fields (``time_budget``, ``executor_timeout``,
    ``run_training_timeout_max``...) still work.  By default AutoResearch uses
    generous long-running timeouts; this policy only adds advanced overrides
    such as staged smoke/pilot/full budgets and promotion gates.
    """

    mode: str = "long-default"
    global_time_budget: int | None = None
    finalization_buffer_fraction: float = 0.10
    require_cost_estimate: bool = False
    default_stage: str = "full"
    max_parallel_executors: int | None = None

    # Optional runtime overrides.  These are intentionally nullable so scalar
    # defaults remain in effect unless the policy opts in.
    executor_timeout: int | None = None
    nested_executor_timeout: int | None = None
    bash_timeout_default: int | None = None
    bash_timeout_max: int | None = None
    run_training_timeout_default: int | None = None
    run_training_timeout_max: int | None = None

    stages: dict[str, BudgetStage] = PydField(default_factory=dict)

    @classmethod
    def from_dict(cls, data: object) -> "BudgetPolicy":
        """Build a policy from a YAML mapping while tolerating aliases."""
        if isinstance(data, BudgetPolicy):
            return data
        if data is None:
            return cls()
        if not isinstance(data, dict):
            raise TypeError("budget_policy must be a mapping")

        stages_raw = data.get("stages") or {}
        stages: dict[str, BudgetStage] = {}
        if isinstance(stages_raw, dict):
            stages = {
                str(name): BudgetStage.from_value(str(name), value)
                for name, value in stages_raw.items()
            }

        return cls(
            mode=str(data.get("mode") or "long-default"),
            global_time_budget=_optional_int(
                _first_present(data, "global_time_budget", "total_time_budget", "time_budget")
            ),
            finalization_buffer_fraction=float(data.get("finalization_buffer_fraction", 0.10)),
            require_cost_estimate=bool(data.get("require_cost_estimate", False)),
            default_stage=str(data.get("default_stage") or "full"),
            max_parallel_executors=_optional_int(data.get("max_parallel_executors")),
            executor_timeout=_optional_int(
                data.get("executor_timeout")
            ),
            nested_executor_timeout=_optional_int(
                data.get("nested_executor_timeout")
            ),
            bash_timeout_default=_optional_int(data.get("bash_timeout_default")),
            bash_timeout_max=_optional_int(data.get("bash_timeout_max")),
            run_training_timeout_default=_optional_int(
                _first_present(data, "run_training_timeout_default", "run_training_default")
            ),
            run_training_timeout_max=_optional_int(
                _first_present(data, "run_training_timeout_max", "run_training_max")
            ),
            stages=stages,
        )

    def normalized_finalization_fraction(self) -> float:
        """Return a safe finalization buffer fraction in [0.05, 0.50]."""
        return min(0.50, max(0.05, self.finalization_buffer_fraction))

    def stage_timeouts(self, *, timeout_default: int, timeout_max: int) -> dict[str, int]:
        """Return stage name -> usable RunTraining timeout seconds."""
        _ = timeout_default
        if self.stages:
            return {
                name: min(max(1, stage.walltime), timeout_max)
                for name, stage in self.stages.items()
            }

        return {}

    def to_prompt_text(
        self,
        *,
        time_budget: int | None,
        executor_timeout: int,
        run_training_timeout_default: int,
        run_training_timeout_max: int,
        max_cycles: int,
    ) -> str:
        """Render concise budget guidance for agent prompts."""
        stage_timeouts = self.stage_timeouts(
            timeout_default=run_training_timeout_default,
            timeout_max=run_training_timeout_max,
        )
        stage_lines = []
        for name, timeout in stage_timeouts.items():
            stage = self.stages.get(name)
            extra: list[str] = []
            if stage and stage.data_fraction is not None:
                extra.append(f"data_fraction={stage.data_fraction:g}")
            if stage and stage.steps is not None:
                extra.append(f"steps={stage.steps}")
            if stage and stage.promotion_gate:
                extra.append(f"promote_if={stage.promotion_gate}")
            suffix = f" ({', '.join(extra)})" if extra else ""
            desc = f": {stage.description}" if stage and stage.description else ""
            stage_lines.append(f"- `{name}`: {timeout}s{suffix}{desc}")
        if stage_lines:
            stages_text = "\n".join(stage_lines)
            stage_guidance = "Use `budget_stage` when you intentionally want a configured shorter or longer fidelity stage."
        else:
            stages_text = "Not configured. The default behavior is a generous long-running timeout; no budget setup is required."
            stage_guidance = "Do not invent stages. Use RunTraining normally unless the user configured stages."

        total = f"{time_budget}s" if time_budget is not None else "unlimited"
        parallel = (
            str(self.max_parallel_executors)
            if self.max_parallel_executors is not None
            else "use judgment"
        )
        cost_gate = (
            "Every idea must include an expected-cost estimate and a minimal test."
            if self.require_cost_estimate
            else "Cost estimates are optional; mention them only when they materially affect the plan."
        )

        if not stage_lines:
            return f"""\
# Long-Running Experiments

- Default timeouts are intentionally generous: executor={executor_timeout}s, RunTraining default/max={run_training_timeout_default}s / {run_training_timeout_max}s.
- No budget configuration is required for normal use. Let real experiments run long enough to produce useful evidence.
- {cost_gate}
- If a command times out, treat it as experimental evidence, not automatic failure. Inspect partial metrics, logs, checkpoints, and decide whether to resume, extend, reduce scope, debug, or prune.
- Use RunTraining for long training/eval commands so partial metrics and logs are captured even when a timeout happens."""

        return f"""\
# Budget Policy

- Mode: `{self.mode}`
- Total run budget: {total}
- Max cycles guideline: {max_cycles}
- Executor walltime: {executor_timeout}s
- RunTraining default/max: {run_training_timeout_default}s / {run_training_timeout_max}s
- Max parallel executors: {parallel}
- Finalization buffer: {self.normalized_finalization_fraction():.0%} of total run budget

## Fidelity Stages
{stages_text}

## Budget Discipline
- {cost_gate}
- Default to letting real experiments run long enough to finish; do not under-timeout training or evaluation commands.
- {stage_guidance}
- If a command times out, treat it as experimental evidence, not automatic failure. Inspect partial metrics, logs, checkpoints, and decide whether to resume, extend, reduce scope, debug, or prune.
- For long training/eval commands, use RunTraining so partial metrics and logs are captured even when a timeout happens."""


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    return int(value)


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    return float(value)


def _first_present(data: dict[str, object], *keys: str) -> object | None:
    for key in keys:
        if key in data:
            return data[key]
    return None


class _WebSearchEnv(BaseSettings):
    """Reads *only* the WEB_* search/browse env vars.

    Kept separate from :class:`SearchConfig` so that ``BaseSettings`` env
    binding never collides with generic field names (``enabled`` / ``mode`` /
    ``background``). Each field maps to its upper-cased name, e.g.
    ``web_search_endpoint`` ← ``WEB_SEARCH_ENDPOINT``.
    """

    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    web_search_endpoint: str | None = None
    web_browse_endpoint: str | None = None
    web_search_api_key: str | None = None
    web_browse_api_key: str | None = None
    serper_api_key: str | None = None  # SERPER_API_KEY
    exa_api_key: str | None = None     # EXA_API_KEY
    jina_api_key: str | None = None    # JINA_API_KEY
    serper_api_key: str | None = None
    exa_api_key: str | None = None
    jina_api_key: str | None = None


class SearchConfig(BaseModel):
    """Configuration for the SearchAgent / web-search tools.

    All fields are optional. If ``web_search_endpoint`` is unset, the
    web-search tools are NOT registered with the coordinator and any Phase 2
    SearchIdeaContext call raises a clear error.
    """

    web_search_endpoint: str | None = None
    web_browse_endpoint: str | None = None
    web_search_provider: str = "google"
    web_search_api_key: str | None = None
    web_browse_api_key: str | None = None  # falls back to web_search_api_key
    # Master switch. When False, the SearchAgent / web tools are NOT
    # registered with the coordinator at all, regardless of endpoint config.
    # Use this to turn the related-work feature off without unsetting URLs.
    enabled: bool = True
    # Cost gate: when True, SearchIdeaContext only runs on nodes that have
    # already been executed AND beat the current trunk score (i.e. nodes
    # that are merge-worthy and worth a novelty check). When False, the
    # tool runs on any node with a hypothesis.
    require_validated: bool = True
    visit_max_content_tokens: int = 2048
    # Full-text budget for the grounded-ideation (ResearchSearch) lane. Larger
    # than visit_max_content_tokens so the model can read a paper's method /
    # results sections, not just the abstract (roadmap 1.1d). The novelty-audit
    # lane keeps the smaller visit_max_content_tokens (abstracts suffice there).
    research_visit_tokens: int = 6000
    agent_max_turns: int = 12             # Phase 2
    # Per-search wall-clock cap (seconds). ``None`` = unlimited; the SearchAgent
    # runs to completion (still bounded by ``agent_max_turns``). Background
    # dispatch (the default) means the coordinator does not block on this anyway.
    agent_timeout: int | None = None
    # Optional override for the SearchAgent's model. ``None`` reuses the
    # provider passed in (typically the coordinator's model). Set to e.g.
    # ``"claude-haiku-4-5-20251001"`` to run the SearchAgent on a cheaper
    # model than the coordinator.
    agent_model: str | None = None
    # Background dispatch: SearchIdeaContext returns immediately and the
    # SearchAgent runs concurrently with the coordinator's other work.
    # When False, the tool blocks until the search finishes.
    background: bool = True
    # When True, TreeAddNode dispatches a background pre-experiment SearchAgent
    # on the newly-added node's hypothesis (novelty/prior-art check BEFORE any
    # executor runs). The verdict lands in the node's ``related_work`` field;
    # it is advisory and never blocks dispatch. Requires a configured backend
    # (``web_search_endpoint`` or ``builtin_backend``).
    auto_search_on_add: bool = False
    # Optional focus directive applied to the pre-experiment auto-search.
    auto_search_focus: str | None = None
    # Built-in, zero-config search backend. ``"none"`` (default) uses the HTTP
    # endpoints above. ``"alphaxiv"`` queries the public alphaXiv API in-process
    # (no endpoint URL, no API key). The ``alphaxiv-py`` package ships with arbor
    # by default on Python >= 3.12; on 3.10/3.11 the backend is unavailable.
    builtin_backend: Literal["none", "alphaxiv"] = "none"
    # Tool-surface mode:
    #   "executor" (default) — register SearchIdeaContext / SearchIdeaContextParallel
    #     on the coordinator. The raw web_search / web_visit tools are NOT exposed,
    #     so verbose SERP / page text never enters the coordinator context.
    #   "inline"            — register web_search / web_visit directly on the
    #     coordinator (Phase-1 surface). Useful for debugging the raw tools or
    #     when you want full coordinator control of the loop.
    mode: str = "executor"
    # Grounded ideation. When True, the coordinator gets a ``ResearchSearch``
    # tool: an on-demand external-knowledge assistant (web + alphaXiv) it can
    # call at any time to find related work, survey a field, look up a fact, or
    # explore a direction. Default OFF — benchmark runs stay fair (the system
    # cannot crib finished work off the web). This is a SEPARATE lane from the
    # post-experiment novelty audit: a research digest enters the coordinator
    # context and the citation that shaped an idea lands in ``node.grounding``,
    # while the novelty audit still runs its own fresh search and writes
    # ``node.related_work`` — the two never share state. Independent of
    # ``mode``. Requires a configured backend.
    grounded_ideation: bool = False
    # ── Pluggable search backends ──────────────────────────────────────────
    # Ordered list of search backends to fan out across and merge. Names:
    #   "alphaxiv" (keyless papers) | "jina" (keyless web) | "serper" (key) |
    #   "exa" (key) | "endpoint" (self-hosted web_search_endpoint).
    # When empty, the legacy fields below are mapped automatically:
    #   builtin_backend="alphaxiv" → ["alphaxiv"]; web_search_endpoint set →
    #   append "endpoint". Backends missing credentials are dropped.
    backends: list[str] = PydField(default_factory=list)
    serper_api_key: str | None = None  # SERPER_API_KEY
    exa_api_key: str | None = None     # EXA_API_KEY (used by both "exa" REST and "exa-mcp")
    jina_api_key: str | None = None    # JINA_API_KEY (optional; raises Jina rate limits)
    # Override the Exa MCP server URL for the "exa-mcp" backend (default
    # https://mcp.exa.ai/mcp). Auth uses exa_api_key via the x-api-key header.
    exa_mcp_url: str | None = None
    # Visit/click backend: "auto" picks per config (browse endpoint → that;
    # alphaXiv-only → alphaXiv SDK; else keyless Jina). Force one with
    # "jina" | "requests" | "alphaxiv" | "endpoint".
    visit_backend: str = "auto"

    @property
    def has_backend(self) -> bool:
        """True when at least one usable search backend resolves from this config
        (explicit ``backends`` list or the legacy endpoint / alphaXiv fields)."""
        from ..core.tools.web.backends import resolve_backend_names
        return bool(resolve_backend_names(self))

    @model_validator(mode="after")
    def _apply_env_fallbacks(self) -> "SearchConfig":
        """Fill unset endpoints/keys from env vars (pydantic-settings)."""
        env = _WebSearchEnv()
        if self.web_search_endpoint is None:
            self.web_search_endpoint = env.web_search_endpoint or None
        if self.web_browse_endpoint is None:
            self.web_browse_endpoint = env.web_browse_endpoint or None
        if self.web_search_api_key is None:
            self.web_search_api_key = env.web_search_api_key or None
        if self.web_browse_api_key is None:
            self.web_browse_api_key = env.web_browse_api_key or self.web_search_api_key
        if self.serper_api_key is None:
            self.serper_api_key = env.serper_api_key or None
        if self.exa_api_key is None:
            self.exa_api_key = env.exa_api_key or None
        if self.jina_api_key is None:
            self.jina_api_key = env.jina_api_key or None
        return self


class CoordinatorConfig(ProxyModel):
    """Runtime configuration for the Coordinator.

    Typed pydantic model composed of the shared subgroups (``llm`` / ``timeout``
    / ``context``) plus meta-specific orchestration fields. Legacy flat keyword
    construction and ``config.<flat_name>`` reads keep working via
    :class:`ProxyModel`, so existing call sites are untouched.
    """

    # Flat read proxies (provider, executor_timeout, time_budget, ...) resolve
    # through the shared subgroup map.
    PROXY: ClassVar[dict[str, tuple[str, str]]] = SHARED_FLAT

    # ── Shared subgroups ─────────────────────────────────────────────
    llm: LLMConfig = PydField(default_factory=LLMConfig)
    timeout: TimeoutConfig = PydField(default_factory=TimeoutConfig)
    context: ContextConfig = PydField(default_factory=ContextConfig)
    ui: UIConfig = PydField(default_factory=UIConfig)

    # ── Target codebase ──────────────────────────────────────────────
    cwd: str = "."
    task: str = ""

    # ── Meta-specific LLM override ───────────────────────────────────
    meta_model: str | None = None

    # ── Tree parameters ──────────────────────────────────────────────
    # Default depth 2 = the intended three levels: 0=root, 1=strategy, 2=idea.
    # Set to None for an unbounded tree (or override via --max-depth / config).
    max_tree_depth: int | None = 2
    max_cycles: int = 40  # hard cap — enforced in RunExecutor(Parallel)

    # ── Executor parameters ──────────────────────────────────────────
    executor_max_turns: int = 50
    # Max resume attempts (retries beyond the initial run) before ResumeExecutor
    # refuses to continue a node. Each resume increments node.attempt; bounds
    # runaway resume loops.
    max_retries: int = 3

    # ── Evaluation ───────────────────────────────────────────────────
    merge_threshold: float = 5.0  # soft guideline for the LLM
    eval_retries: int = 1  # extra B_test attempts after a transient failure
    eval_retry_base_delay: float = 5.0
    eval_retry_max_delay: float = 30.0
    # Tamper-proofing: hash protected paths at worktree creation and verify
    # before trusting a dev score; best-effort OS read-only on top.
    enforce_protected: bool = True
    protected_paths: list[str] = PydField(default_factory=list)
    # Contamination check (non-blocking; auto-falls-back to declarative).
    contamination_probe: bool = True
    contamination_timeout: int = 60

    # ── Loop ─────────────────────────────────────────────────────────
    max_turns: int = 500  # total max turns for the single persistent agent

    # ── Git / persistence ────────────────────────────────────────────
    auto_git: bool = True
    git_branch_prefix: str = "coordinator"
    trunk_branch: str | None = None  # working trunk (keeps main clean)
    base_branch: str | None = None  # expected clean base; defaults to main/master
    require_base_branch: bool = True
    workspace_dir: str | None = None

    # ── Logging ──────────────────────────────────────────────────────
    verbose: bool = False

    # ── Resume (runtime-only flag; gates checkpoint replay, #1) ───────
    resume: bool = PydField(default=False, exclude=True, repr=False)

    # ── Adaptive budget policy ───────────────────────────────────────
    budget_policy: BudgetPolicy = PydField(default_factory=BudgetPolicy)

    # ── Search / related-work annotation ─────────────────────────────
    search: SearchConfig = PydField(default_factory=SearchConfig)

    # ── Convergence detection ────────────────────────────────────────
    convergence: ConvergenceConfig = PydField(default_factory=ConvergenceConfig)

    # ── Skills ───────────────────────────────────────────────────────
    skills_enabled: bool = True
    disabled_skills: list[str] = PydField(default_factory=list)

    # ── Self-evolution ───────────────────────────────────────────────
    # Write a training-ready decision trace (trajectory.jsonl) at finalize.
    export_trajectory: bool = True
    # Capture per-call token-level traces (tokens.jsonl) for SFT/RL. Heavy; off.
    token_trace: bool = False
    # Distill run insights into a reusable skill in ~/.arbor/skills (line 2). Off.
    distill_skills: bool = False
    # Use the LLM to abstract distilled lessons into transferable principles. Off.
    distill_abstract: bool = False

    # ── Plugin (runtime object; not serialized) ──────────────────────
    plugin: Any = PydField(default=None, exclude=True, repr=False)

    # ── Validators ───────────────────────────────────────────────────

    @model_validator(mode="before")
    @classmethod
    def _coerce_structured_blocks(cls, data: Any) -> Any:
        """Normalize legacy aliases and coerce YAML mappings into dataclasses.

        Runs alongside :meth:`ProxyModel._route_flat_keys`; tolerates the
        legacy key spellings (``max_depth`` / ``branch_prefix``) and the
        ``budget_policy`` / ``search`` / ``convergence`` aliases the loader
        used to handle inline.
        """
        if not isinstance(data, dict):
            return data
        out = dict(data)
        # Legacy YAML key aliases → authoritative field names (contract C3).
        for old, new in (("max_depth", "max_tree_depth"), ("branch_prefix", "git_branch_prefix")):
            if old in out and new not in out:
                out[new] = out.pop(old)
        if isinstance(out.get("budget_policy"), dict):
            out["budget_policy"] = BudgetPolicy.from_dict(out["budget_policy"])
        if isinstance(out.get("search"), dict):
            out["search"] = SearchConfig(**out["search"])
        if isinstance(out.get("convergence"), dict):
            out["convergence"] = ConvergenceConfig.from_dict(out["convergence"])
        return out

    @model_validator(mode="after")
    def _apply_budget_policy_overrides(self) -> "CoordinatorConfig":
        """Apply explicit budget_policy runtime overrides onto the timeout group."""
        bp = self.budget_policy
        t = self.timeout
        if bp.global_time_budget is not None and t.time_budget is None:
            t.time_budget = bp.global_time_budget
        if bp.executor_timeout is not None:
            t.executor = bp.executor_timeout
        if bp.nested_executor_timeout is not None:
            t.nested_executor = bp.nested_executor_timeout
        if bp.bash_timeout_default is not None:
            t.bash_default = bp.bash_timeout_default
        if bp.bash_timeout_max is not None:
            t.bash_max = bp.bash_timeout_max
        if bp.run_training_timeout_default is not None:
            t.run_training_default = bp.run_training_timeout_default
        if bp.run_training_timeout_max is not None:
            t.run_training_max = bp.run_training_timeout_max
        return self

    # ── Derived properties ───────────────────────────────────────────

    @property
    def cwd_path(self) -> Path:
        return Path(self.cwd).resolve()

    @property
    def coordinator_dir(self) -> Path:
        if self.workspace_dir:
            d = Path(self.workspace_dir) / ".coordinator"
        else:
            d = self.cwd_path / ".coordinator"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def tree_json_path(self) -> Path:
        return self.coordinator_dir / "idea_tree.json"

    @property
    def tree_md_path(self) -> Path:
        return self.coordinator_dir / "idea_tree.md"

    @property
    def effective_meta_model(self) -> str:
        return self.meta_model or self.llm.model

    # ── Executor config factory ──────────────────────────────────────

    def to_executor_config(self, idea_id: str, idea_hypothesis: str) -> "AgentConfig":
        """Create an AgentConfig for spawning a executor.

        The shared ``llm`` / ``timeout`` / ``context`` subgroups are copied
        wholesale, so this no longer restates every field by hand.
        """
        from ..core.config import AgentConfig

        executor_config = AgentConfig(
            llm=self.llm.model_copy(deep=True),
            timeout=self.timeout.model_copy(deep=True),
            context=self.context.model_copy(deep=True),
            cwd=self.cwd,
            max_turns=self.executor_max_turns,
            max_tool_concurrency=10,
            auto_git=True,
            git_branch_prefix=f"{self.git_branch_prefix}/{idea_id}",
            idea=idea_hypothesis,
            verbose=self.verbose,
            workspace_dir=self.workspace_dir,
            node_id=idea_id,
            agent_label=f"sub:{idea_id}",
        )
        executor_config.run_training_stage_timeouts = self.budget_policy.stage_timeouts(
            timeout_default=self.run_training_timeout_default,
            timeout_max=self.run_training_timeout_max,
        )
        executor_config.budget_policy_summary = self.budget_policy.to_prompt_text(
            time_budget=self.time_budget,
            executor_timeout=self.executor_timeout,
            run_training_timeout_default=self.run_training_timeout_default,
            run_training_timeout_max=self.run_training_timeout_max,
            max_cycles=self.max_cycles,
        )
        return executor_config
