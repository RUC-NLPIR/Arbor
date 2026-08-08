from __future__ import annotations

import copy
import hashlib
import json
import subprocess
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from commissioning.cache_campaign.cachesim import ChildResult
from commissioning.cache_campaign.evaluate import (
    EvaluationError,
    evaluate_r0,
    generate_synthetic_trace,
    parse_capacity_probe,
    parse_metadata_probe,
)
from commissioning.cache_campaign.records import (
    ContractError,
    load_object,
    record_sha256,
    sha256_file,
)
from commissioning.cache_campaign.scope import (
    ConstraintFacts,
    PolicyContract,
    ScopeFacts,
    evaluate_scope,
    load_policy_contract,
)
from scripts import run_aros_cache_eval as eval_cli


POLICY = "CandidatePolicy"
SOURCE = f"libCacheSim/cache/eviction/{POLICY}.c"
CONTRACT_PATH = "commissioning/cache_policy_contract.json"
WIRING = {
    "libCacheSim/include/libCacheSim/evictionAlgo.h": (
        "cache_t *CandidatePolicy_init(const common_cache_params_t params, "
        "const char *specific);\n"
    ),
    "libCacheSim/cache/CMakeLists.txt": "  eviction/CandidatePolicy.c\n",
    "libCacheSim/bin/cachesim/cache_init.h": (
        '  {"CandidatePolicy", CandidatePolicy_init},\n'
    ),
    "test/CMakeLists.txt": (
        "add_test_executable(test_CandidatePolicy test_CandidatePolicy.c)\n"
        "add_test(NAME test_CandidatePolicy COMMAND test_CandidatePolicy "
        "WORKING_DIRECTORY .)\n"
    ),
}
POLICY_CONTRACT = {
    "schema_version": 1,
    "policy": POLICY,
    "reference_policy": "Sieve",
    "policy_source": SOURCE,
    "object_metadata_bytes": 1,
    "global_metadata_bytes": 24,
    "global_metadata_evidence": [
        {
            "source": SOURCE,
            "line": 10,
            "expression": "sizeof(CandidatePolicy_params_t)",
        }
    ],
    "update_complexity": "amortized O(1)",
}


def git(checkout: Path, *argv: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(checkout), *argv],
        capture_output=True,
        check=check,
        text=True,
    )
    return result.stdout.strip()


def write(checkout: Path, relative: str, content: str) -> None:
    path = checkout / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def repository(tmp_path: Path) -> tuple[Path, str, str, dict[str, object]]:
    checkout = tmp_path / "libCacheSim"
    checkout.mkdir()
    git(checkout, "init", "-q")
    git(checkout, "config", "user.name", "R0 Test")
    git(checkout, "config", "user.email", "r0@example.invalid")
    git(checkout, "remote", "add", "origin", "https://example.invalid/libCacheSim.git")
    write(checkout, ".gitignore", "_build-*\n")
    write(checkout, "libCacheSim/cache/eviction/Sieve.c", "/* sieve */\n")
    write(checkout, "libCacheSim/cache/eviction/S3FIFO.c", "/* s3fifo */\n")
    write(checkout, "libCacheSim/bin/cachesim/main.c", "/* simulator */\n")
    write(checkout, "libCacheSim/traceReader/generalReader.c", "/* reader */\n")
    for relative in WIRING:
        write(checkout, relative, "# baseline wiring\n")
    git(checkout, "add", ".")
    git(checkout, "commit", "-qm", "base")
    base = git(checkout, "rev-parse", "HEAD")
    base_tree = git(checkout, "rev-parse", "HEAD^{tree}")

    write(checkout, SOURCE, "/* candidate */\n")
    write(checkout, f"test/test_{POLICY}.c", "/* candidate test */\n")
    write(
        checkout,
        CONTRACT_PATH,
        json.dumps(POLICY_CONTRACT, sort_keys=True) + "\n",
    )
    for relative, addition in WIRING.items():
        with (checkout / relative).open("a", encoding="utf-8") as stream:
            stream.write(addition)
    git(checkout, "add", ".")
    git(checkout, "commit", "-qm", "candidate")
    candidate = git(checkout, "rev-parse", "HEAD")
    lock = {
        "schema_version": 1,
        "repository_url": "https://example.invalid/libCacheSim.git",
        "commit": base,
        "tree": base_tree,
        "configure_argv": [
            "cmake",
            "-S",
            ".",
            "-B",
            "_build",
            "-G",
            "Ninja",
            "-DCMAKE_BUILD_TYPE=Release",
            "-DENABLE_TESTS=ON",
        ],
        "build_argv": ["cmake", "--build", "_build", "-j", "8"],
        "test_argv": ["ctest", "--test-dir", "_build", "--output-on-failure"],
        "binary": "_build/bin/cachesim",
        "baseline_policies": ["Sieve", "S3FIFO"],
        "comparison_policies": ["Sieve", "S3FIFO"],
    }
    return checkout, base, candidate, lock


def source_receipt(path: Path, lock: dict[str, object]) -> Path:
    receipt: dict[str, object] = {
        "schema_version": 1,
        "repository_url": lock["repository_url"],
        "commit": lock["commit"],
        "tree": lock["tree"],
        "clean": True,
        "commands": [
            {
                "argv": copy.deepcopy(lock[key]),
                "returncode": 0,
                "stdout_sha256": "2" * 64,
                "stderr_sha256": "3" * 64,
            }
            for key in ("configure_argv", "build_argv", "test_argv")
        ],
        "versions": {"cmake": "test", "ninja": "test"},
        "compilers": {
            "c": {"path": "/usr/bin/cc", "version": "test cc"},
            "cxx": {"path": "/usr/bin/c++", "version": "test c++"},
        },
        "interpreter": "test",
        "platform": "test",
        "binary": lock["binary"],
        "binary_sha256": "1" * 64,
    }
    receipt["receipt_sha256"] = record_sha256(receipt, "receipt_sha256")
    path.write_text(json.dumps(receipt, sort_keys=True) + "\n", encoding="utf-8")
    return path


