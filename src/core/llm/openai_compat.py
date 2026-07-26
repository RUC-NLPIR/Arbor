"""OpenAI-compatible provider — supports GPT, Azure OpenAI, vLLM, Ollama, etc."""

# pylint: disable=broad-exception-caught

from __future__ import annotations

import json
import logging
import os
from typing import Any, AsyncIterator

import tiktoken
from openai import AsyncOpenAI

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
    make_tool_use_id,
)

log = logging.getLogger(__name__)


def _cached_prompt_tokens(usage: Any) -> int:
    """Read OpenAI's ``prompt_tokens_details.cached_tokens`` (object or dict)."""
    details = getattr(usage, "prompt_tokens_details", None)
    if details is None and isinstance(usage, dict):
        details = usage.get("prompt_tokens_details")
    if details is None:
        return 0
    cached = getattr(details, "cached_tokens", None)
    if cached is None and isinstance(details, dict):
        cached = details.get("cached_tokens")
    return int(cached or 0)


def _extract_logprobs(choice: Any) -> list[dict[str, Any]] | None:
    """Pull sampled-token logprobs from a chat choice, or None if absent.

    Returns ``[{token, logprob}, ...]`` for token-faithful traces. Endpoints
    that don't return logprobs (most non-OpenAI gateways) yield None.
    """
    lp = getattr(choice, "logprobs", None)
    content = getattr(lp, "content", None) if lp is not None else None
    if not content:
        return None
    out: list[dict[str, Any]] = []
    for tok in content:
        out.append({"token": getattr(tok, "token", None), "logprob": getattr(tok, "logprob", None)})
    return out or None


