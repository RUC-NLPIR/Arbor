# AROS Wave 1 Public Entry Smoke Evidence

This record commissions only the Wave 1 public command boundary: a wheel-built
`aros` entry, its transitional `arbor aros` forwarding route, and real
`init`/`status`/`boot` behavior. It is implementation evidence, not a claim that
later AROS modules or the full Design Book are complete.

## Source and environment

- Source commit: `46a68583a6031ce9dec36a740be0ed8f3e8e0fc3`
- Evidence time: `2026-08-02T08:04:50Z`
- Host: `Linux 6.8.0-124-generic x86_64`
- Build and smoke Python: `Python 3.12.3`
- Source worktree: `/workspace/Arbor/.worktree/aros-v1-design`
- Fresh smoke root: `/tmp/aros-wave1-smoke.h17pTVCJ4Y`

The wheel was built from the clean source commit before this evidence file was
written. All installed-command checks ran with the working directory outside
the repository.

## Pre-smoke verification

The plan-exact gates were run first:

```bash
/workspace/Arbor/.venv/bin/ruff check src/ tests/ scripts/check_aros_legacy_freeze.py
/workspace/Arbor/.venv/bin/pytest -q \
  tests/test_aros_public_entry.py \
  tests/test_aros_architecture_boundary.py \
  tests/test_aros_cli.py tests/test_aros_run_cli.py \
  tests/test_document_registry.py
/workspace/Arbor/.venv/bin/pytest -q
git diff --check
git diff --quiet -- uv.lock
git diff --exit-code
```

Repository configuration already supplies `-q`, so the two plan-exact pytest
commands intentionally emitted progress without a numeric summary. The same
sets were rerun with only the configured `addopts` cleared to capture exact
counts:

```bash
/workspace/Arbor/.venv/bin/pytest -o addopts= -q \
  tests/test_aros_public_entry.py \
  tests/test_aros_architecture_boundary.py \
  tests/test_aros_cli.py tests/test_aros_run_cli.py \
  tests/test_document_registry.py
/workspace/Arbor/.venv/bin/pytest -o addopts= -q
```

Receipts:

```text
Ruff: All checks passed! (0 diagnostics)
Focused Wave 1: 66 passed in 3.75s
Full suite: 615 passed, 6 skipped in 20.73s
git diff --check: exit 0, no output
git diff --quiet -- uv.lock: exit 0, no output
git diff --exit-code: exit 0, no output
```

No `uv` command was invoked. The `uv.lock` receipt is a Git diff check and
proves that the lock file was unchanged at this gate.

## Wheel build and entry metadata

A new temporary root and distribution directory were allocated without
deleting or reusing an existing path:

```bash
mktemp -d /tmp/aros-wave1-smoke.XXXXXXXXXX
mkdir /tmp/aros-wave1-smoke.h17pTVCJ4Y/dist
/workspace/Arbor/.venv/bin/python -m pip wheel \
  --no-deps --no-build-isolation \
  --wheel-dir /tmp/aros-wave1-smoke.h17pTVCJ4Y/dist .
sha256sum \
  /tmp/aros-wave1-smoke.h17pTVCJ4Y/dist/arbor_agent-0.0.0-py3-none-any.whl
unzip -p \
  /tmp/aros-wave1-smoke.h17pTVCJ4Y/dist/arbor_agent-0.0.0-py3-none-any.whl \
  '*/entry_points.txt'
```

Receipts:

```text
Successfully built arbor-agent
wheel: arbor_agent-0.0.0-py3-none-any.whl
size: 700441 bytes
sha256: c1e2e7e401f0963d7116cbf9ba8cf8dea8f64d1162b065d29bf8314816e449a4

[console_scripts]
arbor = arbor.cli.app:main
aros = arbor.cli.aros_app:main
coordinator = arbor.coordinator.main:cli
executor = arbor.executor.main:cli
review-research = arbor.review:cli
run-research = arbor.run:cli
```

