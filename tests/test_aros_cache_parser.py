from __future__ import annotations

import hashlib
import os
import signal
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from commissioning.cache_campaign import cachesim as cachesim_module
from commissioning.cache_campaign import linux_subreaper as subreaper_module
from commissioning.cache_campaign.cachesim import (
    CacheSimOutputError,
    ChildRunError,
    ParsedResult,
    parse_cachesim_output,
    run_child,
)
from commissioning.cache_campaign.records import quarantine_unlink, sha256_file


LINE = (
    "/trace/dev-a.oracleGeneral.bin S3FIFO-0.1000-2 cache size  10.00MiB, "
    "          900000 req, miss ratio 0.1234, byte miss ratio 0.2345, "
    "throughput 20.25 MQPS\n"
)
COLORLESS_PINNED_INFO = (
    "[INFO]  08-08-2026 12:34:56 cli_parser.c:802  (tid=1234): "
    "trace path: /trace/dev-a.oracleGeneral.bin, trace_type oracleGeneral, "
    "ofilepath result/dev-a.cachesim, 1 threads, warmup 0 sec, "
    "total 1 algo x 1 size = 1 caches, S3FIFO, consider object metadata\n"
)
ANSI_PINNED_INFO = (
    "\x1b[32m[INFO]  08-08-2026 12:34:56 cli_parser.c:802  (tid=1234): "
    "trace path: /trace/dev-a.oracleGeneral.bin, trace_type oracleGeneral, "
    "ofilepath result/dev-a.cachesim, 1 threads, warmup 0 sec, "
    "total 1 algo x 1 size = 1 caches, S3FIFO, consider object metadata\n"
    "\x1b[0m"
)


def test_parse_single_result_line() -> None:
    parsed = parse_cachesim_output(LINE)
    assert parsed == ParsedResult(
        request_count=900_000,
        object_miss_ratio=Decimal("0.1234"),
        byte_miss_ratio=Decimal("0.2345"),
        simulator_throughput_mqps=Decimal("20.25"),
    )


def test_parse_accepts_no_final_newline_and_blank_stdout_lines() -> None:
    output = "\n" + LINE.rstrip("\n") + "\n\n"
    assert parse_cachesim_output(output).request_count == 900_000


@pytest.mark.parametrize("logger", [COLORLESS_PINNED_INFO, ANSI_PINNED_INFO])
def test_parse_rejects_pinned_stderr_logger_text(logger: str) -> None:
    with pytest.raises(CacheSimOutputError):
        parse_cachesim_output(logger + LINE)


@pytest.mark.parametrize("level", ["INFO", "DEBUG", "WARN", "ERROR"])
def test_parse_rejects_logger_wrapped_complete_result(level: str) -> None:
    wrapped = f"[{level}]  08-08-2026 12:34:56 sim.c:213: " + LINE
    with pytest.raises(CacheSimOutputError):
        parse_cachesim_output(wrapped)


def test_parse_rejects_relative_trace_path_result() -> None:
    with pytest.raises(CacheSimOutputError):
        parse_cachesim_output(LINE.removeprefix("/"))


@pytest.mark.parametrize("character", ["\t", "\r", "\v", "\f", "\0", "\xa0"])
def test_parse_rejects_non_ascii_space_and_control_characters(
    character: str,
) -> None:
    invalid = LINE.replace("cache size  ", f"cache size{character} ")
    with pytest.raises(CacheSimOutputError):
        parse_cachesim_output(invalid)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (", byte miss ratio 0.2345", ""),
        (", throughput 20.25 MQPS", ""),
        ("miss ratio 0.1234", "miss ratio NaN"),
        ("miss ratio 0.1234", "miss ratio Infinity"),
        ("miss ratio 0.1234", "miss ratio -0.1"),
        ("miss ratio 0.1234", "miss ratio +0.1"),
        ("miss ratio 0.1234", "miss ratio 1e999999"),
        ("miss ratio 0.1234", "miss ratio 1"),
        ("miss ratio 0.1234", "miss ratio .1"),
        ("miss ratio 0.1234", "miss ratio 1."),
        ("miss ratio 0.1234", "miss ratio 1.0001"),
        ("byte miss ratio 0.2345", "byte miss ratio 2.0"),
        ("900000 req", "0 req"),
        ("throughput 20.25", "throughput 0.0"),
        ("throughput 20.25", "throughput -1.0"),
    ],
)
def test_parse_rejects_invalid_or_out_of_range_fields(old: str, new: str) -> None:
    with pytest.raises(CacheSimOutputError):
        parse_cachesim_output(LINE.replace(old, new))


def test_parse_rejects_duplicate_result_lines() -> None:
    with pytest.raises(CacheSimOutputError, match="exactly one"):
        parse_cachesim_output(LINE + LINE)


def test_parse_rejects_multi_cache_output_without_throughput() -> None:
    incomplete = LINE.replace(", throughput 20.25 MQPS", "")
    with pytest.raises(CacheSimOutputError, match="unrecognized"):
        parse_cachesim_output(incomplete + incomplete)


