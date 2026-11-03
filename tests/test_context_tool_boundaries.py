"""Compaction must retain complete tool exchanges across every truncation path."""

from __future__ import annotations

import asyncio
import copy
import json

import anthropic
import httpx
import pytest

from arbor.core import create_provider
from arbor.core.agent import Agent
from arbor.core.config import AgentConfig
from arbor.core.context import ContextManager
from arbor.core.llm.base import LLMResponse, TextBlock
from arbor.core.llm.openai_compat import OpenAICompatProvider
from arbor.core.llm.openai_responses import OpenAIResponsesProvider
from arbor.core.tools.base import Tool


_STYLES = ["normalized", "responses", "responses-native-output"]


def _calls(style, *ids):
    if style == "normalized":
        blocks = [{"type": "thinking", "thinking": "inspect", "signature": "signed"}]
        blocks.extend(
            {"type": "tool_use", "id": call_id, "name": "Probe", "input": {"path": call_id}}
            for call_id in ids
        )
    else:
        blocks = [{"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque"}]
        blocks.extend(
            {"type": "function_call", "id": f"fc_{call_id}", "call_id": call_id,
             "name": "Probe", "arguments": "{}"}
            for call_id in ids
        )
    return {"role": "assistant", "content": blocks}


def _results(style, *ids):
    if style == "responses-native-output":
        blocks = [
            {"type": "function_call_output", "call_id": call_id, "output": "result"}
            for call_id in ids
        ]
    else:
        # The agent loop uses normalized tool_result even with Responses calls.
        blocks = [
            {"type": "tool_result", "tool_use_id": call_id, "content": "result"}
            for call_id in ids
        ]
    return {"role": "user", "content": blocks}


def _history(style):
    return [
        {"role": "user", "content": "Inspect the files."},
        {"role": "assistant", "content": "Starting."},
        _calls(style, "old"),
        _results(style, "old"),
        {"role": "assistant", "content": "Old work is complete."},
        _calls(style, "batch-a", "batch-b"),
        _results(style, "batch-a"),
        _results(style, "batch-b"),
        {"role": "user", "content": "Background task finished. " * 10},
        _calls(style, "recent"),
        _results(style, "recent"),
        {"role": "user", "content": "Please also check the logs. " * 10},
    ]


def _assert_paired(messages):
    calls = []
    results = []
    for message in messages:
        if not isinstance(message["content"], list):
            continue
        for block in message["content"]:
            kind = block["type"]
            if kind in {"tool_use", "function_call"}:
                calls.append(block.get("call_id") or block["id"])
            elif kind in {"tool_result", "function_call_output"}:
                call_id = block.get("tool_use_id") or block["call_id"]
                assert call_id in calls, f"orphaned result: {call_id}"
                results.append(call_id)
    assert len(calls) == len(set(calls)), "duplicated tool calls"
    assert sorted(results) == sorted(calls), "tool exchange was split"


class _SummaryProvider:
    model = "summary-test"

    def __init__(self, *, fail=False):
        self.fail = fail
        self.requests = []

    def count_tokens(self, text):
        return len(text)

    async def create(self, **kwargs):
        self.requests.append(copy.deepcopy(kwargs))
        if self.fail:
            raise RuntimeError("summary unavailable")
        return LLMResponse(content=[TextBlock("summary")], stop_reason="end_turn")


def _manager(*, fail=False, keep_recent=5):
    return ContextManager(
        provider=_SummaryProvider(fail=fail), context_window=128,
        compact_threshold=0.5, keep_recent=keep_recent,
    )


@pytest.mark.parametrize("style", _STYLES)
def test_summary_keeps_entire_parallel_tool_exchange(style):
    messages = _history(style)
    original = copy.deepcopy(messages)
    manager = _manager()
    compacted = asyncio.run(manager._summarize_old_messages(messages))

    assert compacted[0]["_internal"] == "context_summary"
    assert compacted[2:] == original[5:]  # Nominal cut 7 splits the parallel results.
    _assert_paired(compacted)
    assert messages == original


@pytest.mark.parametrize("style", _STYLES)
def test_failed_summary_keeps_calls_for_retained_results(style):
    messages = _history(style)
    compacted = asyncio.run(_manager(fail=True)._summarize_old_messages(messages))

    assert compacted == messages[5:]  # Nominal halfway cut 6 lands on a result.
    _assert_paired(compacted)


@pytest.mark.parametrize("style", _STYLES)
@pytest.mark.parametrize("fail", [False, True])
def test_emergency_truncation_preserves_pairs_without_duplicating_prefix(style, fail):
    messages = _history(style)
    original = copy.deepcopy(messages)
    manager = _manager(fail=fail)
    compacted = asyncio.run(manager.maybe_compact(messages, "system"))

    assert len(manager.provider.requests) == 1
    if fail:
        assert compacted == original[9:]
    else:
        assert compacted[0]["_internal"] == "context_summary"
        assert compacted[2:] == original[9:]
    _assert_paired(compacted)
    assert messages == original


@pytest.mark.parametrize("style", _STYLES)
def test_note_at_cut_does_not_hide_a_pending_tool_call(style):
    messages = _history(style)
    # The proposed cut is a plain user note, followed by the second result.
    # Checking only the block type at the cut would miss the earlier call.
    messages.insert(7, {"role": "user", "content": "background notification"})
    compacted = asyncio.run(_manager(keep_recent=6)._summarize_old_messages(messages))

    assert compacted[2:] == messages[5:]
    _assert_paired(compacted)