Thus the wheel contains both the transitional `arbor` script and the direct
`aros` script, whose target is exactly `arbor.cli.aros_app:main`.

## Fresh install and package resolution

The wheel was installed without dependency resolution into a newly created
venv:

```bash
/workspace/Arbor/.venv/bin/python -m venv --system-site-packages \
  /tmp/aros-wave1-smoke.h17pTVCJ4Y/venv
/tmp/aros-wave1-smoke.h17pTVCJ4Y/venv/bin/pip install \
  --no-deps --force-reinstall \
  /tmp/aros-wave1-smoke.h17pTVCJ4Y/dist/arbor_agent-0.0.0-py3-none-any.whl
```

Installation reported `Successfully installed arbor-agent-0.0.0`, and
`pip show arbor-agent` reported this location:

```text
/tmp/aros-wave1-smoke.h17pTVCJ4Y/venv/lib/python3.12/site-packages
```

The fresh venv's `--system-site-packages` setting reaches the base `/usr`
interpreter packages, not packages installed in the creating
`/workspace/Arbor/.venv`. A dependency-free import therefore exposed the real
environment prerequisite before smoke commands ran:

```bash
/tmp/aros-wave1-smoke.h17pTVCJ4Y/venv/bin/python \
  -c 'import arbor.cli.aros_app'
```

```text
ModuleNotFoundError: No module named 'typer'
```

The decision was to preserve the required `--no-deps` wheel install and use the
main venv's physical site-packages only as an offline dependency pool:

```bash
/workspace/Arbor/.venv/bin/python -c \
  'import sysconfig; print(sysconfig.get_path("purelib"))'
```

```text
/workspace/Arbor/.venv/lib/python3.12/site-packages
```

Every subsequent installed AROS import and `aros`/`arbor` invocation used a
single ordered dependency path:

```bash
PYTHONPATH=/tmp/aros-wave1-smoke.h17pTVCJ4Y/venv/lib/python3.12/site-packages:/workspace/Arbor/.venv/lib/python3.12/site-packages
```

The temp venv site-packages is deliberately first, so the wheel package and
ordinary distribution metadata lookup take precedence. The main physical
site-packages is second and supplies only offline dependencies. No
dependency-pool `.pth` file was copied or manually processed. A resolution
assertion rejected any `/workspace/Arbor/src` entry in `sys.path`, verified
that the main editable finder was not loaded, and used the default
`distribution("arbor-agent")` lookup:

```bash
env PYTHONPATH=/tmp/aros-wave1-smoke.h17pTVCJ4Y/venv/lib/python3.12/site-packages:/workspace/Arbor/.venv/lib/python3.12/site-packages \
  /tmp/aros-wave1-smoke.h17pTVCJ4Y/venv/bin/python -c '
from importlib.metadata import distribution
from pathlib import Path
import arbor
import arbor.cli.aros_app as entry
import sys
temp_site = Path(
    "/tmp/aros-wave1-smoke.h17pTVCJ4Y/venv/lib/python3.12/site-packages"
).resolve()
dependency_pool = Path(
    "/workspace/Arbor/.venv/lib/python3.12/site-packages"
).resolve()
arbor_file = Path(arbor.__file__).resolve()
entry_file = Path(entry.__file__).resolve()
dist = distribution("arbor-agent")
dist_root = Path(dist.locate_file("")).resolve()
assert arbor_file.is_relative_to(temp_site)
assert entry_file.is_relative_to(temp_site)
assert dist_root == temp_site
assert Path(dist._path).resolve().is_relative_to(temp_site)
assert sys.path.index(str(temp_site)) < sys.path.index(str(dependency_pool))
assert not any("/workspace/Arbor/src" in value for value in sys.path)
assert not any(
    name.startswith("__editable___arbor_agent") for name in sys.modules
)
print("python=" + sys.executable)
print("pythonpath_order=" + str(temp_site) + ":" + str(dependency_pool))
print("arbor_file=" + str(arbor_file))
print("entry_module=" + str(entry_file))
print("distribution_root=" + str(dist_root))
print("distribution_metadata=" + str(dist._path))
print("aros_entry=" + next(
    ep.value for ep in dist.entry_points
    if ep.group == "console_scripts" and ep.name == "aros"
))
print("editable_source_on_sys_path=false")
print("main_editable_finder_loaded=false")'
```

