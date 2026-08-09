from __future__ import annotations

import struct
import subprocess
from dataclasses import FrozenInstanceError
from decimal import Decimal
from pathlib import Path

import pytest

from commissioning.cache_campaign.diagnostics import (
    DiagnosticError,
    ExploratorySidecar,
    PhaseBin,
    parse_phase_probe_output,
    phase_probe_source,
)


def phase_output(*, reset: bool = False) -> str:
    lines = []
    for index in range(16):
        object_misses = 10 if reset else (10 if index == 0 else 0)
        byte_misses = object_misses * 64
        lines.append(
            f"phase={index} requests=10 object_misses={object_misses} "
            f"request_bytes=640 byte_misses={byte_misses}"
        )
    return "\n".join(lines) + "\n"


def test_phase_parser_requires_sixteen_equal_continuous_bins_and_totals() -> None:
    bins = parse_phase_probe_output(
        phase_output(),
        expected_request_count=160,
        expected_request_bytes=10_240,
        expected_object_miss_ratio=Decimal("0.0625"),
        expected_byte_miss_ratio=Decimal("0.0625"),
    )
    assert len(bins) == 16
    assert sum(item.requests for item in bins) == 160
    assert sum(item.object_misses for item in bins) == 10
    assert isinstance(bins[0], PhaseBin)
    assert PhaseBin.from_record(bins[0].to_record()) == bins[0]
    with pytest.raises(FrozenInstanceError):
        bins[0].requests = 0  # type: ignore[misc]


def test_phase_parser_rejects_reset_behavior_and_incorrect_sums() -> None:
    with pytest.raises(DiagnosticError, match="object miss ratio"):
        parse_phase_probe_output(
            phase_output(reset=True),
            expected_request_count=160,
            expected_request_bytes=10_240,
            expected_object_miss_ratio=Decimal("0.0625"),
            expected_byte_miss_ratio=Decimal("0.0625"),
        )


def test_phase_parser_balances_nondivisible_totals_across_all_sixteen_bins() -> None:
    lines = []
    for index in range(16):
        lower = index * 161 // 16
        upper = (index + 1) * 161 // 16
        requests = upper - lower
        lines.append(
            f"phase={index} requests={requests} object_misses=0 "
            f"request_bytes={requests * 64} byte_misses=0"
        )
    bins = parse_phase_probe_output(
        "\n".join(lines) + "\n",
        expected_request_count=161,
        expected_request_bytes=161 * 64,
        expected_object_miss_ratio=Decimal("0.0000"),
        expected_byte_miss_ratio=Decimal("0.0000"),
    )
    assert [item.requests for item in bins] == [10] * 15 + [11]
    with pytest.raises(DiagnosticError, match="request"):
        parse_phase_probe_output(
            phase_output().replace("phase=15 requests=10", "phase=15 requests=9"),
            expected_request_count=160,
            expected_request_bytes=10_240,
            expected_object_miss_ratio=Decimal("0.0625"),
            expected_byte_miss_ratio=Decimal("0.0625"),
        )


def sidecar_record() -> dict[str, object]:
    value: dict[str, object] = {
        "evidence_class": "exploratory",
        "candidate_commit": "a" * 40,
        "run_sha256": "b" * 64,
        "trace_sha256": "c" * 64,
        "counters": {"queue_hits": 3, "ghost_ratio": Decimal("0.125")},
    }
    value["sidecar_sha256"] = ExploratorySidecar.hash_record(value)
    return value


def test_exploratory_sidecar_accepts_only_strict_raw_counters() -> None:
    sidecar = ExploratorySidecar.from_record(sidecar_record())
    assert sidecar.evidence_class == "exploratory"
    assert dict(sidecar.counters) == {
        "ghost_ratio": Decimal("0.125"),
        "queue_hits": 3,
    }
    serialized = sidecar.to_record()
    assert serialized["counters"] == {
        "ghost_ratio": "0.125",
        "queue_hits": 3,
    }
    assert ExploratorySidecar.from_record(serialized) == sidecar


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("evidence_class", "confirmation"),
        ("candidate_commit", "a" * 64),
        ("run_sha256", "B" * 64),
        ("trace_sha256", "c" * 63),
        ("counters", {}),
        ("counters", {"Bad Counter": 1}),
        ("counters", {"object_miss_ratio": Decimal("0.5")}),
        ("counters", {"queue_hits": True}),
        ("counters", {"queue_hits": Decimal("NaN")}),
    ],
)
def test_exploratory_sidecar_rejects_confirmation_or_malformed_values(
    field: str, invalid: object
) -> None:
    value = sidecar_record()
    value[field] = invalid
    with pytest.raises((DiagnosticError, ValueError)):
        value["sidecar_sha256"] = ExploratorySidecar.hash_record(value)
        ExploratorySidecar.from_record(value)


