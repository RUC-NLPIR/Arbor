from __future__ import annotations

import hashlib
import json
import math
import os
import re
import ctypes
import secrets
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Literal


HEX64 = re.compile(r"[0-9a-f]{64}\Z")
SOURCE_COMMIT = "da022c2945146e9577d91375a48d53850d7041a3"
_TRACE_KEYS = {
    "trace_id",
    "split",
    "organization",
    "application",
    "dataset",
    "provenance_url",
    "license_ref",
    "path",
    "trace_type",
    "origin_sha256",
    "start_request",
    "warmup_seconds",
    "max_requests",
    "working_set_bytes",
}
_PORTFOLIO_KEYS = {"schema_version", "source_commit", "cache_fractions", "traces"}


class ContractError(ValueError):
    pass


def quarantine_unlink(
    directory_descriptor: int,
    name: str,
    identity: tuple[int, int],
    *,
    sha256: str | None = None,
    raw: bytes | None = None,
    fsync_directory: bool = True,
) -> None:
    quarantine = f".quarantine-{secrets.token_hex(16)}"
    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError as error:
        raise ContractError("atomic quarantine rename is unavailable") from error
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int

    def rename(source: str, target: str) -> None:
        result = renameat2(
            directory_descriptor,
            os.fsencode(source),
            directory_descriptor,
            os.fsencode(target),
            1,
        )
        if result != 0:
            number = ctypes.get_errno()
            raise OSError(number, os.strerror(number), source)

    rename(name, quarantine)
    verified = False
    try:
        descriptor = os.open(
            quarantine,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_descriptor,
        )
        digest_value: str | None = None
        observed_value: bytes | None = None
        try:
            metadata = os.fstat(descriptor)
            if sha256 is not None or raw is not None:
                digest = hashlib.sha256()
                observed = bytearray() if raw is not None else None
                with os.fdopen(descriptor, "rb") as stream:
                    descriptor = -1
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        if sha256 is not None:
                            digest.update(block)
                        if observed is not None:
                            observed.extend(block)
                digest_value = digest.hexdigest() if sha256 is not None else None
                observed_value = bytes(observed) if observed is not None else None
            else:
                os.close(descriptor)
                descriptor = -1
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        verified = (
            (metadata.st_dev, metadata.st_ino) == identity
            and (sha256 is None or digest_value == sha256)
            and (raw is None or observed_value == raw)
        )
        if not verified:
            raise ContractError(f"owned file changed before quarantine unlink: {name}")
        os.unlink(quarantine, dir_fd=directory_descriptor)
        if fsync_directory:
            os.fsync(directory_descriptor)
    except BaseException:
        if not verified:
            try:
                rename(quarantine, name)
            except OSError:
                pass
        raise


def _exact_keys(value: Mapping[str, object], expected: set[str], label: str) -> None:
    if set(value) != expected:
        missing = sorted(expected - set(value))
        unknown = sorted(set(value) - expected)
        raise ContractError(f"{label} keys mismatch: missing={missing}, unknown={unknown}")


def _nonempty_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{label} must be a nonempty string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ContractError(f"{label} must be valid UTF-8") from error
    return value


