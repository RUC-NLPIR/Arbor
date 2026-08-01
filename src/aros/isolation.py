"""One fail-closed Linux isolation profile for AROS child processes."""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import os
import platform
import resource
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence


NETWORK_POLICY = "deny-all"
PROCESS_POLICY = "single-process-no-threads"
ENVIRONMENT_POLICY = "strict-allowlist-v1"
_RESERVED_WORKSPACE_ENTRIES = {".git", ".aros", ".worktree", "runs"}
_ENVIRONMENT_ALLOWLIST = (
    "CUDA_VISIBLE_DEVICES",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "MKL_NUM_THREADS",
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "PYTHONIOENCODING",
    "PYTHONUNBUFFERED",
    "TZ",
)
ISOLATED_ENVIRONMENT_KEYS = tuple(sorted(_ENVIRONMENT_ALLOWLIST))

_LANDLOCK_CREATE_RULESET = 444
_LANDLOCK_ADD_RULE = 445
_LANDLOCK_RESTRICT_SELF = 446
_LANDLOCK_CREATE_RULESET_VERSION = 1
_LANDLOCK_RULE_PATH_BENEATH = 1

_FS_EXECUTE = 1 << 0
_FS_WRITE_FILE = 1 << 1
_FS_READ_FILE = 1 << 2
_FS_READ_DIR = 1 << 3
_FS_REMOVE_DIR = 1 << 4
_FS_REMOVE_FILE = 1 << 5
_FS_MAKE_CHAR = 1 << 6
_FS_MAKE_DIR = 1 << 7
_FS_MAKE_REG = 1 << 8
_FS_MAKE_SOCK = 1 << 9
_FS_MAKE_FIFO = 1 << 10
_FS_MAKE_BLOCK = 1 << 11
_FS_MAKE_SYM = 1 << 12
_FS_REFER = 1 << 13
_FS_TRUNCATE = 1 << 14
_FS_ALL = (1 << 15) - 1
_FS_READ = _FS_EXECUTE | _FS_READ_FILE | _FS_READ_DIR
_FS_FILE_READ = _FS_EXECUTE | _FS_READ_FILE
_FS_WRITE = (
    _FS_WRITE_FILE
    | _FS_REMOVE_DIR
    | _FS_REMOVE_FILE
    | _FS_MAKE_DIR
    | _FS_MAKE_REG
    | _FS_MAKE_SYM
    | _FS_REFER
    | _FS_TRUNCATE
)
_FS_FILE_ALLOWED = _FS_EXECUTE | _FS_WRITE_FILE | _FS_READ_FILE | _FS_TRUNCATE
_NET_BIND_TCP = 1 << 0
_NET_CONNECT_TCP = 1 << 1

_PR_SET_DUMPABLE = 4
_PR_SET_KEEPCAPS = 8
_PR_CAPBSET_DROP = 24
_PR_SET_NO_NEW_PRIVS = 38
_PR_CAP_AMBIENT = 47
_PR_CAP_AMBIENT_CLEAR_ALL = 4
_LINUX_CAPABILITY_VERSION_3 = 0x20080522

_SCMP_ACT_ALLOW = 0x7FFF0000
_SCMP_ACT_ERRNO = 0x00050000
_DENIED_SYSCALLS = (
    "socket",
    "socketpair",
    "connect",
    "bind",
    "listen",
    "accept",
    "accept4",
    "sendto",
    "sendmsg",
    "sendmmsg",
    "recvfrom",
    "recvmsg",
    "recvmmsg",
    "io_uring_setup",
    "io_uring_enter",
    "io_uring_register",
    "kill",
    "tkill",
    "tgkill",
    "pidfd_send_signal",
    "mount",
    "umount2",
    "pivot_root",
    "chroot",
    "unshare",
    "setns",
    "open_by_handle_at",
    "name_to_handle_at",
    "move_mount",
    "fsopen",
    "fspick",
    "fsmount",
    "mount_setattr",
    "ptrace",
    "process_vm_writev",
    "process_vm_readv",
    "pidfd_open",
    "pidfd_getfd",
    "fork",
    "vfork",
    "clone",
    "clone3",
    "setsid",
    "setpgid",
    "bpf",
    "perf_event_open",
    "userfaultfd",
    "keyctl",
    "add_key",
    "request_key",
    "kexec_load",
    "kexec_file_load",
    "init_module",
    "finit_module",
    "delete_module",
    "reboot",
    "swapon",
    "swapoff",
)
_REQUIRED_DENIED_SYSCALLS = {
    "socket",
    "kill",
    "mount",
    "unshare",
    "setns",
    "io_uring_setup",
    "io_uring_enter",
    "io_uring_register",
}

