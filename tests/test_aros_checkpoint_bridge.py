"""Strict internal JSON-lines boundary for broker-owned checkpoints."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from arbor.aros.attention import AttentionAuthorityContext, ResearchAttentionService
from arbor.aros.checkpoint import PreparedCheckpoint
from arbor.aros.store import canonical_json_bytes
from arbor.aros.worktrees import bind_repository
from tests import test_aros_attention as attention_support
from tests import test_aros_checkpoint as checkpoint_support


PYTHON = "/workspace/Arbor/.venv/bin/python"
SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
BOOTSTRAP = (
    "import runpy,sys,types;"
    "package=types.ModuleType('arbor');"
    f"package.__path__=[{str(SOURCE_ROOT)!r}];"
    "sys.modules['arbor']=package;"
    "runpy.run_module('arbor.aros.checkpoint_bridge',run_name='__main__')"
)
ERROR_RESPONSE = {"error": "checkpoint_bridge_rejected", "schema_version": 1}


def _request(value: dict[str, object]) -> bytes:
    return canonical_json_bytes(value) + b"\n"


def _run_bridge(
    command: str,
    candidate: Path,
    canonical: Path,
    canonical_ref: str,
    raw: bytes,
    *,
    barrier: tuple[Path, str, int] | None = None,
    isolated: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    arguments = [
        PYTHON,
    ]
    if isolated:
        arguments.append("-I")
    arguments.extend(
        [
            "-c",
            BOOTSTRAP,
            command,
            str(candidate),
            str(canonical),
            canonical_ref,
        ]
    )
    pass_fds: tuple[int, ...] = ()
    if barrier is not None:
        runtime, point, control_fd = barrier
        arguments.extend(
            [
                "--barrier-runtime",
                str(runtime),
                "--barrier-point",
                point,
                "--barrier-fd",
                str(control_fd),
            ]
        )
        pass_fds = (control_fd,)
    return subprocess.run(
        arguments,
        input=raw,
        capture_output=True,
        check=False,
        pass_fds=pass_fds,
    )


def _response(process: subprocess.CompletedProcess[bytes]) -> dict[str, object]:
    assert process.stdout.endswith(b"\n")
    assert process.stdout.count(b"\n") == 1
    value = json.loads(process.stdout)
    assert isinstance(value, dict)
    assert process.stdout == canonical_json_bytes(value) + b"\n"
    return value


def test_bridge_prepare_returns_exact_prepared_checkpoint_without_admission(
    tmp_path: Path,
) -> None:
    (
        _service,
        prepared,
        _receipt,
        _fence,
        candidate,
        canonical,
        base,
        message,
    ) = checkpoint_support._finalize_fixture(
        tmp_path,
        transition_id="T-bridge-prepare",
    )

    process = _run_bridge(
        "prepare",
        candidate,
        canonical,
        prepared.canonical_ref,
        _request({"message": message, "proposal_ref": prepared.proposal_ref}),
    )

    assert process.returncode == 0
    assert process.stderr == b""
    response = _response(process)
    assert set(response) == set(PreparedCheckpoint.__dataclass_fields__)
    assert response["transition_id"] == prepared.transition_id
    assert response["prepared_ref"] == prepared.prepared_ref
    assert response["candidate_subject_sha256"] == (
        prepared.candidate_subject_sha256
    )
    assert response["audit_payload_sha256"] == prepared.audit_payload_sha256
    assert response["canonical_ref"] == prepared.canonical_ref
    assert isinstance(response["audit_testimony"], dict)
    assert isinstance(response["candidate_paths"], list)
    assert checkpoint_support._git_text(canonical, "rev-parse", prepared.canonical_ref) == base
    assert checkpoint_support._git_text(candidate, "rev-parse", prepared.canonical_ref) == base
    assert not (
        candidate / f"transitions/{prepared.transition_id}/admission.json"
    ).exists()


def test_bridge_finalize_round_trips_exact_receipt_and_fence_bytes(
    tmp_path: Path,
) -> None:
    (
        _service,
        prepared,
        receipt,
        fence,
        candidate,
        canonical,
        _base,
        _message,
    ) = checkpoint_support._finalize_fixture(
        tmp_path,
        transition_id="T-bridge-finalize",
    )
    now_ms = int(time.time() * 1_000)
    receipt = checkpoint_support._allow_receipt_bytes(
        candidate_subject_sha256=prepared.candidate_subject_sha256,
        audit_payload_sha256=prepared.audit_payload_sha256,
        canonical_ref=prepared.canonical_ref,
        lease_expires_at=now_ms + 60_000,
    )
    fence = checkpoint_support._fence_bytes(
        checkpoint_support._decoded(receipt),
        issued_at=now_ms - 1_000,
        expires_at=now_ms + 60_000,
    )
    request = {
        "admission_receipt_base64": base64.b64encode(receipt).decode("ascii"),
        "finalize_fence_base64": base64.b64encode(fence).decode("ascii"),
        "prepared_ref": prepared.prepared_ref,
    }

    process = _run_bridge(
        "finalize",
        candidate,
        canonical,
        prepared.canonical_ref,
        _request(request),
    )

    assert process.returncode == 0
    assert process.stderr == b""
    response = _response(process)
    commit = str(response["commit"])
    assert response == {
        "schema_version": 1,
        "transition_id": prepared.transition_id,
        "canonical_ref": prepared.canonical_ref,
        "commit": commit,
        "state": "admitted",
    }
    admission_ref = f"transitions/{prepared.transition_id}/admission.json"
    assert (candidate / admission_ref).read_bytes() == receipt
    assert checkpoint_support._blob(canonical, commit, admission_ref) == receipt


def test_bridge_attention_injects_exact_host_context_into_single_builder(
    tmp_path: Path,
) -> None:
    canonical = tmp_path / "canonical"
    candidate = tmp_path / "candidate"
    canonical.mkdir()
    attention_support._init_workspace(canonical)
    subprocess.run(["git", "clone", "-q", str(canonical), str(candidate)], check=True)
    canonical_ref = checkpoint_support._git_text(canonical, "symbolic-ref", "HEAD")
    request = {
        "authority": {
            "state": "available",
            "enforcement_class": "mediated-test",
            "capabilities": ["checkpoint"],
        },
        "institutional_obligations": [
            {"kind": "lease", "obligation_id": "OBL-bridge"}
        ],
        "remaining_budget": {
            "state": "available",
            "enforcement_class": "hard",
            "remaining_actions": 7,
        },
    }
    context = AttentionAuthorityContext(
        authority=request["authority"],
        remaining_budget=request["remaining_budget"],
        institutional_obligations=tuple(request["institutional_obligations"]),
    )
    expected = ResearchAttentionService(
        candidate,
        canonical_repository=bind_repository(canonical),
    ).build(context=context)

    process = _run_bridge(
        "attention",
        candidate,
        canonical,
        canonical_ref,
        _request(request),
    )

    assert process.returncode == 0
    assert process.stderr == b""
    assert _response(process) == expected


def test_bridge_prepare_accepts_host_barrier_only_from_argv_and_inherited_fd(
    tmp_path: Path,
) -> None:
    (
        _service,
        prepared,
        _receipt,
        _fence,
        candidate,
        canonical,
        _base,
        message,
    ) = checkpoint_support._finalize_fixture(
        tmp_path,
        transition_id="T-bridge-barrier",
    )
    runtime = candidate / ".aros" / "checkpoints" / "bridge-barrier-host"
    runtime.mkdir()
    read_fd, write_fd = os.pipe()
    processes: list[subprocess.CompletedProcess[bytes]] = []
    failures: list[BaseException] = []

    def invoke() -> None:
        try:
            processes.append(
                _run_bridge(
                    "prepare",
                    candidate,
                    canonical,
                    prepared.canonical_ref,
                    _request(
                        {
                            "message": message,
                            "proposal_ref": prepared.proposal_ref,
                        }
                    ),
                    barrier=(runtime, "after_tree", read_fd),
                )
            )
        except BaseException as error:
            failures.append(error)

    try:
        thread = threading.Thread(target=invoke)
        thread.start()
        marker = runtime / "after_tree.marker"
        deadline = time.monotonic() + 3.0
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.read_bytes() == b"after_tree\n"
        assert thread.is_alive()
        os.write(write_fd, b"A")
        thread.join(timeout=3.0)
        assert not thread.is_alive()
    finally:
        os.close(read_fd)
        os.close(write_fd)

    assert failures == []
    assert len(processes) == 1
    assert processes[0].returncode == 0
    assert processes[0].stderr == b""
    assert _response(processes[0])["prepared_ref"] == prepared.prepared_ref


def test_bridge_main_rejects_nonisolated_interpreter_with_fixed_response(
    tmp_path: Path,
) -> None:
    (
        _service,
        prepared,
        _receipt,
        _fence,
        candidate,
        canonical,
        _base,
        message,
    ) = checkpoint_support._finalize_fixture(
        tmp_path,
        transition_id="T-bridge-nonisolated",
    )

    process = _run_bridge(
        "prepare",
        candidate,
        canonical,
        prepared.canonical_ref,
        _request({"message": message, "proposal_ref": prepared.proposal_ref}),
        isolated=False,
    )

    assert process.returncode != 0
    assert process.stderr == b""
    assert _response(process) == ERROR_RESPONSE


@pytest.mark.parametrize(
    "raw",
    (
        b'{"message":"x","message":"x","proposal_ref":"transitions/T-x/proposal.json"}\n',
        _request({"message": "x"}),
        _request(
            {
                "candidate_root": "/model/supplied",
                "message": "x",
                "proposal_ref": "transitions/T-x/proposal.json",
            }
        ),
        _request(
            {
                "barrier_point": "after_cas",
                "message": "x",
                "proposal_ref": "transitions/T-x/proposal.json",
            }
        ),
        b'{ "message":"x","proposal_ref":"transitions/T-x/proposal.json"}\n',
        _request(
            {
                "message": "x",
                "proposal_ref": "transitions/T-x/proposal.json",
            }
        )
        + b"{}\n",
        b"\xff\n",
        b'{"message":"' + b"x" * 2_100_000 + b'","proposal_ref":"x"}\n',
    ),
    ids=(
        "duplicate",
        "missing",
        "authority-root",
        "barrier-field",
        "noncanonical",
        "extra-line",
        "non-utf8",
        "oversized",
    ),
)
def test_bridge_rejects_duplicate_missing_authority_noncanonical_or_oversized_input(
    tmp_path: Path,
    raw: bytes,
) -> None:
    candidate = tmp_path / "candidate"
    canonical = tmp_path / "canonical"
    _base, canonical_ref = checkpoint_support._init_repository(candidate)
    subprocess.run(["git", "clone", "-q", str(candidate), str(canonical)], check=True)

    process = _run_bridge(
        "prepare",
        candidate,
        canonical,
        canonical_ref,
        raw,
    )

    assert process.returncode != 0
    assert process.stderr == b""
    assert _response(process) == ERROR_RESPONSE
    assert b"model/supplied" not in process.stdout


@pytest.mark.parametrize(
    "field",
    ("admission_receipt_base64", "finalize_fence_base64"),
)
def test_bridge_rejects_noncanonical_base64_without_leaking_broker_bytes(
    tmp_path: Path,
    field: str,
) -> None:
    candidate = tmp_path / "candidate"
    canonical = tmp_path / "canonical"
    _base, canonical_ref = checkpoint_support._init_repository(candidate)
    subprocess.run(["git", "clone", "-q", str(candidate), str(canonical)], check=True)
    request = {
        "admission_receipt_base64": base64.b64encode(b"broker-secret").decode("ascii"),
        "finalize_fence_base64": base64.b64encode(b"broker-fence").decode("ascii"),
        "prepared_ref": ".aros/checkpoints/T-secret/prepared.json",
    }
    request[field] += "="

    process = _run_bridge(
        "finalize",
        candidate,
        canonical,
        canonical_ref,
        _request(request),
    )

    assert process.returncode != 0
    assert process.stderr == b""
    assert _response(process) == ERROR_RESPONSE
    assert b"broker-secret" not in process.stdout
    assert b"broker-fence" not in process.stdout


def test_bridge_rejects_unknown_command_with_one_deterministic_response(
    tmp_path: Path,
) -> None:
    candidate = tmp_path / "candidate"
    canonical = tmp_path / "canonical"
    _base, canonical_ref = checkpoint_support._init_repository(candidate)
    subprocess.run(["git", "clone", "-q", str(candidate), str(canonical)], check=True)

    first = _run_bridge("reconcile", candidate, canonical, canonical_ref, b"{}\n")
    second = _run_bridge("reconcile", candidate, canonical, canonical_ref, b"{}\n")

    assert first.returncode != 0
    assert second.returncode != 0
    assert first.stderr == second.stderr == b""
    assert first.stdout == second.stdout == canonical_json_bytes(ERROR_RESPONSE) + b"\n"
