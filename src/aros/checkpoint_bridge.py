"""Internal one-request/one-response checkpoint broker boundary."""

from __future__ import annotations

import base64
import binascii
import sys
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import BinaryIO

from .attention import AttentionAuthorityContext, ResearchAttentionService
from .checkpoint import (
    MAX_ADMISSION_RECEIPT_BYTES,
    MAX_FINALIZE_FENCE_BYTES,
    CheckpointService,
    HostCheckpointBarrier,
    PreparedCheckpoint,
)
from .store import (
    JsonStructureError,
    _strict_json_loads,
    canonical_json_bytes,
    validate_json_shape,
)
from .worktrees import (
    bind_repository,
    read_repository_snapshot,
    resolve_repository_commit,
)


_COMMANDS = {"attention", "prepare", "finalize"}
_REQUEST_FIELDS = {
    "attention": {
        "authority",
        "institutional_obligations",
        "remaining_budget",
    },
    "prepare": {"message", "proposal_ref"},
    "finalize": {
        "admission_receipt_base64",
        "finalize_fence_base64",
        "prepared_ref",
    },
}
_ERROR_RESPONSE = {"error": "checkpoint_bridge_rejected", "schema_version": 1}
_MAX_REQUEST_BYTES = 2_000_000
_MAX_REQUEST_DEPTH = 24
_MAX_REQUEST_NODES = 50_000


class _BridgeError(ValueError):
    pass


def main(argv: list[str] | None = None) -> int:
    if not sys.flags.isolated:
        _write_response(_ERROR_RESPONSE)
        return 2
    arguments = sys.argv[1:] if argv is None else argv
    try:
        response = _dispatch(arguments, sys.stdin.buffer)
    except Exception:
        _write_response(_ERROR_RESPONSE)
        return 2
    _write_response(response)
    return 0


def _dispatch(arguments: list[str], stream: BinaryIO) -> dict[str, object]:
    if len(arguments) not in {4, 10} or arguments[0] not in _COMMANDS:
        raise _BridgeError("invalid bridge argv")
    command, candidate_arg, canonical_arg, canonical_ref = arguments[:4]
    candidate = bind_repository(Path(candidate_arg))
    canonical = bind_repository(Path(canonical_arg))
    for repository in (candidate, canonical):
        snapshot = read_repository_snapshot(repository)
        if snapshot.get("ref") != canonical_ref:
            raise _BridgeError("bridge repository ref mismatch")
        resolve_repository_commit(repository, canonical_ref)
    barrier = None
    if len(arguments) == 10:
        if command == "attention" or arguments[4::2] != [
            "--barrier-runtime",
            "--barrier-point",
            "--barrier-fd",
        ]:
            raise _BridgeError("invalid bridge barrier argv")
        runtime = Path(arguments[5]).absolute()
        try:
            relative_runtime = runtime.relative_to(candidate.root)
        except ValueError as error:
            raise _BridgeError("bridge barrier runtime is outside candidate") from error
        if relative_runtime.parts[:2] != (".aros", "checkpoints"):
            raise _BridgeError("bridge barrier runtime is outside checkpoints")
        raw_fd = arguments[9]
        if not raw_fd.isascii() or not raw_fd.isdecimal():
            raise _BridgeError("bridge barrier FD is invalid")
        control_fd = int(raw_fd)
        if str(control_fd) != raw_fd:
            raise _BridgeError("bridge barrier FD is not canonical")
        barrier = HostCheckpointBarrier(
            runtime,
            point=arguments[7],
            control_fd=control_fd,
        )
    request = _read_request(stream, command)

    if command == "attention":
        obligations = request["institutional_obligations"]
        if not isinstance(obligations, list) or any(
            not isinstance(item, dict) for item in obligations
        ):
            raise _BridgeError("invalid attention obligations")
        context = AttentionAuthorityContext(
            authority=_mapping(request["authority"], "authority"),
            remaining_budget=_mapping(
                request["remaining_budget"],
                "remaining_budget",
            ),
            institutional_obligations=tuple(obligations),
        )
        return ResearchAttentionService(
            candidate.root,
            canonical_repository=canonical,
        ).build(context=context)

    service = CheckpointService(
        candidate.root,
        canonical_repository=canonical,
        canonical_ref=canonical_ref,
        barrier=barrier,
    )
    if command == "prepare":
        proposal_ref = _string(request["proposal_ref"], "proposal_ref")
        message = _string(request["message"], "message")
        return _prepared_response(service.prepare(proposal_ref, message))

    prepared_ref = _string(request["prepared_ref"], "prepared_ref")
    receipt = _decode_base64(
        request["admission_receipt_base64"],
        "admission_receipt_base64",
        MAX_ADMISSION_RECEIPT_BYTES,
    )
    fence = _decode_base64(
        request["finalize_fence_base64"],
        "finalize_fence_base64",
        MAX_FINALIZE_FENCE_BYTES,
    )
    return service.finalize(prepared_ref, receipt, fence)


def _read_request(stream: BinaryIO, command: str) -> dict[str, object]:
    raw = stream.read(_MAX_REQUEST_BYTES + 1)
    if (
        not raw
        or len(raw) > _MAX_REQUEST_BYTES
        or not raw.endswith(b"\n")
        or raw.count(b"\n") != 1
    ):
        raise _BridgeError("invalid bridge request framing")
    payload = raw[:-1]
    try:
        value = _strict_json_loads(payload)
        validate_json_shape(
            value,
            max_depth=_MAX_REQUEST_DEPTH,
            max_nodes=_MAX_REQUEST_NODES,
        )
    except (JsonStructureError, TypeError, UnicodeError, ValueError) as error:
        raise _BridgeError("invalid bridge request JSON") from error
    if (
        not isinstance(value, dict)
        or set(value) != _REQUEST_FIELDS[command]
        or payload != canonical_json_bytes(value)
    ):
        raise _BridgeError("invalid bridge request object")
    return value


def _decode_base64(value: object, field: str, maximum: int) -> bytes:
    text = _string(value, field)
    try:
        encoded = text.encode("ascii")
        decoded = base64.b64decode(encoded, validate=True)
    except (UnicodeError, binascii.Error, ValueError) as error:
        raise _BridgeError(f"invalid {field}") from error
    if len(decoded) > maximum or base64.b64encode(decoded) != encoded:
        raise _BridgeError(f"invalid {field}")
    return decoded


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise _BridgeError(f"invalid {field}")
    return value


def _string(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise _BridgeError(f"invalid {field}")
    return value


def _prepared_response(prepared: PreparedCheckpoint) -> dict[str, object]:
    return {
        field.name: _thaw(getattr(prepared, field.name))
        for field in fields(PreparedCheckpoint)
    }


def _thaw(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _thaw(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_thaw(item) for item in value]
    return value


def _write_response(response: Mapping[str, object]) -> None:
    sys.stdout.buffer.write(canonical_json_bytes(response) + b"\n")
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    raise SystemExit(main())
