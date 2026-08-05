"""Pinned Git repository and detached checkout bindings for AROS."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

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
REPOSITORY_TREE_QUERY_BATCH_SIZE = 256
MAX_REPOSITORY_TREE_QUERY_PATHS = 20_000


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
class RepositoryFile:
    """One exact regular blob read from a pinned repository commit."""

    path: str
    mode: str
    blob_oid: str
    content: bytes


@dataclass(frozen=True)
class RepositoryTreeEntry:
    path: str
    mode: str
    kind: str
    oid: str


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


def read_worktree_inventory(
    repository: RepositoryBinding,
) -> tuple[dict[str, object], ...]:
    """Return one strict read-only projection of registered Git worktrees."""
    _validate_repository_binding(repository)
    raw = _git_bytes(repository, "worktree", "list", "--porcelain", "-z")
    projected: list[dict[str, object]] = []
    for registration in _parse_worktree_registrations(raw):
        path = Path(str(registration["worktree"]))
        try:
            resolved = path.resolve(strict=True)
            mode = path.lstat().st_mode
        except OSError as error:
            raise WorktreeError(f"invalid Git worktree registration: {path}") from error
        if path != resolved or stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise WorktreeError(f"ambiguous Git worktree registration path: {path}")
        if "prunable" in registration:
            raise WorktreeError(f"stale or prunable Git worktree registration: {path}")
        head = registration.get("HEAD")
        if not isinstance(head, str) or (
            _COMMIT.fullmatch(head) is None and (not head or set(head) != {"0"})
        ):
            raise WorktreeError(f"invalid Git worktree HEAD: {path}")
        branch_ref = registration.get("branch")
        detached = registration.get("detached") is True
        if detached == isinstance(branch_ref, str):
            raise WorktreeError(f"ambiguous Git worktree branch state: {path}")
        if branch_ref is not None and (
            not isinstance(branch_ref, str)
            or not branch_ref.startswith("refs/heads/")
            or branch_ref == "refs/heads/"
        ):
            raise WorktreeError(f"invalid Git worktree branch: {path}")
        projected.append(
            {
                "path": str(resolved),
                "head": None if set(head) == {"0"} else head,
                "branch": (
                    branch_ref.removeprefix("refs/heads/")
                    if isinstance(branch_ref, str)
                    else None
                ),
                "detached": detached,
            }
        )
    if bind_repository(repository.root) != repository:
        raise WorktreeError(f"repository binding changed: {repository.root}")
    return tuple(sorted(projected, key=lambda item: str(item["path"])))


def read_repository_snapshot(repository: RepositoryBinding) -> dict[str, object]:
    """Read one exact repository HEAD/ref/branch projection without writes."""
    _validate_repository_binding(repository)
    head = _optional_git_projection(
        repository,
        "rev-parse",
        "--verify",
        "HEAD^{commit}",
        missing_returncodes=frozenset({1, 128}),
    )
    if head is not None and _COMMIT.fullmatch(head) is None:
        raise WorktreeError("repository HEAD projection is invalid")
    ref = _optional_git_projection(
        repository,
        "symbolic-ref",
        "--quiet",
        "HEAD",
        missing_returncodes=frozenset({1}),
    )
    if ref is not None and (
        not ref.startswith("refs/heads/") or ref == "refs/heads/"
    ):
        raise WorktreeError("repository ref projection is invalid")
    snapshot = {
        "repository": str(repository.root),
        "head": head,
        "ref": ref,
        "branch": ref.removeprefix("refs/heads/") if ref is not None else None,
    }
    _validate_repository_binding(repository)
    return snapshot


def resolve_repository_commit(
    repository: RepositoryBinding,
    ref: str,
) -> str:
    """Resolve one exact full Git ref to its current SHA-1 commit."""
    _validate_repository_binding(repository)
    if not isinstance(ref, str) or not ref.startswith("refs/"):
        raise WorktreeError("canonical ref must be a full Git ref")
    checked = _git_result(repository, "check-ref-format", ref)
    if checked.returncode != 0:
        raise WorktreeError(f"invalid canonical ref: {ref!r}")
    commit = _git_text(repository, "rev-parse", "--verify", f"{ref}^{{commit}}")
    if _COMMIT.fullmatch(commit) is None:
        raise WorktreeError(f"canonical ref does not resolve to a SHA-1 commit: {ref}")
    _validate_repository_binding(repository)
    return commit


def read_repository_file(
    repository: RepositoryBinding,
    commit: str,
    path: str,
) -> RepositoryFile | None:
    """Read one exact regular Git file, or ``None`` when it is absent."""
    _validate_repository_binding(repository)
    canonical = _repository_path(path).as_posix()
    _repository_commit(commit)
    entries = read_repository_tree_entries(repository, commit, (canonical,))
    entry = entries[0] if entries else None
    if entry is None:
        _validate_repository_binding(repository)
        return None
    if (
        entry.mode not in {"100644", "100755"}
        or entry.kind != "blob"
    ):
        raise WorktreeError(f"repository path is not a regular SHA-1 blob: {path}")
    content = read_repository_blob(repository, entry.oid)
    _validate_repository_binding(repository)
    return RepositoryFile(
        path=path,
        mode=entry.mode,
        blob_oid=entry.oid,
        content=content,
    )


def read_repository_blob(
    repository: RepositoryBinding,
    blob_oid: str,
) -> bytes:
    """Read and verify one exact SHA-1 blob without changing object storage."""
    _validate_repository_binding(repository)
    if not isinstance(blob_oid, str) or _COMMIT.fullmatch(blob_oid) is None:
        raise WorktreeError(f"invalid repository blob object ID: {blob_oid!r}")
    content = _git_bytes(repository, "cat-file", "blob", blob_oid)
    digest = hashlib.sha1(
        b"blob " + str(len(content)).encode("ascii") + b"\0" + content
    ).hexdigest()
    if digest != blob_oid:
        raise WorktreeError("repository blob bytes do not match object ID")
    _validate_repository_binding(repository)
    return content


def read_repository_tree_entries(
    repository: RepositoryBinding,
    commit: str,
    paths: Iterable[str],
) -> tuple[RepositoryTreeEntry, ...]:
    """Read exact literal tree entries in bounded Git query batches."""
    _validate_repository_binding(repository)
    _repository_commit(commit)
    if isinstance(paths, (str, bytes)):
        raise WorktreeError("repository tree paths must be an iterable of paths")
    requested: set[str] = set()
    for count, path in enumerate(paths, start=1):
        if count > MAX_REPOSITORY_TREE_QUERY_PATHS:
            raise WorktreeError("too many repository tree paths")
        requested.add(_repository_path(path).as_posix())
    ordered = sorted(requested)
    entries: dict[str, RepositoryTreeEntry] = {}
    for offset in range(0, len(ordered), REPOSITORY_TREE_QUERY_BATCH_SIZE):
        batch = ordered[offset : offset + REPOSITORY_TREE_QUERY_BATCH_SIZE]
        raw = _git_bytes(
            repository,
            "--literal-pathspecs",
            "ls-tree",
            "-z",
            "--full-tree",
            commit,
            "--",
            *batch,
        )
        if raw and not raw.endswith(b"\0"):
            raise WorktreeError("repository tree query is not NUL terminated")
        for record in (item for item in raw.split(b"\0") if item):
            entry = _parse_repository_tree_entry(record)
            if entry.path not in requested:
                continue
            if entry.path in entries:
                raise WorktreeError(
                    f"repository tree query returned an ambiguous path: {entry.path}"
                )
            entries[entry.path] = entry
    _validate_repository_binding(repository)
    return tuple(entries[path] for path in sorted(entries))


def find_repository_gitlink_ancestor(
    repository: RepositoryBinding,
    commit: str,
    path: str,
) -> str | None:
    """Return the first base-tree gitlink at or above one candidate path."""
    _validate_repository_binding(repository)
    candidate = _repository_path(path)
    _repository_commit(commit)
    ancestors = [
        PurePosixPath(*candidate.parts[:length]).as_posix()
        for length in range(1, len(candidate.parts) + 1)
    ]
    entries = {
        entry.path: entry
        for entry in read_repository_tree_entries(repository, commit, ancestors)
    }
    gitlink: str | None = None
    for length, ancestor in enumerate(ancestors, start=1):
        entry = entries.get(ancestor)
        if entry is None:
            continue
        if entry.mode == "160000" and entry.kind == "commit":
            gitlink = ancestor
            break
        if length < len(candidate.parts) and not (
            entry.mode == "040000" and entry.kind == "tree"
        ):
            raise WorktreeError(
                f"repository path descends through a non-tree entry: {ancestor}"
            )
    _validate_repository_binding(repository)
    return gitlink


def _repository_path(path: object) -> PurePosixPath:
    candidate = PurePosixPath(path) if isinstance(path, str) else PurePosixPath()
    if (
        not isinstance(path, str)
        or not path
        or "\x00" in path
        or "\\" in path
        or candidate.is_absolute()
        or candidate.as_posix() != path
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        raise WorktreeError(f"invalid repository file path: {path!r}")
    try:
        path.encode("utf-8")
    except UnicodeEncodeError as error:
        raise WorktreeError("repository file path must be valid UTF-8") from error
    return candidate


def _repository_commit(commit: object) -> str:
    if not isinstance(commit, str) or _COMMIT.fullmatch(commit) is None:
        raise WorktreeError(f"invalid repository commit: {commit!r}")
    return commit


def _parse_repository_tree_entry(record: bytes) -> RepositoryTreeEntry:
    header, separator, raw_path = record.partition(b"\t")
    fields = header.split(b" ")
    allowed = {
        (b"040000", b"tree"),
        (b"100644", b"blob"),
        (b"100755", b"blob"),
        (b"120000", b"blob"),
        (b"160000", b"commit"),
    }
    if (
        separator != b"\t"
        or not raw_path
        or len(fields) != 3
        or (fields[0], fields[1]) not in allowed
        or re.fullmatch(rb"[0-9a-f]{40}", fields[2]) is None
    ):
        raise WorktreeError("repository tree query returned an invalid entry")
    try:
        path = raw_path.decode("utf-8")
    except UnicodeDecodeError as error:
        raise WorktreeError("repository tree path is not UTF-8") from error
    _repository_path(path)
    return RepositoryTreeEntry(
        path=path,
        mode=fields[0].decode("ascii"),
        kind=fields[1].decode("ascii"),
        oid=fields[2].decode("ascii"),
    )


def read_candidate_status(repository: RepositoryBinding) -> dict[str, object]:
    """Read one strict candidate dirty-path projection without index refresh."""
    _validate_repository_binding(repository)
    result = _git_result(
        repository,
        "--no-optional-locks",
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--no-renames",
    )
    if result.returncode != 0:
        raise WorktreeError(f"candidate status projection failed: {_git_error(result)}")
    changes: list[dict[str, object]] = []
    for raw in (entry for entry in result.stdout.split(b"\0") if entry):
        if len(raw) < 4 or raw[2:3] != b" ":
            raise WorktreeError("candidate status projection is malformed")
        try:
            status = raw[:2].decode("ascii")
            path = raw[3:].decode("utf-8")
        except UnicodeError as error:
            raise WorktreeError("candidate status projection is not UTF-8") from error
        if not path:
            raise WorktreeError("candidate status projection has an empty path")
        changes.append(
            {
                "path": path,
                "status": status,
                "state_sha256": hashlib.sha256(
                    f"{status}\0{path}".encode("utf-8")
                ).hexdigest(),
            }
        )
    _validate_repository_binding(repository)
    ordered = sorted(
        changes,
        key=lambda item: (str(item["path"]), str(item["status"])),
    )
    return {"state": "available", "dirty": bool(ordered), "dirty_paths": ordered}


def _optional_git_projection(
    repository: RepositoryBinding,
    *args: str,
    missing_returncodes: frozenset[int],
) -> str | None:
    result = _git_result(repository, *args)
    if result.returncode in missing_returncodes:
        return None
    if result.returncode != 0:
        raise WorktreeError(f"Git projection failed: {' '.join(args)}: {_git_error(result)}")
    try:
        return result.stdout.decode("utf-8").strip()
    except UnicodeError as error:
        raise WorktreeError("Git projection returned invalid UTF-8") from error


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
    _verify_raw_tracked_bytes(repo, checkout)
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
    for checkout in (bundle.candidate, bundle.apparatus):
        _verify_raw_tracked_bytes(repo, checkout)
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


def _verify_raw_tracked_bytes(
    repo: RepositoryBinding,
    checkout: CheckoutBinding,
) -> None:
    object_format = _git_text(
        repo,
        "rev-parse",
        "--show-object-format",
        git_dir=checkout.git_dir,
        work_tree=checkout.path,
    )
    if object_format == "sha1":
        oid_length = 40
    elif object_format == "sha256":
        oid_length = 64
    else:
        raise WorktreeError(f"unsupported Git object format: {object_format}")
    raw = _git_bytes(
        repo,
        "ls-files",
        "--stage",
        "-z",
        git_dir=checkout.git_dir,
        work_tree=checkout.path,
    )
    if raw and not raw.endswith(b"\0"):
        raise WorktreeError("Git returned an ambiguous staged-file list")
    records = raw[:-1].split(b"\0") if raw else ()
    seen: set[bytes] = set()
    for record in records:
        metadata, separator, raw_path = record.partition(b"\t")
        fields = metadata.split(b" ")
        if not separator or not raw_path or len(fields) != 3:
            raise WorktreeError("invalid Git staged-file entry")
        mode, expected_oid, stage = fields
        if stage != b"0" or raw_path in seen:
            raise WorktreeError("ambiguous Git staged-file entry")
        seen.add(raw_path)
        if mode not in {b"100644", b"100755", b"120000"}:
            raise WorktreeError(f"unsupported Git staged-file mode: {mode!r}")
        if (
            len(expected_oid) != oid_length
            or any(byte not in b"0123456789abcdef" for byte in expected_oid)
            or expected_oid == b"0" * oid_length
        ):
            raise WorktreeError("invalid Git staged-file object ID")
        components = raw_path.split(b"/")
        if raw_path.startswith(b"/") or any(
            component in {b"", b".", b".."} for component in components
        ):
            raise WorktreeError("unsafe Git staged-file path")
        path = checkout.path.joinpath(
            *(os.fsdecode(component) for component in components)
        )
        if mode == b"120000":
            actual_oid = _raw_symlink_blob_oid(path, object_format)
        else:
            actual_oid = _raw_regular_blob_oid(
                path,
                object_format,
                executable=mode == b"100755",
            )
        if actual_oid.encode("ascii") != expected_oid:
            raise WorktreeError(f"checkout raw tracked bytes differ from index blob: {path}")


def _raw_regular_blob_oid(
    path: Path,
    object_format: str,
    *,
    executable: bool,
) -> str:
    try:
        before = path.lstat()
    except OSError as error:
        raise WorktreeError(f"unable to inspect raw tracked bytes: {path}") from error
    if not stat.S_ISREG(before.st_mode) or bool(before.st_mode & 0o111) is not executable:
        raise WorktreeError(f"invalid raw tracked regular file: {path}")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise WorktreeError(f"unable to open raw tracked bytes: {path}") from error
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or opened.st_size != before.st_size
        ):
            raise WorktreeError(f"raw tracked file changed while opening: {path}")
        digest = hashlib.new(object_format)
        digest.update(b"blob " + str(opened.st_size).encode("ascii") + b"\0")
        byte_count = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            byte_count += len(chunk)
            digest.update(chunk)
        after_descriptor = os.fstat(descriptor)
    except OSError as error:
        raise WorktreeError(f"unable to read raw tracked bytes: {path}") from error
    finally:
        os.close(descriptor)
    try:
        after_path = path.lstat()
    except OSError as error:
        raise WorktreeError(f"unable to revalidate raw tracked bytes: {path}") from error
    identity = (before.st_dev, before.st_ino, before.st_size)
    if (
        byte_count != before.st_size
        or not stat.S_ISREG(after_descriptor.st_mode)
        or not stat.S_ISREG(after_path.st_mode)
        or (after_descriptor.st_dev, after_descriptor.st_ino, after_descriptor.st_size)
        != identity
        or (after_path.st_dev, after_path.st_ino, after_path.st_size) != identity
        or bool(after_path.st_mode & 0o111) is not executable
    ):
        raise WorktreeError(f"raw tracked file changed while reading: {path}")
    return digest.hexdigest()


def _raw_symlink_blob_oid(path: Path, object_format: str) -> str:
    try:
        before = path.lstat()
        target = os.readlink(os.fsencode(path))
        after = path.lstat()
    except OSError as error:
        raise WorktreeError(f"unable to read raw tracked symlink: {path}") from error
    if (
        not stat.S_ISLNK(before.st_mode)
        or not stat.S_ISLNK(after.st_mode)
        or (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)
        or before.st_size != after.st_size
        or len(target) != before.st_size
    ):
        raise WorktreeError(f"raw tracked symlink changed while reading: {path}")
    digest = hashlib.new(object_format)
    digest.update(b"blob " + str(len(target)).encode("ascii") + b"\0")
    digest.update(target)
    return digest.hexdigest()


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
