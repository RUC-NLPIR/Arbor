# AROS Wave 1 Public Entry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Make aros a first-class public command backed by the existing native AROS implementation, keep arbor aros as a temporary warning-only forwarding route, and enforce that AROS cannot grow dependencies on frozen legacy semantic modules.

**Architecture:** One Typer application object remains the command implementation. The new aros console script invokes it directly; the legacy Arbor root mounts that same object and emits a deprecation warning only in its console main. A standard-library CI checker blocks added lines in frozen Coordinator/Executor roots, while AST tests enforce one-way imports.

**Tech Stack:** Python 3.10+, Typer, setuptools project scripts, pytest, Git, GitHub Actions.

**Parent specification:** docs/superpowers/specs/2026-08-02-aros-v1-product-and-migration-design.md

---

## Scope

This wave implements only the public entry and legacy freeze. Subsequent independent plans cover Child, Eval, operations, semantic migration, adapters, commissioning, and final Arbor namespace retirement.

## File map

- src/cli/aros_app.py: direct aros entry, no business logic.
- src/cli/commands/aros_cmd.py: single AROS command implementation.
- src/cli/app.py: legacy Arbor root and forwarding warning.
- pyproject.toml: console entry metadata.
- scripts/check_aros_legacy_freeze.py: CI diff gate.
- tests/test_aros_public_entry.py: direct/legacy entry behavior.
- tests/test_aros_architecture_boundary.py: import and freeze policies.
- docs/aros/README.md: truthful public guide.
- docs/analysis/aros-wave1-public-entry-smoke.md: commissioning evidence.

### Task 1: Add the direct aros console entry

**Files:**
- Create: src/cli/aros_app.py
- Modify: pyproject.toml
- Create: tests/test_aros_public_entry.py

- [ ] **Step 1: Write failing entry tests**

~~~python
from __future__ import annotations

import tomllib
from pathlib import Path

from typer.testing import CliRunner

from arbor.cli.commands.aros_cmd import aros_app


_ROOT = Path(__file__).resolve().parent.parent
runner = CliRunner()


def test_pyproject_exposes_direct_aros_script() -> None:
    metadata = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert metadata["project"]["scripts"]["aros"] == "arbor.cli.aros_app:main"


def test_direct_aros_help_is_the_root_app() -> None:
    result = runner.invoke(aros_app, ["--help"])
    assert result.exit_code == 0, result.output
    for command in ("init", "boot", "status", "start", "run"):
        assert command in result.output
    assert "\naros " not in result.output


def test_direct_entry_reuses_the_single_app() -> None:
    from arbor.cli import aros_app as entry

    assert entry.app is aros_app
~~~

- [ ] **Step 2: Verify RED**

~~~bash
/workspace/Arbor/.venv/bin/pytest -q tests/test_aros_public_entry.py
~~~

Expected: missing module or project-script assertion failure.

- [ ] **Step 3: Add the thin entry**

~~~python
"""Direct public entry point for the AROS CLI."""

from __future__ import annotations

from .commands.aros_cmd import aros_app as app


def main() -> None:
    """Run the single native AROS Typer application."""
    app()


if __name__ == "__main__":
    main()
~~~

Add to project.scripts:

~~~toml
aros = "arbor.cli.aros_app:main"
~~~

Do not rename the distribution or Python namespace in this wave.

- [ ] **Step 4: Verify GREEN**

~~~bash
/workspace/Arbor/.venv/bin/pytest -q \
  tests/test_aros_public_entry.py tests/test_aros_cli.py tests/test_aros_run_cli.py
/workspace/Arbor/.venv/bin/ruff check \
  src/cli/aros_app.py tests/test_aros_public_entry.py
git diff --check
~~~

- [ ] **Step 5: Commit**

~~~bash
git add pyproject.toml src/cli/aros_app.py tests/test_aros_public_entry.py
git -c user.name="AROS Agent" -c user.email="aros@local.invalid" \
  commit -m "feat(aros): add first-class public CLI entry"
~~~

### Task 2: Make arbor aros a warning-only forwarding route

**Files:**
- Modify: src/cli/app.py
- Modify: tests/test_aros_public_entry.py

- [ ] **Step 1: Write failing forwarding tests**

Append:

~~~python
import sys


def test_legacy_root_mounts_the_same_aros_app() -> None:
    from arbor.cli import app as legacy

    registered = {
        group.name: group.typer_instance
        for group in legacy.app.registered_groups
    }
    assert registered["aros"] is aros_app