_LIBC = ctypes.CDLL(None, use_errno=True)
_LIBC.syscall.restype = ctypes.c_long
_LIBC.prctl.restype = ctypes.c_int


class IsolationError(RuntimeError):
    """The requested isolation boundary cannot be proven or installed."""


@dataclass(frozen=True)
class IsolationLimits:
    cpu_seconds: int = 3600
    address_space_bytes: int = 8 * 1024 * 1024 * 1024
    file_size_bytes: int = 2 * 1024 * 1024 * 1024
    open_files: int = 256
    processes: int = 1

    def __post_init__(self) -> None:
        for field, value in self.__dict__.items():
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise IsolationError(f"{field} must be a positive integer")


@dataclass(frozen=True)
class IsolationProbe:
    landlock_abi: int
    seccomp_library: str


@dataclass(frozen=True)
class IsolationPolicy:
    profile: str
    writable_paths: tuple[str, ...]
    limits: IsolationLimits
    network_policy: str
    process_policy: str
    environment_policy: dict[str, object]


@dataclass(frozen=True)
class IsolationLaunch:
    profile: str
    landlock_abi: int
    env: dict[str, str]
    preexec_fn: Callable[[], None]
    writable_paths: tuple[str, ...]
    limits: IsolationLimits
    network_policy: str
    process_policy: str
    environment_policy: dict[str, object]


class _LandlockRulesetAttr(ctypes.Structure):
    _fields_ = [
        ("handled_access_fs", ctypes.c_uint64),
        ("handled_access_net", ctypes.c_uint64),
    ]


class _LandlockPathBeneathAttr(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("allowed_access", ctypes.c_uint64),
        ("parent_fd", ctypes.c_int32),
    ]


class _CapHeader(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]


class _CapData(ctypes.Structure):
    _fields_ = [
        ("effective", ctypes.c_uint32),
        ("permitted", ctypes.c_uint32),
        ("inheritable", ctypes.c_uint32),
    ]


def probe_isolated_linux() -> IsolationProbe:
    """Prove that the fixed Linux isolation primitives are available."""
    if platform.system() != "Linux" or platform.machine() not in {"x86_64", "aarch64"}:
        raise IsolationError("isolated-linux requires Linux on x86_64 or aarch64")
    abi = _probe_landlock_abi()
    if abi != 4:
        raise IsolationError(
            f"isolated-linux supports exactly Landlock ABI 4; host reports ABI {abi}"
        )
    library = _find_seccomp_library()
    if library is None:
        raise IsolationError("isolated-linux requires libseccomp")
    _load_seccomp_library(library)
    if not hasattr(os, "O_PATH"):
        raise IsolationError("isolated-linux requires O_PATH support")
    return IsolationProbe(landlock_abi=abi, seccomp_library=library)


