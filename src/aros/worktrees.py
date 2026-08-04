"""Pinned Git repository and detached checkout bindings for AROS."""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .store import json_sha256


_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_FILTER_CONFIG_KEY = re.compile(
    r"^filter\.(.+)\.(?:clean|smudge|process|required)$",
    re.IGNORECASE,
)
_FILTER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_BASE_CONFIGS = (
    "core.hooksPath=/dev/null",
    "core.fileMode=true",
    "core.fsmonitor=false",
    "core.autocrlf=false",
    "core.eol=lf",
    "core.symlinks=true",
    "core.attributesFile=/dev/null",
)


class WorktreeError(ValueError):
    """Raised when repository or checkout authority is unsafe or ambiguous."""


class BundleRemovalError(WorktreeError):
    """Report exactly which bundle checkouts were removed before failure."""

    def __init__(
        self,
        removed: tuple[Path, ...],
        remaining: tuple[Path, ...],
    ):
        self.removed = removed
        self.remaining = remaining
        super().__init__(
            "execution bundle removal failed; "
            f"removed={[str(path) for path in removed]}; "
            f"remaining={[str(path) for path in remaining]}"
        )


@dataclass(frozen=True)
class RepositoryBinding:
    root: Path
    git_dir: Path
    common_dir: Path


@dataclass(frozen=True)
class CheckoutBinding:
    path: Path
    git_dir: Path
    commit: str
    tree: str


@dataclass(frozen=True)
class ExecutionBundle:
    root: Path
    candidate: CheckoutBinding
    apparatus: CheckoutBinding
    temp: Path
    bundle_sha256: str


def bind_repository(root: str | Path) -> RepositoryBinding:
    """Bind one exact existing Git repository root."""
    supplied = Path(root).expanduser().absolute()
    try:
        resolved = supplied.resolve(strict=True)
    except OSError as error:
        raise WorktreeError(f"repository root does not exist: {supplied}") from error
    if supplied != resolved:
        raise WorktreeError(f"repository root must be exact: {supplied}")
    _require_plain_directory(resolved, "repository root")
    git_dir = _git_directory_from_marker(resolved)
    provisional = RepositoryBinding(resolved, git_dir, git_dir)
    actual_git_dir = Path(
        _git_text(provisional, "rev-parse", "--absolute-git-dir")
    ).resolve(strict=True)
    if actual_git_dir != git_dir:
        raise WorktreeError(f"repository Git directory association mismatch: {resolved}")
    common_dir = Path(
        _git_text(
            provisional,
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        )
    ).resolve(strict=True)
    binding = RepositoryBinding(resolved, git_dir, common_dir)
    top = Path(_git_text(binding, "rev-parse", "--show-toplevel")).resolve(
        strict=True
    )
    if top != resolved:
        raise WorktreeError(f"path is not the Git repository root: {resolved}")
    return binding


def create_detached_checkout(
    repo: RepositoryBinding,
    path: str | Path,
    commit: str,
) -> CheckoutBinding:
    """Create and validate one exact detached clean checkout."""
    _validate_repository_binding(repo)
    if _COMMIT.fullmatch(commit) is None:
        raise WorktreeError("checkout commit must be a full 40-hex object ID")
    resolved_commit = _git_text(repo, "rev-parse", "--verify", f"{commit}^{{commit}}")
    if resolved_commit != commit:
        raise WorktreeError(f"checkout commit is not exact: {commit}")
    tree = _git_text(repo, "rev-parse", "--verify", f"{commit}^{{tree}}")
    _reject_checkout_filters(repo)
    target = Path(path).expanduser().absolute()
    if target.resolve(strict=False) != target:
        raise WorktreeError(f"checkout path must be exact: {target}")
    if _path_exists(target):
        raise WorktreeError(f"checkout path already exists: {target}")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _require_plain_directory(target.parent, "checkout parent")
    result = _git_result(repo, "worktree", "add", "--detach", str(target), commit)
    if result.returncode != 0:
        raise WorktreeError(f"unable to create detached checkout: {_git_error(result)}")
    checkout = CheckoutBinding(
        path=target,
        git_dir=_checkout_git_directory(repo, target),
        commit=commit,
        tree=tree,
    )
    validate_detached_checkout(repo, checkout)
    return checkout