def test_legacy_main_warns_when_forwarding_aros(
    monkeypatch,
    capsys,
) -> None:
    from arbor.cli import app as legacy

    calls: list[list[str]] = []
    monkeypatch.setattr(sys, "argv", ["arbor", "aros", "--help"])
    monkeypatch.setattr(legacy, "app", lambda: calls.append(list(sys.argv)))
    legacy.main()
    captured = capsys.readouterr()

    assert calls == [["arbor", "aros", "--help"]]
    assert "deprecated" in captured.err.lower()
    assert "use aros" in captured.err.lower()
~~~

- [ ] **Step 2: Verify RED**

~~~bash
/workspace/Arbor/.venv/bin/pytest -q \
  tests/test_aros_public_entry.py::test_legacy_root_mounts_the_same_aros_app \
  tests/test_aros_public_entry.py::test_legacy_main_warns_when_forwarding_aros
~~~

Expected: shared-app assertion passes and warning assertion fails.

- [ ] **Step 3: Add one warning helper**

~~~python
def _warn_aros_forward(argv: list[str]) -> None:
    if argv and argv[0] == "aros":
        typer.secho(
            "warning: arbor aros is deprecated; use aros directly",
            fg=typer.colors.YELLOW,
            err=True,
        )
~~~

Call it once in main immediately after argv is read. Do not copy or re-register commands.

- [ ] **Step 4: Verify and commit**

~~~bash
/workspace/Arbor/.venv/bin/pytest -q \
  tests/test_aros_public_entry.py tests/test_aros_cli.py tests/test_aros_run_cli.py
/workspace/Arbor/.venv/bin/ruff check src/cli/app.py tests/test_aros_public_entry.py
git diff --check
git add src/cli/app.py tests/test_aros_public_entry.py
git -c user.name="AROS Agent" -c user.email="aros@local.invalid" \
  commit -m "chore(aros): deprecate nested Arbor entry"
~~~

### Task 3: Enforce the one-way legacy freeze

**Files:**
- Create: scripts/check_aros_legacy_freeze.py
- Create: tests/test_aros_architecture_boundary.py
- Modify: .github/workflows/ci.yml

- [ ] **Step 1: Write failing policy tests**

~~~python
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path


_ROOT = Path(__file__).resolve().parent.parent
_FORBIDDEN = {"coordinator", "executor", "idea_tree", "orchestrator"}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            result.add(node.module or "")
    return result


def test_aros_does_not_import_legacy_semantic_modules() -> None:
    paths = sorted((_ROOT / "src" / "aros").glob("*.py"))
    paths.append(_ROOT / "src" / "cli" / "commands" / "aros_cmd.py")
    violations = sorted(
        module
        for path in paths
        for module in _imports(path)
        if _FORBIDDEN.intersection(module.split("."))
    )
    assert violations == []


def _make_repo(tmp_path: Path, root: str, content: str) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "freeze@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Freeze Test"],
        check=True,
    )
    target = repo / root / "engine.py"
    target.parent.mkdir(parents=True)
    target.write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "baseline"], check=True)
    return repo, target


