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
from arbor.aros.store import canonical_json_bytes
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


def _write_sparse_file(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        stream.truncate(size)


def _reject_opening_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    target: Path,
    *,
    larger_than: int,
) -> None:
    real_open = checkpoint_module.os.open

    def guarded_open(path: object, *args: object, **kwargs: object) -> int:
        if Path(path) == target and target.stat().st_size > larger_than:
            raise AssertionError(f"oversized snapshot was opened: {target}")
        return real_open(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(checkpoint_module.os, "open", guarded_open)


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


def _hashed_record(record: dict[str, object], field: str) -> bytes:
    payload = {key: value for key, value in record.items() if key != field}
    encoded = {
        **payload,
        field: hashlib.sha256(canonical_json_bytes(payload)).hexdigest(),
    }
    return canonical_json_bytes(encoded)


def _allow_receipt_bytes(
    *,
    candidate_subject_sha256: str = "a" * 64,
    audit_payload_sha256: str = "b" * 64,
    canonical_ref: str = "refs/heads/main",
    revision: int = 1,
    lease_expires_at: int = 3_000,
) -> bytes:
    return _hashed_record(
        {
            "schemaVersion": 1,
            "decision": "allow",
            "candidateSubjectSHA256": candidate_subject_sha256,
            "auditPayloadSHA256": audit_payload_sha256,
            "contractID": "pct_test",
            "revision": revision,
            "specHash": "c" * 64,
            "workspaceID": "workspace-test",
            "canonicalRef": canonical_ref,
            "sessionID": "ses_test",
            "promptID": "msg_test",
            "attempt": 1,
            "attemptKey": "1:",
            "leaseOwner": "owner-test",
            "leaseExpiresAt": lease_expires_at,
            "capability": "checkpoint",
            "budgetBefore": {
                "turns": {"limit": 10, "used": 1, "remaining": 9},
                "actions": {"limit": 10, "used": 1, "remaining": 9},
                "deadline": 10_000,
            },
            "charge": {"actions": 1},
            "budgetRemaining": {
                "turns": {"limit": 10, "used": 1, "remaining": 9},
                "actions": {"limit": 10, "used": 2, "remaining": 8},
                "deadline": 10_000,
            },
            "evaluatorPolicyRefs": ["visible/quality@1"],
            "researchContractBindingSHA256": "d" * 64,
            "auditImplementationID": "aros-transition-audit-v1",
            "trustedExecutionClosureSHA256": "e" * 64,
            "enforcementClass": "mediated",
            "authorityDomainID": "opencode/local-same-uid",
            "issuedAt": 1_000,
            "receiptSHA256": "",
        },
        "receiptSHA256",
    )


def _fence_bytes(
    receipt: dict[str, object],
    *,
    revision: int | None = None,
    issued_at: int = 1_400,
    expires_at: int = 1_600,
) -> bytes:
    return _hashed_record(
        {
            "schemaVersion": 1,
            "receiptSHA256": receipt["receiptSHA256"],
            "reservationID": "reservation-test",
            "revision": receipt["revision"] if revision is None else revision,
            "researchContractBindingSHA256": receipt[
                "researchContractBindingSHA256"
            ],
            "sessionID": receipt["sessionID"],
            "promptID": receipt["promptID"],
            "attempt": receipt["attempt"],
            "attemptKey": receipt["attemptKey"],
            "leaseOwner": receipt["leaseOwner"],
            "leaseExpiresAt": receipt["leaseExpiresAt"],
            "issuedAt": issued_at,
            "expiresAt": expires_at,
            "fenceSHA256": "",
        },
        "fenceSHA256",
    )


def _decoded(raw: bytes) -> dict[str, object]:
    value = json.loads(raw)
    assert isinstance(value, dict)
    return value


def _finalize_fixture(
    root: Path,
    *,
    transition_id: str = "T-finalize",
    message: str = "Principal café checkpoint.\nSecond line.\n",
) -> tuple[
    CheckpointService,
    object,
    bytes,
    bytes,
    Path,
    Path,
    str,
    str,
]:
    candidate = root / "candidate"
    canonical = root / "canonical"
    base, canonical_ref = _init_repository(candidate)
    _git(root, "clone", "-q", str(candidate), str(canonical))
    _git(canonical, "config", "user.email", "checkpoint@example.invalid")
    _git(canonical, "config", "user.name", "Checkpoint Test")
    (candidate / "memory" / "NOW.md").write_text(
        "# Current State\n\n## Findings\n\nFinalized finding.\n",
        encoding="utf-8",
    )
    proposal_ref = _write_proposal(
        candidate,
        transition_id,
        base,
        ["memory/NOW.md"],
    )
    service = CheckpointService(
        candidate,
        canonical_repository=bind_repository(canonical),
        canonical_ref=canonical_ref,
        clock=lambda: 1_500,
    )
    prepared = service.prepare(proposal_ref, message)
    receipt_raw = _allow_receipt_bytes(
        candidate_subject_sha256=prepared.candidate_subject_sha256,
        audit_payload_sha256=prepared.audit_payload_sha256,
        canonical_ref=canonical_ref,
    )
    receipt = _decoded(receipt_raw)
    return (
        service,
        prepared,
        receipt_raw,
        _fence_bytes(receipt),
        candidate,
        canonical,
        base,
        message,
    )


def _task_finalize_fixture(
    root: Path,
) -> tuple[CheckpointService, object, bytes, bytes, Path, Path, str, str, str]:
    candidate = root / "candidate"
    canonical = root / "canonical"
    task_service, brief, ownership, _final = task_support._create_terminal_task(
        candidate
    )
    _git(root, "clone", "-q", str(candidate), str(canonical))
    _git(canonical, "config", "user.email", "checkpoint@example.invalid")
    _git(canonical, "config", "user.name", "Checkpoint Test")
    _returned, _child_commit, return_commit = task_support._commit_child_return(
        candidate,
        brief,
        ownership,
    )
    task_id = str(brief["task_id"])
    collected = task_service.collect(task_id)
    assert collected["return_commit"] == return_commit
    base = _git_text(candidate, "rev-parse", "HEAD")
    canonical_ref = _git_text(candidate, "symbolic-ref", "HEAD")
    proposal_ref = _write_proposal(
        candidate,
        "T-task-cas",
        base,
        [f"tasks/{task_id}/collected.json"],
    )
    service = CheckpointService(
        candidate,
        canonical_repository=bind_repository(canonical),
        canonical_ref=canonical_ref,
        clock=lambda: 1_500,
    )
    prepared = service.prepare(proposal_ref, "Task observation checkpoint.\n")
    receipt_raw = _allow_receipt_bytes(
        candidate_subject_sha256=prepared.candidate_subject_sha256,
        audit_payload_sha256=prepared.audit_payload_sha256,
        canonical_ref=canonical_ref,
    )
    closure = prepared.audit_testimony["observation_closure"]
    assert isinstance(closure, tuple) and len(closure) == 1
    observation_ref = str(closure[0]["immutable_ref"])
    receipt = _decoded(receipt_raw)
    return (
        service,
        prepared,
        receipt_raw,
        _fence_bytes(receipt),
        candidate,
        canonical,
        base,
        observation_ref,
        return_commit,
    )


def _eval_finalize_fixture(
    root: Path,
) -> tuple[CheckpointService, object, bytes, bytes, Path, Path, str, str, str]:
    candidate = root / "candidate"
    canonical = root / "canonical"
    installed = observation_support._install_eval_receipt(candidate)
    _git(root, "clone", "-q", str(candidate), str(canonical))
    _git(canonical, "config", "user.email", "checkpoint@example.invalid")
    _git(canonical, "config", "user.name", "Checkpoint Test")
    base = _git_text(candidate, "rev-parse", "HEAD")
    canonical_ref = _git_text(candidate, "symbolic-ref", "HEAD")
    proposal_ref = _write_proposal(
        candidate,
        "T-eval-cas",
        base,
        [str(installed["receipt_ref"])],
    )
    service = CheckpointService(
        candidate,
        canonical_repository=bind_repository(canonical),
        canonical_ref=canonical_ref,
        clock=lambda: 1_500,
    )
    prepared = service.prepare(proposal_ref, "Eval observation checkpoint.\n")
    receipt_raw = _allow_receipt_bytes(
        candidate_subject_sha256=prepared.candidate_subject_sha256,
        audit_payload_sha256=prepared.audit_payload_sha256,
        canonical_ref=canonical_ref,
    )
    closure = prepared.audit_testimony["observation_closure"]
    assert isinstance(closure, tuple) and len(closure) == 1
    observation_ref = str(closure[0]["immutable_ref"])
    receipt = installed["receipt"]
    assert isinstance(receipt, dict)
    candidate_commit = str(receipt["candidate_commit"])
    authority = _decoded(receipt_raw)
    return (
        service,
        prepared,
        receipt_raw,
        _fence_bytes(authority),
        candidate,
        canonical,
        base,
        observation_ref,
        candidate_commit,
    )


def test_procontract_canonical_json_golden_vector_matches_typescript() -> None:
    encoded = canonical_json_bytes(
        {"z": 1, "a": "é", "nested": {"b": 2, "a": [True, None]}}
    )

    assert encoded.hex() == (
        "7b2261223a22c3a9222c226e6573746564223a7b2261223a5b747275652c"
        "6e756c6c5d2c2262223a327d2c227a223a317d"
    )
    assert hashlib.sha256(encoded).hexdigest() == (
        "3d4ef4cab1709da1a1628556cd21d27c5c1c6478d92a03fda97ee98f1236cf44"
    )


def test_allow_receipt_codec_accepts_only_exact_canonical_self_hashed_bytes() -> None:
    raw = _allow_receipt_bytes()

    decoded = checkpoint_module._decode_admission_receipt(raw)

    assert decoded["decision"] == "allow"
    assert decoded["capability"] == "checkpoint"
    assert canonical_json_bytes(decoded) == raw


@pytest.mark.parametrize(
    "case",
    (
        "deny",
        "schema-bool",
        "unknown",
        "missing",
        "hash",
        "budget-shape",
        "budget-negative",
        "timestamp",
        "snake-case",
    ),
)
def test_allow_receipt_codec_rejects_non_allow_or_malformed_authority(
    case: str,
) -> None:
    receipt = _decoded(_allow_receipt_bytes())
    if case == "deny":
        receipt["decision"] = "deny"
    elif case == "schema-bool":
        receipt["schemaVersion"] = True
    elif case == "unknown":
        receipt["actor"] = "principal"
    elif case == "missing":
        receipt.pop("contractID")
    elif case == "hash":
        receipt["receiptSHA256"] = "0" * 64
    elif case == "budget-shape":
        budget = receipt["budgetBefore"]
        assert isinstance(budget, dict)
        budget["extra"] = 1
    elif case == "budget-negative":
        budget = receipt["budgetRemaining"]
        assert isinstance(budget, dict)
        actions = budget["actions"]
        assert isinstance(actions, dict)
        actions["remaining"] = -1
    elif case == "timestamp":
        receipt["leaseExpiresAt"] = -1
    else:
        receipt = {
            "schema_version": 1,
            "receipt_kind": "human_direct",
            "decision": "allow",
        }
    raw = (
        canonical_json_bytes(receipt)
        if case in {"hash", "snake-case"}
        else _hashed_record(receipt, "receiptSHA256")
    )

    with pytest.raises(CheckpointError, match="receipt|budget|timestamp|field|allow"):
        checkpoint_module._decode_admission_receipt(raw)


def test_allow_receipt_codec_does_not_reimplement_budget_arithmetic_policy() -> None:
    receipt = _decoded(_allow_receipt_bytes())
    receipt["budgetBefore"] = {
        "turns": {"limit": 2, "used": 7, "remaining": 11},
        "actions": {"limit": 1, "used": 5, "remaining": 9},
        "deadline": 10_000,
    }
    receipt["charge"] = {"actions": 0}
    receipt["budgetRemaining"] = {
        "turns": {"limit": 99, "used": 0, "remaining": 4},
        "actions": {"limit": 8, "used": 3, "remaining": 17},
        "deadline": 20_000,
    }
    raw = _hashed_record(receipt, "receiptSHA256")

    decoded = checkpoint_module._decode_admission_receipt(raw)

    assert decoded == _decoded(raw)


@pytest.mark.parametrize(
    "raw",
    (
        b'{"a":1,"a":1}',
        _allow_receipt_bytes() + b"\n",
        b'{"schemaVersion":1}',
    ),
)
def test_allow_receipt_codec_rejects_duplicate_or_noncanonical_json(raw: bytes) -> None:
    with pytest.raises(CheckpointError, match="receipt|canonical|JSON|field"):
        checkpoint_module._decode_admission_receipt(raw)


def test_finalize_fence_codec_binds_receipt_authority_and_current_time() -> None:
    receipt = checkpoint_module._decode_admission_receipt(_allow_receipt_bytes())
    raw = _fence_bytes(receipt)

    decoded = checkpoint_module._decode_finalize_fence(
        raw,
        receipt=receipt,
        now_ms=1_500,
    )

    assert decoded["receiptSHA256"] == receipt["receiptSHA256"]
    assert canonical_json_bytes(decoded) == raw


@pytest.mark.parametrize(
    ("case", "now_ms"),
    (
        ("receipt", 1_500),
        ("schema-bool", 1_500),
        ("revision", 1_500),
        ("binding", 1_500),
        ("session", 1_500),
        ("expired", 1_601),
        ("not-issued", 1_399),
        ("lease-expired", 3_000),
        ("hash", 1_500),
    ),
)
def test_finalize_fence_codec_rejects_hash_expiry_or_authority_mismatch(
    case: str,
    now_ms: int,
) -> None:
    receipt = checkpoint_module._decode_admission_receipt(_allow_receipt_bytes())
    fence = _decoded(_fence_bytes(receipt))
    if case == "receipt":
        fence["receiptSHA256"] = "0" * 64
    elif case == "schema-bool":
        fence["schemaVersion"] = True
    elif case == "revision":
        fence["revision"] = 2
    elif case == "binding":
        fence["researchContractBindingSHA256"] = "0" * 64
    elif case == "session":
        fence["sessionID"] = "other-session"
    elif case == "lease-expired":
        pass
    elif case == "hash":
        fence["fenceSHA256"] = "0" * 64
    raw = (
        canonical_json_bytes(fence)
        if case == "hash"
        else _hashed_record(fence, "fenceSHA256")
    )

    with pytest.raises(CheckpointError, match="fence|receipt|revision|binding|session|time|lease"):
        checkpoint_module._decode_finalize_fence(
            raw,
            receipt=receipt,
            now_ms=now_ms,
        )


def test_prepare_binds_exact_admitted_ordinary_index_projection_for_finalize(
    tmp_path: Path,
) -> None:
    (
        _service,
        prepared,
        _receipt,
        _fence,
        candidate,
        _canonical,
        _base,
        _message,
    ) = _finalize_fixture(tmp_path)
    record = json.loads((candidate / prepared.prepared_ref).read_bytes())
    base_tree = _tree(candidate, prepared.base_commit)

    assert record["ordinary_index_entries"] == [
        {
            "path": "memory/NOW.md",
            "mode": base_tree["memory/NOW.md"][0],
            "blob_oid": base_tree["memory/NOW.md"][1],
        }
    ]


def test_finalize_writes_exact_receipt_tree_message_and_sole_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        service,
        prepared,
        receipt,
        fence,
        candidate,
        canonical,
        base,
        message,
    ) = _finalize_fixture(tmp_path)
    (candidate / "unrelated.txt").write_text(
        "unrelated staged during admission\n",
        encoding="utf-8",
    )
    _git(candidate, "add", "unrelated.txt")
    candidate_index = _index_bytes(candidate)

    def forbidden_audit(_proposal_ref: str) -> dict[str, object]:
        raise AssertionError("finalize reran TransitionAudit")

    monkeypatch.setattr(service.audit_service, "audit", forbidden_audit)

    result = service.finalize(prepared.prepared_ref, receipt, fence)

    commit = str(result["commit"])
    assert result == {
        "schema_version": 1,
        "transition_id": prepared.transition_id,
        "canonical_ref": prepared.canonical_ref,
        "commit": commit,
        "state": "projection_pending",
    }
    assert _git_text(canonical, "rev-parse", prepared.canonical_ref) == commit
    assert _git_text(candidate, "rev-parse", prepared.canonical_ref) == base
    parent_line = _git_text(canonical, "rev-list", "--parents", "-n", "1", commit)
    assert parent_line.split() == [commit, base]
    commit_object = _git(canonical, "cat-file", "commit", commit).stdout
    assert commit_object.split(b"\n\n", 1)[1] == message.encode("utf-8")

    final_tree_oid = _git_text(canonical, "rev-parse", f"{commit}^{{tree}}")
    candidate_tree = _tree(canonical, prepared.candidate_tree)
    final_tree = _tree(canonical, final_tree_oid)
    admission_ref = f"transitions/{prepared.transition_id}/admission.json"
    assert set(final_tree) == set(candidate_tree) | {admission_ref}
    assert {
        path: entry for path, entry in final_tree.items() if path != admission_ref
    } == candidate_tree
    assert _blob(canonical, final_tree_oid, admission_ref) == receipt
    assert not (candidate / admission_ref).exists()
    assert not any("fence" in path for path in final_tree)
    assert _index_bytes(candidate) == candidate_index