def test_policy_contract_is_exact_and_typed(tmp_path: Path) -> None:
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(POLICY_CONTRACT), encoding="utf-8")
    assert load_policy_contract(path, expected_policy=POLICY) == PolicyContract(
        schema_version=1,
        policy=POLICY,
        reference_policy="Sieve",
        policy_source=SOURCE,
        object_metadata_bytes=1,
        global_metadata_bytes=24,
        global_metadata_evidence=(
            (SOURCE, 10, "sizeof(CandidatePolicy_params_t)"),
        ),
        update_complexity="amortized O(1)",
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", True),
        ("policy", "../CandidatePolicy"),
        ("reference_policy", "LRU"),
        ("policy_source", "libCacheSim/cache/eviction/Other.c"),
        ("object_metadata_bytes", True),
        ("global_metadata_bytes", -1),
        ("global_metadata_evidence", []),
        ("update_complexity", "O(1)"),
    ],
)
def test_policy_contract_rejects_malformed_fields(
    tmp_path: Path, field: str, value: object
) -> None:
    candidate = copy.deepcopy(POLICY_CONTRACT)
    candidate[field] = value
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(candidate), encoding="utf-8")
    with pytest.raises(ContractError):
        load_policy_contract(path, expected_policy=POLICY)


def test_policy_contract_rejects_duplicates_and_nonfinite(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
    with pytest.raises(ContractError, match="duplicate"):
        load_policy_contract(duplicate, expected_policy=POLICY)
    nonfinite = tmp_path / "nonfinite.json"
    nonfinite.write_text('{"schema_version":NaN}', encoding="utf-8")
    with pytest.raises(ContractError, match="non-finite"):
        load_policy_contract(nonfinite, expected_policy=POLICY)


def test_scope_accepts_only_candidate_additions(tmp_path: Path) -> None:
    checkout, base, candidate, _lock = repository(tmp_path)
    facts, contract = evaluate_scope(
        checkout, base=base, candidate=candidate, policy=POLICY
    )
    assert facts == ScopeFacts(
        allowed_paths=True,
        baseline_unchanged=True,
        additive_wiring_only=True,
        contract_bound=True,
        changed_paths=tuple(
            sorted({SOURCE, f"test/test_{POLICY}.c", CONTRACT_PATH, *WIRING})
        ),
        diff_sha256=facts.diff_sha256,
    )
    assert len(facts.diff_sha256) == 64
    assert contract is not None and contract.policy == POLICY


@pytest.mark.parametrize(
    ("relative", "content"),
    [
        ("libCacheSim/bin/cachesim/main.c", "changed simulator\n"),
        ("libCacheSim/traceReader/generalReader.c", "changed reader\n"),
        ("libCacheSim/cache/eviction/Sieve.c", "changed baseline\n"),
        ("libCacheSim/cache/eviction/SecondPolicy.c", "second policy\n"),
    ],
)
def test_scope_rejects_every_disallowed_path(
    tmp_path: Path, relative: str, content: str
) -> None:
    checkout, base, _candidate, _lock = repository(tmp_path)
    write(checkout, relative, content)
    git(checkout, "add", relative)
    git(checkout, "commit", "-qm", "disallowed")
    candidate = git(checkout, "rev-parse", "HEAD")
    facts, _contract = evaluate_scope(
        checkout, base=base, candidate=candidate, policy=POLICY
    )
    assert facts.allowed_paths is False
    assert facts.baseline_unchanged is False


def test_scope_rejects_delete_rename_and_nonadditive_wiring(tmp_path: Path) -> None:
    checkout, base, _candidate, _lock = repository(tmp_path)
    (checkout / "libCacheSim/cache/eviction/Sieve.c").unlink()
    header = checkout / "libCacheSim/include/libCacheSim/evictionAlgo.h"
    header.write_text(header.read_text().replace("# baseline wiring\n", ""))
    with (checkout / "test/CMakeLists.txt").open("a", encoding="utf-8") as stream:
        stream.write("message(STATUS not_needed_CandidatePolicy)\n")
    git(checkout, "add", "-A")
    git(checkout, "commit", "-qm", "unsafe diff")
    candidate = git(checkout, "rev-parse", "HEAD")
    facts, _contract = evaluate_scope(
        checkout, base=base, candidate=candidate, policy=POLICY
    )
    assert facts.allowed_paths is False
    assert facts.baseline_unchanged is False
    assert facts.additive_wiring_only is False


def test_scope_rejects_policy_named_but_unnecessary_wiring(tmp_path: Path) -> None:
    checkout, base, _candidate, _lock = repository(tmp_path)
    header = checkout / "libCacheSim/include/libCacheSim/evictionAlgo.h"
    with header.open("a", encoding="utf-8") as stream:
        stream.write("void exploit_CandidatePolicy_init(void);\n")
    git(checkout, "add", str(header.relative_to(checkout)))
    git(checkout, "commit", "-qm", "unnecessary wiring")
    candidate = git(checkout, "rev-parse", "HEAD")
    facts, _contract = evaluate_scope(
        checkout, base=base, candidate=candidate, policy=POLICY
    )
    assert facts.additive_wiring_only is False


def test_scope_rejects_custom_cmake_command_naming_policy(tmp_path: Path) -> None:
    checkout, base, _candidate, _lock = repository(tmp_path)
    cmake = checkout / "test/CMakeLists.txt"
    with cmake.open("a", encoding="utf-8") as stream:
        stream.write("add_custom_target(test_CandidatePolicy ALL COMMAND touch bad)\n")
    git(checkout, "add", str(cmake.relative_to(checkout)))
    git(checkout, "commit", "-qm", "custom target")
    candidate = git(checkout, "rev-parse", "HEAD")
    facts, _contract = evaluate_scope(
        checkout, base=base, candidate=candidate, policy=POLICY
    )
    assert facts.additive_wiring_only is False


def test_scope_requires_every_candidate_wiring_file(tmp_path: Path) -> None:
    checkout, base, _candidate, _lock = repository(tmp_path)
    write(checkout, "test/CMakeLists.txt", "# baseline wiring\n")
    git(checkout, "add", "test/CMakeLists.txt")
    git(checkout, "commit", "-qm", "remove candidate test registration")
    candidate = git(checkout, "rev-parse", "HEAD")
    facts, _contract = evaluate_scope(
        checkout, base=base, candidate=candidate, policy=POLICY
    )
    assert facts.allowed_paths is False
    assert facts.additive_wiring_only is False


def test_scope_contract_mismatch_is_a_separate_fact(tmp_path: Path) -> None:
    checkout, base, _candidate, _lock = repository(tmp_path)
    contract = copy.deepcopy(POLICY_CONTRACT)
    contract["global_metadata_evidence"][0]["source"] = (
        "libCacheSim/cache/eviction/Sieve.c"
    )
    write(checkout, CONTRACT_PATH, json.dumps(contract))
    git(checkout, "add", CONTRACT_PATH)
    git(checkout, "commit", "-qm", "bad contract")
    candidate = git(checkout, "rev-parse", "HEAD")
    facts, declared = evaluate_scope(
        checkout, base=base, candidate=candidate, policy=POLICY
    )
    assert facts.contract_bound is False
    assert declared is None


def test_scope_rejects_symlink_candidate_artifact(tmp_path: Path) -> None:
    checkout, base, _candidate, _lock = repository(tmp_path)
    policy_source = checkout / SOURCE
    policy_source.unlink()
    policy_source.symlink_to("Sieve.c")
    git(checkout, "add", SOURCE)
    git(checkout, "commit", "-qm", "symlink candidate source")
    candidate = git(checkout, "rev-parse", "HEAD")
    facts, _contract = evaluate_scope(
        checkout, base=base, candidate=candidate, policy=POLICY
    )
    assert facts.allowed_paths is False


@pytest.mark.parametrize("policy", ["Sieve", "S3FIFO"])
def test_scope_baseline_mode(policy: str, tmp_path: Path) -> None:
    checkout, base, _candidate, _lock = repository(tmp_path)
    facts, contract = evaluate_scope(checkout, base=base, candidate=base, policy=policy)
    assert facts.changed_paths == ()
    assert facts.contract_bound is None
    assert facts.allowed_paths is True
    assert facts.baseline_unchanged is True
    assert facts.additive_wiring_only is True
    assert contract is None


def test_scope_rejects_nonbaseline_unchanged_policy(tmp_path: Path) -> None:
    checkout, base, _candidate, _lock = repository(tmp_path)
    with pytest.raises(ContractError, match="baseline"):
        evaluate_scope(checkout, base=base, candidate=base, policy=POLICY)


def test_constraint_facts_do_not_invent_budget_or_complexity() -> None:
    facts = ConstraintFacts(
        measured_metadata_bytes_per_object=Decimal("1.25"),
        measured_global_metadata_bytes=24,
        metadata_measurement_sha256="a" * 64,
        metadata_within_budget=None,
        complexity_audit="pending_independent_review",
        capacity_conserved=True,
        deterministic=True,
        sanitizer_clean=True,
    )
    assert facts.metadata_within_budget is None
    assert facts.complexity_audit == "pending_independent_review"


def test_synthetic_trace_is_deterministic_and_one_based(tmp_path: Path) -> None:
    first = tmp_path / "first.bin"
    second = tmp_path / "second.bin"
    left = generate_synthetic_trace(first)
    right = generate_synthetic_trace(second)
    assert first.read_bytes() == second.read_bytes()
    assert left == right
    assert left["request_count"] == 10_000
    assert left["sha256"] == sha256_file(first)
    records = list(__import__("struct").iter_unpack("<IQIq", first.read_bytes()))
    future: dict[int, list[int]] = {}
    for index, (_timestamp, object_id, _size, _next) in enumerate(records, start=1):
        future.setdefault(object_id, []).append(index)
    for index, (_timestamp, object_id, _size, next_access) in enumerate(records, start=1):
        later = [value for value in future[object_id] if value > index]
        assert next_access == (later[0] if later else -1)


def test_probe_parsers_enforce_capacity_and_decimal_accounting() -> None:
    capacity = parse_capacity_probe(
        "capacity_conserved=1\nrequests=10000\n"
        "max_occupied_bytes=4096\ncache_size_bytes=4096\n"
    )
    assert capacity["capacity_conserved"] is True
    with pytest.raises(EvaluationError):
        parse_capacity_probe(
            "capacity_conserved=0\nrequests=17\n"
            "max_occupied_bytes=4097\ncache_size_bytes=4096\n"
        )

    metadata = parse_metadata_probe(
        "global_metadata_bytes=24\n"
        "sample=1000 live_bytes=1024 resident_objects=1000\n"
        "sample=5000 live_bytes=10024 resident_objects=5000\n"
        "sample=10000 live_bytes=30024 resident_objects=10000\n"
        "status=ok\n"
    )
    assert metadata == (Decimal("3"), 24)


@pytest.mark.parametrize(
    "output",
    [
        "global_metadata_bytes=24\nstatus=overflow\n",
        "global_metadata_bytes=24\n"
        "sample=1000 live_bytes=23 resident_objects=1000\nstatus=ok\n",
        "global_metadata_bytes=24\n"
        "sample=1000 live_bytes=24 resident_objects=0\nstatus=ok\n",
        "global_metadata_bytes=24\n"
        "sample=1000 live_bytes=24 resident_objects=1000\n"
        "sample=5000 live_bytes=24 resident_objects=5000\n"
        "sample=10000 live_bytes=24 resident_objects=10000\nextra=bad\nstatus=ok\n",
    ],
)
def test_metadata_probe_rejects_accounting_failures(output: str) -> None:
    with pytest.raises(EvaluationError):
        parse_metadata_probe(output)


class FakeRun:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.cwds: list[Path] = []
        self.simulation_count = 0
        self.request_count = 9_999
        self.candidate_test_registered = True
        self.sanitizer_text = b""
        self.mismatch = False
        self.capacity_output = (
            b"capacity_conserved=1\nrequests=10000\n"
            b"max_occupied_bytes=8192\ncache_size_bytes=8192\n"
        )
        self.metadata_output = (
            b"global_metadata_bytes=24\n"
            b"sample=1000 live_bytes=1024 resident_objects=1000\n"
            b"sample=5000 live_bytes=10024 resident_objects=5000\n"
            b"sample=10000 live_bytes=30024 resident_objects=10000\n"
            b"status=ok\n"
        )

    def __call__(
        self, argv: list[str], output_dir: Path, *, cwd: Path | None = None
    ) -> ChildResult:
        command = list(argv)
        self.commands.append(command)
        checkout = Path(cwd) if cwd is not None else None
        assert checkout is not None
        self.cwds.append(checkout)
        stdout = b""
        stderr = b""
        if command[:4] == ["cmake", "-S", ".", "-B"]:
            build = checkout / command[4]
            build.mkdir()
            (build / "CMakeCache.txt").write_text(
                "CMAKE_C_COMPILER:FILEPATH=/usr/bin/cc\n"
                "CMAKE_CXX_COMPILER:FILEPATH=/usr/bin/c++\n"
                "GLib_INCLUDE_DIRS:INTERNAL=/usr/include/glib-2.0;"
                "/usr/lib/test/glib-2.0/include;/usr/include\n"
                "GLib_LIBRARIES:INTERNAL=glib-2.0\n"
                "OPT_SUPPORT_ZSTD_TRACE:BOOL=ON\n"
                "ZSTD_LIBRARY_RELEASE:FILEPATH=/usr/lib/test/libzstd.so\n"
                "Tcmalloc_LIBRARY:FILEPATH=/usr/lib/test/libtcmalloc_minimal.so\n"
            )
        elif command[:3] == ["cmake", "--build", "_build-release"]:
            binary = checkout / "_build-release/bin/cachesim"
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b"release candidate")
            binary.chmod(0o755)
        elif command[:3] == ["cmake", "--build", "_build-sanitize"]:
            binary = checkout / "_build-sanitize/bin/cachesim"
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b"sanitize candidate")
            binary.chmod(0o755)
        elif command[0] == "/usr/bin/cc":
            binary = Path(command[command.index("-o") + 1])
            binary.write_bytes(b"probe")
            binary.chmod(0o755)
        elif command[:3] == ["ctest", "--test-dir", "_build-release"] and "-R" in command:
            if self.candidate_test_registered:
                stdout = (
                    b"Test project /build\n    Start 1: test_CandidatePolicy\n"
                    b"1/1 Test #1: test_CandidatePolicy ... Passed\n"
                    b"100% tests passed, 0 tests failed out of 1\n"
                )
            else:
                stdout = b"Test project /build\nNo tests were found!!!\n"
        elif command[0].endswith("capacity-probe"):
            stdout = self.capacity_output
        elif command[0].endswith("metadata-probe"):
            stdout = self.metadata_output
        elif command[0].endswith("cachesim"):
            self.simulation_count += 1
            ratio = "0.3000" if self.mismatch and self.simulation_count == 2 else "0.2000"
            throughput = "2.0" if self.simulation_count == 1 else "9.0"
            stdout = (
                f"{command[1]} {command[3]} cache size  16.00KiB, "
                f"          {self.request_count} req, miss ratio {ratio}, "
                f"byte miss ratio 0.2500, throughput {throughput} MQPS\n"
            ).encode()
            output_argument = next(
                item for item in command if item.startswith("--output=")
            )
            with Path(output_argument.split("=", 1)[1]).open("ab") as stream:
                stream.write(stdout)
        if command[:3] == ["ctest", "--test-dir", "_build-sanitize"]:
            stderr = self.sanitizer_text
        output_dir.mkdir()
        stdout_path = output_dir / "stdout.raw"
        stderr_path = output_dir / "stderr.raw"
        stdout_path.write_bytes(stdout)
        stderr_path.write_bytes(stderr)
        return ChildResult(
            argv=tuple(command),
            returncode=0,
            wall_ns=10,
            cpu_ns=5,
            stdout_path=stdout_path,
            stdout_bytes=len(stdout),
            stdout_sha256=hashlib.sha256(stdout).hexdigest(),
            stderr_path=stderr_path,
            stderr_bytes=len(stderr),
            stderr_sha256=hashlib.sha256(stderr).hexdigest(),
        )


