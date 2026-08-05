"""Principal-facing Research system call behavior."""

from __future__ import annotations

import asyncio
import copy
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

import arbor.aros.research_tool as research_module
from arbor.aros.attention import AttentionAuthorityContext
from arbor.aros.checkpoint import CheckpointError
from arbor.aros.research_tool import ResearchTool
from arbor.aros.worktrees import RepositoryBinding, bind_repository
from tests import test_aros_transitions as transition_support


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _workspace(root: Path) -> tuple[RepositoryBinding, str]:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "research-tool@example.invalid")
    _git(root, "config", "user.name", "Research Tool Test")
    (root / ".gitignore").write_text("/.aros/\n/.worktree/\n", encoding="utf-8")
    (root / "AROS.md").write_text("# AROS\n", encoding="utf-8")
    (root / "memory").mkdir()
    (root / "memory" / "NOW.md").write_text("# Current State\n", encoding="utf-8")
    _git(root, "add", ".gitignore", "AROS.md", "memory/NOW.md")
    _git(root, "commit", "-qm", "initial research workspace")
    return bind_repository(root), "refs/heads/main"


def _context() -> AttentionAuthorityContext:
    return AttentionAuthorityContext(
        authority={"state": "available", "source": "test-host"},
        remaining_budget={"state": "available", "units": 7},
        institutional_obligations=({"kind": "report", "ref": "policy-1"},),
    )


def _schema_property_names(value: object) -> set[str]:
    names: set[str] = set()
    if isinstance(value, dict):
        properties = value.get("properties")
        if isinstance(properties, dict):
            names.update(str(name) for name in properties)
        for item in value.values():
            names.update(_schema_property_names(item))
    elif isinstance(value, list):
        for item in value:
            names.update(_schema_property_names(item))
    return names


def test_research_schema_has_only_three_exact_closed_actions() -> None:
    schema = ResearchTool.input_schema

    assert set(schema) == {"type", "oneOf"}
    assert schema["type"] == "object"
    branches = schema["oneOf"]
    assert isinstance(branches, list) and len(branches) == 3
    by_action = {
        branch["properties"]["action"]["const"]: branch for branch in branches
    }
    assert set(by_action) == {"attention", "transition_audit", "checkpoint"}
    assert set(by_action["attention"]["properties"]) == {"action", "max_chars"}
    assert by_action["attention"]["required"] == ["action"]
    assert set(by_action["transition_audit"]["properties"]) == {
        "action",
        "proposal_ref",
    }
    assert by_action["transition_audit"]["required"] == [
        "action",
        "proposal_ref",
    ]
    assert set(by_action["checkpoint"]["properties"]) == {
        "action",
        "proposal_ref",
        "message",
    }
    assert by_action["checkpoint"]["required"] == [
        "action",
        "proposal_ref",
        "message",
    ]
    assert all(branch["additionalProperties"] is False for branch in branches)


def test_research_schema_recursively_excludes_host_authority_and_human_selectors() -> None:
    property_names = _schema_property_names(ResearchTool.input_schema)
    forbidden = {
        "actor",
        "contract",
        "revision",
        "lease",
        "session",
        "prompt",
        "attempt",
        "capability",
        "budget",
        "evaluator_policy",
        "canonical_ref",
        "canonical_root",
        "receipt",
        "fence",
        "barrier",
        "gateway",
        "enforcement_class",
        "human_direct",
        "cooperative_human_direct",
    }

    assert property_names.isdisjoint(forbidden)


def test_research_builds_each_host_service_once_and_dispatches_exact_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = RepositoryBinding(tmp_path, tmp_path / ".git", tmp_path / ".git")
    context = _context()
    gateway = object()
    calls: dict[str, list[Any]] = {
        "attention_init": [],
        "attention_build": [],
        "audit_init": [],
        "audit": [],
        "checkpoint_init": [],
        "checkpoint": [],
    }
    packet = {"packet": ["exact", 1]}
    testimony = {"audit": {"valid": True}}
    checkpoint = {"checkpoint": "commit-1"}

    class _Attention:
        def __init__(self, root: str, *, canonical_repository: RepositoryBinding):
            calls["attention_init"].append((root, canonical_repository))

        def build(
            self,
            max_chars: int,
            context: AttentionAuthorityContext | None,
        ) -> dict[str, object]:
            calls["attention_build"].append((max_chars, context))
            return packet

        @staticmethod
        def render_text(value: dict[str, object]) -> str:
            return json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )

    class _Audit:
        def __init__(self, root: str, *, canonical_ref: str):
            calls["audit_init"].append((root, canonical_ref))

        def audit(self, proposal_ref: str) -> dict[str, object]:
            calls["audit"].append(proposal_ref)
            return testimony

    class _Checkpoint:
        def __init__(
            self,
            root: str,
            *,
            canonical_repository: RepositoryBinding,
            canonical_ref: str,
            audit_service: _Audit,
            gateway: object | None,
        ):
            calls["checkpoint_init"].append(
                (root, canonical_repository, canonical_ref, audit_service, gateway)
            )

        def checkpoint(self, proposal_ref: str, message: str) -> dict[str, object]:
            calls["checkpoint"].append((proposal_ref, message))
            return checkpoint

    monkeypatch.setattr(research_module, "ResearchAttentionService", _Attention)
    monkeypatch.setattr(research_module, "TransitionAuditService", _Audit)
    monkeypatch.setattr(research_module, "CheckpointService", _Checkpoint)

    tool = ResearchTool(
        cwd=str(tmp_path),
        canonical_repository=binding,
        canonical_ref="refs/heads/main",
        admission_gateway=gateway,
        attention_context=context,
        persist_results=False,
    )
    attention_service = tool.attention_service
    audit_service = tool.audit_service

    attention_json = asyncio.run(tool.execute(action="attention", max_chars=1_024))
    audit_json = asyncio.run(
        tool.execute(
            action="transition_audit",
            proposal_ref="transitions/T-one/proposal.json",
        )
    )
    checkpoint_json = asyncio.run(
        tool.execute(
            action="checkpoint",
            proposal_ref="transitions/T-one/proposal.json",
            message="Checkpoint one.",
        )
    )

    assert json.loads(attention_json) == packet
    assert json.loads(audit_json) == testimony
    assert json.loads(checkpoint_json) == checkpoint
    assert calls["attention_init"] == [(str(tmp_path), binding)]
    assert calls["attention_build"] == [(1_024, context)]
    assert calls["audit_init"] == [(str(tmp_path), "refs/heads/main")]
    assert calls["audit"] == ["transitions/T-one/proposal.json"]
    assert calls["checkpoint"] == [
        ("transitions/T-one/proposal.json", "Checkpoint one.")
    ]
    assert calls["checkpoint_init"] == [
        (
            str(tmp_path),
            binding,
            "refs/heads/main",
            audit_service,
            gateway,
        )
    ]
    assert tool.attention_service is attention_service
    assert tool.audit_service is audit_service


