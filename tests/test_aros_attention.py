"""Bounded restart Attention derived from workspace, services, and Git."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

from arbor.aros.attention import (
    AttentionAuthorityContext,
    ResearchAttentionService,
)
from arbor.aros.checkpoint import GitCheckpoint
from tests import test_aros_observations as observation_support


TOP_LEVEL_KEYS = {
    "schema_version",
    "snapshot",
    "active_question",
    "current_uncertainty",
    "recent_evidence_delta",
    "hypotheses",
    "pending_measurements",
    "unread_returns",
    "current_obligations",
    "remaining_budget",
    "blocked_reasons",
    "authority",
    "warnings",
    "omitted",
}


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _write_semantic_workspace(root: Path) -> None:
    (root / ".gitignore").write_text("/.aros/\n/.worktree/\n", encoding="utf-8")
    (root / "AROS.md").write_text("# Mission\n\nFind the mechanism.\n", encoding="utf-8")
    (root / "questions/Q-0001").mkdir(parents=True, exist_ok=True)
    (root / "questions/FRONTIER.md").write_text(
        "---\nfocus_question: Q-0001\n---\n# Frontier\n",
        encoding="utf-8",
    )
    (root / "questions/Q-0001/question.md").write_text(
        "---\nid: Q-0001\nstatus: open\n---\n"
        "# Question\n\nDoes the mediator determine the result?\n\n"
        "## Current uncertainty\n\nThe controlled contrast is missing.\n\n"
        "## Resolution criterion\n\nA preregistered contrast resolves it.\n\n"
        "## Stop / pivot criterion\n\nPivot after a precise null.\n\n"
        "## Expected information gain\n\nHigh for the controlled contrast.\n",
        encoding="utf-8",
    )
    (root / "memory").mkdir(exist_ok=True)
    (root / "memory/NOW.md").write_text(
        "# NOW\n\n## Current uncertainty\n\nOne contrast remains.\n\n"
        "## Current obligations\n\nRun the declared evaluator.\n",
        encoding="utf-8",
    )
    (root / "model").mkdir(exist_ok=True)
    (root / "model/CURRENT.md").write_text(
        "# Current Model\n\n## Current uncertainty\n\nThe mediator may be causal.\n",
        encoding="utf-8",
    )


def _workspace(root: Path) -> str:
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "Attention Test")
    _git(root, "config", "user.email", "attention@example.invalid")
    _write_semantic_workspace(root)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "Initialize research workspace")
    return _git(root, "rev-parse", "HEAD")


def _workspace_with_run_return(root: Path) -> tuple[str, str]:
    _service, manifest, _final = observation_support._install_run_final(root)
    _write_semantic_workspace(root)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "Initialize research workspace and run return")
    return (
        _git(root, "rev-parse", "HEAD"),
        f"runs/{manifest['run_id']}/final.json",
    )


def _refs(packet: dict[str, object]) -> list[str]:
    returns = packet["unread_returns"]
    assert isinstance(returns, list)
    return [str(item["ref"]) for item in returns]


def test_attention_has_small_stable_shape_and_canonical_research_views(
    tmp_path: Path,
) -> None:
    head = _workspace(tmp_path)

    packet = ResearchAttentionService(tmp_path).build()

    assert set(packet) == TOP_LEVEL_KEYS
    assert packet["schema_version"] == 1
    assert packet["snapshot"]["canonical"] == head
    assert packet["active_question"]["id"] == "Q-0001"
    assert packet["hypotheses"]["leading"][0]["path"] == "model/CURRENT.md"
    assert packet["unread_returns"] == []
    assert packet["recent_evidence_delta"] == []


def test_terminal_return_without_observed_trailer_is_unread(tmp_path: Path) -> None:
    _head, ref = _workspace_with_run_return(tmp_path)

    packet = ResearchAttentionService(tmp_path).build()

    assert _refs(packet) == [ref]


def test_checkpoint_trailer_marks_return_read_and_exposes_recent_delta(
    tmp_path: Path,
) -> None:
    parent, ref = _workspace_with_run_return(tmp_path)
    (tmp_path / "memory/NOW.md").write_text(
        "# NOW\n\n## Current uncertainty\n\nThe returned run narrows it.\n",
        encoding="utf-8",
    )

    result = GitCheckpoint(tmp_path).commit(
        paths=["memory/NOW.md"],
        message="Interpret the returned run",
        observed_refs=[ref],
    )
    packet = ResearchAttentionService(tmp_path).build()

    assert result["parent"] == parent
    assert packet["unread_returns"] == []
    assert packet["recent_evidence_delta"] == [
        {
            "commit": result["commit"],
            "observed_refs": [ref],
            "paths": ["memory/NOW.md"],
        }
    ]


def test_task_and_owned_run_observations_do_not_leave_duplicate_unread_final(
    tmp_path: Path,
) -> None:
    _service, task_id, collected = observation_support._collected_task(tmp_path)
    task_ref = f"tasks/{task_id}/collected.json"
    run_final_ref = str(collected["run_final_ref"])
    (tmp_path / "memory/NOW.md").write_text(
        "# NOW\n\nThe Task return and owned Run final were reviewed together.\n",
        encoding="utf-8",
    )
    GitCheckpoint(tmp_path).commit(
        paths=["memory/NOW.md"],
        message="Interpret the Task return",
        observed_refs=[run_final_ref, task_ref],
    )

    packet = ResearchAttentionService(tmp_path).build()

    assert packet["unread_returns"] == []


def test_session_exit_before_checkpoint_keeps_return_unread(tmp_path: Path) -> None:
    _head, ref = _workspace_with_run_return(tmp_path)
    (tmp_path / "memory/NOW.md").write_text("# NOW\n\nUncommitted interpretation.\n")

    packet = ResearchAttentionService(tmp_path).build()

    assert _refs(packet) == [ref]
    assert packet["recent_evidence_delta"] == []


def test_invalid_observed_trailer_is_ignored_with_warning(tmp_path: Path) -> None:
    _head, ref = _workspace_with_run_return(tmp_path)
    (tmp_path / "memory/NOW.md").write_text("# NOW\n\nInvalid trailer test.\n")
    _git(tmp_path, "add", "memory/NOW.md")
    _git(
        tmp_path,
        "commit",
        "-qm",
        "Malformed observation\n\nAROS-Observed: arbitrary/file.json",
    )

    packet = ResearchAttentionService(tmp_path).build()

    assert _refs(packet) == [ref]
    assert any(str(item).startswith("invalid_observed_trailer:") for item in packet["warnings"])


def test_attention_is_deterministic_read_only_and_bounded(tmp_path: Path) -> None:
    _workspace(tmp_path)
    (tmp_path / "uncommitted.txt").write_text("preserve me\n", encoding="utf-8")
    before = _git(tmp_path, "status", "--porcelain=v1", "--untracked-files=all")
    service = ResearchAttentionService(tmp_path)

    first = service.build(max_chars=512)
    second = service.build(max_chars=512)

    assert first == second
    assert len(service.render_text(first)) <= 512
    assert _git(tmp_path, "status", "--porcelain=v1", "--untracked-files=all") == before


def test_attention_preserves_host_authority_budget_and_obligations(
    tmp_path: Path,
) -> None:
    _workspace(tmp_path)
    context = AttentionAuthorityContext(
        authority={"state": "available", "enforcement_class": "cooperative"},
        remaining_budget={"state": "available", "turns": 20},
        institutional_obligations=({"kind": "human_review"},),
    )

    packet = ResearchAttentionService(tmp_path).build(context=context)

    assert packet["authority"] == {
        "state": "available",
        "enforcement_class": "cooperative",
    }
    assert packet["remaining_budget"] == {"state": "available", "turns": 20}
    assert packet["current_obligations"]["institutional"] == [
        {"kind": "human_review"}
    ]


def test_attention_tool_has_no_action_and_renders_the_packet(tmp_path: Path) -> None:
    from arbor.aros.attention_tool import AttentionTool

    _workspace(tmp_path)
    tool = AttentionTool(cwd=str(tmp_path), persist_results=False)

    assert tool.input_schema == {
        "type": "object",
        "properties": {
            "max_chars": {
                "type": "integer",
                "minimum": 512,
                "maximum": 16_000,
            }
        },
        "additionalProperties": False,
    }
    packet = json.loads(asyncio.run(tool.execute()))
    assert set(packet) == TOP_LEVEL_KEYS
