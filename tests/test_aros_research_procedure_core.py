from __future__ import annotations

import importlib
import json
import re
import subprocess
from pathlib import Path, PurePosixPath

import pytest


ROOT = Path(__file__).resolve().parent.parent
PROGRAM_ROOT = ROOT / "commissioning/research_program"
SOURCES_PATH = PROGRAM_ROOT / "SOURCES.json"
UPSTREAM_PRODUCT_NAMES = ("claude", "gemini")
SOURCE_RECORD_SUFFIXES = {".json", ".jsonl", ".yaml", ".yml", ".md"}
SOURCE_RECORD_EXEMPTIONS = {
    "SOURCES.json",
    "procedures/aros-source-research.md",
}
RUNTIME_SOURCE_SUFFIXES = {".json", ".md", ".py"}
RUNTIME_ARTIFACT_PARTS = {"__pycache__", "build"}
APPROVED_SOURCE_RECORD = {
    "schema_version": 1,
    "sources": [
        {
            "id": "source-1",
            "repository": "/workspace/Auto-claude-code-research-in-sleep",
            "commit": "df729a3f942e4a97646d212eb8aee1144ab5e31b",
            "license": "MIT",
            "selected_paths": [
                "skills/research-lit/SKILL.md",
                "skills/novelty-check/SKILL.md",
                "skills/citation-audit/SKILL.md",
                "skills/idea-creator/SKILL.md",
                "skills/research-refine/SKILL.md",
                "skills/experiment-plan/SKILL.md",
                "skills/ablation-planner/SKILL.md",
                "skills/analyze-results/SKILL.md",
                "skills/research-wiki/SKILL.md",
                "skills/research-review/SKILL.md",
                "skills/experiment-audit/SKILL.md",
                "skills/integrity-forensics/SKILL.md",
                "skills/result-to-claim/SKILL.md",
                "skills/claims-drafting/SKILL.md",
                "skills/shared-references/external-cadence.md",
                "skills/shared-references/reviewer-independence.md",
                "mcp-servers/claude-review/server.py",
                "mcp-servers/gemini-review/server.py",
                "mcp-servers/manual-review/server.py",
            ],
            "adaptation": (
                "Distill scientific procedures, durable recovery, cadence, and fresh "
                "review; remove scoring, paper production, remote execution, and "
                "duplicate orchestration."
            ),
        },
        {
            "id": "source-2",
            "repository": (
                "/workspace/Arbor/.worktree/aros-long-running-research-program-design"
            ),
            "commit": "e9c58c998767dd87bdea99a727533819850ac281",
            "license": "Apache-2.0",
            "selected_paths": [
                "skills/arbor-agent-setup-intake/SKILL.md",
                "skills/arbor-agent-ideate/SKILL.md",
                "skills/arbor-agent-executor/SKILL.md",
                "skills/arbor-agent-search/SKILL.md",
                "skills/arbor-agent-resume-report/SKILL.md",
                "skills/arbor-agent-tools/SKILL.md",
                "src/mcp/server.py",
                "src/mcp/session_ops.py",
            ],
            "adaptation": (
                "Distill mechanism framing, deterministic tool boundaries, search, and "
                "durable handoff; remove tree authority, scalar evaluation, merge gates, "
                "and duplicate session state."
            ),
        },
    ],
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


def _assert_approved_source_record(record: dict[str, object]) -> None:
    assert record == APPROVED_SOURCE_RECORD


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _assert_commit_object(repository: Path, commit: object) -> None:
    assert isinstance(commit, str)
    assert re.fullmatch(r"[0-9a-f]{40}", commit)
    assert _git(repository, "rev-parse", commit) == commit
    assert _git(repository, "cat-file", "-t", commit) == "commit", (
        "source object must be a commit"
    )


def _is_source_or_provenance_record(program_root: Path, path: Path) -> bool:
    relative = path.relative_to(program_root)
    if path.suffix.casefold() not in SOURCE_RECORD_SUFFIXES:
        return False
    if relative.as_posix() in SOURCE_RECORD_EXEMPTIONS:
        return False
    candidates = (*relative.parts[:-1], path.stem)
    for candidate in candidates:
        tokens = {
            token for token in re.split(r"[^a-z0-9]+", candidate.casefold()) if token
        }
        if tokens & {"source", "sources", "provenance"}:
            return True
    return False


def _assert_sole_source_or_provenance_record(
    program_root: Path, sources_path: Path
) -> None:
    assert sources_path.relative_to(program_root).as_posix() == "SOURCES.json"
    assert sources_path.is_file()
    records = {
        path
        for path in program_root.rglob("*")
        if path.is_file() and _is_source_or_provenance_record(program_root, path)
    }
    assert not records, f"unexpected source or provenance record: {records}"


def _assert_no_upstream_product_names(program_root: Path, sources_path: Path) -> None:
    for path in program_root.rglob("*"):
        relative = path.relative_to(program_root)
        if not path.is_file() or path.suffix.casefold() not in RUNTIME_SOURCE_SUFFIXES:
            continue
        folded_parts = tuple(part.casefold() for part in relative.parts)
        if any(part.startswith(".") for part in relative.parts):
            continue
        if any(part in RUNTIME_ARTIFACT_PARTS for part in folded_parts):
            continue
        relative_name = relative.as_posix().casefold()
        for upstream_name in UPSTREAM_PRODUCT_NAMES:
            assert upstream_name not in relative_name, (
                f"upstream product name {upstream_name!r} in path {path}"
            )
        if path != sources_path:
            content = path.read_bytes().lower()
            for upstream_name in UPSTREAM_PRODUCT_NAMES:
                assert upstream_name.encode() not in content, (
                    f"upstream product name {upstream_name!r} in {path}"
                )


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
    _assert_approved_source_record(record)
    sources = record["sources"]
    assert isinstance(sources, list)

    for source in sources:
        assert isinstance(source, dict)
        adaptation = source["adaptation"]
        assert isinstance(adaptation, str)
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
        _assert_commit_object(repository, commit)

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
            assert (
                _git(repository, "cat-file", "-t", f"{commit}:{selected_path}")
                == "blob"
            )


def test_sources_json_is_the_sole_source_or_provenance_record() -> None:
    _assert_sole_source_or_provenance_record(PROGRAM_ROOT, SOURCES_PATH)


def test_source_runtime_names_do_not_reuse_upstream_product_names() -> None:
    _assert_no_upstream_product_names(PROGRAM_ROOT, SOURCES_PATH)


def test_source_record_scan_allows_exact_scientific_source_research(
    tmp_path: Path,
) -> None:
    program_root = tmp_path / "research_program"
    sources_path = program_root / "SOURCES.json"
    procedure = program_root / "procedures/aros-source-research.md"
    procedure.parent.mkdir(parents=True)
    sources_path.write_text("{}\n", encoding="utf-8")
    procedure.write_text(
        "# Source research\n\nScientific source and provenance analysis.\n",
        encoding="utf-8",
    )

    _assert_sole_source_or_provenance_record(program_root, sources_path)


@pytest.mark.parametrize(
    "relative_path",
    [
        "provenance.json",
        "source-record.json",
        "provenance/record.json",
        "source-record/record.json",
        "duplicate/SOURCES.json",
        "source.json",
        "SOURCES-v2.JSONL",
        "source/record.YAML",
        "Provenance-notes.YML",
        "PROVENANCE.MD",
        "research-provenance.json",
        "upstream-source-record.json",
        "nested/record-sources.yaml",
    ],
)
def test_source_record_scan_rejects_second_record_location(
    tmp_path: Path, relative_path: str
) -> None:
    program_root = tmp_path / "research_program"
    sources_path = program_root / "SOURCES.json"
    second_record = program_root / relative_path
    second_record.parent.mkdir(parents=True, exist_ok=True)
    sources_path.write_text("{}\n", encoding="utf-8")
    second_record.write_text("{}\n", encoding="utf-8")

    with pytest.raises(AssertionError, match="source or provenance record"):
        _assert_sole_source_or_provenance_record(program_root, sources_path)


def test_source_record_scan_ignores_scientific_content(tmp_path: Path) -> None:
    program_root = tmp_path / "research_program"
    sources_path = program_root / "SOURCES.json"
    notes = program_root / "procedures/resource-analysis.md"
    notes.parent.mkdir(parents=True)
    sources_path.write_text("{}\n", encoding="utf-8")
    notes.write_text(
        "Analyze scientific sources and provenance evidence.\n", encoding="utf-8"
    )

    _assert_sole_source_or_provenance_record(program_root, sources_path)


@pytest.mark.parametrize("upstream_name", UPSTREAM_PRODUCT_NAMES)
@pytest.mark.parametrize("placement", ["filename", "content"])
def test_source_runtime_name_scan_rejects_filename_and_content(
    tmp_path: Path, upstream_name: str, placement: str
) -> None:
    program_root = tmp_path / "research_program"
    program_root.mkdir()
    sources_path = program_root / "SOURCES.json"
    sources_path.write_text("{}\n", encoding="utf-8")
    if placement == "filename":
        bad_path = program_root / f"adapter-{upstream_name.upper()}.py"
        bad_path.write_text("pass\n", encoding="utf-8")
    else:
        bad_path = program_root / "adapter.py"
        bad_path.write_text(f"UPSTREAM = {upstream_name.upper()!r}\n", encoding="utf-8")

    with pytest.raises(AssertionError, match=upstream_name):
        _assert_no_upstream_product_names(program_root, sources_path)


def test_source_runtime_name_scan_allows_sources_json_bytes(tmp_path: Path) -> None:
    program_root = tmp_path / "research_program"
    program_root.mkdir()
    sources_path = program_root / "SOURCES.json"
    sources_path.write_text("ClAuDe and GeMiNi\n", encoding="utf-8")

    _assert_no_upstream_product_names(program_root, sources_path)


@pytest.mark.parametrize(
    "relative_path",
    ["__pycache__/cache.pyc", ".hidden.py", "build/generated.py"],
)
def test_source_runtime_name_scan_ignores_artifacts(
    tmp_path: Path, relative_path: str
) -> None:
    program_root = tmp_path / "research_program"
    sources_path = program_root / "SOURCES.json"
    artifact = program_root / relative_path
    artifact.parent.mkdir(parents=True, exist_ok=True)
    sources_path.write_text("{}\n", encoding="utf-8")
    artifact.write_bytes(b"CLAUDE and GEMINI\n")

    _assert_no_upstream_product_names(program_root, sources_path)


def test_source_approved_record_rejects_value_drift() -> None:
    record = _load_source_record()
    sources = record["sources"]
    assert isinstance(sources, list)
    sources[0]["repository"] = "/unapproved/repository"

    with pytest.raises(AssertionError):
        _assert_approved_source_record(record)


def test_source_commit_check_rejects_tree_oid() -> None:
    tree_oid = _git(ROOT, "rev-parse", "HEAD^{tree}")
    assert re.fullmatch(r"[0-9a-f]{40}", tree_oid)

    with pytest.raises(AssertionError, match="commit"):
        _assert_commit_object(ROOT, tree_oid)
