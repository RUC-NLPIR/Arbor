"""Anthropic Claude provider with native tool_use and streaming support."""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

import anthropic
import tiktoken

from .base import (
    ContentBlock,
    LLMProvider,
    LLMResponse,
    StreamDone,
    StreamEvent,
    StreamTextDelta,
    StreamThinkingDelta,
    StreamToolDelta,
    StreamToolStart,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    Usage,
)

log = logging.getLogger(__name__)

_MIN_THINKING_BUDGET_TOKENS = 1024

# Claude model families that use the newer *adaptive* extended-thinking API
# (server-controlled thinking) and reject the legacy explicit-budget shape
# {"type": "enabled", "budget_tokens": N} with HTTP 400. For these we send
# `thinking={"type": "adaptive"}` and map reasoning_effort onto
# `output_config.effort` instead. Matched as a substring so dated snapshots
# (…-YYYYMMDD) are covered.
_ADAPTIVE_THINKING_MODEL_MARKERS = ("opus-4-7", "opus-4-8")

# Effort levels accepted by the adaptive thinking `output_config.effort` field.
_ADAPTIVE_EFFORT_LEVELS = frozenset({"low", "medium", "high", "xhigh", "max"})


class ClaudeProvider(LLMProvider):
    """LLM provider backed by the Anthropic Messages API."""

    def __init__(
        self,
        model: str = "claude-sonnet-4-20250514",
        api_key: str | None = None,
        base_url: str | None = None,
        max_retries: int = 3,
        timeout: float = 300.0,
        reasoning_effort: str | None = "high",
        thinking_budget_tokens: int | None = None,
    ):
        self.model = model
        self.base_url = base_url
        self.timeout = timeout
        self.reasoning_effort = reasoning_effort
        self.thinking_budget_tokens = thinking_budget_tokens
        client_kwargs: dict[str, Any] = {
            "api_key": api_key,
            "max_retries": max_retries,
            "timeout": timeout,
        }
        if base_url:
            client_kwargs["base_url"] = base_url
        self._client = anthropic.AsyncAnthropic(**client_kwargs)
        # tiktoken doesn't have a Claude tokenizer, but cl100k is a reasonable proxy
        try:
            self._enc = tiktoken.encoding_for_model("gpt-4")
        except (KeyError, ValueError):
            self._enc = tiktoken.get_encoding("cl100k_base")

    # ------------------------------------------------------------------
    # Non-streaming
    # ------------------------------------------------------------------

    async def create(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 16384,
    ) -> LLMResponse:
        params = self._build_params(system, messages, tools, max_tokens)
        raw = await self._client.messages.create(**params)
        return self._parse_response(raw)

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    async def create_streaming(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 16384,
    ) -> AsyncIterator[StreamEvent]:
        params = self._build_params(system, messages, tools, max_tokens)

        content_blocks: dict[int, dict] = {}
        usage = Usage()
        stop_reason = "end_turn"
        model_id = self.model

        async with self._client.messages.stream(**params) as stream:
            async for event in stream:
                etype = event.type

                if etype == "message_start":
                    if hasattr(event, "message"):
                        model_id = getattr(event.message, "model", self.model)
                        u = getattr(event.message, "usage", None)
                        if u:
                            usage.input_tokens = getattr(u, "input_tokens", 0)
                            usage.cache_read_tokens = getattr(u, "cache_read_input_tokens", 0)
                            usage.cache_creation_tokens = getattr(u, "cache_creation_input_tokens", 0)

                elif etype == "content_block_start":
                    idx = event.index
                    cb = event.content_block
                    content_blocks[idx] = {
                        "type": cb.type,
                        "text": getattr(cb, "text", ""),
                        "thinking": getattr(cb, "thinking", ""),
                        "signature": getattr(cb, "signature", ""),
                        "data": getattr(cb, "data", ""),
                        "id": getattr(cb, "id", ""),
                        "name": getattr(cb, "name", ""),
                        "input_json": "",
                    }
                    if cb.type == "tool_use":
                        yield StreamToolStart(id=cb.id, name=cb.name)

                elif etype == "content_block_delta":
                    idx = event.index
                    delta = event.delta
                    block = content_blocks.get(idx, {})
                    if delta.type == "text_delta":
                        block["text"] = block.get("text", "") + delta.text
                        yield StreamTextDelta(text=delta.text)
                    elif delta.type == "thinking_delta":
                        block["thinking"] = block.get("thinking", "") + delta.thinking
                        yield StreamThinkingDelta(text=delta.thinking)
                    elif delta.type == "signature_delta":
                        block["signature"] = getattr(delta, "signature", "")
                    elif delta.type == "input_json_delta":
                        block["input_json"] = block.get("input_json", "") + delta.partial_json
                        yield StreamToolDelta(partial_json=delta.partial_json)

                elif etype == "message_delta":
                    if hasattr(event, "delta"):
                        stop_reason = getattr(event.delta, "stop_reason", stop_reason)
                    u = getattr(event, "usage", None)
                    if u:
                        usage.output_tokens = getattr(u, "output_tokens", 0)

        # Build final response
        content = self._assemble_content_blocks(content_blocks)
        raw_content = self._content_to_raw(content)
        resp = LLMResponse(
            content=content,
            stop_reason=stop_reason,
            usage=usage,
            model=model_id,
            raw_content=raw_content,
        )
        yield StreamDone(response=resp)

    # ------------------------------------------------------------------
    # Token counting
    # ------------------------------------------------------------------

    def count_tokens(self, text: str) -> int:
        return len(self._enc.encode(text))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_params(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        max_tokens: int,
    ) -> dict[str, Any]:
        # Build system prompt with cache_control for prompt caching.
        # The system prompt is stable across turns, so marking it as
        # ephemeral lets the API cache it (5x cheaper on cache hits).
        system_blocks = [
            {
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }
        ]

        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system_blocks,
            "messages": self._cache_messages(messages),
        }
        if tools:
            tool_list = [
                {
                    "name": t["name"],
                    "description": t["description"],
                    "input_schema": t["input_schema"],
                }
                for t in tools
            ]
            # Mark the last tool with cache_control so the full tool
            # list gets cached together with the system prompt.
            if tool_list:
                tool_list[-1]["cache_control"] = {"type": "ephemeral"}
            params["tools"] = tool_list
            params["tool_choice"] = {"type": "auto"}

        self._apply_thinking_params(params, max_tokens)
        return params

    @staticmethod
    def _cache_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Add a moving cache_control breakpoint at the end of the history.

        The conversation only ever grows by appending, so marking the last
        block of the last message caches the whole prefix up to it. Next turn
        Anthropic auto-matches that prefix (a cache READ) and only the newly
        appended delta is written — turning "full context uncached every turn"
        into incremental caching. system + last tool keep their own breakpoints
        (3 of the 4 allowed).
        """
        if not messages:
            return messages
        # Framework-only metadata (for example ``_internal`` control markers)
        # is persisted locally but is not part of Anthropic's message schema.
        out = [
            {key: value for key, value in m.items() if not key.startswith("_")}
            for m in messages
            # Older saved histories may contain empty end_turn replies. Drop
            # only empty assistant content, preserving tool/thinking blocks.
            if not (m.get("role") == "assistant" and not m.get("content"))
        ]
        if not out:
            return out
        last = out[-1]
        content = last.get("content")
        if isinstance(content, str):
            last["content"] = [{
                "type": "text",
                "text": content,
                "cache_control": {"type": "ephemeral"},
            }]
        elif isinstance(content, list) and content:
            blocks = [dict(b) if isinstance(b, dict) else b for b in content]
            # cache_control is only valid on text / tool_use / tool_result /
            # image blocks — never on (redacted_)thinking.
            cacheable = {"text", "tool_use", "tool_result", "image"}
            for i in range(len(blocks) - 1, -1, -1):
                if isinstance(blocks[i], dict) and blocks[i].get("type") in cacheable:
                    blocks[i] = {**blocks[i], "cache_control": {"type": "ephemeral"}}
                    break
            last["content"] = blocks
        return out

    def _uses_adaptive_thinking(self) -> bool:
        """True for Claude models that require the adaptive thinking API instead
        of the legacy explicit budget_tokens shape this provider emits."""
        model = (self.model or "").lower()
        return any(m in model for m in _ADAPTIVE_THINKING_MODEL_MARKERS)

    def _adaptive_effort(self) -> str | None:
        """Effort for adaptive-thinking models, or None to use the model default.

        Prefers reasoning_effort; an unrecognized value (or only an explicit but
        now-irrelevant budget) falls back to "medium" rather than dropping
        thinking. Returns None only when neither knob is configured, mirroring
        the legacy budget path's "no thinking requested" behavior.
        """
        effort = (self.reasoning_effort or "").lower()
        if effort in _ADAPTIVE_EFFORT_LEVELS:
            return effort
        if self.reasoning_effort is not None or self.thinking_budget_tokens is not None:
            return "medium"
        return None

    def _apply_thinking_params(self, params: dict[str, Any], max_tokens: int) -> None:
        """Attach the appropriate thinking params for the target model in place.

        Adaptive-thinking models (Opus 4.7/4.8) use `thinking.type=adaptive` plus
        `output_config.effort`; the legacy explicit-budget shape they emitted
        before returns HTTP 400. Older models keep the
        `thinking.type=enabled` + budget_tokens path.
        """
        if self._uses_adaptive_thinking():
            effort = self._adaptive_effort()
            if effort is None:
                return
            params["thinking"] = {"type": "adaptive"}
            params["output_config"] = {"effort": effort}
            log.debug(
                "Using adaptive thinking (effort=%s) for model %s",
                effort,
                self.model,
            )
            return
        thinking = self._build_thinking_config(max_tokens)
        if thinking:
            params["thinking"] = thinking

    def _build_thinking_config(self, max_tokens: int) -> dict[str, Any] | None:
        """Map reasoning_effort onto Anthropic extended-thinking budget."""
        if self.reasoning_effort is None and self.thinking_budget_tokens is None:
            return None

        if max_tokens <= _MIN_THINKING_BUDGET_TOKENS:
            if self.thinking_budget_tokens is not None:
                log.warning(
                    "Cannot enable Claude extended thinking: max_tokens=%s must be greater "
                    "than the minimum budget_tokens=%s",
                    max_tokens,
                    _MIN_THINKING_BUDGET_TOKENS,
                )
            else:
                log.debug(
                    "Skipping Claude extended thinking for small response cap: "
                    "max_tokens=%s, minimum budget_tokens=%s",
                    max_tokens,
                    _MIN_THINKING_BUDGET_TOKENS,
                )
            return None

        budget = self.thinking_budget_tokens
        if budget is None:
            effort = (self.reasoning_effort or "medium").lower()
            if effort == "low":
                budget = _MIN_THINKING_BUDGET_TOKENS
            elif effort == "medium":
                budget = max(_MIN_THINKING_BUDGET_TOKENS, min(4096, max_tokens // 3))
            else:
                budget = max(_MIN_THINKING_BUDGET_TOKENS, min(16384, max_tokens // 2))

        budget = max(_MIN_THINKING_BUDGET_TOKENS, int(budget))
        if budget >= max_tokens:
            budget = max(_MIN_THINKING_BUDGET_TOKENS, max_tokens - _MIN_THINKING_BUDGET_TOKENS)
        if budget >= max_tokens:
            log.warning(
                "Cannot enable Claude extended thinking: budget_tokens=%s, max_tokens=%s",
                budget,
                max_tokens,
            )
            return None

        return {"type": "enabled", "budget_tokens": budget}

    def _parse_response(self, raw: anthropic.types.Message) -> LLMResponse:
        content: list[ContentBlock] = []
        raw_content: list[dict[str, Any]] = []

        for block in raw.content:
            if block.type == "text":
                content.append(TextBlock(text=block.text))
                raw_content.append({"type": "text", "text": block.text})
            elif block.type == "thinking":
                thinking_text = getattr(block, "thinking", "")
                signature = getattr(block, "signature", "")
                content.append(ThinkingBlock(text=thinking_text, signature=signature))
                raw_block = {"type": "thinking", "thinking": thinking_text}
                if signature:
                    raw_block["signature"] = signature
                raw_content.append(raw_block)
            elif block.type == "redacted_thinking":
                raw_block = {"type": "redacted_thinking"}
                data = getattr(block, "data", None)
                if data is not None:
                    raw_block["data"] = data
                raw_content.append(raw_block)
            elif block.type == "tool_use":
                content.append(ToolUseBlock(
                    id=block.id,
                    name=block.name,
                    input=block.input,
                ))
                raw_content.append({
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                })

        usage = Usage(
            input_tokens=raw.usage.input_tokens,
            output_tokens=raw.usage.output_tokens,
            cache_read_tokens=getattr(raw.usage, "cache_read_input_tokens", 0),
            cache_creation_tokens=getattr(raw.usage, "cache_creation_input_tokens", 0),
        )

        return LLMResponse(
            content=content,
            stop_reason=raw.stop_reason,
            usage=usage,
            model=raw.model,
            raw_content=raw_content,
        )

    def _assemble_content_blocks(self, blocks: dict[int, dict]) -> list[ContentBlock]:
        content: list[ContentBlock] = []
        for idx in sorted(blocks.keys()):
            b = blocks[idx]
            if b["type"] == "text":
                if b["text"]:
                    content.append(TextBlock(text=b["text"]))
            elif b["type"] == "thinking":
                content.append(ThinkingBlock(
                    text=b.get("thinking", ""),
                    signature=b.get("signature", ""),
                ))
            elif b["type"] == "tool_use":
                try:
                    tool_input = json.loads(b["input_json"]) if b["input_json"] else {}
                except json.JSONDecodeError:
                    log.warning("Failed to parse tool input JSON: %s", b["input_json"][:200])
                    tool_input = {}
                content.append(ToolUseBlock(
                    id=b["id"],
                    name=b["name"],
                    input=tool_input,
                ))
        return content

    def _content_to_raw(self, content: list[ContentBlock]) -> list[dict[str, Any]]:
        raw: list[dict[str, Any]] = []
        for block in content:
            if isinstance(block, TextBlock):
                raw.append({"type": "text", "text": block.text})
            elif isinstance(block, ThinkingBlock):
                raw_block = {"type": "thinking", "thinking": block.text}
                if block.signature:
                    raw_block["signature"] = block.signature
                raw.append(raw_block)
            elif isinstance(block, ToolUseBlock):
                raw.append({
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                })
        return raw