def evaluated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run: FakeRun | None = None,
    *,
    baseline: bool = False,
) -> tuple[dict[str, object], FakeRun, Path, str, str]:
    checkout, base, candidate, lock = repository(tmp_path)
    monkeypatch.setattr("commissioning.cache_campaign.evaluate.SOURCE_LOCK", lock)
    receipt_path = source_receipt(tmp_path / "source-receipt.json", lock)
    runner = run or FakeRun()
    selected_candidate = base if baseline else candidate
    if baseline:
        git(checkout, "checkout", "-q", base)
    output = tmp_path / "r0-output"
    receipt = evaluate_r0(
        checkout=checkout,
        base=base,
        candidate=selected_candidate,
        policy="Sieve" if baseline else POLICY,
        source_receipt=receipt_path,
        output=output,
        run=runner,
    )
    return receipt, runner, checkout, base, selected_candidate


def test_r0_exact_command_order_flags_and_separate_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt, runner, checkout, _base, _candidate = evaluated(tmp_path, monkeypatch)
    names = [command[0] for command in runner.commands]
    assert names[:10] == [
        "cmake",
        "cmake",
        "ctest",
        "ctest",
        "cmake",
        "cmake",
        "ctest",
        str(checkout / "_build-release/bin/cachesim"),
        str(checkout / "_build-release/bin/cachesim"),
        "/usr/bin/cc",
    ]
    assert len(names) == 13
    assert names[10].endswith("/capacity-probe")
    assert names[11] == "/usr/bin/cc"
    assert names[12].endswith("/metadata-probe")
    assert [item["label"] for item in receipt["commands"]] == [
        "release-configure",
        "release-build",
        "release-full-tests",
        "candidate-test",
        "sanitize-configure",
        "sanitize-build",
        "sanitize-full-tests",
        "determinism-run-1",
        "determinism-run-2",
        "capacity-compile",
        "capacity-run",
        "metadata-compile",
        "metadata-run",
    ]
    assert runner.commands[0] == [
        "cmake",
        "-S",
        ".",
        "-B",
        "_build-release",
        "-G",
        "Ninja",
        "-DCMAKE_BUILD_TYPE=Release",
        "-DENABLE_TESTS=ON",
    ]
    assert runner.commands[1][-2:] == ["-j", "8"]
    assert runner.commands[3][-3:] == [
        "-R",
        "^test_CandidatePolicy$",
        "--no-tests=error",
    ]
    assert runner.commands[4] == [
        "cmake",
        "-S",
        ".",
        "-B",
        "_build-sanitize",
        "-G",
        "Ninja",
        "-DCMAKE_BUILD_TYPE=RelWithDebInfo",
        "-DENABLE_TESTS=ON",
        "-DCMAKE_C_FLAGS=-fsanitize=address,undefined -fno-omit-frame-pointer",
        "-DCMAKE_CXX_FLAGS=-fsanitize=address,undefined -fno-omit-frame-pointer",
        "-DCMAKE_EXE_LINKER_FLAGS=-fsanitize=address,undefined",
    ]
    assert runner.commands[7] == runner.commands[8]
    assert runner.cwds[7] == runner.cwds[8]
    assert runner.cwds[7] != checkout
    assert runner.cwds[7].name.startswith(".r0-output-stage-")
    assert "--num-thread=1" in runner.commands[7]
    assert "--consider-obj-metadata=true" in runner.commands[7]
    assert "--print-head-req=false" in runner.commands[7]
    simulation_output = next(
        item for item in runner.commands[7] if item.startswith("--output=")
    )
    assert Path(simulation_output.split("=", 1)[1]).parent == runner.cwds[7]
    capacity_compile = runner.commands[9]
    assert ["-I", "/usr/include/glib-2.0"] == capacity_compile[5:7]
    assert "-lglib-2.0" in capacity_compile
    assert "/usr/lib/test/libzstd.so" in capacity_compile
    assert "/usr/lib/test/libtcmalloc_minimal.so" in capacity_compile
    assert "-lstdc++" in capacity_compile
    assert receipt["checks"] == {
        "source_binding": True,
        "evidence_binding": True,
        "build": True,
        "full_tests": True,
        "candidate_test": True,
        "sanitizer": True,
        "deterministic": True,
        "capacity": True,
        "metadata_probe": True,
    }
    assert receipt["measured_metadata"] == {
        "bytes_per_object": "3",
        "global_bytes": 24,
        "measurement_sha256": receipt["measured_metadata"]["measurement_sha256"],
        "within_budget": None,
    }
    assert receipt["complexity_audit"] == "pending_independent_review"
    assert receipt["simulator_result"]["size_bytes"] > 0
    assert len(receipt["simulator_result"]["sha256"]) == 64
    assert set(receipt["changed_path_sha256"]) == set(
        receipt["scope"]["changed_paths"]
    )
    assert all(len(digest) == 64 for digest in receipt["changed_path_sha256"].values())
    assert not (checkout / "_build-release").exists()
    assert not (checkout / "_build-sanitize").exists()


