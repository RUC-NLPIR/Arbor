from __future__ import annotations

import ast
import importlib
import json
import re
import subprocess
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parent.parent
PROGRAM_ROOT = ROOT / "commissioning/research_program"
SOURCES_PATH = PROGRAM_ROOT / "SOURCES.json"
SOURCE_KEYS = {
    "id",
    "repository",
    "commit",
    "license",
    "selected_paths",
    "adaptation",
}
EXPECTED_LICENSES = {
    "source-1": "MIT",
    "source-2": "Apache-2.0",
}
EXPECTED_ADAPTATIONS = {
    "source-1": (
        "Distill scientific procedures, durable recovery, cadence, and fresh review; "
        "remove scoring, paper production, remote execution, and duplicate orchestration."
    ),
    "source-2": (
        "Distill mechanism framing, deterministic tool boundaries, search, and durable "
        "handoff; remove tree authority, scalar evaluation, merge gates, and duplicate "
        "session state."
    ),
}


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise AssertionError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _load_source_record() -> dict[str, object]:
    value = json.loads(
        SOURCES_PATH.read_text(encoding="utf-8"),
        object_pairs_hook=_unique_json_object,
    )
    assert isinstance(value, dict)
    return value


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _defined_python_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
    return names


def test_source_schema_versions_are_exact_int_one() -> None:
    record = _load_source_record()
    assert set(record) == {"schema_version", "sources"}
    assert type(record["schema_version"]) is int
    assert record["schema_version"] == 1

    package = importlib.import_module("commissioning.research_program")
    assert type(package.SCHEMA_VERSION) is int
    assert package.SCHEMA_VERSION == 1


def test_source_record_binds_exact_sources_and_adaptations() -> None:
    record = _load_source_record()
    sources = record["sources"]
    assert isinstance(sources, list)
    assert [source["id"] for source in sources] == ["source-1", "source-2"]

    for source in sources:
        assert isinstance(source, dict)
        assert set(source) == SOURCE_KEYS
        source_id = source["id"]
        assert source["license"] == EXPECTED_LICENSES[source_id]
        adaptation = source["adaptation"]
        assert isinstance(adaptation, str)
        assert adaptation == EXPECTED_ADAPTATIONS[source_id]
        assert adaptation == adaptation.strip()
        assert 1 <= len(adaptation) <= 256


def test_source_repositories_commits_and_selected_paths_are_real() -> None:
    sources = _load_source_record()["sources"]
    assert isinstance(sources, list)

    for source in sources:
        repository_value = source["repository"]
        assert isinstance(repository_value, str)
        repository = Path(repository_value)
        assert repository.is_absolute()
        assert repository.is_dir()
        assert _git(repository, "rev-parse", "--is-inside-work-tree") == "true"
        git_directory = Path(
            _git(repository, "rev-parse", "--path-format=absolute", "--git-dir")
        )
        assert git_directory.is_absolute()
        assert git_directory.is_dir()

        commit = source["commit"]
        assert isinstance(commit, str)
        assert re.fullmatch(r"[0-9a-f]{40}", commit)
        assert _git(repository, "rev-parse", commit) == commit

        selected_paths = source["selected_paths"]
        assert isinstance(selected_paths, list)
        assert selected_paths
        assert all(isinstance(path, str) for path in selected_paths)
        assert len(selected_paths) == len(set(selected_paths))
        for selected_path in selected_paths:
            path = PurePosixPath(selected_path)
            assert selected_path == path.as_posix()
            assert not path.is_absolute()
            assert path.parts
            assert all(part not in {"", ".", ".."} for part in path.parts)
            assert "\\" not in selected_path
            assert "\x00" not in selected_path
            assert _git(repository, "cat-file", "-t", f"{commit}:{selected_path}") == "blob"


def test_sources_json_is_the_sole_source_or_provenance_record() -> None:
    records = {
        path
        for path in PROGRAM_ROOT.rglob("*")
        if path.is_file()
        and any(label in path.stem.casefold() for label in ("source", "provenance"))
    }
    assert records == {SOURCES_PATH}


def test_source_runtime_names_do_not_reuse_upstream_product_names() -> None:
    sources = _load_source_record()["sources"]
    assert isinstance(sources, list)
    upstream_names = {
        path.parent.name.removesuffix("-review").casefold()
        for source in sources
        for selected_path in source["selected_paths"]
        for path in (PurePosixPath(selected_path),)
        if path.name == "server.py"
        and path.parent.name.endswith("-review")
        and path.parent.name != "manual-review"
    }
    assert len(upstream_names) == 2

    component_names = {
        component
        for path in PROGRAM_ROOT.rglob("*")
        if path != SOURCES_PATH
        for component in path.relative_to(PROGRAM_ROOT).parts
    }
    runtime_names = set(component_names)
    for path in PROGRAM_ROOT.rglob("*.py"):
        runtime_names.update(_defined_python_names(path))

    for runtime_name in runtime_names:
        folded = runtime_name.casefold()
        assert all(upstream_name not in folded for upstream_name in upstream_names)
