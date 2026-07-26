"""Typed configuration schema — the single source of truth for all settings.

This module defines the *shared* configuration subgroups (``llm`` / ``timeout``
/ ``context``) that both the coordinator and its executors consume, plus the
small machinery that lets one nested model present three faces at once:

* **flat-in**  — legacy ``AgentConfig(provider=..., bash_timeout_default=...)``
  keyword construction keeps working; a ``model_validator(mode="before")``
  routes flat keys into their subgroup.
* **nested-store** — values live in typed subgroups (``cfg.llm.provider``),
  the one authoritative location for each field (C1/C4).
* **flat-out** — read-only ``cfg.provider`` proxies resolve back through the
  same map, so the ~58 existing call sites need no change.

The flat→nested map (:data:`ProxyModel.PROXY`) is also the explicit alias /
field registry (C3): every legacy flat name is listed here once, and
``config_resolve`` reuses it to flatten YAML/CLI layers before validation.

Secrets are stored as plain strings (so the provider clients consume them
unchanged) and redacted *at dump time* by name — see :data:`SENSITIVE_KEYS`
and :func:`redacted_snapshot`. That dump is the snapshot/WebUI visible face (C5).
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


class ProxyModel(BaseModel):
    """Base model giving flat-in construction + flat-out attribute proxies.

    Subclasses declare ``PROXY = {flat_name: (subgroup, field)}``. The shape is
    the single registry behind three behaviours (see module docstring).
    Flat keys are routed symmetrically on construction, attribute read/write,
    and ``model_copy(update=...)``.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, validate_assignment=False)

    #: flat alias -> (subgroup attr, field on subgroup). Overridden per role.
    PROXY: ClassVar[dict[str, tuple[str, str]]] = {}

    def model_copy(self, *, update: dict[str, Any] | None = None, deep: bool = False) -> "ProxyModel":
        """Like ``BaseModel.model_copy`` but routes flat keys in ``update``.

        ``update={"provider": "openai"}`` updates ``llm.provider`` (by copying
        the subgroup) instead of writing a shadow attribute that desyncs from
        the subgroup.
        """
        if not update:
            return super().model_copy(deep=deep)
        proxy = type(self).PROXY
        passthrough: dict[str, Any] = {}
        grouped: dict[str, dict[str, Any]] = {}
        for key, value in update.items():
            target = proxy.get(key)
            if target is None:
                passthrough[key] = value
            else:
                group, field = target
                grouped.setdefault(group, {})[field] = value
        for group, fields in grouped.items():
            current = passthrough.get(group) or getattr(self, group)
            passthrough[group] = current.model_copy(update=fields, deep=deep)
        return super().model_copy(update=passthrough, deep=deep)

    @model_validator(mode="before")
    @classmethod
    def _route_flat_keys(cls, data: Any) -> Any:
        """Fold legacy flat kwargs/dict keys into their subgroup dicts."""
        if not isinstance(data, dict):
            return data
        proxy = cls.PROXY
        if not proxy:
            return data
        passthrough: dict[str, Any] = {}
        grouped: dict[str, dict[str, Any]] = {}
        for key, value in data.items():
            target = proxy.get(key)
            if target is None:
                passthrough[key] = value
            else:
                group, field = target
                grouped.setdefault(group, {})[field] = value
        # Merge routed values onto any explicitly-passed subgroup (dict or model).
        for group, values in grouped.items():
            base = passthrough.get(group, {})
            if isinstance(base, BaseModel):
                base = base.model_dump()
            elif not isinstance(base, dict):
                base = {}
            passthrough[group] = {**base, **values}
        return passthrough

    def __getattr__(self, name: str) -> Any:
        # Only reached when normal attribute lookup misses, i.e. exactly the
        # flat proxy names (real fields resolve before this fires).
        proxy = type(self).PROXY
        if name in proxy:
            group, field = proxy[name]
            return getattr(getattr(self, group), field)
        return super().__getattr__(name)

    def __setattr__(self, name: str, value: Any) -> None:
        # Symmetric with __getattr__: writing a flat proxy name routes into its
        # subgroup, so legacy ``config.llm_timeout = x`` keeps working. Real
        # fields fall through to pydantic's normal assignment.
        proxy = type(self).PROXY
        if name in proxy:
            group, field = proxy[name]
            setattr(getattr(self, group), field, value)
        else:
            super().__setattr__(name, value)