Its output was:

```text
python=/tmp/aros-wave1-smoke.h17pTVCJ4Y/venv/bin/python
pythonpath_order=/tmp/aros-wave1-smoke.h17pTVCJ4Y/venv/lib/python3.12/site-packages:/workspace/Arbor/.venv/lib/python3.12/site-packages
arbor_file=/tmp/aros-wave1-smoke.h17pTVCJ4Y/venv/lib/python3.12/site-packages/arbor/__init__.py
entry_module=/tmp/aros-wave1-smoke.h17pTVCJ4Y/venv/lib/python3.12/site-packages/arbor/cli/aros_app.py
distribution_root=/tmp/aros-wave1-smoke.h17pTVCJ4Y/venv/lib/python3.12/site-packages
distribution_metadata=/tmp/aros-wave1-smoke.h17pTVCJ4Y/venv/lib/python3.12/site-packages/arbor_agent-0.0.0.dist-info
aros_entry=arbor.cli.aros_app:main
editable_source_on_sys_path=false
main_editable_finder_loaded=false
```

The generated console-script interpreters were checked directly:

```bash
head -n 1 /tmp/aros-wave1-smoke.h17pTVCJ4Y/venv/bin/aros
head -n 1 /tmp/aros-wave1-smoke.h17pTVCJ4Y/venv/bin/arbor
```

Both commands returned:

```text
#!/tmp/aros-wave1-smoke.h17pTVCJ4Y/venv/bin/python3.12
```

These receipts prove that the invoked AROS package, entry module, and default
distribution metadata came from the wheel installed in the fresh venv, not the
main checkout's editable source.

## Installed-command smoke

The following environment prefix was used on every installed command below:

```bash
env PYTHONPATH=/tmp/aros-wave1-smoke.h17pTVCJ4Y/venv/lib/python3.12/site-packages:/workspace/Arbor/.venv/lib/python3.12/site-packages
```

Direct and mounted help were exercised first:

```bash
env PYTHONPATH=/tmp/aros-wave1-smoke.h17pTVCJ4Y/venv/lib/python3.12/site-packages:/workspace/Arbor/.venv/lib/python3.12/site-packages \
  /tmp/aros-wave1-smoke.h17pTVCJ4Y/venv/bin/aros --help
env PYTHONPATH=/tmp/aros-wave1-smoke.h17pTVCJ4Y/venv/lib/python3.12/site-packages:/workspace/Arbor/.venv/lib/python3.12/site-packages \
  /tmp/aros-wave1-smoke.h17pTVCJ4Y/venv/bin/aros run --help
```

The direct commands table contained exactly, in order,
`init,boot,status,start,run`; it contained no nested `aros`. The mounted run
table contained `start,status,list,tail,stop` and identified itself as
`Usage: aros run [OPTIONS] COMMAND [ARGS]...`.

A new Git workspace was then initialized and configured:

```bash
mkdir /tmp/aros-wave1-smoke.h17pTVCJ4Y/workspace-temp-first
git -C /tmp/aros-wave1-smoke.h17pTVCJ4Y/workspace-temp-first init -q
git -C /tmp/aros-wave1-smoke.h17pTVCJ4Y/workspace-temp-first \
  config user.email aros-smoke@example.invalid
git -C /tmp/aros-wave1-smoke.h17pTVCJ4Y/workspace-temp-first \
  config user.name "AROS Smoke"
env PYTHONPATH=/tmp/aros-wave1-smoke.h17pTVCJ4Y/venv/lib/python3.12/site-packages:/workspace/Arbor/.venv/lib/python3.12/site-packages \
  /tmp/aros-wave1-smoke.h17pTVCJ4Y/venv/bin/aros init \
  --cwd /tmp/aros-wave1-smoke.h17pTVCJ4Y/workspace-temp-first \
  --mission "Verify direct AROS entry"
```