def test_baseline_skips_contract_and_candidate_ctest_but_runs_probes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt, runner, _checkout, _base, _candidate = evaluated(
        tmp_path, monkeypatch, baseline=True
    )
    assert receipt["scope"]["changed_paths"] == []
    assert receipt["scope"]["contract_bound"] is None
    assert receipt["checks"]["candidate_test"] is None
    assert len(runner.commands) == 12


def test_candidate_ctest_must_run_exact_registered_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRun()
    runner.candidate_test_registered = False
    receipt, _runner, *_rest = evaluated(tmp_path, monkeypatch, runner)
    assert receipt["checks"]["candidate_test"] is False
    assert any("candidate CTest" in error for error in receipt["errors"])


def test_determinism_ignores_throughput_but_detects_scientific_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt, _runner, *_rest = evaluated(tmp_path, monkeypatch)
    assert receipt["checks"]["deterministic"] is True

    other = tmp_path / "other"
    other.mkdir()
    mismatch = FakeRun()
    mismatch.mismatch = True
    failed, _runner, *_rest = evaluated(other, monkeypatch, mismatch)
    assert failed["checks"]["deterministic"] is False


def test_determinism_accepts_pinned_single_cache_first_record_warmup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRun()
    runner.request_count = 9_999
    receipt, _runner, *_rest = evaluated(tmp_path, monkeypatch, runner)
    assert receipt["synthetic_trace"]["request_count"] == 10_000
    assert [item["request_count"] for item in receipt["simulations"]] == [
        9_999,
        9_999,
    ]
    assert receipt["checks"]["deterministic"] is True