# ─────────────────────────────────────────────────────────────────────────────
# Shared subgroups — defined ONCE, composed by both AgentConfig and
# CoordinatorConfig. These three blocks are exactly what every ``AgentConfig(...)``
# construction site used to copy field-by-field.
# ─────────────────────────────────────────────────────────────────────────────


class LLMConfig(BaseModel):
    """Provider / model / sampling / retry settings shared by every agent."""

    model_config = ConfigDict(arbitrary_types_allowed=True, validate_assignment=True)

    # Friendly aliases accepted inside the ``llm:`` YAML block. The top-level
    # ``timeout:`` block is a *different* model (TimeoutConfig), so users
    # naturally write ``llm.timeout`` expecting the request timeout — accept it
    # as a synonym for ``llm_timeout`` instead of silently dropping it. The
    # warning machinery in ``config_resolve`` reads this same registry so the
    # alias is not flagged as an unknown key.
    FIELD_ALIASES: ClassVar[dict[str, str]] = {"timeout": "llm_timeout"}

    # Backend selector (single axis). "auto" routes by model name:
    #   claude* (no custom base_url) → native Anthropic (prompt caching)
    #   everything else             → litellm (DeepSeek/Gemini/Qwen/proxies)
    # Explicit values force a backend: anthropic | openai-responses |
    # openai-chat | litellm. (Legacy: "openai" + openai_api, "claude".)
    provider: str = "auto"
    model: str = "claude-sonnet-4-20250514"
    api_key: str | None = None
    base_url: str | None = None  # for OpenAI-compatible endpoints
    openai_api: str = "responses"  # legacy second axis for provider=openai
    reasoning_effort: str | None = "high"  # low|medium|high (Claude maps to budget)
    reasoning_summary: str | None = "auto"
    text_verbosity: str | None = "medium"
    parallel_tool_calls: bool | None = True
    thinking_budget_tokens: int | None = None
    max_tokens: int = 16384
    # Opt-in: request sampled-token logprobs for token-faithful trajectory traces.
    # Off by default — some OpenAI-compatible endpoints (Gemini, Ollama) reject the
    # field with HTTP 400 instead of ignoring it; enable only where supported.
    logprobs: bool = False
    llm_timeout: float = 300.0
    llm_provider_retries: int = 3
    llm_retry_attempts: int = 5
    llm_retry_base_delay: float = 5.0
    llm_retry_max_delay: float = 120.0

    @field_validator("reasoning_effort", "reasoning_summary", "text_verbosity", mode="before")
    @classmethod
    def _none_string_to_none(cls, value: Any) -> Any:
        """Treat the literal string ``"none"`` (any case) as "feature off" (None)."""
        if isinstance(value, str) and value.lower() == "none":
            return None
        return value

    @model_validator(mode="before")
    @classmethod
    def _apply_field_aliases(cls, data: Any) -> Any:
        """Map friendly aliases (e.g. ``timeout`` → ``llm_timeout``) onto their
        real field before validation. The explicit field always wins if both are
        present; the alias key is consumed so it never trips extra-key handling."""
        if not isinstance(data, dict):
            return data
        patched = data
        for alias, real in cls.FIELD_ALIASES.items():
            if alias in patched:
                if patched is data:  # copy lazily, never mutate the caller's dict
                    patched = dict(data)
                value = patched.pop(alias)
                patched.setdefault(real, value)
        return patched

    @model_validator(mode="after")
    def _default_model_for_provider(self) -> "LLMConfig":
        """Pick a sensible default model when one wasn't given for OpenAI.

        ``model_fields_set`` distinguishes an explicit model from the schema
        default, so this only fires when the user left ``model`` unset.
        """
        openai_like = {"openai", "openai-responses", "openai-chat", "litellm"}
        if "model" not in self.model_fields_set and self.provider in openai_like:
            self.model = "gpt-4o"
        return self


