from __future__ import annotations

import ast
import importlib
import json
import os
import re
import signal
import subprocess
import time
from dataclasses import FrozenInstanceError
from pathlib import Path, PurePosixPath
from types import MappingProxyType

import pytest


ROOT = Path(__file__).resolve().parent.parent
PROGRAM_ROOT = ROOT / "commissioning/research_program"
SOURCES_PATH = PROGRAM_ROOT / "SOURCES.json"
CONTRACTS_PATH = PROGRAM_ROOT / "contracts/procedure_contracts.json"
PROCEDURES_ROOT = PROGRAM_ROOT / "procedures"
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
EXPECTED_ALLOWED_TOOLS = (
    "Source.read",
    "Source.search",
    "Task.create",
    "Task.start",
    "Task.status",
    "Task.collect",
    "Run.request",
    "Run.status",
    "Eval.run",
    "Receipt.read",
    "Research.observe",
    "Research.checkpoint",
    "Research.petition",
    "Git.read",
)
EXPECTED_ARTIFACTS = {
    "ResearchQuestion": ("question_ref", "scope", "decision_context"),
    "SourcePacket": (
        "query",
        "question_ref",
        "sources",
        "retrieved_at",
        "content_refs",
        "content_sha256s",
        "limitations",
    ),
    "RivalMechanismSet": (
        "root_question_ref",
        "mechanisms",
        "predictions",
        "falsifiers",
        "conflicts",
        "remaining_uncertainty",
    ),
    "ExperimentProposal": (
        "mechanism_refs",
        "decision_uncertainty",
        "prediction",
        "falsifier",
        "controls",
        "run_request",
        "expected_information_gain",
        "cost_bound",
    ),
    "RunEvidence": (
        "run_ref",
        "eval_refs",
        "raw_refs",
        "process_state",
        "budget_used",
    ),
    "ObservationUpdate": (
        "evidence_refs",
        "strengthened",
        "weakened",
        "eliminated",
        "counterexamples",
        "negative_results",
        "remaining_uncertainty",
        "next_action_rationale",
    ),
    "Preregistration": (
        "mechanism_hypothesis",
        "key_predictions",
        "falsifiers",
        "controls",
        "primary_comparisons",
        "transfer_prediction",
        "stopping_rules",
        "evaluator_version",
    ),
    "FrozenEvidencePacket": (
        "task_brief_ref",
        "preregistration_ref",
        "commit",
        "source_refs",
        "raw_refs",
        "reproduction_ref",
    ),
    "ReviewerReport": (
        "reproduction_refs",
        "alternative_explanations",
        "leakage_findings",
        "statistical_findings",
        "scope_objections",
        "fatal_objections",
        "unresolved_objections",
    ),
    "AdjudicatedEvidence": (
        "claim_draft_ref",
        "evidence_refs",
        "review_ref",
        "principal_response_ref",
    ),
    "ClaimPackage": (
        "claim",
        "scope",
        "evidence_refs",
        "counterevidence",
        "reproduction_commands",
        "limitations",
        "remaining_uncertainty",
        "review_objections",
    ),
}
EXPECTED_PROCEDURES = {
    "aros-source-research": (
        "ResearchQuestion",
        "SourcePacket",
        ("Source.read", "Source.search"),
    ),
    "aros-rival-mechanisms": (
        "SourcePacket",
        "RivalMechanismSet",
        ("Git.read", "Receipt.read", "Research.observe", "Research.petition"),
    ),
    "aros-experiment-design": (
        "RivalMechanismSet",
        "ExperimentProposal",
        ("Receipt.read", "Research.observe", "Research.petition"),
    ),
    "aros-evidence-update": (
        "RunEvidence",
        "ObservationUpdate",
        (
            "Run.status",
            "Eval.run",
            "Receipt.read",
            "Research.observe",
            "Research.checkpoint",
        ),
    ),
    "aros-independent-review": (
        "FrozenEvidencePacket",
        "ReviewerReport",
        (
            "Source.read",
            "Run.request",
            "Run.status",
            "Eval.run",
            "Receipt.read",
            "Git.read",
        ),
    ),
    "aros-claim-package": (
        "AdjudicatedEvidence",
        "ClaimPackage",
        ("Source.read", "Receipt.read", "Git.read", "Research.checkpoint"),
    ),
}
EXPECTED_PROCEDURE_HEADINGS = (
    "Purpose",
    "Inputs",
    "Method",
    "Output",
    "Completion",
    "Forbidden",
)
EXPECTED_SOURCE_METHOD_RULES = (
    (
        "Prefer primary sources; use secondary sources only to locate or "
        "contextualize primary evidence, and label them as secondary."
    ),
    (
        "Query multiple independent sources and preserve the exact query log, "
        "including every query formulation and retrieval result."
    ),
    (
        "Retain dead ends and contradictions, including unsuccessful queries and "
        "evidence that disagrees with another source."
    ),
    (
        "For every source used, bind its opaque source id, retrieval time, content "
        "reference, and SHA-256 content hash; keep citations traceable to those "
        "bindings."
    ),
    (
        "Treat novelty findings as evidence only, never as a scientific verdict; "
        "report what the search did and did not establish."
    ),
    "State search, access, coverage, and source-quality limitations explicitly.",
)
EXPECTED_SOURCE_FORBIDDEN_RULES = (
    "Do not download experimental data.",
    "Do not fabricate citations.",
    "Do not write to external systems or perform any external write.",
    "Do not make scientific acceptance decisions.",
    "Do not issue scientific verdicts.",
    "Do not execute experiments; experimental execution is outside this procedure.",
)
EXPECTED_SOURCE_COMPLETION_RULES = (
    (
        "Complete only when a bound `SourcePacket` preserves the input "
        "`question_ref` unchanged, the exact query log, retained dead ends and "
        "contradictions, content bindings, and explicit limitations needed for "
        "later inspection."
    ),
)
EXPECTED_RIVAL_METHOD_RULES = (
    (
        "Produce at least two independently formed falsifiable causal mechanisms; "
        "derive each alternative on its own terms before comparing it with the "
        "others."
    ),
    (
        "Apply this priority: mechanism compression before literature novelty, and "
        "literature novelty before impact."
    ),
    (
        "For each mechanism, state its prediction, distinguishing observation, "
        "falsifier, scope, and conflicts with bound evidence or other mechanisms."
    ),
    (
        "Record explicit remaining uncertainty, including uncertainty shared by "
        "every rival, after comparing the alternatives against the same evidence."
    ),
    (
        "Identify observations that could discriminate between rivals; this "
        "procedure does not choose an experiment yet."
    ),
)
EXPECTED_RIVAL_FORBIDDEN_RULES = (
    "Do not rank mechanisms by pilot score or use pilot-score ranking.",
    "Do not select a top winner.",
    "Do not retain unfalsifiable mechanisms.",
    "Do not choose an experiment yet.",
    "Do not call any `Source.*` tool directly.",
)
EXPECTED_RIVAL_COMPLETION_RULES = (
    "Complete only with at least two surviving rivals.",
    (
        "Every surviving rival must have at least one discriminating observation "
        "and a stated falsifier."
    ),
    "If fewer than two rivals survive, do not emit a `RivalMechanismSet`.",
    (
        "Call `Research.observe` with the missing distinguishing evidence, then call "
        "`Research.petition` to request a new `SourcePacket`."
    ),
    "Exit incomplete after those calls; never complete this procedure.",
)


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


