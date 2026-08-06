"""Native AROS research intake mechanics."""

from __future__ import annotations

import hashlib
import io
import json
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Sequence

from pypdf import PdfReader


MAX_LOCAL_MATERIAL_BYTES = 64 * 1024 * 1024
_AROS_OWNED_ROOTS = (
    "AROS.md",
    "memory",
    "questions",
    "model",
    "ideas",
    "knowledge",
    "sources",
    "eval",
    "tasks",
    "runs",
    "transitions",
    ".aros",
    ".worktree",
)
_IGNORE_ENTRIES = ("/.aros/", "/.worktree/")


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


def initialize_knowledge_bank(
    workspace: str | Path,
    question: str,
    material_paths: Sequence[str | Path] = (),
) -> dict[str, object]:
    """Create one Question-centered KB and its exact initialization commit."""
    if not isinstance(question, str) or not question.strip():
        raise IntakeError("Key Research Question must be non-empty")
    exact_question = question.strip()
    materials = inspect_local_materials(material_paths)
    root = Path(workspace).expanduser().resolve(strict=False)
    is_new = not root.exists()
    if is_new:
        parent = root.parent
        try:
            parent_metadata = parent.lstat()
        except OSError as error:
            raise IntakeError(f"workspace parent does not exist: {parent}") from error
        if stat.S_ISLNK(parent_metadata.st_mode) or not stat.S_ISDIR(
            parent_metadata.st_mode
        ):
            raise IntakeError(f"workspace parent must be a plain directory: {parent}")
    else:
        metadata = root.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise IntakeError(f"workspace must be a plain directory: {root}")
        top = _git(root, "rev-parse", "--show-toplevel", check=False)
        if top.returncode != 0 or Path(top.stdout.strip()).resolve() != root:
            raise IntakeError(f"workspace must be a Git repository root: {root}")
        if _git(root, "symbolic-ref", "--quiet", "--short", "HEAD", check=False).returncode:
            raise IntakeError("workspace must have an attached Git branch")
        _require_optional_plain_file(root / "AGENTS.md", "AGENTS.md")
        _require_optional_plain_file(root / ".gitignore", ".gitignore")
        if _git(root, "status", "--porcelain=v1", "--untracked-files=all").stdout:
            raise IntakeError("workspace must be clean before AROS intake")
        collisions = [name for name in _AROS_OWNED_ROOTS if (root / name).exists()]
        if collisions:
            raise IntakeError(
                "workspace contains partial AROS state: " + ", ".join(collisions)
            )

    if is_new:
        root.mkdir()
        result = _git(root, "init", "-q", "-b", "main", check=False)
        if result.returncode != 0:
            raise IntakeError("unable to initialize workspace Git repository")

    for relative in (
        "memory/decisions",
        "questions/Q-0001",
        "model/rivals",
        "ideas",
        "knowledge/claims",
        "sources/papers",
        "eval",
        "tasks",
        "runs",
        "transitions",
        ".aros",
        ".worktree",
    ):
        (root / relative).mkdir(parents=True, exist_ok=False)

    created: list[str] = []
    if not (root / "AGENTS.md").exists():
        _write_text(root, "AGENTS.md", _agents_text(), created)
    _write_text(root, "AROS.md", _aros_text(exact_question), created)
    _write_text(root, "memory/NOW.md", _now_text(materials), created)
    _write_text(root, "questions/FRONTIER.md", _frontier_text(), created)
    _write_text(root, "questions/Q-0001/question.md", _question_text(exact_question), created)
    _write_text(root, "model/CURRENT.md", _model_text(), created)

    manifest_sources: list[dict[str, object]] = []
    for material in materials:
        source_root = f"sources/papers/{material.source_id}"
        (root / source_root).mkdir()
        suffix = ".pdf" if material.kind == "pdf" else ".md"
        original_ref = f"{source_root}/original{suffix}"
        extracted_ref = f"{source_root}/extracted.md"
        metadata_ref = f"{source_root}/metadata.json"
        _write_bytes(root, original_ref, material.content, created)
        _write_text(root, extracted_ref, material.extracted, created)
        metadata_value: dict[str, object] = {
            "schema_version": 1,
            "source_id": material.source_id,
            "kind": material.kind,
            "original_name": material.original_name,
            "content_sha256": material.content_sha256,
            "byte_size": len(material.content),
            "ingested_from": material.provided_path,
            "extracted_ref": extracted_ref,
        }
        _write_json(root, metadata_ref, metadata_value, created)
        manifest_sources.append(
            {
                "source_id": material.source_id,
                "metadata_ref": metadata_ref,
                "original_ref": original_ref,
            }
        )
    _write_json(
        root,
        "sources/manifest.json",
        {"schema_version": 1, "sources": manifest_sources},
        created,
    )

    ignore_changed = _write_ignore(root / ".gitignore")
    staged = list(created)
    if ignore_changed:
        staged.append(".gitignore")
    add = _git(root, "add", "--", *staged, check=False)
    if add.returncode != 0:
        raise IntakeError("unable to stage AROS bootstrap paths")
    commit = _git(
        root,
        "-c",
        "user.name=AROS Intake",
        "-c",
        "user.email=aros-intake@local.invalid",
        "commit",
        "-qm",
        "Initialize AROS research workspace",
        check=False,
    )
    if commit.returncode != 0:
        raise IntakeError("unable to create AROS initialization commit")
    head = _git(root, "rev-parse", "HEAD").stdout.strip()
    return {
        "schema_version": 1,
        "root": str(root),
        "question_id": "Q-0001",
        "source_ids": [material.source_id for material in materials],
        "created_paths": sorted(created),
        "commit": head,
    }


