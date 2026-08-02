from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
CHECKER = REPO_ROOT / "scripts" / "check_aros_legacy_freeze.py"
FROZEN_FILES = (
    "src/coordinator/main.py",
    "src/executor/main.py",
    "src/run.py",
    "src/review.py",
)
FORBIDDEN_IMPORT_COMPONENTS = {
    "coordinator",
    "executor",
    "idea_tree",
    "orchestrator",
}
FROZEN_HELPER = """\
def normalize_legacy(value):
    stripped = value.strip()
    lowered = stripped.lower()
    replaced = lowered.replace("-", " ")
    pieces = replaced.split()
    joined = "_".join(pieces)
    return joined
"""
INDEPENDENT_UTILITY = """\
def summarize_tokens(tokens):
    filtered = [token for token in tokens if token]
    unique = set(filtered)
    pieces = sorted(unique)
    joined = "_".join(pieces)
    width = len(joined)
    if width > 80:
        joined = joined[:80]
    return joined
"""


def test_ci_legacy_freeze_uses_immutable_pull_request_base_sha() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    assert "github.event.pull_request.base.sha" in workflow
    assert 'git fetch --depth=1 --no-tags origin "$BASE_SHA"' in workflow
    assert '--base "$BASE_SHA"' in workflow
    assert "GITHUB_BASE_REF" not in workflow


def test_legacy_freeze_bounds_rename_and_copy_detection() -> None:
    checker = CHECKER.read_text(encoding="utf-8")

    assert '"--find-renames=50%"' in checker
    assert '"--find-copies=50%"' in checker
    assert '"-l1000"' in checker
    assert '"-l0"' not in checker


def _import_components(node: ast.Import | ast.ImportFrom) -> set[str]:
    components: set[str] = set()
    if isinstance(node, ast.ImportFrom) and node.module:
        components.update(node.module.split("."))
    for name in node.names:
        components.update(name.name.split("."))
    return components


