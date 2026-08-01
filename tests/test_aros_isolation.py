"""Real Linux isolation tests for the single AROS isolated profile."""

from __future__ import annotations

import ctypes
import json
import errno
import os
import subprocess
from pathlib import Path

import pytest

from arbor.aros.isolation import (
    ENVIRONMENT_POLICY,
    NETWORK_POLICY,
    PROCESS_POLICY,
    IsolationError,
    IsolationLimits,
    build_isolated_linux,
    isolated_linux_policy,
    probe_isolated_linux,
)


PYTHON = "/usr/bin/python3"


def _run_isolated(
    root: Path,
    code: str,
    *,
    writable_paths: list[str] | None = None,
    limits: IsolationLimits | None = None,
    source_environment: dict[str, str] | None = None,
    args: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    launch = build_isolated_linux(
        root,
        writable_paths or [],
        limits=limits,
        source_environment=source_environment,
    )
    return subprocess.run(
        [PYTHON, "-c", code, *(args or [])],
        cwd=root,
        env=launch.env,
        preexec_fn=launch.preexec_fn,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def test_probe_real_linux_isolation_capabilities() -> None:
    probe = probe_isolated_linux()

    assert probe.landlock_abi >= 4
    assert "seccomp" in probe.seccomp_library


def test_build_exposes_one_fixed_fail_closed_profile(tmp_path: Path) -> None:
    writable = tmp_path / "scratch"
    writable.mkdir()

    launch = build_isolated_linux(tmp_path, ["scratch"])

    assert launch.profile == "isolated-linux"
    assert launch.landlock_abi >= 4
    assert launch.writable_paths == ("scratch",)
    assert launch.network_policy == NETWORK_POLICY == "deny-all"
    assert launch.process_policy == PROCESS_POLICY == "single-process-no-threads"
    assert launch.environment_policy["kind"] == ENVIRONMENT_POLICY
    assert callable(launch.preexec_fn)


def test_filesystem_is_read_only_except_explicit_writable_paths(tmp_path: Path) -> None:
    (tmp_path / "visible.txt").write_text("visible", encoding="utf-8")
    readonly = tmp_path / "readonly"
    readonly.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    outside = tmp_path.parent / f"{tmp_path.name}-outside-secret.txt"
    outside.write_text("outside-secret", encoding="utf-8")
    for reserved in (".git", ".aros", ".worktree", "runs"):
        directory = tmp_path / reserved
        directory.mkdir()
        (directory / "secret.txt").write_text(
            f"{reserved}-secret", encoding="utf-8"
        )
    sensitive_files = {
        ".env": "environment-secret",
        ".env.local": "local-secret",
        "private.pem": "private-key",
        "service.key": "service-key",
    }
    for relative, content in sensitive_files.items():
        (tmp_path / relative).write_text(content, encoding="utf-8")
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "token.txt").write_text("token", encoding="utf-8")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / ".env.production").write_text("nested", encoding="utf-8")

    code = r"""
import errno, json, pathlib, sys
root = pathlib.Path.cwd()
outside = pathlib.Path(sys.argv[1])
def read(path):
    try:
        return {"ok": True, "value": path.read_text()}
    except OSError as error:
        return {"ok": False, "errno": error.errno}
def listdir(path):
    try:
        return {"ok": True, "value": sorted(item.name for item in path.iterdir())}
    except OSError as error:
        return {"ok": False, "errno": error.errno}
def write(path):
    try:
        path.write_text("written")
        return {"ok": True}
    except OSError as error:
        return {"ok": False, "errno": error.errno}
print(json.dumps({
    "visible": read(root / "visible.txt"),
    "outside": read(outside),
    "reserved": {name: read(root / name / "secret.txt") for name in (".git", ".aros", ".worktree", "runs")},
    "reserved_list": {name: listdir(root / name) for name in (".git", ".aros", ".worktree", "runs")},
    "sensitive": {name: read(root / name) for name in (".env", ".env.local", "private.pem", "service.key", "secrets/token.txt", "nested/.env.production")},
    "root_write": write(root / "forbidden.txt"),
    "readonly_write": write(root / "readonly" / "forbidden.txt"),
    "scratch_write": write(root / "scratch" / "allowed.txt"),
}))
"""
    result = _run_isolated(
        tmp_path,
        code,
        writable_paths=["scratch"],
        args=[str(outside)],
    )

    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed["visible"] == {"ok": True, "value": "visible"}
    assert observed["outside"]["ok"] is False
    assert observed["root_write"]["ok"] is False
    assert observed["readonly_write"]["ok"] is False
    assert all(item["ok"] is False for item in observed["reserved"].values())
    assert all(item["ok"] is False for item in observed["reserved_list"].values())
    assert all(item["ok"] is False for item in observed["sensitive"].values())
    assert observed["scratch_write"] == {"ok": True}
    assert (scratch / "allowed.txt").read_text(encoding="utf-8") == "written"
    assert not (tmp_path / "forbidden.txt").exists()


