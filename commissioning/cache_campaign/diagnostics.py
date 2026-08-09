from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from .records import HEX64, canonical_bytes, canonical_decimal


_HEX40 = re.compile(r"[0-9a-f]{40}\Z")
_COUNTER_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_PRIMARY_OR_INTERPRETIVE_NAMES = {
    "aggregate",
    "byte_miss_ratio",
    "cpu_ns_per_request",
    "global_metadata_bytes",
    "metadata_bytes_per_object",
    "object_miss_ratio",
    "objective",
    "pass",
    "request_count",
    "reward",
    "score",
    "simulator_throughput_mqps",
}
_PHASE_LINE = re.compile(
    r"phase=(?P<index>[0-9]{1,2}) requests=(?P<requests>[0-9]+) "
    r"object_misses=(?P<object_misses>[0-9]+) "
    r"request_bytes=(?P<request_bytes>[0-9]+) "
    r"byte_misses=(?P<byte_misses>[0-9]+)\Z"
)

_PHASE_PROBE = r'''#include <inttypes.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "libCacheSim.h"
#include "cache_init.h"

static int parse_u64(const char *raw, uint64_t *value) {
  char *end = NULL;
  unsigned long long parsed = strtoull(raw, &end, 10);
  if (!raw[0] || !end || *end != '\0') return 1;
  *value = (uint64_t)parsed;
  return 0;
}

int main(int argc, char **argv) {
  if (argc != 7) return 2;
  uint64_t cache_size = 0, max_requests = 0, warmup_seconds = 0;
  uint64_t expected_measured = 0;
  if (parse_u64(argv[3], &cache_size) || cache_size == 0 ||
      parse_u64(argv[4], &max_requests) || max_requests == 0 ||
      parse_u64(argv[5], &warmup_seconds) ||
      parse_u64(argv[6], &expected_measured) || expected_measured < 16 ||
      expected_measured > max_requests) return 2;

  reader_init_param_t reader_params = default_reader_init_params();
  reader_params.cap_at_n_req = (int64_t)max_requests;
  reader_t *reader = setup_reader(argv[1], ORACLE_GENERAL_TRACE, &reader_params);
  cache_t *cache = create_cache(argv[1], argv[2], cache_size, NULL, true);
  request_t *request = new_request();
  if (!reader || !cache || !request) return 2;

  uint64_t bin_index = 0, bin_requests = 0, bin_object_misses = 0;
  uint64_t bin_request_bytes = 0, bin_byte_misses = 0;
  uint64_t measured = 0, consumed = 0;
  int read_state = read_one_req(reader, request);
  if (read_state != 0 || !request->valid) return 3;
  int64_t start_timestamp = request->clock_time;
  while (request->valid && consumed < max_requests) {
    int64_t relative_timestamp = request->clock_time - start_timestamp;
    if (relative_timestamp < 0) return 3;
    request->clock_time = relative_timestamp;
    bool hit = cache->get(cache, request);
    if ((uint64_t)relative_timestamp > warmup_seconds) {
      measured++;
      bin_requests++;
      bin_request_bytes += request->obj_size;
      if (!hit) {
        bin_object_misses++;
        bin_byte_misses += request->obj_size;
      }
      uint64_t quotient = expected_measured / 16;
      uint64_t remainder = expected_measured % 16;
      uint64_t boundary = quotient * (bin_index + 1) +
                          remainder * (bin_index + 1) / 16;
      if (measured == boundary) {
        printf("phase=%" PRIu64 " requests=%" PRIu64
               " object_misses=%" PRIu64 " request_bytes=%" PRIu64
               " byte_misses=%" PRIu64 "\n",
               bin_index, bin_requests, bin_object_misses,
               bin_request_bytes, bin_byte_misses);
        bin_index++;
        bin_requests = 0;
        bin_object_misses = 0;
        bin_request_bytes = 0;
        bin_byte_misses = 0;
      }
    }
    consumed++;
    if (consumed == max_requests) break;
    read_one_req(reader, request);
  }
  int status = 0;
  if (consumed != max_requests || measured != expected_measured ||
      bin_index != 16 || bin_requests != 0) status = 4;
  free_request(request);
  close_reader(reader);
  cache->cache_free(cache);
  return status;
}
'''.encode("ascii")


class DiagnosticError(ValueError):
    pass


