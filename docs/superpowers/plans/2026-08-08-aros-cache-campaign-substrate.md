# AROS Cache Campaign Substrate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and independently verify a pinned, trace-sealed, Pareto-reporting libCacheSim apparatus for R0-R3 and freeze host-calibrated SIEVE/S3-FIFO constraints without adding production AROS code.

**Architecture:** Keep every implementation file under `commissioning/cache_campaign`, `scripts`, or `tests`. A small standard-library package owns strict records, source pinning, trace commitments, libCacheSim parsing, factual measurements, baseline calibration, and the one-shot R3 seal; thin scripts expose those operations, while a standalone verifier recomputes the retained evidence without importing the apparatus. The task-visible bundle contains only dev/visible paths and an R3 commitment; the R3 manifest, paths, and one-shot ledger remain in a separate host directory.

**Tech Stack:** Python 3.10+ standard library, pytest, Git, CMake, Ninja, CTest, Clang/GCC sanitizers, and pinned libCacheSim commit `da022c2945146e9577d91375a48d53850d7041a3`.

---

## Scope and success criteria

This is delivery parts 1-2 of the approved cache campaign design only. It does not add a Researcher, Reviewer, Principal driver, campaign Claim verifier, source downloader, or any line below `src/aros`.

The implementation is complete when all of the following are true:

- a dirty, unpinned, or incorrectly built libCacheSim checkout is rejected;
- host-provided trace bytes are bound by provenance, continuous windows, byte size, and SHA-256 without entering Git;
- dev contains at least three windows and two organization/application sources, visible validation differs by application or disjoint time, and R3 uses organizations absent from both visible layers;
- the task-visible bundle contains no R3 path or trace identity, only a commitment;
- libCacheSim output becomes an immutable Pareto record with no scalar score;
- R2 retains exact one-hit/reuse-distance trace facts and continuous phase-bin miss facts without interpreting them;
- R0 reports build/tests, determinism, sanitizer, capacity, scope, metadata, and complexity-audit state separately;
- R1 is exactly three dev windows by three cache fractions, while R2 covers the complete dev and visible manifests;
- baseline calibration freezes repeated SIEVE/S3-FIFO distributions and candidate hard constraints before Researcher launch;
- R3 consumes a host-only one-shot token only after a frozen package commit and never retries;
- the standalone verifier detects mutations of source, manifests, trace commitments, calibration, measurements, or the R3 ledger;
- focused tests, the full repository suite, Ruff, registry validation, and `git diff --check` pass.

## File map

- Create `commissioning/cache_campaign/__init__.py`: explicit commissioning package boundary and schema version.
- Create `commissioning/cache_campaign/source.lock.json`: exact upstream URL, commit, tree, build command, binary path, and baseline policies.
- Create `commissioning/cache_campaign/records.py`: strict JSON/object helpers, canonical hashes, typed trace and measurement records.
- Create `commissioning/cache_campaign/source.py`: validate and build an already host-provisioned pinned checkout.
- Create `commissioning/cache_campaign/oracle.py`: bounded-disk OracleGeneral audit and mechanism-neutral trace diagnostics.
- Create `commissioning/cache_campaign/manifests.py`: freeze dev/visible manifests and the separate R3 commitment.
- Create `commissioning/cache_campaign/cachesim.py`: exact output parser and child CPU accounting.
- Create `commissioning/cache_campaign/scope.py`: confirmation diff, metadata-contract, and complexity-audit checks.
- Create `commissioning/cache_campaign/evaluate.py`: factual R0-R2 execution and immutable receipts.
- Create `commissioning/cache_campaign/diagnostics.py`: phase-probe records and exploratory-sidecar validation.
- Create `commissioning/cache_campaign/calibrate.py`: repeat baselines and freeze non-scalar hard constraints.
- Create `commissioning/cache_campaign/seal.py`: consume the temporal R3 seal and run it once.
- Create `commissioning/cache_campaign/README.md`: operator contract, authority boundary, and exact commands.
- Create `scripts/prepare_aros_cache_source.py`: source validation/build CLI.
- Create `scripts/freeze_aros_cache_manifests.py`: split-freeze CLI.
- Create `scripts/run_aros_cache_eval.py`: R0-R2 CLI.
- Create `scripts/calibrate_aros_cache_baselines.py`: baseline-freeze CLI.
- Create `scripts/run_aros_cache_r3.py`: host-only R3 CLI.
- Create `scripts/verify_aros_cache_substrate.py`: independent retained-evidence verifier.
- Create `tests/test_aros_cache_source.py`: source lock and build tests.
- Create `tests/test_aros_cache_manifests.py`: contamination and visibility tests.
- Create `tests/test_aros_cache_parser.py`: parser and resource-accounting tests.
- Create `tests/test_aros_cache_r0.py`: R0 scope, metadata, sanitizer, and determinism tests.
- Create `tests/test_aros_cache_evaluator.py`: R1/R2 and Pareto receipt tests.
- Create `tests/test_aros_cache_diagnostics.py`: trace/phase diagnostic and exploratory-label tests.
- Create `tests/test_aros_cache_calibration.py`: baseline freeze and hard-constraint tests.
- Create `tests/test_aros_cache_r3.py`: temporal-seal and no-retry tests.
- Create `tests/test_aros_cache_substrate_verifier.py`: standalone tamper tests.
- Modify `docs/document_registry.json`: register the retained substrate result document when execution produces it; do not change product authority.

### Task 1: Pin, validate, and build libCacheSim

**Files:**
- Create: `commissioning/cache_campaign/__init__.py`
- Create: `commissioning/cache_campaign/source.lock.json`
- Create: `commissioning/cache_campaign/records.py`
- Create: `commissioning/cache_campaign/source.py`
- Create: `scripts/prepare_aros_cache_source.py`
- Test: `tests/test_aros_cache_source.py`

- [ ] **Step 1: Write the failing source-lock tests**

Create a temporary Git repository in `tests/test_aros_cache_source.py`, commit an executable fake `_build/bin/cachesim`, and test these exact cases:

```python
def test_source_lock_is_exact() -> None:
    lock = load_object(ROOT / "commissioning/cache_campaign/source.lock.json")
    assert lock == {
        "schema_version": 1,
        "repository_url": "https://github.com/1a1a11a/libCacheSim.git",
        "commit": "da022c2945146e9577d91375a48d53850d7041a3",
        "tree": "d59c0319fff072788ab5d5a5c1f204f758082c80",
        "configure_argv": [
            "cmake", "-S", ".", "-B", "_build", "-G", "Ninja",
            "-DCMAKE_BUILD_TYPE=Release", "-DENABLE_TESTS=ON",
        ],
        "build_argv": ["cmake", "--build", "_build", "-j", "8"],
        "test_argv": ["ctest", "--test-dir", "_build", "--output-on-failure"],
        "binary": "_build/bin/cachesim",
        "baseline_policies": ["Sieve", "S3FIFO"],
        "comparison_policies": ["LRU", "ARC", "WTinyLFU", "Sieve", "S3FIFO", "BeladySize"],
    }


@pytest.mark.parametrize("mutation", ["wrong_head", "dirty", "wrong_remote", "wrong_tree"])
def test_validate_source_rejects_unbound_checkout(tmp_path: Path, mutation: str) -> None:
    checkout, lock = fake_checkout(tmp_path)
    mutate(checkout, lock, mutation)
    with pytest.raises(SourceError):
        validate_source(checkout, lock)


def test_prepare_records_commands_versions_and_binary_hash(tmp_path: Path) -> None:
    checkout, lock = fake_checkout(tmp_path)
    receipt = prepare_source(checkout, tmp_path / "source-receipt.json", lock, run=fake_run)
    assert receipt["commit"] == lock["commit"]
    assert receipt["tree"] == lock["tree"]
    assert receipt["clean"] is True
    assert receipt["binary_sha256"] == sha256_file(checkout / lock["binary"])
    assert [item["argv"] for item in receipt["commands"]] == [
        lock["configure_argv"], lock["build_argv"], lock["test_argv"]
    ]
```