def _parse_procedure_frontmatter(path: Path) -> tuple[dict[str, object], str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines and lines[0] == "---"
    try:
        closing = lines.index("---", 1)
    except ValueError as error:
        raise AssertionError("procedure frontmatter is not closed") from error

    metadata: dict[str, object] = {}
    active_list: list[str] | None = None
    for line in lines[1:closing]:
        field = re.fullmatch(r"([a-z_]+):(.*)", line)
        if field is not None:
            key, raw_value = field.groups()
            assert key not in metadata, f"duplicate frontmatter key: {key}"
            value = raw_value.strip()
            if value:
                metadata[key] = value
                active_list = None
            else:
                active_list = []
                metadata[key] = active_list
            continue

        item = re.fullmatch(r"  - ([A-Za-z0-9.-]+)", line)
        assert item is not None and active_list is not None, (
            f"invalid frontmatter line: {line!r}"
        )
        active_list.append(item.group(1))

    assert list(metadata) == ["name", "source_ids", "input", "output", "tools"]
    return metadata, "\n".join(lines[closing + 1 :])


def _procedure_section(body: str, heading: str) -> str:
    match = re.search(
        rf"(?ms)^## {re.escape(heading)}\n(?P<section>.*?)(?=^## |\Z)", body
    )
    assert match is not None, f"missing procedure section: {heading}"
    return match.group("section")


def _procedure_rules(section: str, *, numbered: bool) -> tuple[str, ...]:
    marker = r"([0-9]+)\. (.+)" if numbered else r"- (.+)"
    rules: list[str] = []
    current: list[str] | None = None
    ordinals: list[int] = []

    for line in section.strip().splitlines():
        match = re.fullmatch(marker, line)
        if match is not None:
            if current is not None:
                rules.append(" ".join(current))
            if numbered:
                ordinals.append(int(match.group(1)))
                current = [match.group(2)]
            else:
                current = [match.group(1)]
            continue

        assert current is not None and line.startswith("  "), (
            f"invalid normative rule line: {line!r}"
        )
        current.append(line.strip())

    assert current is not None, "normative rule list must not be empty"
    rules.append(" ".join(current))
    if numbered:
        assert ordinals == list(range(1, len(rules) + 1))
    return tuple(rules)


def _assert_procedure_rules(
    body: str,
    heading: str,
    expected: tuple[str, ...],
    *,
    numbered: bool,
) -> None:
    section = _procedure_section(body, heading)
    assert _procedure_rules(section, numbered=numbered) == expected


def _required_fields(rule: str) -> tuple[str, ...]:
    assert rule.startswith("Required fields: ") and rule.endswith(".")
    return tuple(re.findall(r"`([A-Za-z0-9_]+)`", rule))


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


def _contract_module():
    if not CONTRACTS_PATH.is_file():
        pytest.skip("canonical contract file is not implemented")
    return importlib.import_module("commissioning.research_program.validate")


def _contract_candidate(tmp_path: Path) -> tuple[object, dict[str, object], Path]:
    module = _contract_module()
    value = json.loads(CONTRACTS_PATH.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    path = tmp_path / "procedure_contracts.json"
    return module, value, path


def _write_contract_candidate(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, allow_nan=False, separators=(",", ":")),
        encoding="utf-8",
    )


def test_contract_file_exists() -> None:
    assert CONTRACTS_PATH.is_file()


def test_contract_set_has_exact_canonical_values() -> None:
    module = _contract_module()
    contracts = module.load_contracts(CONTRACTS_PATH)

    assert type(contracts.schema_version) is int
    assert contracts.schema_version == 1
    assert contracts.allowed_tools == EXPECTED_ALLOWED_TOOLS
    assert dict(contracts.artifacts) == EXPECTED_ARTIFACTS
    assert tuple(contracts.procedures) == tuple(EXPECTED_PROCEDURES)
    for name, (input_name, output_name, tools) in EXPECTED_PROCEDURES.items():
        procedure = contracts.procedures[name]
        assert isinstance(procedure, module.ProcedureContract)
        assert procedure.input == input_name
        assert procedure.output == output_name
        assert procedure.tools == tools


def test_contract_json_has_exact_container_shapes() -> None:
    _contract_module()
    value = json.loads(CONTRACTS_PATH.read_text(encoding="utf-8"))

    assert list(value) == ["schema_version", "allowed_tools", "artifacts", "procedures"]
    assert isinstance(value["allowed_tools"], list)
    assert list(value["artifacts"]) == list(EXPECTED_ARTIFACTS)
    for name, required_fields in value["artifacts"].items():
        assert isinstance(required_fields, list), name
        assert required_fields == list(EXPECTED_ARTIFACTS[name])
    assert list(value["procedures"]) == list(EXPECTED_PROCEDURES)
    for procedure in value["procedures"].values():
        assert list(procedure) == ["input", "output", "tools"]
        assert isinstance(procedure["tools"], list)


def test_contract_results_are_recursively_immutable() -> None:
    module = _contract_module()
    contracts = module.load_contracts(CONTRACTS_PATH)
    procedure = contracts.procedures["aros-source-research"]

    with pytest.raises(FrozenInstanceError):
        contracts.schema_version = 2
    with pytest.raises(FrozenInstanceError):
        procedure.input = "OtherArtifact"
    with pytest.raises(TypeError):
        contracts.artifacts["ResearchQuestion"] = ()
    with pytest.raises(TypeError):
        contracts.procedures["new-procedure"] = procedure
    with pytest.raises(TypeError):
        contracts.artifacts["ResearchQuestion"][0] = "other"


def test_contract_dataclasses_are_slotted_and_forced_mutation_is_isolated() -> None:
    module = _contract_module()
    contracts = module.load_contracts(CONTRACTS_PATH)
    independent = module.load_contracts(CONTRACTS_PATH)
    procedure = contracts.procedures["aros-source-research"]

    assert not hasattr(contracts, "__dict__")
    assert not hasattr(procedure, "__dict__")
    assert isinstance(contracts.artifacts, MappingProxyType)
    assert isinstance(contracts.procedures, MappingProxyType)
    assert all(type(fields) is tuple for fields in contracts.artifacts.values())
    assert all(
        type(item.tools) is tuple for item in contracts.procedures.values()
    )

    for target, field, replacement in (
        (contracts, "artifacts", {"Injected": ["mutable"]}),
        (procedure, "tools", ["Injected.tool"]),
    ):
        try:
            object.__setattr__(target, field, replacement)
        except (AttributeError, TypeError):
            pass

    fresh = module.load_contracts(CONTRACTS_PATH)
    for untouched in (independent, fresh):
        assert dict(untouched.artifacts) == EXPECTED_ARTIFACTS
        assert untouched.procedures["aros-source-research"].tools == (
            "Source.read",
            "Source.search",
        )


def test_contract_loader_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    module = _contract_module()
    raw = CONTRACTS_PATH.read_text(encoding="utf-8")
    duplicate = raw.replace(
        '"schema_version": 1,',
        '"schema_version": 1,\n  "schema_version": 1,',
        1,
    )
    path = tmp_path / "duplicate.json"
    path.write_text(duplicate, encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate"):
        module.load_contracts(path)


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_contract_loader_rejects_non_finite_json(
    tmp_path: Path, constant: str
) -> None:
    module = _contract_module()
    raw = CONTRACTS_PATH.read_text(encoding="utf-8")
    candidate = raw.replace(
        '"schema_version": 1,',
        f'"schema_version": 1,\n  "finite_probe": {constant},',
        1,
    )
    path = tmp_path / "non-finite.json"
    path.write_text(candidate, encoding="utf-8")

    with pytest.raises(ValueError, match="finite"):
        module.load_contracts(path)


@pytest.mark.parametrize(
    "forbidden",
    [
        "score",
        "ranking",
        "pass",
        "reward",
        "objective",
        "aggregate",
        "acceptance_score",
    ],
)
def test_contract_loader_recursively_rejects_forbidden_field_names(
    tmp_path: Path, forbidden: str
) -> None:
    module, value, path = _contract_candidate(tmp_path)
    procedures = value["procedures"]
    assert isinstance(procedures, dict)
    source_research = procedures["aros-source-research"]
    assert isinstance(source_research, dict)
    source_research["nested"] = {"deeper": {forbidden: 0}}
    _write_contract_candidate(path, value)

    with pytest.raises(ValueError, match=forbidden):
        module.load_contracts(path)


def test_contract_loader_requires_plain_schema_integer(tmp_path: Path) -> None:
    module, value, path = _contract_candidate(tmp_path)
    value["schema_version"] = True
    _write_contract_candidate(path, value)

    with pytest.raises(ValueError, match="schema_version"):
        module.load_contracts(path)


@pytest.mark.parametrize("section", ["top", "artifact", "procedure"])
def test_contract_loader_rejects_unknown_fields(
    tmp_path: Path, section: str
) -> None:
    module, value, path = _contract_candidate(tmp_path)
    if section == "top":
        value["unknown"] = None
    elif section == "artifact":
        artifacts = value["artifacts"]
        assert isinstance(artifacts, dict)
        artifacts["UnknownArtifact"] = ["field"]
    else:
        procedures = value["procedures"]
        assert isinstance(procedures, dict)
        procedure = procedures["aros-source-research"]
        assert isinstance(procedure, dict)
        procedure["unknown"] = None
    _write_contract_candidate(path, value)

    with pytest.raises(ValueError, match="unknown"):
        module.load_contracts(path)


@pytest.mark.parametrize("section", ["allowed_tools", "artifact", "procedure_tools"])
def test_contract_loader_requires_exact_duplicate_free_lists(
    tmp_path: Path, section: str
) -> None:
    module, value, path = _contract_candidate(tmp_path)
    if section == "allowed_tools":
        values = value["allowed_tools"]
    elif section == "artifact":
        artifacts = value["artifacts"]
        assert isinstance(artifacts, dict)
        values = artifacts["ResearchQuestion"]
    else:
        procedures = value["procedures"]
        assert isinstance(procedures, dict)
        procedure = procedures["aros-source-research"]
        assert isinstance(procedure, dict)
        values = procedure["tools"]
    assert isinstance(values, list)
    values.append(values[0])
    _write_contract_candidate(path, value)

    with pytest.raises(ValueError, match="duplicate"):
        module.load_contracts(path)


@pytest.mark.parametrize("reference", ["input", "output", "tool"])
def test_contract_loader_rejects_unknown_references(
    tmp_path: Path, reference: str
) -> None:
    module, value, path = _contract_candidate(tmp_path)
    procedures = value["procedures"]
    assert isinstance(procedures, dict)
    procedure = procedures["aros-source-research"]
    assert isinstance(procedure, dict)
    if reference == "input":
        procedure["input"] = "UnknownArtifact"
    elif reference == "output":
        procedure["output"] = "UnknownArtifact"
    else:
        tools = procedure["tools"]
        assert isinstance(tools, list)
        tools[0] = "Unknown.tool"
    _write_contract_candidate(path, value)

    with pytest.raises(ValueError, match="unknown"):
        module.load_contracts(path)


@pytest.mark.parametrize("section", ["allowed_tools", "artifact", "procedure_tools"])
def test_contract_loader_rejects_non_list_collections(
    tmp_path: Path, section: str
) -> None:
    module, value, path = _contract_candidate(tmp_path)
    if section == "allowed_tools":
        value["allowed_tools"] = "Source.read"
    elif section == "artifact":
        artifacts = value["artifacts"]
        assert isinstance(artifacts, dict)
        artifacts["ResearchQuestion"] = "question_ref"
    else:
        procedures = value["procedures"]
        assert isinstance(procedures, dict)
        procedure = procedures["aros-source-research"]
        assert isinstance(procedure, dict)
        procedure["tools"] = "Source.read"
    _write_contract_candidate(path, value)

    with pytest.raises(ValueError, match="list"):
        module.load_contracts(path)


def test_contract_loader_rejects_symlink_non_utf8_and_oversize(
    tmp_path: Path,
) -> None:
    module = _contract_module()
    linked = tmp_path / "linked.json"
    linked.symlink_to(CONTRACTS_PATH)
    invalid_utf8 = tmp_path / "invalid-utf8.json"
    invalid_utf8.write_bytes(b"\xff")
    oversize = tmp_path / "oversize.json"
    oversize.write_bytes(b" " * (128 * 1024 + 1))

    with pytest.raises(ValueError, match="regular file"):
        module.load_contracts(tmp_path)
    with pytest.raises(ValueError, match="symlink"):
        module.load_contracts(linked)
    with pytest.raises(ValueError, match="UTF-8"):
        module.load_contracts(invalid_utf8)
    with pytest.raises(ValueError, match="128 KiB"):
        module.load_contracts(oversize)


def test_contract_loader_binds_single_read_to_lstat_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module, value, path = _contract_candidate(tmp_path)
    _write_contract_candidate(path, value)
    replacement = path.read_bytes()
    real_open = os.open

    def replacing_open(candidate: object, flags: int) -> int:
        path.unlink()
        path.write_bytes(replacement)
        return real_open(candidate, flags)

    monkeypatch.setattr(module.os, "open", replacing_open)

    with pytest.raises(ValueError, match="identity"):
        module.load_contracts(path)


@pytest.mark.parametrize(
    "name",
    ["aros-source-research", "aros-rival-mechanisms"],
)
def test_wave_one_procedure_frontmatter_matches_central_contract(name: str) -> None:
    metadata, _ = _parse_procedure_frontmatter(PROCEDURES_ROOT / f"{name}.md")
    contracts = json.loads(CONTRACTS_PATH.read_text(encoding="utf-8"))
    contract = contracts["procedures"][name]

    assert metadata == {
        "name": name,
        "source_ids": ["source-1", "source-2"],
        "input": contract["input"],
        "output": contract["output"],
        "tools": contract["tools"],
    }


@pytest.mark.parametrize(
    "name",
    ["aros-source-research", "aros-rival-mechanisms"],
)
def test_wave_one_procedures_have_exact_sections_and_opaque_provenance(
    name: str,
) -> None:
    path = PROCEDURES_ROOT / f"{name}.md"
    metadata, body = _parse_procedure_frontmatter(path)
    raw = path.read_bytes().lower()

    assert tuple(re.findall(r"(?m)^## ([^\n]+)$", body)) == (
        EXPECTED_PROCEDURE_HEADINGS
    )
    assert all(body.count(f"## {heading}\n") == 1 for heading in EXPECTED_PROCEDURE_HEADINGS)
    assert all(
        isinstance(source_id, str)
        and re.fullmatch(r"source-[0-9]+", source_id) is not None
        for source_id in metadata["source_ids"]
    )
    assert not any(name.encode() in raw for name in UPSTREAM_PRODUCT_NAMES)
    assert b"repository:" not in raw
    assert b"commit:" not in raw
    assert b"license:" not in raw
    assert b"/workspace/" not in raw


def test_rival_procedure_has_exact_incomplete_branch_authority() -> None:
    metadata, _ = _parse_procedure_frontmatter(
        PROCEDURES_ROOT / "aros-rival-mechanisms.md"
    )

    assert metadata["tools"] == [
        "Git.read",
        "Receipt.read",
        "Research.observe",
        "Research.petition",
    ]
    assert all(
        isinstance(tool, str) and not tool.startswith("Source.")
        for tool in metadata["tools"]
    )


def test_source_procedure_has_exact_normative_method_rules() -> None:
    _, body = _parse_procedure_frontmatter(
        PROCEDURES_ROOT / "aros-source-research.md"
    )

    _assert_procedure_rules(
        body,
        "Method",
        EXPECTED_SOURCE_METHOD_RULES,
        numbered=True,
    )


def test_source_procedure_has_exact_output_fields_and_question_lineage() -> None:
    _, body = _parse_procedure_frontmatter(
        PROCEDURES_ROOT / "aros-source-research.md"
    )
    output = _procedure_rules(_procedure_section(body, "Output"), numbered=False)

    assert len(output) == 4
    assert output[0] == "Artifact: Return exactly one `SourcePacket`."
    assert _required_fields(output[1]) == EXPECTED_ARTIFACTS["SourcePacket"]
    assert output[2] == (
        "Lineage: Copy input `question_ref` unchanged to output `question_ref`."
    )
    assert output[3] == (
        "Evidence binding: Bind every factual statement to a cited content "
        "reference or mark it as unresolved."
    )
    _assert_procedure_rules(
        body,
        "Completion",
        EXPECTED_SOURCE_COMPLETION_RULES,
        numbered=False,
    )


def test_source_procedure_forbids_direct_actions_and_scientific_verdicts() -> None:
    _, body = _parse_procedure_frontmatter(
        PROCEDURES_ROOT / "aros-source-research.md"
    )

    _assert_procedure_rules(
        body,
        "Forbidden",
        EXPECTED_SOURCE_FORBIDDEN_RULES,
        numbered=False,
    )


def test_source_method_rejects_opposite_primary_source_polarity() -> None:
    _, body = _parse_procedure_frontmatter(
        PROCEDURES_ROOT / "aros-source-research.md"
    )
    mutated = body.replace(
        "1. Prefer primary sources;",
        "1. Do not prefer primary sources;",
        1,
    )
    assert mutated != body

    with pytest.raises(AssertionError):
        _assert_procedure_rules(
            mutated,
            "Method",
            EXPECTED_SOURCE_METHOD_RULES,
            numbered=True,
        )


def test_rival_procedure_forms_independent_falsifiable_causal_alternatives() -> None:
    _, body = _parse_procedure_frontmatter(
        PROCEDURES_ROOT / "aros-rival-mechanisms.md"
    )

    inputs = _procedure_rules(_procedure_section(body, "Inputs"), numbered=False)
    assert len(inputs) == 3
    assert inputs[0] == "Artifact: Read exactly one `SourcePacket`."
    assert _required_fields(inputs[1]) == EXPECTED_ARTIFACTS["SourcePacket"]
    assert inputs[2] == (
        "Lineage: Treat input `question_ref` as the immutable root question "
        "reference."
    )
    _assert_procedure_rules(
        body,
        "Method",
        EXPECTED_RIVAL_METHOD_RULES,
        numbered=True,
    )


def test_rival_procedure_has_exact_output_fields_and_question_lineage() -> None:
    _, body = _parse_procedure_frontmatter(
        PROCEDURES_ROOT / "aros-rival-mechanisms.md"
    )
    output = _procedure_rules(_procedure_section(body, "Output"), numbered=False)

    assert len(output) == 4
    assert output[0] == "Artifact: Return exactly one `RivalMechanismSet`."
    assert _required_fields(output[1]) == EXPECTED_ARTIFACTS["RivalMechanismSet"]
    assert output[2] == (
        "Lineage: Set `root_question_ref` exactly to input `question_ref`."
    )
    assert output[3] == (
        "Evidence binding: Preserve the evidence reference supporting or "
        "challenging every mechanism."
    )


def test_rival_completion_requires_two_surviving_discriminable_rivals() -> None:
    _, body = _parse_procedure_frontmatter(
        PROCEDURES_ROOT / "aros-rival-mechanisms.md"
    )

    _assert_procedure_rules(
        body,
        "Completion",
        EXPECTED_RIVAL_COMPLETION_RULES,
        numbered=False,
    )
    completion = _procedure_section(body, "Completion")
    assert "return unresolved" not in completion.lower()
    assert completion.index("`Research.observe`") < completion.index(
        "`Research.petition`"
    )


def test_rival_procedure_rejects_score_winners_and_unfalsifiable_rivals() -> None:
    _, body = _parse_procedure_frontmatter(
        PROCEDURES_ROOT / "aros-rival-mechanisms.md"
    )

    _assert_procedure_rules(
        body,
        "Forbidden",
        EXPECTED_RIVAL_FORBIDDEN_RULES,
        numbered=False,
    )


def test_rival_forbidden_rules_reject_opposite_ranking_polarity() -> None:
    _, body = _parse_procedure_frontmatter(
        PROCEDURES_ROOT / "aros-rival-mechanisms.md"
    )
    mutated = body.replace(
        "- Do not rank mechanisms by pilot score or use pilot-score ranking.",
        "- May rank by pilot score.",
        1,
    )
    assert mutated != body

    with pytest.raises(AssertionError):
        _assert_procedure_rules(
            mutated,
            "Forbidden",
            EXPECTED_RIVAL_FORBIDDEN_RULES,
            numbered=False,
        )


def test_contract_loader_fifo_swap_is_prompt_and_leaks_no_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module, value, path = _contract_candidate(tmp_path)
    _write_contract_candidate(path, value)
    real_open = os.open
    before_descriptors = set(os.listdir("/proc/self/fd"))

    def replacing_open(candidate: object, flags: int) -> int:
        path.unlink()
        os.mkfifo(path)
        return real_open(candidate, flags)

    def interrupt_blocked_open(_signum: int, _frame: object) -> None:
        raise TimeoutError("contract FIFO open blocked")

    monkeypatch.setattr(module.os, "open", replacing_open)
    previous_handler = signal.signal(signal.SIGALRM, interrupt_blocked_open)
    started = time.monotonic()
    try:
        signal.setitimer(signal.ITIMER_REAL, 0.25)
        with pytest.raises(ValueError, match="regular file"):
            module.load_contracts(path)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)

    assert time.monotonic() - started < 1.0
    assert set(os.listdir("/proc/self/fd")) == before_descriptors


def test_contract_loader_uses_only_standard_library_imports() -> None:
    _contract_module()
    validate_path = PROGRAM_ROOT / "validate.py"
    tree = ast.parse(validate_path.read_text(encoding="utf-8"))
    imports = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imports.update(
        node.module.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    )

    assert imports <= {
        "__future__",
        "dataclasses",
        "json",
        "math",
        "os",
        "pathlib",
        "stat",
        "types",
        "typing",
    }