def _integer(value: object, label: str, *, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise ContractError(f"{label} must be an integer >= {minimum}")
    return value


def _hash_regular_file(path: Path) -> tuple[str, int]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    digest = hashlib.sha256()
    size = 0
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ContractError(f"trace path is not a regular file: {path}")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
                size += len(block)
        if size != metadata.st_size:
            raise ContractError(f"trace file changed while hashing: {path}")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return digest.hexdigest(), size


@dataclass(frozen=True)
class TraceWindow:
    trace_id: str
    split: Literal["dev", "visible", "r3"]
    organization: str
    application: str
    dataset: str
    provenance_url: str
    license_ref: str
    path: Path
    trace_type: Literal["oracleGeneral"]
    origin_sha256: str
    start_request: int
    warmup_seconds: int
    max_requests: int
    working_set_bytes: int
    sha256: str
    size_bytes: int

    @classmethod
    def from_candidate(cls, candidate: Mapping[str, object]) -> TraceWindow:
        _exact_keys(candidate, _TRACE_KEYS, "trace")
        trace_id = _nonempty_string(candidate["trace_id"], "trace_id")
        split = candidate["split"]
        if not isinstance(split, str) or split not in {"dev", "visible", "r3"}:
            raise ContractError("split must be dev, visible, or r3")
        organization = _nonempty_string(candidate["organization"], "organization")
        application = _nonempty_string(candidate["application"], "application")
        dataset = _nonempty_string(candidate["dataset"], "dataset")
        provenance_url = _nonempty_string(candidate["provenance_url"], "provenance_url")
        license_ref = _nonempty_string(candidate["license_ref"], "license_ref")
        raw_path = _nonempty_string(candidate["path"], "path")
        path = Path(raw_path)
        if not path.is_absolute():
            raise ContractError(f"trace path must be absolute: {path}")
        metadata = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise ContractError(f"trace path must be a regular non-symlink: {path}")
        resolved = path.resolve(strict=True)
        trace_type = candidate["trace_type"]
        if trace_type != "oracleGeneral":
            raise ContractError("trace_type must be oracleGeneral")
        origin_sha256 = _nonempty_string(candidate["origin_sha256"], "origin_sha256")
        if HEX64.fullmatch(origin_sha256) is None:
            raise ContractError("origin_sha256 must be a lowercase SHA-256")
        start_request = _integer(candidate["start_request"], "start_request", minimum=0)
        warmup_seconds = _integer(candidate["warmup_seconds"], "warmup_seconds", minimum=1)
        max_requests = _integer(candidate["max_requests"], "max_requests", minimum=1)
        working_set_bytes = _integer(
            candidate["working_set_bytes"], "working_set_bytes", minimum=1
        )
        sha256, size_bytes = _hash_regular_file(resolved)
        return cls(
            trace_id=trace_id,
            split=split,
            organization=organization,
            application=application,
            dataset=dataset,
            provenance_url=provenance_url,
            license_ref=license_ref,
            path=resolved,
            trace_type=trace_type,
            origin_sha256=origin_sha256,
            start_request=start_request,
            warmup_seconds=warmup_seconds,
            max_requests=max_requests,
            working_set_bytes=working_set_bytes,
            sha256=sha256,
            size_bytes=size_bytes,
        )


@dataclass(frozen=True)
class Portfolio:
    source_commit: str
    cache_fractions: tuple[float, float, float]
    traces: tuple[TraceWindow, ...]

    @classmethod
    def from_candidate(cls, candidate: Mapping[str, object]) -> Portfolio:
        _exact_keys(candidate, _PORTFOLIO_KEYS, "portfolio")
        if type(candidate["schema_version"]) is not int or candidate["schema_version"] != 1:
            raise ContractError("schema_version must be integer 1")
        source_commit = _nonempty_string(candidate["source_commit"], "source_commit")
        if source_commit != SOURCE_COMMIT:
            raise ContractError(f"source_commit must equal pinned commit {SOURCE_COMMIT}")
        raw_fractions = candidate["cache_fractions"]
        if not isinstance(raw_fractions, list) or len(raw_fractions) != 3:
            raise ContractError("cache_fractions must contain exactly three numbers")
        if any(type(item) is not Decimal for item in raw_fractions):
            raise ContractError("cache_fractions must contain JSON numbers")
        exact = tuple(raw_fractions)
        if exact != (Decimal("0.01"), Decimal("0.05"), Decimal("0.10")):
            raise ContractError("cache_fractions must be exactly [0.01, 0.05, 0.10]")
        raw_traces = candidate["traces"]
        if not isinstance(raw_traces, list):
            raise ContractError("traces must be an array")
        traces = []
        for item in raw_traces:
            if not isinstance(item, dict):
                raise ContractError("each trace must be an object")
            traces.append(TraceWindow.from_candidate(item))
        return cls(
            source_commit=source_commit,
            cache_fractions=(0.01, 0.05, 0.10),
            traces=tuple(traces),
        )


def _unique_object(items: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in items:
        if key in value:
            raise ContractError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _invalid_constant(value: str) -> object:
    raise ContractError(f"non-finite JSON constant is forbidden: {value}")


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ContractError(f"non-finite JSON number is forbidden: {value}")
    return parsed


def _finite_decimal(value: str) -> Decimal:
    parsed = Decimal(value)
    if not parsed.is_finite():
        raise ContractError(f"non-finite JSON number is forbidden: {value}")
    return parsed


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def record_sha256(value: Mapping[str, object], field: str) -> str:
    return hashlib.sha256(
        canonical_bytes({key: item for key, item in value.items() if key != field})
    ).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_object(path: Path) -> dict[str, object]:
    value = json.loads(
        path.read_bytes(),
        object_pairs_hook=_unique_object,
        parse_constant=_invalid_constant,
        parse_float=_finite_float,
    )
    if not isinstance(value, dict):
        raise ContractError(f"JSON object required: {path}")
    return value


def load_candidate_object(path: Path) -> dict[str, object]:
    value = json.loads(
        path.read_bytes(),
        object_pairs_hook=_unique_object,
        parse_constant=_invalid_constant,
        parse_float=_finite_decimal,
    )
    if not isinstance(value, dict):
        raise ContractError(f"JSON object required: {path}")
    return value


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_new_record(path: Path, value: dict[str, object], hash_field: str) -> None:
    value[hash_field] = record_sha256(value, hash_field)
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    published = False
    try:
        stream = os.fdopen(descriptor, "w", encoding="utf-8")
        descriptor = -1
        with stream:
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as error:
            raise ContractError(f"refusing to replace immutable record: {path}") from error
        published = True
        _fsync_directory(path.parent)
    except BaseException:
        if published:
            path.unlink(missing_ok=True)
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
