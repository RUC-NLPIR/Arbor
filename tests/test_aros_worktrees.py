"""Exact Git worktree binding tests for AROS."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path

import pytest

import arbor.aros.worktrees as worktrees_module
from arbor.aros.worktrees import (
    CheckoutBinding,
    ExecutionBundle,
    RepositoryBinding,
    WorktreeError,
    WorktreeLimitError,
    bind_repository,
    create_detached_checkout,
    create_execution_bundle,
    find_repository_gitlink_ancestor,
    read_repository_blob,
    read_repository_tree_entries,
    remove_clean_checkout,
    remove_clean_execution_bundle,
    validate_detached_checkout,
    validate_execution_bundle,
)
from arbor.aros.store import json_sha256


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("GIT_", "LD_", "DYLD_"))
    }
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=check,
        capture_output=True,
        text=True,
        env=environment,
    )


def _git_bytes(root: Path, *args: str) -> bytes:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("GIT_", "LD_", "DYLD_"))
    }
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        env=environment,
    ).stdout


def _init_repository(root: Path) -> tuple[str, str]:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    _git(root, "config", "user.email", "aros@example.invalid")
    _git(root, "config", "user.name", "AROS test")
    (root / "tracked.txt").write_text("exact repository bytes\n", encoding="utf-8")
    _git(root, "add", "tracked.txt")
    _git(root, "commit", "-qm", "initial state")
    commit = _git(root, "rev-parse", "HEAD").stdout.strip()
    tree = _git(root, "show", "-s", "--format=%T", commit).stdout.strip()
    return commit, tree


def _write_oversized_packed_ref(root: Path, commit: str) -> None:
    (root / ".git" / "packed-refs").write_bytes(
        b"# pack-refs with: peeled fully-peeled sorted\n"
        + commit.encode("ascii")
        + b" refs/heads/"
        + b"x" * 5_000_000
        + b"\n"
    )


def _binding_for_checkout(
    root: Path,
    checkout: Path,
    commit: str,
) -> CheckoutBinding:
    return CheckoutBinding(
        path=checkout,
        git_dir=Path(
            _git(checkout, "rev-parse", "--absolute-git-dir").stdout.strip()
        ).resolve(strict=True),
        commit=commit,
        tree=_git(root, "show", "-s", "--format=%T", commit).stdout.strip(),
    )


def test_read_worktree_inventory_is_strict_read_only_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repository"
    commit, _tree = _init_repository(root)
    repository = bind_repository(root)
    calls: list[tuple[str, ...]] = []
    real_git_bytes = worktrees_module._git_bytes

    def recording_git_bytes(
        repo: RepositoryBinding,
        *args: str,
        **kwargs: object,
    ) -> bytes:
        calls.append(args)
        return real_git_bytes(repo, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(worktrees_module, "_git_bytes", recording_git_bytes)

    inventory = worktrees_module.read_worktree_inventory(repository)

    assert inventory == (
        {
            "path": str(root.resolve()),
            "head": commit,
            "branch": "master",
            "detached": False,
        },
    )
    assert ("worktree", "list", "--porcelain", "-z") in calls
    assert all("--expire=now" not in call for call in calls)


def test_read_repository_and_candidate_status_are_strict_projections(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repository"
    commit, _tree = _init_repository(root)
    repository = bind_repository(root)

    snapshot = worktrees_module.read_repository_snapshot(repository)
    status = worktrees_module.read_candidate_status(repository)

    assert snapshot == {
        "repository": str(root.resolve()),
        "head": commit,
        "ref": "refs/heads/master",
        "branch": "master",
    }
    assert status == {"state": "available", "dirty": False, "dirty_paths": []}


def test_repository_gitlink_ancestor_is_pinned_and_allows_new_directories(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repository"
    _init_repository(root)
    nested = root / "model" / "component"
    nested.mkdir(parents=True)
    _init_repository(nested)
    _git(root, "add", "model/component")
    _git(root, "commit", "-qm", "record gitlink")
    commit = _git(root, "rev-parse", "HEAD").stdout.strip()
    repository = bind_repository(root)

    assert find_repository_gitlink_ancestor(
        repository,
        commit,
        "model/component/CURRENT.md",
    ) == "model/component"
    assert (
        find_repository_gitlink_ancestor(
            repository,
            commit,
            "model/new/CURRENT.md",
        )
        is None
    )


def test_repository_tree_queries_batch_many_literal_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repository"
    commit, _tree = _init_repository(root)
    repository = bind_repository(root)
    requested = [f"memory/new-{index:04d}.md" for index in range(600)]
    calls: list[tuple[str, ...]] = []
    real_git_bytes = worktrees_module._git_bytes

    def recording_git_bytes(
        repo: RepositoryBinding,
        *args: str,
        **kwargs: object,
    ) -> bytes:
        if "ls-tree" in args:
            calls.append(args)
        return real_git_bytes(repo, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(worktrees_module, "_git_bytes", recording_git_bytes)

    entries = read_repository_tree_entries(repository, commit, requested)

    assert entries == ()
    assert worktrees_module.REPOSITORY_TREE_QUERY_BATCH_SIZE == 256
    assert len(calls) == 3
    assert all("--literal-pathspecs" in call for call in calls)


def test_repository_blob_limit_checks_size_before_blob_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repository"
    commit, _tree = _init_repository(root)
    repository = bind_repository(root)
    blob_oid = _git(root, "rev-parse", f"{commit}:tracked.txt").stdout.strip()
    blob_reads: list[str] = []
    real_git_bytes = worktrees_module._git_bytes

    def recording_git_bytes(
        repo: RepositoryBinding,
        *args: str,
        **kwargs: object,
    ) -> bytes:
        if args == ("cat-file", "blob", blob_oid):
            blob_reads.append(blob_oid)
        return real_git_bytes(repo, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(worktrees_module, "_git_bytes", recording_git_bytes)

    with pytest.raises(WorktreeLimitError, match="size|0"):
        read_repository_blob(repository, blob_oid, max_bytes=0)

    assert blob_reads == []
    assert read_repository_blob(repository, blob_oid) == b"exact repository bytes\n"


def test_run_git_preserves_pinned_config_and_allows_only_safe_temp_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repository"
    commit, _tree = _init_repository(root)
    repository = bind_repository(root)
    runtime = root / ".aros" / "checkpoints" / "T-safe"
    runtime.mkdir(parents=True)
    index = runtime / "index"
    monkeypatch.setenv("GIT_INDEX_FILE", str(root / ".git" / "index"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(tmp_path / "hooks"))
    calls: list[tuple[list[str], dict[str, object]]] = []
    real_run = subprocess.run

    def recording_run(
        command: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append((command, dict(kwargs)))
        return real_run(command, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(worktrees_module.subprocess, "run", recording_run)

    result = worktrees_module.run_git(
        repository,
        "read-tree",
        commit,
        index_file=index,
    )
    blob = worktrees_module.run_git(
        repository,
        "hash-object",
        "--stdin",
        input_bytes=b"binary\x00payload",
        index_file=index,
    )

    assert result.returncode == 0
    assert index.is_file()
    assert blob.returncode == 0
    assert isinstance(blob.stdout, bytes)
    command, kwargs = next(
        (recorded, options)
        for recorded, options in calls
        if "read-tree" in recorded
    )
    assert command[:4] == [
        "git",
        "--no-replace-objects",
        f"--git-dir={repository.git_dir}",
        f"--work-tree={repository.root}",
    ]
    configured = [
        command[index + 1]
        for index, value in enumerate(command[:-1])
        if value == "-c"
    ]
    assert configured == list(worktrees_module._BASE_CONFIGS)
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is False
    assert kwargs["timeout"] == 10
    assert kwargs["check"] is False
    environment = kwargs["env"]
    assert isinstance(environment, dict)
    assert environment["GIT_INDEX_FILE"] == str(index)
    assert environment["GIT_CONFIG_GLOBAL"] == os.devnull
    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
    assert "GIT_CONFIG_COUNT" not in environment
    assert "GIT_CONFIG_KEY_0" not in environment
    assert "GIT_CONFIG_VALUE_0" not in environment

    unsafe_parent = root / ".aros" / "checkpoints" / "not-a-directory"
    unsafe_parent.write_text("plain file\n", encoding="utf-8")
    symlink_parent = root / ".aros" / "checkpoints" / "linked"
    symlink_parent.symlink_to(tmp_path, target_is_directory=True)
    outside = tmp_path / "outside-index"
    for unsafe in (
        Path("relative-index"),
        root / ".git" / "index",
        root / ".aros" / "checkpoints" / "T-safe" / ".." / "escaped-index",
        unsafe_parent / "index",
        symlink_parent / "index",
        outside,
    ):
        with pytest.raises(WorktreeError, match="index|checkpoint|path|parent"):
            worktrees_module.run_git(
                repository,
                "write-tree",
                index_file=unsafe,
            )

    index.unlink()
    index.symlink_to(root / ".git" / "index")
    with pytest.raises(WorktreeError, match="index|symlink|plain"):
        worktrees_module.run_git(repository, "write-tree", index_file=index)


def test_run_git_rejects_temp_index_drift_during_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repository"
    commit, _tree = _init_repository(root)
    repository = bind_repository(root)
    runtime = root / ".aros" / "checkpoints" / "T-drift"
    runtime.mkdir(parents=True)
    index = runtime / "index"
    assert worktrees_module.run_git(
        repository,
        "read-tree",
        commit,
        index_file=index,
    ).returncode == 0
    real_git_result = worktrees_module._git_result
    replaced = False

    def replace_after_git(
        repo: RepositoryBinding,
        *args: str,
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        nonlocal replaced
        result = real_git_result(repo, *args, **kwargs)  # type: ignore[arg-type]
        if args and args[0] == "hash-object" and not replaced:
            replaced = True
            replacement = index.with_name("replacement-index")
            replacement.write_bytes(index.read_bytes())
            os.replace(replacement, index)
        return result

    monkeypatch.setattr(worktrees_module, "_git_result", replace_after_git)

    with pytest.raises(WorktreeError, match="index|exact|plain"):
        worktrees_module.run_git(
            repository,
            "hash-object",
            "--stdin",
            input_bytes=b"identity drift",
            index_file=index,
        )


def test_checkpoint_ref_transaction_checks_binding_then_fence_then_pinned_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repository"
    commit, _tree = _init_repository(root)
    repository = bind_repository(root)
    observation_ref = "refs/aros/observations/test/record"
    transaction = (
        "start\n"
        f"update refs/heads/master {commit} {commit}\n"
        f"create {observation_ref} {commit}\n"
        "prepare\n"
        "commit\n"
    ).encode("ascii")
    events: list[str] = []
    real_validate = worktrees_module._validate_repository_binding
    real_git_result = worktrees_module._git_result

    def record_binding(repo: RepositoryBinding) -> None:
        events.append("binding")
        real_validate(repo)

    def record_fence() -> None:
        events.append("fence")

    def record_git(
        repo: RepositoryBinding,
        *args: str,
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        if args == ("update-ref", "--stdin"):
            events.append("update")
            assert kwargs == {"input_bytes": transaction}
        return real_git_result(repo, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(worktrees_module, "_validate_repository_binding", record_binding)
    monkeypatch.setattr(worktrees_module, "_git_result", record_git)

    result = worktrees_module.run_checkpoint_ref_transaction(
        repository,
        input_bytes=transaction,
        validate_fence=record_fence,
    )

    assert result.returncode == 0
    assert events == ["binding", "fence", "update", "binding"]
    assert _git(root, "rev-parse", observation_ref).stdout.strip() == commit


@pytest.mark.parametrize(
    ("transaction", "fence"),
    (
        (b"", lambda: None),
        (b"x" * 1_048_577, lambda: None),
        (b"start\r\nprepare\ncommit\n", lambda: None),
        (
            b"start\nupdate refs/heads/main "
            + b"a" * 40
            + b" "
            + b"b" * 40
            + b"\nprepare\ncommit\n",
            None,
        ),
    ),
    ids=("empty", "oversized", "crlf", "missing-fence"),
)
def test_checkpoint_ref_transaction_rejects_unbounded_or_invalid_inputs(
    tmp_path: Path,
    transaction: bytes,
    fence: object,
) -> None:
    root = tmp_path / "repository"
    _init_repository(root)

    with pytest.raises(WorktreeError, match="transaction|fence|bound|input"):
        worktrees_module.run_checkpoint_ref_transaction(
            bind_repository(root),
            input_bytes=transaction,
            validate_fence=fence,  # type: ignore[arg-type]
        )


def test_read_repository_refs_snapshot_rejects_before_full_memory_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repository"
    commit, _tree = _init_repository(root)
    repository = bind_repository(root)
    _write_oversized_packed_ref(root, commit)
    real_run = subprocess.run

    def reject_full_ref_capture(
        command: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        if "for-each-ref" in command and kwargs.get("capture_output") is True:
            raise AssertionError("repository refs used unbounded subprocess capture")
        return real_run(command, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(worktrees_module.subprocess, "run", reject_full_ref_capture)

    with pytest.raises(WorktreeLimitError, match="ref.*bytes|bytes.*ref"):
        worktrees_module.read_repository_refs_snapshot(
            repository,
            max_refs=20_000,
            max_bytes=4_194_304,
        )


def test_read_repository_refs_snapshot_preserves_pinned_git_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repository"
    commit, _tree = _init_repository(root)
    repository = bind_repository(root)
    branch = _git(root, "symbolic-ref", "--short", "HEAD").stdout.strip()
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(tmp_path / "hooks"))
    real_popen = subprocess.Popen
    real_validate = worktrees_module._validate_repository_binding
    calls: list[tuple[list[str], dict[str, object]]] = []
    waits: list[float | None] = []
    validated: list[RepositoryBinding] = []

    class RecordingProcess:
        def __init__(self, process: subprocess.Popen[bytes]):
            self.process = process

        @property
        def returncode(self) -> int | None:
            return self.process.returncode

        def wait(self, timeout: float | None = None) -> int:
            waits.append(timeout)
            return self.process.wait(timeout=timeout)

        def kill(self) -> None:
            self.process.kill()

    def recording_popen(
        command: list[str],
        **kwargs: object,
    ) -> subprocess.Popen[bytes] | RecordingProcess:
        if "for-each-ref" not in command:
            return real_popen(command, **kwargs)  # type: ignore[arg-type]
        calls.append((command, dict(kwargs)))
        return RecordingProcess(real_popen(command, **kwargs))  # type: ignore[arg-type]

    def recording_validate(value: RepositoryBinding) -> None:
        validated.append(value)
        real_validate(value)

    monkeypatch.setattr(worktrees_module.subprocess, "Popen", recording_popen)
    monkeypatch.setattr(
        worktrees_module,
        "_validate_repository_binding",
        recording_validate,
    )

    snapshot = worktrees_module.read_repository_refs_snapshot(
        repository,
        max_refs=10,
        max_bytes=4_194_304,
    )

    assert snapshot == f"refs/heads/{branch}\0{commit}\n".encode("ascii")
    assert validated == [repository, repository]
    assert waits == [10]
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command[:4] == [
        "git",
        "--no-replace-objects",
        f"--git-dir={repository.git_dir}",
        f"--work-tree={repository.root}",
    ]
    configured = [
        command[index + 1]
        for index, value in enumerate(command[:-1])
        if value == "-c"
    ]
    assert configured == list(worktrees_module._BASE_CONFIGS)
    assert command[-3:] == [
        "for-each-ref",
        "--count=11",
        "--format=%(refname)%00%(objectname)",
    ]
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["stdout"] != subprocess.PIPE
    assert kwargs["stderr"] != subprocess.PIPE
    assert kwargs["text"] is False
    environment = kwargs["env"]
    assert isinstance(environment, dict)
    assert environment["GIT_CONFIG_GLOBAL"] == os.devnull
    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
    assert "GIT_CONFIG_COUNT" not in environment
    assert "GIT_CONFIG_KEY_0" not in environment
    assert "GIT_CONFIG_VALUE_0" not in environment


def test_detached_checkout_is_exact_clean_and_hermetic(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "repository"
    commit, tree = _init_repository(root)
    hooks = tmp_path / "ambient-hooks"
    hooks.mkdir()
    marker = tmp_path / "ambient-hook-ran"
    hook = hooks / "post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
    hook.chmod(0o755)
    global_config = tmp_path / "ambient.gitconfig"
    global_config.write_text(
        f"[core]\n\thooksPath = {hooks}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(hooks))
    checkout_path = root / ".worktree" / "eval" / "exact" / "candidate"

    repository = bind_repository(root)
    checkout = create_detached_checkout(repository, checkout_path, commit)

    assert repository == RepositoryBinding(
        root=root.resolve(),
        git_dir=(root / ".git").resolve(),
        common_dir=(root / ".git").resolve(),
    )
    assert checkout == CheckoutBinding(
        path=checkout_path,
        git_dir=checkout.git_dir,
        commit=commit,
        tree=tree,
    )
    assert checkout.git_dir.is_relative_to(repository.common_dir / "worktrees")
    assert _git(checkout.path, "symbolic-ref", "-q", "HEAD", check=False).returncode == 1
    assert _git(checkout.path, "rev-parse", "HEAD").stdout.strip() == commit
    assert _git(checkout.path, "show", "-s", "--format=%T", "HEAD").stdout.strip() == tree
    assert (
        _git(
            checkout.path,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignored=matching",
        ).stdout
        == ""
    )
    assert not marker.exists()


def test_checkout_pins_lf_working_bytes_against_repository_core_eol(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repository"
    _init_repository(root)
    (root / ".gitattributes").write_text("eol.txt text\n", encoding="utf-8")
    (root / "eol.txt").write_bytes(b"line-one\nline-two\n")
    _git(root, "add", ".gitattributes", "eol.txt")
    _git(root, "commit", "-qm", "add text materialization")
    commit = _git(root, "rev-parse", "HEAD").stdout.strip()
    _git(root, "config", "core.eol", "crlf")

    checkout = create_detached_checkout(
        bind_repository(root),
        root / ".worktree" / "eval" / "eol" / "candidate",
        commit,
    )

    assert (checkout.path / "eol.txt").read_bytes() == b"line-one\nline-two\n"


def test_checkout_pins_symlink_materialization_against_repository_config(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repository"
    _init_repository(root)
    link = root / "tracked-link"
    link.symlink_to("tracked.txt")
    _git(root, "add", "tracked-link")
    _git(root, "commit", "-qm", "add committed symlink")
    commit = _git(root, "rev-parse", "HEAD").stdout.strip()
    _git(root, "config", "core.symlinks", "false")

    checkout = create_detached_checkout(
        bind_repository(root),
        root / ".worktree" / "eval" / "symlink" / "candidate",
        commit,
    )

    checked_out_link = checkout.path / "tracked-link"
    assert checked_out_link.is_symlink()
    assert os.readlink(checked_out_link) == "tracked.txt"


def test_checkout_rejects_eol_converted_raw_bytes_and_preserves_registration(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repository"
    _init_repository(root)
    (root / ".gitattributes").write_text(
        "*.txt text eol=crlf\n",
        encoding="utf-8",
    )
    (root / "converted.txt").write_bytes(b"committed lf bytes\n")
    _git(root, "add", ".gitattributes", "converted.txt")
    _git(root, "commit", "-qm", "add explicit CRLF checkout")
    commit = _git(root, "rev-parse", "HEAD").stdout.strip()
    assert _git_bytes(root, "show", f"{commit}:converted.txt") == b"committed lf bytes\n"
    repository = bind_repository(root)
    checkout_path = root / ".worktree" / "eval" / "eol-conversion" / "candidate"

    with pytest.raises(WorktreeError, match="raw tracked bytes"):
        create_detached_checkout(repository, checkout_path, commit)

    converted = checkout_path / "converted.txt"
    assert converted.read_bytes() == b"committed lf bytes\r\n"
    registrations = _git(root, "worktree", "list", "--porcelain").stdout
    assert str(checkout_path) in registrations
    checkout = _binding_for_checkout(root, checkout_path, commit)
    with pytest.raises(WorktreeError, match="raw tracked bytes"):
        validate_detached_checkout(repository, checkout)
    assert converted.read_bytes() == b"committed lf bytes\r\n"
    assert _git(root, "worktree", "list", "--porcelain").stdout == registrations


def test_checkout_rejects_ident_expansion_and_preserves_registration(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repository"
    _init_repository(root)
    (root / ".gitattributes").write_text("ident.txt ident\n", encoding="utf-8")
    (root / "ident.txt").write_bytes(b"$Id$\n")
    _git(root, "add", ".gitattributes", "ident.txt")
    _git(root, "commit", "-qm", "add ident checkout")
    commit = _git(root, "rev-parse", "HEAD").stdout.strip()
    assert _git_bytes(root, "show", f"{commit}:ident.txt") == b"$Id$\n"
    repository = bind_repository(root)
    checkout_path = root / ".worktree" / "eval" / "ident-expansion" / "candidate"

    with pytest.raises(WorktreeError, match="raw tracked bytes"):
        create_detached_checkout(repository, checkout_path, commit)

    expanded = (checkout_path / "ident.txt").read_bytes()
    assert expanded.startswith(b"$Id: ") and expanded.endswith(b" $\n")
    registrations = _git(root, "worktree", "list", "--porcelain").stdout
    assert str(checkout_path) in registrations
    checkout = _binding_for_checkout(root, checkout_path, commit)
    with pytest.raises(WorktreeError, match="raw tracked bytes"):
        validate_detached_checkout(repository, checkout)
    assert (checkout_path / "ident.txt").read_bytes() == expanded
    assert _git(root, "worktree", "list", "--porcelain").stdout == registrations


@pytest.mark.parametrize("executable", (False, True))
def test_checkout_raw_blob_validation_accepts_exact_regular_file(
    tmp_path: Path,
    executable: bool,
) -> None:
    root = tmp_path / "repository"
    _init_repository(root)
    payload = b"\x00exact raw regular bytes\r\n$Id$\n"
    tracked = root / "exact.bin"
    tracked.write_bytes(payload)
    if executable:
        tracked.chmod(0o755)
    _git(root, "add", "exact.bin")
    _git(root, "commit", "-qm", "add exact regular blob")
    commit = _git(root, "rev-parse", "HEAD").stdout.strip()
    repository = bind_repository(root)

    checkout = create_detached_checkout(
        repository,
        root / ".worktree" / "eval" / f"regular-{executable}" / "candidate",
        commit,
    )

    checked_out = checkout.path / "exact.bin"
    assert checked_out.read_bytes() == payload
    assert bool(checked_out.stat().st_mode & 0o111) is executable
    assert validate_detached_checkout(repository, checkout) is None


def test_checkout_raw_blob_validation_accepts_exact_symlink(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    _init_repository(root)
    link = root / "exact-link"
    link.symlink_to("tracked.txt")
    _git(root, "add", "exact-link")
    _git(root, "commit", "-qm", "add exact symlink blob")
    commit = _git(root, "rev-parse", "HEAD").stdout.strip()
    repository = bind_repository(root)

    checkout = create_detached_checkout(
        repository,
        root / ".worktree" / "eval" / "exact-symlink" / "candidate",
        commit,
    )

    checked_out = checkout.path / "exact-link"
    assert checked_out.is_symlink()
    assert os.readlink(checked_out) == "tracked.txt"
    assert validate_detached_checkout(repository, checkout) is None


def test_checkout_rejects_hooks_filters_and_ambient_git_config(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "repository"
    _init_repository(root)
    payload = root / "payload.txt"
    payload.write_text("repository payload\n", encoding="utf-8")
    (root / ".gitattributes").write_text(
        "payload.txt filter=malicious\n",
        encoding="utf-8",
    )
    _git(root, "add", ".gitattributes", "payload.txt")
    _git(root, "commit", "-qm", "add filtered payload")
    commit = _git(root, "rev-parse", "HEAD").stdout.strip()
    hooks = root / ".git" / "malicious-hooks"
    hooks.mkdir()
    hook_marker = tmp_path / "hook-ran"
    hook = hooks / "post-checkout"
    hook.write_text(
        f"#!/bin/sh\ntouch {shlex.quote(str(hook_marker))}\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    filter_marker = tmp_path / "filter-ran"
    _git(root, "config", "core.hooksPath", str(hooks))
    _git(
        root,
        "config",
        "filter.malicious.smudge",
        f"sh -c 'touch {shlex.quote(str(filter_marker))}; cat'",
    )
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(hooks))
    checkout_path = root / ".worktree" / "eval" / "filtered" / "candidate"

    with pytest.raises(WorktreeError, match="filter"):
        create_detached_checkout(bind_repository(root), checkout_path, commit)

    assert not hook_marker.exists()
    assert not filter_marker.exists()
    assert not checkout_path.exists()


@pytest.mark.parametrize("drift", ("head", "index", "registration"))
def test_checkout_validation_rejects_head_index_or_registration_drift(
    tmp_path: Path,
    drift: str,
) -> None:
    root = tmp_path / "repository"
    commit, _tree = _init_repository(root)
    (root / "later.txt").write_text("later commit\n", encoding="utf-8")
    _git(root, "add", "later.txt")
    _git(root, "commit", "-qm", "later state")
    later_commit = _git(root, "rev-parse", "HEAD").stdout.strip()
    repository = bind_repository(root)
    checkout = create_detached_checkout(
        repository,
        root / ".worktree" / "eval" / drift / "candidate",
        commit,
    )
    tracked = checkout.path / "tracked.txt"
    original = tracked.read_bytes()
    if drift == "head":
        _git(checkout.path, "reset", "--hard", later_commit)
    elif drift == "index":
        tracked.write_text("staged index drift\n", encoding="utf-8")
        _git(checkout.path, "add", "tracked.txt")
    else:
        (checkout.git_dir / "gitdir").write_text(
            f"{tmp_path / 'different-registration' / '.git'}\n",
            encoding="utf-8",
        )

    with pytest.raises(WorktreeError, match="checkout|worktree|registration|clean"):
        validate_detached_checkout(repository, checkout)

    assert checkout.path.is_dir()
    assert (checkout.path / ".git").is_file()
    if drift != "index":
        assert tracked.read_bytes() == original
    else:
        assert tracked.read_text(encoding="utf-8") == "staged index drift\n"


def test_detached_checkout_validation_rejects_worktree_filter_before_driver(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repository"
    _init_repository(root)
    (root / ".gitattributes").write_text(
        "tracked.txt filter=runtime\n",
        encoding="utf-8",
    )
    _git(root, "add", ".gitattributes")
    _git(root, "commit", "-qm", "declare runtime filter")
    commit = _git(root, "rev-parse", "HEAD").stdout.strip()
    repository = bind_repository(root)
    checkout = create_detached_checkout(
        repository,
        root / ".worktree" / "eval" / "filter-drift" / "candidate",
        commit,
    )
    marker = tmp_path / "filter-driver-ran"
    _git(root, "config", "extensions.worktreeConfig", "true")
    _git(
        checkout.path,
        "config",
        "--worktree",
        "filter.runtime.clean",
        f"sh -c 'touch {shlex.quote(str(marker))}; cat'",
    )

    with pytest.raises(WorktreeError, match="filter"):
        validate_detached_checkout(repository, checkout)

    assert not marker.exists()


def test_remove_returns_false_and_preserves_dirty_checkout(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    commit, _tree = _init_repository(root)
    repository = bind_repository(root)
    checkout = create_detached_checkout(
        repository,
        root / ".worktree" / "eval" / "dirty" / "candidate",
        commit,
    )
    tracked = checkout.path / "tracked.txt"
    untracked = checkout.path / "untracked.txt"
    tracked.write_text("tracked dirt must survive\n", encoding="utf-8")
    untracked.write_text("untracked dirt must survive\n", encoding="utf-8")

    assert remove_clean_checkout(repository, checkout) is False

    assert tracked.read_text(encoding="utf-8") == "tracked dirt must survive\n"
    assert untracked.read_text(encoding="utf-8") == "untracked dirt must survive\n"
    assert checkout.path.is_dir()
    assert str(checkout.path) in _git(
        root,
        "worktree",
        "list",
        "--porcelain",
    ).stdout


def test_remove_rejects_ambiguous_checkout_authority(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    commit, _tree = _init_repository(root)
    repository = bind_repository(root)
    checkout = create_detached_checkout(
        repository,
        root / ".worktree" / "eval" / "ambiguous" / "candidate",
        commit,
    )
    moved = checkout.path.with_name("moved-candidate")
    _git(root, "worktree", "move", str(checkout.path), str(moved))
    tracked = moved / "tracked.txt"
    preserved = tracked.read_bytes()

    with pytest.raises(WorktreeError, match="ambiguous checkout authority"):
        remove_clean_checkout(repository, checkout)

    assert moved.is_dir()
    assert tracked.read_bytes() == preserved
    registrations = _git(root, "worktree", "list", "--porcelain").stdout
    assert str(moved) in registrations
    assert str(checkout.path) not in registrations


def test_execution_bundle_binds_candidate_and_apparatus_trees(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repository"
    candidate_commit, candidate_tree = _init_repository(root)
    (root / "apparatus.txt").write_text("apparatus revision\n", encoding="utf-8")
    _git(root, "add", "apparatus.txt")
    _git(root, "commit", "-qm", "apparatus state")
    apparatus_commit = _git(root, "rev-parse", "HEAD").stdout.strip()
    apparatus_tree = _git(
        root,
        "show",
        "-s",
        "--format=%T",
        apparatus_commit,
    ).stdout.strip()
    repository = bind_repository(root)
    bundle_root = root / ".worktree" / "eval" / "bundle"

    bundle = create_execution_bundle(
        repository,
        bundle_root,
        candidate_commit,
        apparatus_commit,
    )

    payload = {
        "candidate": {
            "path": "candidate",
            "commit": candidate_commit,
            "tree": candidate_tree,
        },
        "apparatus": {
            "path": "apparatus",
            "commit": apparatus_commit,
            "tree": apparatus_tree,
        },
        "temp": "tmp",
    }
    assert bundle == ExecutionBundle(
        root=bundle_root,
        candidate=CheckoutBinding(
            path=bundle_root / "candidate",
            git_dir=bundle.candidate.git_dir,
            commit=candidate_commit,
            tree=candidate_tree,
        ),
        apparatus=CheckoutBinding(
            path=bundle_root / "apparatus",
            git_dir=bundle.apparatus.git_dir,
            commit=apparatus_commit,
            tree=apparatus_tree,
        ),
        temp=bundle_root / "tmp",
        bundle_sha256=json_sha256(payload),
    )
    assert validate_execution_bundle(repository, bundle) is None
    for section in ("candidate", "apparatus"):
        changed = json.loads(json.dumps(payload))
        changed[section]["tree"] = "0" * 40
        assert json_sha256(changed) != bundle.bundle_sha256


@pytest.mark.parametrize("dirty_checkout", ("candidate", "apparatus"))
def test_remove_clean_execution_bundle_validates_both_before_any_removal(
    tmp_path: Path,
    dirty_checkout: str,
) -> None:
    root = tmp_path / "repository"
    candidate_commit, _tree = _init_repository(root)
    (root / "apparatus.txt").write_text("apparatus revision\n", encoding="utf-8")
    _git(root, "add", "apparatus.txt")
    _git(root, "commit", "-qm", "apparatus state")
    apparatus_commit = _git(root, "rev-parse", "HEAD").stdout.strip()
    repository = bind_repository(root)
    bundle = create_execution_bundle(
        repository,
        root / ".worktree" / "eval" / dirty_checkout,
        candidate_commit,
        apparatus_commit,
    )
    selected = getattr(bundle, dirty_checkout)
    dirt = selected.path / "untracked-dirt.txt"
    dirt.write_text(f"preserve {dirty_checkout}\n", encoding="utf-8")

    assert remove_clean_execution_bundle(repository, bundle) is False

    assert bundle.candidate.path.is_dir()
    assert bundle.apparatus.path.is_dir()
    assert dirt.read_text(encoding="utf-8") == f"preserve {dirty_checkout}\n"
    registrations = _git(root, "worktree", "list", "--porcelain").stdout
    assert str(bundle.candidate.path) in registrations
    assert str(bundle.apparatus.path) in registrations


def test_bundle_removal_reports_midpoint_failure_without_global_prune(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "repository"
    candidate_commit, _tree = _init_repository(root)
    (root / "apparatus.txt").write_text("apparatus revision\n", encoding="utf-8")
    _git(root, "add", "apparatus.txt")
    _git(root, "commit", "-qm", "apparatus state")
    apparatus_commit = _git(root, "rev-parse", "HEAD").stdout.strip()
    repository = bind_repository(root)
    bundle = create_execution_bundle(
        repository,
        root / ".worktree" / "eval" / "midpoint",
        candidate_commit,
        apparatus_commit,
    )
    real_git_result = worktrees_module._git_result
    removal_calls: list[tuple[str, ...]] = []

    class InjectedMidpointFailure(RuntimeError):
        pass

    def fail_after_candidate_removal(repo, *args, **kwargs):
        if args[:2] == ("worktree", "remove"):
            removal_calls.append(args)
            result = real_git_result(repo, *args, **kwargs)
            if args[2] == str(bundle.candidate.path):
                assert result.returncode == 0
                raise InjectedMidpointFailure("after candidate removal")
            return result
        return real_git_result(repo, *args, **kwargs)

    monkeypatch.setattr(worktrees_module, "_git_result", fail_after_candidate_removal)

    with pytest.raises(WorktreeError) as caught:
        remove_clean_execution_bundle(repository, bundle)

    detail = str(caught.value)
    assert "removed" in detail and str(bundle.candidate.path) in detail
    assert "remaining" in detail and str(bundle.apparatus.path) in detail
    assert removal_calls == [
        ("worktree", "remove", str(bundle.candidate.path)),
    ]
    assert not any("prune" in call for call in removal_calls)
    assert not bundle.candidate.path.exists()
    assert bundle.apparatus.path.is_dir()
    assert validate_detached_checkout(repository, bundle.apparatus) is None
    registrations = _git(root, "worktree", "list", "--porcelain").stdout
    assert str(bundle.candidate.path) not in registrations
    assert str(bundle.apparatus.path) in registrations
    assert bundle.temp.is_dir()
