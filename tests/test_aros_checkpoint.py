"""Audited, no-ref checkpoint candidate preparation."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

import arbor.aros.checkpoint as checkpoint_module
import arbor.aros.worktrees as worktrees_module
from arbor.aros.checkpoint import CheckpointError, CheckpointService
from arbor.aros.worktrees import bind_repository
from tests import test_aros_observations as observation_support
from tests import test_aros_tasks as task_support


def _git(
    root: Path,
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("GIT_", "LD_", "DYLD_"))
    }
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=check,
        capture_output=True,
        env=environment,
    )


def _git_text(root: Path, *args: str) -> str:
    return _git(root, *args).stdout.decode("utf-8").strip()


def _init_repository(root: Path) -> tuple[str, str]:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "checkpoint@example.invalid")
    _git(root, "config", "user.name", "Checkpoint Test")
    (root / ".gitignore").write_text("/.aros/\n/.worktree/\n", encoding="utf-8")
    (root / "memory").mkdir()
    (root / "memory" / "NOW.md").write_text(
        "# Current State\n\n## Findings\n\nInitial finding.\n",
        encoding="utf-8",
    )
    (root / "unrelated.txt").write_text("base unrelated\n", encoding="utf-8")
    _git(root, "add", ".gitignore", "memory/NOW.md", "unrelated.txt")
    _git(root, "commit", "-qm", "initial state")
    return _git_text(root, "rev-parse", "HEAD"), "refs/heads/main"


def _write_proposal(
    root: Path,
    transition_id: str,
    base_commit: str,
    workspace_paths: list[str],
    assimilations: list[dict[str, object]] | None = None,
) -> str:
    relative = f"transitions/{transition_id}/proposal.json"
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "base_commit": base_commit,
                "workspace_paths": workspace_paths,
                "assimilations": assimilations or [],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return relative


def _valid_service(
    root: Path,
    transition_id: str = "T-checkpoint",
) -> tuple[CheckpointService, str, str]:
    base, canonical_ref = _init_repository(root)
    (root / "memory" / "NOW.md").write_text(
        "# Current State\n\n## Findings\n\nAudited finding.\n",
        encoding="utf-8",
    )
    proposal_ref = _write_proposal(
        root,
        transition_id,
        base,
        ["memory/NOW.md"],
    )
    return (
        CheckpointService(
            root,
            canonical_repository=bind_repository(root),
            canonical_ref=canonical_ref,
        ),
        proposal_ref,
        base,
    )


def _index_bytes(root: Path) -> bytes:
    return (Path(_git_text(root, "rev-parse", "--absolute-git-dir")) / "index").read_bytes()


def _authority(root: Path) -> tuple[bytes, bytes, bytes | None]:
    git_dir = Path(_git_text(root, "rev-parse", "--absolute-git-dir"))
    fetch_head = git_dir / "FETCH_HEAD"
    return (
        _git(root, "rev-parse", "HEAD").stdout,
        _git(root, "for-each-ref", "--format=%(refname)%00%(objectname)").stdout,
        fetch_head.read_bytes() if fetch_head.exists() else None,
    )


def _tree(root: Path, tree_oid: str) -> dict[str, tuple[str, str]]:
    entries: dict[str, tuple[str, str]] = {}
    for record in _git(root, "ls-tree", "-rz", "--full-tree", tree_oid).stdout.split(b"\0"):
        if not record:
            continue
        header, raw_path = record.split(b"\t", 1)
        mode, kind, oid = header.decode("ascii").split(" ")
        assert kind == "blob"
        entries[raw_path.decode("utf-8")] = (mode, oid)
    return entries


def _blob(root: Path, tree_oid: str, path: str) -> bytes:
    return _git(root, "show", f"{tree_oid}:{path}").stdout


def test_checkpoint_prepare_never_uses_or_changes_user_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, proposal_ref, _base = _valid_service(tmp_path)
    before = _index_bytes(tmp_path)
    calls: list[tuple[tuple[str, ...], Path | None]] = []
    real_git_result = worktrees_module._git_result

    def recording_git_result(
        repository: object,
        *args: str,
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        index_file = kwargs.get("index_file")
        calls.append((args, index_file if isinstance(index_file, Path) else None))
        return real_git_result(repository, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(worktrees_module, "_git_result", recording_git_result)

    prepared = service.prepare(proposal_ref, "exact message")

    assert _index_bytes(tmp_path) == before
    ordinary_index = bind_repository(tmp_path).git_dir / "index"
    assert all(index_file != ordinary_index for _args, index_file in calls)
    prepared_index = tmp_path / prepared.index_ref
    for args, index_file in calls:
        if args and args[0] in {"read-tree", "update-index"}:
            assert index_file == prepared_index
        elif "write-tree" in args:
            assert index_file in {
                prepared_index,
                prepared_index.with_name("index-verification"),
            }


def test_checkpoint_prepare_tree_contains_exact_audited_paths(tmp_path: Path) -> None:
    service, proposal_ref, base = _valid_service(tmp_path)

    prepared = service.prepare(proposal_ref, "exact tree")

    base_tree = _tree(tmp_path, base)
    candidate_tree = _tree(tmp_path, prepared.candidate_tree)
    receipts = {receipt.path: receipt for receipt in prepared.candidate_paths}
    assert set(receipts) == {
        "memory/NOW.md",
        proposal_ref,
        "transitions/T-checkpoint/audit.json",
    }
    assert set(candidate_tree) == set(base_tree) | set(receipts)
    for path, entry in base_tree.items():
        if path not in receipts:
            assert candidate_tree[path] == entry
    for path, receipt in receipts.items():
        assert candidate_tree[path] == (receipt.mode, receipt.blob_oid)
        assert hashlib.sha256(_blob(tmp_path, prepared.candidate_tree, path)).hexdigest() == (
            receipt.content_sha256
        )
    audit = json.loads((tmp_path / "transitions/T-checkpoint/audit.json").read_bytes())
    assert audit["audit_payload_sha256"] == prepared.audit_payload_sha256
    assert audit["candidate_subject_sha256"] == prepared.candidate_subject_sha256


def test_checkpoint_prepare_stages_derived_closure_but_not_base_ref_only_closure(
    tmp_path: Path,
) -> None:
    _run_service, manifest, _final = observation_support._install_run_final(tmp_path)
    run_id = str(manifest["run_id"])
    manifest_ref = f"runs/{run_id}/manifest.json"
    final_ref = f"runs/{run_id}/final.json"
    memory = tmp_path / "memory" / "NOW.md"
    memory.parent.mkdir()
    memory.write_text(
        "# Current State\n\n## Findings\n\nInitial process context.\n",
        encoding="utf-8",
    )
    _git(tmp_path, "add", manifest_ref, "memory/NOW.md")
    _git(tmp_path, "commit", "-qm", "record observation manifest baseline")
    base = _git_text(tmp_path, "rev-parse", "HEAD")
    memory.write_text(
        "# Current State\n\n## Findings\n\nAssimilated process context.\n\n"
        + json.dumps(
            {
                "observation_ref": final_ref,
                "relation": "context",
                "scope": "process context",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    proposal_ref = _write_proposal(
        tmp_path,
        "T-derived-closure",
        base,
        ["memory/NOW.md"],
        assimilations=[
            {
                "observation_ref": final_ref,
                "affected_paths": ["memory/NOW.md"],
                "rationale": "memory/NOW.md#Findings",
            }
        ],
    )

    prepared = CheckpointService(
        tmp_path,
        canonical_repository=bind_repository(tmp_path),
        canonical_ref=_git_text(tmp_path, "symbolic-ref", "HEAD"),
    ).prepare(proposal_ref, "derived closure")

    closure_paths = {
        path["path"]: path["state"]
        for record in prepared.audit_testimony["observation_closure"]
        for path in record["paths"]
    }
    assert closure_paths == {manifest_ref: "ref_only", final_ref: "derived"}
    candidate_paths = {receipt.path for receipt in prepared.candidate_paths}
    assert final_ref in candidate_paths
    assert manifest_ref not in candidate_paths
    base_tree = _tree(tmp_path, base)
    candidate_tree = _tree(tmp_path, prepared.candidate_tree)
    assert candidate_tree[manifest_ref] == base_tree[manifest_ref]
    assert candidate_tree[final_ref][1] == hashlib.sha1(
        b"blob "
        + str((tmp_path / final_ref).stat().st_size).encode("ascii")
        + b"\0"
        + (tmp_path / final_ref).read_bytes()
    ).hexdigest()


def test_checkpoint_prepare_preserves_unrelated_staged_and_unstaged_work(
    tmp_path: Path,
) -> None:
    service, proposal_ref, base = _valid_service(tmp_path)
    unrelated = tmp_path / "unrelated.txt"
    unrelated.write_text("staged unrelated\n", encoding="utf-8")
    _git(tmp_path, "add", "unrelated.txt")
    unrelated.write_text("unstaged unrelated\n", encoding="utf-8")
    extra = tmp_path / "scratch.txt"
    extra.write_text("untracked work\n", encoding="utf-8")
    index_before = _index_bytes(tmp_path)
    status_before = _git(tmp_path, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout

    prepared = service.prepare(proposal_ref, "preserve work")

    assert _index_bytes(tmp_path) == index_before
    assert unrelated.read_bytes() == b"unstaged unrelated\n"
    assert extra.read_bytes() == b"untracked work\n"
    status_after = _git(
        tmp_path,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
    ).stdout
    before_records = {record for record in status_before.split(b"\0") if record}
    after_records = {record for record in status_after.split(b"\0") if record}
    assert after_records == before_records | {
        b"?? transitions/T-checkpoint/audit.json"
    }
    assert _blob(tmp_path, prepared.candidate_tree, "unrelated.txt") == _blob(
        tmp_path,
        base,
        "unrelated.txt",
    )


@pytest.mark.parametrize("drift", ("overlapping_index", "worktree"))
def test_checkpoint_prepare_rejects_overlapping_index_or_worktree_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    service, proposal_ref, _base = _valid_service(tmp_path)
    if drift == "overlapping_index":
        _git(tmp_path, "add", "memory/NOW.md")
    else:
        real_read_tree = checkpoint_module.read_tree_into_index
        changed = False

        def drift_after_read_tree(
            repository: object,
            commit: str,
            **kwargs: object,
        ) -> subprocess.CompletedProcess[bytes]:
            nonlocal changed
            result = real_read_tree(repository, commit, **kwargs)  # type: ignore[arg-type]
            if not changed:
                changed = True
                (tmp_path / "memory/NOW.md").write_text(
                    "# Current State\n\n## Findings\n\nConcurrent drift.\n",
                    encoding="utf-8",
                )
            return result

        monkeypatch.setattr(
            checkpoint_module,
            "read_tree_into_index",
            drift_after_read_tree,
        )
    index_before = _index_bytes(tmp_path)

    with pytest.raises(CheckpointError, match="index|overlap|drift|changed"):
        service.prepare(proposal_ref, "reject overlap")

    assert _index_bytes(tmp_path) == index_before
    assert not (tmp_path / ".aros/checkpoints/T-checkpoint/prepared.json").exists()


def test_checkpoint_prepare_rejects_ineligible_audit_without_tree_or_index_change(
    tmp_path: Path,
) -> None:
    base, canonical_ref = _init_repository(tmp_path)
    proposal_ref = _write_proposal(
        tmp_path,
        "T-ineligible",
        base,
        ["memory/NOW.md"],
    )
    service = CheckpointService(
        tmp_path,
        canonical_repository=bind_repository(tmp_path),
        canonical_ref=canonical_ref,
    )
    before = _authority(tmp_path)
    index_before = _index_bytes(tmp_path)

    with pytest.raises(CheckpointError, match="mechanically valid|audit"):
        service.prepare(proposal_ref, "must deny")

    assert _authority(tmp_path) == before
    assert _index_bytes(tmp_path) == index_before
    assert not (tmp_path / "transitions/T-ineligible/audit.json").exists()
    assert not (tmp_path / ".aros/checkpoints/T-ineligible/index").exists()
    assert not (tmp_path / ".aros/checkpoints/T-ineligible/prepared.json").exists()


def test_checkpoint_prepare_is_exactly_idempotent_and_conflicting_retry_fails(
    tmp_path: Path,
) -> None:
    service, proposal_ref, _base = _valid_service(tmp_path)

    first = service.prepare(proposal_ref, "stable message")
    prepared_path = tmp_path / first.prepared_ref
    prepared_bytes = prepared_path.read_bytes()
    audit_path = tmp_path / "transitions/T-checkpoint/audit.json"
    audit_bytes = audit_path.read_bytes()
    audit_path.unlink()
    second = service.prepare(proposal_ref, "stable message")

    assert second == first
    assert prepared_path.read_bytes() == prepared_bytes
    assert audit_path.read_bytes() == audit_bytes
    with pytest.raises(CheckpointError, match="conflict|message|retry"):
        service.prepare(proposal_ref, "different message")
    assert prepared_path.read_bytes() == prepared_bytes


@pytest.mark.parametrize(
    "drift",
    ("message", "candidate", "proposal", "prepared_bytes"),
)
def test_checkpoint_prepare_incompatible_retry_does_not_restore_missing_audit(
    tmp_path: Path,
    drift: str,
) -> None:
    service, proposal_ref, _base = _valid_service(tmp_path)
    prepared = service.prepare(proposal_ref, "stable message")
    prepared_path = tmp_path / prepared.prepared_ref
    prepared_bytes = prepared_path.read_bytes()
    audit_path = tmp_path / "transitions/T-checkpoint/audit.json"
    audit_path.unlink()
    message = "stable message"
    if drift == "message":
        message = "different message"
    elif drift == "candidate":
        (tmp_path / "memory/NOW.md").write_text(
            "# Current State\n\n## Findings\n\nDifferent finding.\n",
            encoding="utf-8",
        )
    elif drift == "proposal":
        proposal_path = tmp_path / proposal_ref
        proposal = json.loads(proposal_path.read_bytes())
        proposal_path.write_text(
            json.dumps(proposal, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    else:
        record = json.loads(prepared_bytes)
        prepared_path.write_text(
            json.dumps(record, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        prepared_bytes = prepared_path.read_bytes()

    with pytest.raises(CheckpointError, match="conflict|retry"):
        service.prepare(proposal_ref, message)

    assert not audit_path.exists()
    assert prepared_path.read_bytes() == prepared_bytes


def test_checkpoint_prepare_rejects_semantic_prepared_retry_with_different_bytes(
    tmp_path: Path,
) -> None:
    service, proposal_ref, _base = _valid_service(tmp_path)
    prepared = service.prepare(proposal_ref, "stable message")
    prepared_path = tmp_path / prepared.prepared_ref
    value = json.loads(prepared_path.read_bytes())
    prepared_path.unlink()
    prepared_path.write_text(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    with pytest.raises(CheckpointError, match="byte|conflict|retry"):
        service.prepare(proposal_ref, "stable message")


@pytest.mark.parametrize("failure", ("selected_drift", "object_import", "tree"))
def test_checkpoint_prepare_does_not_publish_audit_before_candidate_is_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    service, proposal_ref, _base = _valid_service(tmp_path)
    audit_path = tmp_path / "transitions/T-checkpoint/audit.json"
    if failure == "object_import":
        monkeypatch.setattr(
            checkpoint_module,
            "_import_commit_objects",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                CheckpointError("injected object import failure")
            ),
        )
    elif failure == "tree":
        monkeypatch.setattr(
            checkpoint_module,
            "_write_tree",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                CheckpointError("injected tree failure")
            ),
        )
    else:
        real_verify = checkpoint_module._verify_candidate_tree

        def drift_after_tree(*args: object, **kwargs: object) -> None:
            real_verify(*args, **kwargs)  # type: ignore[arg-type]
            (tmp_path / "memory/NOW.md").write_text(
                "# Current State\n\n## Findings\n\nLate drift.\n",
                encoding="utf-8",
            )

        monkeypatch.setattr(
            checkpoint_module,
            "_verify_candidate_tree",
            drift_after_tree,
        )

    with pytest.raises(CheckpointError, match="import|tree|drift|changed"):
        service.prepare(proposal_ref, "prepublication failure")

    assert not audit_path.exists()
    assert not (tmp_path / ".aros/checkpoints/T-checkpoint/prepared.json").exists()


def test_checkpoint_prepare_rejects_index_replaced_between_tree_and_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, proposal_ref, _base = _valid_service(tmp_path)
    index_path = tmp_path / ".aros/checkpoints/T-checkpoint/index"
    base_index_bytes = _index_bytes(tmp_path)
    real_snapshot = checkpoint_module._snapshot_file
    replaced = False

    def replace_before_snapshot(path: Path) -> object:
        nonlocal replaced
        if path == index_path and path.exists() and not replaced:
            replaced = True
            replacement = path.with_name("replacement-index")
            replacement.write_bytes(base_index_bytes)
            os.replace(replacement, path)
        return real_snapshot(path)

    monkeypatch.setattr(checkpoint_module, "_snapshot_file", replace_before_snapshot)

    with pytest.raises(CheckpointError, match="index|tree|candidate"):
        service.prepare(proposal_ref, "reject index snapshot race")

    assert replaced is True
    assert not (tmp_path / "transitions/T-checkpoint/audit.json").exists()


def test_checkpoint_prepare_rejects_verification_index_swap_before_write_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, proposal_ref, _base = _valid_service(tmp_path)
    index_path = tmp_path / ".aros/checkpoints/T-checkpoint/index"
    base_index_bytes = _index_bytes(tmp_path)
    real_snapshot = checkpoint_module._snapshot_file
    real_git_result = worktrees_module._git_result
    good_candidate_bytes: bytes | None = None
    snapshot_replaced = False
    verification_replaced = False

    def replace_before_snapshot(path: Path) -> object:
        nonlocal good_candidate_bytes, snapshot_replaced
        if path == index_path and path.exists() and not snapshot_replaced:
            good_candidate_bytes = path.read_bytes()
            snapshot_replaced = True
            replacement = path.with_name("replacement-index")
            replacement.write_bytes(base_index_bytes)
            os.replace(replacement, path)
        return real_snapshot(path)

    def replace_before_verification_write_tree(
        repository: object,
        *args: str,
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        nonlocal verification_replaced
        verification_index = kwargs.get("index_file")
        if (
            args
            and "write-tree" in args
            and isinstance(verification_index, Path)
            and verification_index.name == "index-verification"
            and not verification_replaced
        ):
            assert good_candidate_bytes is not None
            verification_replaced = True
            replacement = verification_index.with_name("verification-replacement")
            replacement.write_bytes(good_candidate_bytes)
            os.replace(replacement, verification_index)
        return real_git_result(repository, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(checkpoint_module, "_snapshot_file", replace_before_snapshot)
    monkeypatch.setattr(
        worktrees_module,
        "_git_result",
        replace_before_verification_write_tree,
    )

    with pytest.raises(CheckpointError, match="identity"):
        service.prepare(proposal_ref, "reject verification index swap")

    assert snapshot_replaced is True
    assert verification_replaced is True
    assert not (tmp_path / "transitions/T-checkpoint/audit.json").exists()


@pytest.mark.parametrize(
    "target",
    ("selected", "audit", "temp_index", "prepared"),
)
def test_checkpoint_prepare_revalidates_everything_after_prepared_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    service, proposal_ref, _base = _valid_service(tmp_path)
    real_create_json = checkpoint_module.create_json

    def publish_then_drift(path: str | Path, value: object) -> bool:
        created = real_create_json(path, value)
        prepared_path = Path(path)
        if prepared_path.name != "prepared.json":
            return created
        if target == "selected":
            (tmp_path / "memory/NOW.md").write_text(
                "# Current State\n\n## Findings\n\nPost-publication drift.\n",
                encoding="utf-8",
            )
        elif target == "audit":
            audit_path = tmp_path / "transitions/T-checkpoint/audit.json"
            audit = json.loads(audit_path.read_bytes())
            audit_path.unlink()
            audit_path.write_text(json.dumps(audit), encoding="utf-8")
        elif target == "temp_index":
            index = tmp_path / ".aros/checkpoints/T-checkpoint/index"
            replacement = index.with_name("replacement-index")
            replacement.write_bytes(index.read_bytes())
            os.replace(replacement, index)
        else:
            prepared = json.loads(prepared_path.read_bytes())
            prepared_path.unlink()
            prepared_path.write_text(json.dumps(prepared), encoding="utf-8")
        return created

    monkeypatch.setattr(checkpoint_module, "create_json", publish_then_drift)

    with pytest.raises(CheckpointError, match="audit|candidate|changed|drift|index|prepared"):
        service.prepare(proposal_ref, "postpublication validation")

    assert (tmp_path / ".aros/checkpoints/T-checkpoint/prepared.json").exists()


def test_checkpoint_prepare_final_tree_has_no_admission_receipt(tmp_path: Path) -> None:
    service, proposal_ref, _base = _valid_service(tmp_path)

    prepared = service.prepare(proposal_ref, "no admission")

    paths = set(_tree(tmp_path, prepared.candidate_tree))
    assert "transitions/T-checkpoint/admission.json" not in paths
    assert not any(path.endswith("/admission.json") for path in paths)


def test_checkpoint_prepare_distinct_candidate_imports_exact_objects_without_ref_update(
    tmp_path: Path,
) -> None:
    candidate = tmp_path / "candidate"
    canonical = tmp_path / "canonical"
    service, brief, ownership, _final = task_support._create_terminal_task(candidate)
    _git(tmp_path, "clone", "-q", str(candidate), str(canonical))
    returned, child_commit, return_commit = task_support._commit_child_return(
        candidate,
        brief,
        ownership,
    )
    task_id = str(brief["task_id"])
    collected = service.collect(task_id)
    assert collected["child_commit"] == child_commit == returned["child_commit"]
    assert collected["return_commit"] == return_commit
    base = _git_text(candidate, "rev-parse", "HEAD")
    canonical_ref = _git_text(candidate, "symbolic-ref", "HEAD")
    proposal_ref = _write_proposal(
        candidate,
        "T-distinct",
        base,
        [f"tasks/{task_id}/collected.json"],
    )
    before = _authority(canonical)
    assert _git(canonical, "cat-file", "-e", f"{child_commit}^{{commit}}", check=False).returncode != 0
    assert _git(canonical, "cat-file", "-e", f"{return_commit}^{{commit}}", check=False).returncode != 0
    hook_marker = tmp_path / "upload-pack-hook-ran"
    hook = tmp_path / "pack-objects-hook"
    hook.write_text(
        f"#!/bin/sh\ntouch {hook_marker}\nexec git pack-objects \"$@\"\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    _git(candidate, "config", "uploadpack.packObjectsHook", str(hook))

    prepared = CheckpointService(
        candidate,
        canonical_repository=bind_repository(canonical),
        canonical_ref=canonical_ref,
    ).prepare(proposal_ref, "import exact objects")

    assert _authority(canonical) == before
    assert not hook_marker.exists()
    assert _git(canonical, "rev-parse", f"{child_commit}^{{commit}}").stdout.strip() == child_commit.encode()
    assert _git(canonical, "rev-parse", f"{return_commit}^{{commit}}").stdout.strip() == return_commit.encode()
    assert _git(canonical, "cat-file", "-t", prepared.candidate_tree).stdout == b"tree\n"


def test_checkpoint_prepare_rejects_audit_file_conflict(tmp_path: Path) -> None:
    service, proposal_ref, _base = _valid_service(tmp_path)
    audit_path = tmp_path / "transitions/T-checkpoint/audit.json"
    audit_path.write_bytes(b'{"conflict":true}\n')

    with pytest.raises(CheckpointError, match="audit.*conflict|byte"):
        service.prepare(proposal_ref, "conflicting audit")

    assert audit_path.read_bytes() == b'{"conflict":true}\n'
    assert not (tmp_path / ".aros/checkpoints/T-checkpoint/prepared.json").exists()


def test_checkpoint_prepare_rejects_root_or_ref_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = tmp_path / "candidate"
    canonical = tmp_path / "canonical"
    base, canonical_ref = _init_repository(candidate)
    _git(tmp_path, "clone", "-q", str(candidate), str(canonical))
    _git(canonical, "config", "user.email", "checkpoint@example.invalid")
    _git(canonical, "config", "user.name", "Checkpoint Test")
    (candidate / "memory" / "NOW.md").write_text(
        "# Current State\n\n## Findings\n\nAudited finding.\n",
        encoding="utf-8",
    )
    proposal_ref = _write_proposal(
        candidate,
        "T-ref-drift",
        base,
        ["memory/NOW.md"],
    )
    service = CheckpointService(
        candidate,
        canonical_repository=bind_repository(canonical),
        canonical_ref=canonical_ref,
    )
    tree = _git_text(canonical, "rev-parse", f"{base}^{{tree}}")
    drift_commit = _git_text(
        canonical,
        "commit-tree",
        tree,
        "-p",
        base,
        "-m",
        "concurrent drift",
    )
    real_audit = service.audit_service.audit

    def audit_then_drift(ref: str) -> dict[str, object]:
        testimony = real_audit(ref)
        _git(canonical, "update-ref", canonical_ref, drift_commit, base)
        return testimony

    monkeypatch.setattr(service.audit_service, "audit", audit_then_drift)

    with pytest.raises(CheckpointError, match="root|HEAD|ref|drift"):
        service.prepare(proposal_ref, "reject ref drift")

    assert not (candidate / "transitions/T-ref-drift/audit.json").exists()
    assert not (candidate / ".aros/checkpoints/T-ref-drift/index").exists()


def test_checkpoint_prepare_pins_modes_and_ignores_ambient_hooks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, proposal_ref, _base = _valid_service(tmp_path)
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    marker = tmp_path / "hook-ran"
    hook = hooks / "post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
    hook.chmod(0o755)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(hooks))

    prepared = service.prepare(proposal_ref, "pinned modes")

    assert not marker.exists()
    assert all(receipt.mode == "100644" for receipt in prepared.candidate_paths)
    assert stat.S_IMODE((tmp_path / prepared.index_ref).stat().st_mode) & 0o077 == 0
    with pytest.raises(TypeError):
        prepared.audit_testimony["mechanically_valid"] = False  # type: ignore[index]


def test_checkpoint_prepare_has_no_admission_finalize_or_user_index_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, proposal_ref, _base = _valid_service(tmp_path)
    calls: list[tuple[str, ...]] = []
    real_git_result = worktrees_module._git_result

    def record_git(
        repository: object,
        *args: str,
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append(args)
        return real_git_result(repository, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(worktrees_module, "_git_result", record_git)

    service.prepare(proposal_ref, "preparation only")

    verbs = {args[0] for args in calls if args}
    assert verbs.isdisjoint({"add", "commit", "commit-tree", "reset", "update-ref"})
    source = inspect.getsource(CheckpointService)
    for forbidden in (
        "AdmissionGateway",
        "FinalizeFence",
        "commit-tree",
        "update-ref",
        "compare-and-swap",
    ):
        assert forbidden not in source
    assert not any(
        fragment in name.casefold()
        for name, _value in inspect.getmembers(CheckpointService)
        for fragment in ("admit", "finalize")
    )
