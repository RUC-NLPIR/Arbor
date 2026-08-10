from __future__ import annotations

import ast
import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib


REPO_ROOT = Path(__file__).resolve().parents[1]
CHECKER = REPO_ROOT / "scripts" / "check_aros_legacy_freeze.py"
FROZEN_FILES = (
    "src/coordinator/main.py",
    "src/executor/main.py",
    "src/run.py",
    "src/review.py",
    "src/cli/commands/run.py",
)
ALLOWED_INTERNAL_PREFIXES = (
    "arbor.aros",
    "arbor.core",
)
ALLOWED_INTERNAL_MODULES = (
    "arbor.cli.aros_app",
    "arbor.cli.aros_start",
    "arbor.cli.commands.aros_cmd",
    "arbor.cli.user_config",
    "arbor._app",
)
SEMANTIC_LEGACY_PREFIXES = (
    "arbor.coordinator",  # includes the legacy IdeaTree module
    "arbor.executor",
    "arbor.run",
    "arbor.review",
    "arbor.cli.commands.run",
)
BOUNDARY_PATHS = (
    *sorted((REPO_ROOT / "src" / "aros").rglob("*.py")),
    REPO_ROOT / "src" / "cli" / "aros_app.py",
    REPO_ROOT / "src" / "cli" / "aros_start.py",
    REPO_ROOT / "src" / "cli" / "commands" / "aros_cmd.py",
)
WAVE1_PACKAGE_DIRS = {"arbor": "src", "arbor.skills_suite": "skills"}
RESEARCH_PROCEDURE_WAVE1_BASE = "f7702af306f9669958fe657e3f4bdd186a15e3ff"
RESEARCH_PROCEDURE_WAVE1_TESTS = {
    "tests/test_aros_architecture_boundary.py",
    "tests/test_aros_research_procedure_core.py",
}
RESEARCH_PROCEDURE_WAVE1_PLAN = (
    "docs/superpowers/plans/2026-08-09-aros-research-procedure-core.md"
)


def _assert_research_procedure_wave1_changes(name_status: str) -> None:
    for line in name_status.splitlines():
        fields = line.split("\t")
        assert len(fields) == 2, f"Wave 1 change must have one path: {line!r}"
        status_code, path = fields
        assert status_code in {"A", "M", "D"}, (
            f"Wave 1 change status must be A, M, or D: {line!r}"
        )
        assert not path.startswith("src/aros/"), (
            f"Wave 1 must not change runtime architecture: {line!r}"
        )
        assert (
            path.startswith("commissioning/research_program/")
            or path in RESEARCH_PROCEDURE_WAVE1_TESTS
            or path == RESEARCH_PROCEDURE_WAVE1_PLAN
        ), f"path is outside the research procedure Wave 1 boundary: {line!r}"


def _research_procedure_wave1_name_status(
    repository: Path, baseline: str, head: str = "HEAD"
) -> str:
    environment = os.environ.copy()
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"

    def git_output(*arguments: str) -> str:
        return subprocess.run(
            ["git", "--no-replace-objects", *arguments],
            cwd=repository,
            check=True,
            capture_output=True,
            env=environment,
            text=True,
        ).stdout

    tracked = (
        git_output(
            "diff",
            "--name-status",
            "--no-renames",
            f"{baseline}..{head}",
        )
        + git_output("diff", "--cached", "--name-status", "--no-renames")
        + git_output("diff", "--name-status", "--no-renames")
    )
    untracked = git_output("ls-files", "--others", "--exclude-standard")
    return tracked + "".join(
        f"A\t{path}\n" for path in untracked.splitlines()
    )


def _configured_package_path(package: str, package_dirs: dict[str, str]) -> Path:
    prefix = max(
        (
            candidate
            for candidate in package_dirs
            if package == candidate or package.startswith(f"{candidate}.")
        ),
        key=len,
    )
    suffix = package.removeprefix(prefix).lstrip(".").split(".")
    return (REPO_ROOT / package_dirs[prefix]).joinpath(*(part for part in suffix if part))


def _canonical_package_path(package: str) -> tuple[Path, Path]:
    if package == "arbor.skills_suite" or package.startswith("arbor.skills_suite."):
        root = REPO_ROOT / "skills"
        suffix = package.split(".")[2:]
    else:
        root = REPO_ROOT / "src"
        suffix = package.split(".")[1:]
    return root.joinpath(*suffix), root


def _plain_path_beneath(path: Path, root: Path) -> bool:
    try:
        root_real = root.resolve(strict=True)
        path_real = path.resolve(strict=True)
        path_real.relative_to(root_real)
        relative = path.relative_to(root)
        current = root
        if stat.S_ISLNK(current.lstat().st_mode):
            return False
        for part in relative.parts:
            current /= part
            if stat.S_ISLNK(current.lstat().st_mode):
                return False
    except (FileNotFoundError, OSError, ValueError):
        return False
    return True


def _tree_symlinks(root: Path) -> list[Path]:
    pending = [root]
    symlinks: list[Path] = []
    while pending:
        directory = pending.pop()
        try:
            mode = directory.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            symlinks.append(directory)
            continue
        if not stat.S_ISDIR(mode):
            continue
        for path in directory.iterdir():
            child_mode = path.lstat().st_mode
            if stat.S_ISLNK(child_mode):
                symlinks.append(path)
            elif stat.S_ISDIR(child_mode):
                pending.append(path)
    return sorted(symlinks)


def _configured_project_module_paths() -> dict[str, Path]:
    metadata = tomllib.loads(
        (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    setuptools_config = metadata["tool"]["setuptools"]
    package_dirs = setuptools_config["package-dir"]
    if package_dirs != WAVE1_PACKAGE_DIRS:
        raise ValueError(
            f"Wave 1 package-dir must be exactly {WAVE1_PACKAGE_DIRS!r}"
        )
    packages = setuptools_config["packages"]
    if not isinstance(packages, list) or any(
        not isinstance(package, str) for package in packages
    ):
        raise ValueError("tool.setuptools.packages must be a list of package names")
    for directory in package_dirs.values():
        symlinks = _tree_symlinks(REPO_ROOT / directory)
        if symlinks:
            raise ValueError(
                f"configured package tree contains symlink: "
                f"{symlinks[0].relative_to(REPO_ROOT)}"
            )
    for package in packages:
        if package != "arbor" and not package.startswith("arbor."):
            continue
        if any(not part.isidentifier() for part in package.split(".")):
            raise ValueError(f"configured package name is not canonical: {package!r}")
        actual_path = _configured_package_path(package, package_dirs)
        expected_path, physical_root = _canonical_package_path(package)
        package_init = actual_path / "__init__.py"
        if (
            actual_path != expected_path
            or not actual_path.is_dir()
            or not _plain_path_beneath(actual_path, physical_root)
            or not package_init.is_file()
            or not _plain_path_beneath(package_init, physical_root)
        ):
            raise ValueError(
                f"configured package {package!r} must exist at {expected_path}"
            )
    module_paths: dict[str, Path] = {}

    for package, directory in package_dirs.items():
        package_path = REPO_ROOT / directory
        for path in package_path.rglob("*.py"):
            if not _plain_path_beneath(path, package_path):
                raise ValueError(
                    f"configured module path is not physical: "
                    f"{path.relative_to(REPO_ROOT)}"
                )
            relative = path.relative_to(package_path)
            suffix = (
                relative.parent.parts
                if path.name == "__init__.py"
                else relative.with_suffix("").parts
            )
            if any(not part.isidentifier() for part in suffix):
                continue
            module = ".".join((package, *suffix))
            module_paths[module] = path

    return module_paths


PROJECT_MODULE_PATHS = _configured_project_module_paths()
PROJECT_MODULES = set(PROJECT_MODULE_PATHS)
DYNAMIC_IMPORT_CALL = "<dynamic import call>"
DYNAMIC_IMPORT_REFERENCES = {"__import__", "import_module"}
DYNAMIC_EXECUTION_NAMES = {
    "eval",
    "exec",
    "compile",
    "__import__",
    "import_module",
    "FunctionType",
}
BUILTIN_EXECUTION_NAMES = {"eval", "exec", "compile", "__import__"}
BUILTINS_NAMES = {"builtins", "__builtins__"}
BindingState = tuple[
    set[str],
    set[str],
    set[str],
    set[str],
    set[str],
    dict[str, str],
]
FROZEN_HELPER = """\
def normalize_legacy(value):
    stripped = value.strip()
    lowered = stripped.lower()
    replaced = lowered.replace("-", " ")
    pieces = replaced.split()
    joined = "_".join(pieces)
    return joined
"""
PADDED_FROZEN_SOURCE = "".join(
    f"legacy_step_{number} = {number}\n" for number in range(20)
)
PADDING = "".join(f"new_step_{number} = {number}\n" for number in range(30))
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


def test_eval_module_has_no_process_or_process_final_implementation() -> None:
    path = REPO_ROOT / "src" / "aros" / "eval.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    forbidden_modules = {"subprocess", "tmux", "prctl"}
    forbidden_calls = {
        "Popen",
        "killpg",
        "prctl",
        "tmux",
        "atomic_write_json",
        "final_identity",
        "_finish",
        "_record_launch_failure",
        "_write_event",
    }
    violations: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".", 1)[0] in forbidden_modules:
                    violations.append((node.lineno, f"import {alias.name}"))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.split(".", 1)[0] in forbidden_modules or module.endswith(
                ("runner", "processes")
            ):
                violations.append((node.lineno, f"from {module}"))
            if module == "runs" and {alias.name for alias in node.names} != {
                "RunService"
            }:
                violations.append((node.lineno, "Eval may import only RunService"))
        elif isinstance(node, ast.Call):
            name = _terminal_name(node.func)
            if name in forbidden_calls:
                violations.append((node.lineno, str(name)))

    assert violations == []


def test_run_runner_uses_process_seam_for_spawn_and_group_signals() -> None:
    path = REPO_ROOT / "src" / "aros" / "runner.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    forbidden_calls = {"Popen", "killpg"}
    violations = [
        (node.lineno, node.func.attr)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in forbidden_calls
    ]
    process_calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "processes"
    }

    assert violations == []
    assert {
        "enable_child_subreaper",
        "process_tree_is_live",
        "signal_process_tree",
        "spawn_process",
        "reap_leader",
    } <= process_calls