def phase_probe_source() -> bytes:
    return _PHASE_PROBE


def _integer(value: object, label: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise DiagnosticError(f"{label} must be an integer >= {minimum}")
    return value


@dataclass(frozen=True)
class PhaseBin:
    index: int
    requests: int
    object_misses: int
    request_bytes: int
    byte_misses: int

    def __post_init__(self) -> None:
        _integer(self.index, "phase index")
        if self.index >= 16:
            raise DiagnosticError("phase index must be less than sixteen")
        _integer(self.requests, "phase requests", 1)
        _integer(self.object_misses, "phase object misses")
        _integer(self.request_bytes, "phase request bytes", 1)
        _integer(self.byte_misses, "phase byte misses")
        if self.object_misses > self.requests:
            raise DiagnosticError("phase object misses exceed requests")
        if self.byte_misses > self.request_bytes:
            raise DiagnosticError("phase byte misses exceed request bytes")

    def to_record(self) -> dict[str, int]:
        return {
            "index": self.index,
            "requests": self.requests,
            "object_misses": self.object_misses,
            "request_bytes": self.request_bytes,
            "byte_misses": self.byte_misses,
        }

    @classmethod
    def from_record(cls, value: Mapping[str, object]) -> PhaseBin:
        expected = {
            "index",
            "requests",
            "object_misses",
            "request_bytes",
            "byte_misses",
        }
        if set(value) != expected:
            raise DiagnosticError("phase-bin keys mismatch")
        return cls(
            index=value["index"],  # type: ignore[arg-type]
            requests=value["requests"],  # type: ignore[arg-type]
            object_misses=value["object_misses"],  # type: ignore[arg-type]
            request_bytes=value["request_bytes"],  # type: ignore[arg-type]
            byte_misses=value["byte_misses"],  # type: ignore[arg-type]
        )


def _ratio(numerator: int, denominator: int) -> Decimal:
    if denominator <= 0:
        raise DiagnosticError("phase ratio denominator must be positive")
    return Decimal(numerator) / Decimal(denominator)


def _matches_reported_ratio(observed: Decimal, reported: Decimal) -> bool:
    if type(reported) is not Decimal or not reported.is_finite():
        raise DiagnosticError("primary miss ratio must be a finite Decimal")
    quantum = Decimal(1).scaleb(reported.as_tuple().exponent)
    return observed.quantize(quantum) == reported


def parse_phase_probe_output(
    output: str,
    *,
    expected_request_count: int,
    expected_request_bytes: int,
    expected_object_miss_ratio: Decimal,
    expected_byte_miss_ratio: Decimal,
) -> tuple[PhaseBin, ...]:
    if type(output) is not str or any(
        character != "\n" and not 0x20 <= ord(character) <= 0x7E
        for character in output
    ):
        raise DiagnosticError("phase probe output must be printable ASCII and LF")
    lines = output.splitlines()
    if len(lines) != 16:
        raise DiagnosticError("phase probe must emit exactly sixteen bins")
    bins: list[PhaseBin] = []
    for expected_index, line in enumerate(lines):
        match = _PHASE_LINE.fullmatch(line)
        if match is None:
            raise DiagnosticError("malformed phase probe output")
        fields = {key: int(value) for key, value in match.groupdict().items()}
        if fields["index"] != expected_index:
            raise DiagnosticError("phase bins must be ordered from zero through fifteen")
        bins.append(
            PhaseBin(
                index=fields["index"],
                requests=fields["requests"],
                object_misses=fields["object_misses"],
                request_bytes=fields["request_bytes"],
                byte_misses=fields["byte_misses"],
            )
        )
    if type(expected_request_count) is not int or expected_request_count < 16:
        raise DiagnosticError("phase measurement requires at least sixteen requests")
    expected_bin_counts = [
        (index + 1) * expected_request_count // 16
        - index * expected_request_count // 16
        for index in range(16)
    ]
    if [item.requests for item in bins] != expected_bin_counts:
        raise DiagnosticError("phase bins do not match balanced request boundaries")
    request_count = sum(item.requests for item in bins)
    request_bytes = sum(item.request_bytes for item in bins)
    if request_count != expected_request_count:
        raise DiagnosticError("phase request total does not match primary measurement")
    if request_bytes != expected_request_bytes:
        raise DiagnosticError("phase byte total does not match primary trace facts")
    object_misses = sum(item.object_misses for item in bins)
    byte_misses = sum(item.byte_misses for item in bins)
    if not _matches_reported_ratio(
        _ratio(object_misses, request_count), expected_object_miss_ratio
    ):
        raise DiagnosticError("phase object miss ratio does not match primary measurement")
    if not _matches_reported_ratio(
        _ratio(byte_misses, request_bytes), expected_byte_miss_ratio
    ):
        raise DiagnosticError("phase byte miss ratio does not match primary measurement")
    return tuple(bins)


def _hashable(value: object) -> object:
    if type(value) is Decimal:
        return canonical_decimal(value)
    if isinstance(value, dict):
        return {key: _hashable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_hashable(item) for item in value]
    return value


@dataclass(frozen=True)
class ExploratorySidecar:
    evidence_class: Literal["exploratory"]
    candidate_commit: str
    run_sha256: str
    trace_sha256: str
    counters: tuple[tuple[str, int | Decimal], ...]
    sidecar_sha256: str

    @staticmethod
    def hash_record(value: Mapping[str, object]) -> str:
        material = {
            key: item for key, item in value.items() if key != "sidecar_sha256"
        }
        return hashlib.sha256(canonical_bytes(_hashable(material))).hexdigest()

    def to_record(self) -> dict[str, object]:
        return {
            "evidence_class": self.evidence_class,
            "candidate_commit": self.candidate_commit,
            "run_sha256": self.run_sha256,
            "trace_sha256": self.trace_sha256,
            "counters": {
                name: canonical_decimal(value) if type(value) is Decimal else value
                for name, value in self.counters
            },
            "sidecar_sha256": self.sidecar_sha256,
        }

    @classmethod
    def from_record(cls, value: Mapping[str, object]) -> ExploratorySidecar:
        expected = {
            "evidence_class",
            "candidate_commit",
            "run_sha256",
            "trace_sha256",
            "counters",
            "sidecar_sha256",
        }
        if set(value) != expected:
            raise DiagnosticError("exploratory sidecar keys mismatch")
        if value["evidence_class"] != "exploratory":
            raise DiagnosticError("sidecar evidence_class must be exploratory")
        candidate = value["candidate_commit"]
        run_hash = value["run_sha256"]
        trace_hash = value["trace_sha256"]
        digest = value["sidecar_sha256"]
        if type(candidate) is not str or _HEX40.fullmatch(candidate) is None:
            raise DiagnosticError("sidecar candidate commit is invalid")
        for label, item in (("run", run_hash), ("trace", trace_hash)):
            if type(item) is not str or HEX64.fullmatch(item) is None:
                raise DiagnosticError(f"sidecar {label} SHA-256 is invalid")
        if (
            type(digest) is not str
            or HEX64.fullmatch(digest) is None
            or digest != cls.hash_record(value)
        ):
            raise DiagnosticError("sidecar self-hash mismatch")
        raw_counters = value["counters"]
        if not isinstance(raw_counters, dict) or not raw_counters:
            raise DiagnosticError("sidecar counters must be a nonempty object")
        counters: list[tuple[str, int | Decimal]] = []
        for name, item in sorted(raw_counters.items()):
            if type(name) is not str or _COUNTER_NAME.fullmatch(name) is None:
                raise DiagnosticError("sidecar counter name is invalid")
            if name in _PRIMARY_OR_INTERPRETIVE_NAMES:
                raise DiagnosticError(
                    "exploratory counter cannot duplicate a primary or interpretive field"
                )
            if type(item) is int:
                counter = item
            elif type(item) is Decimal and item.is_finite():
                counter = item
            elif type(item) is str:
                try:
                    parsed = Decimal(item)
                except Exception as error:
                    raise DiagnosticError(
                        "sidecar Decimal counter string is invalid"
                    ) from error
                if not parsed.is_finite() or canonical_decimal(parsed) != item:
                    raise DiagnosticError(
                        "sidecar Decimal counter string is not canonical"
                    )
                counter = parsed
            else:
                raise DiagnosticError("sidecar counter must be a finite int or Decimal")
            counters.append((name, counter))
        return cls(
            evidence_class="exploratory",
            candidate_commit=candidate,
            run_sha256=run_hash,
            trace_sha256=trace_hash,
            counters=tuple(counters),
            sidecar_sha256=digest,
        )