@pytest.mark.parametrize(
    "extra",
    [
        "arbitrary text\n",
        "[NOTICE] plausible-looking but unknown logger\n",
        "[INFO] hacked\n",
        "[INFO] /trace/other Sieve cache size 1MiB, 1 req, miss ratio 0.1\n",
    ],
)
def test_parse_rejects_extra_non_result_output(extra: str) -> None:
    with pytest.raises(CacheSimOutputError, match="unrecognized"):
        parse_cachesim_output(extra + LINE)


def test_parse_rejects_bytes_including_invalid_utf8() -> None:
    with pytest.raises(CacheSimOutputError, match="text"):
        parse_cachesim_output(b"\xff")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "invalid",
    [
        LINE.replace("900000", "9" * 5000),
        LINE.replace("0.1234", "1." + "9" * 5000),
        LINE.replace("20.25", "9" * 5000 + ".25"),
    ],
)
def test_parse_rejects_oversized_numbers_with_bounded_errors(invalid: str) -> None:
    with pytest.raises(CacheSimOutputError) as caught:
        parse_cachesim_output(invalid)
    assert len(str(caught.value)) <= 512


def test_parse_arbitrary_line_error_is_bounded() -> None:
    with pytest.raises(CacheSimOutputError) as caught:
        parse_cachesim_output("x" * 10_000 + "\n" + LINE)
    assert len(str(caught.value)) <= 512


def test_run_child_records_exact_output_exit_and_resource_cost(tmp_path: Path) -> None:
    output = tmp_path / "run"
    argv = [
        sys.executable,
        "-c",
        (
            "import os; "
            "os.write(1, b'out\\x00'); os.write(2, b'err\\xff'); "
            "sum(i*i for i in range(500000)); raise SystemExit(7)"
        ),
    ]

    result = run_child(argv, output)

    assert result.argv == tuple(argv)
    assert result.returncode == 7
    assert result.cpu_ns > 0
    assert result.wall_ns >= 0
    assert result.stdout_path == output / "stdout.raw"
    assert result.stdout_path.read_bytes() == b"out\x00"
    assert result.stdout_bytes == 4
    assert result.stdout_sha256 == hashlib.sha256(b"out\x00").hexdigest()
    assert result.stdout_sha256 == sha256_file(result.stdout_path)
    assert result.stderr_path == output / "stderr.raw"
    assert result.stderr_path.read_bytes() == b"err\xff"
    assert result.stderr_bytes == 4
    assert result.stderr_sha256 == hashlib.sha256(b"err\xff").hexdigest()
    assert result.stderr_sha256 == sha256_file(result.stderr_path)


def test_run_child_timeout_kills_group_and_leaves_no_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_popen = cachesim_module.subprocess.Popen
    processes: list[object] = []

    def capture_process(*args: object, **kwargs: object) -> object:
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(cachesim_module.subprocess, "Popen", capture_process)
    output = tmp_path / "timeout-run"
    started = time.monotonic()
    with pytest.raises(ChildRunError, match="timeout") as caught:
        run_child(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            output,
            timeout_seconds=0.05,
            max_output_bytes=1024,
        )
    assert time.monotonic() - started < 2
    assert len(str(caught.value)) <= 512
    process = processes[0]
    with pytest.raises(ProcessLookupError):
        os.kill(process.pid, 0)  # type: ignore[union-attr]
    with pytest.raises(ProcessLookupError):
        os.killpg(process.pid, 0)  # type: ignore[union-attr]
    assert not output.exists()
    assert not list(tmp_path.glob(".cachesim-stage-*"))


def test_run_child_output_limit_kills_noisy_group_and_leaves_no_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_popen = cachesim_module.subprocess.Popen
    processes: list[object] = []

    def capture_process(*args: object, **kwargs: object) -> object:
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(cachesim_module.subprocess, "Popen", capture_process)
    real_cleanup = cachesim_module._cleanup_outputs
    retained_sizes: list[int] = []

    def observe_then_cleanup(*args: object, **kwargs: object) -> object:
        directory_descriptor = args[2]
        retained_sizes.extend(
            os.stat(name, dir_fd=directory_descriptor).st_size
            for name in ("stdout.raw", "stderr.raw")
        )
        return real_cleanup(*args, **kwargs)

    monkeypatch.setattr(cachesim_module, "_cleanup_outputs", observe_then_cleanup)
    output = tmp_path / "noisy-run"
    code = "import os\nwhile True: os.write(1, b'x' * 65536)"
    started = time.monotonic()
    with pytest.raises(ChildRunError, match="output limit") as caught:
        run_child(
            [sys.executable, "-c", code],
            output,
            timeout_seconds=2,
            max_output_bytes=32 * 1024,
        )
    assert time.monotonic() - started < 2
    assert len(str(caught.value)) <= 512
    process = processes[0]
    with pytest.raises(ProcessLookupError):
        os.kill(process.pid, 0)  # type: ignore[union-attr]
    with pytest.raises(ProcessLookupError):
        os.killpg(process.pid, 0)  # type: ignore[union-attr]
    assert not output.exists()
    assert not list(tmp_path.glob(".cachesim-stage-*"))
    assert retained_sizes
    assert sum(retained_sizes) <= 32 * 1024