def test_run_service_uses_process_seam_for_stop_and_liveness() -> None:
    path = REPO_ROOT / "src" / "aros" / "runs.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    process_calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "processes"
    }
    direct_calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "os"
        and node.func.attr in {"kill", "killpg"}
    }

    assert "identity_is_live" in process_calls
    assert "signal_process_group" not in process_calls
    assert direct_calls == set()


def test_process_seam_has_no_lifecycle_or_domain_policy_imports() -> None:
    path = REPO_ROOT / "src" / "aros" / "processes.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    forbidden_fragments = {
        "lifecycle",
        "status",
        "receipt",
        "worktree",
        "parser",
        "science",
        "store",
    }
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append(node.module or "")

    assert not {
        module
        for module in imports
        if any(fragment in module for fragment in forbidden_fragments)
    }


def test_ci_legacy_freeze_uses_immutable_base_for_pull_requests_and_pushes() -> None:
    workflow = yaml.safe_load(
        (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
    )
    triggers = workflow[True]
    assert triggers["push"]["branches"] == ["main"]
    assert "pull_request" in triggers

    lint_job = workflow["jobs"]["lint"]
    assert "if" not in lint_job
    freeze_step = next(
        step
        for step in lint_job["steps"]
        if step.get("name") == "Enforce AROS legacy source freeze"
    )
    assert "if" not in freeze_step
    assert freeze_step["env"]["BASE_SHA"] == (
        "${{ github.event_name == 'pull_request' "
        "&& github.event.pull_request.base.sha || github.event.before }}"
    )
    assert 'git fetch --depth=1 --no-tags origin "$BASE_SHA"' in freeze_step["run"]
    assert '--base "$BASE_SHA"' in freeze_step["run"]


def test_legacy_freeze_bounds_rename_and_copy_detection() -> None:
    checker = CHECKER.read_text(encoding="utf-8")

    assert '"--find-renames=50%"' in checker
    assert '"--find-copies=50%"' in checker
    assert '"-l1000"' in checker
    assert '"-l0"' not in checker


def test_legacy_freeze_does_not_claim_a_semantic_equivalence_gate() -> None:
    checker = CHECKER.read_text(encoding="utf-8")

    assert "legacy source freeze violation" in checker
    assert "legacy semantic freeze violation" not in checker


def test_legacy_freeze_contains_no_retired_aros_forwarding_exception() -> None:
    checker = CHECKER.read_text(encoding="utf-8")

    assert "AROS_RETIREMENT_GATE_E4" not in checker


def test_aros_docs_describe_the_complete_source_growth_policy() -> None:
    for relative_path in ("README.md", "docs/aros/README.md"):
        text = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
        text = " ".join(line.lstrip("> ") for line in text.splitlines())
        text = " ".join(text.split())
        assert "all non-allowlisted legacy source paths under `src/`" in text.lower()
        assert "transitive project-import reachability" in text
        assert "configured local Python package" in text
        assert "conservative module-scope" in text
        assert "legacy source LOC may only stay level or decrease" in text
        assert "`AROS_RETIREMENT_GATE_E4`" not in text
        assert "compatibility-shim hash" not in text
        assert "semantic duplication" in text
        assert "module commissioning review" in text
        assert "padded copy" in text
        assert "`R100` move" in text
        assert "remaining entry or import" in text
        assert "not configured as a Python package" in text
        for allowed in (
            "src/aros/",
            "src/cli/aros_app.py",
            "src/cli/aros_start.py",
            "src/cli/commands/aros_cmd.py",
        ):
            assert f"`{allowed}`" in text
        assert "`src/core/` remains legacy-frozen" in text


def test_aros_boundary_indexes_configured_external_package_roots() -> None:
    assert PROJECT_MODULE_PATHS["arbor.skills_suite"] == (
        REPO_ROOT / "skills" / "__init__.py"
    )


def test_aros_boundary_resolves_configured_external_package_modules() -> None:
    assert _module_and_package(
        PROJECT_MODULE_PATHS["arbor.skills_suite"]
    ) == ("arbor.skills_suite", "arbor.skills_suite")


def test_aros_boundary_indexes_nested_modules_in_configured_external_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.setuptools]\n"
        'package-dir = { "arbor" = "src", "arbor.skills_suite" = "skills" }\n'
        'packages = ["arbor", "arbor.skills_suite", '
        '"arbor.skills_suite.legacy"]\n',
        encoding="utf-8",
    )
    source_root = tmp_path / "src"
    source_root.mkdir()
    (source_root / "__init__.py").write_text("", encoding="utf-8")
    package_root = tmp_path / "skills"
    package_root.mkdir()
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    nested = package_root / "legacy" / "bridge.py"
    nested.parent.mkdir()
    (nested.parent / "__init__.py").write_text("", encoding="utf-8")
    nested.write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "REPO_ROOT", tmp_path)

    module_paths = _configured_project_module_paths()

    assert "arbor.skills_suite.legacy.bridge" in module_paths
    assert module_paths["arbor.skills_suite.legacy.bridge"] == nested
    monkeypatch.setattr(
        sys.modules[__name__], "PROJECT_MODULE_PATHS", module_paths
    )
    monkeypatch.setattr(
        sys.modules[__name__], "PROJECT_MODULES", set(module_paths)
    )
    tree = ast.parse("import arbor.skills_suite.legacy.bridge")
    assert {
        module for _, module in _forbidden_imports(tree, "arbor.aros")
    } == {"arbor.skills_suite.legacy.bridge"}


def test_aros_boundary_rejects_source_directory_symlink_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.setuptools]\n"
        'package-dir = { "arbor" = "src", "arbor.skills_suite" = "skills" }\n'
        'packages = ["arbor", "arbor.aros", "arbor.coordinator", '
        '"arbor.skills_suite"]\n',
        encoding="utf-8",
    )
    for package_root in (
        tmp_path / "src",
        tmp_path / "src" / "aros",
        tmp_path / "src" / "coordinator",
        tmp_path / "skills",
    ):
        package_root.mkdir(parents=True, exist_ok=True)
        (package_root / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "aros" / "entry.py").write_text(
        "import arbor.aros.link.idea_tree\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "coordinator" / "idea_tree.py").write_text(
        "VALUE = 1\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "aros" / "link").symlink_to(
        "../coordinator",
        target_is_directory=True,
    )
    monkeypatch.setattr(sys.modules[__name__], "REPO_ROOT", tmp_path)

    with pytest.raises(ValueError, match=r"src/aros/link"):
        _configured_project_module_paths()


def test_aros_boundary_rejects_package_dir_aliases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.setuptools]\n"
        'package-dir = { "arbor" = "src", "arbor.skills_suite" = "skills", '
        '"arbor.core.alias" = "legacy" }\n'
        'packages = ["arbor", "arbor.core.alias", "arbor.skills_suite"]\n',
        encoding="utf-8",
    )
    for package_root in (tmp_path / "src", tmp_path / "skills", tmp_path / "legacy"):
        package_root.mkdir()
        (package_root / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "REPO_ROOT", tmp_path)

    with pytest.raises(ValueError, match="package-dir"):
        _configured_project_module_paths()


def test_aros_boundary_rejects_configured_package_without_init(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.setuptools]\n"
        'package-dir = { "arbor" = "src", "arbor.skills_suite" = "skills" }\n'
        'packages = ["arbor", "arbor.core.missing", "arbor.skills_suite"]\n',
        encoding="utf-8",
    )
    for package_root in (tmp_path / "src", tmp_path / "skills"):
        package_root.mkdir()
        (package_root / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "core" / "missing").mkdir(parents=True)
    monkeypatch.setattr(sys.modules[__name__], "REPO_ROOT", tmp_path)

    with pytest.raises(ValueError, match=r"arbor\.core\.missing"):
        _configured_project_module_paths()


def test_aros_boundary_rejects_noncanonical_package_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.setuptools]\n"
        'package-dir = { "arbor" = "src", "arbor.skills_suite" = "skills" }\n'
        'packages = ["arbor", "arbor...legacy", "arbor.skills_suite"]\n',
        encoding="utf-8",
    )
    for package_root in (
        tmp_path / "src",
        tmp_path / "src" / "legacy",
        tmp_path / "skills",
    ):
        package_root.mkdir(parents=True, exist_ok=True)
        (package_root / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "REPO_ROOT", tmp_path)

    with pytest.raises(ValueError, match=r"arbor\.\.\.legacy"):
        _configured_project_module_paths()


def _module_and_package(path: Path) -> tuple[str, str]:
    module = next(
        (
            name
            for name, module_path in PROJECT_MODULE_PATHS.items()
            if module_path == path
        ),
        None,
    )
    if module is None:
        raise ValueError(f"path is not a configured project module: {path}")
    if path.name == "__init__.py":
        return module, module
    return module, module.rpartition(".")[0]


def _is_project_module(module: str) -> bool:
    return module in PROJECT_MODULES