def test_aros_imports_do_not_reach_legacy_control_paths() -> None:
    paths = sorted((REPO_ROOT / "src" / "aros").rglob("*.py"))
    paths.append(REPO_ROOT / "src" / "cli" / "commands" / "aros_cmd.py")
    violations: list[str] = []

    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            forbidden = _import_components(node) & FORBIDDEN_IMPORT_COMPONENTS
            if forbidden:
                relative_path = path.relative_to(REPO_ROOT)
                violations.append(
                    f"{relative_path}:{node.lineno}: {', '.join(sorted(forbidden))}"
                )

    assert not violations, "AROS imports legacy control paths:\n" + "\n".join(violations)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def legacy_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "aros-tests@example.invalid")
    _git(repo, "config", "user.name", "AROS Tests")
    _git(repo, "config", "commit.gpgsign", "false")
    for relative_path in FROZEN_FILES:
        path = repo / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("legacy one\nlegacy two\n", encoding="utf-8")
    (repo / "src" / "coordinator" / "state.bin").write_bytes(b"\x00legacy")
    (repo / "src" / "coordinator" / "empty").touch()
    (repo / "src" / "coordinator" / "helper.py").write_text(
        FROZEN_HELPER, encoding="utf-8"
    )
    (repo / ".gitignore").write_text(
        "src/coordinator/ignored.py\n"
        "src/coordinator/ignored.bin\n"
        "src/coordinator/ignored.empty\n"
        "__pycache__/\n"
        "build/\n",
        encoding="utf-8",
    )
    (repo / "README.md").write_text("outside\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "baseline")
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    return repo, base


def _run_checker(repo: Path, base: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(CHECKER),
            "--repo",
            str(repo),
            f"--base={base}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("relative_path", FROZEN_FILES)
def test_legacy_freeze_rejects_added_lines(
    legacy_repo: tuple[Path, str], relative_path: str
) -> None:
    repo, base = legacy_repo
    path = repo / relative_path
    path.write_text(path.read_text(encoding="utf-8") + "new behavior\n", encoding="utf-8")

    result = _run_checker(repo, base)

    assert result.returncode == 2
    assert relative_path in result.stdout + result.stderr


def test_legacy_freeze_rejects_binary_changes(
    legacy_repo: tuple[Path, str],
) -> None:
    repo, base = legacy_repo
    path = repo / "src" / "coordinator" / "state.bin"
    path.write_bytes(b"\x00changed")

    result = _run_checker(repo, base)

    assert result.returncode == 2
    assert "src/coordinator/state.bin" in result.stdout + result.stderr


def test_legacy_freeze_rejects_binary_change_despite_text_attribute(
    legacy_repo: tuple[Path, str],
) -> None:
    repo, base = legacy_repo
    (repo / ".gitattributes").write_text(
        "src/coordinator/state.bin diff\n", encoding="utf-8"
    )
    _git(repo, "add", ".gitattributes")
    (repo / "src" / "coordinator" / "state.bin").write_bytes(b"")

    result = _run_checker(repo, base)

    assert result.returncode == 2
    assert "src/coordinator/state.bin" in result.stdout + result.stderr


@pytest.mark.parametrize(
    ("relative_path", "content"),
    (
        ("src/coordinator/added.bin", b"\x00added"),
        ("src/coordinator/added.empty", b""),
    ),
)
def test_legacy_freeze_rejects_tracked_binary_and_empty_additions(
    legacy_repo: tuple[Path, str], relative_path: str, content: bytes
) -> None:
    repo, base = legacy_repo
    path = repo / relative_path
    path.write_bytes(content)
    _git(repo, "add", relative_path)

    result = _run_checker(repo, base)

    assert result.returncode == 2
    assert relative_path in result.stdout + result.stderr


@pytest.mark.parametrize(
    ("relative_path", "content"),
    (
        ("src/coordinator/untracked.py", b"new behavior\n"),
        ("src/coordinator/untracked.bin", b"\x00new"),
        ("src/coordinator/untracked.empty", b""),
    ),
)
def test_legacy_freeze_rejects_untracked_files(
    legacy_repo: tuple[Path, str], relative_path: str, content: bytes
) -> None:
    repo, base = legacy_repo
    (repo / relative_path).write_bytes(content)

    result = _run_checker(repo, base)

    assert result.returncode == 2
    assert relative_path in result.stdout + result.stderr


@pytest.mark.parametrize(
    ("relative_path", "content"),
    (
        ("src/coordinator/ignored.py", b"ignored behavior\n"),
        ("src/coordinator/ignored.bin", b"\x00ignored"),
        ("src/coordinator/ignored.empty", b""),
        ("src/coordinator/__pycache__/helper.pyc", b"\x00cache"),
        ("src/executor/build/generated.py", b"generated\n"),
    ),
)
def test_legacy_freeze_permits_ignored_untracked_files(
    legacy_repo: tuple[Path, str], relative_path: str, content: bytes
) -> None:
    repo, base = legacy_repo
    path = repo / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)

    result = _run_checker(repo, base)

    assert result.returncode == 0, result.stdout + result.stderr


def test_legacy_freeze_fails_closed_when_copy_detection_limit_is_hit(
    legacy_repo: tuple[Path, str],
) -> None:
    repo, _ = legacy_repo
    sources = repo / "bulk-sources"
    sources.mkdir()
    for number in range(1001):
        (sources / f"source-{number}.txt").write_text(
            f"source {number}\n", encoding="utf-8"
        )
    _git(repo, "add", "bulk-sources")
    _git(repo, "commit", "-qm", "add copy candidates")
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()

    destination = repo / "copied" / "helper.py"
    destination.parent.mkdir()
    destination.write_text(FROZEN_HELPER + "# new behavior\n", encoding="utf-8")
    candidates = repo / "bulk-destinations"
    candidates.mkdir()
    for number in range(1001):
        (candidates / f"destination-{number}.txt").write_text(
            f"destination {number}\n", encoding="utf-8"
        )
    _git(repo, "add", "copied", "bulk-destinations")

    result = _run_checker(repo, base)

    assert result.returncode == 2
    assert "too many files" in result.stdout + result.stderr


@pytest.mark.parametrize("delete_frozen_helper", (False, True))
def test_legacy_freeze_permits_independent_low_similarity_utility(
    legacy_repo: tuple[Path, str], delete_frozen_helper: bool
) -> None:
    repo, base = legacy_repo
    utility_path = repo / "src" / "independent_utility.py"
    utility_path.write_text(INDEPENDENT_UTILITY, encoding="utf-8")
    _git(repo, "add", str(utility_path.relative_to(repo)))
    if delete_frozen_helper:
        helper_path = repo / "src" / "coordinator" / "helper.py"
        helper_path.unlink()

    result = _run_checker(repo, base)

    assert result.returncode == 0, result.stdout + result.stderr


def test_legacy_freeze_rejects_rename_from_frozen_root(
    legacy_repo: tuple[Path, str],
) -> None:
    repo, base = legacy_repo
    destination = repo / "moved" / "main.py"
    destination.parent.mkdir()
    _git(repo, "mv", "src/coordinator/main.py", "moved/main.py")
    destination.write_text(
        destination.read_text(encoding="utf-8") + "new behavior\n",
        encoding="utf-8",
    )

    result = _run_checker(repo, base)

    assert result.returncode == 2
    output = result.stdout + result.stderr
    assert "src/coordinator/main.py" in output
    assert "moved/main.py" in output


def test_legacy_freeze_rejects_rename_when_git_rename_limit_is_low(
    legacy_repo: tuple[Path, str],
) -> None:
    repo, base = legacy_repo
    _git(repo, "config", "diff.renameLimit", "1")
    destination = repo / "moved" / "main.py"
    destination.parent.mkdir()
    _git(repo, "mv", "src/coordinator/main.py", "moved/main.py")
    destination.write_text(
        destination.read_text(encoding="utf-8") + "new behavior\n",
        encoding="utf-8",
    )
    for number in range(3):
        candidate = repo / "moved" / f"candidate-{number}.py"
        candidate.write_text(f"candidate {number}\n", encoding="utf-8")
        _git(repo, "add", str(candidate.relative_to(repo)))

    result = _run_checker(repo, base)

    assert result.returncode == 2
    output = result.stdout + result.stderr
    assert "src/coordinator/main.py" in output
    assert "moved/main.py" in output


def test_legacy_freeze_rejects_copy_from_frozen_root(
    legacy_repo: tuple[Path, str],
) -> None:
    repo, base = legacy_repo
    source = repo / "src" / "coordinator" / "main.py"
    destination = repo / "copied" / "main.py"
    destination.parent.mkdir()
    destination.write_bytes(source.read_bytes())
    _git(repo, "add", "copied/main.py")

    result = _run_checker(repo, base)

    assert result.returncode == 2
    output = result.stdout + result.stderr
    assert "src/coordinator/main.py" in output
    assert "copied/main.py" in output


def test_legacy_freeze_permits_deletion_only(
    legacy_repo: tuple[Path, str],
) -> None:
    repo, base = legacy_repo
    (repo / "src" / "run.py").write_text("legacy one\n", encoding="utf-8")

    result = _run_checker(repo, base)

    assert result.returncode == 0, result.stdout + result.stderr


def test_legacy_freeze_rejects_deletion_combined_with_mode_change(
    legacy_repo: tuple[Path, str],
) -> None:
    repo, base = legacy_repo
    path = repo / "src" / "run.py"
    path.write_text("legacy one\n", encoding="utf-8")
    path.chmod(0o755)

    result = _run_checker(repo, base)

    assert result.returncode == 2
    assert "src/run.py" in result.stdout + result.stderr


def test_legacy_freeze_permits_text_deletion_despite_binary_attribute(
    legacy_repo: tuple[Path, str],
) -> None:
    repo, base = legacy_repo
    (repo / ".gitattributes").write_text("src/run.py -diff\n", encoding="utf-8")
    _git(repo, "add", ".gitattributes")
    (repo / "src" / "run.py").write_text("legacy one\n", encoding="utf-8")

    result = _run_checker(repo, base)

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "relative_path",
    ("src/coordinator/state.bin", "src/coordinator/empty"),
)
def test_legacy_freeze_permits_tracked_binary_and_empty_deletions(
    legacy_repo: tuple[Path, str], relative_path: str
) -> None:
    repo, base = legacy_repo
    (repo / relative_path).unlink()

    result = _run_checker(repo, base)

    assert result.returncode == 0, result.stdout + result.stderr