@pytest.mark.parametrize("style", _STYLES)
@pytest.mark.parametrize("keep_recent", [0, 1])
def test_single_long_exchange_is_kept_intact_when_no_safe_cut_exists(style, keep_recent):
    messages = [
        _calls(style, "a", "b"),
        _results(style, "a"),
        {"role": "user", "content": "notification" * 20},
        _results(style, "b"),
    ]
    manager = _manager(keep_recent=keep_recent)
    assert asyncio.run(manager.maybe_compact(messages, "system")) == messages
    assert manager.provider.requests == []


@pytest.mark.parametrize("style", _STYLES)
def test_emergency_keeps_overlapping_summary_and_tail_once(style):
    messages = [
        {"role": "user", "_internal": "context_summary", "content": "summary" * 100},
        {"role": "assistant", "content": "Understood."},
        _calls(style, "a"),
        _results(style, "a"),
    ]
    assert asyncio.run(_manager(keep_recent=20).maybe_compact(messages, "system")) == messages


@pytest.mark.parametrize("backend", ["openai-chat", "openai-responses"])
def test_retained_calls_survive_openai_request_conversion(backend):
    style = "normalized" if backend == "openai-chat" else "responses"
    compacted = asyncio.run(_manager()._summarize_old_messages(_history(style)))
    if backend == "openai-chat":
        provider = object.__new__(OpenAICompatProvider)
        converted = provider._convert_messages("system", compacted)
        calls = {
            call["id"] for message in converted for call in message.get("tool_calls", [])
        }
        results = {message["tool_call_id"] for message in converted if message["role"] == "tool"}
    else:
        provider = object.__new__(OpenAIResponsesProvider)
        converted = provider._convert_messages(compacted)
        calls = {item["call_id"] for item in converted if item.get("type") == "function_call"}
        results = {
            item["call_id"] for item in converted if item.get("type") == "function_call_output"
        }
    assert calls == results == {"batch-a", "batch-b", "recent"}


@pytest.mark.parametrize("notification_source", ["dashboard", "background"])
def test_agent_finishes_after_compaction_with_inter_turn_notifications(tmp_path, notification_source):
    state = {"turn": 0, "summaries": 0, "polls": 0}

    def notification():
        state["polls"] += 1
        return ["please also check the logs"] if state["polls"] == 12 else []

    class Probe(Tool):
        name = "Probe"
        description = "Read a synthetic file."
        input_schema = {"type": "object", "properties": {"path": {"type": "string"}}}
        is_read_only = True

        async def execute(self, **kwargs):
            return "result line " * 30  # Too short for layers 1-3 to truncate.

        def drain_notifications(self):
            return notification() if notification_source == "background" else []

    def handler(request):
        body = json.loads(request.content)
        is_summary = body["system"][0]["text"].startswith("You are a conversation summarizer")
        if is_summary:
            state["summaries"] += 1
            content = [{"type": "text", "text": "Summary: probed files."}]
        else:
            # Like Anthropic, merge consecutive same-role messages before
            # validating that results answer the preceding assistant's calls.
            merged = []
            for message in body["messages"]:
                blocks = message["content"]
                if isinstance(blocks, str):
                    blocks = [{"type": "text", "text": blocks}]
                if merged and merged[-1][0] == message["role"]:
                    merged[-1][1].extend(blocks)
                else:
                    merged.append((message["role"], list(blocks)))
            for i, (_, blocks) in enumerate(merged):
                previous_ids = {
                    block["id"] for block in merged[i - 1][1] if block["type"] == "tool_use"
                } if i else set()
                for block in blocks:
                    if block["type"] == "tool_result" and block["tool_use_id"] not in previous_ids:
                        return httpx.Response(400, json={"type": "error", "error": {
                            "type": "invalid_request_error", "message": "orphaned tool_result",
                        }})
            state["turn"] += 1
            if state["turn"] < 25:
                content = [{
                    "type": "tool_use", "id": f"toolu_{state['turn']}",
                    "name": "Probe", "input": {"path": f"f{state['turn']}"},
                }]
            else:
                content = [{"type": "text", "text": "All done."}]
        return httpx.Response(200, json={
            "id": "msg_test", "type": "message", "role": "assistant",
            "model": "claude-sonnet-4-5", "content": content,
            "stop_reason": "tool_use" if content[0]["type"] == "tool_use" else "end_turn",
            "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 3},
        })

    async def run():
        config = AgentConfig(
            cwd=str(tmp_path), provider="anthropic", model="claude-sonnet-4-5",
            api_key="test", auto_git=False, max_turns=40, context_window=1500,
            llm_retry_attempts=1, track_stats=False,
            inter_turn_user_messages=notification if notification_source == "dashboard" else None,
        )
        provider = create_provider(config)
        await provider._client.close()
        async with anthropic.AsyncAnthropic(
            api_key="test", max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        ) as client:
            provider._client = client
            agent = Agent(
                provider=provider, tools=[Probe(cwd=str(tmp_path))],
                system_prompt="sys", config=config,
            )
            assert await agent.run("Probe the files one by one.") == "All done."
            assert state["summaries"] > 0
            assert state["turn"] == 25

    asyncio.run(run())
