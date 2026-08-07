"""Pure parsing for Principal-authored semantic research files."""

from __future__ import annotations

import os
import re
import stat
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, cast

import yaml


_open = os.open
_DIRECTORY_OPEN_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_OPEN_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_QUESTION_PATH = re.compile(r"questions/(Q-[^/]+)/question\.md")
_CLAIM_PATH = re.compile(r"knowledge/claims/(C-[^/]+)\.md")
_IDEA_PATH = re.compile(r"ideas/(I-[^/]+)\.md")
_ATX_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(.+?)[ \t]*$")
_FENCE_START = re.compile(r"^[ ]{0,3}(`{3,}|~{3,})")
MAX_FRONTMATTER_DEPTH = 64
MAX_FRONTMATTER_NODES = 10_000

_QUESTION_SECTIONS = (
    "Question",
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
)
_CLAIM_SECTIONS = (
    "Claim",
    "Statement and scope",
    "Evidence",
    "Counterevidence",
    "Assumptions",
    "Uncertainty and alternatives",
    "Consequences",
)


class ResearchFileError(ValueError):
    """A semantic file is unsafe or mechanically ambiguous."""


class ResearchFileStructureError(ResearchFileError):
    """Semantic frontmatter exceeds explicit structural bounds."""


@dataclass(frozen=True)
class SemanticDocument:
    path: str
    identifier: str | None
    frontmatter: Mapping[str, object]
    sections: Mapping[str, str]
    warnings: tuple[str, ...]


