"""Native AROS research intake mechanics."""

from __future__ import annotations

import hashlib
import io
import stat
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Sequence

from pypdf import PdfReader


MAX_LOCAL_MATERIAL_BYTES = 64 * 1024 * 1024


class IntakeError(ValueError):
    """Reject invalid intake without interpreting scientific meaning."""


@dataclass(frozen=True)
class LocalMaterial:
    """One exact local source observation prepared before workspace mutation."""

    source_id: str
    kind: str
    original_name: str
    content_sha256: str
    content: bytes
    extracted: str
    provided_path: str


def inspect_local_materials(
    paths: Sequence[str | Path],
) -> tuple[LocalMaterial, ...]:
    """Read, hash, and extract supported local materials once in input order."""
    materials: list[LocalMaterial] = []
    seen: set[str] = set()
    for supplied in paths:
        path = Path(supplied).expanduser()
        try:
            metadata = path.lstat()
        except OSError as error:
            raise IntakeError(f"material must be a plain file: {path}") from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise IntakeError(f"material must be a plain file: {path}")
        if metadata.st_size > MAX_LOCAL_MATERIAL_BYTES:
            raise IntakeError(f"material exceeds byte limit: {path}")
        try:
            content = path.read_bytes()
        except OSError as error:
            raise IntakeError(f"unable to read material: {path}") from error
        digest = hashlib.sha256(content).hexdigest()
        if digest in seen:
            continue

        suffix = path.suffix.lower()
        if suffix == ".md":
            try:
                extracted = content.decode("utf-8")
            except UnicodeDecodeError as error:
                raise IntakeError(f"Markdown is not UTF-8: {path}") from error
            kind = "markdown"
        elif suffix == ".pdf":
            try:
                reader = PdfReader(io.BytesIO(content))
                extracted = "\n\n".join(
                    page.extract_text() or "" for page in reader.pages
                ).strip()
            except Exception as error:
                raise IntakeError(f"PDF extraction failed: {path}") from error
            if not extracted:
                raise IntakeError(f"PDF contains no extracted text: {path}")
            kind = "pdf"
        else:
            raise IntakeError(f"unsupported material type: {path}")

        seen.add(digest)
        materials.append(
            LocalMaterial(
                source_id=f"SRC-{digest[:16]}",
                kind=kind,
                original_name=path.name,
                content_sha256=digest,
                content=content,
                extracted=extracted,
                provided_path=str(path),
            )
        )
    return tuple(materials)


__all__ = [
    "IntakeError",
    "LocalMaterial",
    "MAX_LOCAL_MATERIAL_BYTES",
    "inspect_local_materials",
]
