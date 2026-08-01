"""Integrity checks for the repository documentation registry."""

from __future__ import annotations

import json
from pathlib import Path


_ROOT = Path(__file__).resolve().parent.parent
_REGISTRY = _ROOT / "docs" / "document_registry.json"
_ENTRY_FIELDS = {
    "id",
    "title",
    "path",
    "status",
    "authority",
    "agent_visibility",
}
_STATUSES = {"current", "proposed", "historical"}
_AUTHORITIES = {
    "implementation_baseline",
    "target_specification",
    "compatibility",
    "informative",
}
_VISIBILITIES = {"default", "on_demand"}


def _load_registry() -> dict:
    return json.loads(_REGISTRY.read_text(encoding="utf-8"))


def test_document_registry_has_supported_schema_and_enums() -> None:
    registry = _load_registry()

    assert set(registry) == {"schema_version", "documents"}
    assert registry["schema_version"] == 1
    assert isinstance(registry["documents"], list)
    assert registry["documents"]

    for document in registry["documents"]:
        assert set(document) == _ENTRY_FIELDS
        assert all(isinstance(document[field], str) and document[field].strip() for field in _ENTRY_FIELDS)
        assert document["status"] in _STATUSES
        assert document["authority"] in _AUTHORITIES
        assert document["agent_visibility"] in _VISIBILITIES


def test_document_registry_ids_and_paths_are_unique_and_exist() -> None:
    documents = _load_registry()["documents"]
    ids = [document["id"] for document in documents]
    paths = [document["path"] for document in documents]

    assert len(ids) == len(set(ids))
    assert len(paths) == len(set(paths))

    for registered_path in paths:
        relative_path = Path(registered_path)
        assert not relative_path.is_absolute()
        assert ".." not in relative_path.parts
        target = (_ROOT / relative_path).resolve()
        target.relative_to(_ROOT.resolve())
        assert target.is_file()


def test_document_registry_has_one_current_default() -> None:
    documents = _load_registry()["documents"]
    current_default = [
        document
        for document in documents
        if document["status"] == "current" and document["agent_visibility"] == "default"
    ]

    assert [document["id"] for document in current_default] == ["aros-implementation-baseline"]


def test_document_registry_identifies_the_design_book_as_target_specification() -> None:
    target_specifications = [
        document
        for document in _load_registry()["documents"]
        if document["authority"] == "target_specification"
    ]

    assert [document["id"] for document in target_specifications] == ["aros-design-book-v1-0-zh"]
    assert target_specifications[0]["status"] == "current"
