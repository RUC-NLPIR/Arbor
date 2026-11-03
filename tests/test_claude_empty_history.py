"""Empty Claude end_turn replies must not poison later or resumed requests (#69)."""

from __future__ import annotations

import asyncio
import copy
import json
from contextlib import asynccontextmanager

import anthropic
import httpx
import pytest

from arbor.cli.intake.conversation_store import (
    load_messages,
    new_conversation,
    save_conversation,
)
from arbor.coordinator.checkpoint import read_messages, write_messages
from arbor.core import create_provider
from arbor.core.agent import Agent
from arbor.core.config import AgentConfig
from arbor.core.llm.claude import ClaudeProvider
from arbor.core.tools.base import Tool


class _Probe(Tool):
    name = "Probe"
    description = "Return a synthetic read-only result."
    input_schema = {"type": "object", "properties": {}}
    is_read_only = True

    async def execute(self, **kwargs):
        return "probe result"


def _reply(content, stop_reason="end_turn"):
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-4-5",
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 3},
    }


def _tool_history():
    return [
        {"role": "user", "content": "Inspect x for me."},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_1", "name": "Probe", "input": {}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "probe result"},
        ]},
    ]


@asynccontextmanager
async def _agent(tmp_path, replies, *, yield_on_text=False):
    requests = []
    replies = iter(replies)

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        for i, message in enumerate(body["messages"]):
            final_assistant = i == len(body["messages"]) - 1 and message["role"] == "assistant"
            if not message["content"] and not final_assistant:
                return httpx.Response(400, json={"type": "error", "error": {
                    "type": "invalid_request_error",
                    "message": f"messages.{i}: all messages must have non-empty content "
                               "except for the optional final assistant message",
                }})
        return httpx.Response(200, json=next(replies))

    config = AgentConfig(
        cwd=str(tmp_path), provider="anthropic", model="claude-sonnet-4-5",
        api_key="test", auto_git=False, track_stats=False, max_turns=4,
        llm_retry_attempts=1, yield_on_text=yield_on_text,
    )
    provider = create_provider(config)
    await provider._client.close()
    async with anthropic.AsyncAnthropic(
        api_key="test", max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    ) as client:
        provider._client = client
        agent = Agent(
            provider=provider, tools=[_Probe(cwd=str(tmp_path))],
            system_prompt="test", config=config,
        )
        yield agent, requests


@pytest.mark.parametrize("yield_on_text", [False, True])
def test_empty_end_turn_does_not_poison_followup_or_saved_history(tmp_path, yield_on_text):
    async def run():
        replies = [
            _reply(_tool_history()[1]["content"], "tool_use"),
            _reply([]),
            _reply([{"type": "text", "text": "Here is what I found."}]),
        ]
        async with _agent(tmp_path, replies, yield_on_text=yield_on_text) as (agent, requests):
            assert await agent.run("Inspect x for me.") == ""
            assert agent.messages == _tool_history()
            assert len(requests) == 2  # An empty end_turn is not retried.

            path = tmp_path / "messages.jsonl"
            write_messages(path, agent.messages)
            assert read_messages(path) == _tool_history()

            assert await agent.run("What did you find?") == "Here is what I found."
            assert len(requests) == 3
            assert all(message["content"] for message in requests[-1]["messages"])

    asyncio.run(run())


@pytest.mark.parametrize("empty_content", [[], ""])
@pytest.mark.parametrize("store", ["checkpoint", "intake"])
def test_resume_recovers_previously_saved_empty_assistant(tmp_path, empty_content, store):
    history = [*_tool_history(), {"role": "assistant", "content": empty_content}]
    if store == "checkpoint":
        path = tmp_path / "messages.jsonl"
        write_messages(path, history)
        restored = read_messages(path)
    else:
        record = new_conversation(tmp_path)
        save_conversation(record, history)
        restored = load_messages(record)
    assert restored[-1]["content"] == empty_content

    async def run():
        async with _agent(tmp_path, [_reply([{"type": "text", "text": "Recovered."}])]) as (
            agent, requests,
        ):
            agent.messages = restored
            assert await agent.run("What did you find?") == "Recovered."
            assert len(requests) == 1
            sent = requests[0]["messages"]
            assert [message["role"] for message in sent] == ["user", "assistant", "user", "user"]
            assert sent[1]["content"] == _tool_history()[1]["content"]
            assert sent[2]["content"][0]["tool_use_id"] == "toolu_1"

    asyncio.run(run())


@pytest.mark.parametrize("block", [
    {"type": "tool_use", "id": "toolu_1", "name": "Probe", "input": {}},
    {"type": "thinking", "thinking": "reasoning", "signature": "signature"},
    {"type": "redacted_thinking", "data": "opaque"},
])
def test_cache_messages_preserves_nontext_assistant_content_without_mutation(block):
    messages = [
        {"role": "user", "content": "inspect"},
        {"role": "assistant", "content": [block]},
        {"role": "assistant", "content": []},
    ]
    original = copy.deepcopy(messages)
    prepared = ClaudeProvider._cache_messages(messages)

    assert messages == original
    assert len(prepared) == 2
    cached_block = prepared[-1]["content"][0]
    if block["type"] == "tool_use":
        assert cached_block == {**block, "cache_control": {"type": "ephemeral"}}
    else:
        assert cached_block == block  # Thinking blocks must not receive cache_control.


@pytest.mark.parametrize("messages", [
    [],
    [{"role": "assistant", "content": []}],
    [{"role": "assistant", "content": ""}],
])
def test_cache_messages_handles_empty_history_after_filtering(messages):
    assert ClaudeProvider._cache_messages(messages) == []