def validate_detached_checkout(
    repo: RepositoryBinding,
    checkout: CheckoutBinding,
) -> None:
    """Reject any drift from an exact detached clean checkout binding."""
    _reject_checkout_filters(repo)
    _reject_checkout_filters(
        repo,
        git_dir=checkout.git_dir,
        work_tree=checkout.path,
    )
    _validate_checkout_authority(repo, checkout)
    if not _checkout_is_clean(repo, checkout):
        raise WorktreeError(f"checkout is not exactly clean: {checkout.path}")


def create_execution_bundle(
    repo: RepositoryBinding,
    root: str | Path,
    candidate: str,
    apparatus: str,
) -> ExecutionBundle:
    """Create separate exact candidate and apparatus checkouts plus temp storage."""
    _validate_repository_binding(repo)
    bundle_root = Path(root).expanduser().absolute()
    if bundle_root.resolve(strict=False) != bundle_root:
        raise WorktreeError(f"execution bundle root must be exact: {bundle_root}")
    if _path_exists(bundle_root):
        raise WorktreeError(f"execution bundle root already exists: {bundle_root}")
    bundle_root.mkdir(mode=0o700, parents=True)
    candidate_checkout = create_detached_checkout(
        repo,
        bundle_root / "candidate",
        candidate,
    )
    apparatus_checkout = create_detached_checkout(
        repo,
        bundle_root / "apparatus",
        apparatus,
    )
    temporary = bundle_root / "tmp"
    temporary.mkdir(mode=0o700)
    payload = _bundle_payload(candidate_checkout, apparatus_checkout)
    bundle = ExecutionBundle(
        root=bundle_root,
        candidate=candidate_checkout,
        apparatus=apparatus_checkout,
        temp=temporary,
        bundle_sha256=json_sha256(payload),
    )
    validate_execution_bundle(repo, bundle)
    return bundle


def validate_execution_bundle(
    repo: RepositoryBinding,
    bundle: ExecutionBundle,
) -> None:
    """Validate both exact checkouts and the portable bundle binding."""
    _reject_checkout_filters(repo)
    for checkout in (bundle.candidate, bundle.apparatus):
        _reject_checkout_filters(
            repo,
            git_dir=checkout.git_dir,
            work_tree=checkout.path,
        )
    _validate_execution_bundle_authority(repo, bundle)
    candidate_clean = _checkout_is_clean(repo, bundle.candidate)
    apparatus_clean = _checkout_is_clean(repo, bundle.apparatus)
    if not candidate_clean or not apparatus_clean:
        raise WorktreeError(f"execution bundle is not exactly clean: {bundle.root}")


def _validate_execution_bundle_authority(
    repo: RepositoryBinding,
    bundle: ExecutionBundle,
) -> None:
    _validate_repository_binding(repo)
    if bundle.root.absolute() != bundle.root or bundle.root.resolve(strict=True) != bundle.root:
        raise WorktreeError(f"execution bundle root must be exact: {bundle.root}")
    _require_plain_directory(bundle.root, "execution bundle root")
    expected = {
        "candidate": bundle.root / "candidate",
        "apparatus": bundle.root / "apparatus",
        "tmp": bundle.root / "tmp",
    }
    if (
        bundle.candidate.path != expected["candidate"]
        or bundle.apparatus.path != expected["apparatus"]
        or bundle.temp != expected["tmp"]
    ):
        raise WorktreeError(f"execution bundle path binding mismatch: {bundle.root}")
    _require_plain_directory(bundle.temp, "execution bundle temp directory")
    try:
        entries = {entry.name for entry in bundle.root.iterdir()}
    except OSError as error:
        raise WorktreeError(f"unable to inspect execution bundle: {bundle.root}") from error
    if entries != set(expected):
        raise WorktreeError(f"execution bundle contains unexpected paths: {bundle.root}")
    _validate_checkout_authority(repo, bundle.candidate)
    _validate_checkout_authority(repo, bundle.apparatus)
    if bundle.bundle_sha256 != json_sha256(
        _bundle_payload(bundle.candidate, bundle.apparatus)
    ):
        raise WorktreeError(f"execution bundle hash mismatch: {bundle.root}")


