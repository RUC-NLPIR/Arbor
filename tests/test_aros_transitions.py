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
import arbor.aros.worktrees as worktrees_module
from arbor.aros.eval import EvalService
from arbor.aros.observations import ObservationRecord
from arbor.aros.store import atomic_write_json, canonical_json_bytes, json_sha256
from arbor.aros.transitions import (
    Assimilation,
    TransitionAuditService,
    TransitionError,
    TransitionProposal,
    build_operational_proposal,
    load_transition_proposal,
)
from tests import test_aros_observations as observation_support
from tests import test_aros_eval as eval_support


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


def _claim_document(
    identifier: str,
    links: list[dict[str, str]],
    *,
    statement: str = "",
) -> str:
    encoded_links = "\n".join(json.dumps(link, sort_keys=True) for link in links)
    return (
        f"---\nid: {identifier}\n---\n# Claim\n\n"
        f"## Statement and scope\n\n{statement}\n\n"
        f"## Evidence links\n\n{encoded_links}\n"
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


def _install_eval_for_candidate(
    root: Path,
    candidate_commit: str,
) -> str:
    scorer = root / "evaluation" / "score.py"
    scorer.parent.mkdir(exist_ok=True)
    scorer.write_bytes(
        b"print('{\"schema_version\":1,\"metric\":0.5,\"sample_count\":1}')\n"
    )
    _git(root, "add", "evaluation/score.py")
    _git(root, "commit", "-qm", "add joint evaluator apparatus")
    apparatus_commit = _git(root, "rev-parse", "HEAD")
    manifest = eval_support._visible_manifest(
        apparatus_commit,
        hashlib.sha256(scorer.read_bytes()).hexdigest(),
    )
    manifest_path = root / "eval" / "suites" / "quality" / "1" / "manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    _git(root, "add", "eval/suites/quality/1/manifest.json")
    _git(root, "commit", "-qm", "add joint evaluator manifest")
    service = EvalService(root)
    service.register("eval/suites/quality/1/manifest.json", actor="registrar")
    lease = service._begin_execution(
        "quality",
        "1",
        candidate_commit,
        "principal",
        "joint-task-measurement",
    )
    assert isinstance(lease, eval_support.eval_module.ExecutionLease)
    receipt = eval_support._terminal_receipt(root, lease.request, lease.execution)
    receipt_ref = (
        f"eval/evaluations/{lease.request['eval_id']}/receipt.json"
    )
    atomic_write_json(root / receipt_ref, receipt)
    lease.close()
    return receipt_ref


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


def test_constructor_and_audit_reject_non_utf8_public_refs_safely(
    tmp_path: Path,
) -> None:
    _init_workspace(tmp_path)
    with pytest.raises(TransitionError, match="UTF-8|canonical_ref"):
        TransitionAuditService(
            tmp_path,
            canonical_ref="refs/heads/\ud800",
        )

    audit = TransitionAuditService(
        tmp_path,
        canonical_ref=_git(tmp_path, "symbolic-ref", "HEAD"),
    ).audit("transitions/T-\ud800/proposal.json")

    assert set(audit) == AUDIT_FIELDS
    assert audit["mechanically_valid"] is False
    assert canonical_json_bytes(audit)


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


@pytest.mark.parametrize("boundary", ("current_nested", "base_gitlink"))
def test_audit_rejects_proposal_under_nested_repository_or_base_gitlink(
    tmp_path: Path,
    boundary: str,
) -> None:
    base = _init_workspace(tmp_path)
    transition_id = f"T-proposal-{boundary.replace('_', '-')}"
    proposal_directory = tmp_path / "transitions" / transition_id
    proposal_directory.mkdir(parents=True)
    _git(proposal_directory, "init", "-q", "-b", "main")
    _git(proposal_directory, "config", "user.email", "nested@example.invalid")
    _git(proposal_directory, "config", "user.name", "Nested Test")
    (proposal_directory / "nested.txt").write_text("nested\n", encoding="utf-8")
    _git(proposal_directory, "add", "nested.txt")
    _git(proposal_directory, "commit", "-qm", "nested proposal repository")
    if boundary == "base_gitlink":
        _git(tmp_path, "add", f"transitions/{transition_id}")
        _git(tmp_path, "commit", "-qm", "record proposal gitlink")
        base = _git(tmp_path, "rev-parse", "HEAD")
        backup = tmp_path / ".worktree" / f"removed-{transition_id}"
        backup.parent.mkdir()
        proposal_directory.rename(backup)
        proposal_directory.mkdir()
    proposal_ref = _write_proposal(
        tmp_path,
        transition_id,
        base_commit=base,
        workspace_paths=[],
    )

    audit = _audit(tmp_path, proposal_ref)

    assert set(audit) == AUDIT_FIELDS
    assert audit["mechanically_valid"] is False
    assert audit["proposal_blob_sha256"] is None
    assert any(
        "proposal" in str(issue["code"])
        and ("submodule" in str(issue["code"]) or "nested" in str(issue["detail"]))
        for issue in audit["issues"]
    )


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


def test_executable_proposal_is_rejected_before_trusting_bytes(tmp_path: Path) -> None:
    proposal_ref = _changed_semantic_proposal(tmp_path, "T-executable-proposal")
    (tmp_path / proposal_ref).chmod(0o755)

    with pytest.raises(TransitionError, match="executable|mode"):
        load_transition_proposal(tmp_path, proposal_ref)

    audit = _audit(tmp_path, proposal_ref)
    assert audit["mechanically_valid"] is False
    assert audit["proposal_blob_sha256"] is None
    assert any("executable" in str(issue["code"]) for issue in audit["issues"])


def test_chmod_only_semantic_change_binds_mode_and_is_rejected(
    tmp_path: Path,
) -> None:
    base = _init_workspace(tmp_path)
    path = tmp_path / "memory" / "NOW.md"
    path.chmod(0o755)
    proposal_ref = _write_proposal(
        tmp_path,
        "T-chmod-only",
        base_commit=base,
        workspace_paths=["memory/NOW.md"],
    )

    audit = _audit(tmp_path, proposal_ref)

    assert audit["mechanically_valid"] is False
    assert audit["path_receipts"][0]["mode"] == "100755"
    assert any("executable" in str(issue["code"]) for issue in audit["issues"])
    assert not any(
        issue["code"] == "semantic_path_unchanged" for issue in audit["issues"]
    )


def test_executable_service_and_closure_files_bind_mode_and_fail(
    tmp_path: Path,
) -> None:
    _service, manifest, _final = observation_support._install_run_final(tmp_path)
    base = _git(tmp_path, "rev-parse", "HEAD")
    run_id = str(manifest["run_id"])
    manifest_ref = f"runs/{run_id}/manifest.json"
    final_ref = f"runs/{run_id}/final.json"
    (tmp_path / final_ref).chmod(0o755)
    (tmp_path / manifest_ref).chmod(0o755)
    proposal_ref = _write_proposal(
        tmp_path,
        "T-executable-service",
        base_commit=base,
        workspace_paths=[final_ref],
    )

    audit = _audit(tmp_path, proposal_ref)

    assert audit["mechanically_valid"] is False
    assert audit["path_receipts"][0]["mode"] == "100755"
    closure_paths = {
        path["path"]: path
        for record in audit["observation_closure"]
        for path in record["paths"]
    }
    assert closure_paths[manifest_ref]["mode"] == "100755"
    assert closure_paths[final_ref]["mode"] == "100755"
    assert any("executable" in str(issue["code"]) for issue in audit["issues"])


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


@pytest.mark.parametrize("kind", ("run_final", "eval_outcome"))
def test_process_and_eval_outcomes_can_only_link_as_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
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
    observation_ref = (
        "runs/RUN-process-context/final.json"
        if kind == "run_final"
        else f"eval/evaluations/EVAL-{'a' * 64}/receipt.json"
    )
    claim.write_text(
        "---\nid: C-apparatus\n---\n# Claim\n\n## Evidence links\n\n"
        + json.dumps(
            {
                "observation_ref": observation_ref,
                "relation": "supports",
                "scope": "Process or invalid evaluation cannot support the claim.",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    proposal_ref = _write_proposal(
        tmp_path,
        f"T-{kind.replace('_', '-')}-context",
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
            kind=kind,
            candidate_commit="c" * 40,
            measurement_state=("invalid_eval" if kind == "eval_outcome" else None),
        ),
    )

    audit = _audit(tmp_path, proposal_ref)

    assert audit["mechanically_valid"] is False
    assert audit["observation_closure"][0]["kind"] == kind
    assert any("nonmeasurement" in str(issue["code"]) for issue in audit["issues"])


def test_audit_binds_only_new_evidence_link_delta(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_workspace(tmp_path)
    claim = tmp_path / "knowledge" / "claims" / "C-delta.md"
    claim.parent.mkdir(parents=True)
    existing_ref = "runs/RUN-existing/final.json"
    new_ref = f"eval/evaluations/EVAL-{'3' * 64}/receipt.json"
    existing = {
        "observation_ref": existing_ref,
        "relation": "context",
        "scope": "Existing apparatus context.",
    }
    added = {
        "observation_ref": new_ref,
        "relation": "supports",
        "scope": "New bounded measurement.",
    }
    claim.write_text(_claim_document("C-delta", [existing]), encoding="utf-8")
    _git(tmp_path, "add", "knowledge/claims/C-delta.md")
    _git(tmp_path, "commit", "-qm", "add existing EvidenceLink")
    base = _git(tmp_path, "rev-parse", "HEAD")
    claim.write_text(
        _claim_document("C-delta", [existing, added], statement="Updated."),
        encoding="utf-8",
    )
    proposal_ref = _write_proposal(
        tmp_path,
        "T-evidence-delta",
        base_commit=base,
        workspace_paths=["knowledge/claims/C-delta.md"],
        assimilations=[
            {
                "observation_ref": new_ref,
                "affected_paths": ["knowledge/claims/C-delta.md"],
                "rationale": "knowledge/claims/C-delta.md#Evidence links",
            }
        ],
    )
    monkeypatch.setattr(
        transitions_module.ObservationCatalog,
        "resolve",
        lambda _self, _ref, **_kwargs: _fake_observation(
            new_ref,
            kind="measurement",
            candidate_commit="3" * 40,
            measurement_state="valid",
        ),
    )

    audit = _audit(tmp_path, proposal_ref)

    assert audit["mechanically_valid"] is True
    assert [link["observation_ref"] for link in audit["assimilation_links"]] == [
        new_ref
    ]


def test_new_evidence_link_without_declared_assimilation_is_invalid(
    tmp_path: Path,
) -> None:
    _init_workspace(tmp_path)
    claim = tmp_path / "knowledge" / "claims" / "C-undeclared-link.md"
    claim.parent.mkdir(parents=True)
    claim.write_text(_claim_document("C-undeclared-link", []), encoding="utf-8")
    _git(tmp_path, "add", "knowledge/claims/C-undeclared-link.md")
    _git(tmp_path, "commit", "-qm", "add undeclared link claim")
    base = _git(tmp_path, "rev-parse", "HEAD")
    claim.write_text(
        _claim_document(
            "C-undeclared-link",
            [
                {
                    "observation_ref": "tasks/TASK-20260805-forged/collected.json",
                    "relation": "supports",
                    "scope": "Forged evidence bypass.",
                }
            ],
        ),
        encoding="utf-8",
    )
    proposal_ref = _write_proposal(
        tmp_path,
        "T-undeclared-link",
        base_commit=base,
        workspace_paths=["knowledge/claims/C-undeclared-link.md"],
    )

    audit = _audit(tmp_path, proposal_ref)

    assert audit["mechanically_valid"] is False
    assert audit["assimilation_links"] == []
    assert any("evidence_delta" in str(issue["code"]) for issue in audit["issues"])


def test_evidence_link_reorder_is_an_ordinal_delta_requiring_assimilation(
    tmp_path: Path,
) -> None:
    _init_workspace(tmp_path)
    claim = tmp_path / "knowledge" / "claims" / "C-reorder.md"
    claim.parent.mkdir(parents=True)
    first = {
        "observation_ref": "tasks/TASK-20260805-first/collected.json",
        "relation": "context",
        "scope": "First link.",
    }
    second = {
        "observation_ref": "tasks/TASK-20260805-second/collected.json",
        "relation": "context",
        "scope": "Second link.",
    }
    claim.write_text(
        _claim_document("C-reorder", [first, second]),
        encoding="utf-8",
    )
    _git(tmp_path, "add", "knowledge/claims/C-reorder.md")
    _git(tmp_path, "commit", "-qm", "record EvidenceLink order")
    base = _git(tmp_path, "rev-parse", "HEAD")
    claim.write_text(
        _claim_document("C-reorder", [second, first]),
        encoding="utf-8",
    )
    proposal_ref = _write_proposal(
        tmp_path,
        "T-reordered-links",
        base_commit=base,
        workspace_paths=["knowledge/claims/C-reorder.md"],
    )

    audit = _audit(tmp_path, proposal_ref)

    assert audit["mechanically_valid"] is False
    deltas = [
        issue for issue in audit["issues"] if "evidence_delta" in issue["code"]
    ]
    assert len(deltas) == 2


def test_valid_current_claim_can_repair_invalid_base_semantic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_workspace(tmp_path)
    claim = tmp_path / "knowledge" / "claims" / "C-repair.md"
    claim.parent.mkdir(parents=True)
    claim.write_text(
        "---\nid: C-repair\n---\n# Claim\n\n## Evidence links\n\n{invalid}\n",
        encoding="utf-8",
    )
    _git(tmp_path, "add", "knowledge/claims/C-repair.md")
    _git(tmp_path, "commit", "-qm", "record invalid historical Claim")
    base = _git(tmp_path, "rev-parse", "HEAD")
    observation_ref = f"eval/evaluations/EVAL-{'9' * 64}/receipt.json"
    claim.write_text(
        _claim_document(
            "C-repair",
            [
                {
                    "observation_ref": observation_ref,
                    "relation": "supports",
                    "scope": "Repair with explicit measurement.",
                }
            ],
            statement="Repaired current Claim.",
        ),
        encoding="utf-8",
    )
    proposal_ref = _write_proposal(
        tmp_path,
        "T-repair-base",
        base_commit=base,
        workspace_paths=["knowledge/claims/C-repair.md"],
        assimilations=[
            {
                "observation_ref": observation_ref,
                "affected_paths": ["knowledge/claims/C-repair.md"],
                "rationale": "knowledge/claims/C-repair.md#Evidence links",
            }
        ],
    )
    monkeypatch.setattr(
        transitions_module.ObservationCatalog,
        "resolve",
        lambda _self, _ref, **_kwargs: _fake_observation(
            observation_ref,
            kind="measurement",
            candidate_commit="9" * 40,
            measurement_state="valid",
        ),
    )

    audit = _audit(tmp_path, proposal_ref)

    assert audit["mechanically_valid"] is True
    assert len(audit["assimilation_links"]) == 1
    assert any(
        issue["severity"] == "warning" and issue["code"] == "invalid_base_semantic"
        for issue in audit["issues"]
    )


def test_two_assimilations_share_evidence_section_without_cross_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_workspace(tmp_path)
    claim = tmp_path / "knowledge" / "claims" / "C-shared.md"
    claim.parent.mkdir(parents=True)
    claim.write_text(_claim_document("C-shared", []), encoding="utf-8")
    _git(tmp_path, "add", "knowledge/claims/C-shared.md")
    _git(tmp_path, "commit", "-qm", "add shared EvidenceLink section")
    base = _git(tmp_path, "rev-parse", "HEAD")
    candidate = "4" * 40
    eval_ref = f"eval/evaluations/EVAL-{'4' * 64}/receipt.json"
    task_ref = "tasks/TASK-20260805-shared/collected.json"
    claim.write_text(
        _claim_document(
            "C-shared",
            [
                {
                    "observation_ref": eval_ref,
                    "relation": "supports",
                    "scope": "Measurement delta.",
                },
                {
                    "observation_ref": task_ref,
                    "relation": "context",
                    "scope": "Task process context.",
                },
            ],
        ),
        encoding="utf-8",
    )
    records = {
        eval_ref: _fake_observation(
            eval_ref,
            kind="measurement",
            candidate_commit=candidate,
            measurement_state="valid",
        ),
        task_ref: _fake_observation(
            task_ref,
            candidate_commit=candidate,
            base_commit=base,
        ),
    }
    proposal_ref = _write_proposal(
        tmp_path,
        "T-shared-links",
        base_commit=base,
        workspace_paths=["knowledge/claims/C-shared.md"],
        assimilations=[
            {
                "observation_ref": eval_ref,
                "affected_paths": ["knowledge/claims/C-shared.md"],
                "rationale": "knowledge/claims/C-shared.md#Evidence links",
            },
            {
                "observation_ref": task_ref,
                "affected_paths": ["knowledge/claims/C-shared.md"],
                "rationale": "knowledge/claims/C-shared.md#Evidence links",
            },
        ],
    )
    monkeypatch.setattr(
        transitions_module.ObservationCatalog,
        "resolve",
        lambda _self, ref, **_kwargs: records[ref],
    )

    audit = _audit(tmp_path, proposal_ref)

    assert audit["mechanically_valid"] is True
    assert {link["observation_ref"] for link in audit["assimilation_links"]} == {
        eval_ref,
        task_ref,
    }
    assert not any("mismatch" in str(issue["code"]) for issue in audit["issues"])


def test_existing_measurement_link_alone_cannot_support_new_assimilation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_workspace(tmp_path)
    claim = tmp_path / "knowledge" / "claims" / "C-old-link.md"
    claim.parent.mkdir(parents=True)
    observation_ref = f"eval/evaluations/EVAL-{'5' * 64}/receipt.json"
    link = {
        "observation_ref": observation_ref,
        "relation": "supports",
        "scope": "Previously assimilated measurement.",
    }
    claim.write_text(_claim_document("C-old-link", [link]), encoding="utf-8")
    _git(tmp_path, "add", "knowledge/claims/C-old-link.md")
    _git(tmp_path, "commit", "-qm", "record old measurement link")
    base = _git(tmp_path, "rev-parse", "HEAD")
    claim.write_text(
        _claim_document("C-old-link", [link], statement="Changed prose only."),
        encoding="utf-8",
    )
    proposal_ref = _write_proposal(
        tmp_path,
        "T-old-link",
        base_commit=base,
        workspace_paths=["knowledge/claims/C-old-link.md"],
        assimilations=[
            {
                "observation_ref": observation_ref,
                "affected_paths": ["knowledge/claims/C-old-link.md"],
                "rationale": "knowledge/claims/C-old-link.md#Evidence links",
            }
        ],
    )
    monkeypatch.setattr(
        transitions_module.ObservationCatalog,
        "resolve",
        lambda _self, _ref, **_kwargs: _fake_observation(
            observation_ref,
            kind="measurement",
            candidate_commit="5" * 40,
            measurement_state="valid",
        ),
    )

    audit = _audit(tmp_path, proposal_ref)

    assert audit["mechanically_valid"] is False
    assert audit["assimilation_links"] == []
    assert any("link_missing" in str(issue["code"]) for issue in audit["issues"])


def test_changed_evidence_link_scope_is_a_new_delta(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_workspace(tmp_path)
    claim = tmp_path / "knowledge" / "claims" / "C-changed-link.md"
    claim.parent.mkdir(parents=True)
    observation_ref = f"eval/evaluations/EVAL-{'6' * 64}/receipt.json"
    old = {
        "observation_ref": observation_ref,
        "relation": "supports",
        "scope": "Old scope.",
    }
    new = {**old, "scope": "New narrower scope."}
    claim.write_text(_claim_document("C-changed-link", [old]), encoding="utf-8")
    _git(tmp_path, "add", "knowledge/claims/C-changed-link.md")
    _git(tmp_path, "commit", "-qm", "record old link scope")
    base = _git(tmp_path, "rev-parse", "HEAD")
    claim.write_text(_claim_document("C-changed-link", [new]), encoding="utf-8")
    proposal_ref = _write_proposal(
        tmp_path,
        "T-changed-link",
        base_commit=base,
        workspace_paths=["knowledge/claims/C-changed-link.md"],
        assimilations=[
            {
                "observation_ref": observation_ref,
                "affected_paths": ["knowledge/claims/C-changed-link.md"],
                "rationale": "knowledge/claims/C-changed-link.md#Evidence links",
            }
        ],
    )
    monkeypatch.setattr(
        transitions_module.ObservationCatalog,
        "resolve",
        lambda _self, _ref, **_kwargs: _fake_observation(
            observation_ref,
            kind="measurement",
            candidate_commit="6" * 40,
            measurement_state="valid",
        ),
    )

    audit = _audit(tmp_path, proposal_ref)

    assert audit["mechanically_valid"] is True
    assert [link["scope"] for link in audit["assimilation_links"]] == [
        "New narrower scope."
    ]


@pytest.mark.parametrize(
    "relation",
    ("supports", "challenges", "bounds", "context"),
)
def test_task_evidence_links_preserve_owner_valid_relation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relation: str,
) -> None:
    _init_workspace(tmp_path)
    claim = tmp_path / "knowledge" / "claims" / "C-task-relation.md"
    claim.parent.mkdir(parents=True)
    claim.write_text(_claim_document("C-task-relation", []), encoding="utf-8")
    _git(tmp_path, "add", "knowledge/claims/C-task-relation.md")
    _git(tmp_path, "commit", "-qm", "add Task relation claim")
    base = _git(tmp_path, "rev-parse", "HEAD")
    observation_ref = "tasks/TASK-20260805-relation/collected.json"
    claim.write_text(
        _claim_document(
            "C-task-relation",
            [
                {
                    "observation_ref": observation_ref,
                    "relation": relation,
                    "scope": "Explicitly assimilated Task return.",
                }
            ],
        ),
        encoding="utf-8",
    )
    proposal_ref = _write_proposal(
        tmp_path,
        f"T-task-{relation}",
        base_commit=base,
        workspace_paths=["knowledge/claims/C-task-relation.md"],
        assimilations=[
            {
                "observation_ref": observation_ref,
                "affected_paths": ["knowledge/claims/C-task-relation.md"],
                "rationale": "knowledge/claims/C-task-relation.md#Evidence links",
            }
        ],
    )
    monkeypatch.setattr(
        transitions_module.ObservationCatalog,
        "resolve",
        lambda _self, _ref, **_kwargs: _fake_observation(
            observation_ref,
            base_commit=base,
        ),
    )

    audit = _audit(tmp_path, proposal_ref)

    assert audit["mechanically_valid"] is True
    assert [link["relation"] for link in audit["assimilation_links"]] == [relation]
    assert not any("nonmeasurement" in str(issue["code"]) for issue in audit["issues"])


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


def test_direct_terminal_service_records_derive_complete_owner_closure(
    tmp_path: Path,
) -> None:
    task_root = tmp_path / "task"
    _service, task_id, _collected = observation_support._collected_task(task_root)
    task_ref = f"tasks/{task_id}/collected.json"
    task_proposal = _write_proposal(
        task_root,
        "T-direct-task",
        base_commit=_git(task_root, "rev-parse", "HEAD"),
        workspace_paths=[task_ref],
    )

    run_root = tmp_path / "run"
    _run_service, manifest, _final = observation_support._install_run_final(run_root)
    run_id = str(manifest["run_id"])
    run_ref = f"runs/{run_id}/final.json"
    run_proposal = _write_proposal(
        run_root,
        "T-direct-run",
        base_commit=_git(run_root, "rev-parse", "HEAD"),
        workspace_paths=[run_ref],
    )

    eval_root = tmp_path / "eval"
    installed = observation_support._install_eval_receipt(eval_root)
    eval_ref = str(installed["receipt_ref"])
    eval_proposal = _write_proposal(
        eval_root,
        "T-direct-eval",
        base_commit=_git(eval_root, "rev-parse", "HEAD"),
        workspace_paths=[eval_ref],
    )

    cases = (
        (
            _audit(task_root, task_proposal),
            task_ref,
            {f"tasks/{task_id}/brief.json", task_ref},
        ),
        (
            _audit(run_root, run_proposal),
            run_ref,
            {f"runs/{run_id}/manifest.json", run_ref},
        ),
        (
            _audit(eval_root, eval_proposal),
            eval_ref,
            {
                eval_ref,
                f"runs/{installed['run_id']}/manifest.json",
                f"runs/{installed['run_id']}/final.json",
            },
        ),
    )
    for audit, selected_ref, expected_paths in cases:
        assert audit["mechanically_valid"] is True
        assert len(audit["observation_closure"]) == 1
        closure = audit["observation_closure"][0]
        assert closure["observation_ref"] == selected_ref
        paths = {item["path"]: item for item in closure["paths"]}
        assert set(paths) == expected_paths
        assert paths[selected_ref]["state"] == "workspace"
        assert len(paths) == len(set(paths))


def test_overlapping_direct_observations_emit_each_closure_receipt_once(
    tmp_path: Path,
) -> None:
    installed = observation_support._install_eval_receipt(tmp_path)
    eval_ref = str(installed["receipt_ref"])
    run_ref = f"runs/{installed['run_id']}/final.json"
    proposal_ref = _write_proposal(
        tmp_path,
        "T-overlapping-closure",
        base_commit=_git(tmp_path, "rev-parse", "HEAD"),
        workspace_paths=sorted([eval_ref, run_ref]),
    )

    audit = _audit(tmp_path, proposal_ref)

    assert audit["mechanically_valid"] is True
    closure = {item["observation_ref"]: item for item in audit["observation_closure"]}
    assert set(closure) == {eval_ref, run_ref}
    assert set(closure[eval_ref]["versioned_paths"]) == {
        eval_ref,
        f"runs/{installed['run_id']}/manifest.json",
        run_ref,
    }
    assert set(closure[run_ref]["versioned_paths"]) == {
        f"runs/{installed['run_id']}/manifest.json",
        run_ref,
    }
    receipts = [
        path["path"]
        for record in audit["observation_closure"]
        for path in record["paths"]
    ]
    assert len(receipts) == len(set(receipts))


def test_preterminal_task_brief_and_run_manifest_need_no_terminal_closure(
    tmp_path: Path,
) -> None:
    task_root = tmp_path / "task"
    _service, brief, _ownership, _final = (
        observation_support.task_test_support._create_terminal_task(task_root)
    )
    task_ref = f"tasks/{brief['task_id']}/brief.json"
    task_proposal = _write_proposal(
        task_root,
        "T-preterminal-task",
        base_commit=_git(task_root, "rev-parse", "HEAD"),
        workspace_paths=[task_ref],
    )

    run_root = tmp_path / "run"
    _run_service, manifest, _run_final = observation_support._install_run_final(
        run_root
    )
    run_id = str(manifest["run_id"])
    run_ref = f"runs/{run_id}/manifest.json"
    (run_root / "runs" / run_id / "final.json").unlink()
    run_proposal = _write_proposal(
        run_root,
        "T-preterminal-run",
        base_commit=_git(run_root, "rev-parse", "HEAD"),
        workspace_paths=[run_ref],
    )

    for audit in (
        _audit(task_root, task_proposal),
        _audit(run_root, run_proposal),
    ):
        assert audit["mechanically_valid"] is True
        assert audit["observation_closure"] == []


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


def test_two_task_measurement_candidate_groups_do_not_cross_compare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _init_workspace(tmp_path)
    paths = {
        "memory/PAIR-A.md": "Findings",
        "memory/PAIR-B.md": "Findings",
        "model/PAIR-A.md": "Measurement",
        "model/PAIR-B.md": "Measurement",
    }
    for path, heading in paths.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# Record\n\n## {heading}\n\nChanged.\n", encoding="utf-8")
    eval_a = f"eval/evaluations/EVAL-{'7' * 64}/receipt.json"
    eval_b = f"eval/evaluations/EVAL-{'8' * 64}/receipt.json"
    task_a = "tasks/TASK-20260805-pair-a/collected.json"
    task_b = "tasks/TASK-20260805-pair-b/collected.json"
    candidate_a = "7" * 40
    candidate_b = "8" * 40
    records = {
        eval_a: _fake_observation(
            eval_a,
            kind="measurement",
            candidate_commit=candidate_a,
            measurement_state="valid",
        ),
        eval_b: _fake_observation(
            eval_b,
            kind="measurement",
            candidate_commit=candidate_b,
            measurement_state="underpowered",
        ),
        task_a: _fake_observation(
            task_a,
            candidate_commit=candidate_a,
            base_commit=base,
        ),
        task_b: _fake_observation(
            task_b,
            candidate_commit=candidate_b,
            base_commit=base,
        ),
    }
    proposal_ref = _write_proposal(
        tmp_path,
        "T-two-pairs",
        base_commit=base,
        workspace_paths=sorted(paths),
        assimilations=[
            {
                "observation_ref": eval_a,
                "affected_paths": ["model/PAIR-A.md"],
                "rationale": "model/PAIR-A.md#Measurement",
            },
            {
                "observation_ref": eval_b,
                "affected_paths": ["model/PAIR-B.md"],
                "rationale": "model/PAIR-B.md#Measurement",
            },
            {
                "observation_ref": task_a,
                "affected_paths": ["memory/PAIR-A.md"],
                "rationale": "memory/PAIR-A.md#Findings",
            },
            {
                "observation_ref": task_b,
                "affected_paths": ["memory/PAIR-B.md"],
                "rationale": "memory/PAIR-B.md#Findings",
            },
        ],
    )
    monkeypatch.setattr(
        transitions_module.ObservationCatalog,
        "resolve",
        lambda _self, ref, **_kwargs: records[ref],
    )

    audit = _audit(tmp_path, proposal_ref)

    assert audit["mechanically_valid"] is True
    assert not any("candidate_mismatch" in str(issue["code"]) for issue in audit["issues"])


@eval_support.requires_linux_claims
def test_real_task_collection_and_eval_receipt_form_joint_lineage(
    tmp_path: Path,
) -> None:
    _service, task_id, collected = observation_support._collected_task(tmp_path)
    task_ref = f"tasks/{task_id}/collected.json"
    _git(tmp_path, "add", task_ref)
    _git(tmp_path, "commit", "-qm", "record real Task collection")
    eval_ref = _install_eval_for_candidate(
        tmp_path,
        str(collected["child_commit"]),
    )
    base = _git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "memory" / "NOW.md").write_text(
        "# Current State\n\n## Findings\n\nReal Task return.\n",
        encoding="utf-8",
    )
    (tmp_path / "model" / "CURRENT.md").write_text(
        "# Current Model\n\n## Measurement\n\nReal measurement.\n",
        encoding="utf-8",
    )
    proposal_ref = _write_proposal(
        tmp_path,
        "T-real-joint",
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

    audit = _audit(tmp_path, proposal_ref)

    assert audit["mechanically_valid"] is True
    closure = {item["observation_ref"]: item for item in audit["observation_closure"]}
    assert set(closure) == {task_ref, eval_ref}
    assert closure[task_ref]["candidate_commit"] == collected["child_commit"]
    assert closure[eval_ref]["candidate_commit"] == collected["child_commit"]


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


def test_transition_input_limits_are_explicit_and_enforced(tmp_path: Path) -> None:
    assert transitions_module.MAX_PROPOSAL_BYTES == 1_048_576
    assert transitions_module.MAX_WORKSPACE_PATHS == 256
    assert transitions_module.MAX_ASSIMILATIONS == 256
    assert transitions_module.MAX_AFFECTED_PATHS == 256
    assert transitions_module.MAX_PATH_BYTES == 1_024
    assert transitions_module.MAX_REFERENCE_BYTES == 1_024
    assert transitions_module.MAX_RATIONALE_BYTES == 4_096
    assert transitions_module.MAX_EVIDENCE_SCOPE_BYTES == 4_096
    assert transitions_module.MAX_OBSERVATION_CLOSURE_PATHS == 1_024

    base = _init_workspace(tmp_path)
    oversized_ref = _write_proposal(
        tmp_path,
        "T-oversized",
        base_commit=base,
        workspace_paths=[],
    )
    (tmp_path / oversized_ref).write_bytes(
        b" " * (transitions_module.MAX_PROPOSAL_BYTES + 1)
    )
    with pytest.raises(TransitionError, match="size|bytes|large"):
        load_transition_proposal(tmp_path, oversized_ref)
    oversized_audit = _audit(tmp_path, oversized_ref)
    assert oversized_audit["mechanically_valid"] is False
    assert oversized_audit["proposal_blob_sha256"] is None

    workspace_ref = _write_proposal(
        tmp_path,
        "T-too-many-paths",
        base_commit=base,
        workspace_paths=[
            f"memory/P-{index:03d}.md"
            for index in range(transitions_module.MAX_WORKSPACE_PATHS + 1)
        ],
    )
    with pytest.raises(TransitionError, match="workspace_paths|256"):
        load_transition_proposal(tmp_path, workspace_ref)

    assimilation_ref = _write_proposal(
        tmp_path,
        "T-too-many-assimilations",
        base_commit=base,
        workspace_paths=["memory/NOW.md"],
        assimilations=[
            {
                "observation_ref": (
                    f"tasks/TASK-20260805-b{index:03d}/collected.json"
                ),
                "affected_paths": ["memory/NOW.md"],
                "rationale": "memory/NOW.md#Findings",
            }
            for index in range(transitions_module.MAX_ASSIMILATIONS + 1)
        ],
    )
    with pytest.raises(TransitionError, match="assimilations|256"):
        load_transition_proposal(tmp_path, assimilation_ref)

    affected_ref = _write_proposal(
        tmp_path,
        "T-too-many-affected",
        base_commit=base,
        workspace_paths=["memory/NOW.md"],
        assimilations=[
            {
                "observation_ref": "tasks/TASK-20260805-bounded/collected.json",
                "affected_paths": [
                    f"memory/A-{index:03d}.md"
                    for index in range(transitions_module.MAX_AFFECTED_PATHS + 1)
                ],
                "rationale": "memory/A-000.md#Findings",
            }
        ],
    )
    with pytest.raises(TransitionError, match="affected_paths|256"):
        load_transition_proposal(tmp_path, affected_ref)


@pytest.mark.parametrize("field", ("path", "reference", "rationale"))
def test_transition_string_limits_use_utf8_bytes(
    tmp_path: Path,
    field: str,
) -> None:
    base = _init_workspace(tmp_path)
    workspace_paths = ["memory/NOW.md"]
    observation_ref = "tasks/TASK-20260805-string/collected.json"
    rationale = "memory/NOW.md#Findings"
    if field == "path":
        workspace_paths = [
            "memory/" + "x" * transitions_module.MAX_PATH_BYTES + ".md"
        ]
    elif field == "reference":
        observation_ref = (
            "tasks/TASK-20260805-"
            + "r" * transitions_module.MAX_REFERENCE_BYTES
            + "/collected.json"
        )
    else:
        rationale = (
            "memory/NOW.md#"
            + "H" * transitions_module.MAX_RATIONALE_BYTES
        )
    proposal_ref = _write_proposal(
        tmp_path,
        f"T-long-{field}",
        base_commit=base,
        workspace_paths=workspace_paths,
        assimilations=(
            []
            if field == "path"
            else [
                {
                    "observation_ref": observation_ref,
                    "affected_paths": ["memory/NOW.md"],
                    "rationale": rationale,
                }
            ]
        ),
    )

    with pytest.raises(TransitionError, match="long|bytes|1024|4096"):
        load_transition_proposal(tmp_path, proposal_ref)


def test_evidence_scope_is_bounded_without_bounding_semantic_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_workspace(tmp_path)
    claim = tmp_path / "knowledge" / "claims" / "C-scope-bound.md"
    claim.parent.mkdir(parents=True)
    claim.write_text(_claim_document("C-scope-bound", []), encoding="utf-8")
    large = tmp_path / "memory" / "LARGE.md"
    large.write_text("# Large\n\nInitial.\n", encoding="utf-8")
    _git(tmp_path, "add", "knowledge/claims/C-scope-bound.md", "memory/LARGE.md")
    _git(tmp_path, "commit", "-qm", "add bounded link and large semantic files")
    base = _git(tmp_path, "rev-parse", "HEAD")
    observation_ref = "tasks/TASK-20260805-scope/collected.json"
    claim.write_text(
        _claim_document(
            "C-scope-bound",
            [
                {
                    "observation_ref": observation_ref,
                    "relation": "context",
                    "scope": "s"
                    * (transitions_module.MAX_EVIDENCE_SCOPE_BYTES + 1),
                }
            ],
        ),
        encoding="utf-8",
    )
    large.write_text(
        "# Large\n\n" + "content\n" * 150_000,
        encoding="utf-8",
    )
    proposal_ref = _write_proposal(
        tmp_path,
        "T-scope-bound",
        base_commit=base,
        workspace_paths=[
            "knowledge/claims/C-scope-bound.md",
            "memory/LARGE.md",
        ],
        assimilations=[
            {
                "observation_ref": observation_ref,
                "affected_paths": ["knowledge/claims/C-scope-bound.md"],
                "rationale": "knowledge/claims/C-scope-bound.md#Evidence links",
            }
        ],
    )
    monkeypatch.setattr(
        transitions_module.ObservationCatalog,
        "resolve",
        lambda _self, _ref, **_kwargs: _fake_observation(
            observation_ref,
            base_commit=base,
        ),
    )

    audit = _audit(tmp_path, proposal_ref)

    assert audit["mechanically_valid"] is False
    assert any("scope" in str(issue["code"]) for issue in audit["issues"])
    assert any(
        receipt["path"] == "memory/LARGE.md" for receipt in audit["path_receipts"]
    )


def test_observation_closure_path_count_is_bounded_before_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _init_workspace(tmp_path)
    (tmp_path / "memory" / "NOW.md").write_text(
        "# Current State\n\n## Findings\n\nBounded closure.\n",
        encoding="utf-8",
    )
    observation_ref = "runs/RUN-bounded/final.json"
    proposal_ref = _write_proposal(
        tmp_path,
        "T-closure-bound",
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
            versioned_paths=tuple(
                f".aros/runs/RUN-{index}/status.json"
                for index in range(
                    transitions_module.MAX_OBSERVATION_CLOSURE_PATHS + 1
                )
            ),
        ),
    )

    audit = _audit(tmp_path, proposal_ref)

    assert audit["mechanically_valid"] is False
    assert any("closure_limit" in str(issue["code"]) for issue in audit["issues"])
    assert len(audit["issues"]) < 10


def test_audit_reuses_batched_base_tree_projection_for_many_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _init_workspace(tmp_path)
    paths: list[str] = []
    for index in range(200):
        relative = f"memory/batch/P-{index:03d}.md"
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# Batch {index}\n", encoding="utf-8")
        paths.append(relative)
    proposal_ref = _write_proposal(
        tmp_path,
        "T-batched-tree",
        base_commit=base,
        workspace_paths=paths,
    )
    calls: list[tuple[str, ...]] = []
    real_read = worktrees_module.read_repository_tree_entries

    def recording_read(
        repository: object,
        commit: str,
        requested: list[str] | tuple[str, ...] | set[str],
    ):  # type: ignore[no-untyped-def]
        calls.append(tuple(requested))
        return real_read(repository, commit, requested)  # type: ignore[arg-type]

    monkeypatch.setattr(
        worktrees_module,
        "read_repository_tree_entries",
        recording_read,
    )

    audit = _audit(tmp_path, proposal_ref)

    assert audit["mechanically_valid"] is True
    assert len(calls) <= 3
    assert any(len(call) >= len(paths) for call in calls)


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