The fake repository helper must set `remote.origin.url`, and `fake_run` must create the binary only after the build command. Also assert the output path must not exist, the receipt has its own `receipt_sha256`, and no clone or fetch command is accepted.

- [ ] **Step 2: Run the focused test and verify RED**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q tests/test_aros_cache_source.py
```

Expected: collection fails because the cache campaign package and lock do not exist.

- [ ] **Step 3: Add canonical hashing and strict source validation**

Set `SCHEMA_VERSION = 1` in `__init__.py`. In `records.py`, implement and reuse only these primitives:

```python
HEX64 = re.compile(r"[0-9a-f]{64}\Z")


class ContractError(ValueError):
    pass


def canonical_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def record_sha256(value: Mapping[str, object], field: str) -> str:
    return hashlib.sha256(canonical_bytes({k: v for k, v in value.items() if k != field})).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_object(path: Path) -> dict[str, object]:
    value = json.loads(path.read_bytes(), object_pairs_hook=_unique_object)
    if not isinstance(value, dict):
        raise ContractError(f"JSON object required: {path}")
    return value


def write_new_record(path: Path, value: dict[str, object], hash_field: str) -> None:
    if path.exists():
        raise ContractError(f"refusing to replace immutable record: {path}")
    value[hash_field] = record_sha256(value, hash_field)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
```

`_unique_object` must reject duplicate JSON keys. `source.py` must call Git with `capture_output=True`, reject nonzero status, compare exact `HEAD`, `HEAD^{tree}`, normalized origin URL, and empty `git status --porcelain=v1 --untracked-files=all`. `prepare_source` executes the three locked commands with `cwd=checkout`, records argv/return code/stdout SHA-256/stderr SHA-256, captures `cmake --version`, `ninja --version`, compiler version, interpreter, platform, binary SHA-256, and writes a new receipt. It accepts a `run` seam only for tests; production uses `subprocess.run`.

Write `source.lock.json` with the object asserted in Step 1. Before implementation, verify the tree once against the pinned checkout:

```bash
git -C /tmp/aros-cache-plan.Od15BG/libCacheSim rev-parse 'HEAD^{tree}'
```

It must return `d59c0319fff072788ab5d5a5c1f204f758082c80`; otherwise stop because the inspected checkout differs from the approved pin.

- [ ] **Step 4: Add the thin source CLI**

`scripts/prepare_aros_cache_source.py` must expose only:

```python
parser.add_argument("--checkout", type=Path, required=True)
parser.add_argument("--receipt", type=Path, required=True)
args = parser.parse_args()
prepare_source(args.checkout.resolve(strict=True), args.receipt.absolute(), LOCK)
```

Catch `OSError`, `ValueError`, and `subprocess.SubprocessError`, print one `error:` line to stderr, and exit 2. It must never clone, fetch, reset, or clean a checkout.

- [ ] **Step 5: Verify GREEN and commit**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q tests/test_aros_cache_source.py
/workspace/Arbor/.venv/bin/ruff check commissioning/cache_campaign scripts/prepare_aros_cache_source.py tests/test_aros_cache_source.py
git diff --check
git add commissioning/cache_campaign/__init__.py commissioning/cache_campaign/records.py \
  commissioning/cache_campaign/source.py commissioning/cache_campaign/source.lock.json \
  scripts/prepare_aros_cache_source.py tests/test_aros_cache_source.py
git -c user.name='AROS Agent' -c user.email='aros-agent@example.invalid' \
  commit -m 'test(aros): pin cache campaign source'
```

Expected: all commands exit 0.

### Task 2: Freeze trace manifests without exposing R3

**Files:**
- Modify: `commissioning/cache_campaign/records.py`
- Create: `commissioning/cache_campaign/oracle.py`
- Create: `commissioning/cache_campaign/manifests.py`
- Create: `scripts/freeze_aros_cache_manifests.py`
- Test: `tests/test_aros_cache_manifests.py`

- [ ] **Step 1: Write failing manifest and contamination tests**

Use concrete fixture windows made from ten `struct.pack("<IQIq", ...)` OracleGeneral records and a complete input record with cache fractions `[0.01, 0.05, 0.10]`. Each trace object must have exactly:

```python
{
    "trace_id": "dev-meta-kv-a",
    "split": "dev",
    "organization": "Meta",
    "application": "key-value",
    "dataset": "2022_metaKV",
    "provenance_url": "https://github.com/cacheMon/cache_dataset",
    "license_ref": "cache_dataset README and upstream dataset terms",
    "path": str(trace_path),
    "trace_type": "oracleGeneral",
    "origin_sha256": "1" * 64,
    "start_request": 0,
    "warmup_seconds": 1,
    "max_requests": 10,
    "working_set_bytes": 4096,
}
```

The valid fixture has three dev windows across Meta/key-value and Twitter/key-value, two visible windows using Meta/CDN and a disjoint Twitter interval, and one R3 window from Tencent/photo-CDN. Test:

```python
def test_freeze_writes_visible_splits_and_only_r3_commitment(tmp_path: Path) -> None:
    task, host = freeze_manifests(
        input_path, task_root, tmp_path / "task", tmp_path / "host"
    )
    task_bytes = b"".join(path.read_bytes() for path in sorted((tmp_path / "task").rglob("*")) if path.is_file())
    assert b"r3-tencent-photo" not in task_bytes
    assert os.fsencode(r3_path) not in task_bytes
    assert task["r3_commitment_sha256"] == host["manifest_sha256"]
    assert {item["split"] for item in task["traces"]} == {"dev", "visible"}


@pytest.mark.parametrize("defect", [
    "duplicate_bytes", "overlapping_origin_interval", "random_sampling",
    "too_few_dev_windows", "one_dev_source", "visible_reuses_window",
    "r3_seen_organization", "fraction_mismatch", "bad_hash", "relative_path",
])
def test_freeze_rejects_contaminated_or_incomplete_portfolio(tmp_path: Path, defect: str) -> None:
    candidate = valid_candidate(tmp_path)
    inject(candidate, defect)
    with pytest.raises(ManifestError):
        freeze_manifests(
            candidate.path, task_root, tmp_path / "task", tmp_path / "host"
        )
```

Also assert the freezer recomputes file byte size and SHA-256, refuses symlinks/non-regular files, refuses any `sampling` field, requires `start_request >= 0`, `warmup_seconds > 0`, `max_requests > 0`, and requires all intervals sharing an `origin_sha256` to be disjoint. Failed freezing must leave neither output directory behind.

- [ ] **Step 2: Run the focused test and verify RED**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q tests/test_aros_cache_manifests.py
```

Expected: import failure for `commissioning.cache_campaign.manifests`.

- [ ] **Step 3: Define strict typed manifest records**

Add frozen dataclasses in `records.py`:

```python
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