def remove_clean_execution_bundle(
    repo: RepositoryBinding,
    bundle: ExecutionBundle,
) -> bool:
    """Remove both exact checkouts only when both are clean."""
    checkouts = (bundle.candidate, bundle.apparatus)
    try:
        _validate_execution_bundle_authority(repo, bundle)
        candidate_clean = _checkout_is_clean(repo, bundle.candidate)
        apparatus_clean = _checkout_is_clean(repo, bundle.apparatus)
    except WorktreeError as error:
        raise BundleRemovalError((), tuple(item.path for item in checkouts)) from error
    if not candidate_clean or not apparatus_clean:
        return False
    try:
        if not remove_clean_checkout(repo, bundle.candidate):
            return False
        if not remove_clean_checkout(repo, bundle.apparatus):
            raise WorktreeError("apparatus checkout became dirty during removal")
        shutil.rmtree(bundle.temp)
        bundle.root.rmdir()
    except Exception as error:
        registrations = _parse_worktree_registrations(
            _git_bytes(repo, "worktree", "list", "--porcelain", "-z", "--expire=now")
        )
        removed: list[Path] = []
        remaining: list[Path] = []
        for checkout in checkouts:
            registered = any(
                _same_path(str(record["worktree"]), checkout.path)
                for record in registrations
            )
            destination = removed if not _path_exists(checkout.path) and not registered else remaining
            destination.append(checkout.path)
        raise BundleRemovalError(tuple(removed), tuple(remaining)) from error
    return True


def _bundle_payload(
    candidate: CheckoutBinding,
    apparatus: CheckoutBinding,
) -> dict[str, object]:
    return {
        "candidate": {
            "path": "candidate",
            "commit": candidate.commit,
            "tree": candidate.tree,
        },
        "apparatus": {
            "path": "apparatus",
            "commit": apparatus.commit,
            "tree": apparatus.tree,
        },
        "temp": "tmp",
    }


def remove_clean_checkout(
    repo: RepositoryBinding,
    checkout: CheckoutBinding,
) -> bool:
    """Remove one exact clean checkout, preserving dirty material."""
    try:
        _validate_checkout_authority(repo, checkout)
    except WorktreeError as error:
        raise WorktreeError(
            f"ambiguous checkout authority: {checkout.path}"
        ) from error
    if not _checkout_is_clean(repo, checkout):
        return False
    result = _git_result(repo, "worktree", "remove", str(checkout.path))
    if result.returncode != 0:
        raise WorktreeError(f"unable to remove detached checkout: {_git_error(result)}")
    return True


def _reject_checkout_filters(
    repo: RepositoryBinding,
    *,
    git_dir: Path | None = None,
    work_tree: Path | None = None,
) -> None:
    result = _git_result(
        repo,
        "config",
        "--null",
        "--name-only",
        "--get-regexp",
        r"^filter\..*\.(clean|smudge|process|required)$",
        git_dir=git_dir,
        work_tree=work_tree,
    )
    if result.returncode not in {0, 1}:
        raise WorktreeError(f"unable to inspect Git filter configuration: {_git_error(result)}")
    configured: list[str] = []
    for raw_key in result.stdout.split(b"\0"):
        if not raw_key:
            continue
        try:
            key = raw_key.decode("utf-8")
        except UnicodeError as error:
            raise WorktreeError("ambiguous Git filter configuration") from error
        match = _FILTER_CONFIG_KEY.fullmatch(key)
        if match is None or _FILTER_NAME.fullmatch(match.group(1)) is None:
            raise WorktreeError(f"ambiguous Git filter configuration: {key!r}")
        configured.append(key)
    if configured:
        raise WorktreeError(
            f"checkout filters are not allowed: {', '.join(sorted(configured))}"
        )


def _validate_repository_binding(repo: RepositoryBinding) -> None:
    if bind_repository(repo.root) != repo:
        raise WorktreeError(f"repository binding changed: {repo.root}")


