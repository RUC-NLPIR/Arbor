"""Explicitly cooperative human-direct checkpoint behavior."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

import arbor.aros.checkpoint as checkpoint_module
import arbor.aros.checkpoint_bridge as checkpoint_bridge_module
from arbor.aros.checkpoint import CheckpointError, CheckpointService
from arbor.aros.store import canonical_json_bytes
from arbor.aros.transition_index import TransitionIndex
from arbor.aros.worktrees import bind_repository
from arbor.cli.commands import aros_cmd
from tests import test_aros_checkpoint as checkpoint_support
from tests import test_aros_transition_index as transition_index_support


runner = CliRunner()
HUMAN_DIRECT_FIELDS = {
    "schema_version",
    "receipt_kind",
    "decision",
    "candidate_subject_sha256",
    "audit_payload_sha256",
    "enforcement_class",
    "issuer",
    "issued_at",
    "receipt_sha256",
}


def _workspace(root: Path, transition_id: str) -> tuple[str, str, str]:
    base, canonical_ref = checkpoint_support._init_repository(root)
    (root / "memory" / "NOW.md").write_text(
        "# Current State\n\n## Findings\n\nHuman-reviewed finding.\n",
        encoding="utf-8",
    )
    proposal_ref = checkpoint_support._write_proposal(
        root,
        transition_id,
        base,
        ["memory/NOW.md"],
    )
    return base, canonical_ref, proposal_ref


def _human_receipt(
    *,
    subject: str = "a" * 64,
    audit: str = "b" * 64,
    issued_at: int = 1_234,
) -> bytes:
    gateway = aros_cmd.HumanDirectGateway(clock=lambda: issued_at)
    return gateway.admit_transition(
        candidate_subject_sha256=subject,
        audit_payload_sha256=audit,
        audit_testimony={},
    )


def _rehash_human(value: dict[str, object]) -> bytes:
    payload = {key: item for key, item in value.items() if key != "receipt_sha256"}
    value = {
        **payload,
        "receipt_sha256": hashlib.sha256(canonical_json_bytes(payload)).hexdigest(),
    }
    return canonical_json_bytes(value)


def test_checkpoint_without_explicit_human_flag_fails_without_moving_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base, _canonical_ref, proposal_ref = _workspace(tmp_path, "T-no-human-flag")
    constructed = False

    def forbidden_gateway(*_args: object, **_kwargs: object) -> object:
        nonlocal constructed
        constructed = True
        raise AssertionError("implicit human-direct gateway construction")

    monkeypatch.setattr(
        aros_cmd,
        "HumanDirectGateway",
        forbidden_gateway,
        raising=False,
    )

    result = runner.invoke(
        aros_cmd.aros_app,
        [
            "checkpoint",
            "--cwd",
            str(tmp_path),
            "--proposal",
            proposal_ref,
            "--message",
            "Human checkpoint.",
        ],
    )

    assert result.exit_code == 2
    assert "cooperative" in result.output.lower()
    assert "human-direct" in result.output.lower()
    assert constructed is False
    assert checkpoint_support._git_text(tmp_path, "rev-parse", "HEAD") == base
    assert not (tmp_path / ".aros/checkpoints/T-no-human-flag/prepared.json").exists()


def test_flagged_checkpoint_writes_exact_cooperative_human_receipt(
    tmp_path: Path,
) -> None:
    base, _canonical_ref, proposal_ref = _workspace(tmp_path, "T-human-cli")

    result = runner.invoke(
        aros_cmd.aros_app,
        [
            "checkpoint",
            "--cwd",
            str(tmp_path),
            "--proposal",
            proposal_ref,
            "--message",
            "Explicit human checkpoint.",
            "--cooperative-human-direct",
        ],
    )

    assert result.exit_code == 0, result.output
    output = json.loads(result.output)
    assert output["checkpoint_authority"] == "human-direct"
    assert output["enforcement_class"] == "cooperative"
    assert "mediated" not in result.output.lower()
    assert "protected" not in result.output.lower()
    commit = str(output["commit"])
    assert checkpoint_support._git_text(tmp_path, "rev-parse", "HEAD") == commit
    assert commit != base
    admission_ref = "transitions/T-human-cli/admission.json"
    receipt_raw = (tmp_path / admission_ref).read_bytes()
    assert checkpoint_support._blob(tmp_path, commit, admission_ref) == receipt_raw
    receipt = json.loads(receipt_raw)
    assert set(receipt) == HUMAN_DIRECT_FIELDS
    assert canonical_json_bytes(receipt) == receipt_raw
    assert receipt["schema_version"] == 1
    assert receipt["receipt_kind"] == "human_direct"
    assert receipt["decision"] == "allow"
    assert receipt["enforcement_class"] == "cooperative"
    assert receipt["issuer"] == "human-direct"
    assert type(receipt["issued_at"]) is int and receipt["issued_at"] >= 0
    payload = {
        key: item for key, item in receipt.items() if key != "receipt_sha256"
    }
    assert receipt["receipt_sha256"] == hashlib.sha256(
        canonical_json_bytes(payload)
    ).hexdigest()


def test_receipt_codec_dispatch_keeps_human_and_procontract_schemas_separate() -> None:
    human_raw = _human_receipt()
    procontract_raw = checkpoint_support._allow_receipt_bytes()

    human = checkpoint_module._decode_admission_receipt(human_raw)
    procontract = checkpoint_module._decode_admission_receipt(procontract_raw)

    assert human["receipt_kind"] == "human_direct"
    assert procontract["capability"] == "checkpoint"
    with pytest.raises(CheckpointError, match="field|human|receipt"):
        checkpoint_module._decode_human_direct_admission_receipt(procontract_raw)
    with pytest.raises(CheckpointError, match="field|admission|receipt"):
        checkpoint_module._decode_procontract_admission_receipt(human_raw)


@pytest.mark.parametrize(
    "case",
    ("duplicate", "unknown", "missing", "mixed-camel", "noncanonical"),
)
def test_human_receipt_rejects_duplicate_unknown_missing_mixed_or_noncanonical(
    case: str,
) -> None:
    raw = _human_receipt()
    value = json.loads(raw)
    if case == "duplicate":
        raw = raw.replace(
            b'"decision":"allow"',
            b'"decision":"allow","decision":"allow"',
            1,
        )
    elif case == "unknown":
        value["authority_domain"] = "same-uid"
        raw = _rehash_human(value)
    elif case == "missing":
        value.pop("issuer")
        raw = _rehash_human(value)
    elif case == "mixed-camel":
        value["schemaVersion"] = 1
        raw = _rehash_human(value)
    else:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode(
            "utf-8"
        )

    with pytest.raises(CheckpointError, match="receipt|field|canonical|JSON"):
        checkpoint_module._decode_admission_receipt(raw)


def test_human_gateway_runs_through_shared_checkpoint_logic_and_returns_same_token(
    tmp_path: Path,
) -> None:
    _base, canonical_ref, proposal_ref = _workspace(tmp_path, "T-human-shared")
    calls: list[str] = []

    class RecordingGateway(aros_cmd.HumanDirectGateway):
        def admit_transition(self, **kwargs: object) -> bytes:
            calls.append("admit")
            return super().admit_transition(**kwargs)  # type: ignore[arg-type]

        def revalidate_transition(self, receipt: bytes) -> bytes:
            calls.append("revalidate")
            token = super().revalidate_transition(receipt)
            assert token == receipt
            return token

    service = CheckpointService(
        tmp_path,
        canonical_repository=bind_repository(tmp_path),
        canonical_ref=canonical_ref,
        gateway=RecordingGateway(clock=lambda: 4_321),
        clock=lambda: (_ for _ in ()).throw(
            AssertionError("human-direct authorization used expiry time")
        ),
    )

    result = service.checkpoint(proposal_ref, "Shared checkpoint logic.")

    assert result["state"] == "admitted"
    assert calls == ["admit", "revalidate"]
    receipt = (
        tmp_path / "transitions/T-human-shared/admission.json"
    ).read_bytes()
    assert json.loads(receipt)["issued_at"] == 4_321


def test_human_finalize_requires_fence_bytes_to_equal_receipt(
    tmp_path: Path,
) -> None:
    base, canonical_ref, proposal_ref = _workspace(tmp_path, "T-human-token")
    service = CheckpointService(
        tmp_path,
        canonical_repository=bind_repository(tmp_path),
        canonical_ref=canonical_ref,
    )
    prepared = service.prepare(proposal_ref, "Human token equality.")
    receipt = aros_cmd.HumanDirectGateway(clock=lambda: 1_000).admit_transition(
        candidate_subject_sha256=prepared.candidate_subject_sha256,
        audit_payload_sha256=prepared.audit_payload_sha256,
        audit_testimony=prepared.audit_testimony,
    )

    with pytest.raises(CheckpointError, match="human|token|fence|equal|receipt"):
        service.finalize(prepared.prepared_ref, receipt, b"different token")

    assert checkpoint_support._git_text(tmp_path, "rev-parse", "HEAD") == base
    assert not (tmp_path / "transitions/T-human-token/admission.json").exists()


def test_model_checkpoint_surface_has_no_human_route_selector() -> None:
    for method_name in ("prepare", "finalize", "checkpoint"):
        parameters = inspect.signature(
            getattr(CheckpointService, method_name)
        ).parameters
        assert not any(
            "human" in name or "cooperative" in name for name in parameters
        )
    assert not hasattr(checkpoint_module, "HumanDirectGateway")
    for fields in checkpoint_bridge_module._REQUEST_FIELDS.values():
        assert not any(
            "human" in field or "cooperative" in field for field in fields
        )


def test_human_gateway_is_not_publicly_exported_from_cli_module() -> None:
    assert "HumanDirectGateway" not in aros_cmd.__all__


def test_checkpoint_help_labels_human_direct_as_cooperative_only() -> None:
    result = runner.invoke(aros_cmd.aros_app, ["checkpoint", "--help"])

    assert result.exit_code == 0, result.output
    help_text = result.output.lower()
    assert "human-direct" in help_text
    assert "cooperative" in help_text
    assert "mediated" not in help_text
    assert "protected" not in help_text


def _human_assimilation_followup(
    fixture: transition_index_support._AdmittedFixture,
) -> tuple[str, str]:
    transition_id = "T-human-index"
    base = checkpoint_support._git_text(fixture.candidate, "rev-parse", "HEAD")
    now = fixture.candidate / "memory" / "NOW.md"
    now.write_text(
        now.read_text(encoding="utf-8")
        + "\n## Human review\n\nHuman-direct review retained this evidence.\n",
        encoding="utf-8",
    )
    proposal_ref = checkpoint_support._write_proposal(
        fixture.candidate,
        transition_id,
        base,
        ["memory/NOW.md"],
        [
            {
                "observation_ref": fixture.observation_ref,
                "affected_paths": ["memory/NOW.md"],
                "rationale": "memory/NOW.md#Human review",
            }
        ],
    )
    service = CheckpointService(
        fixture.candidate,
        canonical_repository=bind_repository(fixture.canonical),
        canonical_ref=fixture.canonical_ref,
        gateway=aros_cmd.HumanDirectGateway(clock=lambda: 9_876),
    )
    result = service.checkpoint(proposal_ref, "Cooperative human assimilation.\n")
    return transition_id, str(result["commit"])


def test_transition_index_accepts_ancestral_human_direct_admission(
    tmp_path: Path,
) -> None:
    fixture = transition_index_support._admitted_assimilation(tmp_path)
    transition_id, human_commit = _human_assimilation_followup(fixture)
    tree = checkpoint_support._git_text(
        fixture.canonical,
        "rev-parse",
        f"{human_commit}^{{tree}}",
    )
    later = checkpoint_support._git_text(
        fixture.canonical,
        "commit-tree",
        tree,
        "-p",
        human_commit,
        "-m",
        "later canonical operation",
    )
    checkpoint_support._git(
        fixture.canonical,
        "update-ref",
        fixture.canonical_ref,
        later,
        human_commit,
    )

    rebuilt = TransitionIndex(
        bind_repository(fixture.candidate),
        bind_repository(fixture.canonical),
    ).rebuild()

    assert rebuilt.state == "complete"
    records = rebuilt.assimilations[fixture.observation_ref]
    assert any(
        record.transition_id == transition_id and record.commit == human_commit
        for record in records
    )
    receipt_raw = checkpoint_support._git(
        fixture.canonical,
        "show",
        f"{human_commit}:transitions/{transition_id}/admission.json",
    ).stdout
    receipt = json.loads(receipt_raw)
    assert receipt["receipt_kind"] == "human_direct"
    assert receipt["enforcement_class"] == "cooperative"
    assert TransitionIndex(
        bind_repository(fixture.candidate),
        bind_repository(fixture.canonical),
    ).read() == rebuilt


def _replace_admission_commit(
    fixture: transition_index_support._AdmittedFixture,
    transition_id: str,
    commit: str,
    receipt_raw: bytes,
    index_path: Path,
) -> str:
    admission_ref = f"transitions/{transition_id}/admission.json"
    object_id = subprocess.run(
        ["git", "-C", str(fixture.canonical), "hash-object", "-w", "--stdin"],
        input=receipt_raw,
        check=True,
        capture_output=True,
        text=False,
    ).stdout.decode("ascii").strip()
    environment = {**os.environ, "GIT_INDEX_FILE": str(index_path)}

    def indexed_git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(fixture.canonical), *args],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        ).stdout.strip()

    indexed_git("read-tree", f"{commit}^{{tree}}")
    indexed_git(
        "update-index",
        "--add",
        "--cacheinfo",
        "100644",
        object_id,
        admission_ref,
    )
    tree = indexed_git("write-tree")
    parent = checkpoint_support._git_text(fixture.canonical, "rev-parse", f"{commit}^")
    replacement = checkpoint_support._git_text(
        fixture.canonical,
        "commit-tree",
        tree,
        "-p",
        parent,
        "-m",
        "invalid human admission",
    )
    checkpoint_support._git(
        fixture.canonical,
        "update-ref",
        fixture.canonical_ref,
        replacement,
        commit,
    )
    return replacement


@pytest.mark.parametrize("case", ("copied", "malformed", "mixed"))
def test_transition_index_rejects_copied_malformed_or_mixed_human_receipt(
    tmp_path: Path,
    case: str,
) -> None:
    fixture = transition_index_support._admitted_assimilation(tmp_path)
    transition_id, human_commit = _human_assimilation_followup(fixture)
    admission_ref = f"transitions/{transition_id}/admission.json"
    if case == "copied":
        copied_id = "T-human-copy"
        environment = {
            **os.environ,
            "GIT_INDEX_FILE": str(tmp_path / "copied.index"),
        }
        subprocess.run(
            [
                "git",
                "-C",
                str(fixture.canonical),
                "read-tree",
                f"{human_commit}^{{tree}}",
            ],
            check=True,
            env=environment,
        )
        for name in ("proposal.json", "audit.json", "admission.json"):
            raw = checkpoint_support._git(
                fixture.canonical,
                "show",
                f"{human_commit}:transitions/{transition_id}/{name}",
            ).stdout
            object_id = subprocess.run(
                [
                    "git",
                    "-C",
                    str(fixture.canonical),
                    "hash-object",
                    "-w",
                    "--stdin",
                ],
                input=raw,
                check=True,
                capture_output=True,
            ).stdout.decode("ascii").strip()
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(fixture.canonical),
                    "update-index",
                    "--add",
                    "--cacheinfo",
                    "100644",
                    object_id,
                    f"transitions/{copied_id}/{name}",
                ],
                check=True,
                env=environment,
            )
        copied_tree = subprocess.run(
            ["git", "-C", str(fixture.canonical), "write-tree"],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        ).stdout.strip()
        copied_commit = checkpoint_support._git_text(
            fixture.canonical,
            "commit-tree",
            copied_tree,
            "-p",
            human_commit,
            "-m",
            "copied human receipt",
        )
        checkpoint_support._git(
            fixture.canonical,
            "update-ref",
            fixture.canonical_ref,
            copied_commit,
            human_commit,
        )
        changed = checkpoint_support._git(
            fixture.canonical,
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "-r",
            human_commit,
            copied_commit,
        ).stdout.decode("utf-8").splitlines()
        assert set(changed) == {
            f"transitions/{copied_id}/{name}"
            for name in ("proposal.json", "audit.json", "admission.json")
        }
    else:
        receipt = json.loads(
            checkpoint_support._git(
                fixture.canonical,
                "show",
                f"{human_commit}:{admission_ref}",
            ).stdout
        )
        if case == "malformed":
            receipt["receipt_sha256"] = "0" * 64
            receipt_raw = canonical_json_bytes(receipt)
        else:
            receipt["schemaVersion"] = 1
            receipt_raw = _rehash_human(receipt)
        _replace_admission_commit(
            fixture,
            transition_id,
            human_commit,
            receipt_raw,
            tmp_path / f"{case}.index",
        )

    state = TransitionIndex(
        bind_repository(fixture.candidate),
        bind_repository(fixture.canonical),
    ).rebuild()

    assert state.state == "index_incomplete"
    assert state.assimilations == {}
    assert state.latest_evidence_transition is None