def _is_logically_allowed_internal(module: str) -> bool:
    return (
        any(
            module == allowed or module.startswith(f"{allowed}.")
            for allowed in ALLOWED_INTERNAL_PREFIXES
        )
        or module in ALLOWED_INTERNAL_MODULES
        or any(
            allowed.startswith(f"{module}.")
            for allowed in (*ALLOWED_INTERNAL_PREFIXES, *ALLOWED_INTERNAL_MODULES)
        )
    )


def _canonical_module_paths(module: str) -> set[Path]:
    suffix = module.split(".")[1:]
    stem = (REPO_ROOT / "src").joinpath(*suffix)
    if not suffix:
        return {REPO_ROOT / "src" / "__init__.py"}
    return {stem.with_suffix(".py"), stem / "__init__.py"}


def _allowed_module_root(module: str) -> Path:
    if module == "arbor.aros" or module.startswith("arbor.aros."):
        return REPO_ROOT / "src" / "aros"
    if module == "arbor.core" or module.startswith("arbor.core."):
        return REPO_ROOT / "src" / "core"
    return REPO_ROOT / "src"


def _is_allowed_internal(module: str) -> bool:
    if not _is_logically_allowed_internal(module):
        return False
    path = PROJECT_MODULE_PATHS.get(module)
    if (
        path is None
        or not path.is_file()
        or not stat.S_ISREG(path.lstat().st_mode)
        or not _plain_path_beneath(path, _allowed_module_root(module))
    ):
        return False
    exact_paths = {
        "arbor.cli.aros_app": REPO_ROOT / "src" / "cli" / "aros_app.py",
        "arbor.cli.aros_start": REPO_ROOT / "src" / "cli" / "aros_start.py",
        "arbor.cli.commands.aros_cmd": (
            REPO_ROOT / "src" / "cli" / "commands" / "aros_cmd.py"
        ),
        "arbor.cli.user_config": REPO_ROOT / "src" / "cli" / "user_config.py",
        "arbor._app": REPO_ROOT / "src" / "_app.py",
    }
    if module in exact_paths:
        return path == exact_paths[module]
    return path in _canonical_module_paths(module)


def _project_module_with_packages(module: str) -> list[str]:
    modules = [module]
    parent = module
    while "." in parent:
        parent = parent.rpartition(".")[0]
        if parent in PROJECT_MODULES:
            modules.append(parent)
    return modules


def _resolve_import_from(node: ast.ImportFrom, package: str) -> str:
    if not node.level:
        return node.module or ""
    relative = "." * node.level + (node.module or "")
    return importlib.util.resolve_name(relative, package)