def _validate_checkout_authority(
    repo: RepositoryBinding,
    checkout: CheckoutBinding,
) -> None:
    _validate_repository_binding(repo)
    _require_plain_directory(checkout.path, "detached checkout")
    if _checkout_git_directory(repo, checkout.path) != checkout.git_dir:
        raise WorktreeError(f"checkout Git directory association changed: {checkout.path}")
    registrations = _worktree_registrations(repo)
    matches = [
        record
        for record in registrations
        if _same_path(str(record["worktree"]), checkout.path)
    ]
    if len(matches) != 1:
        raise WorktreeError(f"checkout is not uniquely registered: {checkout.path}")
    registration = matches[0]
    if (
        registration.get("HEAD") != checkout.commit
        or registration.get("detached") is not True
        or "branch" in registration
        or "prunable" in registration
    ):
        raise WorktreeError(f"checkout registration mismatch: {checkout.path}")
    common = Path(
        _git_text(
            repo,
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
            git_dir=checkout.git_dir,
            work_tree=checkout.path,
        )
    ).resolve(strict=True)
    if common != repo.common_dir:
        raise WorktreeError(f"checkout common Git directory mismatch: {checkout.path}")
    top = Path(
        _git_text(
            repo,
            "rev-parse",
            "--show-toplevel",
            git_dir=checkout.git_dir,
            work_tree=checkout.path,
        )
    ).resolve(strict=True)
    if top != checkout.path:
        raise WorktreeError(f"checkout root mismatch: {checkout.path}")
    attached = _git_result(
        repo,
        "symbolic-ref",
        "-q",
        "HEAD",
        git_dir=checkout.git_dir,
        work_tree=checkout.path,
    )
    if attached.returncode != 1:
        raise WorktreeError(f"checkout HEAD is not detached: {checkout.path}")
    head = _git_text(
        repo,
        "rev-parse",
        "--verify",
        "HEAD^{commit}",
        git_dir=checkout.git_dir,
        work_tree=checkout.path,
    )
    tree = _git_text(
        repo,
        "rev-parse",
        "--verify",
        "HEAD^{tree}",
        git_dir=checkout.git_dir,
        work_tree=checkout.path,
    )
    if head != checkout.commit or tree != checkout.tree:
        raise WorktreeError(f"checkout commit or tree mismatch: {checkout.path}")


def _checkout_is_clean(
    repo: RepositoryBinding,
    checkout: CheckoutBinding,
) -> bool:
    status = _git_bytes(
        repo,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--ignored=matching",
        "--ignore-submodules=none",
        git_dir=checkout.git_dir,
        work_tree=checkout.path,
    )
    index_entries = _git_bytes(
        repo,
        "ls-files",
        "-v",
        "-z",
        git_dir=checkout.git_dir,
        work_tree=checkout.path,
    )
    ambiguous_index = any(
        record[:1] == b"S" or record[:1].islower()
        for record in index_entries.split(b"\0")
        if record
    )
    index = _git_result(
        repo,
        "diff-index",
        "--cached",
        "--quiet",
        checkout.commit,
        "--",
        git_dir=checkout.git_dir,
        work_tree=checkout.path,
    )
    return not status and not ambiguous_index and index.returncode == 0


def _worktree_registrations(repo: RepositoryBinding) -> list[dict[str, object]]:
    raw = _git_bytes(repo, "worktree", "list", "--porcelain", "-z", "--expire=now")
    registrations = _parse_worktree_registrations(raw)
    for registration in registrations:
        path = Path(str(registration["worktree"]))
        try:
            mode = path.lstat().st_mode
        except OSError as error:
            raise WorktreeError(f"stale Git worktree registration: {path}") from error
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise WorktreeError(f"ambiguous Git worktree registration path: {path}")
        if "prunable" in registration:
            raise WorktreeError(f"stale or prunable Git worktree registration: {path}")
    return registrations


def _parse_worktree_registrations(raw: bytes) -> list[dict[str, object]]:
    registrations: list[dict[str, object]] = []
    for raw_record in raw.split(b"\0\0"):
        if not raw_record:
            continue
        record: dict[str, object] = {}
        for field in raw_record.strip(b"\0").split(b"\0"):
            key, separator, value = field.partition(b" ")
            try:
                name = key.decode("ascii")
            except UnicodeError as error:
                raise WorktreeError("invalid Git worktree registration") from error
            if name in record:
                raise WorktreeError("ambiguous Git worktree registration")
            if not separator:
                record[name] = True
            elif name == "worktree":
                record[name] = os.fsdecode(value)
            else:
                try:
                    record[name] = value.decode("utf-8")
                except UnicodeError as error:
                    raise WorktreeError("invalid Git worktree registration") from error
        if not isinstance(record.get("worktree"), str):
            raise WorktreeError("invalid Git worktree registration")
        registrations.append(record)
    return registrations