class OpenAICompatProvider(LLMProvider):
    """LLM provider for any OpenAI-compatible API.

    Works with:
    - OpenAI (GPT-4o, o1, etc.)
    - Azure OpenAI
    - vLLM (with --served-model-name)
    - Ollama (via OpenAI-compatible endpoint)
    - LiteLLM proxy
    - Any other OpenAI-compatible server
    """

    def __init__(
        self,
        model: str = "gpt-4o",
        api_key: str | None = None,
        base_url: str | None = None,
        max_retries: int = 3,
        timeout: float = 300.0,
        logprobs: bool = False,
    ):
        self.model = model
        self.base_url = base_url
        self.timeout = timeout
        self.logprobs = logprobs
        self._client = AsyncOpenAI(
            api_key=api_key or os.environ.get("OPENAI_API_KEY", "dummy"),
            base_url=base_url,
            max_retries=max_retries,
            timeout=timeout,
        )
        try:
            self._enc = tiktoken.encoding_for_model(model)
        except Exception:
            self._enc = tiktoken.get_encoding("cl100k_base")

    # ------------------------------------------------------------------
    # Transport seam — subclasses (e.g. LiteLLMProvider) override this to
    # swap the chat-completions backend while reusing all the message
    # conversion, streaming, and parsing logic below.
    # ------------------------------------------------------------------

    async def _acompletion(self, **params: Any) -> Any:
        return await self._client.chat.completions.create(**params)

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
        oai_messages = self._convert_messages(system, messages)
        oai_tools = self._convert_tools(tools) if tools else None

        params: dict[str, Any] = {
            "model": self.model,
            "messages": oai_messages,
            "max_tokens": max_tokens,
        }
        # Token-faithful traces: ask for sampled-token logprobs when enabled. Opt-in
        # — some OpenAI-compatible endpoints (Gemini, Ollama) reject the field with
        # HTTP 400 instead of ignoring it. _extract_logprobs degrades to None when
        # the endpoint returns none.
        if self.logprobs:
            params["logprobs"] = True
        if oai_tools:
            params["tools"] = oai_tools
            params["tool_choice"] = "auto"

        raw = await self._acompletion(**params)
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
        oai_messages = self._convert_messages(system, messages)
        oai_tools = self._convert_tools(tools) if tools else None

        params: dict[str, Any] = {
            "model": self.model,
            "messages": oai_messages,
            "max_tokens": max_tokens,
            "stream": True,
        }
        if oai_tools:
            params["tools"] = oai_tools
            params["tool_choice"] = "auto"

        text_accum = ""
        reasoning_accum = ""
        tool_calls_accum: dict[int, dict] = {}

        async for chunk in await self._acompletion(**params):
            delta = chunk.choices[0].delta if chunk.choices else None
            if not delta:
                continue

            # Reasoning content (prefer `reasoning_content`, fall back to `reasoning`)
            reasoning_piece = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
            if reasoning_piece:
                reasoning_accum += reasoning_piece
                yield StreamThinkingDelta(text=reasoning_piece)

            # Text content
            if delta.content:
                text_accum += delta.content
                yield StreamTextDelta(text=delta.content)

            # Tool calls
            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    if idx not in tool_calls_accum:
                        tool_calls_accum[idx] = {
                            "id": tc_delta.id or "",
                            "name": "",
                            "arguments": "",
                        }
                    entry = tool_calls_accum[idx]
                    if tc_delta.id:
                        entry["id"] = tc_delta.id
                    if tc_delta.function:
                        if tc_delta.function.name:
                            entry["name"] = tc_delta.function.name
                            yield StreamToolStart(id=entry["id"], name=entry["name"])
                        if tc_delta.function.arguments:
                            entry["arguments"] += tc_delta.function.arguments
                            yield StreamToolDelta(partial_json=tc_delta.function.arguments)

        # Build final response
        content: list[ContentBlock] = []
        raw_content: list[dict[str, Any]] = []

        if reasoning_accum:
            content.append(ThinkingBlock(text=reasoning_accum))
            raw_content.append({"type": "thinking", "text": reasoning_accum})

        if text_accum:
            content.append(TextBlock(text=text_accum))
            raw_content.append({"type": "text", "text": text_accum})

        for idx in sorted(tool_calls_accum.keys()):
            tc = tool_calls_accum[idx]
            try:
                tool_input = json.loads(tc["arguments"]) if tc["arguments"] else {}
            except json.JSONDecodeError:
                tool_input = {}

            # Map OpenAI function call ID to our format
            tool_id = tc["id"] or make_tool_use_id()
            content.append(ToolUseBlock(id=tool_id, name=tc["name"], input=tool_input))
            raw_content.append({
                "type": "tool_use",
                "id": tool_id,
                "name": tc["name"],
                "input": tool_input,
            })

        stop_reason = "tool_use" if tool_calls_accum else "end_turn"
        resp = LLMResponse(
            content=content,
            stop_reason=stop_reason,
            usage=Usage(),  # Streaming doesn't always give usage
            model=self.model,
            raw_content=raw_content,
        )
        yield StreamDone(response=resp)

    # ------------------------------------------------------------------
    # Token counting
    # ------------------------------------------------------------------

    def count_tokens(self, text: str) -> int:
        return len(self._enc.encode(text))

    # ------------------------------------------------------------------
    # Message format conversion: Anthropic → OpenAI
    # ------------------------------------------------------------------

    def _convert_messages(
        self,
        system: str,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Convert Anthropic-format messages to OpenAI chat format."""
        oai_msgs: list[dict[str, Any]] = [{"role": "system", "content": system}]

        for msg in messages:
            role = msg["role"]
            content = msg.get("content", "")

            if isinstance(content, str):
                oai_msgs.append({"role": role, "content": content})
                continue

            if not isinstance(content, list):
                oai_msgs.append({"role": role, "content": str(content)})
                continue

            # Handle Anthropic content block arrays
            if role == "assistant":
                oai_msgs.append(self._build_assistant_message(content))

            elif role == "user":
                # User messages with tool_result blocks
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "tool_result":
                            oai_msgs.append({
                                "role": "tool",
                                "tool_call_id": block["tool_use_id"],
                                "content": block.get("content", ""),
                            })
                        elif block.get("type") == "text":
                            oai_msgs.append({"role": "user", "content": block["text"]})
                    elif isinstance(block, str):
                        oai_msgs.append({"role": "user", "content": block})

        return oai_msgs

    def _build_assistant_message(self, content: list[Any]) -> dict[str, Any]:
        """Build one OpenAI assistant message from our content blocks.

        Reasoning IS replayed as ``reasoning_content``: reasoning chat models
        need their prior reasoning back to stay coherent (DeepSeek's thinking
        mode *requires* it on every assistant message; others ignore an
        unrecognised field). Subclasses routing through litellm additionally
        attach signed Anthropic ``thinking_blocks`` (see :class:`LiteLLMProvider`).
        """
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        reasoning_opaque: str | None = None
        tool_calls_list: list[dict] = []
        for block in content:
            if isinstance(block, dict):
                btype = block.get("type")
                if btype == "text":
                    text_parts.append(block["text"])
                elif btype == "thinking":
                    reasoning_parts.append(block.get("thinking") or block.get("text") or "")
                    if block.get("opaque"):
                        reasoning_opaque = block["opaque"]
                elif btype == "tool_use":
                    tool_calls_list.append({
                        "id": block["id"],
                        "type": "function",
                        "function": {
                            "name": block["name"],
                            "arguments": json.dumps(block.get("input", {})),
                        },
                    })

        oai_msg: dict[str, Any] = {"role": "assistant"}
        oai_msg["content"] = "\n".join(text_parts) if text_parts else None
        joined_reasoning = "\n".join(p for p in reasoning_parts if p)
        if joined_reasoning:
            # `reasoning_content` is the field DeepSeek/vLLM use; `reasoning` is
            # the OpenRouter alias; `reasoning_text` is the Copilot-proxy display
            # field. Backends that recognise none of them ignore them.
            oai_msg["reasoning_content"] = joined_reasoning
            oai_msg["reasoning"] = joined_reasoning
            oai_msg["reasoning_text"] = joined_reasoning
        if reasoning_opaque:
            # The opaque token is the replayable reasoning state (Copilot-style
            # proxies); sending it back keeps the chain coherent across turns.
            oai_msg["reasoning_opaque"] = reasoning_opaque
        if tool_calls_list:
            oai_msg["tool_calls"] = tool_calls_list
        return oai_msg

    def _convert_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert Anthropic tool schemas to OpenAI function schemas."""
        oai_tools: list[dict[str, Any]] = []
        for tool in tools:
            oai_tools.append({
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool["description"],
                    "parameters": tool["input_schema"],
                },
            })
        return oai_tools

    def _parse_response(self, raw: Any) -> LLMResponse:
        """Parse OpenAI response into unified format."""
        choice = raw.choices[0]
        message = choice.message

        content: list[ContentBlock] = []
        raw_content: list[dict[str, Any]] = []

        # Reasoning — backends differ on field names: reasoning_content
        # (DeepSeek/vLLM), reasoning (OpenRouter), reasoning_text + an opaque
        # replay token reasoning_opaque (GitHub-Copilot-style proxies). litellm
        # stashes provider-specific fields in ``provider_specific_fields``, so
        # check there too.
        psf = getattr(message, "provider_specific_fields", None) or {}

        def _field(name: str) -> Any:
            value = getattr(message, name, None)
            if value is None and isinstance(psf, dict):
                value = psf.get(name)
            return value

        reasoning_text = _field("reasoning_content") or _field("reasoning") or _field("reasoning_text")
        reasoning_opaque = _field("reasoning_opaque")
        if reasoning_text or reasoning_opaque:
            content.append(ThinkingBlock(text=reasoning_text or ""))
            block: dict[str, Any] = {"type": "thinking", "text": reasoning_text or ""}
            if reasoning_opaque:
                block["opaque"] = reasoning_opaque  # carried back to maintain the chain
            raw_content.append(block)

        if message.content:
            content.append(TextBlock(text=message.content))
            raw_content.append({"type": "text", "text": message.content})

        if message.tool_calls:
            for tc in message.tool_calls:
                try:
                    tool_input = json.loads(tc.function.arguments) if tc.function.arguments else {}
                except json.JSONDecodeError:
                    tool_input = {}
                content.append(ToolUseBlock(
                    id=tc.id,
                    name=tc.function.name,
                    input=tool_input,
                ))
                raw_content.append({
                    "type": "tool_use",
                    "id": tc.id,
                    "name": tc.function.name,
                    "input": tool_input,
                })

        usage = Usage()
        if raw.usage:
            prompt = int(getattr(raw.usage, "prompt_tokens", 0) or 0)
            cached = _cached_prompt_tokens(raw.usage)
            # OpenAI counts cached tokens inside prompt_tokens; split them out so
            # cache hit-rate is measurable without double-counting (#13).
            usage.input_tokens = max(0, prompt - cached)
            usage.cache_read_tokens = cached
            usage.output_tokens = int(getattr(raw.usage, "completion_tokens", 0) or 0)

        stop_reason = "tool_use" if message.tool_calls else "end_turn"
        return LLMResponse(
            content=content,
            stop_reason=stop_reason,
            usage=usage,
            model=raw.model or self.model,
            raw_content=raw_content,
            logprobs=_extract_logprobs(choice),
        )
