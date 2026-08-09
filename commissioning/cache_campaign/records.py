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
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
from pathlib import Path
from typing import Literal


HEX64 = re.compile(r"[0-9a-f]{64}\Z")
SOURCE_COMMIT = "da022c2945146e9577d91375a48d53850d7041a3"
_MAX_CANONICAL_DECIMAL_CHARS = 4096
_SCIENTIFIC_DECIMAL_PRECISION = 128
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


@dataclass(frozen=True)
class NewRecordReceipt:
    identity: tuple[int, int]
    mode: int
    size_bytes: int
    sha256: str


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


def canonical_decimal(value: Decimal) -> str:
    if type(value) is not Decimal or not value.is_finite():
        raise ContractError("canonical decimal must be finite")
    sign, raw_digits, raw_exponent = value.as_tuple()
    if not any(raw_digits):
        return "0"
    if type(raw_exponent) is not int:
        raise ContractError("canonical decimal exponent is invalid")
    digits = list(raw_digits)
    exponent = raw_exponent
    while digits[-1] == 0:
        digits.pop()
        exponent += 1
    coefficient = "".join(str(digit) for digit in digits)
    sign_chars = 1 if sign else 0
    if exponent >= 0:
        required = sign_chars + len(coefficient) + exponent
        if required > _MAX_CANONICAL_DECIMAL_CHARS:
            raise ContractError("canonical decimal expansion must be bounded")
        result = coefficient + "0" * exponent
    else:
        point = len(coefficient) + exponent
        if point > 0:
            required = sign_chars + len(coefficient) + 1
            if required > _MAX_CANONICAL_DECIMAL_CHARS:
                raise ContractError("canonical decimal expansion must be bounded")
            result = coefficient[:point] + "." + coefficient[point:]
        else:
            zero_count = -point
            required = sign_chars + 2 + zero_count + len(coefficient)
            if required > _MAX_CANONICAL_DECIMAL_CHARS:
                raise ContractError("canonical decimal expansion must be bounded")
            result = "0." + "0" * zero_count + coefficient
    return "-" + result if sign else result


def scientific_decimal_context() -> Context:
    """Return the apparatus context: 128 significant digits, ties-to-even."""
    return Context(
        prec=_SCIENTIFIC_DECIMAL_PRECISION,
        rounding=ROUND_HALF_EVEN,
        Emin=-999_999,
        Emax=999_999,
        capitals=1,
        clamp=0,
    )


def deterministic_decimal_ratio(numerator: int, denominator: int) -> Decimal:
    """Divide integers under the fixed apparatus context, never the caller's."""
    if type(numerator) is not int or type(denominator) is not int or denominator <= 0:
        raise ContractError("decimal ratio requires integer numerator and positive denominator")
    with localcontext(scientific_decimal_context()):
        return Decimal(numerator) / Decimal(denominator)


def _decimal(
    value: object,
    label: str,
    *,
    minimum: Decimal,
    maximum: Decimal | None = None,
    minimum_inclusive: bool = True,
) -> Decimal:
    if type(value) is not Decimal or not value.is_finite():
        raise ContractError(f"{label} must be a finite Decimal")
    if (value < minimum if minimum_inclusive else value <= minimum) or (
        maximum is not None and value > maximum
    ):
        interval = "[" if minimum_inclusive else "("
        upper = str(maximum) if maximum is not None else "infinity"
        raise ContractError(f"{label} must be in {interval}{minimum}, {upper}]")
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