@dataclass(frozen=True)
class Portfolio:
    source_commit: str
    cache_fractions: tuple[float, float, float]
    traces: tuple[TraceWindow, ...]
```

Implement `TraceWindow.from_candidate` with exact-key validation, absolute path resolution, regular-file/no-symlink validation, streaming hash, positive `working_set_bytes`, and nonempty string checks. Do not accept coercions such as string integers. `oracle.py` scans each uncompressed 24-byte OracleGeneral window (`<IQIq`) through exactly `max_requests`, rejects truncation/non-monotonic timestamps/nonpositive object sizes, and partitions `(object_id, object_size)` pairs into 256 private bucket files. Process one bucket at a time to compute final size and access count per object, then delete it; this bounds RAM without random sampling. Require the resulting working-set sum to equal the supplied `working_set_bytes`. The same scan freezes one-hit object/request fractions plus base-2 reuse-distance counts from `next_access_vtime`; compressed inputs are rejected so the audit uses only the standard library. `Portfolio` accepts exactly `[0.01, 0.05, 0.10]`; these fractions are the common cache-size range for every split and become immutable after calibration.

- [ ] **Step 4: Implement transactional split freezing**

`freeze_manifests(candidate_path, task_root, task_output, host_output)` must validate everything before creating a sibling temporary directory. Enforce:

```python
if len(dev) < 3 or len({(t.organization, t.application) for t in dev}) < 2:
    raise ManifestError("dev requires three windows and two sources")
if any(not differs_from_every_dev_window(item, dev) for item in visible):
    raise ManifestError("visible must differ by application or disjoint time")
seen_orgs = {t.organization for t in dev + visible}
if any(item.organization in seen_orgs for item in r3):
    raise ManifestError("R3 organization must be unseen")
```

The task manifest contains all dev/visible fields plus measured hashes, paths, and trace-diagnostic hashes. The host manifest contains the equivalent R3 facts. First hash the host manifest without a self-hash, then put that digest in task field `r3_commitment_sha256`; each final record receives its own `manifest_sha256`. Atomically rename both temporary directories only after both records are durable. The task output must not contain R3 `trace_id`, organization, application, dataset, path, origin hash, file hash, provenance URL, or diagnostics.

- [ ] **Step 5: Add the freeze CLI and verify GREEN**

The script accepts required `--input`, `--task-root`, `--task-output`, and
`--host-output` arguments, requires `task-output` strictly beneath the real
task root and the host output outside it, resolves the input, requires both
output paths not to exist, calls `freeze_manifests`, and prints only the task
manifest path plus R3 commitment.

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q tests/test_aros_cache_manifests.py
/workspace/Arbor/.venv/bin/ruff check commissioning/cache_campaign/manifests.py \
  commissioning/cache_campaign/oracle.py \
  commissioning/cache_campaign/records.py scripts/freeze_aros_cache_manifests.py \
  tests/test_aros_cache_manifests.py
git diff --check
git add commissioning/cache_campaign/records.py commissioning/cache_campaign/oracle.py \
  commissioning/cache_campaign/manifests.py \
  scripts/freeze_aros_cache_manifests.py tests/test_aros_cache_manifests.py
git -c user.name='AROS Agent' -c user.email='aros-agent@example.invalid' \
  commit -m 'test(aros): freeze cache trace boundaries'
```

Expected: all commands exit 0.

### Task 3: Parse libCacheSim output and measure child CPU cost

**Files:**
- Create: `commissioning/cache_campaign/cachesim.py`
- Test: `tests/test_aros_cache_parser.py`

- [ ] **Step 1: Write failing parser tests from the pinned binary format**

Use this exact upstream line, including a policy name with parameters:

```python
LINE = (
    "/trace/dev-a.oracleGeneral.bin S3FIFO-0.1000-2 cache size  10.00MiB, "
    "          900000 req, miss ratio 0.1234, byte miss ratio 0.2345, "
    "throughput 20.25 MQPS\n"
)


def test_parse_single_result_line() -> None:
    parsed = parse_cachesim_output(LINE)
    assert parsed == ParsedResult(
        request_count=900_000,
        object_miss_ratio=Decimal("0.1234"),
        byte_miss_ratio=Decimal("0.2345"),
        simulator_throughput_mqps=Decimal("20.25"),
    )
```

Reject missing byte ratio, missing throughput, NaN/infinity, ratios outside `[0, 1]`, zero requests, duplicate result lines, and extra non-log result lines. Accept known libCacheSim INFO/header lines but retain their bytes in the raw-output hash.

For resource accounting, launch a tiny child that burns CPU and exits 7. Assert `run_child` returns stdout/stderr bytes, the exact exit code, wall nanoseconds, user+system CPU nanoseconds from `os.wait4`, and never shells out:

```python
result = run_child([sys.executable, "-c", "sum(i*i for i in range(100000)); raise SystemExit(7)"], tmp_path)
assert result.returncode == 7
assert result.cpu_ns > 0
assert result.wall_ns >= 0
assert result.argv[0] == sys.executable
```

- [ ] **Step 2: Run the focused test and verify RED**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q tests/test_aros_cache_parser.py
```

Expected: import failure for `commissioning.cache_campaign.cachesim`.

- [ ] **Step 3: Implement the anchored parser and `wait4` runner**

Use `Decimal`, not float, for parsed metrics. Anchor one expression to the whole result line:

```python
RESULT = re.compile(
    r"^.+ cache size\s+[^,]+,\s+(?P<requests>[0-9]+) req, "
    r"miss ratio (?P<object>[0-9]+\.[0-9]+), byte miss ratio "
    r"(?P<byte>[0-9]+\.[0-9]+), throughput (?P<throughput>[0-9]+\.[0-9]+) MQPS$"
)
```

`run_child` opens raw stdout/stderr files inside a new output directory, calls `subprocess.Popen(argv, shell=False, start_new_session=True)`, then `os.wait4(process.pid, 0)`. Set `process.returncode = os.waitstatus_to_exitcode(status)` after `wait4` so cleanup does not reap twice. Return an immutable `ChildResult`; `cpu_ns = round((ru_utime + ru_stime) * 1_000_000_000)`. This Linux-only commissioning implementation must fail explicitly when `os.wait4` is unavailable.

- [ ] **Step 4: Verify GREEN and commit**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q tests/test_aros_cache_parser.py
/workspace/Arbor/.venv/bin/ruff check commissioning/cache_campaign/cachesim.py tests/test_aros_cache_parser.py
git diff --check
git add commissioning/cache_campaign/cachesim.py tests/test_aros_cache_parser.py
git -c user.name='AROS Agent' -c user.email='aros-agent@example.invalid' \
  commit -m 'test(aros): parse cache Pareto measurements'
```

Expected: all commands exit 0.

### Task 4: Build factual R0 scope, metadata, and invariant checks

**Files:**
- Create: `commissioning/cache_campaign/scope.py`
- Create: `commissioning/cache_campaign/evaluate.py`
- Create: `scripts/run_aros_cache_eval.py`
- Test: `tests/test_aros_cache_r0.py`

- [ ] **Step 1: Write failing R0 contract tests**

The confirmation candidate must include `commissioning/cache_policy_contract.json` at its commit with this exact schema:

