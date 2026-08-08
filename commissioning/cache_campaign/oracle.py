from __future__ import annotations

import hashlib
import os
import struct
from pathlib import Path
from typing import BinaryIO

from .records import TraceWindow, record_sha256


ORACLE_GENERAL = struct.Struct("<IQIq")
_BUCKET_RECORD = struct.Struct("<QI")
_BUCKET_COUNT = 256
_MAX_VTIME = (1 << 63) - 1
_COMPRESSED_SUFFIXES = {".bz2", ".gz", ".xz", ".zst"}
_COMPRESSED_MAGIC = (b"\x1f\x8b", b"BZh", b"\xfd7zXZ\x00", b"\x28\xb5\x2f\xfd")


class OracleError(ValueError):
    pass


def _bucket_name(bucket: int) -> str:
    return f"bucket-{bucket:03d}.bin"


def _open_bucket(directory_descriptor: int, bucket: int) -> BinaryIO:
    descriptor = os.open(
        _bucket_name(bucket),
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
        dir_fd=directory_descriptor,
    )
    return os.fdopen(descriptor, "wb")


def _compression_magic(prefix: bytes) -> bool:
    return any(prefix.startswith(magic) for magic in _COMPRESSED_MAGIC)


def scan_oracle_general(
    trace: TraceWindow,
    temporary_directory: Path,
    *,
    temporary_descriptor: int | None = None,
) -> dict[str, object]:
    if trace.path.suffix.lower() in _COMPRESSED_SUFFIXES:
        raise OracleError(f"compressed OracleGeneral input is unsupported: {trace.path}")
    if trace.size_bytes % ORACLE_GENERAL.size:
        raise OracleError(f"misaligned OracleGeneral data: {trace.path}")
    if trace.size_bytes // ORACLE_GENERAL.size < trace.max_requests:
        raise OracleError(f"OracleGeneral window has fewer than max_requests: {trace.path}")
    owns_temporary_descriptor = temporary_descriptor is None
    if temporary_descriptor is None:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            temporary_descriptor = os.open(temporary_directory, flags)
        except OSError as error:
            raise OracleError(
                f"scanner temporary directory is missing: {temporary_directory}"
            ) from error
    bucket_streams: dict[int, BinaryIO] = {}
    reuse_counts: dict[int, int] = {}
    no_next_count = 0
    digest = hashlib.sha256()
    observed_size = 0
    previous_timestamp: int | None = None
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        if os.listdir(temporary_descriptor):
            raise OracleError(
                f"scanner temporary directory must be empty: {temporary_directory}"
            )
        descriptor = os.open(trace.path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            prefix = stream.read(ORACLE_GENERAL.size)
            if _compression_magic(prefix):
                raise OracleError(f"compressed OracleGeneral input is unsupported: {trace.path}")
            stream.seek(0)
            for request_index in range(trace.max_requests):
                raw = stream.read(ORACLE_GENERAL.size)
                if len(raw) != ORACLE_GENERAL.size:
                    raise OracleError(
                        f"OracleGeneral window has fewer than max_requests: {trace.path}"
                    )
                digest.update(raw)
                observed_size += len(raw)
                timestamp, object_id, object_size, next_access_vtime = ORACLE_GENERAL.unpack(raw)
                if previous_timestamp is not None and timestamp < previous_timestamp:
                    raise OracleError(f"non-monotonic OracleGeneral timestamp: {trace.path}")
                previous_timestamp = timestamp
                if object_size <= 0:
                    raise OracleError(f"nonpositive OracleGeneral object size: {trace.path}")

                bucket = object_id & (_BUCKET_COUNT - 1)
                bucket_stream = bucket_streams.get(bucket)
                if bucket_stream is None:
                    bucket_stream = _open_bucket(temporary_descriptor, bucket)
                    bucket_streams[bucket] = bucket_stream
                bucket_stream.write(_BUCKET_RECORD.pack(object_id, object_size))

                current_vtime = trace.start_request + request_index
                if next_access_vtime in {-1, _MAX_VTIME}:
                    no_next_count += 1
                else:
                    if next_access_vtime <= current_vtime:
                        raise OracleError(
                            f"backward OracleGeneral next_access_vtime: {trace.path}"
                        )
                    distance = next_access_vtime - current_vtime
                    bin_index = distance.bit_length() - 1
                    reuse_counts[bin_index] = reuse_counts.get(bin_index, 0) + 1

            if stream.read(1):
                raise OracleError(
                    f"OracleGeneral window has records beyond max_requests: {trace.path}"
                )
        if observed_size != trace.size_bytes or digest.hexdigest() != trace.sha256:
            raise OracleError(f"trace file changed after candidate validation: {trace.path}")

        for stream in bucket_streams.values():
            stream.close()
        bucket_streams.clear()

        unique_object_count = 0
        one_hit_object_count = 0
        working_set_bytes = 0
        for bucket in range(_BUCKET_COUNT):
            name = _bucket_name(bucket)
            try:
                bucket_descriptor = os.open(
                    name,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=temporary_descriptor,
                )
            except FileNotFoundError:
                continue
            objects: dict[int, tuple[int, int]] = {}
            try:
                with os.fdopen(bucket_descriptor, "rb") as stream:
                    bucket_descriptor = -1
                    while True:
                        raw = stream.read(_BUCKET_RECORD.size)
                        if not raw:
                            break
                        if len(raw) != _BUCKET_RECORD.size:
                            raise OracleError("internal OracleGeneral bucket is truncated")
                        object_id, object_size = _BUCKET_RECORD.unpack(raw)
                        count, _ = objects.get(object_id, (0, object_size))
                        objects[object_id] = (count + 1, object_size)
                unique_object_count += len(objects)
                one_hit_object_count += sum(count == 1 for count, _ in objects.values())
                working_set_bytes += sum(size for _, size in objects.values())
            finally:
                if bucket_descriptor >= 0:
                    os.close(bucket_descriptor)
                try:
                    os.unlink(name, dir_fd=temporary_descriptor)
                except FileNotFoundError:
                    pass

        if working_set_bytes != trace.working_set_bytes:
            raise OracleError(
                f"working set mismatch for {trace.trace_id}: "
                f"declared {trace.working_set_bytes}, observed {working_set_bytes}"
            )
        diagnostic: dict[str, object] = {
            "schema_version": 1,
            "trace_id": trace.trace_id,
            "request_count": trace.max_requests,
            "unique_object_count": unique_object_count,
            "working_set_bytes": working_set_bytes,
            "one_hit_object_fraction": {
                "numerator": one_hit_object_count,
                "denominator": unique_object_count,
            },
            "one_hit_request_fraction": {
                "numerator": one_hit_object_count,
                "denominator": trace.max_requests,
            },
            "reuse_distance": {
                "bin_convention": "bin k counts d where 2^k <= d < 2^(k+1)",
                "counts": {str(key): reuse_counts[key] for key in sorted(reuse_counts)},
                "no_next_count": no_next_count,
            },
        }
        diagnostic["diagnostic_sha256"] = record_sha256(
            diagnostic, "diagnostic_sha256"
        )
        return diagnostic
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        for stream in bucket_streams.values():
            stream.close()
        for bucket in range(_BUCKET_COUNT):
            try:
                os.unlink(_bucket_name(bucket), dir_fd=temporary_descriptor)
            except FileNotFoundError:
                pass
        if owns_temporary_descriptor:
            os.close(temporary_descriptor)