def test_determinism_rejects_equal_but_wrong_request_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRun()
    runner.request_count = 1
    receipt, _runner, *_rest = evaluated(tmp_path, monkeypatch, runner)
    assert receipt["checks"]["deterministic"] is False


def test_sanitizer_capacity_and_metadata_fail_independently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRun()
    runner.sanitizer_text = b"ERROR: AddressSanitizer: heap-use-after-free\n"
    runner.capacity_output = (
        b"capacity_conserved=0\nrequests=10\n"
        b"max_occupied_bytes=16385\ncache_size_bytes=16384\n"
    )
    runner.metadata_output = b"global_metadata_bytes=24\nstatus=unknown_free\n"
    receipt, _runner, *_rest = evaluated(tmp_path, monkeypatch, runner)
    assert receipt["checks"]["sanitizer"] is False
    assert receipt["checks"]["capacity"] is False
    assert receipt["checks"]["metadata_probe"] is False
    assert receipt["measured_metadata"] is None


def test_nonzero_command_keeps_raw_process_receipt_and_only_its_fact_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class NonzeroRun(FakeRun):
        def __call__(
            self, argv: list[str], output_dir: Path, *, cwd: Path | None = None
        ) -> ChildResult:
            result = super().__call__(argv, output_dir, cwd=cwd)
            if list(argv[:3]) == ["ctest", "--test-dir", "_build-release"] and len(
                argv
            ) == 4:
                return replace(result, returncode=9)
            return result

    receipt, _runner, *_rest = evaluated(tmp_path, monkeypatch, NonzeroRun())
    assert receipt["checks"]["full_tests"] is False
    process = next(
        item for item in receipt["commands"] if item["label"] == "release-full-tests"
    )
    assert process["returncode"] == 9
    assert len(process["stdout"]["sha256"]) == 64
    assert receipt["checks"]["build"] is True