```python
POLICY_CONTRACT = {
    "schema_version": 1,
    "policy": "CandidatePolicy",
    "reference_policy": "Sieve",
    "policy_source": "libCacheSim/cache/eviction/CandidatePolicy.c",
    "object_metadata_bytes": 1,
    "global_metadata_bytes": 24,
    "global_metadata_evidence": [
        {"source": "libCacheSim/cache/eviction/CandidatePolicy.c", "line": 10, "expression": "sizeof(CandidatePolicy_params_t)"}
    ],
    "update_complexity": "amortized O(1)",
}
```

Test exact-key/type validation and `reference_policy in {"Sieve", "S3FIFO"}`. Given a pinned base and candidate commit, accept only:

```text
libCacheSim/cache/eviction/CandidatePolicy.c
libCacheSim/include/libCacheSim/evictionAlgo.h
libCacheSim/cache/CMakeLists.txt
libCacheSim/bin/cachesim/cache_init.h
test/CMakeLists.txt
test/test_CandidatePolicy.c
commissioning/cache_policy_contract.json
```

Reject deletion/rename of an existing baseline file, changes to simulator/reader/baseline policies, more than one candidate policy source, a contract whose policy/source differs from the diff, and metadata evidence outside the candidate policy. Require the wiring files to change only by additions naming `CandidatePolicy`; test this by inspecting zero-context unified diff hunks.

Mock commands and assert candidate R0 invokes, in order: clean Release configure/build/CTest, candidate-specific CTest, clean ASan+UBSan configure/build/CTest, two identical deterministic synthetic simulations, a capacity probe, and an allocation-accounting metadata probe. A baseline R0 with `candidate == base` and policy `Sieve` or `S3FIFO` skips candidate-diff/contract checks but runs every build, invariant, and operational metadata check. The two parsed output records must match except runtime fields. A sanitizer diagnostic, capacity violation, output mismatch, failed test, unaccounted allocation, or dirty source makes its individual check false.

- [ ] **Step 2: Run the focused test and verify RED**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q tests/test_aros_cache_r0.py
```

Expected: import failure for `scope` or missing `evaluate_r0`.

- [ ] **Step 3: Implement exact confirmation scope and audit states**

In `scope.py`, use `git diff --name-status --no-renames <base>..<candidate>` and `git diff --unified=0`. Return facts, not one boolean:

```python
@dataclass(frozen=True)
class ScopeFacts:
    allowed_paths: bool
    baseline_unchanged: bool
    additive_wiring_only: bool
    contract_bound: bool | None
    changed_paths: tuple[str, ...]
    diff_sha256: str


@dataclass(frozen=True)
class ConstraintFacts:
    measured_metadata_bytes_per_object: Decimal | None
    measured_global_metadata_bytes: int | None
    metadata_measurement_sha256: str | None
    metadata_within_budget: bool | None
    complexity_audit: Literal["pending_independent_review", "accepted", "rejected"]
    capacity_conserved: bool
    deterministic: bool
    sanitizer_clean: bool
```

`contract_bound=None` is valid only for an unchanged pinned baseline. Other `None` values mean a factual comparison cannot yet be made because calibration or independent source audit is absent; they must never be converted to pass. The apparatus reports both operational metadata measurements and candidate-declared source-line evidence, but the independent Reviewer later decides whether the declaration and allocation probe cover every policy path. Do not claim that a regex proves amortized complexity.

- [ ] **Step 4: Implement R0 with pre-registered synthetic data**

`evaluate_r0` creates a deterministic 10,000-request `oracleGeneral` fixture in its private output directory using `struct.pack("<IQIq", timestamp, object_id, size, next_access)`. Its generator seed, distribution, and file hash are written to the receipt; it is apparatus data, not an undeclared production trace. Use separate `_build-release` and `_build-sanitize` directories and configure sanitizer flags explicitly:

```text
-DCMAKE_BUILD_TYPE=RelWithDebInfo
-DCMAKE_C_FLAGS=-fsanitize=address,undefined -fno-omit-frame-pointer
-DCMAKE_CXX_FLAGS=-fsanitize=address,undefined -fno-omit-frame-pointer
-DCMAKE_EXE_LINKER_FLAGS=-fsanitize=address,undefined
```

Before building, require a clean checkout whose `HEAD` equals `candidate`; require `base` to equal the source-lock commit/tree and `git merge-base --is-ancestor base candidate` to succeed. Do not require candidate HEAD or candidate binary to equal the pinned baseline source receipt: the receipt binds the parent apparatus, while R0 binds the derived candidate commit, diff, and newly built binary.

The capacity probe is a small generated C test linked against the candidate build; after every request it asserts `0 <= cache->get_occupied_byte(cache) <= cache->cache_size`. A second generated probe links with `--wrap=malloc`, `--wrap=calloc`, `--wrap=realloc`, and `--wrap=free`; a fixed host-side pointer table records requested live bytes without allocating inside the wrappers. It emits live bytes immediately after policy initialization (`global_metadata_bytes`) and after 1,000, 5,000, and 10,000 unique inserts. For each point compute `(live_bytes - global_metadata_bytes) / cache->get_n_obj(cache)` and retain the maximum as `metadata_bytes_per_object`; any unknown free/realloc, table overflow, negative delta, or zero resident count invalidates the fact. This operational metric includes common cache structures and ghost-policy allocations, but the apparatus is identical for reference and candidate.

The receipt has separate `checks`, `scope`, `declared_metadata`, `measured_metadata`, `complexity_audit`, command receipts, raw hashes, source commit/tree, candidate commit/diff, evaluator hash, and `receipt_sha256`. For unchanged baselines, `scope.changed_paths=[]`, `scope.contract_bound=null`, and policy must be one of the two locked baselines. It has no `score`, `objective`, `reward`, or aggregate `pass` field.

The CLI accepts:

```text
--rung r0 --checkout PATH --candidate COMMIT --base COMMIT --policy NAME
--source-receipt PATH --output NEW_DIRECTORY
```

It refuses `r1`, `r2`, or `r3` until later tasks add their exact arguments.

- [ ] **Step 5: Verify GREEN and commit**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q tests/test_aros_cache_r0.py tests/test_aros_cache_parser.py
/workspace/Arbor/.venv/bin/ruff check commissioning/cache_campaign/scope.py \
  commissioning/cache_campaign/evaluate.py scripts/run_aros_cache_eval.py tests/test_aros_cache_r0.py
git diff --check
git add commissioning/cache_campaign/scope.py commissioning/cache_campaign/evaluate.py \
  scripts/run_aros_cache_eval.py tests/test_aros_cache_r0.py
git -c user.name='AROS Agent' -c user.email='aros-agent@example.invalid' \
  commit -m 'test(aros): add cache campaign r0 checks'
```

Expected: all commands exit 0.

### Task 5: Run R1/R2 as immutable Pareto measurements

**Files:**
- Modify: `commissioning/cache_campaign/records.py`
- Modify: `commissioning/cache_campaign/evaluate.py`
- Create: `commissioning/cache_campaign/diagnostics.py`
- Modify: `scripts/run_aros_cache_eval.py`
- Test: `tests/test_aros_cache_evaluator.py`
- Test: `tests/test_aros_cache_diagnostics.py`

- [ ] **Step 1: Write failing rung and receipt tests**

Use a fake cachesim executable that appends its argv to a log and emits a valid result. Assert:

