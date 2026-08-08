from __future__ import annotations

import hashlib
import signal
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from commissioning.cache_campaign import cachesim as cachesim_module
from commissioning.cache_campaign.cachesim import (
    CacheSimOutputError,
    ChildRunError,
    ParsedResult,
    parse_cachesim_output,
    run_child,
)
from commissioning.cache_campaign.records import sha256_file


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


def test_parse_accepts_no_final_newline_and_known_logger_lines() -> None:
    output = (
        "[INFO]  trace path: /trace/dev-a.oracleGeneral.bin, trace_type oracleGeneral\n"
        "\n"
        + LINE.rstrip("\n")
    )
    assert parse_cachesim_output(output).request_count == 900_000


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
        "[INFO] /trace/other Sieve cache size 1MiB, 1 req, miss ratio 0.1\n",
    ],
)
def test_parse_rejects_extra_non_result_output(extra: str) -> None:
    with pytest.raises(CacheSimOutputError, match="unrecognized"):
        parse_cachesim_output(extra + LINE)


def test_parse_rejects_bytes_including_invalid_utf8() -> None:
    with pytest.raises(CacheSimOutputError, match="text"):
        parse_cachesim_output(b"\xff")  # type: ignore[arg-type]


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


def test_run_child_creates_a_new_directory_without_replacement(tmp_path: Path) -> None:
    output = tmp_path / "run"
    output.mkdir()
    marker = output / "foreign"
    marker.write_bytes(b"keep")

    with pytest.raises(ChildRunError, match="must not exist"):
        run_child([sys.executable, "-c", "pass"], output)

    assert marker.read_bytes() == b"keep"


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
    monkeypatch.setattr(
        cachesim_module.os,
        "wait4",
        lambda pid, options: (
            pid,
            7 << 8,
            SimpleNamespace(ru_utime=0.000000001, ru_stime=0.000000002),
        ),
    )
    times = iter([1000, 2500])
    monkeypatch.setattr(cachesim_module.time, "monotonic_ns", lambda: next(times))

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
        if path == output:
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


def test_run_child_wait4_failure_closes_outputs_without_double_reap(
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

    def fail_wait4(pid: int, options: int) -> object:
        raise OSError("wait4 failed")

    monkeypatch.setattr(cachesim_module.os, "wait4", fail_wait4)
    output = tmp_path / "run"
    with pytest.raises(ChildRunError, match="wait4 failed"):
        run_child(["program"], output)
    assert observed["stdout"].closed  # type: ignore[union-attr]
    assert observed["stderr"].closed  # type: ignore[union-attr]
    assert observed["process"].wait_calls == 0  # type: ignore[union-attr]
    assert not output.exists()


def test_run_child_cleanup_preserves_a_replacement_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "run"

    def replace_stdout_then_fail(argv: tuple[str, ...], **kwargs: object) -> object:
        (output / "stdout.raw").unlink()
        (output / "stdout.raw").write_bytes(b"foreign")
        raise OSError("spawn failed")

    monkeypatch.setattr(
        cachesim_module.subprocess, "Popen", replace_stdout_then_fail
    )
    with pytest.raises(ChildRunError, match="spawn failed"):
        run_child(["program"], output)
    assert (output / "stdout.raw").read_bytes() == b"foreign"
    assert not (output / "stderr.raw").exists()


def test_run_child_cleanup_preserves_a_replacement_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "run"
    moved = tmp_path / "moved-owned-run"

    def replace_directory_then_fail(argv: tuple[str, ...], **kwargs: object) -> object:
        output.rename(moved)
        output.mkdir()
        (output / "foreign").write_bytes(b"keep")
        raise OSError("spawn failed")

    monkeypatch.setattr(
        cachesim_module.subprocess, "Popen", replace_directory_then_fail
    )
    with pytest.raises(ChildRunError, match="spawn failed"):
        run_child(["program"], output)
    assert (output / "foreign").read_bytes() == b"keep"
    assert moved.exists()