def test_timeout_is_an_explicit_command_failure_with_retained_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class TimeoutRun(FakeRun):
        thrown = False

        def __call__(
            self, argv: list[str], output_dir: Path, *, cwd: Path | None = None
        ) -> ChildResult:
            if not self.thrown and argv[0].endswith("cachesim"):
                self.thrown = True
                self.commands.append(list(argv))
                raise subprocess.TimeoutExpired(argv, timeout=1)
            return super().__call__(argv, output_dir, cwd=cwd)

    receipt, _runner, *_rest = evaluated(tmp_path, monkeypatch, TimeoutRun())
    assert receipt["checks"]["deterministic"] is False
    process = next(
        item for item in receipt["commands"] if item["label"] == "determinism-run-1"
    )
    assert process["returncode"] is None
    assert "timed out" in process["error"]
    assert (tmp_path / "r0-output/receipt.json").is_file()


def recursive_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {key for item in value.values() for key in recursive_keys(item)}
    if isinstance(value, list):
        return {key for item in value for key in recursive_keys(item)}
    return set()


def test_receipt_is_self_hashed_and_has_no_aggregate_vocabulary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt, _runner, *_rest = evaluated(tmp_path, monkeypatch)
    assert receipt["receipt_sha256"] == record_sha256(receipt, "receipt_sha256")
    assert {"score", "objective", "reward", "aggregate", "pass"}.isdisjoint(
        recursive_keys(receipt)
    )
    published = load_object(tmp_path / "r0-output/receipt.json")
    assert published == receipt


def test_generated_probes_bind_exact_policy_reader_and_allocation_wrappers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt, _runner, *_rest = evaluated(tmp_path, monkeypatch)
    output = tmp_path / "r0-output"
    capacity = (output / receipt["probes"]["capacity"]["source_path"]).read_text()
    metadata = (output / receipt["probes"]["metadata"]["source_path"]).read_text()
    assert f"{POLICY}_init" in capacity
    assert "setup_reader" in capacity
    assert "ORACLE_GENERAL_TRACE" in capacity
    assert "cache->get(cache, request)" in capacity
    assert f"{POLICY}_init" in metadata
    assert "__wrap_malloc" in metadata
    assert "__wrap_calloc" in metadata
    assert "__wrap_realloc" in metadata
    assert "__wrap_free" in metadata
    assert "unknown free" in metadata
    assert "unknown realloc" in metadata
    assert "fixed table overflow" in metadata