```python
def test_r1_is_three_dev_windows_by_three_sizes(tmp_path: Path) -> None:
    receipt = evaluate_portfolio(
        rung="r1",
        task_root=task_root,
        task_manifest=task_manifest,
        checkout=checkout,
        candidate=candidate,
        policy="CandidatePolicy",
        source_receipt=source_receipt,
        r0_receipt=r0_receipt,
        output=output,
    )
    assert len(receipt["measurements"]) == 9
    assert {m["trace_id"] for m in receipt["measurements"]} == set(first_three_dev_ids)
    assert {Decimal(m["cache_fraction"]) for m in receipt["measurements"]} == {
        Decimal("0.01"), Decimal("0.05"), Decimal("0.10")
    }


def test_r2_covers_every_dev_and_visible_window(tmp_path: Path) -> None:
    receipt = evaluate_portfolio(
        rung="r2",
        task_root=task_root,
        task_manifest=task_manifest,
        checkout=checkout,
        candidate=candidate,
        policy="Sieve",
        source_receipt=source_receipt,
        r0_receipt=r0_receipt,
        output=output,
    )
    assert len(receipt["measurements"]) == len(task_manifest["traces"]) * 3
```

Every cachesim invocation must be one policy, one trace, one fraction, `--num-thread=1`, the exact manifest `--num-req`, exact `--warmup-sec`, `--consider-obj-metadata=true`, and `--print-head-req=false`. Test that changing trace bytes, manifest hash, pinned source receipt, R0-bound candidate binary, candidate commit, policy contract, or evaluator file after request construction aborts before launch.

Assert every measurement has exactly these scientific fields:

```python
{
    "object_miss_ratio", "byte_miss_ratio", "simulator_throughput_mqps",
    "cpu_ns_per_request", "metadata_bytes_per_object", "global_metadata_bytes",
}
```

and provenance fields for rung/split/trace/policy/cache fraction/cache bytes/request count, trace/source/candidate/evaluator hashes, argv, exit code, raw stdout/stderr hashes, CPU/wall nanoseconds, and measurement hash. Recursively reject keys named `score`, `reward`, `objective`, or `aggregate`.

In `tests/test_aros_cache_diagnostics.py`, use a 160-request continuous fixture with four known phases. Require the generated phase probe to preserve cache state across sixteen equal request-count bins after warm-up and emit per-bin requests, object misses, byte requests, and byte misses. Require R2 to bind those facts plus the manifest's one-hit/reuse-distance diagnostic hash. An optional Researcher-produced sidecar is accepted only with `evidence_class="exploratory"`, exact candidate/run/trace hashes, and raw counter names/values; reject it as confirmation evidence and do not require queue, ghost, or admission counters when the policy does not expose them.

- [ ] **Step 2: Run the focused test and verify RED**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q \
  tests/test_aros_cache_evaluator.py tests/test_aros_cache_diagnostics.py
```

Expected: failure because `evaluate_portfolio`, phase diagnostics, and R1/R2 CLI support do not exist.

- [ ] **Step 3: Add immutable measurement records**

Define:

```python
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
```

Serialize `Decimal` as a canonical decimal string, never binary float. Compute `cache_size_bytes` from the exact fraction of the `working_set_bytes` already audited and frozen in Task 2. Do not ask libCacheSim to auto-detect sizes during evaluation.

In `diagnostics.py`, define immutable `PhaseBin` and `ExploratorySidecar` records. Generate and compile one apparatus-owned C phase probe against the exact candidate build; it initializes the requested policy through pinned `cache_init.h`, consumes the same continuous reader once, warms by relative timestamp, and then emits sixteen fixed request-count bins without resetting the cache. Validate the emitted counts sum to the measured request and byte totals. This provides mechanism-neutral phase facts; queue residence, ghost hits, admission precision, and other policy internals remain optional model-authored exploratory counters and are never silently treated as confirmatory measurements.

- [ ] **Step 4: Implement deterministic R1/R2 selection and raw retention**

`evaluate_portfolio` revalidates all bindings immediately before each launch, including a successful R0 receipt for the exact source, candidate, policy, and binary. It writes one directory per measurement containing `request.json`, `stdout.raw`, `stderr.raw`, and `measurement.json`, using exclusive creation. R1 selects the first three dev entries in manifest order and omits the extra phase probe to remain cheap; R2 selects every dev and visible entry, runs the phase probe, and binds the frozen trace diagnostics. Execute sequentially in this substrate plan so CPU accounting is not contaminated; the later driver may launch at most three independent evaluator Runs, each still single-process.

CPU/request is `Decimal(child.cpu_ns) / request_count`; throughput remains the simulator's own timed-loop measurement. Copy measured object/global metadata from the bound R0 receipt into each Pareto record and include `metadata_measurement_sha256`. Never derive or rank a scalar. The root receipt lists measurement hashes and exact negative/process failures; a failed child still gets a process receipt, but no scientific measurement.

Extend the CLI with:

```text
--rung {r1,r2} --checkout PATH --candidate COMMIT --policy NAME
--task-root PATH --task-manifest PATH --source-receipt PATH
--r0-receipt PATH --output NEW_DIRECTORY
```

- [ ] **Step 5: Verify GREEN and commit**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q \
  tests/test_aros_cache_manifests.py tests/test_aros_cache_parser.py \
  tests/test_aros_cache_r0.py tests/test_aros_cache_evaluator.py \
  tests/test_aros_cache_diagnostics.py
/workspace/Arbor/.venv/bin/ruff check commissioning/cache_campaign \
  scripts/run_aros_cache_eval.py tests/test_aros_cache_evaluator.py
git diff --check
git add commissioning/cache_campaign/records.py commissioning/cache_campaign/evaluate.py \
  commissioning/cache_campaign/diagnostics.py \
  scripts/run_aros_cache_eval.py tests/test_aros_cache_manifests.py \
  tests/test_aros_cache_evaluator.py tests/test_aros_cache_diagnostics.py
git -c user.name='AROS Agent' -c user.email='aros-agent@example.invalid' \
  commit -m 'test(aros): add cache Pareto evaluation rungs'
```

Expected: all commands exit 0.

### Task 6: Calibrate and freeze baseline hard constraints

**Files:**
- Create: `commissioning/cache_campaign/calibrate.py`
- Create: `scripts/calibrate_aros_cache_baselines.py`
- Test: `tests/test_aros_cache_calibration.py`

- [ ] **Step 1: Write failing calibration tests**

Feed one valid baseline R0 receipt for each of the six comparison policies,
five complete R2 repetitions for each constraint baseline (`Sieve`,
`S3FIFO`), plus one complete R2 receipt for `LRU`, `ARC`, `WTinyLFU`, and the
`BeladySize` oracle. Assert the calibrator refuses any missing/failed policy R0,
fewer than five constraint repetitions, any missing comparison policy/cell,
duplicate receipt hashes, different manifests/source/evaluator, failed
measurements, incomplete trace-size cells, mixed host fingerprints, or an
existing output.