def test_output_limit_does_not_truncate_unrelated_child_artifacts(
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    artifact = cwd / "artifact.bin"
    result = run_child(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; Path('artifact.bin').write_bytes(b'x' * 20000)",
        ],
        tmp_path / "artifact-run",
        cwd=cwd,
        timeout_seconds=2,
        max_output_bytes=32 * 1024,
    )
    assert result.returncode == 0
    assert artifact.read_bytes() == b"x" * 20000


@pytest.mark.parametrize(
    ("timeout_seconds", "max_output_bytes"),
    [(0, 1024), (True, 1024), (1, 0), (1, True), (1, 2**31)],
)
def test_run_child_rejects_invalid_resource_limits_before_output(
    tmp_path: Path, timeout_seconds: object, max_output_bytes: object
) -> None:
    output = tmp_path / "invalid-limit"
    with pytest.raises(ChildRunError, match="timeout|output"):
        run_child(
            [sys.executable, "-c", "pass"],
            output,
            timeout_seconds=timeout_seconds,  # type: ignore[arg-type]
            max_output_bytes=max_output_bytes,  # type: ignore[arg-type]
        )
    assert not output.exists()


def test_run_child_closes_partial_pipe_setup_after_second_pipe_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_pipe2 = cachesim_module.os.pipe2
    created: list[int] = []
    calls = 0

    def fail_second_pipe(flags: int) -> tuple[int, int]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("second pipe failed")
        descriptors = real_pipe2(flags)
        created.extend(descriptors)
        return descriptors

    monkeypatch.setattr(cachesim_module.os, "pipe2", fail_second_pipe)
    output = tmp_path / "partial-pipe"
    with pytest.raises(ChildRunError, match="second pipe failed"):
        run_child([sys.executable, "-c", "pass"], output)
    assert len(created) == 2
    for descriptor in created:
        with pytest.raises(OSError):
            os.fstat(descriptor)
    assert not output.exists()


def test_run_child_creates_a_new_directory_without_replacement(tmp_path: Path) -> None:
    output = tmp_path / "run"
    output.mkdir()
    marker = output / "foreign"
    marker.write_bytes(b"keep")

    with pytest.raises(ChildRunError, match="must not exist"):
        run_child([sys.executable, "-c", "pass"], output)

    assert marker.read_bytes() == b"keep"


def test_run_child_existing_output_error_is_bounded(tmp_path: Path) -> None:
    parent = tmp_path
    for index in range(30):
        parent = parent / (f"segment-{index:02d}-" + "x" * 35)
        parent.mkdir()
    output = parent / "run"
    output.mkdir()
    with pytest.raises(ChildRunError) as caught:
        run_child([sys.executable, "-c", "pass"], output)
    assert len(str(caught.value)) <= 512


@pytest.mark.parametrize(
    "argv",
    [
        "python -c pass",
        b"python",
        [],
        [""],
        ["python", "bad\x00argument"],
        [b"python"],
        (item for item in ["python"]),
    ],
)
def test_run_child_rejects_invalid_argv_without_creating_output(
    tmp_path: Path, argv: object
) -> None:
    output = tmp_path / "run"
    with pytest.raises(ChildRunError, match="argv"):
        run_child(argv, output)  # type: ignore[arg-type]
    assert not output.exists()


def test_run_child_uses_explicit_real_cwd(tmp_path: Path) -> None:
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    output = tmp_path / "run"
    result = run_child(
        [sys.executable, "-c", "import os; print(os.getcwd(), end='')"],
        output,
        cwd=cwd,
    )
    assert result.stdout_path.read_text() == str(cwd.resolve())


@pytest.mark.parametrize("kind", ["missing", "file", "symlink"])
def test_run_child_rejects_non_real_cwd(tmp_path: Path, kind: str) -> None:
    cwd = tmp_path / kind
    if kind == "file":
        cwd.write_text("not a directory")
    elif kind == "symlink":
        target = tmp_path / "target"
        target.mkdir()
        cwd.symlink_to(target, target_is_directory=True)
    output = tmp_path / "run"
    with pytest.raises(ChildRunError, match="cwd"):
        run_child([sys.executable, "-c", "pass"], output, cwd=cwd)
    assert not output.exists()


def test_run_child_documents_default_cwd() -> None:
    assert run_child.__doc__ is not None
    assert "inherits the caller's current working directory" in run_child.__doc__


def test_run_child_starts_a_new_session(tmp_path: Path) -> None:
    result = run_child(
        [
            sys.executable,
            "-c",
            "import os; print(int(os.getsid(0) == os.getpid()), end='')",
        ],
        tmp_path / "run",
    )
    assert result.stdout_path.read_bytes() == b"1"


def test_run_child_converts_signal_status(tmp_path: Path) -> None:
    result = run_child(
        [
            sys.executable,
            "-c",
            "import os, signal; os.kill(os.getpid(), signal.SIGTERM)",
        ],
        tmp_path / "run",
    )
    assert result.returncode == -signal.SIGTERM