def build_isolated_linux(
    root: str | Path,
    writable_paths: Sequence[str],
    *,
    limits: IsolationLimits | None = None,
    source_environment: Mapping[str, str] | None = None,
) -> IsolationLaunch:
    """Build environment and pre-exec enforcement for ``isolated-linux``."""
    workspace = Path(root).expanduser().resolve()
    policy = isolated_linux_policy(workspace, writable_paths, limits=limits)
    probe = probe_isolated_linux()
    environment = _isolated_environment(
        workspace,
        policy.writable_paths,
        source_environment if source_environment is not None else os.environ,
    )
    rules = _filesystem_rules(workspace, policy.writable_paths)
    seccomp = _load_seccomp_library(probe.seccomp_library)
    cap_last = _cap_last()

    def apply_isolation() -> None:
        _apply_limits(policy.limits)
        _drop_capabilities(cap_last)
        _set_no_new_privileges()
        _apply_landlock(probe.landlock_abi, rules)
        _apply_seccomp(seccomp)

    return IsolationLaunch(
        profile="isolated-linux",
        landlock_abi=probe.landlock_abi,
        env=environment,
        preexec_fn=apply_isolation,
        writable_paths=policy.writable_paths,
        limits=policy.limits,
        network_policy=policy.network_policy,
        process_policy=policy.process_policy,
        environment_policy=policy.environment_policy,
    )


def isolated_linux_policy(
    root: str | Path,
    writable_paths: Sequence[str],
    *,
    limits: IsolationLimits | None = None,
) -> IsolationPolicy:
    """Validate and freeze policy without depending on this host's capabilities."""
    workspace = Path(root).expanduser().resolve()
    if not workspace.is_dir():
        raise IsolationError(f"workspace must be an existing directory: {workspace}")
    return IsolationPolicy(
        profile="isolated-linux",
        writable_paths=_normalize_writable_paths(workspace, writable_paths),
        limits=limits or IsolationLimits(),
        network_policy=NETWORK_POLICY,
        process_policy=PROCESS_POLICY,
        environment_policy={
            "kind": ENVIRONMENT_POLICY,
            "allowed_keys": list(ISOLATED_ENVIRONMENT_KEYS),
            "generated_keys": [
                "AROS_SECURITY_PROFILE",
                "HOME",
                "PATH",
                "PYTHONDONTWRITEBYTECODE",
                "TMPDIR",
            ],
        },
    )


def _probe_landlock_abi() -> int:
    ctypes.set_errno(0)
    result = _LIBC.syscall(
        _LANDLOCK_CREATE_RULESET,
        ctypes.c_void_p(),
        ctypes.c_size_t(0),
        ctypes.c_uint(_LANDLOCK_CREATE_RULESET_VERSION),
    )
    return int(result) if result >= 0 else 0


def _find_seccomp_library() -> str | None:
    return ctypes.util.find_library("seccomp")


def _load_seccomp_library(path: str) -> ctypes.CDLL:
    try:
        library = ctypes.CDLL(path, use_errno=True)
        library.seccomp_init.argtypes = [ctypes.c_uint32]
        library.seccomp_init.restype = ctypes.c_void_p
        library.seccomp_rule_add.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_int,
            ctypes.c_uint,
        ]
        library.seccomp_rule_add.restype = ctypes.c_int
        library.seccomp_load.argtypes = [ctypes.c_void_p]
        library.seccomp_load.restype = ctypes.c_int
        library.seccomp_release.argtypes = [ctypes.c_void_p]
        library.seccomp_release.restype = None
        library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
        library.seccomp_syscall_resolve_name.restype = ctypes.c_int
    except (AttributeError, OSError) as error:
        raise IsolationError(f"unable to load libseccomp: {error}") from error
    return library


def _normalize_writable_paths(
    root: Path,
    writable_paths: Sequence[str],
) -> tuple[str, ...]:
    if isinstance(writable_paths, (str, bytes)):
        raise IsolationError("writable_paths must be a sequence of relative paths")
    normalized: list[str] = []
    for value in writable_paths:
        if not isinstance(value, str) or not value.strip():
            raise IsolationError("writable path must be a non-empty string")
        candidate = (root / value).resolve()
        try:
            relative = candidate.relative_to(root)
        except ValueError as error:
            raise IsolationError(f"writable path escapes the workspace: {value}") from error
        if not candidate.exists():
            raise IsolationError(f"writable path must be existing: {value}")
        if relative == Path("."):
            raise IsolationError("workspace root cannot be writable because it contains reserved paths")
        if relative.parts[0] in _RESERVED_WORKSPACE_ENTRIES:
            raise IsolationError(f"writable path is reserved: {value}")
        normalized.append(relative.as_posix())
    return tuple(sorted(set(normalized)))