def test_research_rejects_unknown_or_host_only_model_arguments(tmp_path: Path) -> None:
    binding, canonical_ref = _workspace(tmp_path)
    tool = ResearchTool(
        cwd=str(tmp_path),
        canonical_repository=binding,
        canonical_ref=canonical_ref,
        admission_gateway=None,
    )

    with pytest.raises(ValueError, match="unknown Research action"):
        asyncio.run(tool.execute(action="inspect"))
    with pytest.raises(ValueError, match="unexpected Research arguments"):
        asyncio.run(
            tool.execute(
                action="checkpoint",
                proposal_ref="transitions/T-one/proposal.json",
                message="No human selector.",
                cooperative_human_direct=True,
            )
        )
    with pytest.raises(ValueError, match="unexpected Research arguments"):
        asyncio.run(
            tool.execute(
                action="attention",
                canonical_ref="refs/heads/replaced-by-model",
            )
        )


def test_research_checkpoint_without_gateway_fails_closed(tmp_path: Path) -> None:
    binding, canonical_ref = _workspace(tmp_path)
    tool = ResearchTool(
        cwd=str(tmp_path),
        canonical_repository=binding,
        canonical_ref=canonical_ref,
        admission_gateway=None,
    )
    delegated = False

    def forbidden_delegate(_proposal_ref: str, _message: str) -> dict[str, object]:
        nonlocal delegated
        delegated = True
        return {"unexpected": "recovery without authority"}

    tool.checkpoint_service.checkpoint = forbidden_delegate  # type: ignore[method-assign]

    with pytest.raises(
        CheckpointError,
        match="checkpoint requires an injected admission gateway",
    ):
        asyncio.run(
            tool.execute(
                action="checkpoint",
                proposal_ref="transitions/T-no-gateway/proposal.json",
                message="Must not fall back.",
            )
        )
    assert delegated is False


def test_research_transition_audit_is_deterministic_and_writes_nothing(
    tmp_path: Path,
) -> None:
    proposal_ref = transition_support._changed_semantic_proposal(
        tmp_path,
        "T-research-read-only",
    )
    binding = bind_repository(tmp_path)
    tool = ResearchTool(
        cwd=str(tmp_path),
        canonical_repository=binding,
        canonical_ref="refs/heads/main",
        admission_gateway=None,
    )
    status_before = transition_support._git(
        tmp_path,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    )
    before = transition_support._snapshot_tree(tmp_path)

    first = json.loads(
        asyncio.run(
            tool.execute(action="transition_audit", proposal_ref=proposal_ref)
        )
    )
    second = json.loads(
        asyncio.run(
            tool.execute(action="transition_audit", proposal_ref=proposal_ref)
        )
    )

    assert first == second
    assert transition_support._snapshot_tree(tmp_path) == before
    assert (
        transition_support._git(
            tmp_path,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        )
        == status_before
    )
    assert not (tmp_path / ".aros").exists()


def test_research_schema_exports_are_defensive_and_host_context_independent(
    tmp_path: Path,
) -> None:
    binding, canonical_ref = _workspace(tmp_path)
    tool = ResearchTool(
        cwd=str(tmp_path),
        canonical_repository=binding,
        canonical_ref=canonical_ref,
        admission_gateway=object(),
        attention_context=_context(),
    )
    expected = copy.deepcopy(ResearchTool.input_schema)

    exported = tool.to_api_schema()
    exported["input_schema"]["oneOf"][0]["properties"]["action"]["const"] = (
        "replaced"
    )

    assert tool.input_schema == expected
    assert ResearchTool.input_schema == expected


def test_research_attention_wire_is_the_once_built_bounded_packet(
    tmp_path: Path,
) -> None:
    binding, canonical_ref = _workspace(tmp_path)
    tool = ResearchTool(
        cwd=str(tmp_path),
        canonical_repository=binding,
        canonical_ref=canonical_ref,
        admission_gateway=None,
    )
    built: list[dict[str, object]] = []
    real_build = tool.attention_service.build

    def recording_build(
        max_chars: int,
        context: AttentionAuthorityContext | None,
    ) -> dict[str, object]:
        packet = real_build(max_chars=max_chars, context=context)
        built.append(packet)
        return packet

    tool.attention_service.build = recording_build  # type: ignore[method-assign]

    wire = asyncio.run(tool.execute(action="attention", max_chars=512))

    assert len(built) == 1
    assert json.loads(wire) == built[0]
    assert len(wire) <= 512
    assert wire == tool.attention_service.render_text(built[0])