def test_run_child_uses_wait4_cpu_rounding_and_popen_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: dict[str, object] = {}

    class FakeProcess:
        pid = 4321
        returncode = None

        def __init__(self, argv: tuple[str, ...], **kwargs: object) -> None:
            observed["argv"] = argv
            observed.update(kwargs)
            observed["process"] = self

    monkeypatch.setattr(cachesim_module.subprocess, "Popen", FakeProcess)
    wait_options: list[int] = []

    def completed_wait4(pid: int, options: int) -> tuple[int, int, object]:
        wait_options.append(options)
        return (
            pid,
            7 << 8,
            SimpleNamespace(ru_utime=0.000000001, ru_stime=0.000000002),
        )

    monkeypatch.setattr(cachesim_module.os, "wait4", completed_wait4)
    times = iter([1000, 2500])
    monkeypatch.setattr(
        cachesim_module.time, "monotonic_ns", lambda: next(times, 3000)
    )
    group_checks: list[tuple[int, int]] = []

    def empty_process_group(pgid: int, sig: int) -> None:
        group_checks.append((pgid, sig))
        raise ProcessLookupError

    monkeypatch.setattr(cachesim_module.os, "killpg", empty_process_group)

    result = run_child(["program", "argument"], tmp_path / "run")

    assert observed["argv"] == ("program", "argument")
    assert observed["shell"] is False
    assert observed["start_new_session"] is True
    assert observed["cwd"] is None
    assert "env" not in observed
    assert result.returncode == 7
    assert result.wall_ns == 1500
    assert result.cpu_ns == 3
    assert observed["process"].returncode == 7  # type: ignore[union-attr]
    assert group_checks == [(4321, 0)]
    assert wait_options == [os.WNOHANG]


def test_run_child_requires_wait4_before_creating_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delattr(cachesim_module.os, "wait4")
    output = tmp_path / "run"
    with pytest.raises(ChildRunError, match="wait4"):
        run_child([sys.executable, "-c", "pass"], output)
    assert not output.exists()


def test_run_child_closes_streams_and_cleans_up_after_spawn_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: dict[str, object] = {}

    def fail_spawn(argv: tuple[str, ...], **kwargs: object) -> object:
        observed.update(kwargs)
        raise OSError("spawn failed")

    monkeypatch.setattr(cachesim_module.subprocess, "Popen", fail_spawn)
    output = tmp_path / "run"
    with pytest.raises(ChildRunError, match="spawn failed"):
        run_child(["program"], output)
    assert observed["stdout"].closed  # type: ignore[union-attr]
    assert observed["stderr"].closed  # type: ignore[union-attr]
    assert not output.exists()


def test_run_child_cleans_up_if_the_new_directory_cannot_be_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "run"
    real_open = cachesim_module.os.open

    def fail_directory_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        if isinstance(path, str) and path.startswith(".cachesim-stage-"):
            raise OSError("directory open failed")
        return real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(cachesim_module.os, "open", fail_directory_open)
    with pytest.raises(ChildRunError, match="directory open failed"):
        run_child(["program"], output)
    assert not output.exists()


def test_run_child_cleans_up_if_a_raw_stream_cannot_be_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "run"
    real_fdopen = cachesim_module.os.fdopen
    calls = 0

    def fail_first_fdopen(descriptor: int, mode: str) -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("fdopen failed")
        return real_fdopen(descriptor, mode)

    monkeypatch.setattr(cachesim_module.os, "fdopen", fail_first_fdopen)
    with pytest.raises(ChildRunError, match="fdopen failed"):
        run_child(["program"], output)
    assert not output.exists()