def _isolated_environment(
    root: Path,
    writable_paths: tuple[str, ...],
    source: Mapping[str, str],
) -> dict[str, str]:
    environment = {
        key: str(source[key])
        for key in _ENVIRONMENT_ALLOWLIST
        if key in source
    }
    home = next(
        (
            str((root / relative).resolve())
            for relative in writable_paths
            if (root / relative).is_dir()
        ),
        "/nonexistent",
    )
    environment["PATH"] = "/usr/local/bin:/usr/bin:/bin"
    environment["HOME"] = home
    environment["TMPDIR"] = home
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["AROS_SECURITY_PROFILE"] = "isolated-linux"
    return environment


def _filesystem_rules(
    root: Path,
    writable_paths: tuple[str, ...],
) -> tuple[tuple[str, int], ...]:
    rules, _has_protected = _workspace_read_rules(root)
    for relative in writable_paths:
        path = (root / relative).resolve()
        access = (_FS_READ | _FS_WRITE) if path.is_dir() else (_FS_FILE_READ | _FS_WRITE)
        rules.append((str(path), access))
    for system_path, access in _system_runtime_rules():
        if os.path.exists(system_path):
            rules.append((system_path, access))
    return tuple(rules)


def _workspace_read_rules(directory: Path) -> tuple[list[tuple[str, int]], bool]:
    child_rules: list[tuple[str, int]] = []
    has_protected = False
    try:
        entries = list(directory.iterdir())
    except OSError as error:
        raise IsolationError(f"cannot inspect workspace path: {directory}") from error
    for entry in entries:
        if _is_sensitive_workspace_name(entry.name) or entry.is_symlink():
            has_protected = True
            continue
        if entry.is_dir():
            nested_rules, nested_protected = _workspace_read_rules(entry)
            if nested_protected:
                child_rules.extend(nested_rules)
                has_protected = True
            else:
                child_rules.append((str(entry), _FS_READ))
        elif entry.is_file():
            child_rules.append((str(entry), _FS_FILE_READ))
        else:
            has_protected = True
    if has_protected:
        return child_rules, True
    return [(str(directory), _FS_READ)], False


def _is_sensitive_workspace_name(name: str) -> bool:
    lowered = name.lower()
    return bool(
        name in _RESERVED_WORKSPACE_ENTRIES
        or lowered in {"secrets", ".secrets", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}
        or lowered == ".env"
        or lowered.startswith(".env.")
        or lowered.endswith((".key", ".pem", ".p12", ".pfx"))
    )


def _system_runtime_rules() -> tuple[tuple[str, int], ...]:
    return (
        ("/usr", _FS_READ),
        ("/bin", _FS_READ),
        ("/lib", _FS_READ),
        ("/lib64", _FS_READ),
        ("/etc/ld.so.cache", _FS_READ_FILE),
        ("/etc/localtime", _FS_READ_FILE),
        ("/dev/null", _FS_READ_FILE | _FS_WRITE_FILE),
        ("/dev/zero", _FS_READ_FILE | _FS_WRITE_FILE),
        ("/dev/random", _FS_READ_FILE),
        ("/dev/urandom", _FS_READ_FILE),
        ("/proc/self/status", _FS_READ_FILE),
    )


def _apply_limits(limits: IsolationLimits) -> None:
    values = (
        (resource.RLIMIT_CPU, limits.cpu_seconds),
        (resource.RLIMIT_AS, limits.address_space_bytes),
        (resource.RLIMIT_FSIZE, limits.file_size_bytes),
        (resource.RLIMIT_NOFILE, limits.open_files),
        (resource.RLIMIT_NPROC, limits.processes),
        (resource.RLIMIT_CORE, 0),
    )
    for resource_id, value in values:
        resource.setrlimit(resource_id, (value, value))