class TimeoutConfig(BaseModel):
    """Authoritative home for every wall-clock timeout (contract C4).

    Both coordinator and executors share one instance; executors simply ignore
    the orchestration-only fields (``executor`` / ``eval`` / ``lifecycle_script``).
    """

    model_config = ConfigDict(validate_assignment=True)

    # Orchestration timeouts (coordinator only).
    executor: int = 172_800
    eval: int = 86_400
    lifecycle_script: int = 120
    # Total run budget (seconds); None = unlimited.
    time_budget: int | None = None
    # Tool-runtime timeouts (shared by coordinator + executor).
    nested_executor: int = 14_400
    bash_default: int = 600
    bash_max: int = 86_400
    run_training_default: int = 86_400
    run_training_max: int = 604_800
    # Idle (stall) timeout for RunTraining: terminate a job that produces no
    # output for this many seconds, keeping partial metrics (#8). None = off.
    run_training_stall: int | None = 1_800
    # How often a blocked agent emits a HEARTBEAT liveness event (#8), seconds.
    heartbeat_interval: float = 30.0


class ContextConfig(BaseModel):
    """Context-window management shared by every agent."""

    model_config = ConfigDict(validate_assignment=True)

    window: int = 200_000
    compact_threshold: float = 0.80
    compact_keep_recent: int = 20


class UIConfig(BaseModel):
    """Interaction / observability switches (contract C6).

    Reserved home for the human-in-the-loop and WebUI features built later by
    member B. Declaring them here fixes their config location so the engine
    refactor (member A) cannot collide with it.
    """

    model_config = ConfigDict(validate_assignment=True)

    # Unified human interaction mode:
    # - auto: no human interruption
    # - direction: ask the user for the next exploration direction during IDEATE
    # - review: review proposed ideas before TreeAddNode and before Executor dispatch
    # - collaborative: direction + review
    interaction_mode: str = "auto"  # "auto" | "direction" | "review" | "collaborative"

    idea_direction_timeout: int = 1_800
    idea_review_timeout: int = 1_800
    # Seconds the review gate waits for a human before auto-approving the idea,
    # so a "review" run left unattended makes progress instead of hanging (#2).
    review_timeout: int = 1_800
    # Independent monitoring port for the read-only WebUI (#7). None = off.
    webui_port: int | None = None
    # Whether mid-run quick commands / questions over stdin are accepted (#11).
    quick_commands_enabled: bool = True
    # Whether the agent may ask the user for missing info (#10). When False
    # (unattended default) the AskUser tool is not registered at all. When True,
    # AskUser emits AWAIT_USER and waits up to ``ask_user_timeout`` for a reply.
    allow_agent_questions: bool = False
    ask_user_timeout: int = 1_800  # seconds to wait for a human reply

    @field_validator("interaction_mode", mode="before")
    @classmethod
    def _normalize_interaction_mode(cls, value: Any) -> str:
        mode = str(value or "auto").lower()
        if mode not in {"auto", "direction", "review", "collaborative"}:
            raise ValueError("interaction_mode must be auto, direction, review, or collaborative")
        return mode


# Shared flat-alias entries reused by both roles' PROXY maps. Listing each
# legacy flat name once here is the explicit field registry (C3).
_LLM_FLAT: dict[str, tuple[str, str]] = {
    "provider": ("llm", "provider"),
    "model": ("llm", "model"),
    "api_key": ("llm", "api_key"),
    "base_url": ("llm", "base_url"),
    "openai_api": ("llm", "openai_api"),
    "reasoning_effort": ("llm", "reasoning_effort"),
    "reasoning_summary": ("llm", "reasoning_summary"),
    "text_verbosity": ("llm", "text_verbosity"),
    "parallel_tool_calls": ("llm", "parallel_tool_calls"),
    "thinking_budget_tokens": ("llm", "thinking_budget_tokens"),
    "max_tokens": ("llm", "max_tokens"),
    "llm_timeout": ("llm", "llm_timeout"),
    "llm_provider_retries": ("llm", "llm_provider_retries"),
    "llm_retry_attempts": ("llm", "llm_retry_attempts"),
    "llm_retry_base_delay": ("llm", "llm_retry_base_delay"),
    "llm_retry_max_delay": ("llm", "llm_retry_max_delay"),
}

