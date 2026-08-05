"""Native transition audit and explicit index-rebuild CLI behavior."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import MappingProxyType

import pytest
from typer.testing import CliRunner

from arbor.aros.transition_index import TransitionIndexState
from arbor.aros.transitions import TransitionAuditService
from arbor.cli.commands import aros_cmd
from tests import test_aros_transitions as transition_support


runner = CliRunner()


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_transition_audit_cli_emits_exact_testimony_and_writes_nothing(
    tmp_path: Path,
) -> None:
    proposal_ref = transition_support._changed_semantic_proposal(
        tmp_path,
        "T-cli-audit",
    )
    expected = TransitionAuditService(
        tmp_path,
        canonical_ref="refs/heads/main",
    ).audit(proposal_ref)
    status_before = _git(
        tmp_path,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    )
    before = transition_support._snapshot_tree(tmp_path)

    result = runner.invoke(
        aros_cmd.aros_app,
        ["transition", "audit", proposal_ref, "--cwd", str(tmp_path)],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == expected
    assert transition_support._snapshot_tree(tmp_path) == before
    assert (
        _git(tmp_path, "status", "--porcelain=v1", "--untracked-files=all")
        == status_before
    )
    assert not (tmp_path / ".aros").exists()


def test_transition_audit_rejects_detached_canonical_head(tmp_path: Path) -> None:
    proposal_ref = transition_support._changed_semantic_proposal(
        tmp_path,
        "T-detached-audit",
    )
    _git(tmp_path, "checkout", "-q", "--detach")

    result = runner.invoke(
        aros_cmd.aros_app,
        ["transition", "audit", proposal_ref, "--cwd", str(tmp_path)],
    )

    assert result.exit_code == 2
    assert "attached canonical git branch" in result.output.lower()


def test_transition_audit_rejects_invalid_repository(tmp_path: Path) -> None:
    result = runner.invoke(
        aros_cmd.aros_app,
        [
            "transition",
            "audit",
            "transitions/T-invalid/proposal.json",
            "--cwd",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 2
    assert "error:" in result.output.lower()


def test_explicit_audit_rebuild_calls_full_rebuild_not_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = object()
    calls: list[tuple[object, object] | str] = []
    state = TransitionIndexState(
        state="complete",
        head="a" * 40,
        validated_through="a" * 40,
        assimilations=MappingProxyType({}),
        latest_evidence_transition=None,
    )

    class _Index:
        def __init__(self, candidate: object, canonical: object):
            calls.append((candidate, canonical))

        def read(self) -> TransitionIndexState:
            raise AssertionError("explicit rebuild must not use bounded cache read")

        def rebuild(self) -> TransitionIndexState:
            calls.append("rebuild")
            return state

    monkeypatch.setattr(aros_cmd, "bind_repository", lambda _root: binding)
    monkeypatch.setattr(
        aros_cmd,
        "read_repository_snapshot",
        lambda _repository: {"head": "a" * 40, "ref": "refs/heads/main"},
    )
    monkeypatch.setattr(aros_cmd, "TransitionIndex", _Index)

    result = runner.invoke(
        aros_cmd.aros_app,
        ["audit", "--rebuild-index", "--cwd", str(tmp_path)],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "state": "complete",
        "head": "a" * 40,
        "validated_through": "a" * 40,
        "assimilations": {},
        "latest_evidence_transition": None,
    }
    assert calls == [(binding, binding), "rebuild"]


def test_audit_rebuild_writes_only_disposable_transition_index(tmp_path: Path) -> None:
    head = transition_support._init_workspace(tmp_path)
    cache = tmp_path / ".aros/indexes/transition-index.json"
    status_before = _git(
        tmp_path,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    )

    result = runner.invoke(
        aros_cmd.aros_app,
        ["audit", "--rebuild-index", "--cwd", str(tmp_path)],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "state": "complete",
        "head": head,
        "validated_through": head,
        "assimilations": {},
        "latest_evidence_transition": None,
    }
    assert json.loads(cache.read_bytes()) == {
        "schema_version": 1,
        "head": head,
        "validated_through": head,
        "assimilations": {},
        "latest_evidence_transition": None,
    }
    assert (
        _git(tmp_path, "status", "--porcelain=v1", "--untracked-files=all")
        == status_before
    )


def test_audit_without_rebuild_flag_fails_before_repository_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(_root: Path) -> object:
        raise AssertionError("repository accessed without explicit rebuild flag")

    monkeypatch.setattr(aros_cmd, "bind_repository", forbidden)

    result = runner.invoke(aros_cmd.aros_app, ["audit", "--cwd", str(tmp_path)])

    assert result.exit_code == 2
    assert "--rebuild-index" in result.output


def test_audit_rebuild_rejects_detached_canonical_head(tmp_path: Path) -> None:
    transition_support._init_workspace(tmp_path)
    _git(tmp_path, "checkout", "-q", "--detach")

    result = runner.invoke(
        aros_cmd.aros_app,
        ["audit", "--rebuild-index", "--cwd", str(tmp_path)],
    )

    assert result.exit_code == 2
    assert "attached canonical git branch" in result.output.lower()
    assert not (tmp_path / ".aros/indexes/transition-index.json").exists()


@pytest.mark.parametrize(
    "argv",
    [
        ["transition", "audit", "transitions/T-unborn/proposal.json"],
        ["audit", "--rebuild-index"],
    ],
)
def test_transition_commands_reject_unborn_canonical_ref(
    tmp_path: Path,
    argv: list[str],
) -> None:
    _git(tmp_path, "init", "-q", "-b", "main")

    result = runner.invoke(
        aros_cmd.aros_app,
        [*argv, "--cwd", str(tmp_path)],
    )

    assert result.exit_code == 2
    assert "attached canonical git branch" in result.output.lower()


def test_boot_never_rebuilds_transition_index(tmp_path: Path) -> None:
    transition_support._init_workspace(tmp_path)
    cache = tmp_path / ".aros/indexes/transition-index.json"

    result = runner.invoke(
        aros_cmd.aros_app,
        ["boot", "--json", "--cwd", str(tmp_path)],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["schema_version"] == 1
    assert not cache.exists()