def _static_imports(tree: ast.AST, package: str) -> list[tuple[int, str]]:
    imports: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend((node.lineno, name.name) for name in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_import_from(node, package)
            if base:
                imports.append((node.lineno, base))
            for name in node.names:
                candidate = f"{base}.{name.name}" if base else name.name
                if _is_project_module(candidate):
                    imports.append((node.lineno, candidate))
    return imports


class _ModuleImportVisitor(ast.NodeVisitor):
    def __init__(self, package: str) -> None:
        self.package = package
        self.imports: list[tuple[int, str]] = []

    def visit_Import(self, node: ast.Import) -> None:
        self.imports.extend((node.lineno, name.name) for name in node.names)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        base = _resolve_import_from(node, self.package)
        if base:
            self.imports.append((node.lineno, base))
        for name in node.names:
            candidate = f"{base}.{name.name}" if base else name.name
            if _is_project_module(candidate):
                self.imports.append((node.lineno, candidate))

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        pass

    visit_AsyncFunctionDef = visit_FunctionDef
    visit_Lambda = visit_FunctionDef


def _module_static_imports(tree: ast.AST, package: str) -> list[tuple[int, str]]:
    visitor = _ModuleImportVisitor(package)
    visitor.visit(tree)
    return visitor.imports


def _terminal_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_importlib_metadata(module: str) -> bool:
    return module == "importlib.metadata" or module.startswith("importlib.metadata.")


def _literal_string(node: ast.AST, bindings: dict[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return bindings.get(node.id)
    if isinstance(node, ast.NamedExpr):
        return _literal_string(node.value, bindings)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _literal_string(node.left, bindings)
        right = _literal_string(node.right, bindings)
        if left is not None and right is not None:
            return left + right
    return None


class _DynamicImportVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.violations: list[tuple[int, str]] = []
        self.dynamic_import_names = set(DYNAMIC_IMPORT_REFERENCES)
        self.execution_names = set(DYNAMIC_EXECUTION_NAMES)
        self.builtins_names = set(BUILTINS_NAMES)
        self.importlib_names = {"importlib"}
        self.types_names = {"types"}
        self.string_bindings: dict[str, str] = {}
        self.class_outer_state: BindingState | None = None

    def _mark(self, node: ast.AST) -> None:
        self.violations.append((node.lineno, DYNAMIC_IMPORT_CALL))

    def _snapshot(self) -> BindingState:
        return (
            set(self.dynamic_import_names),
            set(self.execution_names),
            set(self.builtins_names),
            set(self.importlib_names),
            set(self.types_names),
            dict(self.string_bindings),
        )

    def _restore(self, state: BindingState) -> None:
        (
            dynamic_import_names,
            execution_names,
            builtins_names,
            importlib_names,
            types_names,
            string_bindings,
        ) = state
        self.dynamic_import_names = set(dynamic_import_names)
        self.execution_names = set(execution_names)
        self.builtins_names = set(builtins_names)
        self.importlib_names = set(importlib_names)
        self.types_names = set(types_names)
        self.string_bindings = dict(string_bindings)

    def _restore_name(self, name: str, state: BindingState) -> None:
        for names in (
            self.dynamic_import_names,
            self.execution_names,
            self.builtins_names,
            self.importlib_names,
            self.types_names,
        ):
            names.discard(name)
        self.string_bindings.pop(name, None)
        for current, previous in zip(
            (
                self.dynamic_import_names,
                self.execution_names,
                self.builtins_names,
                self.importlib_names,
                self.types_names,
            ),
            state[:5],
        ):
            if name in previous:
                current.add(name)
        if name in state[5]:
            self.string_bindings[name] = state[5][name]

    def _clear_binding(self, name: str) -> None:
        for names in (
            self.dynamic_import_names,
            self.execution_names,
            self.builtins_names,
            self.importlib_names,
            self.types_names,
        ):
            names.discard(name)
        self.string_bindings.pop(name, None)
        if name in DYNAMIC_IMPORT_REFERENCES:
            self.dynamic_import_names.add(name)
        if name in BUILTINS_NAMES:
            self.builtins_names.add(name)
        if name == "importlib":
            self.importlib_names.add(name)
        if name == "types":
            self.types_names.add(name)

    def _is_execution_expression(self, value: ast.AST) -> bool:
        if isinstance(value, ast.Name):
            return value.id in self.execution_names
        if isinstance(value, ast.Attribute):
            return (
                (
                    value.attr in BUILTIN_EXECUTION_NAMES
                    and _terminal_name(value.value) in self.builtins_names
                )
                or (
                    value.attr == "import_module"
                    and _terminal_name(value.value) in self.importlib_names
                )
                or (
                    value.attr == "FunctionType"
                    and _terminal_name(value.value) in self.types_names
                )
            )
        if isinstance(value, ast.Subscript):
            key = _literal_string(value.slice, self.string_bindings)
            return key in DYNAMIC_EXECUTION_NAMES
        if isinstance(value, ast.Call) and _terminal_name(value.func) == "getattr":
            attribute = (
                _literal_string(value.args[1], self.string_bindings)
                if len(value.args) >= 2
                else None
            )
            namespace = _terminal_name(value.args[0]) if value.args else None
            return (
                namespace in self.builtins_names
                and attribute in BUILTIN_EXECUTION_NAMES
            ) or (
                namespace in self.importlib_names and attribute == "import_module"
            ) or (namespace in self.types_names and attribute == "FunctionType")
        return False

    def _bind(self, name: str, value: ast.AST) -> None:
        source_name = _terminal_name(value)
        literal = _literal_string(value, self.string_bindings)
        is_dynamic_import = source_name in self.dynamic_import_names
        is_execution = self._is_execution_expression(value)
        is_builtins = source_name in self.builtins_names
        is_importlib = source_name in self.importlib_names
        is_types = source_name in self.types_names
        self._clear_binding(name)
        if literal is not None:
            self.string_bindings[name] = literal
        if is_dynamic_import:
            self.dynamic_import_names.add(name)
        if is_execution:
            self.execution_names.add(name)
        if is_builtins:
            self.builtins_names.add(name)
        if is_importlib:
            self.importlib_names.add(name)
        if is_types:
            self.types_names.add(name)

    def visit_Import(self, node: ast.Import) -> None:
        for imported in node.names:
            bound = imported.asname or imported.name.split(".", 1)[0]
            self._clear_binding(bound)
            if imported.name == "importlib":
                self.importlib_names.add(bound)
                self._mark(node)
            elif imported.name.startswith("importlib."):
                if not _is_importlib_metadata(imported.name):
                    self._mark(node)
                if imported.asname is None:
                    self.importlib_names.add("importlib")
            elif imported.name == "builtins":
                self.builtins_names.add(bound)
            elif imported.name == "types":
                self.types_names.add(bound)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for imported in node.names:
            bound = imported.asname or imported.name
            self._clear_binding(bound)
            if node.level == 0 and node.module == "importlib":
                if imported.name == "import_module":
                    self.dynamic_import_names.add(bound)
                    self._mark(node)
                elif imported.name != "metadata":
                    self._mark(node)
            elif (
                node.level == 0
                and node.module is not None
                and node.module.startswith("importlib.")
                and not _is_importlib_metadata(node.module)
            ):
                self._mark(node)
            elif node.level == 0 and node.module == "builtins":
                if imported.name in DYNAMIC_IMPORT_REFERENCES:
                    self.dynamic_import_names.add(bound)
                if imported.name in DYNAMIC_EXECUTION_NAMES:
                    self.execution_names.add(bound)
            elif (
                node.level == 0
                and node.module == "types"
                and imported.name == "FunctionType"
            ):
                self.execution_names.add(bound)

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        for target in node.targets:
            self._bind_target(target, node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.visit(node.annotation)
        if node.value is not None:
            self.visit(node.value)
            if isinstance(node.target, ast.Name):
                self._bind(node.target.id, node.value)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.visit(node.value)
        if isinstance(node.target, ast.Name):
            self._bind(node.target.id, node.value)

    def _clear_target(self, target: ast.AST) -> None:
        if isinstance(target, ast.Name):
            self._clear_binding(target.id)
        elif isinstance(target, (ast.List, ast.Tuple)):
            for item in target.elts:
                self._clear_target(item)

    def _target_names(self, target: ast.AST) -> list[str]:
        if isinstance(target, ast.Name):
            return [target.id]
        if isinstance(target, (ast.List, ast.Tuple)):
            return [name for item in target.elts for name in self._target_names(item)]
        return []

    def _bind_target(self, target: ast.AST, value: ast.AST) -> None:
        if isinstance(target, ast.Name):
            self._bind(target.id, value)
        elif (
            isinstance(target, (ast.List, ast.Tuple))
            and isinstance(value, (ast.List, ast.Tuple))
            and len(target.elts) == len(value.elts)
        ):
            for target_item, value_item in zip(target.elts, value.elts):
                self._bind_target(target_item, value_item)
        else:
            self._clear_target(target)

    def visit_For(self, node: ast.For) -> None:
        saved = self._snapshot()
        self.visit(node.iter)
        self._clear_target(node.target)
        for statement in node.body:
            self.visit(statement)
        for name in self._target_names(node.target):
            self._restore_name(name, saved)
        for statement in node.orelse:
            self.visit(statement)

    visit_AsyncFor = visit_For

    def visit_With(self, node: ast.With) -> None:
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars is not None:
                self._clear_target(item.optional_vars)
        for statement in node.body:
            self.visit(statement)

    visit_AsyncWith = visit_With

    def _visit_comprehension(
        self,
        generators: list[ast.comprehension],
        expressions: list[ast.expr],
    ) -> None:
        saved = self._snapshot()
        for generator in generators:
            self.visit(generator.iter)
            if isinstance(generator.iter, (ast.List, ast.Tuple)) and len(generator.iter.elts) == 1:
                self._bind_target(generator.target, generator.iter.elts[0])
            else:
                self._clear_target(generator.target)
            for condition in generator.ifs:
                self.visit(condition)
        for expression in expressions:
            self.visit(expression)
        self._restore(saved)

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._visit_comprehension(node.generators, [node.elt])

    visit_SetComp = visit_ListComp
    visit_GeneratorExp = visit_ListComp

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._visit_comprehension(node.generators, [node.key, node.value])

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.type is not None:
            self.visit(node.type)
        saved = self._snapshot()
        if node.name is not None:
            self._clear_binding(node.name)
        for statement in node.body:
            self.visit(statement)
        if node.name is not None:
            self._restore_name(node.name, saved)

    def visit_Call(self, node: ast.Call) -> None:
        attribute = (
            _literal_string(node.args[1], self.string_bindings)
            if len(node.args) >= 2
            else None
        )
        namespace = _terminal_name(node.args[0]) if node.args else None
        protected_getattr = _terminal_name(node.func) == "getattr" and (
            (
                namespace in self.builtins_names
                and attribute in BUILTIN_EXECUTION_NAMES
            )
            or (namespace in self.importlib_names and attribute == "import_module")
            or (namespace in self.types_names and attribute == "FunctionType")
        )
        direct_execution = (
            isinstance(node.func, ast.Name) and node.func.id in self.execution_names
        )
        protected_attribute = isinstance(node.func, ast.Attribute) and (
            (
                node.func.attr in BUILTIN_EXECUTION_NAMES
                and _terminal_name(node.func.value) in self.builtins_names
            )
            or (
                node.func.attr == "import_module"
                and _terminal_name(node.func.value) in self.importlib_names
            )
            or (
                node.func.attr == "FunctionType"
                and _terminal_name(node.func.value) in self.types_names
            )
        )
        lookup_key = (
            _literal_string(node.func.slice, self.string_bindings)
            if isinstance(node.func, ast.Subscript)
            else None
        )
        if (
            protected_getattr
            or direct_execution
            or protected_attribute
            or lookup_key in DYNAMIC_EXECUTION_NAMES
        ):
            self._mark(node)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load) and node.id in self.dynamic_import_names:
            self._mark(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in self.dynamic_import_names:
            self._mark(node)
        self.generic_visit(node)

    def _visit_scoped(
        self,
        body: list[ast.stmt],
        parameters: list[str],
        *,
        base_state: BindingState | None = None,
    ) -> None:
        saved = self._snapshot()
        if base_state is not None:
            self._restore(base_state)
        for parameter in parameters:
            self._clear_binding(parameter)
        for statement in body:
            self.visit(statement)
        self._restore(saved)

    def _visit_scoped_expression(
        self,
        expression: ast.expr,
        parameters: list[str],
        *,
        base_state: BindingState | None = None,
    ) -> None:
        saved = self._snapshot()
        if base_state is not None:
            self._restore(base_state)
        for parameter in parameters:
            self._clear_binding(parameter)
        self.visit(expression)
        self._restore(saved)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self.visit(default)
        arguments = [
            *node.args.posonlyargs,
            *node.args.args,
            *node.args.kwonlyargs,
        ]
        if node.args.vararg is not None:
            arguments.append(node.args.vararg)
        if node.args.kwarg is not None:
            arguments.append(node.args.kwarg)
        for argument in arguments:
            if argument.annotation is not None:
                self.visit(argument.annotation)
        if node.returns is not None:
            self.visit(node.returns)
        parameters = [argument.arg for argument in arguments]
        is_method = self.class_outer_state is not None
        body_base = self.class_outer_state or self._snapshot()
        class_outer_state = self.class_outer_state
        self.class_outer_state = None
        self._visit_scoped(
            node.body,
            parameters if is_method else [*parameters, node.name],
            base_state=body_base,
        )
        self.class_outer_state = class_outer_state
        self._clear_binding(node.name)

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    def visit_Lambda(self, node: ast.Lambda) -> None:
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self.visit(default)
        parameters = [
            argument.arg
            for argument in (
                *node.args.posonlyargs,
                *node.args.args,
                *node.args.kwonlyargs,
            )
        ]
        if node.args.vararg is not None:
            parameters.append(node.args.vararg.arg)
        if node.args.kwarg is not None:
            parameters.append(node.args.kwarg.arg)
        body_base = self.class_outer_state or self._snapshot()
        class_outer_state = self.class_outer_state
        self.class_outer_state = None
        self._visit_scoped_expression(
            node.body,
            parameters,
            base_state=body_base,
        )
        self.class_outer_state = class_outer_state

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for base in node.bases:
            self.visit(base)
        for keyword in node.keywords:
            self.visit(keyword.value)
        method_outer_state = self.class_outer_state or self._snapshot()
        class_outer_state = self.class_outer_state
        self.class_outer_state = method_outer_state
        self._visit_scoped(node.body, [])
        self.class_outer_state = class_outer_state
        self._clear_binding(node.name)


def _dynamic_imports(tree: ast.AST) -> list[tuple[int, str]]:
    visitor = _DynamicImportVisitor()
    visitor.visit(tree)
    return visitor.violations


def _forbidden_imports(tree: ast.AST, package: str) -> list[tuple[int, str]]:
    return [
        (line, module)
        for line, module in (*_static_imports(tree, package), *_dynamic_imports(tree))
        if module == DYNAMIC_IMPORT_CALL
        or (_is_project_module(module) and not _is_allowed_internal(module))
    ]


def _transitive_static_import_violations() -> list[str]:
    entry_modules = {_module_and_package(path)[0] for path in BOUNDARY_PATHS}
    pending = [
        module
        for path in BOUNDARY_PATHS
        for module in _project_module_with_packages(_module_and_package(path)[0])
    ]
    visited: set[str] = set()
    violations: list[str] = []

    while pending:
        module = pending.pop()
        if module in visited:
            continue
        visited.add(module)
        path = PROJECT_MODULE_PATHS[module]
        if _is_logically_allowed_internal(module) and not _is_allowed_internal(module):
            violations.append(
                f"{path.relative_to(REPO_ROOT)}: invalid physical mapping for {module}"
            )
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        _, package = _module_and_package(path)
        imports = (
            _static_imports(tree, package)
            if module in entry_modules
            else _module_static_imports(tree, package)
        )
        for line, imported in imports:
            if not _is_project_module(imported):
                continue
            if not _is_allowed_internal(imported):
                violations.append(
                    f"{path.relative_to(REPO_ROOT)}:{line}: {imported}"
                )
            pending.extend(_project_module_with_packages(imported))

    return list(dict.fromkeys(violations))


def test_aros_imports_only_use_the_one_way_internal_boundary() -> None:
    """Enforce reviewable architecture discipline; this is not a sandbox."""
    violations = _transitive_static_import_violations()

    for path in BOUNDARY_PATHS:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for line, module in _dynamic_imports(tree):
            violations.append(f"{path.relative_to(REPO_ROOT)}:{line}: {module}")

    assert not violations, "AROS imports cross the one-way boundary:\n" + "\n".join(violations)


def _install_synthetic_project_graph(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sources: dict[str, tuple[Path, str]],
    entry_module: str,
) -> None:
    for path, content in sources.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    monkeypatch.setattr(sys.modules[__name__], "REPO_ROOT", tmp_path)
    monkeypatch.setattr(
        sys.modules[__name__],
        "BOUNDARY_PATHS",
        (sources[entry_module][0],),
    )
    monkeypatch.setattr(sys.modules[__name__], "PROJECT_MODULES", set(sources))
    monkeypatch.setattr(
        sys.modules[__name__],
        "PROJECT_MODULE_PATHS",
        {module: path for module, (path, _) in sources.items()},
        raising=False,
    )


def test_aros_boundary_rejects_transitive_config_resolve_coordinator_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = tmp_path / "src"
    sources = {
        "arbor.aros.entry": (
            source_root / "aros" / "entry.py",
            "from ..core import config_resolve\n",
        ),
        "arbor.core.config_resolve": (
            source_root / "core" / "config_resolve.py",
            "from ..coordinator import main\n",
        ),
        "arbor.coordinator.main": (
            source_root / "coordinator" / "main.py",
            "VALUE = 1\n",
        ),
    }
    _install_synthetic_project_graph(
        tmp_path,
        monkeypatch,
        sources,
        "arbor.aros.entry",
    )

    with pytest.raises(AssertionError, match=r"arbor\.coordinator\.main"):
        test_aros_imports_only_use_the_one_way_internal_boundary()


def test_aros_boundary_rejects_allowed_module_relabelled_to_legacy_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sources = {
        "arbor.aros.entry": (
            tmp_path / "src" / "aros" / "entry.py",
            "from ..core import alias\n",
        ),
        "arbor.core.alias": (
            tmp_path / "legacy" / "alias.py",
            "VALUE = 1\n",
        ),
    }
    _install_synthetic_project_graph(
        tmp_path,
        monkeypatch,
        sources,
        "arbor.aros.entry",
    )

    with pytest.raises(AssertionError, match=r"arbor\.core\.alias"):
        test_aros_imports_only_use_the_one_way_internal_boundary()


def test_aros_boundary_rejects_boundary_entry_relabelled_to_legacy_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sources = {
        "arbor.aros.entry": (
            tmp_path / "legacy" / "entry.py",
            "VALUE = 1\n",
        ),
    }
    _install_synthetic_project_graph(
        tmp_path,
        monkeypatch,
        sources,
        "arbor.aros.entry",
    )

    with pytest.raises(AssertionError, match=r"arbor\.aros\.entry"):
        test_aros_imports_only_use_the_one_way_internal_boundary()


def test_aros_boundary_rejects_allowed_module_symlinked_outside_physical_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alias = tmp_path / "src" / "core" / "alias.py"
    sources = {
        "arbor.aros.entry": (
            tmp_path / "src" / "aros" / "entry.py",
            "from ..core import alias\n",
        ),
        "arbor.core.alias": (alias, "VALUE = 1\n"),
    }
    _install_synthetic_project_graph(
        tmp_path,
        monkeypatch,
        sources,
        "arbor.aros.entry",
    )
    outside = tmp_path / "legacy" / "alias.py"
    outside.parent.mkdir()
    outside.write_text("VALUE = 1\n", encoding="utf-8")
    alias.unlink()
    alias.symlink_to(outside)

    with pytest.raises(AssertionError, match=r"arbor\.core\.alias"):
        test_aros_imports_only_use_the_one_way_internal_boundary()


def test_aros_boundary_does_not_scan_core_dynamic_provider_internals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = tmp_path / "src"
    sources = {
        "arbor.aros.entry": (
            source_root / "aros" / "entry.py",
            "from ..core import provider\n",
        ),
        "arbor.core.provider": (
            source_root / "core" / "provider.py",
            "import importlib\n\n"
            "def load(name):\n"
            "    return importlib.import_module(name)\n",
        ),
    }
    _install_synthetic_project_graph(
        tmp_path,
        monkeypatch,
        sources,
        "arbor.aros.entry",
    )

    test_aros_imports_only_use_the_one_way_internal_boundary()


def test_aros_boundary_conservatively_scans_module_scope_branches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = tmp_path / "src"
    sources = {
        "arbor.aros.entry": (
            source_root / "aros" / "entry.py",
            "from ..core import provider\n",
        ),
        "arbor.core.provider": (
            source_root / "core" / "provider.py",
            "if False:\n"
            "    from ..coordinator import main\n",
        ),
        "arbor.coordinator.main": (
            source_root / "coordinator" / "main.py",
            "VALUE = 1\n",
        ),
    }
    _install_synthetic_project_graph(
        tmp_path,
        monkeypatch,
        sources,
        "arbor.aros.entry",
    )

    with pytest.raises(AssertionError, match=r"arbor\.coordinator\.main"):
        test_aros_imports_only_use_the_one_way_internal_boundary()


@pytest.mark.parametrize(
    ("source", "expected"),
    (
        ("from .. import run", {"arbor.run"}),
        ("from arbor import executor", {"arbor.executor"}),
        ("import importlib as loader\nloader.import_module('arbor.review')", {DYNAMIC_IMPORT_CALL}),
        ("import importlib.util\nimportlib.import_module('arbor.review')", {DYNAMIC_IMPORT_CALL}),
        ("from importlib import import_module as load\nload('arbor.coordinator.main')", {DYNAMIC_IMPORT_CALL}),
        ("__import__('arbor.mcp.server')", {DYNAMIC_IMPORT_CALL}),
        ("module = 'arbor.review'\n__import__(module)", {DYNAMIC_IMPORT_CALL}),
        ("__import__('run', globals(), locals(), (), 2)", {DYNAMIC_IMPORT_CALL}),
        ("import builtins as b\nb.__import__('arbor.review')", {DYNAMIC_IMPORT_CALL}),
        ("import importlib\nimportlib.import_module('arbor.aros.runs')", {DYNAMIC_IMPORT_CALL}),
        ("target = 'arbor.review'\nimport_module(target)", {DYNAMIC_IMPORT_CALL}),
        ("target = 'arbor.review'\nloader.import_module(target)", {DYNAMIC_IMPORT_CALL}),
        ("from importlib import import_module as load\ndef later(target):\n    return load(target)", {DYNAMIC_IMPORT_CALL}),
        ("import importlib\nload = importlib.import_module\ndef later():\n    return load('arbor.review')", {DYNAMIC_IMPORT_CALL}),
        ("import importlib\nload = getattr(importlib, 'import_module')", {DYNAMIC_IMPORT_CALL}),
        ("import importlib", {DYNAMIC_IMPORT_CALL}),
        ("import importlib.util as util", {DYNAMIC_IMPORT_CALL}),
        ("from importlib.metadata import version", set()),
        ("import importlib.metadata", set()),
        ("import importlib.metadata_loader", {DYNAMIC_IMPORT_CALL}),
        ("from importlib import metadata", set()),
        ("from importlib import util", {DYNAMIC_IMPORT_CALL}),
        ("from importlib import import_module as load", {DYNAMIC_IMPORT_CALL}),
        ("load = __import__", {DYNAMIC_IMPORT_CALL}),
        ("name = '__import__'", set()),
        ("name = 'import_module'", set()),
        ("import builtins\ngetattr(builtins, 'exec')", {DYNAMIC_IMPORT_CALL}),
        ("name = 'eval'", set()),
        ("model.eval()", set()),
        ("runner = model.eval\nrunner()", set()),
        ("eval(source)", {DYNAMIC_IMPORT_CALL}),
        ("exec(source)", {DYNAMIC_IMPORT_CALL}),
        ("import builtins", set()),
        ("from builtins import open", set()),
        ("def load(builtins):\n    return getattr(builtins, '__im' + 'port__')", {DYNAMIC_IMPORT_CALL}),
        ("reference = __builtins__", set()),
        ("reference = runtime.__builtins__", set()),
        ("getattr(runtime.builtins, attribute)", set()),
        ("getattr(builtins, 'open')", set()),
        ("name: str = 'ev' + 'al'\ngetattr(builtins, name)", {DYNAMIC_IMPORT_CALL}),
        ("getattr(builtins, (name := 'eval'))", {DYNAMIC_IMPORT_CALL}),
        ("b: object = builtins\ngetattr(b, 'eval')", {DYNAMIC_IMPORT_CALL}),
        ("name = 'eval'\ngetattr(builtins, name)\nname = 'open'", {DYNAMIC_IMPORT_CALL}),
        ("name = 'open'\ngetattr(builtins, name)\nname = 'eval'", set()),
        ("name = 'open'\ndef nested():\n    name = 'eval'\ngetattr(builtins, name)", set()),
        ("def annotated(value: exec(source)) -> eval(source):\n    pass", {DYNAMIC_IMPORT_CALL}),
        ("class C(metaclass=eval(source)):\n    pass", {DYNAMIC_IMPORT_CALL}),
        ("lambda eval: eval(source)", set()),
        ("def eval(source):\n    return source\neval(source)", set()),
        ("class C:\n    eval = lambda value: value\n    def method(self):\n        return eval(source)", {DYNAMIC_IMPORT_CALL}),
        ("class C:\n    def eval(self, source):\n        return eval(source)", {DYNAMIC_IMPORT_CALL}),
        ("class C:\n    def eval(self, source):\n        return self.eval(source)", set()),
        ("for eval in functions:\n    eval(source)", set()),
        ("try:\n    operation()\nexcept Exception as eval:\n    eval(source)", set()),
        ("[eval(source) for eval in callbacks]", set()),
        ("(eval(source) for eval in callbacks)", set()),
        ("with callback() as eval:\n    eval(source)", set()),
        ("eval, other = callbacks\neval(source)", set()),
        ("for eval in []:\n    pass\neval(source)", {DYNAMIC_IMPORT_CALL}),
        ("[getattr(builtins, name) for name in ('eval',)]", {DYNAMIC_IMPORT_CALL}),
        ("b, = (builtins,)\ngetattr(b, 'eval')", {DYNAMIC_IMPORT_CALL}),
        ("from .builtins import helper", set()),
        ("from .importlib import helper", set()),
        ("from ..core import config", set()),
        ("import arbor.skills_suite", {"arbor.skills_suite"}),
    ),
)
def test_aros_boundary_resolves_relative_imports_and_rejects_dynamic_imports(
    source: str,
    expected: set[str],
) -> None:
    tree = ast.parse(source)

    assert {module for _, module in _forbidden_imports(tree, "arbor.aros")} == expected


@pytest.mark.parametrize(
    "source",
    (
        "compile(source, '<aros>', 'exec')",
        "FunctionType(code, globals())",
        "import builtins\nbuiltins.eval(source)",
        "import builtins as b\nb.exec(source)",
        "import builtins\nbuiltins.compile(source, '<aros>', 'exec')",
        "import types\ntypes.FunctionType(code, globals())",
        "import types as t\nfactory = t.FunctionType\nfactory(code, globals())",
    ),
)
def test_aros_boundary_rejects_direct_dynamic_execution_calls(source: str) -> None:
    assert _forbidden_imports(ast.parse(source), "arbor.aros")


@pytest.mark.parametrize(
    "name",
    ("eval", "exec", "compile", "__import__", "import_module", "FunctionType"),
)
def test_aros_boundary_rejects_callable_string_lookups(name: str) -> None:
    tree = ast.parse(f"dispatch[{name!r}](source)")

    assert _forbidden_imports(tree, "arbor.aros")


def test_aros_boundary_rejects_folded_builtin_dict_execution_lookup() -> None:
    tree = ast.parse("builtins.__dict__['ev' + 'al'](source)")

    assert _forbidden_imports(tree, "arbor.aros")


@pytest.mark.parametrize(
    "source",
    (
        "load = builtins.__dict__['__import__']\nload('arbor.review')",
        "run = builtins.__dict__['ev' + 'al']\nrun(source)",
    ),
)
def test_aros_boundary_tracks_dynamic_subscript_aliases(source: str) -> None:
    tree = ast.parse(source)

    assert (2, DYNAMIC_IMPORT_CALL) in _dynamic_imports(tree)


def test_aros_boundary_tracks_protected_getattr_alias_call() -> None:
    tree = ast.parse(
        "load = getattr(builtins, '__im' + 'port__')\n"
        "load('arbor.review')"
    )

    assert (2, DYNAMIC_IMPORT_CALL) in _dynamic_imports(tree)


def test_aros_boundary_permits_harmless_subscript_alias() -> None:
    tree = ast.parse(
        "callbacks = {'safe': callback}\n"
        "load = callbacks['safe']\n"
        "load(source)"
    )

    assert not _forbidden_imports(tree, "arbor.aros")


def test_aros_boundary_rejects_protected_compile_getattr() -> None:
    tree = ast.parse("getattr(builtins, 'com' + 'pile')(source, '<aros>', 'exec')")

    assert _forbidden_imports(tree, "arbor.aros")


@pytest.mark.parametrize(
    "name",
    ("eval", "exec", "compile", "__import__", "import_module", "FunctionType"),
)
def test_aros_boundary_permits_harmless_execution_strings(name: str) -> None:
    tree = ast.parse(f"label = {name!r}")

    assert not _forbidden_imports(tree, "arbor.aros")


def test_aros_boundary_permits_harmless_folded_execution_string() -> None:
    tree = ast.parse("label = '__im' + 'port__'")

    assert not _forbidden_imports(tree, "arbor.aros")


@pytest.mark.parametrize("name", ("__import__", "import_module"))
def test_aros_boundary_rejects_dynamic_import_references(name: str) -> None:
    tree = ast.parse(f"reflection = {name}")

    assert _forbidden_imports(tree, "arbor.aros")


@pytest.mark.parametrize(
    "name",
    ("vars", "globals", "locals", "getattr", "eval", "exec"),
)
def test_aros_boundary_permits_ordinary_reflection_names(name: str) -> None:
    tree = ast.parse(f"reflection = {name}")

    assert not _forbidden_imports(tree, "arbor.aros")


def test_aros_boundary_permits_harmless_standard_imports() -> None:
    tree = ast.parse(
        "import json\n"
        "import os\n"
        "from pathlib import Path\n"
        "directory_flag = getattr(os, 'O_DIRECTORY', 0)"
    )

    assert not _forbidden_imports(tree, "arbor.aros")


def test_aros_boundary_permits_normal_getattr() -> None:
    tree = ast.parse(
        "directory_flag = getattr(os, 'O_DIRECTORY', 0)\n"
        "value = getattr(subject, attribute, None)"
    )

    assert not _forbidden_imports(tree, "arbor.aros")


def test_aros_boundary_gate_is_documented_as_architecture_discipline() -> None:
    documentation = test_aros_imports_only_use_the_one_way_internal_boundary.__doc__ or ""

    assert "architecture discipline" in documentation
    assert "not a sandbox" in documentation


def test_config_substrate_is_in_the_canonical_allowlist() -> None:
    assert {"arbor._app", "arbor.cli.user_config"} <= set(ALLOWED_INTERNAL_MODULES)


def test_direct_aros_import_loads_no_forbidden_project_modules() -> None:
    script = """
import importlib
import importlib.util
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
spec = importlib.util.spec_from_file_location(
    "arbor",
    root / "src" / "__init__.py",
    submodule_search_locations=[str(root / "src")],
)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
sys.modules["arbor"] = module
spec.loader.exec_module(module)
importlib.import_module("arbor.cli.aros_app")
print(json.dumps(sorted(sys.modules)))
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", script, str(REPO_ROOT)],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    loaded = json.loads(result.stdout)
    forbidden = sorted(
        module
        for module in loaded
        if _is_project_module(module) and not _is_allowed_internal(module)
    )
    assert not forbidden, f"direct AROS import loaded forbidden modules: {forbidden}"


def test_start_config_path_loads_no_legacy_user_or_semantic_config(
    tmp_path: Path,
) -> None:
    script = """
import importlib
import importlib.util
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
spec = importlib.util.spec_from_file_location(
    "arbor",
    root / "src" / "__init__.py",
    submodule_search_locations=[str(root / "src")],
)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
sys.modules["arbor"] = module
spec.loader.exec_module(module)
commands = importlib.import_module("arbor.cli.commands.aros_cmd")
print(json.dumps(sorted(sys.modules)))
"""
    environment = dict(os.environ)
    environment["HOME"] = str(tmp_path)
    result = subprocess.run(
        [sys.executable, "-I", "-c", script, str(REPO_ROOT)],
        cwd=REPO_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    loaded = json.loads(result.stdout)
    assert "arbor.cli.user_config" not in loaded
    forbidden = sorted(
        module
        for module in loaded
        if any(
            module == prefix or module.startswith(f"{prefix}.")
            for prefix in SEMANTIC_LEGACY_PREFIXES
        )
    )
    assert not forbidden, f"start config path loaded semantic legacy: {forbidden}"


def test_direct_aros_start_has_no_legacy_user_config_dependency() -> None:
    source = (REPO_ROOT / "src/cli/commands/aros_cmd.py").read_text(
        encoding="utf-8"
    )

    assert "user_config" not in source
    assert "llm_defaults" not in source
    assert "fallback" not in source.lower()


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
    (repo / "src" / "coordinator" / "padded_source.py").write_text(
        PADDED_FROZEN_SOURCE, encoding="utf-8"
    )
    (repo / "src" / "legacy.py").write_text(
        "legacy one\nlegacy two\n", encoding="utf-8"
    )
    (repo / "legacy_control.py").write_text(
        "legacy control\n", encoding="utf-8"
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


@pytest.fixture
def source_growth_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "aros-tests@example.invalid")
    _git(repo, "config", "user.name", "AROS Tests")
    _git(repo, "config", "commit.gpgsign", "false")
    existing = repo / "src" / "existing.py"
    existing.parent.mkdir()
    existing.write_text("existing source\n", encoding="utf-8")
    (repo / "src" / "existing.bin").write_bytes(b"\0existing binary")
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
    utility_path = repo / "independent_utility.py"
    utility_path.write_text(INDEPENDENT_UTILITY, encoding="utf-8")
    _git(repo, "add", str(utility_path.relative_to(repo)))
    if delete_frozen_helper:
        helper_path = repo / "src" / "coordinator" / "helper.py"
        helper_path.unlink()

    result = _run_checker(repo, base)

    assert result.returncode == 0, result.stdout + result.stderr


def test_legacy_freeze_rejects_padded_copy_in_core_by_path_growth(
    legacy_repo: tuple[Path, str],
) -> None:
    repo, base = legacy_repo
    destination = repo / "src" / "core" / "control_v2.py"
    destination.parent.mkdir()
    destination.write_text(PADDED_FROZEN_SOURCE + PADDING, encoding="utf-8")
    _git(repo, "add", "src/core/control_v2.py")

    result = _run_checker(repo, base)

    assert result.returncode == 2
    assert "src/core/control_v2.py" in result.stdout + result.stderr


def test_legacy_freeze_permits_padded_copy_in_aros_for_commissioning_review(
    legacy_repo: tuple[Path, str],
) -> None:
    repo, base = legacy_repo
    destination = repo / "src" / "aros" / "control_v2.py"
    destination.parent.mkdir()
    destination.write_text(PADDED_FROZEN_SOURCE + PADDING, encoding="utf-8")
    _git(repo, "add", "src/aros/control_v2.py")

    result = _run_checker(repo, base)

    assert result.returncode == 0, result.stdout + result.stderr


def test_legacy_freeze_rejects_growth_in_existing_legacy_source(
    legacy_repo: tuple[Path, str],
) -> None:
    repo, base = legacy_repo
    path = repo / "src" / "legacy.py"
    path.write_text(path.read_text(encoding="utf-8") + "new behavior\n", encoding="utf-8")

    result = _run_checker(repo, base)

    assert result.returncode == 2
    assert "src/legacy.py" in result.stdout + result.stderr


@pytest.mark.parametrize("staged", (False, True))
def test_legacy_freeze_rejects_new_source_symlinks(
    legacy_repo: tuple[Path, str], staged: bool
) -> None:
    repo, base = legacy_repo
    path = repo / "src" / "legacy_link.py"
    path.symlink_to("legacy.py")
    if staged:
        _git(repo, "add", "src/legacy_link.py")

    result = _run_checker(repo, base)

    assert result.returncode == 2
    assert "src/legacy_link.py" in result.stdout + result.stderr


@pytest.mark.parametrize("staged", (False, True))
@pytest.mark.parametrize(
    ("relative_path", "target", "target_is_directory"),
    (
        ("src/aros/link", "../coordinator", True),
        ("src/aros/run_link.py", "../run.py", False),
    ),
)
def test_legacy_freeze_rejects_symlinks_inside_allowed_growth_root(
    legacy_repo: tuple[Path, str],
    staged: bool,
    relative_path: str,
    target: str,
    target_is_directory: bool,
) -> None:
    repo, base = legacy_repo
    path = repo / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(target, target_is_directory=target_is_directory)
    if staged:
        _git(repo, "add", relative_path)

    result = _run_checker(repo, base)

    assert result.returncode == 2
    assert relative_path in result.stdout + result.stderr


@pytest.mark.parametrize(
    ("content", "staged"),
    (
        (b"", False),
        (b"", True),
        (b"\0legacy binary", False),
        (b"\0legacy binary", True),
    ),
)
def test_legacy_freeze_rejects_new_empty_and_binary_source_paths(
    source_growth_repo: tuple[Path, str], content: bytes, staged: bool
) -> None:
    repo, base = source_growth_repo
    path = repo / "src" / "legacy_payload.py"
    path.write_bytes(content)
    if staged:
        _git(repo, "add", "src/legacy_payload.py")

    result = _run_checker(repo, base)

    assert result.returncode == 2
    assert "src/legacy_payload.py" in result.stdout + result.stderr


def test_legacy_freeze_rejects_new_gitlink_under_source(
    source_growth_repo: tuple[Path, str],
) -> None:
    repo, base = source_growth_repo
    _git(
        repo,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{base},src/legacy-submodule",
    )
    staged = _git(repo, "ls-files", "--stage", "src/legacy-submodule").stdout
    assert staged.startswith("160000 ")

    result = _run_checker(repo, base)

    assert result.returncode == 2
    assert "src/legacy-submodule" in result.stdout + result.stderr


def test_legacy_freeze_rejects_gitlink_type_change_under_source(
    source_growth_repo: tuple[Path, str],
) -> None:
    repo, base = source_growth_repo
    _git(
        repo,
        "update-index",
        "--cacheinfo",
        f"160000,{base},src/existing.py",
    )
    cached = _git(
        repo,
        "diff",
        "--cached",
        "--raw",
        base,
        "--",
        "src/existing.py",
    ).stdout
    assert " T\tsrc/existing.py" in cached

    result = _run_checker(repo, base)

    assert result.returncode == 2
    assert "src/existing.py" in result.stdout + result.stderr


def test_legacy_freeze_rejects_gitlink_update_under_source(
    source_growth_repo: tuple[Path, str],
) -> None:
    repo, original_base = source_growth_repo
    _git(
        repo,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{original_base},src/existing-submodule",
    )
    _git(repo, "commit", "-qm", "add gitlink")
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(
        repo,
        "update-index",
        "--cacheinfo",
        f"160000,{base},src/existing-submodule",
    )
    cached = _git(
        repo,
        "diff",
        "--cached",
        "--raw",
        base,
        "--",
        "src/existing-submodule",
    ).stdout
    assert cached.startswith(":160000 160000 ")
    assert " M\tsrc/existing-submodule" in cached

    result = _run_checker(repo, base)

    assert result.returncode == 2
    assert "src/existing-submodule" in result.stdout + result.stderr


@pytest.mark.parametrize("staged", (False, True))
def test_legacy_freeze_rejects_tracked_binary_replacement(
    source_growth_repo: tuple[Path, str], staged: bool
) -> None:
    repo, base = source_growth_repo
    path = repo / "src" / "existing.bin"
    path.write_bytes(b"\0replacement binary")
    if staged:
        _git(repo, "add", "src/existing.bin")
    cached = ("--cached",) if staged else ()
    numstat = _git(
        repo,
        "diff",
        *cached,
        "--numstat",
        base,
        "--",
        "src/existing.bin",
    ).stdout
    assert numstat.startswith("-\t-\t")

    result = _run_checker(repo, base)

    assert result.returncode == 2
    assert "src/existing.bin" in result.stdout + result.stderr


def test_legacy_freeze_rejects_r100_move_into_non_allowlisted_source(
    legacy_repo: tuple[Path, str],
) -> None:
    repo, base = legacy_repo
    _git(repo, "mv", "legacy_control.py", "src/control_v2.py")
    raw = _git(
        repo,
        "diff",
        "--raw",
        "--find-renames=50%",
        base,
        "--",
    ).stdout
    assert "R100" in raw

    result = _run_checker(repo, base)

    assert result.returncode == 2
    assert "src/control_v2.py" in result.stdout + result.stderr


def test_legacy_freeze_rejects_staged_symlink_hidden_by_unstaged_delete(
    legacy_repo: tuple[Path, str],
) -> None:
    repo, base = legacy_repo
    path = repo / "src" / "legacy_link.py"
    path.symlink_to("legacy.py")
    _git(repo, "add", "src/legacy_link.py")
    path.unlink()

    result = _run_checker(repo, base)

    assert result.returncode == 2
    assert "src/legacy_link.py" in result.stdout + result.stderr


def test_legacy_freeze_rejects_staged_text_growth_hidden_by_unstaged_delete(
    legacy_repo: tuple[Path, str],
) -> None:
    repo, base = legacy_repo
    path = repo / "src" / "new_legacy.py"
    path.write_text("staged legacy behavior\n", encoding="utf-8")
    _git(repo, "add", "src/new_legacy.py")
    path.unlink()

    result = _run_checker(repo, base)

    assert result.returncode == 2
    assert "src/new_legacy.py" in result.stdout + result.stderr


def test_legacy_freeze_rejects_staged_r100_hidden_by_unstaged_delete(
    legacy_repo: tuple[Path, str],
) -> None:
    repo, base = legacy_repo
    destination = repo / "src" / "control_v2.py"
    _git(repo, "mv", "legacy_control.py", "src/control_v2.py")
    destination.unlink()
    cached = _git(
        repo,
        "diff",
        "--cached",
        "--raw",
        "--find-renames=50%",
        base,
        "--",
    ).stdout
    assert "R100" in cached

    result = _run_checker(repo, base)

    assert result.returncode == 2
    assert "src/control_v2.py" in result.stdout + result.stderr


def test_legacy_freeze_rejects_staged_c100_hidden_by_unstaged_delete(
    legacy_repo: tuple[Path, str],
) -> None:
    repo, base = legacy_repo
    source = repo / "legacy_control.py"
    destination = repo / "src" / "control_copy.py"
    destination.write_bytes(source.read_bytes())
    _git(repo, "add", "src/control_copy.py")
    destination.unlink()
    cached = _git(
        repo,
        "diff",
        "--cached",
        "--raw",
        "--find-copies=50%",
        "--find-copies-harder",
        base,
        "--",
    ).stdout
    assert "C100" in cached

    result = _run_checker(repo, base)

    assert result.returncode == 2
    assert "src/control_copy.py" in result.stdout + result.stderr


@pytest.mark.parametrize(
    "relative_path",
    (
        "src/aros/new_behavior.py",
        "src/cli/aros_app.py",
        "src/cli/commands/aros_cmd.py",
    ),
)
def test_legacy_freeze_permits_explicit_aros_growth(
    legacy_repo: tuple[Path, str], relative_path: str
) -> None:
    repo, base = legacy_repo
    path = repo / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("new AROS behavior\n", encoding="utf-8")

    result = _run_checker(repo, base)

    assert result.returncode == 0, result.stdout + result.stderr


def test_legacy_freeze_rejects_core_growth(
    legacy_repo: tuple[Path, str],
) -> None:
    repo, base = legacy_repo
    path = repo / "src" / "core" / "new_behavior.py"
    path.parent.mkdir()
    path.write_text("new behavior\n", encoding="utf-8")

    result = _run_checker(repo, base)

    assert result.returncode == 2
    assert "src/core/new_behavior.py" in result.stdout + result.stderr


@pytest.mark.parametrize("staged", (False, True))
def test_legacy_freeze_rejects_legacy_cli_app_growth(
    legacy_repo: tuple[Path, str], staged: bool
) -> None:
    repo, _ = legacy_repo
    path = repo / "src/cli/app.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("legacy entry\n", encoding="utf-8")
    _git(repo, "add", "src/cli/app.py")
    _git(repo, "commit", "-qm", "add legacy cli")
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    path.write_text("legacy entry\nnew behavior\n", encoding="utf-8")
    if staged:
        _git(repo, "add", "src/cli/app.py")

    result = _run_checker(repo, base)

    assert result.returncode == 2
    assert "src/cli/app.py" in result.stdout + result.stderr


@pytest.mark.parametrize("staged", (False, True))
def test_legacy_freeze_rejects_legacy_cli_app_mode_change(
    legacy_repo: tuple[Path, str], staged: bool
) -> None:
    repo, _ = legacy_repo
    path = repo / "src/cli/app.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("legacy entry\n", encoding="utf-8")
    _git(repo, "add", "src/cli/app.py")
    _git(repo, "commit", "-qm", "add legacy cli")
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    path.chmod(0o755)
    if staged:
        _git(repo, "add", "src/cli/app.py")

    result = _run_checker(repo, base)

    assert result.returncode == 2
    assert "src/cli/app.py" in result.stdout + result.stderr


@pytest.mark.parametrize("staged", (False, True))
def test_legacy_freeze_permits_legacy_cli_app_deletion(
    legacy_repo: tuple[Path, str], staged: bool
) -> None:
    repo, _ = legacy_repo
    path = repo / "src/cli/app.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("legacy entry\n", encoding="utf-8")
    _git(repo, "add", "src/cli/app.py")
    _git(repo, "commit", "-qm", "add legacy cli")
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    path.unlink()
    if staged:
        _git(repo, "add", "-u", "src/cli/app.py")

    result = _run_checker(repo, base)

    assert result.returncode == 0, result.stdout + result.stderr


def test_legacy_freeze_file_allowlist_does_not_allow_descendants(
    legacy_repo: tuple[Path, str],
) -> None:
    repo, base = legacy_repo
    path = repo / "src" / "cli" / "app.py" / "escape.py"
    path.parent.mkdir(parents=True)
    path.write_text("new legacy behavior\n", encoding="utf-8")

    result = _run_checker(repo, base)

    assert result.returncode == 2
    assert "src/cli/app.py/escape.py" in result.stdout + result.stderr


@pytest.mark.parametrize("staged", (False, True))
def test_legacy_freeze_permits_pure_deletion_in_non_allowlisted_source(
    legacy_repo: tuple[Path, str], staged: bool
) -> None:
    repo, base = legacy_repo
    (repo / "src" / "legacy.py").write_text("legacy one\n", encoding="utf-8")
    if staged:
        _git(repo, "add", "src/legacy.py")

    result = _run_checker(repo, base)

    assert result.returncode == 0, result.stdout + result.stderr


def test_legacy_freeze_permits_crlf_text_deletion(
    legacy_repo: tuple[Path, str],
) -> None:
    repo, _ = legacy_repo
    (repo / ".gitattributes").write_text(
        "src/legacy.py text eol=crlf\n", encoding="utf-8"
    )
    _git(repo, "add", ".gitattributes")
    _git(repo, "commit", "-qm", "declare CRLF checkout")
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    path = repo / "src" / "legacy.py"
    path.write_bytes(b"legacy one\r\n")
    assert b"\r\n" in path.read_bytes()
    numstat = _git(repo, "diff", "--numstat", base, "--", "src/legacy.py").stdout
    assert numstat.startswith("0\t1\t")

    result = _run_checker(repo, base)

    assert result.returncode == 0, result.stdout + result.stderr


def test_legacy_freeze_permits_r100_move_from_source_to_outside_source(
    legacy_repo: tuple[Path, str],
) -> None:
    repo, base = legacy_repo
    destination = repo / "moved" / "main.py"
    destination.parent.mkdir()
    _git(repo, "mv", "src/coordinator/main.py", "moved/main.py")
    raw = _git(
        repo,
        "diff",
        "--raw",
        "--find-renames=50%",
        base,
        "--",
    ).stdout
    assert "R100" in raw

    result = _run_checker(repo, base)

    assert result.returncode == 0, result.stdout + result.stderr


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


def test_phase0a_removes_duplicate_task_carrier() -> None:
    root = Path(__file__).resolve().parents[1]
    assert not (root / "src/aros/task_runner.py").exists()
    assert (root / "src/aros/task_adapter.py").is_file()
    source = (root / "src/aros/tasks.py").read_text(encoding="utf-8")
    assert "task_runner" not in source
    assert "_run_carrier_guardian" not in source


def test_phase0a_aros_source_budget_after_task_carrier_deletion() -> None:
    root = Path(__file__).resolve().parents[1] / "src/aros"
    lines = sum(len(path.read_bytes().splitlines()) for path in root.rglob("*.py"))
    assert lines <= 17_700, f"Phase 0A AROS source budget exceeded: {lines}"


def _cache_campaign_changed_paths(repository: Path, baseline: str) -> set[str]:
    result = subprocess.run(
        [
            "git",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.untrackedCache=false",
            "diff",
            "--name-only",
            "--diff-filter=ACDMRTUXB",
            baseline,
            "--",
        ],
        cwd=repository,
        capture_output=True,
        check=True,
        text=True,
    )
    changed = set(result.stdout.splitlines())
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "--", "src/aros"],
        cwd=repository,
        capture_output=True,
        check=True,
        text=True,
    )
    changed.update(untracked.stdout.splitlines())
    return changed


def test_cache_campaign_tasks_do_not_change_aros_product_source() -> None:
    """The commissioning-only cache substrate must remain outside src/aros."""
    repository = Path(__file__).resolve().parents[1]
    baseline = "a49632b581578d0e026ae10772df391b3b2ab465"
    changed = _cache_campaign_changed_paths(repository, baseline)
    assert not sorted(path for path in changed if path.startswith("src/aros/"))


def test_cache_campaign_boundary_detects_deleted_aros_source(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "boundary@example.invalid")
    _git(repository, "config", "user.name", "Boundary Test")
    source = repository / "src/aros/deleted.py"
    source.parent.mkdir(parents=True)
    source.write_text("value = 1\n")
    _git(repository, "add", ".")
    _git(repository, "commit", "-qm", "baseline")
    baseline = _git(repository, "rev-parse", "HEAD").stdout.strip()
    source.unlink()

    assert "src/aros/deleted.py" in _cache_campaign_changed_paths(
        repository, baseline
    )


def test_research_procedure_wave1_changes_stay_inside_static_boundary() -> None:
    name_status = _research_procedure_wave1_name_status(
        REPO_ROOT, RESEARCH_PROCEDURE_WAVE1_BASE
    )

    _assert_research_procedure_wave1_changes(name_status)


@pytest.mark.parametrize("status_code", ["A", "M", "D"])
def test_research_procedure_wave1_boundary_has_no_status_bypass(
    status_code: str,
) -> None:
    with pytest.raises(AssertionError, match="runtime architecture"):
        _assert_research_procedure_wave1_changes(
            f"{status_code}\tsrc/aros/procedure_runtime.py\n"
        )


def test_research_procedure_wave1_boundary_ignores_replace_objects(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "boundary@example.invalid")
    _git(repository, "config", "user.name", "Boundary Test")
    (repository / "README.md").write_text("base\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-qm", "base")
    baseline = _git(repository, "rev-parse", "HEAD").stdout.strip()
    runtime = repository / "src/aros/replaced.py"
    runtime.parent.mkdir(parents=True)
    runtime.write_text("value = 1\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-qm", "runtime change")
    head = _git(repository, "rev-parse", "HEAD").stdout.strip()
    _git(repository, "replace", baseline, head)
    hidden = _git(
        repository,
        "diff",
        "--name-status",
        "--no-renames",
        f"{baseline}..{head}",
    ).stdout
    assert hidden == ""

    name_status = _research_procedure_wave1_name_status(
        repository, baseline, head
    )

    with pytest.raises(AssertionError, match="runtime architecture"):
        _assert_research_procedure_wave1_changes(name_status)


def test_research_procedure_wave1_boundary_allows_exact_plan_correction() -> None:
    _assert_research_procedure_wave1_changes(
        "M\tdocs/superpowers/plans/2026-08-09-aros-research-procedure-core.md\n"
    )


@pytest.mark.parametrize("surface", ["working_deletion", "staged", "untracked"])
def test_research_procedure_wave1_boundary_includes_worktree_surfaces(
    tmp_path: Path, surface: str
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "boundary@example.invalid")
    _git(repository, "config", "user.name", "Boundary Test")
    (repository / "README.md").write_text("base\n", encoding="utf-8")
    if surface == "working_deletion":
        runtime = repository / "src/aros/deleted.py"
        runtime.parent.mkdir(parents=True)
        runtime.write_text("value = 1\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-qm", "base")
    baseline = _git(repository, "rev-parse", "HEAD").stdout.strip()

    if surface == "working_deletion":
        (repository / "src/aros/deleted.py").unlink()
    else:
        runtime = repository / f"src/aros/{surface}.py"
        runtime.parent.mkdir(parents=True)
        runtime.write_text("value = 1\n", encoding="utf-8")
        if surface == "staged":
            _git(repository, "add", str(runtime.relative_to(repository)))

    name_status = _research_procedure_wave1_name_status(
        repository, baseline
    )

    with pytest.raises(AssertionError, match="runtime architecture"):
        _assert_research_procedure_wave1_changes(name_status)