def test_finalize_passes_non_newline_principal_message_exactly_to_commit_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = "Principal exact café message"
    (
        service,
        prepared,
        receipt,
        fence,
        candidate,
        canonical,
        _base,
        _message,
    ) = _finalize_fixture(tmp_path, message=message)
    commit_inputs: list[bytes] = []
    real_git_result = worktrees_module._git_result

    def record_commit_input(
        repository: object,
        *args: str,
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        if args and args[0] == "commit-tree":
            raw = kwargs.get("input_bytes")
            assert isinstance(raw, bytes)
            commit_inputs.append(raw)
        return real_git_result(repository, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(worktrees_module, "_git_result", record_commit_input)

    result = service.finalize(prepared.prepared_ref, receipt, fence)

    assert commit_inputs == [message.encode("utf-8")]
    assert (candidate / prepared.prepared_ref).with_name("message").read_bytes() == (
        message.encode("utf-8")
    )
    commit = str(result["commit"])
    body = _git(canonical, "cat-file", "commit", commit).stdout.split(b"\n\n", 1)[1]
    assert body == message.encode("utf-8")


def test_finalize_rejects_final_index_drift_immediately_before_cas(
    tmp_path: Path,
) -> None:
    (
        service,
        prepared,
        receipt,
        fence,
        candidate,
        canonical,
        base,
        _message,
    ) = _finalize_fixture(tmp_path)
    clock_calls = 0

    def drift_on_second_time_check() -> int:
        nonlocal clock_calls
        clock_calls += 1
        if clock_calls == 2:
            final_index = (candidate / prepared.index_ref).with_name("index-final")
            final_index.write_bytes(final_index.read_bytes() + b"drift")
        return 1_500

    service.clock = drift_on_second_time_check

    with pytest.raises(CheckpointError, match="final.*index|index.*drift"):
        service.finalize(prepared.prepared_ref, receipt, fence)

    assert clock_calls == 2
    assert _git_text(canonical, "rev-parse", prepared.canonical_ref) == base


@pytest.mark.parametrize(
    "target",
    (
        "candidate-bytes",
        "candidate-mode",
        "proposal",
        "audit",
        "message",
        "prepared",
        "prepared-schema",
        "temp-index",
        "user-index",
    ),
)
def test_finalize_revalidates_every_prepared_fact_before_cas(
    tmp_path: Path,
    target: str,
) -> None:
    (
        service,
        prepared,
        receipt,
        fence,
        candidate,
        canonical,
        base,
        _message,
    ) = _finalize_fixture(tmp_path)
    if target == "candidate-bytes":
        (candidate / "memory/NOW.md").write_text("drift\n", encoding="utf-8")
    elif target == "candidate-mode":
        (candidate / "memory/NOW.md").chmod(0o755)
    elif target == "proposal":
        (candidate / prepared.proposal_ref).write_bytes(b"{}\n")
    elif target == "audit":
        audit = candidate / f"transitions/{prepared.transition_id}/audit.json"
        audit.write_bytes(audit.read_bytes() + b" ")
    elif target == "message":
        (candidate / prepared.prepared_ref).with_name("message").write_bytes(
            b"changed message"
        )
    elif target == "prepared":
        path = candidate / prepared.prepared_ref
        path.write_bytes(path.read_bytes() + b" ")
    elif target == "prepared-schema":
        path = candidate / prepared.prepared_ref
        record = json.loads(path.read_bytes())
        record["schema_version"] = True
        path.write_text(
            json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
    elif target == "temp-index":
        path = candidate / prepared.index_ref
        path.write_bytes(path.read_bytes() + b"drift")
    elif target == "user-index":
        _git(candidate, "add", "memory/NOW.md")

    with pytest.raises(CheckpointError, match="prepared|candidate|proposal|audit|message|index|admission|drift|conflict"):
        service.finalize(prepared.prepared_ref, receipt, fence)

    assert _git_text(canonical, "rev-parse", prepared.canonical_ref) == base


def test_late_fence_failure_leaves_no_working_admission_and_new_receipt_retries(
    tmp_path: Path,
) -> None:
    (
        service,
        prepared,
        first_receipt,
        first_fence,
        candidate,
        canonical,
        base,
        _message,
    ) = _finalize_fixture(tmp_path)
    times = iter((1_500, 1_601))
    service.clock = lambda: next(times)

    with pytest.raises(CheckpointError, match="fence|time|expired"):
        service.finalize(prepared.prepared_ref, first_receipt, first_fence)

    admission_ref = f"transitions/{prepared.transition_id}/admission.json"
    assert not (candidate / admission_ref).exists()
    assert _git_text(canonical, "rev-parse", prepared.canonical_ref) == base

    second_receipt = _allow_receipt_bytes(
        candidate_subject_sha256=prepared.candidate_subject_sha256,
        audit_payload_sha256=prepared.audit_payload_sha256,
        canonical_ref=prepared.canonical_ref,
        revision=2,
    )
    second_decoded = _decoded(second_receipt)
    service.clock = lambda: 1_500

    result = service.finalize(
        prepared.prepared_ref,
        second_receipt,
        _fence_bytes(second_decoded),
    )

    commit = str(result["commit"])
    assert _blob(canonical, commit, admission_ref) == second_receipt
    assert not (candidate / admission_ref).exists()


@pytest.mark.parametrize(
    "case",
    (
        "deny",
        "subject",
        "audit",
        "canonical-ref",
        "expired-fence",
        "revised-fence",
    ),
)
def test_finalize_rejects_denial_binding_or_stale_fence_before_materialization(
    tmp_path: Path,
    case: str,
) -> None:
    (
        service,
        prepared,
        receipt_raw,
        fence_raw,
        candidate,
        canonical,
        base,
        _message,
    ) = _finalize_fixture(tmp_path)
    receipt = _decoded(receipt_raw)
    if case == "deny":
        receipt["decision"] = "deny"
        receipt_raw = _hashed_record(receipt, "receiptSHA256")
    elif case == "subject":
        receipt["candidateSubjectSHA256"] = "0" * 64
        receipt_raw = _hashed_record(receipt, "receiptSHA256")
        fence_raw = _fence_bytes(_decoded(receipt_raw))
    elif case == "audit":
        receipt["auditPayloadSHA256"] = "0" * 64
        receipt_raw = _hashed_record(receipt, "receiptSHA256")
        fence_raw = _fence_bytes(_decoded(receipt_raw))
    elif case == "canonical-ref":
        receipt["canonicalRef"] = "refs/heads/other"
        receipt_raw = _hashed_record(receipt, "receiptSHA256")
        fence_raw = _fence_bytes(_decoded(receipt_raw))
    elif case == "expired-fence":
        fence_raw = _fence_bytes(receipt, expires_at=1_499)
    else:
        fence_raw = _fence_bytes(receipt, revision=2)

    with pytest.raises(CheckpointError, match="receipt|allow|subject|audit|canonical|fence|revision|time"):
        service.finalize(prepared.prepared_ref, receipt_raw, fence_raw)

    admission = (
        candidate / "transitions" / prepared.transition_id / "admission.json"
    )
    assert not admission.exists()
    assert _git_text(canonical, "rev-parse", prepared.canonical_ref) == base


def test_finalize_atomic_transaction_updates_canonical_and_creates_task_ref(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        service,
        prepared,
        receipt,
        fence,
        _candidate,
        canonical,
        base,
        observation_ref,
        return_commit,
    ) = _task_finalize_fixture(tmp_path)
    transactions: list[bytes] = []
    real_git_result = worktrees_module._git_result

    def record_transaction(
        repository: object,
        *args: str,
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        if args[:2] == ("update-ref", "--stdin"):
            raw = kwargs.get("input_bytes")
            assert isinstance(raw, bytes)
            transactions.append(raw)
        return real_git_result(repository, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(worktrees_module, "_git_result", record_transaction)

    result = service.finalize(prepared.prepared_ref, receipt, fence)

    commit = str(result["commit"])
    assert _git_text(canonical, "rev-parse", observation_ref) == return_commit
    assert len(transactions) == 1
    commands = transactions[0].decode("ascii").splitlines()
    assert commands[0] == "start"
    assert f"update {prepared.canonical_ref} {commit} {base}" in commands
    assert f"create {observation_ref} {return_commit}" in commands
    assert not any(
        command.startswith(f"create {prepared.canonical_ref} ")
        for command in commands
    )
    assert commands[-2:] == ["prepare", "commit"]


def test_finalize_reuses_preexisting_exact_observation_ref(
    tmp_path: Path,
) -> None:
    (
        service,
        prepared,
        receipt,
        fence,
        _candidate,
        canonical,
        _base,
        observation_ref,
        return_commit,
    ) = _task_finalize_fixture(tmp_path)
    _git(canonical, "update-ref", observation_ref, return_commit)

    result = service.finalize(prepared.prepared_ref, receipt, fence)

    assert result["state"] == "projection_pending"
    assert _git_text(canonical, "rev-parse", observation_ref) == return_commit


def test_conflicting_observation_ref_aborts_canonical_update(
    tmp_path: Path,
) -> None:
    (
        service,
        prepared,
        receipt,
        fence,
        _candidate,
        canonical,
        base,
        observation_ref,
        return_commit,
    ) = _task_finalize_fixture(tmp_path)
    assert return_commit != base
    _git(canonical, "update-ref", observation_ref, base)

    with pytest.raises(CheckpointError, match="observation|ref|conflict"):
        service.finalize(prepared.prepared_ref, receipt, fence)

    assert _git_text(canonical, "rev-parse", prepared.canonical_ref) == base
    assert _git_text(canonical, "rev-parse", observation_ref) == base


def test_cas_loss_rolls_back_atomic_task_observation_ref(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        service,
        prepared,
        receipt,
        fence,
        _candidate,
        canonical,
        base,
        observation_ref,
        _return_commit,
    ) = _task_finalize_fixture(tmp_path)
    tree = _git_text(canonical, "rev-parse", f"{base}^{{tree}}")
    drift = _git_text(
        canonical,
        "commit-tree",
        tree,
        "-p",
        base,
        "-m",
        "concurrent winner",
    )
    real_git_result = worktrees_module._git_result
    raced = False

    def lose_cas(
        repository: object,
        *args: str,
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        nonlocal raced
        if args[:2] == ("update-ref", "--stdin") and not raced:
            raced = True
            _git(canonical, "update-ref", prepared.canonical_ref, drift, base)
        return real_git_result(repository, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(worktrees_module, "_git_result", lose_cas)

    with pytest.raises(CheckpointError, match="CAS|transaction|ref|update"):
        service.finalize(prepared.prepared_ref, receipt, fence)

    assert raced is True
    assert _git_text(canonical, "rev-parse", prepared.canonical_ref) == drift
    assert _git(canonical, "show-ref", "--verify", observation_ref, check=False).returncode != 0


def test_fence_expiry_during_observation_preflight_never_reaches_ref_transaction(
    tmp_path: Path,
) -> None:
    (
        service,
        prepared,
        receipt,
        fence,
        _candidate,
        canonical,
        base,
        observation_ref,
        _return_commit,
    ) = _task_finalize_fixture(tmp_path)
    times = iter((1_500, 1_500, 1_601))
    service.clock = lambda: next(times)

    with pytest.raises(CheckpointError, match="fence|time|expired"):
        service.finalize(prepared.prepared_ref, receipt, fence)

    assert _git_text(canonical, "rev-parse", prepared.canonical_ref) == base
    assert _git(canonical, "show-ref", "--verify", observation_ref, check=False).returncode != 0


def test_fence_expiry_during_transaction_binding_validation_never_reaches_cas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        service,
        prepared,
        receipt,
        fence,
        _candidate,
        canonical,
        base,
        observation_ref,
        _return_commit,
    ) = _task_finalize_fixture(tmp_path)
    now = 1_500
    expired_during_binding = False
    real_validate = worktrees_module._validate_repository_binding

    def expire_after_transaction_binding(repository: object) -> None:
        nonlocal now, expired_during_binding
        real_validate(repository)  # type: ignore[arg-type]
        caller = inspect.currentframe().f_back  # type: ignore[union-attr]
        if caller is None:
            return
        caller_name = caller.f_code.co_name
        run_git_args = caller.f_locals.get("args")
        is_current_transaction = (
            caller_name == "run_git"
            and isinstance(run_git_args, tuple)
            and run_git_args[:2] == ("update-ref", "--stdin")
        )
        is_specialized_transaction = (
            caller_name == "run_checkpoint_ref_transaction"
        )
        if (
            not expired_during_binding
            and (is_current_transaction or is_specialized_transaction)
        ):
            now = 1_601
            expired_during_binding = True

    monkeypatch.setattr(
        worktrees_module,
        "_validate_repository_binding",
        expire_after_transaction_binding,
    )
    service.clock = lambda: now

    with pytest.raises(CheckpointError, match="fence|time|expired"):
        service.finalize(prepared.prepared_ref, receipt, fence)

    assert expired_during_binding is True
    assert _git_text(canonical, "rev-parse", prepared.canonical_ref) == base
    assert _git(canonical, "show-ref", "--verify", observation_ref, check=False).returncode != 0


def test_finalize_creates_eval_ref_at_validated_candidate_commit(
    tmp_path: Path,
) -> None:
    (
        service,
        prepared,
        receipt,
        fence,
        _candidate,
        canonical,
        _base,
        observation_ref,
        candidate_commit,
    ) = _eval_finalize_fixture(tmp_path)

    service.finalize(prepared.prepared_ref, receipt, fence)

    assert _git_text(canonical, "rev-parse", observation_ref) == candidate_commit


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


def test_checkpoint_prepare_allows_unchanged_declared_service_records(
    tmp_path: Path,
) -> None:
    _run_service, manifest, _final = observation_support._install_run_final(tmp_path)
    run_id = str(manifest["run_id"])
    service_paths = tuple(
        sorted(
            (
                f"runs/{run_id}/manifest.json",
                f"runs/{run_id}/final.json",
            )
        )
    )
    _git(tmp_path, "add", "--", *service_paths)
    _git(tmp_path, "commit", "-qm", "version service records")
    base = _git_text(tmp_path, "rev-parse", "HEAD")
    proposal_ref = _write_proposal(
        tmp_path,
        "T-unchanged-service",
        base,
        list(service_paths),
    )
    service = CheckpointService(
        tmp_path,
        canonical_repository=bind_repository(tmp_path),
        canonical_ref=_git_text(tmp_path, "symbolic-ref", "HEAD"),
    )

    audit = service.audit_service.audit(proposal_ref)
    assert audit["mechanically_valid"] is True, audit["issues"]
    prepared = service.prepare(proposal_ref, "unchanged service records")

    receipts = {receipt.path for receipt in prepared.candidate_paths}
    audit_ref = "transitions/T-unchanged-service/audit.json"
    assert receipts == {*service_paths, proposal_ref, audit_ref}
    base_tree = _tree(tmp_path, base)
    candidate_tree = _tree(tmp_path, prepared.candidate_tree)
    assert all(candidate_tree[path] == base_tree[path] for path in service_paths)
    assert {
        path
        for path, entry in candidate_tree.items()
        if base_tree.get(path) != entry
    } == {proposal_ref, audit_ref}


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


def test_checkpoint_file_snapshots_require_an_explicit_bound() -> None:
    parameter = inspect.signature(checkpoint_module._snapshot_file).parameters.get(
        "max_bytes"
    )

    assert parameter is not None
    assert parameter.default is inspect.Parameter.empty


def test_checkpoint_prepare_rejects_oversized_existing_audit_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, proposal_ref, _base = _valid_service(tmp_path)
    audit_path = tmp_path / "transitions/T-checkpoint/audit.json"
    _write_sparse_file(audit_path, checkpoint_module.MAX_AUDIT_FILE_BYTES + 1)
    _reject_opening_snapshot(
        monkeypatch,
        audit_path,
        larger_than=checkpoint_module.MAX_AUDIT_FILE_BYTES,
    )

    with pytest.raises(CheckpointError, match="bound"):
        service.prepare(proposal_ref, "bounded audit snapshot")


def test_checkpoint_prepare_rejects_oversized_user_index_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, proposal_ref, _base = _valid_service(tmp_path)
    index_path = bind_repository(tmp_path).git_dir / "index"
    real_resolve = checkpoint_module.resolve_repository_commit
    resolved = 0

    def enlarge_after_authority_resolution(
        repository: object,
        ref: str,
    ) -> str:
        nonlocal resolved
        commit = real_resolve(repository, ref)  # type: ignore[arg-type]
        resolved += 1
        if resolved == 2:
            _write_sparse_file(index_path, 4_194_305)
        return commit

    monkeypatch.setattr(
        checkpoint_module,
        "resolve_repository_commit",
        enlarge_after_authority_resolution,
    )
    _reject_opening_snapshot(monkeypatch, index_path, larger_than=4_194_304)

    with pytest.raises(CheckpointError, match="bound"):
        service.prepare(proposal_ref, "bounded user index snapshot")


def test_checkpoint_prepare_rejects_oversized_temp_index_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, proposal_ref, _base = _valid_service(tmp_path)
    index_path = tmp_path / ".aros/checkpoints/T-checkpoint/index"
    real_verify = checkpoint_module._verify_candidate_tree

    def enlarge_after_tree_verification(*args: object, **kwargs: object) -> None:
        real_verify(*args, **kwargs)  # type: ignore[arg-type]
        _write_sparse_file(index_path, 4_194_305)

    monkeypatch.setattr(
        checkpoint_module,
        "_verify_candidate_tree",
        enlarge_after_tree_verification,
    )
    _reject_opening_snapshot(monkeypatch, index_path, larger_than=4_194_304)

    with pytest.raises(CheckpointError, match="bound"):
        service.prepare(proposal_ref, "bounded temp index snapshot")


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


def test_checkpoint_prepare_persists_exact_unstaged_principal_message(
    tmp_path: Path,
) -> None:
    service, proposal_ref, _base = _valid_service(tmp_path)
    message = "Principal chose café evidence.\nSecond line."

    prepared = service.prepare(proposal_ref, message)

    message_path = (tmp_path / prepared.prepared_ref).with_name("message")
    assert message_path.read_bytes() == message.encode("utf-8")
    metadata = message_path.stat()
    assert stat.S_ISREG(metadata.st_mode)
    assert metadata.st_nlink == 1
    assert stat.S_IMODE(metadata.st_mode) == 0o600
    assert hashlib.sha256(message_path.read_bytes()).hexdigest() == (
        prepared.message_sha256
    )
    assert all(path != ".aros" for path in _tree(tmp_path, prepared.candidate_tree))
    assert "message" not in _tree(tmp_path, prepared.candidate_tree)


def test_checkpoint_prepare_rejects_drifted_runtime_message_artifact(
    tmp_path: Path,
) -> None:
    service, proposal_ref, _base = _valid_service(tmp_path)
    prepared = service.prepare(proposal_ref, "stable message")
    message_path = (tmp_path / prepared.prepared_ref).with_name("message")
    message_path.write_bytes(b"drifted message")

    with pytest.raises(CheckpointError, match="message|conflict|hash"):
        service.prepare(proposal_ref, "stable message")


def test_checkpoint_prepare_exact_retry_reconstructs_missing_message_artifact(
    tmp_path: Path,
) -> None:
    service, proposal_ref, _base = _valid_service(tmp_path)
    first = service.prepare(proposal_ref, "recover exact message")
    message_path = (tmp_path / first.prepared_ref).with_name("message")
    audit_path = tmp_path / "transitions/T-checkpoint/audit.json"
    message_path.unlink()
    audit_path.unlink()

    second = service.prepare(proposal_ref, "recover exact message")

    assert second == first
    assert message_path.read_bytes() == b"recover exact message"
    assert audit_path.exists()


def test_checkpoint_message_publication_is_atomic_across_partial_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, proposal_ref, _base = _valid_service(tmp_path)
    message_path = tmp_path / ".aros/checkpoints/T-checkpoint/message"
    real_write_all = checkpoint_module._write_all

    def partial_write(descriptor: int, content: bytes) -> None:
        if content == b"atomic message":
            os.write(descriptor, content[:3])
            raise OSError("injected partial message write")
        real_write_all(descriptor, content)

    with monkeypatch.context() as fault:
        fault.setattr(checkpoint_module, "_write_all", partial_write)
        with pytest.raises(CheckpointError, match="partial|message|preparation"):
            service.prepare(proposal_ref, "atomic message")

    assert not message_path.exists()
    assert not any(
        path.name.endswith(".tmp")
        for path in message_path.parent.iterdir()
    )

    prepared = service.prepare(proposal_ref, "atomic message")

    assert message_path.read_bytes() == b"atomic message"
    assert prepared.message_sha256 == hashlib.sha256(b"atomic message").hexdigest()


def test_checkpoint_prepare_never_calls_injected_admission_gateway(
    tmp_path: Path,
) -> None:
    _base, canonical_ref = _init_repository(tmp_path)
    (tmp_path / "memory" / "NOW.md").write_text(
        "# Current State\n\n## Findings\n\nGateway-free prepare.\n",
        encoding="utf-8",
    )
    proposal_ref = _write_proposal(
        tmp_path,
        "T-gateway-free-prepare",
        _git_text(tmp_path, "rev-parse", "HEAD"),
        ["memory/NOW.md"],
    )

    class ForbiddenGateway:
        def admit_transition(self, **_kwargs: object) -> bytes:
            raise AssertionError("prepare called admission")

        def revalidate_transition(self, _receipt: bytes) -> bytes:
            raise AssertionError("prepare called revalidation")

    service = CheckpointService(
        tmp_path,
        canonical_repository=bind_repository(tmp_path),
        canonical_ref=canonical_ref,
        gateway=ForbiddenGateway(),
    )

    service.prepare(proposal_ref, "prepare only")


def test_checkpoint_requires_gateway_before_preparing(tmp_path: Path) -> None:
    service, proposal_ref, _base = _valid_service(tmp_path)

    with pytest.raises(CheckpointError, match="gateway|admission"):
        service.checkpoint(proposal_ref, "requires authority")

    assert not (tmp_path / ".aros/checkpoints/T-checkpoint/prepared.json").exists()


def test_checkpoint_calls_gateway_admit_and_revalidate_once_each(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base, canonical_ref = _init_repository(tmp_path)
    (tmp_path / "memory" / "NOW.md").write_text(
        "# Current State\n\n## Findings\n\nComposite checkpoint.\n",
        encoding="utf-8",
    )
    proposal_ref = _write_proposal(
        tmp_path,
        "T-composite",
        base,
        ["memory/NOW.md"],
    )
    calls: list[tuple[str, object]] = []

    class RecordingGateway:
        def admit_transition(
            self,
            *,
            candidate_subject_sha256: str,
            audit_payload_sha256: str,
            audit_testimony: object,
        ) -> bytes:
            calls.append(
                (
                    "admit",
                    (
                        candidate_subject_sha256,
                        audit_payload_sha256,
                        audit_testimony,
                    ),
                )
            )
            return b"exact receipt"

        def revalidate_transition(self, receipt: bytes) -> bytes:
            calls.append(("revalidate", receipt))
            return b"exact fence"

    service = CheckpointService(
        tmp_path,
        canonical_repository=bind_repository(tmp_path),
        canonical_ref=canonical_ref,
        gateway=RecordingGateway(),
    )

    def finalize(
        prepared_ref: str,
        admission_receipt: bytes,
        finalize_fence: bytes,
    ) -> dict[str, object]:
        calls.append(
            (
                "finalize",
                (prepared_ref, admission_receipt, finalize_fence),
            )
        )
        return {"state": "projection_pending"}

    monkeypatch.setattr(service, "finalize", finalize, raising=False)

    result = service.checkpoint(proposal_ref, "composite message")

    assert result == {"state": "projection_pending"}
    assert [name for name, _value in calls] == ["admit", "revalidate", "finalize"]
    prepared = service.prepare(proposal_ref, "composite message")
    admitted = calls[0][1]
    assert isinstance(admitted, tuple)
    assert admitted[:2] == (
        prepared.candidate_subject_sha256,
        prepared.audit_payload_sha256,
    )
    assert calls[1] == ("revalidate", b"exact receipt")
    assert calls[2] == (
        "finalize",
        (prepared.prepared_ref, b"exact receipt", b"exact fence"),
    )


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

    def replace_before_snapshot(path: Path, *, max_bytes: int) -> object:
        nonlocal replaced
        if path == index_path and path.exists() and not replaced:
            replaced = True
            replacement = path.with_name("replacement-index")
            replacement.write_bytes(base_index_bytes)
            os.replace(replacement, path)
        return real_snapshot(path, max_bytes=max_bytes)

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

    def replace_before_snapshot(path: Path, *, max_bytes: int) -> object:
        nonlocal good_candidate_bytes, snapshot_replaced
        if path == index_path and path.exists() and not snapshot_replaced:
            good_candidate_bytes = path.read_bytes()
            snapshot_replaced = True
            replacement = path.with_name("replacement-index")
            replacement.write_bytes(base_index_bytes)
            os.replace(replacement, path)
        return real_snapshot(path, max_bytes=max_bytes)

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


def test_checkpoint_object_import_rejects_oversized_fetch_head_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = tmp_path / "candidate"
    canonical = tmp_path / "canonical"
    commit, _canonical_ref = _init_repository(candidate)
    _git(tmp_path, "clone", "-q", str(candidate), str(canonical))
    canonical_repository = bind_repository(canonical)
    fetch_head = canonical_repository.git_dir / "FETCH_HEAD"
    _write_sparse_file(fetch_head, 1_048_577)
    _reject_opening_snapshot(monkeypatch, fetch_head, larger_than=1_048_576)

    with pytest.raises(CheckpointError, match="bound"):
        checkpoint_module._import_commit_objects(
            canonical_repository,
            bind_repository(candidate),
            (commit,),
        )


def test_checkpoint_ref_snapshot_avoids_unbounded_run_git_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repository"
    commit, _canonical_ref = _init_repository(root)
    repository = bind_repository(root)
    (repository.git_dir / "packed-refs").write_bytes(
        b"# pack-refs with: peeled fully-peeled sorted\n"
        + commit.encode("ascii")
        + b" refs/heads/"
        + b"x" * 5_000_000
        + b"\n"
    )
    real_run = subprocess.run

    def reject_full_ref_capture(
        command: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        if "for-each-ref" in command and kwargs.get("capture_output") is True:
            raise AssertionError("checkpoint refs used unbounded subprocess capture")
        return real_run(command, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(worktrees_module.subprocess, "run", reject_full_ref_capture)

    with pytest.raises(CheckpointError, match="ref.*bound|bound.*ref"):
        checkpoint_module._snapshot_canonical_refs(
            repository,
            "snapshot canonical refs",
        )


@pytest.mark.parametrize("excess", ("bytes", "count"))
def test_checkpoint_object_import_bounds_canonical_ref_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    excess: str,
) -> None:
    candidate = tmp_path / "candidate"
    canonical = tmp_path / "canonical"
    commit, _canonical_ref = _init_repository(candidate)
    _git(tmp_path, "clone", "-q", str(candidate), str(canonical))
    if excess == "bytes":
        ref_snapshot = b"x" * 4_194_305
    else:
        ref_snapshot = b"".join(
            f"refs/heads/r{index:05d}\0{commit}\n".encode("ascii")
            for index in range(20_001)
        )
    limits: list[tuple[int, int]] = []

    def oversized_ref_snapshot(
        repository: object,
        *,
        max_refs: int,
        max_bytes: int,
    ) -> bytes:
        del repository
        limits.append((max_refs, max_bytes))
        return ref_snapshot

    monkeypatch.setattr(
        checkpoint_module,
        "read_repository_refs_snapshot",
        oversized_ref_snapshot,
    )

    with pytest.raises(CheckpointError, match="ref.*bound|bound.*ref"):
        checkpoint_module._import_commit_objects(
            bind_repository(canonical),
            bind_repository(candidate),
            (commit,),
        )
    assert limits == [(20_000, 4_194_304)]


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