def _checkout_git_directory(repo: RepositoryBinding, path: Path) -> Path:
    git_dir = _git_directory_from_marker(path)
    try:
        contained = git_dir.is_relative_to(repo.common_dir / "worktrees")
    except ValueError:
        contained = False
    if not contained:
        raise WorktreeError(f"checkout Git directory escaped common Git data: {path}")
    return git_dir


def _git_directory_from_marker(root: Path) -> Path:
    marker = root / ".git"
    try:
        mode = marker.lstat().st_mode
    except OSError as error:
        raise WorktreeError(f"invalid Git directory association: {root}") from error
    if stat.S_ISDIR(mode):
        git_dir = marker.resolve(strict=True)
    elif stat.S_ISREG(mode):
        try:
            lines = marker.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as error:
            raise WorktreeError(f"invalid Git directory association: {root}") from error
        if len(lines) != 1 or not lines[0].startswith("gitdir: "):
            raise WorktreeError(f"invalid Git directory association: {root}")
        target = Path(lines[0].removeprefix("gitdir: "))
        if not target.is_absolute():
            target = root / target
        try:
            git_dir = target.resolve(strict=True)
        except OSError as error:
            raise WorktreeError(f"invalid Git directory association: {root}") from error
    else:
        raise WorktreeError(f"invalid Git directory association: {root}")
    _require_plain_directory(git_dir, "Git directory")
    return git_dir


def _git_text(
    repo: RepositoryBinding,
    *args: str,
    git_dir: Path | None = None,
    work_tree: Path | None = None,
) -> str:
    result = _git_result(repo, *args, git_dir=git_dir, work_tree=work_tree)
    if result.returncode != 0:
        raise WorktreeError(f"Git command failed: {' '.join(args)}: {_git_error(result)}")
    try:
        return result.stdout.decode("utf-8").strip()
    except UnicodeError as error:
        raise WorktreeError(f"Git command returned invalid UTF-8: {' '.join(args)}") from error


def _git_bytes(
    repo: RepositoryBinding,
    *args: str,
    git_dir: Path | None = None,
    work_tree: Path | None = None,
) -> bytes:
    result = _git_result(repo, *args, git_dir=git_dir, work_tree=work_tree)
    if result.returncode != 0:
        raise WorktreeError(f"Git command failed: {' '.join(args)}: {_git_error(result)}")
    return result.stdout


def _git_result(
    repo: RepositoryBinding,
    *args: str,
    git_dir: Path | None = None,
    work_tree: Path | None = None,
) -> subprocess.CompletedProcess[bytes]:
    command = [
        "git",
        "--no-replace-objects",
        f"--git-dir={git_dir or repo.git_dir}",
        f"--work-tree={work_tree or repo.root}",
    ]
    for config in _BASE_CONFIGS:
        command.extend(("-c", config))
    command.extend(args)
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=False,
            timeout=10,
            check=False,
            env=_git_environment(),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise WorktreeError(f"Git command failed: {' '.join(args)}") from error


def _git_environment() -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("GIT_", "PYTHON", "LD_", "DYLD_"))
    }
    environment["GIT_CONFIG_GLOBAL"] = os.devnull
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    return environment


def _same_path(raw: str, target: Path) -> bool:
    raw_absolute = Path(raw).absolute()
    target_absolute = target.absolute()
    if os.path.normcase(str(raw_absolute)) == os.path.normcase(str(target_absolute)):
        return True
    return os.path.normcase(str(raw_absolute.resolve(strict=False))) == os.path.normcase(
        str(target_absolute.resolve(strict=False))
    )


def _require_plain_directory(path: Path, description: str) -> None:
    try:
        mode = path.lstat().st_mode
    except OSError as error:
        raise WorktreeError(f"unable to inspect {description}: {path}") from error
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise WorktreeError(f"{description} must be a plain directory: {path}")


def _path_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise WorktreeError(f"unable to inspect worktree path: {path}") from error
    return True


def _git_error(result: subprocess.CompletedProcess[bytes]) -> str:
    detail = (result.stderr or result.stdout).decode("utf-8", errors="replace").strip()
    return detail or f"exit {result.returncode}"
