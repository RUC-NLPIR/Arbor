"""LiteLLM-backed provider — one transport for the whole chat-completions family.

Used for the *hybrid* reasoning strategy (#9): models that cannot preserve a
reasoning chain across ReAct turns (DeepSeek-R1, Qwen, Gemini, OpenAI chat, any
OpenAI-compatible proxy, ...) all go through ``litellm.acompletion`` here, while
Claude (signed thinking blocks) and the OpenAI Responses API (encrypted
reasoning items) keep their native providers where the chain *is* preserved.

Reasoning is **display-only**: ``reasoning_content`` is captured for live
streaming (THINKING_DELTA) but never replayed — chat completions treats it as an
output-only field, so it cannot be fed back (see :class:`OpenAICompatProvider`).

This subclass reuses all of :class:`OpenAICompatProvider`'s message conversion,
streaming, and parsing; it only swaps the transport via ``_acompletion``.
"""

from __future__ import annotations

import logging
from typing import Any

import tiktoken

from .base import LLMResponse
from .openai_compat import OpenAICompatProvider

log = logging.getLogger(__name__)


class LiteLLMProvider(OpenAICompatProvider):
    """OpenAI-compatible provider whose transport is the litellm library."""

    def __init__(
        self,
        model: str = "gpt-4o",
        api_key: str | None = None,
        base_url: str | None = None,
        max_retries: int = 3,
        timeout: float = 300.0,
        reasoning_effort: str | None = "high",
        logprobs: bool = False,
    ):
        import litellm

        # Drop params a given model doesn't support (e.g. max_tokens vs
        # max_completion_tokens, reasoning_effort on non-reasoning models)
        # instead of raising; quiet the banner.
        litellm.drop_params = True
        litellm.suppress_debug_info = True

        self.model = self._litellm_model(model, base_url)
        self.base_url = base_url
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries
        self.reasoning_effort = reasoning_effort
        self.logprobs = logprobs
        # No AsyncOpenAI client — litellm.acompletion is the transport.
        try:
            self._enc = tiktoken.encoding_for_model(model)
        except Exception:
            self._enc = tiktoken.get_encoding("cl100k_base")

    @staticmethod
    def _litellm_model(model: str, base_url: str | None) -> str:
        """Resolve a litellm model id.

        litellm routes by a ``provider/model`` prefix. A bare model name behind
        a custom ``base_url`` (vLLM, Ollama, a litellm proxy, ...) is treated as
        OpenAI-compatible, matching the standard ``openai/<model>`` + api_base
        pattern. Names that already carry a prefix are left untouched.
        """
        if base_url and "/" not in model:
            return f"openai/{model}"
        return model

    async def _acompletion(self, **params: Any) -> Any:
        import litellm

        kwargs = dict(params)
        if self.base_url:
            kwargs["api_base"] = self.base_url
        if self.api_key:
            kwargs["api_key"] = self.api_key
        # Enable reasoning via litellm's unified param — it maps per provider
        # (OpenAI reasoning_effort, Anthropic thinking budget, DeepSeek thinking
        # enabled). drop_params=True silently ignores it on non-reasoning models.
        if self.reasoning_effort and self.reasoning_effort != "none":
            kwargs.setdefault("reasoning_effort", self.reasoning_effort)
        kwargs.setdefault("num_retries", self.max_retries)
        kwargs.setdefault("timeout", self.timeout)
        return await litellm.acompletion(**kwargs)

    # ------------------------------------------------------------------
    # Reasoning chain — litellm exposes signed Anthropic ``thinking_blocks``
    # which CAN be replayed to keep the chain coherent across turns (litellm
    # forwards them per-provider). Plain ``reasoning_content`` (DeepSeek-R1 etc.)
    # stays display-only — it is output-only and cannot be fed back.
    # ------------------------------------------------------------------

    def _parse_response(self, raw: Any) -> LLMResponse:
        resp = super()._parse_response(raw)
        message = raw.choices[0].message
        signed = [
            b for b in (self._to_native_thinking(tb) for tb in _as_list(getattr(message, "thinking_blocks", None)))
            if b is not None
        ]
        if signed:
            # Replace the unsigned reasoning_content display block with the
            # signed thinking blocks (replayable) and put them first so they
            # precede text/tool_use, matching Anthropic's required ordering.
            resp.raw_content = signed + [b for b in resp.raw_content if b.get("type") != "thinking"]

        # Anthropic-via-litellm reports cache tokens as top-level usage fields
        # (not inside prompt_tokens_details), and prompt_tokens is already the
        # uncached input — so surface them additively for cache hit-rate (#13).
        usage = getattr(raw, "usage", None)
        if usage is not None and not resp.usage.cache_read_tokens and not resp.usage.cache_creation_tokens:
            cr = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
            cc = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
            if cr or cc:
                resp.usage.cache_read_tokens = cr
                resp.usage.cache_creation_tokens = cc
        return resp

    def _build_assistant_message(self, content: list[Any]) -> dict[str, Any]:
        # Base class attaches reasoning_content (what DeepSeek-family needs).
        msg = super()._build_assistant_message(content)
        thinking_blocks = [
            tb for tb in (self._to_replayable_thinking(b) for b in content if isinstance(b, dict))
            if tb is not None
        ]
        if thinking_blocks:
            # Anthropic-family carries the chain via signed thinking_blocks; drop
            # the duplicate reasoning_* fields so the model gets one thinking copy.
            msg["thinking_blocks"] = thinking_blocks
            msg.pop("reasoning_content", None)
            msg.pop("reasoning", None)
            msg.pop("reasoning_text", None)
            msg.pop("reasoning_opaque", None)
        return msg

    @staticmethod
    def _to_native_thinking(block: Any) -> dict[str, Any] | None:
        """Normalize a litellm thinking_block (dict or object) for history."""
        b = block if isinstance(block, dict) else _obj_to_dict(block)
        if not b:
            return None
        if b.get("type") == "thinking" and b.get("signature"):
            return {"type": "thinking", "thinking": b.get("thinking", ""), "signature": b["signature"]}
        if b.get("type") == "redacted_thinking" and b.get("data"):
            return {"type": "redacted_thinking", "data": b["data"]}
        return None

    @staticmethod
    def _to_replayable_thinking(block: dict[str, Any]) -> dict[str, Any] | None:
        """Return a thinking block to replay — ONLY if it carries a signature
        (or redacted data); unsigned reasoning_content is display-only."""
        if block.get("type") == "thinking" and block.get("signature"):
            return {
                "type": "thinking",
                "thinking": block.get("thinking") or block.get("text") or "",
                "signature": block["signature"],
            }
        if block.get("type") == "redacted_thinking" and block.get("data"):
            return {"type": "redacted_thinking", "data": block["data"]}
        return None


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def _obj_to_dict(obj: Any) -> dict[str, Any]:
    for attr in ("model_dump", "dict"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                return fn()
            except TypeError:
                return fn(exclude_none=True)
    return {k: v for k, v in vars(obj).items() if not k.startswith("_")} if hasattr(obj, "__dict__") else {}
