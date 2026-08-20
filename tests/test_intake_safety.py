"""Regression tests for intake turn boundaries, intent routing, and path scope."""

from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from typing import Any

import pytest

from arbor.cli.intake import repl
from arbor.cli.intake.scope import (
    IntakeMode,
    IntakePathPolicy,
    extract_explicit_paths,
    infer_intake_mode,
    is_explicit_launch_approval,
)
from arbor.cli.intake.conversation_store import new_conversation, save_conversation
from arbor.core.agent import Agent
from arbor.core.config import AgentConfig
from arbor.core.llm.base import LLMResponse, TextBlock, ToolUseBlock, Usage
from arbor.core.tools.base import Tool
from arbor.core.tools.file_read import FileReadTool
from arbor.core.tools.glob_tool import GlobTool
from arbor.core.tools.grep import GrepTool


class _ScriptedProvider:
    model = "scripted-model"
    base_url = None

    def __init__(self, responses: list[LLMResponse]):
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> LLMResponse:
        self.calls.append(copy.deepcopy(kwargs))
        if not self.responses:
            raise AssertionError("unexpected extra LLM call")
        return self.responses.pop(0)

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)


class _RecordingTool(Tool):
    name = "Probe"
    description = "Record a synthetic read-only probe."
    input_schema = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }
    is_read_only = True

    def __init__(self, *, cwd: str):
        super().__init__(cwd=cwd)
        self.calls: list[dict[str, Any]] = []

    async def execute(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return "probe result"


def _text_response(text: str) -> LLMResponse:
    return LLMResponse(
        content=[TextBlock(text=text)],
        stop_reason="end_turn",
        usage=Usage(),
        raw_content=[{"type": "text", "text": text}],
    )


def _tool_response(
    *,
    text: str | None = None,
    name: str = "Probe",
    tool_input: dict[str, Any] | None = None,
    tool_id: str = "tool-1",
    native_responses_history: bool = False,
) -> LLMResponse:
    tool_input = tool_input or {"path": "target"}
    content = []
    raw_content: list[dict[str, Any]] = []
    if text is not None:
        content.append(TextBlock(text=text))
        if native_responses_history:
            raw_content.append({
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            })
        else:
            raw_content.append({"type": "text", "text": text})
    content.append(ToolUseBlock(id=tool_id, name=name, input=tool_input))
    if native_responses_history:
        raw_content.append({
            "type": "function_call",
            "call_id": tool_id,
            "name": name,
            "arguments": "{}",
        })
    else:
        raw_content.append({
            "type": "tool_use",
            "id": tool_id,
            "name": name,
            "input": tool_input,
        })
    return LLMResponse(
        content=content,
        stop_reason="tool_use",
        usage=Usage(),
        raw_content=raw_content,
    )


def _run_agent(
    tmp_path: Path,
    responses: list[LLMResponse],
    *,
    yield_on_text: bool,
    premature_stop_nudges: bool,
) -> tuple[Agent, _ScriptedProvider, _RecordingTool, str]:
    provider = _ScriptedProvider(responses)
    tool = _RecordingTool(cwd=str(tmp_path))
    agent = Agent(
        provider=provider,
        tools=[tool],
        system_prompt="test",
        config=AgentConfig(
            cwd=str(tmp_path),
            max_turns=8,
            auto_git=False,
            llm_retry_attempts=1,
            yield_on_text=yield_on_text,
            premature_stop_nudges=premature_stop_nudges,
        ),
    )
    result = asyncio.run(agent.run("只阅读 topic1.md 和 topic2.md，不要查看其他项目。"))
    return agent, provider, tool, result


def test_interactive_chinese_question_yields_without_hidden_nudge(tmp_path):
    text = "两个文件已经读完。接下来需要确认：用公开数据还是内部数据？"
    agent, provider, tool, result = _run_agent(
        tmp_path,
        [_text_response(text)],
        yield_on_text=True,
        premature_stop_nudges=False,
    )

    assert result == text
    assert len(provider.calls) == 1
    assert tool.calls == []
    assert agent.stop_reason == "awaiting_user"
    assert not any(message.get("_internal") for message in agent.messages)


@pytest.mark.parametrize("native_responses_history", [False, True])
def test_interactive_mixed_text_and_tool_is_suppressed_and_history_is_valid(
    tmp_path, native_responses_history
):
    text = "我发现父目录还有一个相关项目，先进去看看。"
    agent, provider, tool, result = _run_agent(
        tmp_path,
        [
            _tool_response(
                text=text,
                tool_input={"path": "../sibling"},
                native_responses_history=native_responses_history,
            )
        ],
        yield_on_text=True,
        premature_stop_nudges=False,
    )

    assert result == text
    assert len(provider.calls) == 1
    assert tool.calls == []
    assert agent.tool_uses == []
    assert agent.suppressed_tool_uses == [
        {"name": "Probe", "input": {"path": "../sibling"}}
    ]
    assistant_content = agent.messages[-1]["content"]
    assert all(
        block.get("type") not in {"tool_use", "function_call"}
        for block in assistant_content
    )


def test_interactive_tool_only_turn_executes_then_visible_text_yields(tmp_path):
    agent, provider, tool, result = _run_agent(
        tmp_path,
        [_tool_response(), _text_response("已根据工具结果完成分析。")],
        yield_on_text=True,
        premature_stop_nudges=False,
    )

    assert result == "已根据工具结果完成分析。"
    assert len(provider.calls) == 2
    assert tool.calls == [{"path": "target"}]
    assert agent.tool_uses == [{"name": "Probe", "input": {"path": "target"}}]
    assert any(
        isinstance(message.get("content"), list)
        and any(block.get("type") == "tool_result" for block in message["content"])
        for message in agent.messages
        if message.get("role") == "user"
    )


def test_interactive_control_tool_yields_without_another_model_call(tmp_path):
    class _ControlTool(_RecordingTool):
        yield_after_execute = True

    provider = _ScriptedProvider([_tool_response()])
    tool = _ControlTool(cwd=str(tmp_path))
    agent = Agent(
        provider=provider,
        tools=[tool],
        system_prompt="test",
        config=AgentConfig(
            cwd=str(tmp_path),
            max_turns=8,
            auto_git=False,
            llm_retry_attempts=1,
            yield_on_text=True,
            premature_stop_nudges=False,
        ),
    )

    assert asyncio.run(agent.run("stage")) == ""
    assert len(provider.calls) == 1
    assert tool.calls == [{"path": "target"}]
    assert agent.stop_reason == "awaiting_user"


def test_interactive_control_tool_failure_returns_to_model(tmp_path):
    class _FailingControlTool(_RecordingTool):
        yield_after_execute = True

        async def execute(self, **kwargs: Any) -> str:
            self.calls.append(kwargs)
            return "BLOCKED: invalid target"

        def should_yield_after_execute(self, output: str) -> bool:
            return output.startswith("STAGED")

    provider = _ScriptedProvider([
        _tool_response(),
        _text_response("Please provide the correct target path."),
    ])
    tool = _FailingControlTool(cwd=str(tmp_path))
    agent = Agent(
        provider=provider,
        tools=[tool],
        system_prompt="test",
        config=AgentConfig(
            cwd=str(tmp_path), max_turns=4, auto_git=False,
            llm_retry_attempts=1, yield_on_text=True,
            premature_stop_nudges=False,
        ),
    )

    assert asyncio.run(agent.run("stage")) == "Please provide the correct target path."
    assert len(provider.calls) == 2


def test_interactive_control_tool_batch_is_rejected_atomically(tmp_path):
    class _ControlTool(_RecordingTool):
        name = "Control"
        yield_after_execute = True

    provider = _ScriptedProvider([
        LLMResponse(
            content=[
                ToolUseBlock(id="control", name="Control", input={"path": "a"}),
                ToolUseBlock(id="probe", name="Probe", input={"path": "b"}),
            ],
            stop_reason="tool_use",
            usage=Usage(),
            raw_content=[
                {"type": "tool_use", "id": "control", "name": "Control", "input": {"path": "a"}},
                {"type": "tool_use", "id": "probe", "name": "Probe", "input": {"path": "b"}},
            ],
        ),
        _text_response("I will retry with the control tool alone."),
    ])
    control = _ControlTool(cwd=str(tmp_path))
    probe = _RecordingTool(cwd=str(tmp_path))
    agent = Agent(
        provider=provider,
        tools=[control, probe],
        system_prompt="test",
        config=AgentConfig(
            cwd=str(tmp_path), max_turns=4, auto_git=False,
            llm_retry_attempts=1, yield_on_text=True,
            premature_stop_nudges=False,
        ),
    )

    assert asyncio.run(agent.run("stage")) == "I will retry with the control tool alone."
    assert control.calls == []
    assert probe.calls == []
    assert agent.tool_uses == []
    assert {entry["name"] for entry in agent.suppressed_tool_uses} == {"Control", "Probe"}


def test_default_autonomous_agent_keeps_mixed_text_tool_behavior(tmp_path):
    agent, provider, tool, result = _run_agent(
        tmp_path,
        [
            _tool_response(text="Inspecting the target."),
            _text_response("Analysis complete."),
        ],
        yield_on_text=False,
        premature_stop_nudges=True,
    )

    assert result == "Analysis complete."
    assert len(provider.calls) == 2
    assert tool.calls == [{"path": "target"}]
    assert agent.suppressed_tool_uses == []


def test_default_autonomous_agent_keeps_premature_stop_nudge(tmp_path):
    agent, provider, tool, result = _run_agent(
        tmp_path,
        [_text_response("接下来我会检查目标。"), _text_response("检查完成。")],
        yield_on_text=False,
        premature_stop_nudges=True,
    )

    assert result == "检查完成。"
    assert len(provider.calls) == 2
    assert tool.calls == []
    assert any(
        message.get("_internal") == "premature_stop_nudge"
        for message in agent.messages
    )


@pytest.mark.parametrize(
    "future_work",
    [
        "The initial portfolio will separate the approaches and will test them next.",
        "I created the portfolio and will test it next.",
    ],
)
def test_autonomous_agent_nudges_reported_implicit_future_work(
    tmp_path, future_work
):
    agent, provider, tool, result = _run_agent(
        tmp_path,
        [
            _text_response(future_work),
            _tool_response(tool_input={"path": "portfolio.json"}),
            _text_response("The portfolio test completed successfully."),
        ],
        yield_on_text=False,
        premature_stop_nudges=True,
    )

    assert result == "The portfolio test completed successfully."
    assert len(provider.calls) == 3
    assert tool.calls == [{"path": "portfolio.json"}]
    assert any(
        message.get("_internal") == "premature_stop_nudge"
        for message in agent.messages
    )
    final_history = provider.calls[-1]["messages"]
    assert final_history[-2]["role"] == "assistant"
    assert final_history[-1]["role"] == "user"
    assert final_history[-1]["content"][0]["type"] == "tool_result"


@pytest.mark.parametrize(
    "text",
    [
        "The scheduler will retry failed jobs automatically.",
        "This portfolio will test the next approach.",
        "The scheduler will retry failed jobs next week.",
    ],
)
def test_autonomous_agent_does_not_nudge_general_will_statement(tmp_path, text):
    agent, provider, tool, result = _run_agent(
        tmp_path,
        [_text_response(text)],
        yield_on_text=False,
        premature_stop_nudges=True,
    )

    assert result == text
    assert len(provider.calls) == 1
    assert tool.calls == []
    assert agent.stop_reason == "finished"
    assert not any(message.get("_internal") for message in agent.messages)


def test_autonomous_agent_config_defaults_are_unchanged(tmp_path):
    provider = _ScriptedProvider([
        _tool_response(text="Inspecting the target."),
        _text_response("Analysis complete."),
    ])
    tool = _RecordingTool(cwd=str(tmp_path))
    agent = Agent(
        provider=provider,
        tools=[tool],
        system_prompt="test",
        config=AgentConfig(
            cwd=str(tmp_path),
            max_turns=4,
            auto_git=False,
            llm_retry_attempts=1,
        ),
    )

    assert asyncio.run(agent.run("inspect")) == "Analysis complete."
    assert tool.calls == [{"path": "target"}]
    assert len(provider.calls) == 2


@pytest.mark.parametrize(
    ("message", "current", "expected"),
    [
        ("请阅读两个 topic 并讨论 novelty", None, IntakeMode.DISCUSSION),
        ("只讨论方案，不要运行实验", IntakeMode.PLANNING, IntakeMode.DISCUSSION),
        ("improve validation score above baseline", None, IntakeMode.PLANNING),
        ("现在开始实验吧", IntakeMode.DISCUSSION, IntakeMode.PLANNING),
        ("go ahead", IntakeMode.DISCUSSION, IntakeMode.PLANNING),
        ("继续", IntakeMode.DISCUSSION, IntakeMode.DISCUSSION),
    ],
)
def test_intake_mode_routing(message, current, expected):
    assert infer_intake_mode(message, current) == expected


@pytest.mark.parametrize(
    "message",
    ["go", "yes, please", "好的，开始吧", "确认启动", "没问题，执行吧"],
)
def test_explicit_launch_approval_accepts_only_confirmation_messages(message):
    assert is_explicit_launch_approval(message)


@pytest.mark.parametrize(
    "message",
    ["yes, but use test", "可以先解释一下", "不要启动", "start with another repo"],
)
def test_explicit_launch_approval_rejects_plan_edits_and_questions(message):
    assert not is_explicit_launch_approval(message)


def test_path_extraction_handles_mixed_windows_separator_without_tab():
    message = "请阅读 work/Ali/topics\\topic2.md 和 `work/Ali/topics/topic1.md`。"
    paths = extract_explicit_paths(message)
    assert "work/Ali/topics\\topic2.md" in paths
    assert "work/Ali/topics/topic1.md" in paths
    assert all("\t" not in path for path in paths)


def _topic_tree(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    starting = tmp_path / "Arbor"
    topics = tmp_path / "work" / "Ali" / "topics"
    starting.mkdir()
    topics.mkdir(parents=True)
    topic1 = topics / "topic1.md"
    topic2 = topics / "topic2.md"
    other = topics / "other.md"
    topic1.write_text("topic one", encoding="utf-8")
    topic2.write_text("topic two", encoding="utf-8")
    other.write_text("not approved", encoding="utf-8")
    return starting, topic1, topic2, other


def test_discussion_scope_allows_only_explicit_files(tmp_path):
    starting, topic1, topic2, other = _topic_tree(tmp_path)
    policy = IntakePathPolicy(starting)
    policy.update(
        "请阅读 work/Ali/topics\\topic1.md 和 work/Ali/topics/topic2.md",
        IntakeMode.DISCUSSION,
    )

    assert policy.authorize(str(topic1.resolve())) is None
    assert policy.authorize(str(topic2.resolve())) is None
    assert policy.authorize(str(other.resolve())) is not None
    assert policy.authorize(str(topic1.parent.resolve())) is not None

    read = FileReadTool(cwd=str(starting), path_authorizer=policy.authorize)
    assert "topic one" in asyncio.run(read.execute(file_path=str(topic1)))
    assert asyncio.run(read.execute(file_path=str(other))).startswith("BLOCKED:")


def test_discussion_correction_replaces_old_scope(tmp_path):
    starting, topic1, topic2, _other = _topic_tree(tmp_path)
    policy = IntakePathPolicy(starting)
    policy.update(str(topic1), IntakeMode.DISCUSSION)
    assert policy.authorize(str(topic1.resolve())) is None

    policy.update(f"不要看之前的文件，只看 {topic2}", IntakeMode.DISCUSSION)
    assert policy.authorize(str(topic1.resolve())) is not None
    assert policy.authorize(str(topic2.resolve())) is None


def test_unquoted_conceptual_slash_does_not_grant_scope(tmp_path):
    starting = tmp_path / "project"
    conceptual = starting / "client" / "server"
    conceptual.mkdir(parents=True)
    policy = IntakePathPolicy(starting)

    policy.update("讨论 client/server 架构", IntakeMode.DISCUSSION)

    assert policy.explicit_paths == ()
    assert policy.authorize(str(conceptual.resolve())) is not None


def test_planning_scope_is_confined_to_project_and_explicit_redirect(tmp_path):
    starting = tmp_path / "project"
    sibling = tmp_path / "sibling"
    starting.mkdir()
    sibling.mkdir()
    inside = starting / "inside.txt"
    outside = sibling / "outside.txt"
    inside.write_text("inside", encoding="utf-8")
    outside.write_text("outside", encoding="utf-8")

    policy = IntakePathPolicy(starting)
    assert policy.authorize(str(inside.resolve())) is None
    assert policy.authorize(str(outside.resolve())) is not None

    policy.update(str(sibling), IntakeMode.PLANNING)
    assert policy.authorize(str(outside.resolve())) is None
    assert policy.authorize(str(inside.resolve())) is not None


def test_planning_external_file_does_not_authorize_its_directory(tmp_path):
    starting = tmp_path / "project"
    external = tmp_path / "external"
    starting.mkdir()
    external.mkdir()
    named = external / "topic.md"
    sibling = external / "secret.md"
    named.write_text("topic", encoding="utf-8")
    sibling.write_text("secret", encoding="utf-8")

    policy = IntakePathPolicy(starting)
    policy.update(str(named), IntakeMode.PLANNING)

    assert policy.authorize(str(named.resolve())) is None
    assert policy.authorize(str(sibling.resolve())) is not None
    assert policy.authorize(str(external.resolve())) is not None


def test_unresolved_explicit_planning_path_does_not_fall_back_to_cwd(tmp_path):
    starting = tmp_path / "project"
    starting.mkdir()
    inside = starting / "inside.txt"
    inside.write_text("inside", encoding="utf-8")

    policy = IntakePathPolicy(starting)
    policy.update("请在 missing/project.md 中提高 score", IntakeMode.PLANNING)

    assert policy.unresolved_paths == ("missing/project.md",)
    assert policy.authorize(str(inside.resolve())) is not None


def test_glob_blocks_parent_pattern_and_symlink_escape(tmp_path):
    starting = tmp_path / "project"
    approved = starting / "approved"
    outside = tmp_path / "outside"
    approved.mkdir(parents=True)
    outside.mkdir()
    (approved / "inside.txt").write_text("inside", encoding="utf-8")
    secret = outside / "secret.txt"
    secret.write_text("secret", encoding="utf-8")

    policy = IntakePathPolicy(starting)
    policy.update(str(approved), IntakeMode.DISCUSSION)
    glob = GlobTool(cwd=str(starting), path_authorizer=policy.authorize)

    parent_result = asyncio.run(glob.execute(pattern="../outside/*", path=str(approved)))
    assert parent_result.startswith("BLOCKED:")

    link = approved / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:  # pragma: no cover - platform permission edge
        pytest.skip(f"symlink unavailable: {exc}")

    link_result = asyncio.run(glob.execute(pattern="escape/*", path=str(approved)))
    assert link_result.startswith("BLOCKED:")
    assert "secret.txt" not in link_result

    read = FileReadTool(cwd=str(starting), path_authorizer=policy.authorize)
    assert asyncio.run(read.execute(file_path=str(link / "secret.txt"))).startswith(
        "BLOCKED:"
    )
    grep = GrepTool(cwd=str(starting), path_authorizer=policy.authorize)
    assert asyncio.run(
        grep.execute(pattern="secret", path=str(link / "secret.txt"))
    ).startswith("BLOCKED:")


class _NoopDisplay:
    def __init__(self, *args: Any, **kwargs: Any):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args: Any):
        return False


def _scripted_reader(inputs: list[str]):
    queue = list(inputs)

    async def _reader(_session):
        if not queue:
            raise EOFError
        return queue.pop(0)

    return _reader


def _run_intake(
    monkeypatch,
    cwd: Path,
    inputs: list[str],
    responses: list[LLMResponse],
):
    provider = _ScriptedProvider(responses)
    monkeypatch.setattr(repl, "_read_user_line", _scripted_reader(inputs))
    monkeypatch.setattr(repl, "_build_session", lambda *args, **kwargs: None)
    monkeypatch.setattr(repl, "_print_welcome", lambda *args, **kwargs: None)
    monkeypatch.setattr(repl, "IntakeDisplay", _NoopDisplay)
    outcome = asyncio.run(
        repl.run_intake(provider=provider, starting_cwd=cwd, seed_message=None)
    )
    return outcome, provider


def _tool_names(call: dict[str, Any]) -> set[str]:
    return {tool["name"] for tool in call["tools"]}


def test_discussion_mode_exposes_read_only_scoped_tools(monkeypatch, tmp_path):
    topic = tmp_path / "topic.md"
    topic.write_text("research topic", encoding="utf-8")
    outcome, provider = _run_intake(
        monkeypatch,
        tmp_path,
        ["请阅读 `topic.md` 并讨论这个研究方向", "/quit"],
        [_text_response("这是一个干净的研究问题。")],
    )

    assert outcome is None
    assert len(provider.calls) == 1
    assert _tool_names(provider.calls[0]) == {"Read", "Glob", "Grep"}
    assert "research discussion assistant" in provider.calls[0]["system"]
    assert str(topic.resolve()) in provider.calls[0]["system"]


def test_intake_can_switch_from_discussion_to_planning(monkeypatch, tmp_path):
    topic = tmp_path / "topic.md"
    topic.write_text("research topic", encoding="utf-8")
    outcome, provider = _run_intake(
        monkeypatch,
        tmp_path,
        [
            "请阅读 `topic.md` 并讨论这个研究方向",
            "现在开始实验吧",
            "/quit",
        ],
        [
            _text_response("这个方向值得进一步验证。"),
            _text_response("我已整理好启动计划，是否开始？"),
        ],
    )

    assert outcome is None
    assert len(provider.calls) == 2
    assert _tool_names(provider.calls[0]) == {"Read", "Glob", "Grep"}
    assert _tool_names(provider.calls[1]) == {
        "Read",
        "Glob",
        "Grep",
        "LaunchExperiment",
    }
    assert "benchmark-grinding agent" in provider.calls[1]["system"]
    assert "Bash" not in _tool_names(provider.calls[1])


def test_intake_can_switch_from_planning_back_to_discussion(monkeypatch, tmp_path):
    outcome, provider = _run_intake(
        monkeypatch,
        tmp_path,
        [
            "improve validation score above baseline",
            "先不要启动，只讨论这个研究方向",
            "/quit",
        ],
        [
            _text_response("I need one metric clarification."),
            _text_response("这个方向的核心假设是清楚的。"),
        ],
    )

    assert outcome is None
    assert "LaunchExperiment" in _tool_names(provider.calls[0])
    assert _tool_names(provider.calls[1]) == {"Read", "Glob", "Grep"}
    assert "research discussion assistant" in provider.calls[1]["system"]


def test_planning_launch_flow_still_returns_plan(monkeypatch, tmp_path):
    instruction = (
        "Maximize score from python eval.py on dev; baseline unknown; push as "
        "high as possible; novelty-leaning; do not modify eval.py or test data."
    )
    outcome, provider = _run_intake(
        monkeypatch,
        tmp_path,
        ["improve validation score above baseline", "go"],
        [
            _tool_response(
                name="LaunchExperiment",
                tool_input={"cwd": str(tmp_path), "instruction": instruction},
            ),
        ],
    )

    assert outcome is not None
    assert outcome.cwd == str(tmp_path.resolve())
    assert outcome.instruction == instruction
    # The confirmation is handled by controller code; the model cannot rewrite
    # the staged cwd/instruction after the user says go.
    assert len(provider.calls) == 1
    assert all("Bash" not in _tool_names(call) for call in provider.calls)
    conversations = list((tmp_path / ".arbor" / "conversations").glob("*/messages.jsonl"))
    assert len(conversations) == 1
    assert '"content": "go"' in conversations[0].read_text(encoding="utf-8")


def test_mixed_launch_call_cannot_launch_behind_visible_text(monkeypatch, tmp_path):
    outcome, provider = _run_intake(
        monkeypatch,
        tmp_path,
        ["improve validation score", "/quit"],
        [
            _tool_response(
                text="Should I launch this plan?",
                name="LaunchExperiment",
                tool_input={"cwd": str(tmp_path), "instruction": "improve score"},
            )
        ],
    )

    assert outcome is None
    assert len(provider.calls) == 1


@pytest.mark.parametrize("marker", ["接下来", "下一步", "我会", "应该"])
def test_repl_chinese_future_markers_do_not_auto_continue(
    monkeypatch, tmp_path, marker
):
    outcome, provider = _run_intake(
        monkeypatch,
        tmp_path,
        ["只讨论这个研究问题", "/quit"],
        [_text_response(f"{marker}先确认：你更关心理论还是实验？")],
    )

    assert outcome is None
    assert len(provider.calls) == 1
    assert not any(
        message.get("_internal")
        for message in provider.calls[0]["messages"]
    )


def test_continue_restores_context_but_not_filesystem_authority(
    monkeypatch, tmp_path
):
    project = tmp_path / "project"
    project.mkdir()
    topic = tmp_path / "topic.md"
    topic.write_text("private topic", encoding="utf-8")
    rec = new_conversation(project)
    save_conversation(
        rec,
        [
            {"role": "user", "content": f"请阅读 `{topic}` 并讨论"},
            {"role": "assistant", "content": [{"type": "text", "text": "旧回答"}]},
        ],
    )
    provider = _ScriptedProvider([_text_response("请重新点名需要读取的路径。")])
    monkeypatch.setattr(repl, "_read_user_line", _scripted_reader(["继续", "/quit"]))
    monkeypatch.setattr(repl, "_build_session", lambda *args, **kwargs: None)
    monkeypatch.setattr(repl, "_print_welcome", lambda *args, **kwargs: None)
    monkeypatch.setattr(repl, "IntakeDisplay", _NoopDisplay)

    outcome = asyncio.run(
        repl.run_intake(
            provider=provider,
            starting_cwd=project,
            seed_message=None,
            continue_latest=True,
        )
    )

    assert outcome is None
    assert "no filesystem paths are currently approved" in provider.calls[0]["system"]
    assert str(topic.resolve()) not in provider.calls[0]["system"]


def test_reset_clears_scope_and_refreshes_prompt(monkeypatch, tmp_path):
    topic = tmp_path / "topic.md"
    topic.write_text("topic", encoding="utf-8")
    outcome, provider = _run_intake(
        monkeypatch,
        tmp_path,
        [f"请阅读 `{topic}` 并讨论", "/reset", "继续讨论", "/quit"],
        [_text_response("第一轮。"), _text_response("请重新提供文件路径。")],
    )

    assert outcome is None
    assert str(topic.resolve()) in provider.calls[0]["system"]
    assert "no filesystem paths are currently approved" in provider.calls[1]["system"]
    assert [m["content"] for m in provider.calls[1]["messages"] if m["role"] == "user"] == [
        "继续讨论"
    ]
