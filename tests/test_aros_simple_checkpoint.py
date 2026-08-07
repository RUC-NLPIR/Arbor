"""Ordinary Git checkpoint behavior without transition ceremony."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from arbor.aros.checkpoint import CheckpointError, GitCheckpoint
from arbor.aros.checkpoint_tool import CheckpointTool
from arbor.aros.observed import ObservedRefs


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repo(root: Path) -> str:
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "Checkpoint Test")
    _git(root, "config", "user.email", "checkpoint@example.invalid")
    (root / ".gitignore").write_text("/.aros/\n/.worktree/\n", encoding="utf-8")
    (root / "model").mkdir()
    (root / "model/CURRENT.md").write_text("# Model\nold\n", encoding="utf-8")
    _git(root, "add", ".gitignore", "model/CURRENT.md")
    _git(root, "commit", "-qm", "base")
    return _git(root, "rev-parse", "HEAD")


def test_git_checkpoint_commits_exact_paths_trailers_and_preserves_unselected_dirt(
    tmp_path: Path,
) -> None:
    parent = _repo(tmp_path)
    (tmp_path / "model/CURRENT.md").write_text("# Model\nnew\n", encoding="utf-8")
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory/NOW.md").write_text("# NOW\n", encoding="utf-8")
    (tmp_path / "unrelated.txt").write_text("keep dirty\n", encoding="utf-8")
    observed = (
        "eval/evaluations/EVAL-x/receipt.json",
        "tasks/TASK-x/collected.json",
    )

    result = GitCheckpoint(tmp_path).commit(
        paths=["model/CURRENT.md", "memory/NOW.md"],
        message="Revise model after measurement",
        observed_refs=observed,
    )

    assert result["parent"] == parent
    assert result["commit"] == _git(tmp_path, "rev-parse", "HEAD")
    assert result["paths"] == ["memory/NOW.md", "model/CURRENT.md"]
    assert result["observed_refs"] == list(observed)
    assert _git(tmp_path, "show", "HEAD:model/CURRENT.md") == "# Model\nnew"
    assert _git(tmp_path, "show", "HEAD:memory/NOW.md") == "# NOW"
    message = _git(tmp_path, "log", "-1", "--format=%B")
    assert message == (
        "Revise model after measurement\n\n"
        "AROS-Observed: eval/evaluations/EVAL-x/receipt.json\n"
        "AROS-Observed: tasks/TASK-x/collected.json"
    )
    assert (tmp_path / "unrelated.txt").read_text(encoding="utf-8") == "keep dirty\n"
    assert _git(tmp_path, "status", "--short") == "?? unrelated.txt"


def test_git_checkpoint_commits_tracked_deletion(tmp_path: Path) -> None:
    _repo(tmp_path)
    (tmp_path / "model/CURRENT.md").unlink()

    result = GitCheckpoint(tmp_path).commit(
        paths=["model/CURRENT.md"],
        message="Remove stale model",
    )

    assert result["paths"] == ["model/CURRENT.md"]
    assert "model/CURRENT.md" not in _git(
        tmp_path, "ls-tree", "-r", "--name-only", "HEAD"
    ).splitlines()


def test_git_checkpoint_rejects_preexisting_staged_change_without_mutation(
    tmp_path: Path,
) -> None:
    parent = _repo(tmp_path)
    (tmp_path / "staged.txt").write_text("staged\n", encoding="utf-8")
    _git(tmp_path, "add", "staged.txt")
    (tmp_path / "model/CURRENT.md").write_text("# Model\nnew\n", encoding="utf-8")

    with pytest.raises(CheckpointError, match="index must be clean"):
        GitCheckpoint(tmp_path).commit(
            paths=["model/CURRENT.md"],
            message="Must not commit",
        )

    assert _git(tmp_path, "rev-parse", "HEAD") == parent
    assert _git(tmp_path, "diff", "--cached", "--name-only") == "staged.txt"


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/absolute.md",
        "../escape.md",
        "model/../escape.md",
        ".git/config",
        ".aros/state.json",
        ".worktree/child/file",
        "model\\CURRENT.md",
        "model/CURRENT.md\x00",
    ],
)
def test_git_checkpoint_rejects_unsafe_paths(tmp_path: Path, path: str) -> None:
    _repo(tmp_path)

    with pytest.raises(CheckpointError):
        GitCheckpoint(tmp_path).commit(paths=[path], message="Unsafe")


def test_git_checkpoint_rejects_empty_or_duplicate_inputs(tmp_path: Path) -> None:
    _repo(tmp_path)

    with pytest.raises(CheckpointError, match="paths"):
        GitCheckpoint(tmp_path).commit(paths=[], message="No paths")
    with pytest.raises(CheckpointError, match="unique"):
        GitCheckpoint(tmp_path).commit(
            paths=["model/CURRENT.md", "model/CURRENT.md"],
            message="Duplicate",
        )
    with pytest.raises(CheckpointError, match="message"):
        GitCheckpoint(tmp_path).commit(paths=["model/CURRENT.md"], message="  ")


def test_checkpoint_tool_has_only_message_and_paths_inputs() -> None:
    assert CheckpointTool.input_schema == {
        "type": "object",
        "properties": {
            "message": {"type": "string", "minLength": 1},
            "paths": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "minItems": 1,
                "uniqueItems": True,
            },
        },
        "required": ["message", "paths"],
        "additionalProperties": False,
    }


def test_checkpoint_tool_records_and_clears_host_observations_after_success(
    tmp_path: Path,
) -> None:
    _repo(tmp_path)
    observed = ObservedRefs()
    observed.record("tasks/TASK-x/collected.json")
    (tmp_path / "model/CURRENT.md").write_text("# Model\nnew\n", encoding="utf-8")
    tool = CheckpointTool(
        cwd=str(tmp_path),
        observed=observed,
        persist_results=False,
    )

    result = json.loads(
        asyncio.run(
            tool.execute(
                message="Interpret returned work",
                paths=["model/CURRENT.md"],
            )
        )
    )

    assert result["observed_refs"] == ["tasks/TASK-x/collected.json"]
    assert observed.snapshot() == ()
    assert "AROS-Observed: tasks/TASK-x/collected.json" in _git(
        tmp_path, "log", "-1", "--format=%B"
    )


def test_checkpoint_tool_keeps_host_observations_when_commit_fails(
    tmp_path: Path,
) -> None:
    _repo(tmp_path)
    observed = ObservedRefs()
    observed.record("tasks/TASK-x/collected.json")
    tool = CheckpointTool(
        cwd=str(tmp_path),
        observed=observed,
        persist_results=False,
    )

    with pytest.raises(CheckpointError, match="no changes"):
        asyncio.run(
            tool.execute(
                message="Nothing changed",
                paths=["model/CURRENT.md"],
            )
        )

    assert observed.snapshot() == ("tasks/TASK-x/collected.json",)


def test_generated_record_commit_reuses_exact_head_without_new_commit(
    tmp_path: Path,
) -> None:
    parent = _repo(tmp_path)
    (tmp_path / "runs").mkdir()
    (tmp_path / "runs/final.json").write_text("{}\n", encoding="utf-8")
    checkpoint = GitCheckpoint(tmp_path)

    first = checkpoint.commit_paths(
        ("runs/final.json",),
        "Record run final",
    )
    reused = checkpoint.commit_paths(
        ("runs/final.json",),
        "Record run final",
    )

    assert first["parent"] == parent
    assert reused == {
        "commit": first["commit"],
        "paths": ["runs/final.json"],
        "reused": True,
        "enforcement_class": "cooperative",
    }
    assert _git(tmp_path, "rev-list", "--count", f"{parent}..HEAD") == "1"
