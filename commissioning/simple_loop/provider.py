"""Deterministic provider for the replacement AROS E2E."""

from __future__ import annotations

import json
from typing import Any

from arbor.core.llm.base import LLMResponse, TextBlock, ToolUseBlock, Usage


class SimpleLoopProvider:
    model = "aros-simple-loop-fixture"
    base_url = None

    def __init__(self, *, restart: bool = False) -> None:
        self.restart = restart
        self.step = 0
        self.phase: str | None = None
        self.expected_tool_id: str | None = None
        self.task_id: str | None = None
        self.child_commit: str | None = None
        self.return_commit: str | None = None
        self.eval_id: str | None = None
        self.collected_ref: str | None = None
        self.eval_ref: str | None = None

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
        if self.expected_tool_id is None:
            if self.phase is not None:
                raise ValueError("provider is already complete")
            return self._tool(
                "Attention",
                {},
                phase="restart_attention" if self.restart else "initial_attention",
            )
        result = self._last_tool_result(messages)
        return self._restart(result) if self.restart else self._primary(result)

    def _tool(
        self,
        name: str,
        tool_input: dict[str, Any],
        *,
        phase: str,
    ) -> LLMResponse:
        tool_id = f"simple-{self.step:02d}"
        self.step += 1
        self.phase = phase
        self.expected_tool_id = tool_id
        raw = {"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}
        return LLMResponse(
            content=[ToolUseBlock(id=tool_id, name=name, input=tool_input)],
            stop_reason="tool_use",
            usage=Usage(),
            raw_content=[raw],
        )

    def _text(self, text: str) -> LLMResponse:
        self.phase = "done"
        self.expected_tool_id = None
        return LLMResponse(
            content=[TextBlock(text=text)],
            stop_reason="end_turn",
            usage=Usage(),
            raw_content=[{"type": "text", "text": text}],
        )

    def _last_tool_result(self, messages: list[dict[str, Any]]) -> str:
        if not messages or messages[-1].get("role") != "user":
            raise ValueError("latest message must be a tool result")
        content = messages[-1].get("content")
        if not isinstance(content, list) or len(content) != 1:
            raise ValueError("exactly one tool result is required")
        result = content[0]
        if not isinstance(result, dict) or result.get("type") != "tool_result":
            raise ValueError("latest message is not a tool result")
        if result.get("tool_use_id") != self.expected_tool_id:
            raise ValueError("tool result ID mismatch")
        if result.get("is_error") is True:
            raise ValueError("tool result is_error")
        value = result.get("content")
        if not isinstance(value, str):
            raise ValueError("tool result content must be text")
        return value

    @staticmethod
    def _object(raw: str) -> dict[str, Any]:
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("tool result must be an object")
        return value

    @staticmethod
    def _string(value: dict[str, Any], field: str) -> str:
        item = value.get(field)
        if not isinstance(item, str) or not item:
            raise ValueError(f"tool result lacks {field}")
        return item

    def _primary(self, raw: str) -> LLMResponse:
        if self.phase == "initial_attention":
            packet = self._object(raw)
            if packet.get("unread_returns") != []:
                raise ValueError("initial attention has unread returns")
            return self._write("model/CURRENT.md", self._preregistered_model(), "prereg_model")
        if self.phase == "prereg_model":
            return self._write("ideas/I-E2E.md", self._preregistered_idea(), "prereg_idea")
        if self.phase == "prereg_idea":
            return self._tool(
                "Checkpoint",
                {
                    "message": "Preregister deterministic mechanism and test.",
                    "paths": ["ideas/I-E2E.md", "model/CURRENT.md"],
                },
                phase="prereg_checkpoint",
            )
        if self.phase == "prereg_checkpoint":
            self._string(self._object(raw), "commit")
            return self._tool(
                "Task",
                {
                    "action": "create",
                    "objective": "Produce the deterministic success candidate.",
                    "mode": "write",
                    "adapter_argv": [
                        "python3",
                        "commissioning/simple_loop/task_adapter.py",
                    ],
                    "capabilities": {"network": False, "shell": True},
                    "deliverables": ["candidate-mode.txt"],
                    "acceptance": ["candidate-mode.txt equals success"],
                    "timeout_seconds": 120,
                    "idempotency_key": "simple-loop-task",
                },
                phase="task_create",
            )
        if self.phase == "task_create":
            self.task_id = self._string(self._object(raw), "task_id")
            return self._task("start", "task_start")
        if self.phase == "task_start":
            return self._task("status", "task_status")
        if self.phase == "task_status":
            result = self._object(raw)
            state = result.get("state")
            if state in {"created", "starting", "running"}:
                return self._task("status", "task_status")
            if state != "completed":
                raise ValueError(f"Task ended in {state!r}")
            return self._task("collect", "task_collect")
        if self.phase == "task_collect":
            result = self._object(raw)
            self.child_commit = self._string(result, "child_commit")
            self.return_commit = self._string(result, "return_commit")
            self.collected_ref = f"tasks/{self.task_id}/collected.json"
            return self._tool(
                "Eval",
                {
                    "action": "run",
                    "evaluator_id": "simple-loop",
                    "version": "1",
                    "candidate_commit": self.child_commit,
                    "idempotency_key": "simple-loop-eval",
                },
                phase="eval_run",
            )
        if self.phase == "eval_run":
            result = self._object(raw)
            if result.get("candidate_commit") != self.child_commit:
                raise ValueError("Eval candidate differs from Task candidate")
            if result.get("measurement_state") != "valid" or result.get("metric") != 1.0:
                raise ValueError("Eval did not return the expected measurement")
            self.eval_id = self._string(result, "eval_id")
            self.eval_ref = f"eval/evaluations/{self.eval_id}/receipt.json"
            return self._tool("Attention", {}, phase="post_eval_attention")
        if self.phase == "post_eval_attention":
            packet = self._object(raw)
            returns = packet.get("unread_returns")
            refs = {
                item.get("ref")
                for item in returns or []
                if isinstance(item, dict)
            }
            if refs != {self.collected_ref, self.eval_ref}:
                raise ValueError("Attention lacks exact Task and Eval returns")
            return self._write("questions/Q-0001/question.md", self._question(), "final_question")
        writes = {
            "final_question": ("model/CURRENT.md", self._final_model(), "final_model"),
            "final_model": ("ideas/I-E2E.md", self._final_idea(), "final_idea"),
            "final_idea": ("knowledge/claims/C-0001.md", self._claim(), "final_claim"),
            "final_claim": ("memory/NOW.md", self._now(), "final_now"),
        }
        if self.phase in writes:
            path, content, phase = writes[self.phase]
            return self._write(path, content, phase)
        if self.phase == "final_now":
            return self._tool(
                "Checkpoint",
                {
                    "message": "Interpret deterministic Task return and measurement.",
                    "paths": [
                        "ideas/I-E2E.md",
                        "knowledge/claims/C-0001.md",
                        "memory/NOW.md",
                        "model/CURRENT.md",
                        "questions/Q-0001/question.md",
                    ],
                },
                phase="final_checkpoint",
            )
        if self.phase == "final_checkpoint":
            self._string(self._object(raw), "commit")
            return self._text("Deterministic research loop checkpointed.")
        raise ValueError(f"unexpected provider phase: {self.phase!r}")

    def _restart(self, raw: str) -> LLMResponse:
        if self.phase != "restart_attention":
            raise ValueError("restart provider phase is invalid")
        packet = self._object(raw)
        if packet.get("unread_returns") != []:
            raise ValueError("restart still has unread returns")
        recent = packet.get("recent_evidence_delta")
        refs = recent[0].get("observed_refs") if isinstance(recent, list) and recent else None
        if (
            not isinstance(refs, list)
            or len(refs) != 2
            or not any(str(item).startswith("tasks/TASK-") for item in refs)
            or not any(str(item).startswith("eval/evaluations/EVAL-") for item in refs)
        ):
            raise ValueError("restart lacks exact observed return kinds")
        return self._text("Recovered deterministic research state with no unread returns.")

    def _task(self, action: str, phase: str) -> LLMResponse:
        if self.task_id is None:
            raise ValueError("Task identity is unavailable")
        return self._tool("Task", {"action": action, "task_id": self.task_id}, phase=phase)

    def _write(self, path: str, content: str, phase: str) -> LLMResponse:
        return self._tool("Write", {"file_path": path, "content": content}, phase=phase)

    @staticmethod
    def _preregistered_model() -> str:
        return "# Current Model\n\nThe candidate should emit `success`.\n"

    @staticmethod
    def _preregistered_idea() -> str:
        return "---\nid: I-E2E\n---\n# Idea\n\nTest the exact candidate with evaluator v1.\n"

    def _question(self) -> str:
        return (
            "---\nid: Q-0001\nstatus: resolved\n---\n# Question\n\n"
            "Does the deterministic candidate produce the expected valid measurement?\n\n"
            "## Current best answer\n\nYes under the fixed commissioning apparatus.\n\n"
            "## Current uncertainty\n\nExternal validity is not tested.\n\n"
            "## Resolution criterion\n\nOne valid metric of 1.0.\n\n"
            "## Stop / pivot criterion\n\nStop after the declared evaluator succeeds.\n\n"
            "## Expected information gain\n\nThe commissioned question is resolved.\n"
        )

    def _final_model(self) -> str:
        return (
            "# Current Model\n\nThe candidate emitted `success` and evaluator "
            f"`{self.eval_id}` measured 1.0.\n\n## Current uncertainty\n\n"
            "The fixture does not establish external validity.\n"
        )

    def _final_idea(self) -> str:
        return (
            "---\nid: I-E2E\n---\n# Idea\n\n## Result\n\n"
            f"Task `{self.task_id}` produced candidate `{self.child_commit}`; "
            f"evaluation `{self.eval_id}` returned a valid metric of 1.0.\n"
        )

    def _claim(self) -> str:
        return (
            "---\nid: C-0001\n---\n# Claim\n\n## Statement and scope\n\n"
            "The deterministic candidate passes the fixed simple-loop evaluator.\n\n"
            f"## Evidence\n\n- `{self.eval_ref}` — valid metric 1.0 for `{self.child_commit}`.\n\n"
            "## Counterevidence\n\nNone within this fixture.\n"
        )

    def _now(self) -> str:
        return (
            "# Current State\n\n## Result\n\n"
            f"Task `{self.task_id}` return `{self.collected_ref}` and evaluation "
            f"`{self.eval_ref}` resolved Q-0001 within fixture scope.\n\n"
            "## Current uncertainty\n\nExternal validity remains unknown.\n"
        )


__all__ = ["SimpleLoopProvider"]
