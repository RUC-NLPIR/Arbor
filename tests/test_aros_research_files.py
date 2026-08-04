"""Behavior tests for Principal-authored semantic research files."""

from __future__ import annotations

import os
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from arbor.aros import research_files as research_files_module
from arbor.aros.research_files import (
    ResearchFileError,
    read_semantic_document,
)
from arbor.aros.store import json_sha256


def _write(root: Path, relative: str, content: str) -> str:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return relative


def _write_claim(root: Path, line: str) -> str:
    return _write(
        root,
        "knowledge/claims/C-0001.md",
        "---\n"
        "id: C-0001\n"
        "---\n"
        "# Claim\n\n"
        "## Evidence links\n"
        f"{line}\n",
    )


def test_question_frontmatter_id_must_match_path(tmp_path: Path) -> None:
    relative = _write(
        tmp_path,
        "questions/Q-0001/question.md",
        "---\nid: Q-9999\n---\n# Question\n",
    )

    with pytest.raises(ResearchFileError, match="identifier"):
        read_semantic_document(tmp_path, relative)


def test_claim_and_idea_frontmatter_ids_must_match_paths(tmp_path: Path) -> None:
    claim = _write(
        tmp_path,
        "knowledge/claims/C-0001.md",
        "---\nid: C-9999\n---\n# Claim\n",
    )
    idea = _write(
        tmp_path,
        "ideas/I-0001.md",
        "---\nid: I-9999\n---\n# Idea\n",
    )

    for relative in (claim, idea):
        with pytest.raises(ResearchFileError, match="identifier"):
            read_semantic_document(tmp_path, relative)


def test_frontmatter_rejects_duplicate_keys(tmp_path: Path) -> None:
    relative = _write(
        tmp_path,
        "questions/Q-0001/question.md",
        "---\nid: Q-0001\nid: Q-0001\n---\n# Question\n",
    )

    with pytest.raises(ResearchFileError, match="duplicate"):
        read_semantic_document(tmp_path, relative)


def test_evidence_link_accepts_exact_three_field_json_line(tmp_path: Path) -> None:
    first = {
        "observation_ref": f"eval/evaluations/EVAL-{'a' * 64}/receipt.json",
        "relation": "supports",
        "scope": "seed 7",
    }
    second = {
        "observation_ref": "runs/RUN-a/final.json",
        "relation": "bounds",
        "scope": "single accelerator",
    }
    claim = _write_claim(
        tmp_path,
        "{"
        f'"scope":"{first["scope"]}",'
        f'"relation":"{first["relation"]}",'
        f'"observation_ref":"{first["observation_ref"]}"'
        "}\n"
        "{"
        f'"observation_ref":"{second["observation_ref"]}",'
        f'"relation":"{second["relation"]}",'
        f'"scope":"{second["scope"]}"'
        "}\n\n"
        "## Notes\n"
        '{"observation_ref":"prose","relation":"proves","scope":"ignored"}',
    )

    document = read_semantic_document(tmp_path, claim)

    assert document.path == claim
    assert document.identifier == "C-0001"
    assert [occurrence.anchor for occurrence in document.evidence_links] == [
        "Evidence links",
        "Evidence links",
    ]
    assert [occurrence.ordinal for occurrence in document.evidence_links] == [0, 1]
    assert document.evidence_links[0].link.relation == "supports"
    assert document.evidence_links[0].canonical_sha256 == json_sha256(first)
    assert document.evidence_links[1].canonical_sha256 == json_sha256(second)


@pytest.mark.parametrize(
    "line",
    [
        '{"observation_ref":"runs/RUN-a/final.json","relation":"supports",'
        '"scope":"x","extra":1}',
        '{"observation_ref":"runs/RUN-a/final.json","relation":"proves",'
        '"scope":"x"}',
        '{"observation_ref":"runs/RUN-a/final.json",'
        '"observation_ref":"x","relation":"supports","scope":"x"}',
        '{"observation_ref":"runs/RUN-a/final.json","relation":null,'
        '"scope":"x"}',
        '{"observation_ref":"runs/RUN-a/final.json","relation":"supports",'
        '"scope":"   "}',
    ],
)
def test_evidence_link_rejects_duplicate_unknown_or_invalid_relation(
    tmp_path: Path,
    line: str,
) -> None:
    claim = _write_claim(tmp_path, line)

    with pytest.raises(ResearchFileError):
        read_semantic_document(tmp_path, claim)


def test_duplicate_markdown_heading_is_mechanically_ambiguous(tmp_path: Path) -> None:
    claim = _write(
        tmp_path,
        "knowledge/claims/C-0001.md",
        "---\nid: C-0001\n---\n"
        "# Claim\n\n"
        "## Evidence links\n"
        "not-json\n\n"
        "## Evidence links ##\n"
        '{"observation_ref":"runs/RUN-a/final.json","relation":"supports",'
        '"scope":"x"}\n',
    )

    with pytest.raises(
        ResearchFileError,
        match=r"knowledge/claims/C-0001\.md.*Evidence links",
    ):
        read_semantic_document(tmp_path, claim)