```python
def test_calibration_freezes_both_reference_distributions(tmp_path: Path) -> None:
    frozen = calibrate(
        task_manifest,
        r0_receipts,
        inputs,
        tmp_path / "baseline-calibration.json",
    )
    assert set(frozen["references"]) == {"Sieve", "S3FIFO"}
    cell = frozen["references"]["Sieve"]["dev-meta-kv-a"]["0.01"]
    assert cell["repetitions"] == 5
    assert cell["throughput_median_mqps"] == "20"
    assert cell["throughput_floor_mqps"] == "18"
    assert "object_miss_ratio_values" in cell
    assert "byte_miss_ratio_values" in cell
    assert "cpu_ns_per_request_values" in cell
    assert frozen["references"]["Sieve"]["metadata"] == {
        "bytes_per_object": "1.000000",
        "global_bytes": 4096,
        "measurement_sha256": sieve_r0_metadata_sha256,
        "independent_audit": "pending_independent_review",
    }
    assert set(frozen["comparisons"]) == {
        "LRU", "ARC", "WTinyLFU", "Sieve", "S3FIFO", "BeladySize"
    }
```

The throughput floor is exactly `0.90 * median` for the candidate's declared reference policy and same trace/fraction. Metadata limits are the reference R0 probe's operational `metadata_bytes_per_object` and `global_metadata_bytes`; calibration records the exact probe hash and independent audit remains pending. A later candidate can be mechanically compared to these measured limits, but final compliance must not become true until the independent allocation/source audit is accepted. `comparisons` retains object/byte miss and phase facts for every declared policy so later receipts can report signed gaps, but it does not rank or aggregate them. Assert no scalar campaign score or ranking is created.

- [ ] **Step 2: Run the focused test and verify RED**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q tests/test_aros_cache_calibration.py
```

Expected: import failure for `commissioning.cache_campaign.calibrate`.

- [ ] **Step 3: Implement exact-distribution calibration**

Group by `(policy, trace_id, cache_fraction)`, require five unique measurement receipts, and sort Decimal values before computing the middle value. Preserve all five values for every Pareto dimension. The calibration record contains:

```python
{
    "schema_version": 1,
    "task_manifest_sha256": task_manifest_sha256,
    "source_receipt_sha256": source_receipt_sha256,
    "source_commit": source_commit,
    "binary_sha256": binary_sha256,
    "evaluator_sha256": evaluator_sha256,
    "host_fingerprint": host_fingerprint,
    "repetitions": 5,
    "cache_fractions": ["0.01", "0.05", "0.10"],
    "references": references,
    "comparisons": comparisons,
    "r0_receipt_sha256s": {
        "LRU": lru_r0_sha, "ARC": arc_r0_sha,
        "WTinyLFU": wtinylfu_r0_sha, "Sieve": sieve_r0_sha,
        "S3FIFO": s3fifo_r0_sha, "BeladySize": beladysize_r0_sha,
    },
    "input_receipt_sha256s": sorted(input_hashes),
    "calibration_sha256": digest,
}
```

Expose `compare_constraints(candidate_measurement, candidate_r0, contract,
calibration_path, expected_calibration_sha256, independent_audit)` returning
separate values for throughput, measured object metadata, measured global
metadata, declared-metadata consistency, amortized-complexity audit, capacity,
determinism, sanitizer, and signed object/byte/phase gaps to each comparison
policy. The read-only calibration path must match the externally supplied
digest. Missing or rejected audit is false/unknown, never true.

- [ ] **Step 4: Add the one-way calibration CLI**

The CLI accepts exactly six repeated `--r0-receipt PATH` values, exactly
fourteen repeated `--receipt PATH` values, exactly one `--task-manifest`, and a
new `--output`. It prints the calibration SHA only after the read-only record
is durably published. It has no update or force flag; changing traces, sizes,
baseline repetitions, or host requires a new campaign identity and fresh
output.

- [ ] **Step 5: Verify GREEN and commit**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q \
  tests/test_aros_cache_calibration.py tests/test_aros_cache_evaluator.py
/workspace/Arbor/.venv/bin/ruff check commissioning/cache_campaign/calibrate.py \
  scripts/calibrate_aros_cache_baselines.py tests/test_aros_cache_calibration.py
git diff --check
git add commissioning/cache_campaign/calibrate.py \
  scripts/calibrate_aros_cache_baselines.py tests/test_aros_cache_calibration.py
git -c user.name='AROS Agent' -c user.email='aros-agent@example.invalid' \
  commit -m 'test(aros): freeze cache baseline constraints'
```

Expected: all commands exit 0.

### Task 7: Enforce a host-only, exactly-once temporal R3 seal

**Files:**
- Create: `commissioning/cache_campaign/seal.py`
- Create: `scripts/run_aros_cache_r3.py`
- Test: `tests/test_aros_cache_r3.py`

- [ ] **Step 1: Write failing temporal-seal tests**

Define a later-driver input `frozen-package.json` with exact keys:

```python
{
    "schema_version": 1,
    "project": str(project),
    "frozen_commit": frozen_commit,
    "candidate_commit": candidate_commit,
    "policy": "CandidatePolicy",
    "candidate_diff_sha256": diff_sha256,
    "policy_contract_sha256": contract_sha256,
    "claim_ref": "knowledge/claims/C-0001/claim.md",
    "preregistration_ref": "experiments/confirmation/preregistration.md",
    "review_ref": "reviews/RV-0001/report.md",
    "principal_response_ref": "reviews/RV-0001/principal-response.md",
    "reproduction_ref": "reviews/RV-0001/reproduction.json",
    "r0_receipt_sha256": r0_sha256,
    "r2_receipt_sha256": r2_sha256,
    "calibration_sha256": calibration_sha256,
    "r3_commitment_sha256": commitment,
}
```

Test that all refs are regular Git blobs at `frozen_commit`, the worktree is clean, `HEAD` equals the frozen commit, the candidate diff and contract hashes match, the host manifest hashes to the task commitment, and no R3 path/identity bytes occur in the TaskBrief or repository tree.

```python
def test_r3_consumes_ledger_before_launch_and_cannot_retry(tmp_path: Path) -> None:
    first = run_r3(
        package, host_manifest, calibration, calibration_sha256,
        source_receipt, candidate_r0, checkout, canonical_ledger, output,
        runner=failing_runner,
    )
    assert first["state"] == "process_failed"
    assert canonical_ledger.exists()
    with pytest.raises(SealError, match="already consumed"):
        run_r3(
            package, host_manifest, calibration, calibration_sha256,
            source_receipt, candidate_r0, checkout, canonical_ledger,
            tmp_path / "again",
        )
```

Also reject a ledger/output path inside the Researcher task worktree, a pre-existing ledger, missing Reviewer/Principal blobs, dirty Git, post-freeze commit, source/evaluator/R0/R2/calibration mismatch, or any request for more than one R3 execution.