_TIMEOUT_FLAT: dict[str, tuple[str, str]] = {
    "executor_timeout": ("timeout", "executor"),
    "eval_timeout": ("timeout", "eval"),
    "lifecycle_script_timeout": ("timeout", "lifecycle_script"),
    "time_budget": ("timeout", "time_budget"),
    "nested_executor_timeout": ("timeout", "nested_executor"),
    "bash_timeout_default": ("timeout", "bash_default"),
    "bash_timeout_max": ("timeout", "bash_max"),
    "run_training_timeout_default": ("timeout", "run_training_default"),
    "run_training_timeout_max": ("timeout", "run_training_max"),
    "run_training_stall_timeout": ("timeout", "run_training_stall"),
    "heartbeat_interval": ("timeout", "heartbeat_interval"),
}

_CONTEXT_FLAT: dict[str, tuple[str, str]] = {
    "context_window": ("context", "window"),
    "compact_threshold": ("context", "compact_threshold"),
    "compact_keep_recent": ("context", "compact_keep_recent"),
}

#: Flat aliases for the three shared subgroups, reused by every role's PROXY.
SHARED_FLAT: dict[str, tuple[str, str]] = {**_LLM_FLAT, **_TIMEOUT_FLAT, **_CONTEXT_FLAT}

#: Keys whose values are fully masked in any snapshot / event payload.
_FULL_REDACT_KEYS: frozenset[str] = frozenset({
    "api_key",
    "web_search_api_key",
    "web_browse_api_key",
})

#: Keys holding a URL where only embedded credentials (``user:pass@``) are
#: masked — the endpoint host/path stays visible for debugging.
_URL_CREDENTIAL_KEYS: frozenset[str] = frozenset({"base_url"})

#: Every key that gets some form of masking (kept for introspection/tests).
SENSITIVE_KEYS: frozenset[str] = _FULL_REDACT_KEYS | _URL_CREDENTIAL_KEYS

_REDACTED = "***REDACTED***"


def _mask_url_credentials(url: str) -> str:
    """Mask only ``user:pass@`` in a URL; leave a credential-free URL intact."""
    from urllib.parse import urlsplit, urlunsplit

    try:
        parts = urlsplit(url)
    except ValueError:
        return _REDACTED  # unparseable → fail closed
    if not (parts.username or parts.password):
        return url
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, f"***@{host}", parts.path, parts.query, parts.fragment))


def _redact(value: Any) -> Any:
    """Recursively mask sensitive values by key name (see SENSITIVE_KEYS)."""
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for k, v in value.items():
            key = k.lower() if isinstance(k, str) else k
            if v is None:
                out[k] = v
            elif key in _FULL_REDACT_KEYS:
                out[k] = _REDACTED
            elif key in _URL_CREDENTIAL_KEYS and isinstance(v, str):
                out[k] = _mask_url_credentials(v)
            else:
                out[k] = _redact(v)
        return out
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


#: Public alias — redact secrets in an arbitrary already-loaded mapping (e.g. a
#: raw user YAML dict before it is copied into a run log).
redact_secrets = _redact


def redacted_snapshot(model: BaseModel) -> dict[str, Any]:
    """Return a JSON-safe config dump with secrets masked (contract C5).

    This is the structure #1 checkpoint stores and #7 WebUI displays. Sensitive
    fields (api_key, base_url, ...) are replaced with ``"***REDACTED***"`` at
    every nesting depth; runtime-only handles (event bus, callbacks) are
    excluded by ``model_dump``.
    """
    return _redact(model.model_dump(mode="json", exclude_none=False))

