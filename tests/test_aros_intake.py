"""Native AROS research intake behavior."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

import arbor.aros.intake as intake_module
from arbor.aros.intake import IntakeError, LocalMaterial, inspect_local_materials


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
