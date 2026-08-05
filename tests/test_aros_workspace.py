"""Behavior tests for the minimal, transcript-independent AROS workspace."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from arbor.aros.workspace import boot_workspace, init_workspace, status_workspace


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _init_git(repo: Path) -> None:
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "aros-test@example.com")
    _git(repo, "config", "user.name", "AROS Test")


def test_init_workspace_creates_only_the_minimal_real_workspace(tmp_path: Path) -> None:
    _init_git(tmp_path)
    result = init_workspace(tmp_path, "Determine whether intervention X improves outcome Y.")

    assert result["root"] == str(tmp_path.resolve())
    assert (tmp_path / "AROS.md").read_text(encoding="utf-8") == (
        "# AROS Project\n\n"
        "## Mission\n\n"
        "Determine whether intervention X improves outcome Y.\n"
    )
    now = (tmp_path / "memory" / "NOW.md").read_text(encoding="utf-8")
    assert now.startswith("# Current State\n")
    assert "thesis" not in now.lower()
    assert "result" not in now.lower()

    agents = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert "scientific principal" in agents
    assert "AROS.md" in agents
    assert "memory/NOW.md" in agents
    assert ".worktree/" in agents

    assert (tmp_path / ".aros").is_dir()
    assert (tmp_path / ".worktree").is_dir()
    assert (tmp_path / ".gitignore").read_text(encoding="utf-8") == (
        "/.aros/\n/.worktree/\n"
    )

    assert not (tmp_path / "memory" / "BOOT.md").exists()
    assert (tmp_path / "memory" / "decisions").is_dir()
    assert (tmp_path / "knowledge" / "claims").is_dir()
    assert (tmp_path / "ideas").is_dir()
    assert (tmp_path / "transitions").is_dir()

    frontier = (tmp_path / "questions" / "FRONTIER.md").read_text(encoding="utf-8")
    assert "focus_question:" in frontier
    assert "Q-0001" not in frontier
    current_model = (tmp_path / "model" / "CURRENT.md").read_text(encoding="utf-8")
    assert current_model.startswith("# Current Model\n")
    assert "claim" not in current_model.lower()

    assert list((tmp_path / "questions").glob("Q-*")) == []
    assert list((tmp_path / "knowledge" / "claims").glob("C-*.md")) == []
    assert list((tmp_path / "ideas").glob("I-*.md")) == []
    assert not (tmp_path / "runs").exists()
    assert not (tmp_path / ".arbor").exists()


def test_init_workspace_requires_a_real_mission_before_writing(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="mission"):
        init_workspace(tmp_path, "  \n  ")

    assert list(tmp_path.iterdir()) == []


def test_init_workspace_requires_the_git_repository_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Git repository root"):
        init_workspace(tmp_path, "Mission")

    assert list(tmp_path.iterdir()) == []

    _init_git(tmp_path)
    nested = tmp_path / "nested"
    nested.mkdir()
    with pytest.raises(ValueError, match="Git repository root"):
        init_workspace(nested, "Mission")

    assert list(nested.iterdir()) == []


def test_init_workspace_is_idempotent_and_never_overwrites_project_files(tmp_path: Path) -> None:
    _init_git(tmp_path)
    for relative in ("memory", "questions", "model"):
        (tmp_path / relative).mkdir()
    originals = {
        "AROS.md": "# Existing mission\n",
        "memory/NOW.md": "# Human state\nDo not replace me.\n",
        "AGENTS.md": "# Existing project instructions\n",
        "questions/FRONTIER.md": "# Human frontier\nDo not replace me.\n",
        "model/CURRENT.md": "# Human model\nDo not replace me.\n",
    }
    for relative, content in originals.items():
        (tmp_path / relative).write_text(content, encoding="utf-8")
    (tmp_path / ".gitignore").write_text("build/", encoding="utf-8")

    first = init_workspace(tmp_path, "A replacement mission that must not win.")
    second = init_workspace(tmp_path, "Another replacement mission.")

    for relative, content in originals.items():
        assert (tmp_path / relative).read_text(encoding="utf-8") == content
    assert (tmp_path / ".gitignore").read_text(encoding="utf-8") == (
        "build/\n/.aros/\n/.worktree/\n"
    )
    assert first["preserved"] == [
        "AGENTS.md",
        "AROS.md",
        "memory/NOW.md",
        "questions/FRONTIER.md",
        "model/CURRENT.md",
    ]
    assert second["created"] == []
    assert second["updated"] == []


@pytest.mark.parametrize("component", ["memory", "questions", "model", "knowledge"])
def test_init_workspace_rejects_symlinked_scaffold_component(
    tmp_path: Path,
    component: str,
) -> None:
    _init_git(tmp_path)
    target = tmp_path.parent / f"{tmp_path.name}-{component}-target"
    target.mkdir()
    (tmp_path / component).symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        init_workspace(tmp_path, "Mission")

    assert list(target.iterdir()) == []


def test_init_workspace_rejects_symlinked_navigation_file(tmp_path: Path) -> None:
    _init_git(tmp_path)
    (tmp_path / "questions").mkdir()
    target = tmp_path.parent / f"{tmp_path.name}-frontier.md"
    target.write_text("# Existing external frontier\n", encoding="utf-8")
    (tmp_path / "questions" / "FRONTIER.md").symlink_to(target)

    with pytest.raises(ValueError, match="symlink"):
        init_workspace(tmp_path, "Mission")

    assert target.read_text(encoding="utf-8") == "# Existing external frontier\n"


def test_init_workspace_does_not_duplicate_existing_ignore_entries(tmp_path: Path) -> None:
    _init_git(tmp_path)
    (tmp_path / ".gitignore").write_text("/.aros/\n", encoding="utf-8")

    init_workspace(tmp_path, "Mission")
    init_workspace(tmp_path, "Mission")

    assert (tmp_path / ".gitignore").read_text(encoding="utf-8") == (
        "/.aros/\n/.worktree/\n"
    )


def test_status_workspace_reports_explicit_views_without_reading_their_meaning(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)
    init_workspace(tmp_path, "Mission")
    (tmp_path / "questions" / "FRONTIER.md").write_text("live question", encoding="utf-8")

    status = status_workspace(tmp_path)

    assert status["root"] == str(tmp_path.resolve())
    assert status["initialized"] is True
    assert status["git"]["is_repository"] is True
    assert status["git"]["branch"] in {"main", "master"}
    assert status["git"]["head"] is None
    assert status["git"]["dirty"] is True
    assert status["git"]["changes_truncated"] is False
    assert status["git"]["worktrees_truncated"] is False
    assert status["views"] == {
        "mission": {"path": "AROS.md", "exists": True},
        "now": {"path": "memory/NOW.md", "exists": True},
        "frontier": {"path": "questions/FRONTIER.md", "exists": True},
    }
    assert status["runs"] == {
        "total": 0,
        "counts": {},
        "items": [],
        "truncated": False,
        "operational_error": None,
    }


def test_status_workspace_discovers_bounded_operational_run_facts(
    tmp_path: Path, monkeypatch,
) -> None:
    from arbor.aros import runs as runs_module

    _init_git(tmp_path)
    init_workspace(tmp_path, "Mission")

    run_statuses = [
        {
            "run_id": f"RUN-completed-{index:02d}",
            "state": "completed",
            "updated_at": f"2026-08-01T00:{index:02d}:00Z",
            "exit_code": 0,
            "final_ref": f"runs/RUN-completed-{index:02d}/final.json",
            "unbounded_detail": "X" * 10_000,
        }
        for index in range(24)
    ]
    run_statuses.extend(
        [
            {
                "run_id": "RUN-lost-important",
                "state": "lost",
                "updated_at": "2026-08-01T01:00:00Z",
                "reason": "process_absent_without_final_receipt",
            },
            {
                "run_id": "RUN-running-important",
                "state": "running",
                "updated_at": "2026-08-01T02:00:00Z",
                "process_pid": 1234,
            },
        ]
    )

    class FakeRunService:
        def __init__(self, root: Path) -> None:
            assert root == tmp_path.resolve()

        def list(self) -> list[dict[str, object]]:
            return run_statuses

    monkeypatch.setattr(runs_module, "RunService", FakeRunService)

    status = status_workspace(tmp_path)
    summary = status["runs"]
    assert summary["total"] == 26
    assert summary["counts"] == {"completed": 24, "lost": 1, "running": 1}
    assert summary["truncated"] is True
    assert len(summary["items"]) == 20
    assert {item["run_id"] for item in summary["items"]} >= {
        "RUN-lost-important",
        "RUN-running-important",
    }
    assert all("unbounded_detail" not in item for item in summary["items"])

def test_run_discovery_reports_operational_error_without_inventing_lost_state(
    tmp_path: Path, monkeypatch,
) -> None:
    from arbor.aros import runs as runs_module

    _init_git(tmp_path)
    init_workspace(tmp_path, "Mission")

    class BrokenRunService:
        def __init__(self, root: Path) -> None:
            assert root == tmp_path.resolve()

        def list(self) -> list[dict[str, object]]:
            raise runs_module.RunError("receipt store is unreadable")

    monkeypatch.setattr(runs_module, "RunService", BrokenRunService)

    status = status_workspace(tmp_path)
    assert status["runs"] == {
        "total": None,
        "counts": {},
        "items": [],
        "truncated": False,
        "operational_error": "receipt store is unreadable",
    }

def test_status_workspace_reports_git_dirty_state_and_worktrees(tmp_path: Path) -> None:
    _init_git(tmp_path)
    init_workspace(tmp_path, "Mission")
    _git(tmp_path, "add", "AGENTS.md", "AROS.md", "memory/NOW.md", ".gitignore")
    _git(tmp_path, "commit", "-qm", "initialize workspace")
    (tmp_path / "memory" / "NOW.md").write_text("# Changed\n", encoding="utf-8")
    (tmp_path / "untracked.txt").write_text("new", encoding="utf-8")

    status = status_workspace(tmp_path)
    git = status["git"]

    assert git["is_repository"] is True
    assert git["branch"] in {"main", "master"}
    assert git["head"] == _git(tmp_path, "rev-parse", "HEAD")
    assert git["dirty"] is True
    assert any("memory/NOW.md" in line for line in git["changes"])
    assert any("untracked.txt" in line for line in git["changes"])
    assert git["changes_truncated"] is False
    assert git["worktrees"] == [
        {
            "path": str(tmp_path.resolve()),
            "head": git["head"],
            "branch": git["branch"],
            "detached": False,
        }
    ]
    assert git["worktrees_truncated"] is False


def test_boot_workspace_uses_only_durable_allowlisted_views_and_git(tmp_path: Path) -> None:
    _init_git(tmp_path)
    init_workspace(tmp_path, "Find the causal mechanism.")
    (tmp_path / "memory" / "NOW.md").write_text(
        "# Current State\n\nThe strongest counterevidence is C.\n",
        encoding="utf-8",
    )
    (tmp_path / "questions" / "FRONTIER.md").write_text(
        "# Frontier\n\nIs mediator M load-bearing?\n",
        encoding="utf-8",
    )
    (tmp_path / "runs").mkdir()
    (tmp_path / "runs" / "ACTIVE.md").write_text(
        "FORBIDDEN_ACTIVE_MD_SENTINEL",
        encoding="utf-8",
    )
    (tmp_path / ".arbor" / "sessions").mkdir(parents=True)
    (tmp_path / ".arbor" / "sessions" / "transcript.md").write_text(
        "FORBIDDEN_TRANSCRIPT_SENTINEL", encoding="utf-8"
    )
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "memory.md").write_text(
        "FORBIDDEN_PROVIDER_SENTINEL", encoding="utf-8"
    )

    boot = boot_workspace(tmp_path)

    packet = json.loads(boot)
    assert packet["schema_version"] == 1
    assert packet["snapshot"]["candidate"]["git_status"]["dirty"] is True
    assert packet["snapshot"]["canonical"] is None
    assert "FORBIDDEN_ACTIVE_MD_SENTINEL" not in boot
    assert "FORBIDDEN_TRANSCRIPT_SENTINEL" not in boot
    assert "FORBIDDEN_PROVIDER_SENTINEL" not in boot
    assert "IdeaTree" not in boot


def test_boot_workspace_has_a_strict_size_limit_without_losing_git_reality(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)
    init_workspace(tmp_path, "M" * 20_000)
    (tmp_path / "memory" / "NOW.md").write_text("N" * 20_000, encoding="utf-8")
    (tmp_path / "questions" / "FRONTIER.md").write_text(
        "Q" * 20_000, encoding="utf-8"
    )

    boot = boot_workspace(tmp_path, max_chars=1_000)
    packet = json.loads(boot)

    assert len(boot) <= 1_000
    assert set(packet) == {
        "schema_version",
        "snapshot",
        "active_question",
        "current_uncertainty",
        "recent_evidence_delta",
        "hypotheses",
        "pending_measurements",
        "unassimilated_returns",
        "current_obligations",
        "remaining_budget",
        "blocked_reasons",
        "authority",
        "warnings",
        "omitted",
    }
    assert "truncated" in packet["warnings"]
    assert "canonical" in packet["snapshot"]
    assert "candidate" in packet["snapshot"]


def test_boot_workspace_rejects_a_useless_size_limit(tmp_path: Path) -> None:
    _init_git(tmp_path)
    init_workspace(tmp_path, "Mission")

    with pytest.raises(ValueError, match="max_chars"):
        boot_workspace(tmp_path, max_chars=100)


def test_boot_workspace_requires_initialized_workspace(tmp_path: Path) -> None:
    _init_git(tmp_path)

    with pytest.raises(ValueError, match="not initialized"):
        boot_workspace(tmp_path)
