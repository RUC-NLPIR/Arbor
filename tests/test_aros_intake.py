"""Native AROS research intake behavior."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

import arbor.aros.intake as intake_module
from arbor.aros.intake import (
    IntakeError,
    LocalMaterial,
    initialize_knowledge_bank,
    inspect_local_materials,
)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _pdf_bytes(text: str) -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=240, height=160)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject(
                {NameObject("/F1"): font}
            )
        }
    )
    content = DecodedStreamObject()
    content.set_data(
        f"BT /F1 12 Tf 20 80 Td ({text}) Tj ET".encode("ascii")
    )
    page[NameObject("/Contents")] = content
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def test_inspect_markdown_preserves_exact_bytes_and_provenance(
    tmp_path: Path,
) -> None:
    source = tmp_path / "paper.md"
    raw = b"# Finding\n\nObserved mechanism.\n"
    source.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()

    materials = inspect_local_materials([source])

    assert materials == (
        LocalMaterial(
            source_id=f"SRC-{digest[:16]}",
            kind="markdown",
            original_name="paper.md",
            content_sha256=digest,
            content=raw,
            extracted="# Finding\n\nObserved mechanism.\n",
            provided_path=str(source),
        ),
    )


def test_inspect_pdf_uses_pypdf_and_requires_real_extracted_text(
    tmp_path: Path,
) -> None:
    source = tmp_path / "paper.pdf"
    raw = _pdf_bytes("Observed mechanism")
    source.write_bytes(raw)

    material = inspect_local_materials([source])[0]

    assert material.kind == "pdf"
    assert material.content == raw
    assert "Observed mechanism" in material.extracted


def test_inspect_materials_deduplicates_identical_bytes_in_input_order(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.md"
    duplicate = tmp_path / "duplicate.md"
    last = tmp_path / "last.md"
    first.write_text("same\n", encoding="utf-8")
    duplicate.write_text("same\n", encoding="utf-8")
    last.write_text("different\n", encoding="utf-8")

    materials = inspect_local_materials([first, duplicate, last])

    assert [material.original_name for material in materials] == [
        "first.md",
        "last.md",
    ]


@pytest.mark.parametrize("name", ["paper.txt", "paper.json", "paper"])
def test_inspect_materials_rejects_unsupported_suffix(
    tmp_path: Path,
    name: str,
) -> None:
    source = tmp_path / name
    source.write_text("content", encoding="utf-8")

    with pytest.raises(IntakeError, match="unsupported material type"):
        inspect_local_materials([source])


def test_inspect_materials_rejects_symlink_nonfile_and_invalid_utf8(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target.md"
    target.write_text("target", encoding="utf-8")
    link = tmp_path / "link.md"
    link.symlink_to(target)
    directory = tmp_path / "directory.md"
    directory.mkdir()
    invalid = tmp_path / "invalid.md"
    invalid.write_bytes(b"\xff")

    with pytest.raises(IntakeError, match="plain file"):
        inspect_local_materials([link])
    with pytest.raises(IntakeError, match="plain file"):
        inspect_local_materials([directory])
    with pytest.raises(IntakeError, match="not UTF-8"):
        inspect_local_materials([invalid])


def test_inspect_materials_rejects_empty_pdf_extraction(tmp_path: Path) -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    source = tmp_path / "blank.pdf"
    source.write_bytes(buffer.getvalue())

    with pytest.raises(IntakeError, match="no extracted text"):
        inspect_local_materials([source])


def test_inspect_materials_rejects_input_over_byte_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "large.md"
    source.write_bytes(b"12345")
    monkeypatch.setattr(intake_module, "MAX_LOCAL_MATERIAL_BYTES", 4)

    with pytest.raises(IntakeError, match="exceeds byte limit"):
        inspect_local_materials([source])


def test_initialize_knowledge_bank_creates_question_source_and_one_commit(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "causal_KB"
    paper = tmp_path / "paper.md"
    raw = b"# Prior work\n\nThe intervention changed outcome Y.\n"
    paper.write_bytes(raw)
    question = "What mechanism explains the change in outcome Y?"

    receipt = initialize_knowledge_bank(workspace, question, [paper])

    assert receipt["schema_version"] == 1
    assert receipt["root"] == str(workspace.resolve())
    assert receipt["question_id"] == "Q-0001"
    assert receipt["commit"] == _git(workspace, "rev-parse", "HEAD")
    assert receipt["source_ids"] == [
        "SRC-" + hashlib.sha256(raw).hexdigest()[:16]
    ]
    assert _git(workspace, "branch", "--show-current") == "main"
    assert _git(workspace, "log", "-1", "--format=%an <%ae>") == (
        "AROS Intake <aros-intake@local.invalid>"
    )
    assert _git(workspace, "status", "--short") == ""

    question_text = (workspace / "questions/Q-0001/question.md").read_text(
        encoding="utf-8"
    )
    assert question in question_text
    for heading in (
        "Why load-bearing",
        "Current best answer",
        "Live alternatives",
        "Known facts",
        "Current uncertainty",
        "Unobserved variables",
        "Evidence that would change the answer",
        "Resolution criterion",
        "Stop / pivot criterion",
        "Expected information gain",
        "Links",
    ):
        assert f"## {heading}" in question_text
    assert "Not yet assessed" in question_text
    assert "focus_question: Q-0001" in (
        workspace / "questions/FRONTIER.md"
    ).read_text(encoding="utf-8")
    assert "No explanatory model has been admitted" in (
        workspace / "model/CURRENT.md"
    ).read_text(encoding="utf-8")
    assert list((workspace / "ideas").glob("I-*.md")) == []
    assert list((workspace / "knowledge/claims").glob("C-*.md")) == []

    source_id = receipt["source_ids"][0]
    source_root = workspace / "sources/papers" / source_id
    assert (source_root / "original.md").read_bytes() == raw
    assert (source_root / "extracted.md").read_text(encoding="utf-8") == raw.decode()
    metadata = json.loads((source_root / "metadata.json").read_bytes())
    assert metadata == {
        "schema_version": 1,
        "source_id": source_id,
        "kind": "markdown",
        "original_name": "paper.md",
        "content_sha256": hashlib.sha256(raw).hexdigest(),
        "byte_size": len(raw),
        "ingested_from": str(paper),
        "extracted_ref": f"sources/papers/{source_id}/extracted.md",
    }


def test_initialize_knowledge_bank_preserves_repo_owned_files(
    tmp_path: Path,
) -> None:
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "README.md").write_text("# Existing repo\n", encoding="utf-8")
    agents = b"# Existing instructions\nDo not replace.\n"
    (tmp_path / "AGENTS.md").write_bytes(agents)
    (tmp_path / ".gitignore").write_text("build/", encoding="utf-8")
    _git(tmp_path, "add", "README.md", "AGENTS.md", ".gitignore")
    _git(
        tmp_path,
        "-c",
        "user.name=Repo Owner",
        "-c",
        "user.email=owner@example.invalid",
        "commit",
        "-qm",
        "existing repository",
    )
    existing = _git(tmp_path, "rev-parse", "HEAD")

    initialize_knowledge_bank(tmp_path, "Why does the system fail?")

    assert (tmp_path / "AGENTS.md").read_bytes() == agents
    assert (tmp_path / ".gitignore").read_text(encoding="utf-8") == (
        "build/\n/.aros/\n/.worktree/\n"
    )
    assert _git(tmp_path, "rev-parse", "HEAD^") == existing
    changed = set(
        _git(tmp_path, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").splitlines()
    )
    assert "AGENTS.md" not in changed
    assert {"AROS.md", ".gitignore", "questions/Q-0001/question.md"} <= changed


def test_initialize_validates_question_and_material_before_workspace_mutation(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "new_KB"
    unsupported = tmp_path / "paper.txt"
    unsupported.write_text("not supported", encoding="utf-8")

    with pytest.raises(IntakeError, match="Question"):
        initialize_knowledge_bank(workspace, "  ")
    assert not workspace.exists()

    with pytest.raises(IntakeError, match="unsupported material"):
        initialize_knowledge_bank(workspace, "A real question?", [unsupported])
    assert not workspace.exists()


def test_initialize_rejects_dirty_repo_before_aros_paths_are_written(
    tmp_path: Path,
) -> None:
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "untracked.txt").write_text("dirty", encoding="utf-8")

    with pytest.raises(IntakeError, match="must be clean"):
        initialize_knowledge_bank(tmp_path, "Why is it dirty?")

    assert not (tmp_path / "AROS.md").exists()
    assert not (tmp_path / "memory").exists()


def test_initialize_rejects_partial_aros_state_without_merging(
    tmp_path: Path,
) -> None:
    _git(tmp_path, "init", "-q", "-b", "main")
    original = b"# Existing AROS meaning\n"
    (tmp_path / "AROS.md").write_bytes(original)
    _git(tmp_path, "add", "AROS.md")
    _git(
        tmp_path,
        "-c",
        "user.name=Owner",
        "-c",
        "user.email=owner@example.invalid",
        "commit",
        "-qm",
        "partial state",
    )

    with pytest.raises(IntakeError, match="partial AROS state"):
        initialize_knowledge_bank(tmp_path, "Do not merge this state?")

    assert (tmp_path / "AROS.md").read_bytes() == original
    assert not (tmp_path / "memory").exists()


def test_initialize_requires_root_attached_repo_and_plain_repo_owned_files(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "seed.txt").write_text("seed", encoding="utf-8")
    _git(repo, "add", "seed.txt")
    _git(
        repo,
        "-c",
        "user.name=Owner",
        "-c",
        "user.email=owner@example.invalid",
        "commit",
        "-qm",
        "seed",
    )

    nested = repo / "nested"
    nested.mkdir()
    with pytest.raises(IntakeError, match="Git repository root"):
        initialize_knowledge_bank(nested, "Nested?")

    _git(repo, "checkout", "-q", "--detach")
    with pytest.raises(IntakeError, match="attached"):
        initialize_knowledge_bank(repo, "Detached?")
    _git(repo, "switch", "-q", "main")

    outside = tmp_path / "outside-agents"
    outside.write_text("outside", encoding="utf-8")
    (repo / "AGENTS.md").symlink_to(outside)
    with pytest.raises(IntakeError, match="AGENTS.md must be a plain file"):
        initialize_knowledge_bank(repo, "Symlink?")
    assert outside.read_text(encoding="utf-8") == "outside"