def test_missing_recommended_sections_are_warnings(tmp_path: Path) -> None:
    question = _write(
        tmp_path,
        "questions/Q-0001/question.md",
        "---\nid: Q-0001\n---\n# Question\n",
    )
    claim = _write(
        tmp_path,
        "knowledge/claims/C-0001.md",
        "---\nid: C-0001\n---\n# Claim\n",
    )

    question_document = read_semantic_document(tmp_path, question)
    claim_document = read_semantic_document(tmp_path, claim)

    assert "missing recommended section: Current uncertainty" in (
        question_document.warnings
    )
    assert "missing recommended section: Counterevidence" in claim_document.warnings
    assert not any("Question" in warning for warning in question_document.warnings)
    assert not any("Claim" in warning for warning in claim_document.warnings)


def test_semantic_reader_rejects_symlink_non_utf8_and_escape(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    target = tmp_path / "target.md"
    target.write_text("# Question\n", encoding="utf-8")
    symlink = root / "questions/Q-0001/question.md"
    symlink.parent.mkdir(parents=True)
    symlink.symlink_to(target)

    with pytest.raises(ResearchFileError, match="symlink"):
        read_semantic_document(root, "questions/Q-0001/question.md")

    non_utf8 = root / "knowledge/claims/C-0001.md"
    non_utf8.parent.mkdir(parents=True)
    non_utf8.write_bytes(b"\xff\xfe")
    with pytest.raises(ResearchFileError, match="UTF-8"):
        read_semantic_document(root, "knowledge/claims/C-0001.md")

    outside = tmp_path / "outside.md"
    outside.write_text("# Outside\n", encoding="utf-8")
    with pytest.raises(ResearchFileError, match="contained"):
        read_semantic_document(root, "../outside.md")


def test_semantic_reader_rejects_symlink_root(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    relative = _write(
        actual,
        "questions/Q-0001/question.md",
        "---\nid: Q-0001\n---\n# Question\n",
    )
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(actual, target_is_directory=True)

    with pytest.raises(ResearchFileError, match="symlink"):
        read_semantic_document(linked_root, relative)


@pytest.mark.parametrize("swap", ["component", "final"])
def test_semantic_reader_rejects_component_or_final_symlink_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    swap: str,
) -> None:
    root = tmp_path / "workspace"
    relative = _write(
        root,
        "questions/Q-0001/question.md",
        "---\nid: Q-0001\n---\n# Question\n",
    )
    outside = tmp_path / "outside"
    _write(
        outside,
        "question.md",
        "---\nid: Q-0001\n---\n# Question\n",
    )
    original_open = os.open
    swapped = False

    def racing_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        name = os.fsdecode(path)
        trigger = "Q-0001" if swap == "component" else "question.md"
        if not swapped and name == trigger:
            if swap == "component":
                victim = root / "questions/Q-0001"
                victim.rename(root / "questions/Q-0001-original")
                victim.symlink_to(outside, target_is_directory=True)
            else:
                victim = root / relative
                victim.rename(victim.with_suffix(".original.md"))
                victim.symlink_to(outside / "question.md")
            swapped = True
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(research_files_module, "_open", racing_open, raising=False)

    with pytest.raises(ResearchFileError, match="symlink|contained"):
        read_semantic_document(root, relative)
    assert swapped is True


def test_frontier_focus_is_optional_and_does_not_hide_other_questions(
    tmp_path: Path,
) -> None:
    frontier = _write(
        tmp_path,
        "questions/FRONTIER.md",
        "---\n---\n"
        "# Research Frontier\n\n"
        "## Live branches\n"
        "- [Q-0001](Q-0001/question.md) — live\n"
        "- [Q-0002](Q-0002/question.md) — blocked\n"
        "- [Q-0003](Q-0003/question.md) — speculative\n",
    )

    document = read_semantic_document(tmp_path, frontier)

    assert document.identifier is None
    assert document.frontmatter == {}
    assert "Q-0001" in document.sections["Live branches"]
    assert "Q-0002" in document.sections["Live branches"]
    assert "Q-0003" in document.sections["Live branches"]
    assert document.warnings == ()


def test_semantic_records_are_immutable(tmp_path: Path) -> None:
    claim = _write_claim(
        tmp_path,
        '{"observation_ref":"runs/RUN-a/final.json","relation":"context",'
        '"scope":"diagnostic only"}',
    )
    document = read_semantic_document(tmp_path, claim)

    with pytest.raises(FrozenInstanceError):
        document.path = "other.md"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        document.evidence_links[0].link.scope = "changed"  # type: ignore[misc]
    with pytest.raises(TypeError):
        document.frontmatter["id"] = "C-9999"  # type: ignore[index]
    with pytest.raises(TypeError):
        document.sections["Evidence links"] = "changed"  # type: ignore[index]