def test_stage_fstat_precedes_path_stat_and_failure_cleans_owned_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_fstat = cachesim_module.os.fstat
    real_stat = cachesim_module.os.stat
    stage_fstat_attempted = False
    failed = False

    def fail_stage_fstat(descriptor: int) -> os.stat_result:
        nonlocal stage_fstat_attempted, failed
        target = os.readlink(f"/proc/self/fd/{descriptor}")
        if ".cachesim-stage-" in target and not failed:
            stage_fstat_attempted = True
            failed = True
            raise OSError("stage fstat failed")
        return real_fstat(descriptor)

    def forbid_early_stage_stat(
        path: object, *args: object, **kwargs: object
    ) -> os.stat_result:
        if (
            isinstance(path, str)
            and path.startswith(".cachesim-stage-")
            and not stage_fstat_attempted
        ):
            raise AssertionError("stage path stat preceded retained fstat")
        return real_stat(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(cachesim_module.os, "fstat", fail_stage_fstat)
    monkeypatch.setattr(cachesim_module.os, "stat", forbid_early_stage_stat)
    with pytest.raises(ChildRunError, match="stage fstat failed"):
        run_child(["program"], tmp_path / "run")
    assert not any(
        path.name.startswith(".cachesim-stage-") for path in tmp_path.iterdir()
    )


def test_stage_path_stat_failure_after_fstat_cleans_owned_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_stat = cachesim_module.os.stat
    failed = False

    def fail_stage_stat(
        path: object, *args: object, **kwargs: object
    ) -> os.stat_result:
        nonlocal failed
        if isinstance(path, str) and path.startswith(".cachesim-stage-") and not failed:
            failed = True
            raise OSError("stage stat failed")
        return real_stat(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(cachesim_module.os, "stat", fail_stage_stat)
    with pytest.raises(ChildRunError, match="stage stat failed"):
        run_child(["program"], tmp_path / "run")
    assert failed
    assert not any(
        path.name.startswith(".cachesim-stage-") for path in tmp_path.iterdir()
    )


def test_raw_fstat_failure_registers_owned_file_for_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_fstat = cachesim_module.os.fstat
    failed = False

    def fail_stdout_fstat(descriptor: int) -> os.stat_result:
        nonlocal failed
        target = os.readlink(f"/proc/self/fd/{descriptor}")
        if target.endswith("/stdout.raw") and not failed:
            failed = True
            raise OSError("raw fstat failed")
        return real_fstat(descriptor)

    monkeypatch.setattr(cachesim_module.os, "fstat", fail_stdout_fstat)
    with pytest.raises(ChildRunError, match="raw fstat failed"):
        run_child(["program"], tmp_path / "run")
    assert failed
    assert not any(
        path.name.startswith(".cachesim-stage-") for path in tmp_path.iterdir()
    )


def test_run_child_wait4_failure_kills_and_reaps_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: dict[str, object] = {}

    class FakeProcess:
        pid = 4321
        returncode = None
        wait_calls = 0

        def __init__(self, argv: tuple[str, ...], **kwargs: object) -> None:
            observed.update(kwargs)
            observed["process"] = self

        def wait(self) -> int:
            self.wait_calls += 1
            return 0

    monkeypatch.setattr(cachesim_module.subprocess, "Popen", FakeProcess)
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        cachesim_module.os,
        "killpg",
        lambda pgid, sig: signals.append((pgid, sig)),
    )

    def fail_wait4(pid: int, options: int) -> object:
        raise OSError("wait4 failed")

    monkeypatch.setattr(cachesim_module.os, "wait4", fail_wait4)
    output = tmp_path / "run"
    with pytest.raises(ChildRunError, match="wait4 failed"):
        run_child(["program"], output)
    assert observed["stdout"].closed  # type: ignore[union-attr]
    assert observed["stderr"].closed  # type: ignore[union-attr]
    assert observed["process"].wait_calls == 1  # type: ignore[union-attr]
    assert signals == [(4321, signal.SIGKILL)]
    assert not output.exists()


def test_run_child_wait4_failure_leaves_no_live_or_zombie_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_popen = cachesim_module.subprocess.Popen
    processes: list[object] = []

    def capture_process(*args: object, **kwargs: object) -> object:
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(cachesim_module.subprocess, "Popen", capture_process)

    def fail_wait4(pid: int, options: int) -> object:
        raise OSError("injected wait4 failure")

    monkeypatch.setattr(cachesim_module.os, "wait4", fail_wait4)
    output = tmp_path / "run"
    try:
        with pytest.raises(ChildRunError, match="injected wait4 failure"):
            run_child(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                output,
            )
        process = processes[0]
        pid = process.pid  # type: ignore[union-attr]
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        with pytest.raises(ProcessLookupError):
            os.killpg(pid, 0)
        with pytest.raises(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)
        assert not output.exists()
    finally:
        for process in processes:
            if process.returncode is None:  # type: ignore[union-attr]
                try:
                    os.killpg(process.pid, signal.SIGKILL)  # type: ignore[union-attr]
                except ProcessLookupError:
                    pass
                process.wait()  # type: ignore[union-attr]


def test_run_child_rejects_and_kills_descendant_after_leader_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_path = tmp_path / "descendant.pid"
    output = tmp_path / "run"
    real_popen = cachesim_module.subprocess.Popen
    leaders: list[object] = []

    def capture_leader(*args: object, **kwargs: object) -> object:
        process = real_popen(*args, **kwargs)
        leaders.append(process)
        return process

    monkeypatch.setattr(cachesim_module.subprocess, "Popen", capture_leader)
    descendant_code = (
        "import os, time; time.sleep(60); "
        "os.write(1, b'late stdout'); os.write(2, b'late stderr')"
    )
    leader_code = (
        "import pathlib, subprocess, sys; "
        f"child = subprocess.Popen([sys.executable, '-c', {descendant_code!r}]); "
        f"pathlib.Path({str(pid_path)!r}).write_text(str(child.pid))"
    )
    try:
        with pytest.raises(ChildRunError, match="descendant"):
            run_child([sys.executable, "-c", leader_code], output)
        descendant_pid = int(pid_path.read_text())
        leader_pid = leaders[0].pid  # type: ignore[union-attr]
        with pytest.raises(ProcessLookupError):
            os.kill(descendant_pid, 0)
        with pytest.raises(ProcessLookupError):
            os.killpg(leader_pid, 0)
        assert not output.exists()
    finally:
        if leaders:
            try:
                os.killpg(leaders[0].pid, signal.SIGKILL)  # type: ignore[union-attr]
            except ProcessLookupError:
                pass
        if pid_path.exists():
            descendant_pid = int(pid_path.read_text())
            for _ in range(100):
                try:
                    os.kill(descendant_pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.01)


def _escaped_descendant_leader_code(
    pid_path: Path,
    ready_path: Path,
    marker_path: Path,
    mode: str,
) -> str:
    descendant = (
        "import os, pathlib, time; os.setsid(); "
        f"pathlib.Path({str(pid_path)!r}).write_text(str(os.getpid())); "
        f"pathlib.Path({str(ready_path)!r}).write_text('ready'); "
        "time.sleep(0.3); "
        f"pathlib.Path({str(marker_path)!r}).write_text('late'); "
        "time.sleep(60)"
    )
    tail = {
        "exit": "",
        "sleep": "time.sleep(60)",
        "noise": "while True: os.write(1, b'x' * 65536)",
    }[mode]
    return (
        "import os, pathlib, subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {descendant!r}]); "
        f"ready = pathlib.Path({str(ready_path)!r}); "
        "deadline = time.monotonic() + 5; "
        "\nwhile not ready.exists():\n"
        "  assert time.monotonic() < deadline\n"
        "  time.sleep(0.005)\n"
        + tail
    )


def _assert_process_absent(pid: int) -> None:
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.01)
    raise AssertionError(f"process survived: {pid}")


