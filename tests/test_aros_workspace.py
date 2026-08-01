"""Behavior tests for the minimal, transcript-independent AROS workspace."""

from __future__ import annotations

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
    assert not (tmp_path / "questions").exists()
    assert not (tmp_path / "model").exists()
    assert not (tmp_path / "ideas").exists()
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
    (tmp_path / "memory").mkdir()
    originals = {
        "AROS.md": "# Existing mission\n",
        "memory/NOW.md": "# Human state\nDo not replace me.\n",
        "AGENTS.md": "# Existing project instructions\n",
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
    assert first["preserved"] == ["AGENTS.md", "AROS.md", "memory/NOW.md"]
    assert second["created"] == []
    assert second["updated"] == []


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
    (tmp_path / "questions").mkdir()
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
        "active_runs": {"path": "runs/ACTIVE.md", "exists": False},
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
    (tmp_path / "questions").mkdir()
    (tmp_path / "questions" / "FRONTIER.md").write_text(
        "# Frontier\n\nIs mediator M load-bearing?\n",
        encoding="utf-8",
    )
    (tmp_path / "runs").mkdir()
    (tmp_path / "runs" / "ACTIVE.md").write_text(
        "# Active Runs\n\nrun-001 is running.\n",
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

    assert "Find the causal mechanism." in boot
    assert "The strongest counterevidence is C." in boot
    assert "Is mediator M load-bearing?" in boot
    assert "run-001 is running." in boot
    assert "## Git and workspace status" in boot
    assert "Branch:" in boot
    assert "FORBIDDEN_TRANSCRIPT_SENTINEL" not in boot
    assert "FORBIDDEN_PROVIDER_SENTINEL" not in boot
    assert "IdeaTree" not in boot


def test_boot_workspace_has_a_strict_size_limit_without_losing_git_reality(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)
    init_workspace(tmp_path, "M" * 20_000)
    (tmp_path / "memory" / "NOW.md").write_text("N" * 20_000, encoding="utf-8")
    (tmp_path / "questions").mkdir()
    (tmp_path / "questions" / "FRONTIER.md").write_text(
        "Q" * 20_000, encoding="utf-8"
    )

    boot = boot_workspace(tmp_path, max_chars=1_000)

    assert len(boot) <= 1_000
    assert "## Mission and constraints" in boot
    assert "## Working memory" in boot
    assert "## Live questions" in boot
    assert "## Git and workspace status" in boot
    assert "truncated" in boot


def test_boot_workspace_rejects_a_useless_size_limit(tmp_path: Path) -> None:
    _init_git(tmp_path)
    init_workspace(tmp_path, "Mission")

    with pytest.raises(ValueError, match="max_chars"):
        boot_workspace(tmp_path, max_chars=100)


def test_boot_workspace_requires_initialized_workspace(tmp_path: Path) -> None:
    _init_git(tmp_path)

    with pytest.raises(ValueError, match="not initialized"):
        boot_workspace(tmp_path)