def test_legacy_freeze_permits_changes_outside_frozen_roots(
    legacy_repo: tuple[Path, str],
) -> None:
    repo, base = legacy_repo
    (repo / "README.md").write_text("outside\nnew docs\n", encoding="utf-8")

    result = _run_checker(repo, base)

    assert result.returncode == 0, result.stdout + result.stderr


def test_legacy_freeze_fails_closed_for_repo_subdirectory(
    legacy_repo: tuple[Path, str],
) -> None:
    repo, base = legacy_repo
    subdirectory = repo / "nested"
    subdirectory.mkdir()

    result = _run_checker(subdirectory, base)

    assert result.returncode == 2
    assert "top level" in result.stdout + result.stderr


def test_legacy_freeze_fails_closed_for_invalid_base(
    legacy_repo: tuple[Path, str],
) -> None:
    repo, _ = legacy_repo

    result = _run_checker(repo, "not-a-valid-base")

    assert result.returncode == 2
    assert "not-a-valid-base" in result.stdout + result.stderr


def test_legacy_freeze_fails_closed_for_option_shaped_base(
    legacy_repo: tuple[Path, str],
) -> None:
    repo, _ = legacy_repo

    result = _run_checker(repo, "--quiet")

    assert result.returncode == 2
    assert "--quiet" in result.stdout + result.stderr
