"""CLI ⇆ config binding — generate flat ``--flags`` from one registry.

The coordinator's command line used to restate every setting twice: ~47 hand
written ``add_argument`` calls *and* a ~60-line ``CoordinatorConfig(...)`` kwargs
block. Both are now generated from :data:`CONFIG_FLAGS`, a single list of
:class:`CLIField` specs. ``argparse`` defaults are :data:`argparse.SUPPRESS`, so
an unset flag never appears in the namespace — that is how :func:`cli_overrides`
tells "user provided it" from "fall back to YAML / pydantic default".

Control flags that drive imperative setup (``--config``, ``--cwd``, ``--resume``,
git/branch flags) are intentionally *not* here; they live in the entrypoint
because they do more than carry a value.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class CLIField:
    """One auto-generated CLI flag bound to a config key.

    ``key`` is the config field/alias the value flows into (resolved through the
    schema's flat→nested proxy map, so e.g. ``executor_timeout`` lands in
    ``timeout.executor``).
    """

    flag: str
    key: str
    type: Callable[[str], Any] | None = None
    help: str = ""
    choices: tuple[str, ...] | None = None
    bool_optional: bool = False  # argparse.BooleanOptionalAction (--x / --no-x)
    store_true: bool = False
    required: bool = False
    metavar: str | None = None


# ── Composable flag groups ───────────────────────────────────────────────────
# Each group is a tuple of CLIField specs. Roles (coordinator / executor) compose
# the groups they expose, so a shared field (e.g. --provider) is declared once.

LLM_FLAGS: tuple[CLIField, ...] = (
    CLIField("--provider", "provider", str, "Backend: auto (default) | anthropic | anthropic-oauth | openai-responses | openai-chat | openai-oauth | litellm", choices=("auto", "anthropic", "anthropic-oauth", "claude", "claude-oauth", "openai", "openai-responses", "openai-chat", "openai-oauth", "litellm")),
    CLIField("--model", "model", str, "Model name (default: claude-sonnet-4-20250514 / gpt-4o)"),
    CLIField("--api-key", "api_key", str, "API key (default: from env)"),
    CLIField("--base-url", "base_url", str, "Custom API base URL (vLLM, Ollama, proxy, ...)"),
    CLIField("--openai-api", "openai_api", str, "[legacy] OpenAI mode when --provider openai (use --provider openai-responses/-chat instead)", choices=("responses", "chat")),
    CLIField("--reasoning-effort", "reasoning_effort", str, "Reasoning effort; Claude maps to thinking budget (default: high)", choices=("low", "medium", "high", "none")),
    CLIField("--reasoning-summary", "reasoning_summary", str, "OpenAI Responses reasoning.summary (default: auto; 'none' to omit)"),
    CLIField("--text-verbosity", "text_verbosity", str, "OpenAI Responses text.verbosity (default: medium; 'none' to omit)", choices=("low", "medium", "high", "none")),
    CLIField("--parallel-tool-calls", "parallel_tool_calls", None, "Allow GPT Responses models to request multiple tools in one turn", bool_optional=True),
    CLIField("--thinking-budget-tokens", "thinking_budget_tokens", int, "Claude extended-thinking token budget override"),
    CLIField("--max-tokens", "max_tokens", int, "Max output tokens per LLM call (default: 16384)"),
    CLIField("--logprobs", "logprobs", None, "Request sampled-token logprobs for trajectory traces (default: off; Gemini/Ollama reject it)", bool_optional=True),
    CLIField("--llm-timeout", "llm_timeout", float, "HTTP timeout per LLM API call (default: 300)"),
    CLIField("--llm-provider-retries", "llm_provider_retries", int, "Native SDK retries per LLM API call (default: 3)"),
    CLIField("--llm-retry-attempts", "llm_retry_attempts", int, "Agent-level retry attempts for transient LLM errors (default: 5)"),
    CLIField("--llm-retry-base-delay", "llm_retry_base_delay", float, "Base delay for transient LLM retry backoff (default: 5)"),
    CLIField("--llm-retry-max-delay", "llm_retry_max_delay", float, "Max delay for transient LLM retry backoff (default: 120)"),
)

# Tool-runtime timeouts shared by coordinator and executor agents (live in TimeoutConfig).
TOOL_TIMEOUT_FLAGS: tuple[CLIField, ...] = (
    CLIField("--nested-executor-timeout", "nested_executor_timeout", int, "Timeout for nested Executor tool calls (default: 14400 / 4h)"),
    CLIField("--bash-timeout-default", "bash_timeout_default", int, "Default Bash tool timeout (default: 600)"),
    CLIField("--bash-timeout-max", "bash_timeout_max", int, "Maximum Bash tool timeout (default: 86400 / 24h)"),
    CLIField("--run-training-timeout-default", "run_training_timeout_default", int, "Default RunTraining timeout (default: 86400 / 24h)"),
    CLIField("--run-training-timeout-max", "run_training_timeout_max", int, "Maximum RunTraining timeout (default: 604800 / 7d)"),
    CLIField("--run-training-stall-timeout", "run_training_stall_timeout", int, "Kill a RunTraining job idle (no output) this long (default: 1800 / 30m; 0 disables)"),
    CLIField("--heartbeat-interval", "heartbeat_interval", float, "Seconds between progress heartbeat events while an agent blocks (default: 30)"),
)

CONTEXT_FLAGS: tuple[CLIField, ...] = (
    CLIField("--context-window", "context_window", int, "Context window size (default: 200000)"),
)

# Coordinator orchestration knobs (no meaning for a standalone executor).
META_FLAGS: tuple[CLIField, ...] = (
    CLIField("--task", "task", str, "High-level research task description"),
    CLIField("--max-depth", "max_tree_depth", int, "Max tree depth (default: 2; 0=root,1=strategy,2=idea)"),
    CLIField("--max-cycles", "max_cycles", int, "Target number of arbor cycles (default: 40)"),
    CLIField("--max-turns", "max_turns", int, "Max ReAct turns for the coordinator (default: 500)"),
    CLIField("--executor-max-turns", "executor_max_turns", int, "Max turns per executor (default: 50)"),
    CLIField("--executor-timeout", "executor_timeout", int, "Timeout per executor in seconds (default: 172800 / 48h)"),
    CLIField("--merge-threshold", "merge_threshold", float, "Min %% improvement to merge (default: 5.0)"),
    CLIField("--eval-timeout", "eval_timeout", int, "Timeout per B_test verification attempt (default: 86400 / 24h)"),
    CLIField("--eval-retries", "eval_retries", int, "Extra B_test retries after transient failure (default: 1)"),
    CLIField("--eval-retry-base-delay", "eval_retry_base_delay", float, "Base delay for B_test retry backoff (default: 5)"),
    CLIField("--eval-retry-max-delay", "eval_retry_max_delay", float, "Max delay for B_test retry backoff (default: 30)"),
    CLIField("--lifecycle-script-timeout", "lifecycle_script_timeout", int, "Timeout for plugin lifecycle scripts (default: 120)"),
    CLIField("--skills-enabled", "skills_enabled", None, "Enable LoadSkill-based IDEATE flow (use --no-skills-enabled to disable)", bool_optional=True),
    CLIField("--time-budget", "time_budget", int, "Wall-clock time budget in seconds; forces finalization at 90%% elapsed"),
)

# Sub-agent specific (single-idea standalone runs).
EXECUTOR_FLAGS: tuple[CLIField, ...] = (
    CLIField("--idea", "idea", str, "The research idea / optimization direction to implement", required=True),
    CLIField("--max-turns", "max_turns", int, "Max ReAct turns for the agent (default: 100)"),
    CLIField("--experiment-cmd", "experiment_cmd", str, "Default experiment command (e.g. 'python train.py')"),
)

# Shared misc.
MISC_FLAGS: tuple[CLIField, ...] = (
    CLIField("--workspace-dir", "workspace_dir", str, "Directory for agent state (default: <cwd>/../<cwd_name>_workspace)"),
    CLIField("--verbose", "verbose", None, "Enable verbose logging", store_true=True),
)

#: Full flag set for the coordinator CLI (`coordinator` / `run-research`).
META_CLI_FLAGS: tuple[CLIField, ...] = (
    *LLM_FLAGS, *TOOL_TIMEOUT_FLAGS, *CONTEXT_FLAGS, *META_FLAGS, *MISC_FLAGS,
)

#: Full flag set for the standalone executor CLI (`executor`).
EXECUTOR_CLI_FLAGS: tuple[CLIField, ...] = (
    *LLM_FLAGS, *TOOL_TIMEOUT_FLAGS, *CONTEXT_FLAGS, *EXECUTOR_FLAGS, *MISC_FLAGS,
)

# Backwards-compatible default = the coordinator flag set.
CONFIG_FLAGS: tuple[CLIField, ...] = META_CLI_FLAGS


def add_arguments(parser: argparse.ArgumentParser, fields: tuple[CLIField, ...]) -> None:
    """Register ``fields`` on ``parser`` with SUPPRESS defaults."""
    for f in fields:
        names = ["-v", f.flag] if f.flag == "--verbose" else [f.flag]
        kwargs: dict[str, Any] = {"dest": f.key, "default": argparse.SUPPRESS, "help": f.help}
        if f.required:
            kwargs["required"] = True
        if f.bool_optional:
            kwargs["action"] = argparse.BooleanOptionalAction
        elif f.store_true:
            kwargs["action"] = "store_true"
        else:
            if f.type is not None:
                kwargs["type"] = f.type
            if f.choices is not None:
                kwargs["choices"] = list(f.choices)
            if f.metavar is not None:
                kwargs["metavar"] = f.metavar
        parser.add_argument(*names, **kwargs)


def add_config_arguments(parser: argparse.ArgumentParser, fields: tuple[CLIField, ...] = META_CLI_FLAGS) -> None:
    """Register the coordinator (default) or a custom flag set on ``parser``."""
    add_arguments(parser, fields)


def cli_overrides(args: argparse.Namespace, fields: tuple[CLIField, ...] = META_CLI_FLAGS) -> dict[str, Any]:
    """Extract only the flags the user actually passed into a config-key dict.

    Unset flags are absent (SUPPRESS), so they never shadow YAML or defaults.
    """
    out: dict[str, Any] = {}
    for f in fields:
        if hasattr(args, f.key):
            out[f.key] = getattr(args, f.key)
    return out