def _git(
    root: Path,
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise IntakeError(detail or f"git {args[0]} failed")
    return result


def _require_optional_plain_file(path: Path, description: str) -> None:
    if not path.exists() and not path.is_symlink():
        return
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise IntakeError(f"{description} must be a plain file")


def _write_text(root: Path, relative: str, content: str, created: list[str]) -> None:
    _write_bytes(root, relative, content.encode("utf-8"), created)


def _write_json(
    root: Path,
    relative: str,
    value: dict[str, object],
    created: list[str],
) -> None:
    _write_text(
        root,
        relative,
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        created,
    )


def _write_bytes(root: Path, relative: str, content: bytes, created: list[str]) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(content)
    except FileExistsError as error:
        raise IntakeError(f"AROS bootstrap path already exists: {relative}") from error
    created.append(relative)


def _write_ignore(path: Path) -> bool:
    if path.exists():
        content = path.read_text(encoding="utf-8")
    else:
        content = ""
    lines = set(content.splitlines())
    missing = [entry for entry in _IGNORE_ENTRIES if entry not in lines]
    if not missing:
        return False
    prefix = "" if not content or content.endswith("\n") else "\n"
    path.write_text(
        content + prefix + "".join(f"{entry}\n" for entry in missing),
        encoding="utf-8",
    )
    return True


def _agents_text() -> str:
    return (
        "# AROS Workspace\n\n"
        "Act as the scientific principal for this workspace.\n\n"
        "- Treat project files and Git history as durable memory.\n"
        "- Treat source, Task, Run, and Eval returns as observations.\n"
        "- Assimilate observations explicitly before changing canonical meaning.\n"
        "- Put write-heavy child work only in `.worktree/`.\n"
    )


def _aros_text(question: str) -> str:
    return f"# AROS Project\n\n## Mission\n\nAnswer the Key Research Question.\n\n## Key Research Question\n\n{question}\n"


def _frontier_text() -> str:
    return "---\nfocus_question: Q-0001\n---\n# Research Frontier\n"


def _question_text(question: str) -> str:
    return (
        "---\nid: Q-0001\nstatus: open\nparents: []\nscope: project\n---\n"
        "# Question\n\n"
        f"## Key Research Question\n\n{question}\n\n"
        "## Why load-bearing\n\nNot yet assessed.\n\n"
        "## Current best answer\n\nNot yet assessed.\n\n"
        "## Live alternatives\n\nNot yet assessed.\n\n"
        "## Known facts\n\nThe Key Research Question was supplied by the human owner.\n\n"
        "## Current uncertainty\n\nNot yet assessed.\n\n"
        "## Unobserved variables\n\nNot yet assessed.\n\n"
        "## Evidence that would change the answer\n\nNot yet assessed.\n\n"
        "## Resolution criterion\n\nNot yet assessed.\n\n"
        "## Stop / pivot criterion\n\nNot yet assessed.\n\n"
        "## Expected information gain\n\nNot yet assessed.\n\n"
        "## Links\n"
    )


def _model_text() -> str:
    return "# Current Model\n\nNo explanatory model has been admitted.\n"


def _now_text(materials: Sequence[LocalMaterial]) -> str:
    lines = [
        "# Current State",
        "",
        "## Active question",
        "",
        "- `questions/Q-0001/question.md`",
        "",
        "## Unassimilated local sources",
        "",
    ]
    if materials:
        lines.extend(
            f"- `sources/papers/{material.source_id}/metadata.json`"
            for material in materials
        )
    else:
        lines.append("None.")
    return "\n".join(lines) + "\n"


__all__ = [
    "IntakeError",
    "LocalMaterial",
    "MAX_LOCAL_MATERIAL_BYTES",
    "initialize_knowledge_bank",
    "inspect_local_materials",
]