@dataclass(frozen=True)
class ParetoMeasurement:
    rung: Literal["r1", "r2", "r3"]
    split: Literal["dev", "visible", "r3"]
    trace_id: str
    policy: str
    cache_fraction: Decimal
    cache_size_bytes: int
    request_count: int
    object_miss_ratio: Decimal
    byte_miss_ratio: Decimal
    simulator_throughput_mqps: Decimal
    cpu_ns_per_request: Decimal
    metadata_bytes_per_object: Decimal
    global_metadata_bytes: int
    metadata_measurement_sha256: str

    def __post_init__(self) -> None:
        if self.rung not in {"r1", "r2", "r3"}:
            raise ContractError("rung must be r1, r2, or r3")
        if self.split not in {"dev", "visible", "r3"}:
            raise ContractError("split must be dev, visible, or r3")
        _nonempty_string(self.trace_id, "trace_id")
        _nonempty_string(self.policy, "policy")
        _decimal(
            self.cache_fraction,
            "cache_fraction",
            minimum=Decimal(0),
            maximum=Decimal(1),
            minimum_inclusive=False,
        )
        _integer(self.cache_size_bytes, "cache_size_bytes", minimum=1)
        _integer(self.request_count, "request_count", minimum=1)
        _decimal(
            self.object_miss_ratio,
            "object_miss_ratio",
            minimum=Decimal(0),
            maximum=Decimal(1),
        )
        _decimal(
            self.byte_miss_ratio,
            "byte_miss_ratio",
            minimum=Decimal(0),
            maximum=Decimal(1),
        )
        _decimal(
            self.simulator_throughput_mqps,
            "simulator_throughput_mqps",
            minimum=Decimal(0),
            minimum_inclusive=False,
        )
        _decimal(
            self.cpu_ns_per_request,
            "cpu_ns_per_request",
            minimum=Decimal(0),
            minimum_inclusive=False,
        )
        _decimal(
            self.metadata_bytes_per_object,
            "metadata_bytes_per_object",
            minimum=Decimal(0),
        )
        _integer(self.global_metadata_bytes, "global_metadata_bytes", minimum=0)
        if HEX64.fullmatch(self.metadata_measurement_sha256) is None:
            raise ContractError(
                "metadata_measurement_sha256 must be a lowercase SHA-256"
            )

    def to_record(self) -> dict[str, object]:
        return {
            "rung": self.rung,
            "split": self.split,
            "trace_id": self.trace_id,
            "policy": self.policy,
            "cache_fraction": canonical_decimal(self.cache_fraction),
            "cache_size_bytes": self.cache_size_bytes,
            "request_count": self.request_count,
            "object_miss_ratio": canonical_decimal(self.object_miss_ratio),
            "byte_miss_ratio": canonical_decimal(self.byte_miss_ratio),
            "simulator_throughput_mqps": canonical_decimal(
                self.simulator_throughput_mqps
            ),
            "cpu_ns_per_request": canonical_decimal(self.cpu_ns_per_request),
            "metadata_bytes_per_object": canonical_decimal(
                self.metadata_bytes_per_object
            ),
            "global_metadata_bytes": self.global_metadata_bytes,
            "metadata_measurement_sha256": self.metadata_measurement_sha256,
        }

    @classmethod
    def from_record(cls, value: Mapping[str, object]) -> ParetoMeasurement:
        expected = {
            "rung",
            "split",
            "trace_id",
            "policy",
            "cache_fraction",
            "cache_size_bytes",
            "request_count",
            "object_miss_ratio",
            "byte_miss_ratio",
            "simulator_throughput_mqps",
            "cpu_ns_per_request",
            "metadata_bytes_per_object",
            "global_metadata_bytes",
            "metadata_measurement_sha256",
        }
        _exact_keys(value, expected, "Pareto measurement")

        def parsed_decimal(name: str) -> Decimal:
            raw = value[name]
            if type(raw) is not str:
                raise ContractError(f"{name} must be a canonical Decimal string")
            try:
                parsed = Decimal(raw)
            except Exception as error:
                raise ContractError(f"{name} must be a canonical Decimal string") from error
            if not parsed.is_finite() or canonical_decimal(parsed) != raw:
                raise ContractError(f"{name} must be a canonical Decimal string")
            return parsed

        return cls(
            rung=value["rung"],  # type: ignore[arg-type]
            split=value["split"],  # type: ignore[arg-type]
            trace_id=value["trace_id"],  # type: ignore[arg-type]
            policy=value["policy"],  # type: ignore[arg-type]
            cache_fraction=parsed_decimal("cache_fraction"),
            cache_size_bytes=value["cache_size_bytes"],  # type: ignore[arg-type]
            request_count=value["request_count"],  # type: ignore[arg-type]
            object_miss_ratio=parsed_decimal("object_miss_ratio"),
            byte_miss_ratio=parsed_decimal("byte_miss_ratio"),
            simulator_throughput_mqps=parsed_decimal(
                "simulator_throughput_mqps"
            ),
            cpu_ns_per_request=parsed_decimal("cpu_ns_per_request"),
            metadata_bytes_per_object=parsed_decimal("metadata_bytes_per_object"),
            global_metadata_bytes=value["global_metadata_bytes"],  # type: ignore[arg-type]
            metadata_measurement_sha256=value[  # type: ignore[arg-type]
                "metadata_measurement_sha256"
            ],
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


def write_new_record(
    path: Path, value: dict[str, object], hash_field: str
) -> NewRecordReceipt:
    value[hash_field] = record_sha256(value, hash_field)
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    published = False
    owned_identity: tuple[int, int] | None = None
    try:
        stream = os.fdopen(descriptor, "wb")
        descriptor = -1
        with stream:
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        temporary_metadata = temporary.stat(follow_symlinks=False)
        owned_identity = (temporary_metadata.st_dev, temporary_metadata.st_ino)
        try:
            os.link(temporary, path)
        except FileExistsError as error:
            raise ContractError(f"refusing to replace immutable record: {path}") from error
        published = True
        _fsync_directory(path.parent)
        output_descriptor = os.open(
            path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            output_metadata = os.fstat(output_descriptor)
            with os.fdopen(output_descriptor, "rb") as output_stream:
                output_descriptor = -1
                observed = output_stream.read()
        finally:
            if output_descriptor >= 0:
                os.close(output_descriptor)
        if (
            (output_metadata.st_dev, output_metadata.st_ino) != owned_identity
            or observed != serialized
        ):
            raise ContractError(f"immutable record changed during publication: {path}")
        return NewRecordReceipt(
            identity=owned_identity,
            mode=stat.S_IMODE(output_metadata.st_mode),
            size_bytes=len(serialized),
            sha256=hashlib.sha256(serialized).hexdigest(),
        )
    except BaseException:
        if published and owned_identity is not None:
            try:
                metadata = path.lstat()
                if (metadata.st_dev, metadata.st_ino) == owned_identity:
                    path.unlink()
            except OSError:
                pass
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