class _UniqueSafeLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate mapping keys at every depth."""

    def construct_mapping(
        self,
        node: yaml.MappingNode,
        deep: bool = False,
    ) -> dict[object, object]:
        self.flatten_mapping(node)
        mapping: dict[object, object] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in mapping
            except TypeError as error:
                raise yaml.constructor.ConstructorError(
                    "while constructing frontmatter",
                    node.start_mark,
                    "frontmatter keys must be hashable",
                    key_node.start_mark,
                ) from error
            if duplicate:
                raise yaml.constructor.ConstructorError(
                    "while constructing frontmatter",
                    node.start_mark,
                    f"duplicate frontmatter key: {key}",
                    key_node.start_mark,
                )
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


def read_semantic_document(root: Path, relative: str) -> SemanticDocument:
    """Read one contained ordinary UTF-8 semantic document."""
    normalized_relative = _normalized_relative(relative)
    raw = _read_ordinary_contained_file(root, normalized_relative)
    return parse_semantic_document_bytes(normalized_relative, raw)


def parse_semantic_document_bytes(
    relative_path: str,
    raw: bytes,
) -> SemanticDocument:
    """Parse one exact semantic file payload without filesystem access."""
    normalized_relative = _normalized_relative(relative_path)
    if not isinstance(raw, bytes):
        raise ResearchFileError("semantic content must be bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ResearchFileError(
            f"semantic file is not UTF-8: {normalized_relative}"
        ) from error

    frontmatter, body = _split_frontmatter(text, normalized_relative)
    identifier = _validate_navigation_identity(normalized_relative, frontmatter)
    sections = _sections(body, normalized_relative)
    warnings = tuple(
        f"missing recommended section: {heading}"
        for heading in _recommended_sections(normalized_relative)
        if heading not in sections
    )
    return SemanticDocument(
        path=normalized_relative,
        identifier=identifier,
        frontmatter=MappingProxyType(dict(frontmatter)),
        sections=MappingProxyType(dict(sections)),
        warnings=warnings,
    )


def _normalized_relative(relative: str) -> str:
    if not isinstance(relative, str) or not relative:
        raise ResearchFileError("semantic path must name a contained file")
    relative_path = Path(relative)
    if (
        relative_path.is_absolute()
        or not relative_path.parts
        or ".." in relative_path.parts
    ):
        raise ResearchFileError(f"semantic path must be contained: {relative}")
    return relative_path.as_posix()


def _read_ordinary_contained_file(root: Path, relative: str) -> bytes:
    parts = Path(relative).parts
    workspace = Path(root).expanduser()
    with ExitStack() as descriptors:
        try:
            directory_fd = _open(workspace, _DIRECTORY_OPEN_FLAGS)
        except OSError as error:
            raise ResearchFileError(
                f"semantic root must be an ordinary directory without symlinks: {root}"
            ) from error
        descriptors.callback(os.close, directory_fd)

        for component in parts[:-1]:
            try:
                directory_fd = _open(
                    component,
                    _DIRECTORY_OPEN_FLAGS,
                    dir_fd=directory_fd,
                )
            except OSError as error:
                raise ResearchFileError(
                    f"semantic path must stay contained and contain no symlink: {relative}"
                ) from error
            descriptors.callback(os.close, directory_fd)

        try:
            file_fd = _open(parts[-1], _FILE_OPEN_FLAGS, dir_fd=directory_fd)
        except OSError as error:
            raise ResearchFileError(
                f"semantic path must stay contained and contain no symlink: {relative}"
            ) from error
        descriptors.callback(os.close, file_fd)

        try:
            metadata = os.fstat(file_fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise ResearchFileError(
                    f"semantic path must be a regular file: {relative}"
                )
            with os.fdopen(file_fd, "rb", closefd=False) as handle:
                return handle.read()
        except OSError as error:
            raise ResearchFileError(f"semantic file could not be read: {relative}") from error

def _split_frontmatter(
    text: str,
    relative: str,
) -> tuple[dict[str, object], str]:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, text

    closing = next(
        (index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"),
        None,
    )
    if closing is None:
        raise ResearchFileError(f"unterminated frontmatter: {relative}")

    try:
        loaded = yaml.load("".join(lines[1:closing]), Loader=_UniqueSafeLoader)
    except RecursionError as error:
        raise ResearchFileStructureError(
            f"frontmatter recursion limit exceeded: {relative}"
        ) from error
    except (TypeError, ValueError, yaml.YAMLError) as error:
        raise ResearchFileError(f"invalid frontmatter in {relative}: {error}") from error
    if loaded is None:
        frontmatter: dict[object, object] = {}
    elif isinstance(loaded, dict):
        frontmatter = loaded
    else:
        raise ResearchFileError(f"frontmatter must be a mapping: {relative}")
    _validate_frontmatter_shape(frontmatter, relative)
    if any(not isinstance(key, str) for key in frontmatter):
        raise ResearchFileError(f"frontmatter keys must be strings: {relative}")
    return cast(dict[str, object], frontmatter), "".join(lines[closing + 1 :])


def _validate_frontmatter_shape(value: object, relative: str) -> None:
    pending: list[tuple[object, int]] = [(value, 1)]
    seen_containers: set[int] = set()
    nodes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if nodes > MAX_FRONTMATTER_NODES:
            raise ResearchFileStructureError(
                f"frontmatter nodes exceed {MAX_FRONTMATTER_NODES}: {relative}"
            )
        if depth > MAX_FRONTMATTER_DEPTH:
            raise ResearchFileStructureError(
                f"frontmatter depth exceeds {MAX_FRONTMATTER_DEPTH}: {relative}"
            )
        if isinstance(item, Mapping):
            identity = id(item)
            if identity in seen_containers:
                raise ResearchFileStructureError(
                    f"frontmatter contains recursive or aliased containers: {relative}"
                )
            seen_containers.add(identity)
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, (list, tuple, set)):
            identity = id(item)
            if identity in seen_containers:
                raise ResearchFileStructureError(
                    f"frontmatter contains recursive or aliased containers: {relative}"
                )
            seen_containers.add(identity)
            pending.extend((child, depth + 1) for child in item)


def _validate_navigation_identity(
    relative: str,
    frontmatter: Mapping[str, object],
) -> str | None:
    expected: str | None = None
    for pattern in (_QUESTION_PATH, _CLAIM_PATH, _IDEA_PATH):
        match = pattern.fullmatch(relative)
        if match is not None:
            expected = match.group(1)
            break

    identifier = frontmatter.get("id")
    if expected is not None:
        if identifier != expected:
            raise ResearchFileError(
                f"semantic identifier must match path: expected {expected}, got {identifier!r}"
            )
        return expected
    if identifier is None:
        return None
    if not isinstance(identifier, str) or not identifier.strip():
        raise ResearchFileError(f"semantic identifier must be a non-empty string: {relative}")
    return identifier


def _sections(body: str, relative: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    heading: str | None = None
    content: list[str] = []
    fence: tuple[str, int] | None = None

    def finish() -> None:
        if heading is not None:
            sections[heading] = "\n".join(content).strip()

    for line in body.splitlines():
        if fence is not None:
            content.append(line)
            if _closes_fence(line, *fence):
                fence = None
            continue

        fence_match = _FENCE_START.match(line)
        if fence_match is not None:
            marker = fence_match.group(1)
            fence = (marker[0], len(marker))
            if heading is not None:
                content.append(line)
            continue

        match = _ATX_HEADING.fullmatch(line)
        if match is None:
            if heading is not None:
                content.append(line)
            continue
        finish()
        next_heading = re.sub(r"[ \t]+#+[ \t]*$", "", match.group(1)).strip()
        if next_heading in sections:
            raise ResearchFileError(
                f"duplicate Markdown heading in {relative}: {next_heading}"
            )
        heading = next_heading
        content = []

    finish()
    return sections


def _closes_fence(line: str, marker: str, minimum: int) -> bool:
    stripped = line.lstrip(" ")
    if len(line) - len(stripped) > 3 or not stripped.startswith(marker * minimum):
        return False
    run = len(stripped) - len(stripped.lstrip(marker))
    return run >= minimum and not stripped[run:].strip()


def _recommended_sections(relative: str) -> tuple[str, ...]:
    if _QUESTION_PATH.fullmatch(relative):
        return _QUESTION_SECTIONS
    if _CLAIM_PATH.fullmatch(relative):
        return _CLAIM_SECTIONS
    return ()


__all__ = [
    "MAX_FRONTMATTER_DEPTH",
    "MAX_FRONTMATTER_NODES",
    "ResearchFileError",
    "ResearchFileStructureError",
    "SemanticDocument",
    "parse_semantic_document_bytes",
    "read_semantic_document",
]