- [ ] **Step 2: Run the focused test and verify RED**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q tests/test_aros_cache_r3.py
```

Expected: import failure for `commissioning.cache_campaign.seal`.

- [ ] **Step 3: Implement consume-before-run sealing**

`run_r3` first validates all immutable inputs, including the exact candidate R0 receipt and binary, then creates the ledger with `os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)`, writes and `fsync`s a `state="consumed"` record containing all input hashes and request time, and only then invokes `evaluate_portfolio("r3", ...)` with the host manifest. R3 selects every sealed R3 entry and the same three frozen fractions. Any failure writes a separate final receipt; the consumed ledger is never deleted or reset.

The successful factual receipt contains the same Pareto vector and hard-constraint comparisons as R2 plus `frozen_commit`, ledger hash, R3 commitment, start/end time, and `state="measured"`. It contains no recommendation and cannot update the Claim, policy, calibration, preregistration, or package.

- [ ] **Step 4: Add a visibly host-only CLI**

Require all arguments explicitly:

```text
--frozen-package PATH --host-r3-manifest PATH --calibration PATH
--calibration-sha256 SHA256 --source-receipt PATH
--candidate-r0-receipt PATH --checkout PATH
--ledger NEW_PATH --output NEW_DIRECTORY
```

At startup, require the external calibration digest to match both the frozen
package and read-only calibration file. Derive the fixed authority ID from the
frozen commit, R3 commitment, candidate commit, and policy, and accept only the
canonical per-UID ledger path beneath the passwd-home authority directory;
moving the package or changing `HOME`/XDG variables cannot create a fresh
authority. Reject when the host manifest, ledger, or output resolves beneath
the project path from the frozen package. Do not expose R3 through
`run_aros_cache_eval.py`.

- [ ] **Step 5: Verify GREEN and commit**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q tests/test_aros_cache_r3.py
/workspace/Arbor/.venv/bin/ruff check commissioning/cache_campaign/seal.py \
  scripts/run_aros_cache_r3.py tests/test_aros_cache_r3.py
git diff --check
git add commissioning/cache_campaign/seal.py scripts/run_aros_cache_r3.py \
  tests/test_aros_cache_r3.py
git -c user.name='AROS Agent' -c user.email='aros-agent@example.invalid' \
  commit -m 'test(aros): seal temporal cache transfer'
```

Expected: all commands exit 0.

### Task 8: Add a standalone substrate verifier and retained evidence

**Files:**
- Create: `scripts/verify_aros_cache_substrate.py`
- Create: `tests/test_aros_cache_substrate_verifier.py`
- Create: `commissioning/cache_campaign/README.md`
- Test: `tests/test_aros_architecture_boundary.py`

- [ ] **Step 1: Write failing independent-verifier tamper tests**

Load the script with `importlib.util`, inspect its AST, and reject imports from `commissioning.cache_campaign`, `arbor.aros`, or `src`. Build a complete small retained fixture, verify it, then mutate one item at a time:

```python
@pytest.mark.parametrize("target", [
    "source_receipt", "binary", "task_manifest", "trace_bytes",
    "r3_commitment", "measurement", "raw_stdout", "calibration",
    "frozen_commit", "candidate_diff", "ledger", "r3_receipt",
])
def test_verifier_rejects_each_broken_binding(tmp_path: Path, target: str) -> None:
    evidence = valid_retained_substrate(tmp_path)
    mutate(evidence, target)
    with pytest.raises(VerificationError):
        verify(evidence / "index.json")
```

Test that verification reports independent fields `source`, `data_boundary`, `r0`, `r1`, `r2`, `calibration`, and optional `r3`; it must not emit one overall scientific pass. Add or extend the architecture-boundary test to assert no cache-campaign commit path begins with `src/aros/`.

- [ ] **Step 2: Run the focused test and verify RED**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q \
  tests/test_aros_cache_substrate_verifier.py tests/test_aros_architecture_boundary.py
```

Expected: failure because the verifier does not exist.

- [ ] **Step 3: Implement independent recomputation**

The verifier uses only `argparse`, `hashlib`, `json`, `os`, `subprocess`, `sys`, and `pathlib`. Duplicate only the small security boundary: canonical JSON hash, streaming file hash, exact-key/type checks, Git blob/diff commands, path containment, and ledger exclusivity facts. It must:

- recompute every self-hash and referenced hash;
- rerun pinned-base tree/remote checks, candidate ancestry/HEAD/clean checks, and the R0-bound candidate binary hash;
- rehash dev/visible bytes and verify the private R3 manifest only when host evidence is supplied;
- recompute non-overlap and split-identity constraints;
- parse raw stdout independently and match every Pareto field;
- recompute five-repeat medians and the 90% throughput floors;
- verify candidate scope and frozen-package Git blobs;
- verify the R3 ledger predates R3 raw outputs and occurs after the frozen commit/package;
- ensure no R3 identity/path bytes occur in task-visible files;
- return separate factual states and unresolved audit fields.

The CLI exits 0 for structurally verified evidence, 2 for invalid evidence, and prints canonical JSON.

- [ ] **Step 4: Document exact operator flow and limits**

`commissioning/cache_campaign/README.md` must include these commands with concrete argument roles:

```bash
python scripts/prepare_aros_cache_source.py --checkout "$LIBCACHESIM" --receipt "$HOST/source.json"
python scripts/freeze_aros_cache_manifests.py --input "$HOST/candidate-input.json" --task-root "$TASK" --task-output "$TASK/manifests" --host-output "$HOST/sealed"
python scripts/run_aros_cache_eval.py --rung r0 --checkout "$LIBCACHESIM" --candidate "$BASE" --base "$BASE" --policy Sieve --source-receipt "$HOST/source.json" --output "$HOST/r0-sieve"
python scripts/run_aros_cache_eval.py --rung r2 --task-root "$TASK" --checkout "$LIBCACHESIM" --candidate "$BASE" --policy Sieve --task-manifest "$TASK/manifests/task.json" --source-receipt "$HOST/source.json" --r0-receipt "$HOST/r0-sieve/receipt.json" --output "$HOST/r2-sieve-1"
python scripts/calibrate_aros_cache_baselines.py --task-manifest "$TASK/manifests/task.json" --r0-receipt "$HOST/r0-lru/receipt.json" --r0-receipt "$HOST/r0-arc/receipt.json" --r0-receipt "$HOST/r0-wtinylfu/receipt.json" --r0-receipt "$HOST/r0-sieve/receipt.json" --r0-receipt "$HOST/r0-s3fifo/receipt.json" --r0-receipt "$HOST/r0-beladysize/receipt.json" --receipt "$HOST/r2-sieve-1/receipt.json" --receipt "$HOST/r2-sieve-2/receipt.json" --receipt "$HOST/r2-sieve-3/receipt.json" --receipt "$HOST/r2-sieve-4/receipt.json" --receipt "$HOST/r2-sieve-5/receipt.json" --receipt "$HOST/r2-s3fifo-1/receipt.json" --receipt "$HOST/r2-s3fifo-2/receipt.json" --receipt "$HOST/r2-s3fifo-3/receipt.json" --receipt "$HOST/r2-s3fifo-4/receipt.json" --receipt "$HOST/r2-s3fifo-5/receipt.json" --receipt "$HOST/r2-lru/receipt.json" --receipt "$HOST/r2-arc/receipt.json" --receipt "$HOST/r2-wtinylfu/receipt.json" --receipt "$HOST/r2-beladysize/receipt.json" --output "$HOST/baseline-calibration.json"
python scripts/run_aros_cache_r3.py --frozen-package "$HOST/frozen-package.json" --host-r3-manifest "$HOST/sealed/r3.json" --calibration "$HOST/baseline-calibration.json" --calibration-sha256 "$CALIBRATION_SHA256" --source-receipt "$HOST/source.json" --candidate-r0-receipt "$HOST/candidate-r0/receipt.json" --checkout "$LIBCACHESIM" --ledger "$PASSWD_HOME/.local/state/aros/cache-campaign-r3/r3-$AUTHORITY_ID.consumed.json" --output "$HOST/r3-result"
python scripts/verify_aros_cache_substrate.py "$HOST/retained/index.json"
```

Explain that calibration requires one R0 receipt for each of all six policies,
five R2 receipts for each constraint baseline, and one R2 receipt for every
other comparison policy; include this exact expanded form:

```bash
python scripts/calibrate_aros_cache_baselines.py \
  --task-manifest "$TASK/manifests/task.json" \
  --r0-receipt "$HOST/r0-lru/receipt.json" \
  --r0-receipt "$HOST/r0-arc/receipt.json" \
  --r0-receipt "$HOST/r0-wtinylfu/receipt.json" \
  --r0-receipt "$HOST/r0-sieve/receipt.json" \
  --r0-receipt "$HOST/r0-s3fifo/receipt.json" \
  --r0-receipt "$HOST/r0-beladysize/receipt.json" \
  --receipt "$HOST/r2-sieve-1/receipt.json" --receipt "$HOST/r2-sieve-2/receipt.json" \
  --receipt "$HOST/r2-sieve-3/receipt.json" --receipt "$HOST/r2-sieve-4/receipt.json" \
  --receipt "$HOST/r2-sieve-5/receipt.json" --receipt "$HOST/r2-s3fifo-1/receipt.json" \
  --receipt "$HOST/r2-s3fifo-2/receipt.json" --receipt "$HOST/r2-s3fifo-3/receipt.json" \
  --receipt "$HOST/r2-s3fifo-4/receipt.json" --receipt "$HOST/r2-s3fifo-5/receipt.json" \
  --receipt "$HOST/r2-lru/receipt.json" --receipt "$HOST/r2-arc/receipt.json" \
  --receipt "$HOST/r2-wtinylfu/receipt.json" --receipt "$HOST/r2-beladysize/receipt.json" \
  --output "$HOST/baseline-calibration.json"
