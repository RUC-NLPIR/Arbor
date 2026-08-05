"""Exact, read-only TransitionProposal audit behavior."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

import arbor.aros.transitions as transitions_module
from arbor.aros.observations import ObservationRecord
from arbor.aros.store import canonical_json_bytes, json_sha256
from arbor.aros.transitions import (
    Assimilation,
    TransitionAuditService,
    TransitionError,
    TransitionProposal,
    build_operational_proposal,
    load_transition_proposal,
)
from tests import test_aros_observations as observation_support


AUDIT_FIELDS = {
    "schema_version",
    "transition_id",
    "base_commit",
    "current_head",
    "proposal_blob_sha256",
    "path_receipts",
    "observation_closure",
    "assimilation_links",
    "audit_payload_sha256",
    "candidate_subject_sha256",
    "mechanically_valid",
    "issues",
}


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _init_workspace(root: Path) -> str:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "transition@example.invalid")
    _git(root, "config", "user.name", "Transition Test")
    (root / ".gitignore").write_text("/.aros/\n/.worktree/\n", encoding="utf-8")
    (root / "AROS.md").write_text("# AROS\n", encoding="utf-8")
    (root / "memory").mkdir()
    (root / "memory" / "NOW.md").write_text(
        "# Current State\n\n## Findings\n\nInitial finding.\n",
        encoding="utf-8",
    )
    _git(root, "add", ".gitignore", "AROS.md", "memory/NOW.md")
    _git(root, "commit", "-qm", "initial state")
    return _git(root, "rev-parse", "HEAD")


def _write_proposal(
    root: Path,
    transition_id: str,
    *,
    base_commit: str,
    workspace_paths: list[str],
    assimilations: list[dict[str, object]] | None = None,
    extra: dict[str, object] | None = None,
) -> str:
    relative = f"transitions/{transition_id}/proposal.json"
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    proposal: dict[str, object] = {
        "schema_version": 1,
        "base_commit": base_commit,
        "workspace_paths": workspace_paths,
        "assimilations": assimilations or [],
    }
    proposal.update(extra or {})
    path.write_text(
        json.dumps(proposal, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return relative


def _audit(root: Path, proposal_ref: str) -> dict[str, object]:
    return TransitionAuditService(
        root,
        canonical_ref=_git(root, "symbolic-ref", "HEAD"),
    ).audit(proposal_ref)


def _fake_observation(
    ref: str,
    *,
    kind: str = "task_return",
    candidate_commit: str = "a" * 40,
    versioned_paths: tuple[str, ...] = (),
    measurement_state: str | None = None,
    base_commit: str | None = None,
) -> ObservationRecord:
    payload: dict[str, object] = {"candidate_commit": candidate_commit}
    if kind == "task_return":
        payload["child_commit"] = candidate_commit
        payload["base_commit"] = base_commit or candidate_commit
    return ObservationRecord(
        ref=ref,
        kind=kind,  # type: ignore[arg-type]
        record_sha256="f" * 64,
        versioned_paths=versioned_paths,
        candidate_commit=candidate_commit,
        measurement_state=measurement_state,
        payload=payload,
    )


def _snapshot_tree(root: Path) -> dict[str, tuple[int, bytes | None]]:
    snapshot: dict[str, tuple[int, bytes | None]] = {}
    for path in [root, *sorted(root.rglob("*"), key=lambda item: item.as_posix())]:
        metadata = path.lstat()
        payload: bytes | None = None
        if stat.S_ISREG(metadata.st_mode):
            payload = path.read_bytes()
        elif stat.S_ISLNK(metadata.st_mode):
            payload = os.fsencode(os.readlink(path))
        relative = "." if path == root else path.relative_to(root).as_posix()
        snapshot[relative] = (metadata.st_mode, payload)
    return snapshot


def _changed_semantic_proposal(root: Path, transition_id: str = "T-basic") -> str:
    base = _init_workspace(root)
    (root / "memory" / "NOW.md").write_text(
        "# Current State\n\n## Findings\n\nChanged finding.\n",
        encoding="utf-8",
    )
    return _write_proposal(
        root,
        transition_id,
        base_commit=base,
        workspace_paths=["memory/NOW.md"],
    )


def test_proposal_requires_exact_four_fields_and_directory_identity(
    tmp_path: Path,
) -> None:
    base = _init_workspace(tmp_path)
    proposal_ref = _write_proposal(
        tmp_path,
        "T-exact",
        base_commit=base,
        workspace_paths=["memory/NOW.md"],
        assimilations=[
            {
                "observation_ref": "runs/RUN-example/final.json",
                "affected_paths": ["memory/NOW.md"],
                "rationale": "memory/NOW.md#Findings",
            }
        ],
    )

    proposal = load_transition_proposal(tmp_path, proposal_ref)

    assert proposal == TransitionProposal(
        schema_version=1,
        base_commit=base,
        workspace_paths=("memory/NOW.md",),
        assimilations=(
            Assimilation(
                observation_ref="runs/RUN-example/final.json",
                affected_paths=("memory/NOW.md",),
                rationale="memory/NOW.md#Findings",
            ),
        ),
    )
    assert not hasattr(proposal, "transition_id")

    with pytest.raises(TransitionError, match="exact|field|schema"):
        load_transition_proposal(
            tmp_path,
            _write_proposal(
                tmp_path,
                "T-extra",
                base_commit=base,
                workspace_paths=[],
                extra={"transition_id": "T-forged"},
            ),
        )

    wrong = tmp_path / "transitions" / "not-a-transition" / "proposal.json"
    wrong.parent.mkdir(parents=True)
    wrong.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "base_commit": base,
                "workspace_paths": [],
                "assimilations": [],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(TransitionError, match="path|identity"):
        load_transition_proposal(
            tmp_path,
            "transitions/not-a-transition/proposal.json",
        )

    duplicate = tmp_path / "transitions" / "T-duplicate" / "proposal.json"
    duplicate.parent.mkdir(parents=True)
    duplicate.write_text(
        '{"schema_version":1,"schema_version":1,"base_commit":"'
        + base
        + '","workspace_paths":[],"assimilations":[]}',
        encoding="utf-8",
    )
    with pytest.raises(TransitionError, match="duplicate|JSON"):
        load_transition_proposal(
            tmp_path,
            "transitions/T-duplicate/proposal.json",
        )

    transition_id, operational = build_operational_proposal(
        base,
        ["runs/RUN-z/final.json", "runs/RUN-a/manifest.json", "runs/RUN-z/final.json"],
        "b" * 64,
    )
    assert transition_id == f"T-OPS-{base[:12]}-{'b' * 12}"
    assert set(operational) == {
        "schema_version",
        "base_commit",
        "workspace_paths",
        "assimilations",
    }
    assert operational["workspace_paths"] == [
        "runs/RUN-a/manifest.json",
        "runs/RUN-z/final.json",
    ]
    assert operational["assimilations"] == []


def test_proposal_rejects_non_utf8_scalar_paths(tmp_path: Path) -> None:
    base = _init_workspace(tmp_path)
    relative = "transitions/T-surrogate/proposal.json"
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_bytes(
        (
            '{"schema_version":1,"base_commit":"'
            + base
            + '","workspace_paths":["memory/\\ud800.md"],"assimilations":[]}'
        ).encode("ascii")
    )

    with pytest.raises(TransitionError, match="UTF-8|canonical"):
        load_transition_proposal(tmp_path, relative)

    audit = _audit(tmp_path, relative)
    assert set(audit) == AUDIT_FIELDS
    assert audit["mechanically_valid"] is False


def test_audit_reports_non_utf8_scalar_semantic_links(tmp_path: Path) -> None:
    _init_workspace(tmp_path)
    claim = tmp_path / "knowledge" / "claims" / "C-scalar.md"
    claim.parent.mkdir(parents=True)
    claim.write_text(
        "---\nid: C-scalar\n---\n# Claim\n\n## Evidence links\n",
        encoding="utf-8",
    )
    _git(tmp_path, "add", "knowledge/claims/C-scalar.md")
    _git(tmp_path, "commit", "-qm", "add scalar claim")
    base = _git(tmp_path, "rev-parse", "HEAD")
    claim.write_bytes(
        b"---\nid: C-scalar\n---\n# Claim\n\n## Evidence links\n\n"
        b'{"observation_ref":"runs/RUN-scalar/final.json","relation":"context",'
        b'"scope":"\\ud800"}\n'
    )
    proposal_ref = _write_proposal(
        tmp_path,
        "T-link-scalar",
        base_commit=base,
        workspace_paths=["knowledge/claims/C-scalar.md"],
    )

    audit = _audit(tmp_path, proposal_ref)

    assert audit["mechanically_valid"] is False
    assert any("semantic" in str(issue["code"]) for issue in audit["issues"])


def test_audit_rejects_stale_base_symlink_runtime_and_undeclared_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale_root = tmp_path / "stale"
    stale_base = _init_workspace(stale_root)
    (stale_root / "memory" / "NOW.md").write_text("# Current State\n", encoding="utf-8")
    _git(stale_root, "add", "memory/NOW.md")
    _git(stale_root, "commit", "-qm", "advance canonical ref")
    stale_ref = _write_proposal(
        stale_root,
        "T-stale",
        base_commit=stale_base,
        workspace_paths=["memory/NOW.md"],
    )

    symlink_root = tmp_path / "symlink"
    symlink_base = _init_workspace(symlink_root)
    (symlink_root / "outside.md").write_text("outside\n", encoding="utf-8")
    (symlink_root / "memory" / "NOW.md").unlink()
    (symlink_root / "memory" / "NOW.md").symlink_to("../outside.md")
    symlink_ref = _write_proposal(
        symlink_root,
        "T-symlink",
        base_commit=symlink_base,
        workspace_paths=["memory/NOW.md"],
    )

    runtime_root = tmp_path / "runtime"
    runtime_base = _init_workspace(runtime_root)
    (runtime_root / ".aros").mkdir()
    (runtime_root / ".aros" / "state.json").write_text("{}\n", encoding="utf-8")
    runtime_ref = _write_proposal(
        runtime_root,
        "T-runtime",
        base_commit=runtime_base,
        workspace_paths=[".aros/state.json"],
    )

    undeclared_root = tmp_path / "undeclared"
    undeclared_base = _init_workspace(undeclared_root)
    (undeclared_root / "AROS.md").write_text("# Changed AROS\n", encoding="utf-8")
    (undeclared_root / "memory" / "NOW.md").write_text(
        "# Current State\n\n## Findings\n\nChanged.\n",
        encoding="utf-8",
    )
    observation_ref = "tasks/TASK-20260805-example/collected.json"
    undeclared_ref = _write_proposal(
        undeclared_root,
        "T-undeclared",
        base_commit=undeclared_base,
        workspace_paths=["AROS.md"],
        assimilations=[
            {
                "observation_ref": observation_ref,
                "affected_paths": ["memory/NOW.md"],
                "rationale": "memory/NOW.md#Findings",
            }
        ],
    )
    monkeypatch.setattr(
        transitions_module.ObservationCatalog,
        "resolve",
        lambda _self, _ref, **_kwargs: _fake_observation(observation_ref),
    )

    audits = [
        _audit(stale_root, stale_ref),
        _audit(symlink_root, symlink_ref),
        _audit(runtime_root, runtime_ref),
        _audit(undeclared_root, undeclared_ref),
    ]

    assert all(set(audit) == AUDIT_FIELDS for audit in audits)
    assert all(audit["mechanically_valid"] is False for audit in audits)
    codes = [{str(issue["code"]) for issue in audit["issues"]} for audit in audits]
    assert any("stale" in code for code in codes[0])
    assert any("unsafe" in code or "ordinary" in code for code in codes[1])
    assert any("unsupported" in code or "runtime" in code for code in codes[2])
    assert any("undeclared" in code for code in codes[3])


def test_audit_rejects_selected_file_inside_submodule(tmp_path: Path) -> None:
    _init_workspace(tmp_path)
    nested = tmp_path / "model" / "component"
    nested.mkdir(parents=True)
    _git(nested, "init", "-q", "-b", "main")
    _git(nested, "config", "user.email", "nested@example.invalid")
    _git(nested, "config", "user.name", "Nested Test")
    (nested / "CURRENT.md").write_text("# Nested Model\n", encoding="utf-8")
    _git(nested, "add", "CURRENT.md")
    _git(nested, "commit", "-qm", "nested state")
    _git(tmp_path, "add", "model/component")
    _git(tmp_path, "commit", "-qm", "add submodule gitlink")
    base = _git(tmp_path, "rev-parse", "HEAD")
    (nested / "CURRENT.md").write_text("# Changed Nested Model\n", encoding="utf-8")
    proposal_ref = _write_proposal(
        tmp_path,
        "T-submodule",
        base_commit=base,
        workspace_paths=["model/component/CURRENT.md"],
    )

    audit = _audit(tmp_path, proposal_ref)

    assert audit["mechanically_valid"] is False
    assert any("submodule" in str(issue["code"]) for issue in audit["issues"])


def test_audit_rejects_base_gitlink_descendant_after_marker_removed(
    tmp_path: Path,
) -> None:
    _init_workspace(tmp_path)
    nested = tmp_path / "model" / "component"
    nested.mkdir(parents=True)
    _git(nested, "init", "-q", "-b", "main")
    _git(nested, "config", "user.email", "nested@example.invalid")
    _git(nested, "config", "user.name", "Nested Test")
    (nested / "CURRENT.md").write_text("# Nested Model\n", encoding="utf-8")
    _git(nested, "add", "CURRENT.md")
    _git(nested, "commit", "-qm", "nested state")
    _git(tmp_path, "add", "model/component")
    _git(tmp_path, "commit", "-qm", "record base gitlink")
    base = _git(tmp_path, "rev-parse", "HEAD")

    backup = tmp_path / ".worktree" / "removed-component"
    backup.parent.mkdir()
    nested.rename(backup)
    nested.mkdir()
    (nested / "CURRENT.md").write_text(
        "# Ordinary Replacement Model\n",
        encoding="utf-8",
    )
    assert not (nested / ".git").exists()
    proposal_ref = _write_proposal(
        tmp_path,
        "T-base-gitlink",
        base_commit=base,
        workspace_paths=["model/component/CURRENT.md"],
    )

    audit = _audit(tmp_path, proposal_ref)

    assert audit["mechanically_valid"] is False
    assert any("submodule" in str(issue["code"]) for issue in audit["issues"])


@pytest.mark.parametrize("case", ("unchanged", "rationale"))
def test_audit_requires_changed_semantic_rationale_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    base = _init_workspace(tmp_path)
    if case == "rationale":
        (tmp_path / "memory" / "NOW.md").write_text(
            "# Current State\n\n## Findings\n\nChanged.\n",
            encoding="utf-8",
        )
    observation_ref = "tasks/TASK-20260805-rationale/collected.json"
    proposal_ref = _write_proposal(
        tmp_path,
        f"T-{case}",
        base_commit=base,
        workspace_paths=["memory/NOW.md"],
        assimilations=[
            {
                "observation_ref": observation_ref,
                "affected_paths": ["memory/NOW.md"],
                "rationale": (
                    "memory/NOW.md#Missing"
                    if case == "rationale"
                    else "memory/NOW.md#Findings"
                ),
            }
        ],
    )
    monkeypatch.setattr(
        transitions_module.ObservationCatalog,
        "resolve",
        lambda _self, _ref, **_kwargs: _fake_observation(observation_ref),
    )

    audit = _audit(tmp_path, proposal_ref)

    assert audit["mechanically_valid"] is False
    assert any(
        fragment in str(issue["code"])
        for issue in audit["issues"]
        for fragment in (("unchanged",) if case == "unchanged" else ("rationale", "anchor"))
    )


def test_audit_binds_evidence_link_to_same_assimilation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _init_workspace(tmp_path)
    claim = tmp_path / "knowledge" / "claims" / "C-link.md"
    claim.parent.mkdir(parents=True)
    claim.write_text(
        "---\nid: C-link\n---\n# Claim\n\n## Evidence links\n\n",
        encoding="utf-8",
    )
    _git(tmp_path, "add", "knowledge/claims/C-link.md")
    _git(tmp_path, "commit", "-qm", "add initial claim")
    base = _git(tmp_path, "rev-parse", "HEAD")
    observation_ref = "tasks/TASK-20260805-bound/collected.json"
    other_ref = "tasks/TASK-20260805-other/collected.json"
    claim.write_text(
        "---\nid: C-link\n---\n# Claim\n\n## Evidence links\n\n"
        + json.dumps(
            {
                "observation_ref": other_ref,
                "relation": "context",
                "scope": "Wrong observation.",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    proposal_ref = _write_proposal(
        tmp_path,
        "T-link-binding",
        base_commit=base,
        workspace_paths=["knowledge/claims/C-link.md"],
        assimilations=[
            {
                "observation_ref": observation_ref,
                "affected_paths": ["knowledge/claims/C-link.md"],
                "rationale": "knowledge/claims/C-link.md#Evidence links",
            }
        ],
    )
    monkeypatch.setattr(
        transitions_module.ObservationCatalog,
        "resolve",
        lambda _self, _ref, **_kwargs: _fake_observation(observation_ref),
    )

    audit = _audit(tmp_path, proposal_ref)

    assert audit["mechanically_valid"] is False
    assert audit["assimilation_links"] == []
    assert any("link" in str(issue["code"]) for issue in audit["issues"])


def test_measurement_claim_requires_owner_parsed_link(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _init_workspace(tmp_path)
    claim = tmp_path / "knowledge" / "claims" / "C-measurement.md"
    claim.parent.mkdir(parents=True)
    claim.write_text(
        "---\nid: C-measurement\n---\n# Claim\n\n## Evidence links\n",
        encoding="utf-8",
    )
    _git(tmp_path, "add", "knowledge/claims/C-measurement.md")
    _git(tmp_path, "commit", "-qm", "add measurement claim")
    base = _git(tmp_path, "rev-parse", "HEAD")
    observation_ref = f"eval/evaluations/EVAL-{'d' * 64}/receipt.json"
    proposal_ref = _write_proposal(
        tmp_path,
        "T-measurement-link",
        base_commit=base,
        workspace_paths=["knowledge/claims/C-measurement.md"],
        assimilations=[
            {
                "observation_ref": observation_ref,
                "affected_paths": ["knowledge/claims/C-measurement.md"],
                "rationale": "knowledge/claims/C-measurement.md#Evidence links",
            }
        ],
    )
    record = _fake_observation(
        observation_ref,
        kind="measurement",
        candidate_commit="c" * 40,
        measurement_state="valid",
    )
    monkeypatch.setattr(
        transitions_module.ObservationCatalog,
        "resolve",
        lambda _self, _ref, **_kwargs: record,
    )

    missing = _audit(tmp_path, proposal_ref)
    assert missing["mechanically_valid"] is False
    assert any("link_missing" in str(issue["code"]) for issue in missing["issues"])

    link = {
        "observation_ref": observation_ref,
        "relation": "supports",
        "scope": "Only the preregistered scalar contrast.",
    }
    claim.write_text(
        "---\nid: C-measurement\n---\n# Claim\n\n## Evidence links\n\n"
        + json.dumps(link, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    audit = _audit(tmp_path, proposal_ref)

    assert audit["mechanically_valid"] is True
    assert len(audit["assimilation_links"]) == 1
    receipt = audit["assimilation_links"][0]
    canonical_sha256 = json_sha256(link)
    identity = {
        "schema_version": 1,
        "transition_id": "T-measurement-link",
        "path": "knowledge/claims/C-measurement.md",
        "anchor": "Evidence links",
        "ordinal": 0,
        "canonical_sha256": canonical_sha256,
    }
    assert receipt["link_id"] == f"EL-{json_sha256(identity)}"
    assert receipt["canonical_sha256"] == canonical_sha256


def test_eval_outcome_can_only_link_as_process_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _init_workspace(tmp_path)
    claim = tmp_path / "knowledge" / "claims" / "C-apparatus.md"
    claim.parent.mkdir(parents=True)
    claim.write_text(
        "---\nid: C-apparatus\n---\n# Claim\n\n## Evidence links\n",
        encoding="utf-8",
    )
    _git(tmp_path, "add", "knowledge/claims/C-apparatus.md")
    _git(tmp_path, "commit", "-qm", "add apparatus claim")
    base = _git(tmp_path, "rev-parse", "HEAD")
    observation_ref = f"eval/evaluations/EVAL-{'a' * 64}/receipt.json"
    claim.write_text(
        "---\nid: C-apparatus\n---\n# Claim\n\n## Evidence links\n\n"
        + json.dumps(
            {
                "observation_ref": observation_ref,
                "relation": "supports",
                "scope": "Invalid evaluation cannot support the claim.",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    proposal_ref = _write_proposal(
        tmp_path,
        "T-eval-context",
        base_commit=base,
        workspace_paths=["knowledge/claims/C-apparatus.md"],
        assimilations=[
            {
                "observation_ref": observation_ref,
                "affected_paths": ["knowledge/claims/C-apparatus.md"],
                "rationale": "knowledge/claims/C-apparatus.md#Evidence links",
            }
        ],
    )
    monkeypatch.setattr(
        transitions_module.ObservationCatalog,
        "resolve",
        lambda _self, _ref, **_kwargs: _fake_observation(
            observation_ref,
            kind="eval_outcome",
            candidate_commit="c" * 40,
            measurement_state="invalid_eval",
        ),
    )

    audit = _audit(tmp_path, proposal_ref)

    assert audit["mechanically_valid"] is False
    assert audit["observation_closure"][0]["kind"] == "eval_outcome"
    assert any("nonmeasurement" in str(issue["code"]) for issue in audit["issues"])


def test_audit_derives_exact_new_observation_closure(tmp_path: Path) -> None:
    _service, manifest, _final = observation_support._install_run_final(tmp_path)
    base = _git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "NOW.md").write_text(
        "# Current State\n\n## Findings\n\nAssimilated process context.\n",
        encoding="utf-8",
    )
    observation_ref = f"runs/{manifest['run_id']}/final.json"
    proposal_ref = _write_proposal(
        tmp_path,
        "T-new-closure",
        base_commit=base,
        workspace_paths=["memory/NOW.md"],
        assimilations=[
            {
                "observation_ref": observation_ref,
                "affected_paths": ["memory/NOW.md"],
                "rationale": "memory/NOW.md#Findings",
            }
        ],
    )

    audit = _audit(tmp_path, proposal_ref)

    assert audit["mechanically_valid"] is True
    closure = audit["observation_closure"]
    assert isinstance(closure, list) and len(closure) == 1
    paths = {item["path"]: item for item in closure[0]["paths"]}
    expected = {
        f"runs/{manifest['run_id']}/manifest.json",
        observation_ref,
    }
    assert set(paths) == expected
    assert all(paths[path]["state"] == "derived" for path in expected)
    for path in expected:
        raw = (tmp_path / path).read_bytes()
        blob_oid = hashlib.sha1(
            b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw
        ).hexdigest()
        assert paths[path]["blob_oid"] == blob_oid
    assert all(not item["path"].startswith(".aros/") for item in paths.values())


def test_audit_marks_exact_base_observation_paths_ref_only(tmp_path: Path) -> None:
    _service, manifest, _final = observation_support._install_run_final(tmp_path)
    (tmp_path / "memory").mkdir()
    now = tmp_path / "memory" / "NOW.md"
    now.write_text(
        "# Current State\n\n## Findings\n\nInitial process context.\n",
        encoding="utf-8",
    )
    _git(tmp_path, "add", "memory/NOW.md", "runs")
    _git(tmp_path, "commit", "-qm", "version observation records")
    base = _git(tmp_path, "rev-parse", "HEAD")
    now.write_text(
        "# Current State\n\n## Findings\n\nChanged process context.\n",
        encoding="utf-8",
    )
    observation_ref = f"runs/{manifest['run_id']}/final.json"
    proposal_ref = _write_proposal(
        tmp_path,
        "T-ref-only",
        base_commit=base,
        workspace_paths=["memory/NOW.md"],
        assimilations=[
            {
                "observation_ref": observation_ref,
                "affected_paths": ["memory/NOW.md"],
                "rationale": "memory/NOW.md#Findings",
            }
        ],
    )

    audit = _audit(tmp_path, proposal_ref)

    assert audit["mechanically_valid"] is True
    assert {
        path["state"]
        for path in audit["observation_closure"][0]["paths"]
    } == {"ref_only"}


def test_audit_validates_explicit_service_records(tmp_path: Path) -> None:
    _service, manifest, _final = observation_support._install_run_final(tmp_path)
    base = _git(tmp_path, "rev-parse", "HEAD")
    run_id = str(manifest["run_id"])
    paths = sorted(
        [f"runs/{run_id}/manifest.json", f"runs/{run_id}/final.json"]
    )
    proposal_ref = _write_proposal(
        tmp_path,
        "T-service-records",
        base_commit=base,
        workspace_paths=paths,
    )

    audit = _audit(tmp_path, proposal_ref)

    assert audit["mechanically_valid"] is True
    assert [receipt["path"] for receipt in audit["path_receipts"]] == paths
    assert {receipt["owner"] for receipt in audit["path_receipts"]} == {"run"}


def test_audit_task_measurement_pair_requires_equal_candidate_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _init_workspace(tmp_path)
    (tmp_path / "memory" / "NOW.md").write_text(
        "# Current State\n\n## Findings\n\nTask return.\n",
        encoding="utf-8",
    )
    (tmp_path / "model").mkdir()
    (tmp_path / "model" / "CURRENT.md").write_text(
        "# Current Model\n\n## Measurement\n\nMeasured update.\n",
        encoding="utf-8",
    )
    task_ref = "tasks/TASK-20260805-pair/collected.json"
    eval_ref = f"eval/evaluations/EVAL-{'e' * 64}/receipt.json"
    records = {
        task_ref: _fake_observation(
            task_ref,
            candidate_commit="a" * 40,
            base_commit=base,
        ),
        eval_ref: _fake_observation(
            eval_ref,
            kind="measurement",
            candidate_commit="b" * 40,
            measurement_state="valid",
        ),
    }
    proposal_ref = _write_proposal(
        tmp_path,
        "T-pair",
        base_commit=base,
        workspace_paths=["memory/NOW.md", "model/CURRENT.md"],
        assimilations=[
            {
                "observation_ref": eval_ref,
                "affected_paths": ["model/CURRENT.md"],
                "rationale": "model/CURRENT.md#Measurement",
            },
            {
                "observation_ref": task_ref,
                "affected_paths": ["memory/NOW.md"],
                "rationale": "memory/NOW.md#Findings",
            },
        ],
    )
    monkeypatch.setattr(
        transitions_module.ObservationCatalog,
        "resolve",
        lambda _self, ref, **_kwargs: records[ref],
    )

    audit = _audit(tmp_path, proposal_ref)

    assert audit["mechanically_valid"] is False
    assert any("candidate" in str(issue["detail"]).casefold() for issue in audit["issues"])


def test_stale_task_base_is_bound_as_fact_without_denial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _init_workspace(tmp_path)
    (tmp_path / "memory" / "NOW.md").write_text(
        "# Current State\n\n## Findings\n\nStale Task prose context.\n",
        encoding="utf-8",
    )
    observation_ref = "tasks/TASK-20260805-stale/collected.json"
    proposal_ref = _write_proposal(
        tmp_path,
        "T-stale-task",
        base_commit=base,
        workspace_paths=["memory/NOW.md"],
        assimilations=[
            {
                "observation_ref": observation_ref,
                "affected_paths": ["memory/NOW.md"],
                "rationale": "memory/NOW.md#Findings",
            }
        ],
    )
    monkeypatch.setattr(
        transitions_module.ObservationCatalog,
        "resolve",
        lambda _self, _ref, **_kwargs: _fake_observation(
            observation_ref,
            base_commit="b" * 40,
        ),
    )

    audit = _audit(tmp_path, proposal_ref)

    assert audit["mechanically_valid"] is True
    assert audit["observation_closure"][0]["task_base_status"] == "stale"


def test_audit_contains_no_scientific_verdict(tmp_path: Path) -> None:
    proposal_ref = _changed_semantic_proposal(tmp_path)

    audit = _audit(tmp_path, proposal_ref)
    encoded = canonical_json_bytes(audit).decode("utf-8").casefold()

    assert audit["mechanically_valid"] is True
    for forbidden in (
        "scientifically_valid",
        "scientific_validity",
        "scientific_verdict",
        "scientific_judgment",
    ):
        assert forbidden not in encoded


def test_audit_writes_nothing_and_is_byte_deterministic(tmp_path: Path) -> None:
    proposal_ref = _changed_semantic_proposal(tmp_path)
    status_before = _git(tmp_path, "status", "--porcelain=v1", "--untracked-files=all")
    before = _snapshot_tree(tmp_path)
    service = TransitionAuditService(tmp_path, canonical_ref="refs/heads/main")

    first = service.audit(proposal_ref)
    second = service.audit(proposal_ref)

    assert first == second
    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    assert _snapshot_tree(tmp_path) == before
    assert _git(tmp_path, "status", "--porcelain=v1", "--untracked-files=all") == status_before
    assert not (tmp_path / ".aros").exists()


def test_candidate_subject_hash_binds_every_payload_field(tmp_path: Path) -> None:
    proposal_ref = _changed_semantic_proposal(tmp_path, "T-hashes")

    audit = _audit(tmp_path, proposal_ref)

    payload = {
        key: value
        for key, value in audit.items()
        if key not in {"audit_payload_sha256", "candidate_subject_sha256"}
    }
    assert audit["audit_payload_sha256"] == json_sha256(payload)
    workspace = sorted(
        [receipt["path"], receipt["owner"], receipt["blob_oid"]]
        for receipt in audit["path_receipts"]
    )
    closure = sorted(
        [path["path"], path["blob_oid"]]
        for record in audit["observation_closure"]
        for path in record["paths"]
        if path["state"] == "derived"
    )
    subject = {
        "schema_version": audit["schema_version"],
        "transition_id": audit["transition_id"],
        "base_commit": audit["base_commit"],
        "workspace": workspace,
        "observation_closure": closure,
        "proposal_blob_sha256": audit["proposal_blob_sha256"],
        "audit_payload_sha256": audit["audit_payload_sha256"],
    }
    assert audit["candidate_subject_sha256"] == json_sha256(subject)

    for key in payload:
        changed = dict(payload)
        changed[key] = {"changed": key}
        changed_payload_hash = json_sha256(changed)
        assert changed_payload_hash != audit["audit_payload_sha256"]
        changed_subject = dict(subject)
        changed_subject["audit_payload_sha256"] = changed_payload_hash
        assert json_sha256(changed_subject) != audit["candidate_subject_sha256"]


def test_missing_recommended_heading_is_warning_not_denial(tmp_path: Path) -> None:
    base = _init_workspace(tmp_path)
    question = tmp_path / "questions" / "Q-heading" / "question.md"
    question.parent.mkdir(parents=True)
    question.write_text(
        "---\nid: Q-heading\n---\n# Question\n\nInitial question.\n",
        encoding="utf-8",
    )
    _git(tmp_path, "add", "questions/Q-heading/question.md")
    _git(tmp_path, "commit", "-qm", "add question")
    base = _git(tmp_path, "rev-parse", "HEAD")
    question.write_text(
        "---\nid: Q-heading\n---\n# Question\n\nChanged question.\n",
        encoding="utf-8",
    )
    proposal_ref = _write_proposal(
        tmp_path,
        "T-warning",
        base_commit=base,
        workspace_paths=["questions/Q-heading/question.md"],
    )

    audit = _audit(tmp_path, proposal_ref)

    assert audit["mechanically_valid"] is True
    warnings = [issue for issue in audit["issues"] if issue["severity"] == "warning"]
    assert warnings
    assert all("missing" in str(issue["detail"]).casefold() for issue in warnings)


def test_audit_rejects_drift_and_runtime_observation_closure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drift_root = tmp_path / "drift"
    drift_ref = _changed_semantic_proposal(drift_root, "T-drift")
    real_parse = transitions_module.parse_semantic_document_bytes
    drifted = False

    def parse_then_drift(path: str, raw: bytes):  # type: ignore[no-untyped-def]
        nonlocal drifted
        parsed = real_parse(path, raw)
        if not drifted:
            drifted = True
            (drift_root / path).write_text("# Replaced during audit\n", encoding="utf-8")
        return parsed

    monkeypatch.setattr(
        transitions_module,
        "parse_semantic_document_bytes",
        parse_then_drift,
    )
    drift_audit = _audit(drift_root, drift_ref)
    assert drift_audit["mechanically_valid"] is False
    assert any("drift" in str(issue["code"]) for issue in drift_audit["issues"])

    runtime_root = tmp_path / "runtime-closure"
    base = _init_workspace(runtime_root)
    (runtime_root / "memory" / "NOW.md").write_text(
        "# Current State\n\n## Findings\n\nChanged.\n",
        encoding="utf-8",
    )
    observation_ref = "runs/RUN-runtime/final.json"
    proposal_ref = _write_proposal(
        runtime_root,
        "T-runtime-closure",
        base_commit=base,
        workspace_paths=["memory/NOW.md"],
        assimilations=[
            {
                "observation_ref": observation_ref,
                "affected_paths": ["memory/NOW.md"],
                "rationale": "memory/NOW.md#Findings",
            }
        ],
    )
    monkeypatch.setattr(
        transitions_module.ObservationCatalog,
        "resolve",
        lambda _self, _ref, **_kwargs: _fake_observation(
            observation_ref,
            kind="run_final",
            versioned_paths=(".aros/runs/RUN-runtime/status.json",),
        ),
    )

    runtime_audit = _audit(runtime_root, proposal_ref)

    assert runtime_audit["mechanically_valid"] is False
    assert b".aros/" not in canonical_json_bytes(
        runtime_audit["observation_closure"]
    )
    assert all(
        not path["path"].startswith(".aros/")
        for record in runtime_audit["observation_closure"]
        for path in record["paths"]
    )
    assert any("runtime" in str(issue["code"]) for issue in runtime_audit["issues"])


def test_unrelated_dirty_paths_do_not_invalidate_audit(tmp_path: Path) -> None:
    proposal_ref = _changed_semantic_proposal(tmp_path, "T-unrelated")
    (tmp_path / "unrelated.tmp").write_text("preserve me\n", encoding="utf-8")

    audit = _audit(tmp_path, proposal_ref)

    assert audit["mechanically_valid"] is True
    assert (tmp_path / "unrelated.tmp").read_text(encoding="utf-8") == "preserve me\n"


@pytest.mark.parametrize(
    ("base_commit", "workspace_paths", "record_sha256"),
    (
        ("not-a-commit", [], "a" * 64),
        ("a" * 40, [".aros/runtime.json"], "b" * 64),
        ("a" * 40, ["runs/RUN-a/final.json"], "not-a-hash"),
    ),
)
def test_operational_proposal_factory_validates_inputs(
    base_commit: str,
    workspace_paths: list[str],
    record_sha256: str,
) -> None:
    with pytest.raises(TransitionError):
        build_operational_proposal(base_commit, workspace_paths, record_sha256)