def test_untracked_source_mutation_invalidates_facts_but_retains_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class MutatingRun(FakeRun):
        def __call__(
            self, argv: list[str], output_dir: Path, *, cwd: Path | None = None
        ) -> ChildResult:
            result = super().__call__(argv, output_dir, cwd=cwd)
            if len(self.commands) == 2:
                assert cwd is not None
                (cwd / "foreign-untracked.c").write_text("mutation\n")
            return result

    receipt, _runner, checkout, *_rest = evaluated(
        tmp_path, monkeypatch, MutatingRun()
    )
    assert receipt["checks"]["source_binding"] is False
    assert all(
        value is False
        for key, value in receipt["checks"].items()
        if key != "candidate_test"
    )
    assert receipt["checks"]["candidate_test"] is False
    assert receipt["measured_metadata"] is None
    assert (checkout / "foreign-untracked.c").read_text() == "mutation\n"
    assert (tmp_path / "r0-output/receipt.json").is_file()


def test_synthetic_trace_mutation_invalidates_dependent_measurements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class TraceMutatingRun(FakeRun):
        def __call__(
            self, argv: list[str], output_dir: Path, *, cwd: Path | None = None
        ) -> ChildResult:
            result = super().__call__(argv, output_dir, cwd=cwd)
            if self.simulation_count == 1:
                trace = Path(argv[1])
                trace.write_bytes(trace.read_bytes() + b"mutation")
                self.simulation_count += 1
            return result

    receipt, _runner, *_rest = evaluated(tmp_path, monkeypatch, TraceMutatingRun())
    assert receipt["checks"]["deterministic"] is False
    assert receipt["checks"]["capacity"] is False
    assert receipt["checks"]["metadata_probe"] is False
    assert receipt["measured_metadata"] is None
    assert any("synthetic trace" in error for error in receipt["errors"])


def test_late_evidence_mutation_is_recorded_and_invalidates_every_fact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class EvidenceMutatingRun(FakeRun):
        def __call__(
            self, argv: list[str], output_dir: Path, *, cwd: Path | None = None
        ) -> ChildResult:
            result = super().__call__(argv, output_dir, cwd=cwd)
            if argv[0].endswith("metadata-probe"):
                assert cwd is not None
                stage = Path(cwd)
                (stage / "commands/08-determinism-run-1/stdout.raw").write_bytes(
                    b"late mutation\n"
                )
                (stage / "simulator-results.cachesim").write_bytes(b"late mutation\n")
                (stage / "capacity-probe").write_bytes(b"late mutation\n")
            return result

    receipt, _runner, *_rest = evaluated(tmp_path, monkeypatch, EvidenceMutatingRun())
    assert receipt["checks"]["evidence_binding"] is False
    assert all(
        value is False
        for key, value in receipt["checks"].items()
        if key != "candidate_test"
    )
    assert receipt["checks"]["candidate_test"] is False
    assert receipt["measured_metadata"] is None
    raw = tmp_path / "r0-output/commands/08-determinism-run-1/stdout.raw"
    process = receipt["commands"][7]["stdout"]
    assert process["binding_intact"] is False
    assert process["sha256"] == sha256_file(raw)
    assert process["initial_sha256"] != process["sha256"]


def test_unregistered_stage_output_is_removed_and_invalidates_every_fact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ExtraOutputRun(FakeRun):
        def __call__(
            self, argv: list[str], output_dir: Path, *, cwd: Path | None = None
        ) -> ChildResult:
            result = super().__call__(argv, output_dir, cwd=cwd)
            if argv[0].endswith("metadata-probe"):
                assert cwd is not None
                (Path(cwd) / "unbound-candidate-output.bin").write_bytes(b"unbound")
            return result

    receipt, _runner, *_rest = evaluated(tmp_path, monkeypatch, ExtraOutputRun())
    assert receipt["checks"]["evidence_binding"] is False
    assert all(
        value is False
        for key, value in receipt["checks"].items()
        if key != "candidate_test"
    )
    assert receipt["checks"]["candidate_test"] is False
    assert receipt["unexpected_stage_entries"] == [
        {
            "path": "unbound-candidate-output.bin",
            "sha256": hashlib.sha256(b"unbound").hexdigest(),
            "size_bytes": 7,
            "type": "regular",
        }
    ]
    assert not (tmp_path / "r0-output/unbound-candidate-output.bin").exists()


def test_preflight_failure_retains_failure_receipt_and_runs_no_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout, base, candidate, lock = repository(tmp_path)
    monkeypatch.setattr("commissioning.cache_campaign.evaluate.SOURCE_LOCK", lock)
    source = source_receipt(tmp_path / "source.json", lock)
    write(checkout, "dirty.txt", "dirty\n")
    run = FakeRun()
    output = tmp_path / "failure"
    with pytest.raises(EvaluationError, match="dirty"):
        evaluate_r0(
            checkout=checkout,
            base=base,
            candidate=candidate,
            policy=POLICY,
            source_receipt=source,
            output=output,
            run=run,
        )
    failure = load_object(output / "receipt.json")
    assert failure["rung"] == "r0"
    assert failure["checks"]["source_binding"] is False
    assert all(
        value is None
        for key, value in failure["checks"].items()
        if key != "source_binding"
    )
    assert failure["errors"]
    assert run.commands == []