The exact init receipt was:

```json
{
  "created": [
    "AGENTS.md",
    "AROS.md",
    "memory/NOW.md",
    ".gitignore"
  ],
  "preserved": [],
  "root": "/tmp/aros-wave1-smoke.h17pTVCJ4Y/workspace-temp-first",
  "updated": []
}
```

Status and boot used the installed direct entry:

```bash
env PYTHONPATH=/tmp/aros-wave1-smoke.h17pTVCJ4Y/venv/lib/python3.12/site-packages:/workspace/Arbor/.venv/lib/python3.12/site-packages \
  /tmp/aros-wave1-smoke.h17pTVCJ4Y/venv/bin/aros status \
  --cwd /tmp/aros-wave1-smoke.h17pTVCJ4Y/workspace-temp-first --json
env PYTHONPATH=/tmp/aros-wave1-smoke.h17pTVCJ4Y/venv/lib/python3.12/site-packages:/workspace/Arbor/.venv/lib/python3.12/site-packages \
  /tmp/aros-wave1-smoke.h17pTVCJ4Y/venv/bin/aros boot \
  --cwd /tmp/aros-wave1-smoke.h17pTVCJ4Y/workspace-temp-first
```

The exact status receipt was:

```json
{
  "git": {
    "branch": "master",
    "changes": [
      "?? .gitignore",
      "?? AGENTS.md",
      "?? AROS.md",
      "?? memory/NOW.md"
    ],
    "changes_truncated": false,
    "dirty": true,
    "head": null,
    "is_repository": true,
    "worktrees": [
      {
        "branch": "master",
        "detached": false,
        "head": null,
        "path": "/tmp/aros-wave1-smoke.h17pTVCJ4Y/workspace-temp-first"
      }
    ],
    "worktrees_truncated": false
  },
  "initialized": true,
  "root": "/tmp/aros-wave1-smoke.h17pTVCJ4Y/workspace-temp-first",
  "runs": {
    "counts": {},
    "items": [],
    "operational_error": null,
    "total": 0,
    "truncated": false
  },
  "views": {
    "frontier": {
      "exists": false,
      "path": "questions/FRONTIER.md"
    },
    "mission": {
      "exists": true,
      "path": "AROS.md"
    },
    "now": {
      "exists": true,
      "path": "memory/NOW.md"
    }
  }
}
```

An unborn, dirty Git state is expected immediately after init because its four
new files have not yet been committed. Boot reproduced the exact durable
mission:

```text
# AROS Boot

## Mission and constraints — AROS.md

# AROS Project

## Mission

Verify direct AROS entry
```

The compatibility route was invoked separately:

```bash
env PYTHONPATH=/tmp/aros-wave1-smoke.h17pTVCJ4Y/venv/lib/python3.12/site-packages:/workspace/Arbor/.venv/lib/python3.12/site-packages \
  /tmp/aros-wave1-smoke.h17pTVCJ4Y/venv/bin/arbor aros --help
```

Its stderr contained exactly one line and one warning occurrence:

```text
warning: arbor aros is deprecated; use aros directly
```

A captured-output assertion compared both command tables:

