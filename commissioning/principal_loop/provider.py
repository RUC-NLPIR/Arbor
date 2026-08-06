"""Deterministic provider used only by the live Principal commissioning."""

from __future__ import annotations

import json
from typing import Any

from arbor.core.llm.base import LLMResponse, TextBlock, ToolUseBlock, Usage


class PrincipalLoopProvider:
    """Drive the real Agent loop without accessing any reality interface."""

    model = "aros-principal-loop-fixture"
    base_url = None

    def __init__(self, *, restart: bool = False) -> None:
        self.restart = restart
        self.step = 0
        self.phase: str | None = None
        self._expected_tool_id: str | None = None
        self.task_id: str | None = None
        self.child_commit: str | None = None
        self.return_commit: str | None = None
        self.eval_id: str | None = None
        self.collected_ref: str | None = None
        self.eval_ref: str | None = None
        self.base_commit: str | None = None

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
        if self._expected_tool_id is None:
            if self.phase is not None:
                raise ValueError("provider is already complete")
            return self._tool(
                "Research",
                {"action": "attention"},
                phase="restart_attention" if self.restart else "initial_attention",
            )
        result = self._last_tool_result(messages)
        return self._restart_response(result) if self.restart else self._primary_response(result)

    def _tool(
        self,
        name: str,
        tool_input: dict[str, Any],
        *,
        phase: str,
    ) -> LLMResponse:
        tool_id = f"commission-{self.step:02d}"
        self.step += 1
        self.phase = phase
        self._expected_tool_id = tool_id
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

    def _text(self, value: str) -> LLMResponse:
        self.phase = "done"
        self._expected_tool_id = None
        raw = {"type": "text", "text": value}
        return LLMResponse(
            content=[TextBlock(text=value)],
            stop_reason="end_turn",
            usage=Usage(),
            raw_content=[raw],
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
        if result.get("tool_use_id") != self._expected_tool_id:
            raise ValueError("tool result ID mismatch")
        if result.get("is_error") is True:
            raise ValueError("tool result is_error")
        value = result.get("content")
        if not isinstance(value, str):
            raise ValueError("tool result content must be text")
        return value

    @staticmethod
    def _object(value: str) -> dict[str, Any]:
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as error:
            raise ValueError("tool result is not JSON") from error
        if not isinstance(decoded, dict):
            raise ValueError("tool result must be a JSON object")
        return decoded

    @staticmethod
    def _required_string(value: dict[str, Any], field: str) -> str:
        item = value.get(field)
        if not isinstance(item, str) or not item:
            raise ValueError(f"tool result lacks {field}")
        return item

    def _primary_response(self, raw: str) -> LLMResponse:
        phase = self.phase
        if phase == "initial_attention":
            packet = self._object(raw)
            if packet.get("unassimilated_returns") != []:
                raise ValueError("initial attention has pending returns")
            return self._tool(
                "Task",
                {
                    "action": "create",
                    "objective": (
                        "Produce one deterministic success candidate and strict return."
                    ),
                    "mode": "write",
                    "adapter_argv": [
                        "python3",
                        "commissioning/principal_loop/task_adapter.py",
                    ],
                    "capabilities": {"network": False, "shell": True},
                    "deliverables": ["candidate-mode.txt"],
                    "acceptance": ["candidate-mode.txt equals success"],
                    "timeout_seconds": 120,
                    "idempotency_key": "principal-loop-task",
                },
                phase="task_create",
            )
        if phase == "task_create":
            result = self._object(raw)
            if result.get("admission_required") is not False:
                raise ValueError("Task brief was not operationally admitted")
            self.task_id = self._required_string(result, "task_id")
            return self._task("start", phase="task_start")
        if phase == "task_start":
            result = self._object(raw)
            if result.get("task_id") != self.task_id:
                raise ValueError("Task start identity mismatch")
            return self._task("status", phase="task_status")
        if phase == "task_status":
            result = self._object(raw)
            if result.get("task_id") != self.task_id:
                raise ValueError("Task status identity mismatch")
            state = result.get("state")
            if state in {"created", "starting", "running"}:
                return self._task("status", phase="task_status")
            if state != "completed":
                raise ValueError(f"Task reached non-completed state: {state!r}")
            return self._task("collect", phase="task_collect")
        if phase == "task_collect":
            result = self._object(raw)
            if result.get("task_id") != self.task_id:
                raise ValueError("Task collection identity mismatch")
            if result.get("admission_required") is not False:
                raise ValueError("Task collection was not operationally admitted")
            self.child_commit = self._required_string(result, "child_commit")
            self.return_commit = self._required_string(result, "return_commit")
            self.collected_ref = f"tasks/{self.task_id}/collected.json"
            return self._tool(
                "Eval",
                {
                    "action": "run",
                    "evaluator_id": "principal-loop",
                    "version": "1",
                    "candidate_commit": self.child_commit,
                    "idempotency_key": "principal-loop-eval",
                },
                phase="eval_run",
            )
        if phase == "eval_run":
            result = self._object(raw)
            if result.get("admission_required") is not False:
                raise ValueError("Eval receipt was not operationally admitted")
            if result.get("candidate_commit") != self.child_commit:
                raise ValueError("Eval candidate_commit differs from Task child_commit")
            if result.get("measurement_state") != "valid" or result.get("metric") != 1.0:
                raise ValueError("Eval did not return the exact valid measurement")
            self.eval_id = self._required_string(result, "eval_id")
            self.eval_ref = f"eval/evaluations/{self.eval_id}/receipt.json"
            return self._tool(
                "Research",
                {"action": "attention"},
                phase="post_eval_attention",
            )
        if phase == "post_eval_attention":
            packet = self._object(raw)
            snapshot = packet.get("snapshot")
            candidate = snapshot.get("candidate") if isinstance(snapshot, dict) else None
            if not isinstance(candidate, dict):
                raise ValueError("attention lacks candidate snapshot")
            self.base_commit = self._required_string(candidate, "head")
            returns = packet.get("unassimilated_returns")
            if not isinstance(returns, list):
                raise ValueError("attention returns are invalid")
            refs = {
                item.get("ref")
                for item in returns
                if isinstance(item, dict) and isinstance(item.get("ref"), str)
            }
            if refs != {self.collected_ref, self.eval_ref}:
                raise ValueError("attention lacks exact pending Task and Eval refs")
            return self._tool(
                "Read",
                {"file_path": "knowledge/claims/C-0001.md"},
                phase="read_claim",
            )
        if phase == "read_claim":
            return self._tool(
                "Read",
                {"file_path": "memory/NOW.md"},
                phase="read_now",
            )
        if phase == "read_now":
            return self._tool(
                "Write",
                {
                    "file_path": "knowledge/claims/C-0001.md",
                    "content": self._claim(),
                },
                phase="write_claim",
            )
        if phase == "write_claim":
            return self._tool(
                "Write",
                {"file_path": "memory/NOW.md", "content": self._now()},
                phase="write_now",
            )
        if phase == "write_now":
            return self._tool(
                "Write",
                {
                    "file_path": "transitions/T-E2E-ASSIMILATE/proposal.json",
                    "content": self._proposal(),
                },
                phase="write_proposal",
            )
        if phase == "write_proposal":
            return self._tool(
                "Research",
                {
                    "action": "transition_audit",
                    "proposal_ref": "transitions/T-E2E-ASSIMILATE/proposal.json",
                },
                phase="transition_audit",
            )
        if phase == "transition_audit":
            result = self._object(raw)
            if result.get("mechanically_valid") is not True:
                raise ValueError("transition audit is not mechanically valid")
            return self._tool(
                "Research",
                {
                    "action": "checkpoint",
                    "proposal_ref": "transitions/T-E2E-ASSIMILATE/proposal.json",
                    "message": (
                        "Assimilate deterministic Task return and valid measurement."
                    ),
                },
                phase="checkpoint",
            )
        if phase == "checkpoint":
            result = self._object(raw)
            self._required_string(result, "commit")
            return self._text("Cooperative research transition admitted.")
        raise ValueError(f"unexpected primary provider phase: {phase!r}")

    def _restart_response(self, raw: str) -> LLMResponse:
        if self.phase != "restart_attention":
            raise ValueError(f"unexpected restart provider phase: {self.phase!r}")
        packet = self._object(raw)
        if packet.get("unassimilated_returns") != []:
            raise ValueError("restart attention still has pending returns")
        recent = packet.get("recent_evidence_delta")
        if (
            not isinstance(recent, list)
            or not recent
            or not isinstance(recent[0], dict)
            or recent[0].get("transition_id") != "T-E2E-ASSIMILATE"
        ):
            raise ValueError("restart attention lacks admitted evidence delta")
        return self._text("Recovered admitted transition T-E2E-ASSIMILATE.")

    def _task(self, action: str, *, phase: str) -> LLMResponse:
        if self.task_id is None:
            raise ValueError("Task identity is unavailable")
        return self._tool(
            "Task",
            {"action": action, "task_id": self.task_id},
            phase=phase,
        )

    def _claim(self) -> str:
        if self.eval_ref is None or self.child_commit is None:
            raise ValueError("Claim evidence is unavailable")
        link = json.dumps(
            {
                "observation_ref": self.eval_ref,
                "relation": "supports",
                "scope": (
                    f"candidate {self.child_commit}; fixed seed 7; "
                    "visible principal-loop evaluator v1"
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return (
            "---\nid: C-0001\n---\n# Claim C-0001\n\n"
            "## Statement\n\n"
            "The deterministic candidate produced the expected success value and "
            "received a valid metric of 1.0.\n\n"
            f"## Evidence links\n\n{link}\n\n"
            "## Counterevidence\n"
        )

    def _now(self) -> str:
        if None in {
            self.task_id,
            self.child_commit,
            self.return_commit,
            self.eval_id,
        }:
            raise ValueError("NOW evidence is unavailable")
        return (
            "# Current State\n\n"
            "## Assimilated task return\n\n"
            f"Task `{self.task_id}` returned candidate commit "
            f"`{self.child_commit}` and return commit `{self.return_commit}`.\n\n"
            "## Measurement\n\n"
            f"Evaluation `{self.eval_id}` measured `principal_loop_quality=1.0` "
            "with state `valid` for the same candidate commit.\n"
        )

    def _proposal(self) -> str:
        if self.base_commit is None or self.eval_ref is None or self.collected_ref is None:
            raise ValueError("proposal evidence is unavailable")
        return json.dumps(
            {
                "schema_version": 1,
                "base_commit": self.base_commit,
                "workspace_paths": [
                    "knowledge/claims/C-0001.md",
                    "memory/NOW.md",
                ],
                "assimilations": [
                    {
                        "observation_ref": self.eval_ref,
                        "affected_paths": [
                            "knowledge/claims/C-0001.md",
                            "memory/NOW.md",
                        ],
                        "rationale": "knowledge/claims/C-0001.md#Evidence links",
                    },
                    {
                        "observation_ref": self.collected_ref,
                        "affected_paths": ["memory/NOW.md"],
                        "rationale": "memory/NOW.md#Assimilated task return",
                    },
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ) + "\n"
