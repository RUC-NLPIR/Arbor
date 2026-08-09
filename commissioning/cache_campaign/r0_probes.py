from __future__ import annotations

import hashlib
import os
import re
import stat
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path


REQUEST_COUNT = 10_000


class ProbeError(ValueError):
    pass


def _regular_bytes(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ProbeError(f"probe input must be a regular file: {path}")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            return stream.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _printable_output(raw: str, label: str) -> list[str]:
    if type(raw) is not str or any(
        character != "\n" and not 0x20 <= ord(character) <= 0x7E
        for character in raw
    ):
        raise ProbeError(f"{label} output must be printable ASCII and LF")
    return raw.splitlines()


def parse_capacity_probe(output: str) -> dict[str, int | bool]:
    lines = _printable_output(output, "capacity probe")
    pattern = re.compile(r"([a-z_]+)=([0-9]+)\Z")
    values: dict[str, int] = {}
    for line in lines:
        match = pattern.fullmatch(line)
        if match is None or match.group(1) in values:
            raise ProbeError("malformed capacity probe output")
        values[match.group(1)] = int(match.group(2))
    expected = {
        "capacity_conserved",
        "requests",
        "max_occupied_bytes",
        "cache_size_bytes",
    }
    if set(values) != expected:
        raise ProbeError("capacity probe fields mismatch")
    if (
        values["capacity_conserved"] != 1
        or values["requests"] != REQUEST_COUNT
        or values["cache_size_bytes"] <= 0
        or not 0 <= values["max_occupied_bytes"] <= values["cache_size_bytes"]
    ):
        raise ProbeError("capacity probe reported a capacity violation")
    return {
        "capacity_conserved": True,
        "requests": values["requests"],
        "max_occupied_bytes": values["max_occupied_bytes"],
        "cache_size_bytes": values["cache_size_bytes"],
    }


def parse_metadata_probe(output: str) -> tuple[Decimal, int]:
    lines = _printable_output(output, "metadata probe")
    if len(lines) != 5 or lines[-1] != "status=ok":
        raise ProbeError("allocation-accounting metadata probe failed")
    global_match = re.fullmatch(r"global_metadata_bytes=([0-9]+)", lines[0])
    if global_match is None:
        raise ProbeError("malformed global metadata measurement")
    global_bytes = int(global_match.group(1))
    sample_pattern = re.compile(
        r"sample=([0-9]+) live_bytes=([0-9]+) resident_objects=([0-9]+)\Z"
    )
    measurements: list[Decimal] = []
    for line, expected_point in zip(
        lines[1:4], (1_000, 5_000, 10_000), strict=True
    ):
        match = sample_pattern.fullmatch(line)
        if match is None or int(match.group(1)) != expected_point:
            raise ProbeError("malformed metadata sample")
        live_bytes = int(match.group(2))
        resident = int(match.group(3))
        if live_bytes < global_bytes or resident <= 0:
            raise ProbeError("invalid allocation-accounting metadata sample")
        try:
            measurements.append(
                Decimal(live_bytes - global_bytes) / Decimal(resident)
            )
        except (InvalidOperation, ZeroDivisionError) as error:
            raise ProbeError("invalid exact metadata arithmetic") from error
    return max(measurements), global_bytes


_CAPACITY_TEMPLATE = r'''#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "libCacheSim.h"

int main(int argc, char **argv) {
  if (argc != 4 || strcmp(argv[2], "@POLICY@") != 0) return 2;
  char *end = NULL;
  uint64_t cache_size = strtoull(argv[3], &end, 10);
  if (!end || *end != '\0' || cache_size == 0) return 2;
  reader_init_param_t reader_params = default_reader_init_params();
  reader_params.cap_at_n_req = 10000;
  reader_t *reader = setup_reader(argv[1], ORACLE_GENERAL_TRACE, &reader_params);
  common_cache_params_t cache_params = default_common_cache_params();
  cache_params.cache_size = cache_size;
  cache_params.hashpower = 16;
  cache_params.consider_obj_metadata = true;
  cache_t *cache = @POLICY@_init(cache_params, NULL);
  if (!reader || !cache) return 2;
  int64_t maximum = 0;
  uint64_t requests = 0;
  request_t *request = new_request();
  while (requests < 10000 && read_one_req(reader, request) == 0) {
    cache->get(cache, request);
    int64_t occupied = cache->get_occupied_byte(cache);
    if (occupied < 0 || occupied > cache->cache_size) {
      fprintf(stderr, "capacity violation at request %" PRIu64 "\n", requests + 1);
      return 3;
    }
    if (occupied > maximum) maximum = occupied;
    requests++;
  }
  if (requests != 10000) return 4;
  printf("capacity_conserved=1\nrequests=%" PRIu64
         "\nmax_occupied_bytes=%" PRId64 "\ncache_size_bytes=%" PRId64 "\n",
         requests, maximum, cache->cache_size);
  free_request(request);
  close_reader(reader);
  cache->cache_free(cache);
  return 0;
}
'''.encode()


_METADATA_TEMPLATE = r'''#define _GNU_SOURCE
#include <dlfcn.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "libCacheSim.h"

typedef struct {
  size_t live_bytes;
  size_t live_allocations;
  unsigned error_flags;
} aros_alloc_snapshot_t;
typedef void (*reset_fn)(void);
typedef void (*enable_fn)(int);
typedef void (*snapshot_fn)(aros_alloc_snapshot_t *);

int main(int argc, char **argv) {
  if (argc != 4 || strcmp(argv[2], "@POLICY@") != 0) return 2;
  reset_fn reset = dlsym(RTLD_DEFAULT, "aros_alloc_reset");
  enable_fn enable = dlsym(RTLD_DEFAULT, "aros_alloc_set_enabled");
  snapshot_fn snapshot = dlsym(RTLD_DEFAULT, "aros_alloc_snapshot");
  if (!reset || !enable || !snapshot) return 2;
  char *end = NULL;
  uint64_t cache_size = strtoull(argv[3], &end, 10);
  if (!end || *end != '\0' || cache_size == 0) return 2;
  common_cache_params_t cache_params = default_common_cache_params();
  cache_params.cache_size = cache_size;
  cache_params.hashpower = 16;
  cache_params.consider_obj_metadata = true;
  reset(); enable(1);
  cache_t *cache = @POLICY@_init(cache_params, NULL);
  if (!cache) { enable(0); return 2; }
  aros_alloc_snapshot_t observed;
  snapshot(&observed);
  size_t global = observed.live_bytes;
  unsigned errors = observed.error_flags;
  size_t samples[3] = {0, 0, 0};
  int64_t residents[3] = {0, 0, 0};
  size_t sample_index = 0;
  request_t request;
  memset(&request, 0, sizeof(request));
  request.obj_size = 64;
  request.valid = true;
  for (size_t inserted = 1; inserted <= 10000; inserted++) {
    request.obj_id = inserted;
    request.clock_time = inserted - 1;
    cache->get(cache, &request);
    if (inserted == 1000 || inserted == 5000 || inserted == 10000) {
      snapshot(&observed);
      samples[sample_index] = observed.live_bytes;
      residents[sample_index] = cache->get_n_obj(cache);
      errors |= observed.error_flags;
      sample_index++;
    }
  }
  cache->cache_free(cache);
  snapshot(&observed);
  errors |= observed.error_flags;
  enable(0);
  printf("global_metadata_bytes=%zu\n", global);
  printf("sample=1000 live_bytes=%zu resident_objects=%lld\n",
         samples[0], (long long)residents[0]);
  printf("sample=5000 live_bytes=%zu resident_objects=%lld\n",
         samples[1], (long long)residents[1]);
  printf("sample=10000 live_bytes=%zu resident_objects=%lld\n",
         samples[2], (long long)residents[2]);
  printf("status=%s\n", errors ? "accounting_error" : "ok");
  if (errors) fprintf(stderr, "allocator_error_flags=%u\n", errors);
  return errors ? 3 : 0;
}
'''.encode()

_ALLOCATOR_INTERPOSER = r'''
#define _GNU_SOURCE
#include <errno.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdatomic.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <unistd.h>

#ifndef AROS_POINTER_CAPACITY
#define AROS_POINTER_CAPACITY 262144
#endif
#define AROS_ERROR_ARITHMETIC 1u
#define AROS_ERROR_UNKNOWN_FREE 2u
#define AROS_ERROR_UNKNOWN_REALLOC 4u
#define AROS_ERROR_TABLE_OVERFLOW 8u
#define AROS_ERROR_MMAP_ACCOUNTING 16u

typedef struct {
  void *pointer;
  size_t size;
  unsigned kind;
} pointer_entry_t;

typedef struct {
  size_t live_bytes;
  size_t live_allocations;
  unsigned error_flags;
} aros_alloc_snapshot_t;

extern void *__libc_malloc(size_t);
extern void *__libc_calloc(size_t, size_t);
extern void *__libc_realloc(void *, size_t);
extern void __libc_free(void *);
extern void *__libc_memalign(size_t, size_t);

static pointer_entry_t pointer_table[AROS_POINTER_CAPACITY];
static pthread_mutex_t pointer_lock = PTHREAD_MUTEX_INITIALIZER;
static _Thread_local int recursion_guard;
static _Atomic int accounting_enabled;
static size_t live_bytes;
static size_t live_allocations;
static unsigned error_flags;

static int accounting_is_enabled(void) {
  return atomic_load_explicit(&accounting_enabled, memory_order_acquire);
}

static size_t find_pointer(void *pointer) {
  for (size_t i = 0; i < AROS_POINTER_CAPACITY; ++i) {
    if (pointer_table[i].pointer == pointer) return i;
  }
  return AROS_POINTER_CAPACITY;
}

static void set_error(unsigned flag) {
  pthread_mutex_lock(&pointer_lock);
  error_flags |= flag;
  pthread_mutex_unlock(&pointer_lock);
}

static void record_pointer(void *pointer, size_t size, unsigned kind) {
  if (!pointer) return;
  pthread_mutex_lock(&pointer_lock);
  size_t slot = AROS_POINTER_CAPACITY;
  for (size_t i = 0; i < AROS_POINTER_CAPACITY; ++i) {
    if (!pointer_table[i].pointer) { slot = i; break; }
  }
  if (slot == AROS_POINTER_CAPACITY) {
    error_flags |= AROS_ERROR_TABLE_OVERFLOW;
  } else if (SIZE_MAX - live_bytes < size) {
    error_flags |= AROS_ERROR_ARITHMETIC;
  } else {
    pointer_table[slot].pointer = pointer;
    pointer_table[slot].size = size;
    pointer_table[slot].kind = kind;
    live_bytes += size;
    live_allocations += 1;
  }
  pthread_mutex_unlock(&pointer_lock);
}

static int remove_pointer(void *pointer, size_t size, unsigned kind,
                          unsigned unknown_error) {
  int found = 0;
  pthread_mutex_lock(&pointer_lock);
  size_t slot = find_pointer(pointer);
  if (slot == AROS_POINTER_CAPACITY || pointer_table[slot].kind != kind ||
      (kind == 2u && pointer_table[slot].size != size)) {
    error_flags |= unknown_error;
  } else {
    size_t old_size = pointer_table[slot].size;
    if (live_bytes < old_size || live_allocations == 0) {
      error_flags |= AROS_ERROR_ARITHMETIC;
    } else {
      live_bytes -= old_size;
      live_allocations -= 1;
    }
    memset(&pointer_table[slot], 0, sizeof(pointer_table[slot]));
    found = 1;
  }
  pthread_mutex_unlock(&pointer_lock);
  return found;
}

static void discard_replaced_mappings(void *address, size_t size,
                                      void *preserve_pointer) {
  uintptr_t replacement_start = (uintptr_t)address;
  uintptr_t replacement_end = replacement_start + size;
  int found = 0;
  pthread_mutex_lock(&pointer_lock);
  if (replacement_end < replacement_start) {
    error_flags |= AROS_ERROR_ARITHMETIC;
    pthread_mutex_unlock(&pointer_lock);
    return;
  }
  for (size_t i = 0; i < AROS_POINTER_CAPACITY; ++i) {
    if (!pointer_table[i].pointer || pointer_table[i].kind != 2u) continue;
    uintptr_t entry_start = (uintptr_t)pointer_table[i].pointer;
    uintptr_t entry_end = entry_start + pointer_table[i].size;
    if (entry_end < entry_start) {
      error_flags |= AROS_ERROR_ARITHMETIC;
      continue;
    }
    if (entry_start >= replacement_end || replacement_start >= entry_end) continue;
    if (pointer_table[i].pointer == preserve_pointer) {
      error_flags |= AROS_ERROR_MMAP_ACCOUNTING;
      continue;
    }
    if (entry_start != replacement_start || pointer_table[i].size != size)
      error_flags |= AROS_ERROR_MMAP_ACCOUNTING;
    if (live_bytes < pointer_table[i].size || live_allocations == 0) {
      error_flags |= AROS_ERROR_ARITHMETIC;
    } else {
      live_bytes -= pointer_table[i].size;
      live_allocations -= 1;
    }
    memset(&pointer_table[i], 0, sizeof(pointer_table[i]));
    found = 1;
  }
  if (!found) error_flags |= AROS_ERROR_MMAP_ACCOUNTING;
  pthread_mutex_unlock(&pointer_lock);
}

void aros_alloc_set_enabled(int enabled) {
  pthread_mutex_lock(&pointer_lock);
  atomic_store_explicit(&accounting_enabled, enabled ? 1 : 0,
                        memory_order_release);
  pthread_mutex_unlock(&pointer_lock);
}

void aros_alloc_reset(void) {
  pthread_mutex_lock(&pointer_lock);
  memset(pointer_table, 0, sizeof(pointer_table));
  live_bytes = 0;
  live_allocations = 0;
  error_flags = 0;
  pthread_mutex_unlock(&pointer_lock);
}

void aros_alloc_snapshot(aros_alloc_snapshot_t *snapshot) {
  if (!snapshot) return;
  pthread_mutex_lock(&pointer_lock);
  snapshot->live_bytes = live_bytes;
  snapshot->live_allocations = live_allocations;
  snapshot->error_flags = error_flags;
  pthread_mutex_unlock(&pointer_lock);
}

void *malloc(size_t size) {
  void *pointer = __libc_malloc(size);
  if (accounting_is_enabled() && !recursion_guard && pointer) {
    recursion_guard = 1;
    record_pointer(pointer, size, 1u);
    recursion_guard = 0;
  }
  return pointer;
}

void *calloc(size_t count, size_t size) {
  if (size && count > SIZE_MAX / size) {
    if (accounting_is_enabled() && !recursion_guard) set_error(AROS_ERROR_ARITHMETIC);
    errno = ENOMEM;
    return NULL;
  }
  void *pointer = __libc_calloc(count, size);
  if (accounting_is_enabled() && !recursion_guard && pointer) {
    recursion_guard = 1;
    record_pointer(pointer, count * size, 1u);
    recursion_guard = 0;
  }
  return pointer;
}

void free(void *pointer) {
  if (!pointer) return;
  if (accounting_is_enabled() && !recursion_guard) {
    recursion_guard = 1;
    remove_pointer(pointer, 0, 1u, AROS_ERROR_UNKNOWN_FREE);
    recursion_guard = 0;
  }
  __libc_free(pointer);
}

void *realloc(void *pointer, size_t size) {
  if (!pointer) return malloc(size);
  if (size == 0) {
    if (accounting_is_enabled() && !recursion_guard) {
      recursion_guard = 1;
      remove_pointer(pointer, 0, 1u, AROS_ERROR_UNKNOWN_REALLOC);
      recursion_guard = 0;
    }
    __libc_free(pointer);
    return NULL;
  }
  size_t old_size = 0;
  int known = 0;
  if (accounting_is_enabled() && !recursion_guard) {
    recursion_guard = 1;
    pthread_mutex_lock(&pointer_lock);
    size_t slot = find_pointer(pointer);
    if (slot == AROS_POINTER_CAPACITY || pointer_table[slot].kind != 1u) {
      error_flags |= AROS_ERROR_UNKNOWN_REALLOC;
    } else {
      old_size = pointer_table[slot].size;
      known = 1;
    }
    pthread_mutex_unlock(&pointer_lock);
    recursion_guard = 0;
  }
  void *replacement = __libc_realloc(pointer, size);
  if (accounting_is_enabled() && !recursion_guard && replacement) {
    recursion_guard = 1;
    if (known) remove_pointer(pointer, 0, 1u, AROS_ERROR_UNKNOWN_REALLOC);
    record_pointer(replacement, size, 1u);
    recursion_guard = 0;
  } else if (accounting_is_enabled() && known && !replacement) {
    (void)old_size;
  }
  return replacement;
}

void *memalign(size_t alignment, size_t size) {
  void *pointer = __libc_memalign(alignment, size);
  if (accounting_is_enabled() && !recursion_guard && pointer) {
    recursion_guard = 1;
    record_pointer(pointer, size, 1u);
    recursion_guard = 0;
  }
  return pointer;
}

void *aligned_alloc(size_t alignment, size_t size) {
  if (!alignment || size % alignment != 0) { errno = EINVAL; return NULL; }
  return memalign(alignment, size);
}

void *valloc(size_t size) {
  long page = sysconf(_SC_PAGESIZE);
  if (page <= 0) { errno = EINVAL; return NULL; }
  return memalign((size_t)page, size);
}

void *pvalloc(size_t size) {
  long raw_page = sysconf(_SC_PAGESIZE);
  if (raw_page <= 0) { errno = EINVAL; return NULL; }
  size_t page = (size_t)raw_page;
  if (size > SIZE_MAX - (page - 1)) {
    if (accounting_is_enabled() && !recursion_guard) set_error(AROS_ERROR_ARITHMETIC);
    errno = ENOMEM;
    return NULL;
  }
  size_t rounded = (size + page - 1) & ~(page - 1);
  return memalign(page, rounded);
}

int posix_memalign(void **result, size_t alignment, size_t size) {
  if (!result || alignment < sizeof(void *) || (alignment & (alignment - 1)))
    return EINVAL;
  void *pointer = __libc_memalign(alignment, size);
  if (!pointer) return ENOMEM;
  *result = pointer;
  if (accounting_is_enabled() && !recursion_guard) {
    recursion_guard = 1;
    record_pointer(pointer, size, 1u);
    recursion_guard = 0;
  }
  return 0;
}

void *mmap(void *address, size_t length, int protection, int flags,
           int descriptor, off_t offset) {
  void *result = (void *)syscall(SYS_mmap, address, length, protection, flags,
                                 descriptor, offset);
  if (result == MAP_FAILED) return MAP_FAILED;
  if (accounting_is_enabled() && !recursion_guard) {
    recursion_guard = 1;
    if (flags & MAP_FIXED) discard_replaced_mappings(result, length, NULL);
    record_pointer(result, length, 2u);
    recursion_guard = 0;
  }
  return result;
}

void *mmap64(void *address, size_t length, int protection, int flags,
             int descriptor, off64_t offset) {
  return mmap(address, length, protection, flags, descriptor, (off_t)offset);
}

void *mremap(void *old_address, size_t old_size, size_t new_size, int flags,
             ...) {
  void *new_address = NULL;
  if (flags & MREMAP_FIXED) {
    va_list arguments;
    va_start(arguments, flags);
    new_address = va_arg(arguments, void *);
    va_end(arguments);
  }
  void *result = (void *)syscall(SYS_mremap, old_address, old_size, new_size,
                                 flags, new_address);
  if (result == MAP_FAILED || !accounting_is_enabled() || recursion_guard) return result;
  recursion_guard = 1;
  if (flags & MREMAP_FIXED)
    discard_replaced_mappings(result, new_size, old_address);
#ifdef MREMAP_DONTUNMAP
  if (flags & MREMAP_DONTUNMAP) {
    pthread_mutex_lock(&pointer_lock);
    size_t old_slot = find_pointer(old_address);
    if (old_slot == AROS_POINTER_CAPACITY ||
        pointer_table[old_slot].kind != 2u ||
        pointer_table[old_slot].size != old_size)
      error_flags |= AROS_ERROR_MMAP_ACCOUNTING;
    pthread_mutex_unlock(&pointer_lock);
    record_pointer(result, new_size, 2u);
    recursion_guard = 0;
    return result;
  }
#endif
  remove_pointer(old_address, old_size, 2u, AROS_ERROR_MMAP_ACCOUNTING);
  record_pointer(result, new_size, 2u);
  recursion_guard = 0;
  return result;
}

int munmap(void *address, size_t length) {
  long result = syscall(SYS_munmap, address, length);
  if (result == 0 && accounting_is_enabled() && !recursion_guard) {
    recursion_guard = 1;
    remove_pointer(address, length, 2u, AROS_ERROR_MMAP_ACCOUNTING);
    recursion_guard = 0;
  }
  return (int)result;
}
'''.encode("ascii")


def allocator_interposer_source() -> bytes:
    return _ALLOCATOR_INTERPOSER


def capacity_probe_source(policy: str) -> bytes:
    return _CAPACITY_TEMPLATE.replace(b"@POLICY@", policy.encode("ascii"))


def metadata_probe_source(policy: str) -> bytes:
    return _METADATA_TEMPLATE.replace(b"@POLICY@", policy.encode("ascii"))


def probe_build_flags(
    cache_path: Path, source_receipt: Mapping[str, object]
) -> tuple[list[str], list[str], str]:
    raw = _regular_bytes(cache_path)
    if len(raw) > 4 * 1024 * 1024:
        raise ProbeError("Release CMake cache is too large")
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeError as error:
        raise ProbeError("Release CMake cache is not UTF-8") from error
    values: dict[str, str] = {}
    for line in lines:
        if not line or line.startswith(("#", "//")) or ":" not in line or "=" not in line:
            continue
        name = line.split(":", 1)[0]
        value = line.split("=", 1)[1]
        if name in values:
            raise ProbeError(f"duplicate Release CMake cache binding: {name}")
        values[name] = value
    compilers = source_receipt["compilers"]
    expected_c = compilers["c"]["path"]
    expected_cxx = compilers["cxx"]["path"]
    if (
        values.get("CMAKE_C_COMPILER") != expected_c
        or values.get("CMAKE_CXX_COMPILER") != expected_cxx
    ):
        raise ProbeError("Release compiler selection differs from source receipt")
    includes = values.get("GLib_INCLUDE_DIRS", "").split(";")
    if not includes or any(not item or not Path(item).is_absolute() for item in includes):
        raise ProbeError("Release GLib include binding is invalid")
    libraries = values.get("GLib_LIBRARIES", "").split(";")
    if not libraries or any(
        re.fullmatch(r"[A-Za-z0-9_.+-]+", item) is None for item in libraries
    ):
        raise ProbeError("Release GLib library binding is invalid")
    include_flags = [flag for item in includes for flag in ("-I", item)]
    link_flags = [f"-l{item}" for item in libraries]
    if values.get("OPT_SUPPORT_ZSTD_TRACE") == "ON":
        zstd = values.get("ZSTD_LIBRARY_RELEASE")
        if zstd is None or not Path(zstd).is_absolute() or zstd.endswith("-NOTFOUND"):
            raise ProbeError("Release ZSTD library binding is invalid")
        link_flags.append(zstd)
    tcmalloc = values.get("Tcmalloc_LIBRARY")
    if tcmalloc and not tcmalloc.endswith("-NOTFOUND"):
        if not Path(tcmalloc).is_absolute():
            raise ProbeError("Release tcmalloc library binding is invalid")
        link_flags.append(tcmalloc)
    link_flags.extend(["-lstdc++", "-lm", "-ldl", "-pthread"])
    return include_flags, link_flags, hashlib.sha256(raw).hexdigest()


def capacity_compile_argv(
    compiler: str,
    checkout: Path,
    output: Path,
    source: Path,
    archive: Path,
    include_flags: Sequence[str],
    link_flags: Sequence[str],
) -> list[str]:
    return [
        compiler,
        "-std=c11",
        "-O2",
        "-I",
        str(checkout / "libCacheSim/include"),
        *include_flags,
        "-o",
        str(output),
        str(source),
        str(archive),
        *link_flags,
    ]


def allocator_compile_argv(
    compiler: str, output: Path, source: Path
) -> list[str]:
    return [
        compiler,
        "-std=c11",
        "-O2",
        "-shared",
        "-fPIC",
        "-pthread",
        "-ldl",
        "-o",
        str(output),
        str(source),
    ]


def metadata_compile_argv(
    compiler: str,
    checkout: Path,
    output: Path,
    source: Path,
    archive: Path,
    include_flags: Sequence[str],
    link_flags: Sequence[str],
) -> list[str]:
    return [
        compiler,
        "-std=c11",
        "-O2",
        "-I",
        str(checkout / "libCacheSim/include"),
        *include_flags,
        "-o",
        str(output),
        str(source),
        str(archive),
        *link_flags,
    ]


def metadata_run_argv(
    interposer: Path,
    probe: Path,
    trace: Path,
    policy: str,
    cache_size: int,
) -> list[str]:
    return [
        "/usr/bin/env",
        f"LD_PRELOAD={interposer}",
        str(probe),
        str(trace),
        policy,
        str(cache_size),
    ]