```bash
env PYTHONPATH=/tmp/aros-wave1-smoke.h17pTVCJ4Y/venv/lib/python3.12/site-packages:/workspace/Arbor/.venv/lib/python3.12/site-packages \
  /tmp/aros-wave1-smoke.h17pTVCJ4Y/venv/bin/python -c '
import os
import re
import subprocess
direct = subprocess.run(
    ["/tmp/aros-wave1-smoke.h17pTVCJ4Y/venv/bin/aros", "--help"],
    text=True, capture_output=True, env=os.environ
)
legacy = subprocess.run(
    ["/tmp/aros-wave1-smoke.h17pTVCJ4Y/venv/bin/arbor", "aros", "--help"],
    text=True, capture_output=True, env=os.environ
)
pattern = re.compile(r"^│ ([a-z][a-z0-9-]*)\s", re.MULTILINE)
direct_surface = tuple(pattern.findall(direct.stdout))
legacy_surface = tuple(pattern.findall(legacy.stdout))
warning = "warning: arbor aros is deprecated; use aros directly"
assert direct.returncode == legacy.returncode == 0
assert direct.stderr == ""
assert direct_surface == ("init", "boot", "status", "start", "run")
assert legacy_surface == direct_surface
assert "aros" not in direct_surface
assert legacy.stderr.count(warning) == 1
assert legacy.stderr.strip() == warning
print("direct_surface=" + ",".join(direct_surface))
print("legacy_surface=" + ",".join(legacy_surface))
print("nested_aros_count=" + str(direct_surface.count("aros")))
print("deprecation_warning_count=" + str(legacy.stderr.count(warning)))
print("deprecation_warning=" + legacy.stderr.strip())'
```

It produced:

```text
direct_surface=init,boot,status,start,run
legacy_surface=init,boot,status,start,run
nested_aros_count=0
deprecation_warning_count=1
deprecation_warning=warning: arbor aros is deprecated; use aros directly
```

## Workspace artifact check

A path/type assertion after boot used:

```bash
env PYTHONPATH=/tmp/aros-wave1-smoke.h17pTVCJ4Y/venv/lib/python3.12/site-packages:/workspace/Arbor/.venv/lib/python3.12/site-packages \
  /tmp/aros-wave1-smoke.h17pTVCJ4Y/venv/bin/python -c '
from pathlib import Path
root = Path("/tmp/aros-wave1-smoke.h17pTVCJ4Y/workspace-temp-first")
required_files = ("AROS.md", "AGENTS.md", "memory/NOW.md")
required_dirs = ("memory", ".aros", ".worktree")
for relative in required_files:
    assert (root / relative).is_file(), relative
for relative in required_dirs:
    assert (root / relative).is_dir(), relative
assert not (root / ".arbor").exists()
assert "Verify direct AROS entry" in (
    root / "AROS.md"
).read_text(encoding="utf-8")
print("required_files=" + ",".join(required_files))
print("required_directories=" + ",".join(required_dirs))
print("mission=Verify direct AROS entry")
print(".arbor_present=false")'
```

It produced:

```text
required_files=AROS.md,AGENTS.md,memory/NOW.md
required_directories=memory,.aros,.worktree
mission=Verify direct AROS entry
.arbor_present=false
```

The real initialized workspace therefore has `AROS.md`, `AGENTS.md`, durable
`memory/NOW.md`, `.aros`, and `.worktree`, and it does not create `.arbor`.

## Limits and prerequisites

- This is offline dependency seeding, not a fully dependency-isolated install.
  The wheel itself was installed with `--no-deps`; third-party imports were
  supplied by the main venv's physical site-packages.
- The smoke proves the installed `aros` command surface, its mounted `run`
  group, compatibility warning, and real init/status/boot path. It does not run
  a provider-backed `aros start` or launch an experiment with `aros run start`.
- Durable launch requires a clean committed Git HEAD and `tmux`. The default
  `isolated-linux` run profile additionally requires supported x86_64/aarch64
  Linux, exactly Landlock ABI 4, `libseccomp`, and `O_PATH`; `trusted-local` is
  explicitly not a security sandbox.
- Wave 1 does not claim child task substrate, deterministic/protected
  evaluation, migration adapters, MCP parity, semantic migration, Arbor
  retirement, or full Design Book commissioning.

## Exit result

At the recorded source commit, a cleanly built wheel exposes the first-class
`aros` public entry and the warning-only transitional `arbor aros` route over
the same five-command surface. The installed direct entry creates and boots a
fresh AROS workspace without `.arbor` state. This satisfies the narrow Wave 1
public-entry gate only, subject to the dependency-seeding limitation above.