def test_exploratory_sidecar_rejects_self_hash_mutation() -> None:
    value = sidecar_record()
    value["counters"] = {"queue_hits": 4}
    with pytest.raises(DiagnosticError, match="self-hash"):
        ExploratorySidecar.from_record(value)


FAKE_CACHE_INIT = r'''
#ifndef FAKE_CACHE_INIT_H
#define FAKE_CACHE_INIT_H
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
typedef struct request {
  uint64_t obj_id;
  uint64_t obj_size;
  int64_t clock_time;
  bool valid;
} request_t;
typedef struct reader { FILE *stream; uint64_t remaining; } reader_t;
typedef struct { int64_t cap_at_n_req; } reader_init_param_t;
typedef struct cache cache_t;
struct cache {
  bool (*get)(cache_t *, request_t *);
  void (*cache_free)(cache_t *);
  bool seen[2048];
};
typedef struct { uint64_t cache_size; } common_cache_params_t;
#define ORACLE_GENERAL_TRACE 1
static reader_init_param_t default_reader_init_params(void) {
  reader_init_param_t value = { .cap_at_n_req = -1 }; return value;
}
static reader_t *setup_reader(const char *path, int type,
                              reader_init_param_t *params) {
  (void)type;
  reader_t *reader = calloc(1, sizeof(*reader));
  reader->stream = fopen(path, "rb");
  reader->remaining = (uint64_t)params->cap_at_n_req;
  return reader;
}
static request_t *new_request(void) { return calloc(1, sizeof(request_t)); }
static int read_one_req(reader_t *reader, request_t *request) {
  uint32_t timestamp = 0, size = 0;
  int64_t next_access = 0;
  if (reader->remaining == 0 ||
      fread(&timestamp, sizeof(timestamp), 1, reader->stream) != 1 ||
      fread(&request->obj_id, sizeof(request->obj_id), 1, reader->stream) != 1 ||
      fread(&size, sizeof(size), 1, reader->stream) != 1 ||
      fread(&next_access, sizeof(next_access), 1, reader->stream) != 1) {
    request->valid = false; return 1;
  }
  (void)next_access;
  reader->remaining--;
  request->clock_time = timestamp;
  request->obj_size = size;
  request->valid = true;
  return 0;
}
static void free_request(request_t *request) { free(request); }
static void close_reader(reader_t *reader) {
  fclose(reader->stream); free(reader);
}
static bool fake_get(cache_t *cache, request_t *request) {
  if (request->clock_time > 161) return false;
  bool hit = cache->seen[request->obj_id];
  cache->seen[request->obj_id] = true;
  return hit;
}
static void fake_free(cache_t *cache) { free(cache); }
static cache_t *create_cache(const char *trace, const char *policy,
                             uint64_t size, const char *params,
                             bool metadata) {
  (void)trace; (void)size; (void)params; (void)metadata;
  if (strcmp(policy, "Sieve") != 0) return NULL;
  cache_t *cache = calloc(1, sizeof(*cache));
  cache->get = fake_get; cache->cache_free = fake_free; return cache;
}
#endif
'''


def test_generated_phase_probe_compiles_and_preserves_state_across_sixteen_bins(
    tmp_path: Path,
) -> None:
    include = tmp_path / "include"
    include.mkdir()
    (include / "cache_init.h").write_text(FAKE_CACHE_INIT)
    (include / "libCacheSim.h").write_text("/* fake umbrella */\n")
    source = tmp_path / "phase_probe.c"
    source.write_bytes(phase_probe_source())
    binary = tmp_path / "phase-probe"
    subprocess.run(
        [
            "/usr/bin/cc",
            "-std=c11",
            "-O2",
            "-I",
            str(include),
            "-o",
            str(binary),
            str(source),
        ],
        check=True,
    )
    trace = tmp_path / "fixture.oracleGeneral"
    oracle = struct.Struct("<IQIq")
    raw = bytearray()
    raw.extend(oracle.pack(1000, 1000, 64, -1))
    raw.extend(oracle.pack(1001, 1001, 64, -1))
    phases = [
        [index % 10 for index in range(40)],
        [10 + index % 10 for index in range(40)],
        [index % 10 for index in range(40)],
        [20 + index % 10 for index in range(40)],
    ]
    for index, object_id in enumerate(
        object_id for phase in phases for object_id in phase
    ):
        raw.extend(oracle.pack(index + 1002, object_id, 64, -1))
    trace.write_bytes(raw)
    result = subprocess.run(
        [str(binary), str(trace), "Sieve", "64", "162", "1", "160"],
        capture_output=True,
        check=True,
        text=True,
    )
    bins = parse_phase_probe_output(
        result.stdout,
        expected_request_count=160,
        expected_request_bytes=10_240,
        expected_object_miss_ratio=Decimal("0.1875"),
        expected_byte_miss_ratio=Decimal("0.1875"),
    )
    assert [item.object_misses for item in bins] == [
        10,
        0,
        0,
        0,
        10,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        10,
        0,
        0,
        0,
    ]
