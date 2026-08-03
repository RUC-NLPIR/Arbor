"""Integrity checks for the repository documentation registry."""

from __future__ import annotations

import json
import runpy
from pathlib import Path

import yaml


_ROOT = Path(__file__).resolve().parent.parent
_REGISTRY = _ROOT / "docs" / "document_registry.json"
_FROZEN_ROOTS = tuple(
    runpy.run_path(str(_ROOT / "scripts" / "check_aros_legacy_freeze.py"))[
        "FROZEN_ROOTS"
    ]
)
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


class _MkDocsLoader(yaml.SafeLoader):
    pass


_MkDocsLoader.add_multi_constructor(
    "tag:yaml.org,2002:python/name:",
    lambda _loader, suffix, _node: suffix,
)


def _load_registry() -> dict:
    return json.loads(_REGISTRY.read_text(encoding="utf-8"))


def _load_mkdocs_config() -> dict:
    return yaml.load(
        (_ROOT / "mkdocs.yml").read_text(encoding="utf-8"),
        Loader=_MkDocsLoader,
    )


def _nav_paths(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [path for item in value for path in _nav_paths(item)]
    if isinstance(value, dict):
        return [path for item in value.values() for path in _nav_paths(item)]
    return []


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


def test_registry_contains_approved_aros_v1_design() -> None:
    by_id = {document["id"]: document for document in _load_registry()["documents"]}

    assert by_id["aros-v1-product-migration-design"] == {
        "id": "aros-v1-product-migration-design",
        "title": "AROS v1 Product, Architecture, and Arbor Migration Design",
        "path": (
            "docs/superpowers/specs/"
            "2026-08-02-aros-v1-product-and-migration-design.md"
        ),
        "status": "current",
        "authority": "implementation_baseline",
        "agent_visibility": "on_demand",
    }


def test_registry_contains_current_wave3_eval_design() -> None:
    by_id = {document["id"]: document for document in _load_registry()["documents"]}

    assert by_id["aros-wave3-eval-design"] == {
        "id": "aros-wave3-eval-design",
        "title": "AROS Wave 3 Evaluation Design",
        "path": "docs/superpowers/specs/2026-08-03-aros-wave3-eval-design.md",
        "status": "current",
        "authority": "implementation_baseline",
        "agent_visibility": "on_demand",
    }


def test_registry_contains_current_public_aros_guide() -> None:
    by_id = {document["id"]: document for document in _load_registry()["documents"]}

    assert by_id["aros-public-guide"] == {
        "id": "aros-public-guide",
        "title": "AROS Public Guide",
        "path": "docs/aros/README.md",
        "status": "current",
        "authority": "informative",
        "agent_visibility": "on_demand",
    }


def test_current_default_baseline_uses_direct_aros_entry() -> None:
    by_id = {document["id"]: document for document in _load_registry()["documents"]}
    baseline_path = _ROOT / by_id["aros-implementation-baseline"]["path"]
    baseline = baseline_path.read_text(encoding="utf-8")

    assert "新原生入口直接使用 `aros`" in baseline
    assert "`arbor aros` 仅作为临时转发兼容入口" in baseline


def test_aros_public_docs_use_direct_entry() -> None:
    text = (_ROOT / "docs" / "aros" / "README.md").read_text(encoding="utf-8")

    for command in (
        "aros init",
        "aros boot",
        "aros status",
        "aros start",
        "aros run start|status|list|tail|stop",
        "aros task create|start|status|list|message|stop|collect|preserve|prune",
    ):
        assert command in text
    assert "`arbor aros` is a temporary forwarding compatibility route" in text
    assert "## Not yet implemented" in text
    for unavailable_capability in (
        "deterministic/protected evaluation",
        "migration adapters",
        "MCP parity",
        "Arbor retirement",
    ):
        assert unavailable_capability in text
    assert "capabilities_enforced=false" in text
    assert "filesystem_permissions_enforced=false" in text


def test_aros_public_guide_states_runtime_prerequisites() -> None:
    text = (_ROOT / "docs" / "aros" / "README.md").read_text(encoding="utf-8")

    assert "exposed command surface" in text
    assert "clean committed Git HEAD" in text
    assert "`tmux`" in text
    assert "supported Linux architecture (x86_64 or aarch64)" in text
    assert "exactly Landlock ABI 4" in text
    assert "`libseccomp`" in text
    assert (
        "Task adapters are trusted-local and application-scoped, not a security sandbox"
        in text
    )


def test_task_docs_publish_the_exact_v1_containment_claim() -> None:
    paths = (
        _ROOT / "docs" / "aros" / "README.md",
        _ROOT
        / "docs"
        / "superpowers"
        / "specs"
        / "2026-08-02-aros-v1-product-and-migration-design.md",
        _ROOT / "docs" / "analysis" / "aros-wave2-child-substrate-smoke.md",
    )

    for path in paths:
        text = " ".join(path.read_text(encoding="utf-8").split())
        assert (
            "V1 terminal truth covers the exact PGID plus descendants reparented to "
            "the live subreaper."
        ) in text
        assert (
            "A new-session process that outlives runner death is not claimed contained "
            "and cannot justify a clean final receipt or prune."
        ) in text
        assert (
            "Delegated per-task cgroups belong to the shared Operations process core, "
            "not the Wave 2 security claim."
        ) in text


def test_aros_migration_docs_match_the_ci_freeze_scope() -> None:
    root_readme = (_ROOT / "README.md").read_text(encoding="utf-8")
    public_guide = (_ROOT / "docs" / "aros" / "README.md").read_text(
        encoding="utf-8"
    )

    for text in (root_readme, public_guide):
        for frozen_root in _FROZEN_ROOTS:
            assert f"`{frozen_root}`" in text
        assert "Other `arbor` commands remain legacy implementations until migrated." in text
        assert "Existing Arbor research commands remain frozen" not in text
        assert "frozen compatibility paths" not in text


def test_aros_public_docs_route_and_describe_migration() -> None:
    root_readme = (_ROOT / "README.md").read_text(encoding="utf-8")
    docs_readme = (_ROOT / "docs" / "README.md").read_text(encoding="utf-8")

    assert "direct entry for\n> bootable workspaces and durable runs" in root_readme
    assert "[AROS public guide](docs/aros/README.md)" in root_readme
    assert "[aros/README.md](aros/README.md)" in docs_readme


def test_mkdocs_nav_includes_registered_public_aros_guide() -> None:
    by_id = {document["id"]: document for document in _load_registry()["documents"]}
    guide_path = by_id["aros-public-guide"]["path"]
    config = _load_mkdocs_config()

    assert guide_path.removeprefix("docs/") in _nav_paths(config["nav"])