def test_run_child_rejects_setsid_descendant_after_leader_exit(tmp_path: Path) -> None:
    pid_path = tmp_path / "escaped.pid"
    ready_path = tmp_path / "escaped.ready"
    marker_path = tmp_path / "late.marker"
    output = tmp_path / "escaped-run"
    try:
        with pytest.raises(ChildRunError, match="descendant"):
            run_child(
                [
                    sys.executable,
                    "-c",
                    _escaped_descendant_leader_code(
                        pid_path, ready_path, marker_path, "exit"
                    ),
                ],
                output,
                timeout_seconds=2,
                max_output_bytes=1024 * 1024,
            )
        escaped_pid = int(pid_path.read_text())
        _assert_process_absent(escaped_pid)
        time.sleep(0.35)
        assert not marker_path.exists()
        assert not output.exists()
    finally:
        if pid_path.exists():
            try:
                os.kill(int(pid_path.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize(
    ("mode", "timeout_seconds", "max_output_bytes", "message"),
    [
        ("sleep", 0.1, 1024 * 1024, "timeout"),
        ("noise", 2.0, 32 * 1024, "output limit"),
    ],
)
def test_run_child_reaps_setsid_descendant_on_operational_failure(
    tmp_path: Path,
    mode: str,
    timeout_seconds: float,
    max_output_bytes: int,
    message: str,
) -> None:
    pid_path = tmp_path / f"{mode}.pid"
    ready_path = tmp_path / f"{mode}.ready"
    marker_path = tmp_path / f"{mode}.marker"
    output = tmp_path / f"{mode}-run"
    try:
        with pytest.raises(ChildRunError, match=message):
            run_child(
                [
                    sys.executable,
                    "-c",
                    _escaped_descendant_leader_code(
                        pid_path, ready_path, marker_path, mode
                    ),
                ],
                output,
                timeout_seconds=timeout_seconds,
                max_output_bytes=max_output_bytes,
            )
        escaped_pid = int(pid_path.read_text())
        _assert_process_absent(escaped_pid)
        assert not marker_path.exists()
        assert not output.exists()
    finally:
        if pid_path.exists():
            try:
                os.kill(int(pid_path.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_run_child_does_not_reap_preexisting_child(tmp_path: Path) -> None:
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        result = run_child(
            [sys.executable, "-c", "pass"],
            tmp_path / "isolated-run",
            timeout_seconds=2,
        )
        assert result.returncode == 0
        assert unrelated.poll() is None
    finally:
        unrelated.kill()
        unrelated.wait()


def test_run_child_restores_process_subreaper_state(tmp_path: Path) -> None:
    before = subreaper_module._get_subreaper()
    result = run_child(
        [sys.executable, "-c", "pass"],
        tmp_path / "subreaper-state-run",
    )
    assert result.returncode == 0
    assert subreaper_module._get_subreaper() is before


def test_run_child_preserves_previously_enabled_subreaper_state(tmp_path: Path) -> None:
    before = subreaper_module._get_subreaper()
    subreaper_module._set_subreaper(True)
    try:
        result = run_child(
            [sys.executable, "-c", "pass"],
            tmp_path / "enabled-subreaper-run",
        )
        assert result.returncode == 0
        assert subreaper_module._get_subreaper() is True
    finally:
        subreaper_module._set_subreaper(before)


def test_run_child_cleanup_preserves_a_replacement_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "run"
    replaced: dict[str, Path] = {}
    real_create = cachesim_module._create_raw_file

    def create_then_replace(
        directory_descriptor: int,
        name: str,
        files: dict[str, object],
    ) -> object:
        stream = real_create(directory_descriptor, name, files)  # type: ignore[arg-type]
        if name == "stdout.raw":
            path = Path(os.readlink(f"/proc/self/fd/{stream.fileno()}"))
            path.unlink()
            path.write_bytes(b"foreign")
            replaced["path"] = path
        return stream

    def fail_spawn(argv: tuple[str, ...], **kwargs: object) -> object:
        raise OSError("spawn failed")

    monkeypatch.setattr(cachesim_module, "_create_raw_file", create_then_replace)
    monkeypatch.setattr(cachesim_module.subprocess, "Popen", fail_spawn)
    with pytest.raises(ChildRunError, match="spawn failed"):
        run_child(["program"], output)
    assert replaced["path"].read_bytes() == b"foreign"
    assert not (replaced["path"].parent / "stderr.raw").exists()


def test_run_child_cleanup_preserves_a_replacement_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "run"
    moved = tmp_path / "moved-owned-run"
    replaced: dict[str, Path] = {}
    real_create = cachesim_module._create_raw_file

    def create_then_replace_directory(
        directory_descriptor: int,
        name: str,
        files: dict[str, object],
    ) -> object:
        stream = real_create(directory_descriptor, name, files)  # type: ignore[arg-type]
        if name == "stdout.raw":
            stage = Path(os.readlink(f"/proc/self/fd/{stream.fileno()}"))
            stage = stage.parent
            stage.rename(moved)
            stage.mkdir()
            (stage / "foreign").write_bytes(b"keep")
            replaced["stage"] = stage
        return stream

    def fail_spawn(argv: tuple[str, ...], **kwargs: object) -> object:
        raise OSError("spawn failed")

    monkeypatch.setattr(
        cachesim_module, "_create_raw_file", create_then_replace_directory
    )
    monkeypatch.setattr(cachesim_module.subprocess, "Popen", fail_spawn)
    with pytest.raises(ChildRunError, match="spawn failed"):
        run_child(["program"], output)
    assert (replaced["stage"] / "foreign").read_bytes() == b"keep"
    assert moved.exists()


def test_cleanup_quarantine_preserves_swap_immediately_before_file_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    swapped: dict[str, Path] = {}

    def swap_then_quarantine(
        directory_descriptor: int,
        name: str,
        identity: tuple[int, int],
        **kwargs: object,
    ) -> None:
        if name == "stdout.raw" and not swapped:
            directory = Path(
                os.readlink(f"/proc/self/fd/{directory_descriptor}")
            )
            os.unlink(name, dir_fd=directory_descriptor)
            descriptor = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=directory_descriptor,
            )
            try:
                os.write(descriptor, b"foreign")
            finally:
                os.close(descriptor)
            swapped["directory"] = directory
        quarantine_unlink(
            directory_descriptor,
            name,
            identity,
            **kwargs,
        )

    monkeypatch.setattr(
        cachesim_module,
        "quarantine_unlink",
        swap_then_quarantine,
        raising=False,
    )

    def fail_spawn(argv: tuple[str, ...], **kwargs: object) -> object:
        raise OSError("spawn failed")

    monkeypatch.setattr(cachesim_module.subprocess, "Popen", fail_spawn)
    with pytest.raises(ChildRunError, match="spawn failed"):
        run_child(["program"], tmp_path / "run")
    assert (swapped["directory"] / "stdout.raw").read_bytes() == b"foreign"


def test_cleanup_quarantine_preserves_swap_before_directory_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "run"
    moved = tmp_path / "moved-owned"
    swapped: dict[str, object] = {}
    real_rename = cachesim_module._rename_noreplace

    def swap_then_rename(
        directory_descriptor: int, source: str, target: str
    ) -> None:
        if source.startswith(".cachesim-stage-") and not swapped:
            os.rename(
                source,
                moved.name,
                src_dir_fd=directory_descriptor,
                dst_dir_fd=directory_descriptor,
            )
            os.mkdir(source, 0o700, dir_fd=directory_descriptor)
            metadata = os.stat(
                source,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            swapped["identity"] = (metadata.st_dev, metadata.st_ino)
            swapped["name"] = source
        real_rename(directory_descriptor, source, target)

    monkeypatch.setattr(
        cachesim_module,
        "_rename_noreplace",
        swap_then_rename,
        raising=False,
    )

    def fail_spawn(argv: tuple[str, ...], **kwargs: object) -> object:
        raise OSError("spawn failed")

    monkeypatch.setattr(cachesim_module.subprocess, "Popen", fail_spawn)
    with pytest.raises(ChildRunError, match="spawn failed"):
        run_child(["program"], output)
    replacement = tmp_path / str(swapped["name"])
    metadata = replacement.lstat()
    assert (metadata.st_dev, metadata.st_ino) == swapped["identity"]


def test_run_child_rejects_post_create_directory_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "run"
    moved = tmp_path / "moved-owned"
    real_lstat = Path.lstat
    swapped = False

    def swap_before_receipt(path: Path) -> os.stat_result:
        nonlocal swapped
        if path == output and not swapped and path.is_dir():
            path.rename(moved)
            path.mkdir()
            (path / "foreign").write_bytes(b"keep")
            swapped = True
        return real_lstat(path)

    monkeypatch.setattr(Path, "lstat", swap_before_receipt)
    with pytest.raises(ChildRunError):
        run_child([sys.executable, "-c", "pass"], output)
    assert swapped
    assert (output / "foreign").read_bytes() == b"keep"


def test_run_child_detects_same_inode_mutation_after_task1_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stdout_calls = 0

    def mutate_after_hash(path: Path) -> str:
        nonlocal stdout_calls
        digest = sha256_file(path)
        if path.name == "stdout.raw":
            stdout_calls += 1
            if stdout_calls == 2:
                path.write_bytes(b"other")
        return digest

    monkeypatch.setattr(
        cachesim_module,
        "sha256_file",
        mutate_after_hash,
        raising=False,
    )
    with pytest.raises(ChildRunError, match="changed"):
        run_child(
            [sys.executable, "-c", "import os; os.write(1, b'alpha')"],
            tmp_path / "run",
        )
    assert stdout_calls >= 2
    assert not (tmp_path / "run").exists()


def test_run_child_preserves_replacement_installed_after_task1_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "run"
    replaced = False
    replacement_path: Path | None = None

    def replace_after_hash(path: Path) -> str:
        nonlocal replaced, replacement_path
        digest = sha256_file(path)
        if path.name == "stdout.raw" and not replaced:
            replacement_path = Path(os.readlink(path.parent)) / path.name
            path.unlink()
            path.write_bytes(b"foreign")
            replaced = True
        return digest

    monkeypatch.setattr(
        cachesim_module,
        "sha256_file",
        replace_after_hash,
        raising=False,
    )
    with pytest.raises(ChildRunError, match="changed"):
        run_child(
            [sys.executable, "-c", "import os; os.write(1, b'original')"],
            output,
        )
    assert replaced
    assert replacement_path is not None
    assert replacement_path.read_bytes() == b"foreign"


def test_run_child_never_adopts_empty_final_created_during_stage_mkdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "run"
    displaced = tmp_path / "displaced-owned"
    real_mkdir = cachesim_module.os.mkdir
    installed_identity: tuple[int, int] | None = None

    def mkdir_then_install_final(
        path: object,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal installed_identity
        if dir_fd is None:
            real_mkdir(path, mode)  # type: ignore[arg-type]
        else:
            real_mkdir(path, mode, dir_fd=dir_fd)  # type: ignore[arg-type]
        if installed_identity is not None:
            return
        if dir_fd is None and Path(path) == output:  # type: ignore[arg-type]
            output.rename(displaced)
            real_mkdir(output, 0o700)
        elif (
            dir_fd is not None
            and isinstance(path, str)
            and path.startswith(".cachesim-stage-")
        ):
            real_mkdir(output.name, 0o700, dir_fd=dir_fd)
        else:
            return
        metadata = output.lstat()
        installed_identity = (metadata.st_dev, metadata.st_ino)

    monkeypatch.setattr(cachesim_module.os, "mkdir", mkdir_then_install_final)
    with pytest.raises(ChildRunError):
        run_child([sys.executable, "-c", "pass"], output)
    metadata = output.lstat()
    assert (metadata.st_dev, metadata.st_ino) == installed_identity
    assert not any(path.name.startswith(".cachesim-stage-") for path in tmp_path.iterdir())


def test_run_child_revalidates_stdout_after_atomic_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "run"
    real_rename = cachesim_module._rename_noreplace
    replaced = False

    def publish_then_replace_stdout(
        directory_descriptor: int, source: str, target: str
    ) -> None:
        nonlocal replaced
        real_rename(directory_descriptor, source, target)
        if source.startswith(".cachesim-stage-") and target == output.name:
            final_descriptor = os.open(
                target,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=directory_descriptor,
            )
            try:
                os.unlink("stdout.raw", dir_fd=final_descriptor)
                descriptor = os.open(
                    "stdout.raw",
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=final_descriptor,
                )
                try:
                    os.write(descriptor, b"foreign")
                finally:
                    os.close(descriptor)
            finally:
                os.close(final_descriptor)
            replaced = True

    monkeypatch.setattr(cachesim_module, "_rename_noreplace", publish_then_replace_stdout)
    with pytest.raises(ChildRunError, match="changed"):
        run_child(
            [sys.executable, "-c", "import os; os.write(1, b'original')"],
            output,
        )
    assert replaced
    assert (output / "stdout.raw").read_bytes() == b"foreign"


def test_run_child_spawn_failure_cleans_stage_through_retained_parent_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    moved_parent = tmp_path / "moved-parent"
    foreign_parent = tmp_path / "foreign-parent"
    foreign_parent.mkdir()
    (foreign_parent / "sentinel").write_bytes(b"keep")

    def swap_parent_then_fail(argv: tuple[str, ...], **kwargs: object) -> object:
        parent.rename(moved_parent)
        parent.symlink_to(foreign_parent, target_is_directory=True)
        raise OSError("spawn failed")

    monkeypatch.setattr(cachesim_module.subprocess, "Popen", swap_parent_then_fail)
    with pytest.raises(ChildRunError, match="spawn failed"):
        run_child(["program"], parent / "run")
    assert parent.is_symlink()
    assert (foreign_parent / "sentinel").read_bytes() == b"keep"
    assert list(moved_parent.iterdir()) == []


def test_run_child_rejects_symlinked_output_parent(tmp_path: Path) -> None:
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(ChildRunError, match="parent"):
        run_child([sys.executable, "-c", "pass"], linked_parent / "run")
    assert list(real_parent.iterdir()) == []