def _run(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(_ROOT / "scripts" / "check_aros_legacy_freeze.py"),
            "--repo",
            str(repo),
            "--base",
            "HEAD",
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_freeze_rejects_added_lines(tmp_path: Path) -> None:
    repo, target = _make_repo(tmp_path, "src/coordinator", "OLD = 1\n")
    target.write_text("OLD = 1\nNEW_FEATURE = 2\n", encoding="utf-8")
    result = _run(repo)
    assert result.returncode == 2
    assert "src/coordinator/engine.py" in result.stderr


def test_freeze_allows_pure_deletion(tmp_path: Path) -> None:
    repo, target = _make_repo(tmp_path, "src/executor", "A = 1\nB = 2\n")
    target.write_text("A = 1\n", encoding="utf-8")
    result = _run(repo)
    assert result.returncode == 0, result.stderr
~~~

- [ ] **Step 2: Verify RED**

~~~bash
/workspace/Arbor/.venv/bin/pytest -q tests/test_aros_architecture_boundary.py
~~~

Expected: script-not-found failures.

- [ ] **Step 3: Add the freeze checker**

~~~python
#!/usr/bin/env python3
"""Reject feature growth in frozen Arbor semantic-control paths."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


FROZEN_ROOTS = (
    "src/coordinator",
    "src/executor",
    "src/run.py",
    "src/review.py",
)


def added_line_violations(repo: Path, base: str) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(repo), "diff", "--numstat", base, "--", *FROZEN_ROOTS],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git diff failed")
    violations: list[str] = []
    for raw in result.stdout.splitlines():
        added, _deleted, path = raw.split("\t", 2)
        if added == "-" or int(added) > 0:
            violations.append(path)
    return sorted(violations)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--base", required=True)
    args = parser.parse_args()
    try:
        violations = added_line_violations(args.repo.resolve(), args.base)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"legacy freeze check failed: {error}", file=sys.stderr)
        return 2
    if violations:
        print(
            "legacy semantic roots are frozen; added lines found in:\n"
            + "\n".join(f"- {path}" for path in violations),
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
~~~

Add to the pull-request CI job:

~~~yaml
      - name: Enforce AROS legacy freeze
        if: github.event_name == 'pull_request'
        run: |
          git fetch origin \
            "$GITHUB_BASE_REF:refs/remotes/origin/$GITHUB_BASE_REF"
          python scripts/check_aros_legacy_freeze.py \
            --base "origin/$GITHUB_BASE_REF"
~~~

- [ ] **Step 4: Verify and commit**

~~~bash
/workspace/Arbor/.venv/bin/pytest -q tests/test_aros_architecture_boundary.py
/workspace/Arbor/.venv/bin/ruff check \
  scripts/check_aros_legacy_freeze.py tests/test_aros_architecture_boundary.py
git diff --check
git add .github/workflows/ci.yml scripts/check_aros_legacy_freeze.py \
  tests/test_aros_architecture_boundary.py
git -c user.name="AROS Agent" -c user.email="aros@local.invalid" \
  commit -m "ci(aros): freeze legacy semantic control paths"
~~~

### Task 4: Publish truthful Wave 1 documentation

**Files:**
- Create: docs/aros/README.md
- Modify: README.md
- Modify: docs/README.md
- Modify: docs/document_registry.json
- Modify: tests/test_document_registry.py

- [ ] **Step 1: Write failing documentation tests**

Append:

~~~python
def test_registry_contains_approved_aros_v1_design() -> None:
    by_id = {document["id"]: document for document in _load_registry()["documents"]}
    design = by_id["aros-v1-product-migration-design"]
    assert design["path"] == (
        "docs/superpowers/specs/"
        "2026-08-02-aros-v1-product-and-migration-design.md"
    )
    assert design["status"] == "current"
    assert design["authority"] == "implementation_baseline"
    assert design["agent_visibility"] == "on_demand"


def test_aros_public_docs_use_direct_entry() -> None:
    text = (_ROOT / "docs" / "aros" / "README.md").read_text(encoding="utf-8")
    assert "aros init" in text
    assert "aros boot" in text
    assert "arbor aros" not in text
    assert "Not yet implemented" in text
~~~

- [ ] **Step 2: Verify RED**

~~~bash
/workspace/Arbor/.venv/bin/pytest -q tests/test_document_registry.py
~~~

- [ ] **Step 3: Write the guide**

docs/aros/README.md contains exactly these truthful sections:

~~~markdown
# AROS

AROS is the Agent-centric research operating system being commissioned in this repository.

## Available now

- aros init
- aros boot
- aros status
- aros start
- aros run start|status|list|tail|stop

## Not yet implemented

- child task substrate
- deterministic/protected evaluation
- migration adapters
- MCP parity
- Arbor retirement

## Compatibility

The direct aros command is the public AROS entry. Existing Arbor research commands remain frozen until equivalent AROS capabilities pass commissioning.
~~~

Add this exact migration notice near the top of README.md:

~~~markdown
> **AROS migration:** The Agent-centric AROS path is being commissioned now.
> Installations from this repository expose `aros` as the direct entry for
> bootable workspaces and durable runs. Existing `arbor` research commands are
> frozen compatibility paths until equivalent AROS modules pass commissioning.
~~~

Add this exact routing item to docs/README.md:

~~~markdown
- Public AROS CLI and current capability guide: [aros/README.md](aros/README.md)
~~~

Register the design:

~~~json
{
  "id": "aros-v1-product-migration-design",
  "title": "AROS v1 Product, Architecture, and Arbor Migration Design",
  "path": "docs/superpowers/specs/2026-08-02-aros-v1-product-and-migration-design.md",
  "status": "current",
  "authority": "implementation_baseline",
  "agent_visibility": "on_demand"
}
~~~

- [ ] **Step 4: Verify and commit**

~~~bash
/workspace/Arbor/.venv/bin/pytest -q tests/test_document_registry.py
git diff --check
git add README.md docs/README.md docs/aros/README.md \
  docs/document_registry.json tests/test_document_registry.py
git -c user.name="AROS Agent" -c user.email="aros@local.invalid" \
  commit -m "docs(aros): publish direct entry migration guide"
~~~

### Task 5: Commission the real public entry

**Files:**
- Create: docs/analysis/aros-wave1-public-entry-smoke.md
- Modify: docs/document_registry.json

- [ ] **Step 1: Run focused and full verification**

~~~bash
/workspace/Arbor/.venv/bin/ruff check src/ tests/ scripts/check_aros_legacy_freeze.py
/workspace/Arbor/.venv/bin/pytest -q \
  tests/test_aros_public_entry.py \
  tests/test_aros_architecture_boundary.py \
  tests/test_aros_cli.py tests/test_aros_run_cli.py \
  tests/test_document_registry.py
/workspace/Arbor/.venv/bin/pytest -q
git diff --check
git diff --quiet -- uv.lock
~~~

- [ ] **Step 2: Build and install the wheel**

~~~bash
rm -rf dist /tmp/aros-wave1-venv /tmp/aros-wave1-workspace
/workspace/Arbor/.venv/bin/python -m pip wheel \
  --no-deps --no-build-isolation --wheel-dir dist .
/workspace/Arbor/.venv/bin/python -m venv \
  --system-site-packages /tmp/aros-wave1-venv
/tmp/aros-wave1-venv/bin/pip install --no-deps --force-reinstall dist/*.whl
~~~

- [ ] **Step 3: Run the clean-install smoke**

~~~bash
/tmp/aros-wave1-venv/bin/aros --help
mkdir -p /tmp/aros-wave1-workspace
git -C /tmp/aros-wave1-workspace init -q
git -C /tmp/aros-wave1-workspace config user.email aros-smoke@example.invalid
git -C /tmp/aros-wave1-workspace config user.name "AROS Smoke"
/tmp/aros-wave1-venv/bin/aros init \
  --cwd /tmp/aros-wave1-workspace \
  --mission "Verify direct AROS entry"
/tmp/aros-wave1-venv/bin/aros status \
  --cwd /tmp/aros-wave1-workspace --json
/tmp/aros-wave1-venv/bin/aros boot \
  --cwd /tmp/aros-wave1-workspace
/tmp/aros-wave1-venv/bin/arbor aros --help
~~~

Verify direct command parity, exact boot mission, deprecation warning, required workspace files, and absence of .arbor.

- [ ] **Step 4: Write and register exact evidence**

The evidence records source commit, wheel filename/hash, exact commands, direct help commands, initialized status, boot mission, warning text, absence of .arbor, and focused/full test counts.

Register:

~~~json
{
  "id": "aros-wave1-public-entry-smoke",
  "title": "AROS Wave 1 Public Entry Smoke Evidence",
  "path": "docs/analysis/aros-wave1-public-entry-smoke.md",
  "status": "current",
  "authority": "informative",
  "agent_visibility": "on_demand"
}
~~~

- [ ] **Step 5: Re-run and commit**

~~~bash
/workspace/Arbor/.venv/bin/pytest -q tests/test_document_registry.py
/workspace/Arbor/.venv/bin/pytest -q
git diff --check
git diff --quiet -- uv.lock
git add docs/analysis/aros-wave1-public-entry-smoke.md \
  docs/document_registry.json
git -c user.name="AROS Agent" -c user.email="aros@local.invalid" \
  commit -m "docs(aros): commission first-class public entry"
~~~

## Wave 1 exit gate

Wave 1 is complete only when a clean wheel exposes aros; aros runs the single native app; arbor aros mounts the same app and adds only a warning; AROS imports no frozen semantic module; CI blocks legacy feature growth; real init/status/boot creates no .arbor state; focused/full tests pass; evidence is registered; and frozen legacy commands still work.

## Execution discipline

Use strict TDD and a fresh subagent per task. After every task run spec-compliance review, then code-quality review. Preserve the paused M4 prototype worktree as historical evidence and never merge it.