```

State explicitly that source/data provisioning is host work, data never enters Git, operational metadata is measured but coverage plus amortized O(1) remain pending until independent code audit, R3 never retries, and this substrate neither demonstrates Researcher capability nor proves scientific quality.

- [ ] **Step 5: Verify GREEN and commit**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q \
  tests/test_aros_cache_source.py tests/test_aros_cache_manifests.py \
  tests/test_aros_cache_parser.py tests/test_aros_cache_r0.py \
  tests/test_aros_cache_evaluator.py tests/test_aros_cache_calibration.py \
  tests/test_aros_cache_r3.py tests/test_aros_cache_substrate_verifier.py \
  tests/test_aros_architecture_boundary.py
/workspace/Arbor/.venv/bin/ruff check commissioning/cache_campaign \
  scripts/prepare_aros_cache_source.py scripts/freeze_aros_cache_manifests.py \
  scripts/run_aros_cache_eval.py scripts/calibrate_aros_cache_baselines.py \
  scripts/run_aros_cache_r3.py scripts/verify_aros_cache_substrate.py \
  tests/test_aros_cache_*.py
git diff --check
git add commissioning/cache_campaign/README.md scripts/verify_aros_cache_substrate.py \
  tests/test_aros_cache_substrate_verifier.py tests/test_aros_architecture_boundary.py
git -c user.name='AROS Agent' -c user.email='aros-agent@example.invalid' \
  commit -m 'test(aros): verify cache campaign substrate'
```

Expected: all commands exit 0.

### Task 9: Run real baseline calibration, retain the substrate package, and register its status

**Files:**
- Create after a real run: `docs/analysis/aros-cache-campaign-substrate.md`
- Modify: `docs/document_registry.json`
- Test: `tests/test_document_registry.py`

- [ ] **Step 1: Provision only pre-approved host inputs**

The human/host supplies an already cloned pinned source checkout and candidate trace input outside the repository. The candidate input must identify at least the valid portfolio shape from Task 2, bind exact local files, and contain no randomly sampled request set. Run source preparation and manifest freezing once. Do not download traces from an Agent session and do not commit trace bytes or private R3 records.

- [ ] **Step 2: Calibrate the exact baseline apparatus**

Run R0 once for each of LRU, ARC, WTinyLFU, Sieve, S3FIFO, and BeladySize,
including allocation-accounting probes. Run R1 once for a timing sanity check,
R2 five times per constraint baseline, and R2 once each for LRU, ARC,
WTinyLFU, and BeladySize on the same idle host fingerprint. Freeze the
calibration and run the standalone verifier without R3. If host noise or
process failure invalidates a repetition, preserve that failed receipt and
start a new campaign calibration identity rather than replacing it.

- [ ] **Step 3: Write the factual retained result**

The analysis document must record exact source commit/tree/binary hash, task manifest and R3 commitment hashes, split counts and identities at the non-secret organization/application level, cache fractions, R0 check states, all baseline Pareto distributions, throughput floors, metadata budgets/audit status, commands, host fingerprint, retained package path/hash, verifier output, and remaining limitations. It must say R3 is unrun and sealed, no Researcher/Reviewer was tested, and no aggregate campaign pass exists.

- [ ] **Step 4: Register only the informative result**

Add exactly:

```json
{
  "id": "aros-cache-campaign-substrate",
  "title": "AROS Cache Campaign Substrate and Baseline Evidence",
  "path": "docs/analysis/aros-cache-campaign-substrate.md",
  "status": "current",
  "authority": "informative",
  "agent_visibility": "on_demand"
}
```

Do not change the approved design's authority or mark the substrate as product behavior.

- [ ] **Step 5: Run final gates**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q tests/test_document_registry.py tests/test_aros_cache_*.py
/workspace/Arbor/.venv/bin/python -m pytest -q
/workspace/Arbor/.venv/bin/ruff check commissioning/cache_campaign scripts/prepare_aros_cache_source.py \
  scripts/freeze_aros_cache_manifests.py scripts/run_aros_cache_eval.py \
  scripts/calibrate_aros_cache_baselines.py scripts/run_aros_cache_r3.py \
  scripts/verify_aros_cache_substrate.py tests/test_aros_cache_*.py
python scripts/verify_aros_cache_substrate.py "$HOST/retained/index.json"
git diff --check
git status --short
```

Expected: all tests and verification exit 0; status lists only the intended analysis/registry changes before commit; no path under `src/aros` changed.

- [ ] **Step 6: Commit the retained evidence pointer**

```bash
git add docs/analysis/aros-cache-campaign-substrate.md docs/document_registry.json
git -c user.name='AROS Agent' -c user.email='aros-agent@example.invalid' \
  commit -m 'docs(aros): record cache substrate calibration'
git status --short --branch
```

Expected: commit succeeds and the worktree is clean.

## Plan self-review checklist

- The plan covers approved delivery parts 1-2: pinned source, external trace binding, R0-R3 factual apparatus, baseline calibration, independent verification, and retained evidence.
- It deliberately defers real Researcher memory/restart, independent scientific Reviewer, Principal campaign driver, complete capability-gate verification, clean-wheel campaign, and Claim-package result to later plans.
- R3 is host-only and exactly once; dev/visible remain Researcher-visible; no additional experimental trace download is implemented.
- Metrics remain a Pareto vector and hard constraints; no scalar score or scientific interpretation appears in the apparatus.
- Metadata and amortized complexity are not falsely mechanized: declared values are recorded, but compliance remains unresolved until independent source audit.
- All implementation paths are outside `src/aros`, and every task has a RED command, minimal implementation boundary, GREEN command, and commit point.