def test_scope_rejection_receipt_preserves_established_preflight_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout, base, _candidate, lock = repository(tmp_path)
    write(checkout, "libCacheSim/bin/cachesim/main.c", "mutated simulator\n")
    git(checkout, "add", "libCacheSim/bin/cachesim/main.c")
    git(checkout, "commit", "-qm", "invalid scope")
    candidate = git(checkout, "rev-parse", "HEAD")
    monkeypatch.setattr("commissioning.cache_campaign.evaluate.SOURCE_LOCK", lock)
    source = source_receipt(tmp_path / "source.json", lock)
    output = tmp_path / "rejection"
    with pytest.raises(EvaluationError, match="scope"):
        evaluate_r0(
            checkout=checkout,
            base=base,
            candidate=candidate,
            policy=POLICY,
            source_receipt=source,
            output=output,
            run=FakeRun(),
        )
    rejection = load_object(output / "receipt.json")
    assert rejection["checks"]["source_binding"] is True
    assert all(
        value is None
        for key, value in rejection["checks"].items()
        if key != "source_binding"
    )
    assert rejection["source_receipt_sha256"] == load_object(source)[
        "receipt_sha256"
    ]
    assert rejection["scope"]["allowed_paths"] is False
    assert "libCacheSim/bin/cachesim/main.c" in rejection["scope"]["changed_paths"]


def test_source_receipt_rejects_rehashed_invalid_build_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout, base, candidate, lock = repository(tmp_path)
    monkeypatch.setattr("commissioning.cache_campaign.evaluate.SOURCE_LOCK", lock)
    source = source_receipt(tmp_path / "source.json", lock)
    value = json.loads(source.read_text())
    value["commands"][0]["stdout_sha256"] = "invalid"
    value["receipt_sha256"] = record_sha256(value, "receipt_sha256")
    source.write_text(json.dumps(value))
    with pytest.raises(EvaluationError, match="command binding"):
        evaluate_r0(
            checkout=checkout,
            base=base,
            candidate=candidate,
            policy=POLICY,
            source_receipt=source,
            output=tmp_path / "failure",
            run=FakeRun(),
        )
    failure = load_object(tmp_path / "failure/receipt.json")
    assert failure["checks"]["source_binding"] is False
    assert failure["source_receipt_sha256"] is None


@pytest.mark.parametrize("mutation", ["head", "ancestor", "remote", "receipt"])
def test_preflight_binds_candidate_ancestry_remote_and_source_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    checkout, base, candidate, lock = repository(tmp_path)
    monkeypatch.setattr("commissioning.cache_campaign.evaluate.SOURCE_LOCK", lock)
    source = source_receipt(tmp_path / "source.json", lock)
    if mutation == "head":
        git(checkout, "checkout", "-q", base)
    elif mutation == "ancestor":
        candidate = base
        base = "0" * 40
    elif mutation == "remote":
        git(checkout, "remote", "set-url", "origin", "https://example.invalid/evil.git")
    else:
        value = json.loads(source.read_text())
        value["binary_sha256"] = "2" * 64
        source.write_text(json.dumps(value))
    with pytest.raises(EvaluationError):
        evaluate_r0(
            checkout=checkout,
            base=base,
            candidate=candidate,
            policy=POLICY if candidate != base else "Sieve",
            source_receipt=source,
            output=tmp_path / "failure",
            run=FakeRun(),
        )


def test_output_is_no_replace_and_preserves_foreign_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout, base, candidate, lock = repository(tmp_path)
    monkeypatch.setattr("commissioning.cache_campaign.evaluate.SOURCE_LOCK", lock)
    source = source_receipt(tmp_path / "source.json", lock)
    output = tmp_path / "existing"
    output.mkdir()
    marker = output / "foreign"
    marker.write_text("keep")
    with pytest.raises(EvaluationError, match="exist"):
        evaluate_r0(
            checkout=checkout,
            base=base,
            candidate=candidate,
            policy=POLICY,
            source_receipt=source,
            output=output,
            run=FakeRun(),
        )
    assert marker.read_text() == "keep"


def test_output_inside_checkout_is_explicitly_owned_not_a_source_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout, base, candidate, lock = repository(tmp_path)
    monkeypatch.setattr("commissioning.cache_campaign.evaluate.SOURCE_LOCK", lock)
    source = source_receipt(tmp_path / "source.json", lock)
    output = checkout / "commissioning/r0-evidence"
    receipt = evaluate_r0(
        checkout=checkout,
        base=base,
        candidate=candidate,
        policy=POLICY,
        source_receipt=source,
        output=output,
        run=FakeRun(),
    )
    assert receipt["checks"]["source_binding"] is True
    assert output.is_dir()
    assert git(checkout, "diff", "--name-only") == ""
    assert git(checkout, "diff", "--cached", "--name-only") == ""


def test_cli_supports_only_r0_and_prints_individual_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    source = tmp_path / "source.json"
    source.write_text("{}")
    output = tmp_path / "output"
    expected = {
        "rung": "r0",
        "receipt_sha256": "a" * 64,
        "checks": {"build": True, "deterministic": False},
    }

    def fake_evaluate(**kwargs: object) -> dict[str, object]:
        Path(kwargs["output"]).mkdir()  # type: ignore[arg-type]
        return expected

    monkeypatch.setattr(eval_cli, "evaluate_r0", fake_evaluate)
    result = eval_cli.main(
        [
            "--rung",
            "r0",
            "--checkout",
            str(checkout),
            "--candidate",
            "b" * 40,
            "--base",
            "a" * 40,
            "--policy",
            POLICY,
            "--source-receipt",
            str(source),
            "--output",
            str(output),
        ]
    )
    assert result == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed == {
        "checks": expected["checks"],
        "receipt_path": str(output / "receipt.json"),
        "receipt_sha256": "a" * 64,
        "rung": "r0",
    }
    assert eval_cli.main(["--rung", "r1"]) == 2
    error = capsys.readouterr().err
    assert error.startswith("error:")
    assert len(error.splitlines()) == 1
