"""Reality-blind provider for native-start clean-wheel commissioning."""

from __future__ import annotations

import json
from typing import Any

from arbor.core.llm.base import LLMResponse, TextBlock, ToolUseBlock, Usage


class NativeStartProvider:
    model = "aros-native-start-fixture"
    base_url = None

    def __init__(self, *, source_ref: str, restart: bool = False) -> None:
        self.source_ref = source_ref
        self.restart = restart
        self.step = 0
        self.expected_tool_id: str | None = None

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    async def create(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 16_384,
    ) -> LLMResponse:
        del system, tools, max_tokens
        if self.step == 0:
            if self.restart:
                return self._tool("Research", {"action": "attention"})
            return self._tool(
                "Read",
                {"file_path": "questions/Q-0001/question.md"},
            )
        result = self._result(messages)
        if self.restart:
            packet = self._object(result)
            active = packet.get("active_question")
            if not isinstance(active, dict) or active.get("id") != "Q-0001":
                raise ValueError("restart Attention lacks Q-0001")
            return self._text("Recovered Q-0001 from canonical Attention.")
        if self.step == 1:
            return self._tool("Read", {"file_path": self.source_ref})
        if self.step == 2:
            return self._text("Question and local source observed.")
        raise ValueError("unexpected native-start provider step")

    def _tool(self, name: str, tool_input: dict[str, Any]) -> LLMResponse:
        tool_id = f"native-start-{self.step:02d}"
        self.step += 1
        self.expected_tool_id = tool_id
        raw = {
            "type": "tool_use",
            "id": tool_id,
            "name": name,
            "input": tool_input,
        }
        return LLMResponse(
            content=[ToolUseBlock(id=tool_id, name=name, input=tool_input)],
            stop_reason="tool_use",
            usage=Usage(),
            raw_content=[raw],
        )

    def _text(self, text: str) -> LLMResponse:
        self.step += 1
        self.expected_tool_id = None
        return LLMResponse(
            content=[TextBlock(text=text)],
            stop_reason="end_turn",
            usage=Usage(),
            raw_content=[{"type": "text", "text": text}],
        )

    def _result(self, messages: list[dict[str, Any]]) -> str:
        if not messages or messages[-1].get("role") != "user":
            raise ValueError("latest message must be a tool result")
        content = messages[-1].get("content")
        if not isinstance(content, list) or len(content) != 1:
            raise ValueError("exactly one tool result is required")
        block = content[0]
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            raise ValueError("latest message is not a tool result")
        if block.get("tool_use_id") != self.expected_tool_id:
            raise ValueError("tool result ID mismatch")
        if block.get("is_error") is True:
            raise ValueError("tool result is_error")
        result = block.get("content")
        if not isinstance(result, str):
            raise ValueError("tool result content must be text")
        return result

    @staticmethod
    def _object(value: str) -> dict[str, Any]:
        decoded = json.loads(value)
        if not isinstance(decoded, dict):
            raise ValueError("Attention result must be an object")
        return decoded


__all__ = ["NativeStartProvider"]