def _cap_last() -> int:
    try:
        return int(Path("/proc/sys/kernel/cap_last_cap").read_text(encoding="ascii"))
    except (OSError, ValueError) as error:
        raise IsolationError("cannot determine Linux capability range") from error


def _drop_capabilities(cap_last: int) -> None:
    for capability in range(cap_last + 1):
        if _LIBC.prctl(_PR_CAPBSET_DROP, capability, 0, 0, 0) != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
    if _LIBC.prctl(
        _PR_CAP_AMBIENT,
        _PR_CAP_AMBIENT_CLEAR_ALL,
        0,
        0,
        0,
    ) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    header = _CapHeader(version=_LINUX_CAPABILITY_VERSION_3, pid=0)
    data = (_CapData * 2)()
    if _LIBC.capset(ctypes.byref(header), ctypes.byref(data)) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    for option in (_PR_SET_KEEPCAPS, _PR_SET_DUMPABLE):
        if _LIBC.prctl(option, 0, 0, 0, 0) != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))


def _set_no_new_privileges() -> None:
    if _LIBC.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _apply_landlock(abi: int, rules: tuple[tuple[str, int], ...]) -> None:
    if abi < 4:
        raise OSError(errno.ENOSYS, "Landlock ABI is insufficient")
    attributes = _LandlockRulesetAttr(
        handled_access_fs=_FS_ALL,
        handled_access_net=_NET_BIND_TCP | _NET_CONNECT_TCP,
    )
    ctypes.set_errno(0)
    ruleset_fd = int(
        _LIBC.syscall(
            _LANDLOCK_CREATE_RULESET,
            ctypes.byref(attributes),
            ctypes.sizeof(attributes),
            0,
        )
    )
    if ruleset_fd < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    opened: dict[tuple[int, int], tuple[int, int]] = {}
    try:
        for path, access in rules:
            fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            metadata = os.fstat(fd)
            key = (metadata.st_dev, metadata.st_ino)
            if key in opened:
                prior_fd, prior_access = opened[key]
                opened[key] = (prior_fd, prior_access | access)
                os.close(fd)
            else:
                opened[key] = (fd, access)
        for fd, access in opened.values():
            if not stat.S_ISDIR(os.fstat(fd).st_mode):
                access &= _FS_FILE_ALLOWED
            path_rule = _LandlockPathBeneathAttr(
                allowed_access=access,
                parent_fd=fd,
            )
            ctypes.set_errno(0)
            result = _LIBC.syscall(
                _LANDLOCK_ADD_RULE,
                ruleset_fd,
                _LANDLOCK_RULE_PATH_BENEATH,
                ctypes.byref(path_rule),
                0,
            )
            if result != 0:
                error = ctypes.get_errno()
                raise OSError(error, os.strerror(error))
        ctypes.set_errno(0)
        if _LIBC.syscall(_LANDLOCK_RESTRICT_SELF, ruleset_fd, 0) != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
    finally:
        for fd, _access in opened.values():
            os.close(fd)
        os.close(ruleset_fd)


def _apply_seccomp(library: ctypes.CDLL) -> None:
    context = library.seccomp_init(_SCMP_ACT_ALLOW)
    if not context:
        raise OSError(errno.ENOMEM, "seccomp_init failed")
    try:
        for name in _DENIED_SYSCALLS:
            syscall_number = library.seccomp_syscall_resolve_name(name.encode("ascii"))
            if syscall_number < 0:
                if name in _REQUIRED_DENIED_SYSCALLS:
                    raise OSError(errno.ENOSYS, f"libseccomp cannot resolve {name}")
                continue
            result = library.seccomp_rule_add(
                context,
                _SCMP_ACT_ERRNO | errno.EPERM,
                syscall_number,
                0,
            )
            if result != 0:
                raise OSError(-result, f"seccomp rule failed for {name}")
        result = library.seccomp_load(context)
        if result != 0:
            raise OSError(-result, "seccomp_load failed")
    finally:
        library.seccomp_release(context)