def test_secret_environment_network_kill_and_escape_syscalls_are_blocked(
    tmp_path: Path,
) -> None:
    code = r"""
import ctypes, errno, json, os, socket
def denied(call):
    try:
        call()
        return None
    except OSError as error:
        return error.errno
status = {}
for line in open("/proc/self/status"):
    if line.startswith(("CapInh:", "CapPrm:", "CapEff:", "CapBnd:", "CapAmb:", "NoNewPrivs:")):
        key, value = line.split(":", 1)
        status[key] = value.strip()
libc = ctypes.CDLL(None, use_errno=True)
def unshare():
    if libc.unshare(0x10000000) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
def io_uring_setup():
    if libc.syscall(425, 1, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
print(json.dumps({
    "secret": os.environ.get("AROS_SECRET_SENTINEL"),
    "profile": os.environ.get("AROS_SECURITY_PROFILE"),
    "socket_errno": denied(lambda: socket.socket()),
    "kill_errno": denied(lambda: os.kill(os.getpid(), 0)),
    "unshare_errno": denied(unshare),
    "io_uring_errno": denied(io_uring_setup),
    "status": status,
}))
"""
    environment = dict(os.environ)
    environment["AROS_SECRET_SENTINEL"] = "must-not-cross-boundary"
    environment["PATH"] = "/host/attacker/path"

    result = _run_isolated(
        tmp_path,
        code,
        source_environment=environment,
    )

    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed["secret"] is None
    assert observed["profile"] == "isolated-linux"
    assert observed["socket_errno"] == errno.EPERM
    assert observed["kill_errno"] == errno.EPERM
    assert observed["unshare_errno"] == errno.EPERM
    assert observed["io_uring_errno"] == errno.EPERM
    assert observed["status"]["NoNewPrivs"] == "1"
    assert all(
        int(observed["status"][field], 16) == 0
        for field in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")
    )


def test_environment_uses_fixed_path_and_only_real_writable_home(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    source = {"PATH": "/host/secret/bin", "LANG": "C.UTF-8"}

    without_writes = build_isolated_linux(
        tmp_path,
        [],
        source_environment=source,
    )
    with_writes = build_isolated_linux(
        tmp_path,
        ["scratch"],
        source_environment=source,
    )

    assert without_writes.env["PATH"] == "/usr/local/bin:/usr/bin:/bin"
    assert without_writes.env["HOME"] == without_writes.env["TMPDIR"] == "/nonexistent"
    assert with_writes.env["HOME"] == with_writes.env["TMPDIR"] == str(scratch)
    assert with_writes.env["PYTHONDONTWRITEBYTECODE"] == "1"


def test_kernel_resource_limits_are_applied(tmp_path: Path) -> None:
    limits = IsolationLimits(
        cpu_seconds=2,
        address_space_bytes=256 * 1024 * 1024,
        file_size_bytes=4096,
        open_files=32,
        processes=8,
    )
    code = r"""
import json, resource
names = ["RLIMIT_CPU", "RLIMIT_AS", "RLIMIT_FSIZE", "RLIMIT_NOFILE", "RLIMIT_NPROC", "RLIMIT_CORE"]
print(json.dumps({name: resource.getrlimit(getattr(resource, name)) for name in names}))
"""

    result = _run_isolated(tmp_path, code, limits=limits)

    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed == {
        "RLIMIT_CPU": [2, 2],
        "RLIMIT_AS": [256 * 1024 * 1024, 256 * 1024 * 1024],
        "RLIMIT_FSIZE": [4096, 4096],
        "RLIMIT_NOFILE": [32, 32],
        "RLIMIT_NPROC": [8, 8],
        "RLIMIT_CORE": [0, 0],
    }


def test_process_and_thread_creation_are_denied(tmp_path: Path) -> None:
    code = r"""
import errno, json, os, threading
observed = {"thread_error": None, "fork_errno": None}
def work():
    pass
try:
    thread = threading.Thread(target=work)
    thread.start()
except RuntimeError as error:
    observed["thread_error"] = str(error)
try:
    os.fork()
except OSError as error:
    observed["fork_errno"] = error.errno
print(json.dumps(observed))
"""

    result = _run_isolated(tmp_path, code)

    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert "can't start new thread" in observed["thread_error"]
    assert observed["fork_errno"] == errno.EPERM


@pytest.mark.parametrize("relative", [".git", ".aros/out", ".worktree/x", "runs/x"])
def test_reserved_paths_can_never_be_made_writable(
    tmp_path: Path,
    relative: str,
) -> None:
    (tmp_path / relative).mkdir(parents=True)

    with pytest.raises(IsolationError, match="reserved"):
        build_isolated_linux(tmp_path, [relative])


def test_writable_path_must_be_existing_and_contained(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (tmp_path / "escape").symlink_to(outside, target_is_directory=True)

    with pytest.raises(IsolationError, match="existing"):
        build_isolated_linux(tmp_path, ["missing"])
    with pytest.raises(IsolationError, match="workspace"):
        build_isolated_linux(tmp_path, ["escape"])


def test_capability_probe_failure_is_fail_closed(tmp_path: Path, monkeypatch) -> None:
    from arbor.aros import isolation

    monkeypatch.setattr(isolation, "_probe_landlock_abi", lambda: 0)
    policy = isolated_linux_policy(tmp_path, [])
    assert policy.profile == "isolated-linux"
    with pytest.raises(IsolationError, match="Landlock"):
        build_isolated_linux(tmp_path, [])

    monkeypatch.undo()
    monkeypatch.setattr(isolation, "_find_seccomp_library", lambda: None)
    with pytest.raises(IsolationError, match="seccomp"):
        build_isolated_linux(tmp_path, [])


def test_capability_bounding_drop_failure_is_not_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arbor.aros import isolation

    def denied_prctl(*_args: object) -> int:
        ctypes.set_errno(errno.EPERM)
        return -1

    monkeypatch.setattr(isolation._LIBC, "prctl", denied_prctl)

    with pytest.raises(OSError) as error:
        isolation._drop_capabilities(0)
    assert error.value.errno == errno.EPERM
