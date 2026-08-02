# AROS Wave 1 Public Entry Smoke Evidence

This record commissions only the Wave 1 public command boundary: a wheel-built
`aros` entry, its transitional `arbor aros` forwarding route, the packaged OAuth
import boundary, and real `init`/`status`/`boot` behavior. It is implementation
evidence, not a claim that later AROS modules or the full Design Book are
complete.

## Source and environment

- Source commit: `6b3595f3b55028cea09746da5df8193953a85583`
- Evidence time: `2026-08-02T20:55:43Z`
- Host: `Linux 6.8.0-124-generic x86_64 GNU/Linux`
- Build and smoke Python: `Python 3.12.3`
- Source worktree: `/workspace/Arbor/.worktree/aros-v1-design`
- Fresh clean-clone source: `/tmp/aros-wave1-clean.pLySEu7hOE/source`
- Fresh smoke root: `/tmp/aros-wave1-clean.pLySEu7hOE`

The wheel was built from a new local clone of the exact committed source. The
clone had no ignored build directory, distribution directory, egg-info,
virtualenv, egg-link, `.pth`, or editable-install artifact before the build.
All installed-command checks ran with the working directory outside both the
source worktree and clean clone.

## Pre-smoke verification

The following gates ran in the source worktree before the clone was created:

```bash
/workspace/Arbor/.venv/bin/ruff check \
  src/ tests/ scripts/check_aros_legacy_freeze.py
/workspace/Arbor/.venv/bin/python scripts/check_aros_legacy_freeze.py \
  --repo . \
  --base 321c6f0bafb53e8bea9238519e3e25f2db91eddb
/workspace/Arbor/.venv/bin/pytest -o addopts= -q \
  tests/test_aros_public_entry.py \
  tests/test_aros_architecture_boundary.py \
  tests/test_aros_cli.py tests/test_aros_run_cli.py \
  tests/test_aros_workspace.py \
  tests/test_document_registry.py
/workspace/Arbor/.venv/bin/pytest -o addopts= -q
git diff --check
git diff --quiet -- uv.lock
git diff --exit-code
```

The legacy checker base is the branch's `main` ancestor. Clearing only pytest's
configured `addopts` made the numeric summaries visible without changing test
selection.

Receipts:

```text
Ruff: All checks passed! (0 diagnostics)
Legacy semantic freeze checker: exit 0, no output
Focused Wave 1: 253 passed in 10.83s
Full suite: 789 passed, 6 skipped in 25.94s
git diff --check: exit 0, no output
git diff --quiet -- uv.lock: exit 0, no output
git diff --exit-code: exit 0, no output
```

No `uv` command was invoked. The `uv.lock` receipt is only a Git diff check and
proves that the lock file was unchanged at this gate.

## Clean committed-source proof

A new path was allocated and cloned with Git's local hard-link optimization
disabled. Only committed objects were checked out:

```bash
SMOKE_ROOT=$(mktemp -d /tmp/aros-wave1-clean.XXXXXXXXXX)
git clone --quiet --no-local \
  --branch aros-v1-design --single-branch \
  /workspace/Arbor "$SMOKE_ROOT/source"
git -C "$SMOKE_ROOT/source" rev-parse HEAD
git -C "$SMOKE_ROOT/source" branch --show-current
git -C "$SMOKE_ROOT/source" status --short --branch
```

```text
6b3595f3b55028cea09746da5df8193953a85583
aros-v1-design
## aros-v1-design...origin/aros-v1-design
```

Before building, the following artifact and ignored-file scans both produced no
paths:

```bash
find "$SMOKE_ROOT/source" \
  -path "$SMOKE_ROOT/source/.git" -prune -o \
  \( -type d \
       \( -name build -o -name dist -o -name '*.egg-info' -o -name .venv \) \
     -o -type f \
       \( -name '*.egg-link' -o -name '__editable__*' -o -name '*.pth' \) \
  \) -print
git -C "$SMOKE_ROOT/source" status --short --ignored
```

```text
pre-build artifact scan: no output
pre-build ignored-file scan: no output
```

The clone had no tags and contained 254 commits. The Git identity used by the
version backend was therefore the exact clean `6b3595f3` checkout, not stale
metadata from the development worktree.

## Build isolation and wheel metadata

The distribution directory was outside the clone. Pip used its default PEP 517
build isolation; `--no-build-isolation` was deliberately not passed:

```bash
mkdir /tmp/aros-wave1-clean.pLySEu7hOE/dist
/workspace/Arbor/.venv/bin/python -m pip wheel \
  --verbose --no-deps \
  --wheel-dir /tmp/aros-wave1-clean.pLySEu7hOE/dist \
  .
```

Pip created a temporary build environment and reported:

```text
Installing build dependencies: started
Successfully installed packaging-26.2 setuptools-83.0.0 setuptools-scm-10.2.1 vcs-versioning-2.2.3
Installing build dependencies: finished with status 'done'
Getting requirements to build wheel: finished with status 'done'
Preparing metadata (pyproject.toml): finished with status 'done'
Building wheel for arbor-agent (pyproject.toml): finished with status 'done'
Successfully built arbor-agent
```

The backend emitted setuptools package-discovery warnings for namespace-like
package-data directories. They were non-fatal; the archive assertions below
verify the configured Python packages and allowed top-level members directly.
Pip reported `Using cached` for all four isolated build-dependency wheels. As
expected, setuptools created ignored `build/` and `arbor_agent.egg-info/` paths
only after the empty pre-build scans; neither appears in the wheel.

With Git metadata present, the configured setuptools-scm backend, whose
configured fallback is `fallback_version = "0.1.0"`, produced a version tied to
the source commit:

```text
wheel: arbor_agent-0.1.1.dev254+g6b3595f3b-py3-none-any.whl
version: 0.1.1.dev254+g6b3595f3b
size: 701248 bytes
sha256: 999b8604f093a55e20203495431f6f3e1168b354c80a6ffc581a2aa67611833b
```

The version is not the stale `0.0.0` placeholder.

## Wheel content and entry metadata

An archive assertion loaded the configured package list from the clean clone's
`pyproject.toml`, required each package's `__init__.py`, required the OAuth
files, compared the complete entry-point file byte-for-byte, and rejected
source/build/editable contamination:

```bash
/workspace/Arbor/.venv/bin/python - <<'PY'
from email.parser import BytesParser
from hashlib import sha256
from pathlib import Path
from zipfile import ZipFile
import tomllib

root = Path('/tmp/aros-wave1-clean.pLySEu7hOE')
wheel, = (root / 'dist').glob('*.whl')
with (root / 'source' / 'pyproject.toml').open('rb') as stream:
    configured = tuple(tomllib.load(stream)['tool']['setuptools']['packages'])
with ZipFile(wheel) as archive:
    names = tuple(archive.namelist())
    dist_info, = {
        name.split('/', 1)[0] for name in names if '.dist-info/' in name
    }
    metadata = BytesParser().parsebytes(
        archive.read(f'{dist_info}/METADATA')
    )
    entry_points = archive.read(
        f'{dist_info}/entry_points.txt'
    ).decode()
    assert all(
        f"{package.replace('.', '/')}/__init__.py" in names
        for package in configured
    )
    assert all(path in names for path in (
        'arbor/core/oauth/__init__.py',
        'arbor/core/oauth/openai.py',
        'arbor/core/oauth/anthropic.py',
    ))
    assert entry_points == '''[console_scripts]
arbor = arbor.cli.app:main
aros = arbor.cli.aros_app:main
coordinator = arbor.coordinator.main:cli
executor = arbor.executor.main:cli
review-research = arbor.review:cli
run-research = arbor.run:cli
'''
    assert metadata['Name'] == 'arbor-agent'
    assert metadata['Version'] == '0.1.1.dev254+g6b3595f3b'
    assert not {
        name.split('/', 1)[0] for name in names
    } - {'arbor', dist_info}
    assert not any(
        name.startswith(('src/', 'build/', 'tests/'))
        or '.egg-info/' in name
        or '/.git/' in name
        or '__pycache__' in name
        or name.endswith(('.pyc', '.pyo', '.egg-link'))
        or '__editable__' in name
        for name in names
    )
print('size=' + str(wheel.stat().st_size))
print('sha256=' + sha256(wheel.read_bytes()).hexdigest())
print('member_count=' + str(len(names)))
print('missing_configured_packages=none')
print('source_build_editable_contamination=none')
PY
```

It reported 206 archive members and no missing or unexpected path. All 22
configured packages were present:

```text
arbor
arbor.aros
arbor.cli
arbor.cli.commands
arbor.cli.intake
arbor.coordinator
arbor.coordinator.tools
arbor.core
arbor.core.llm
arbor.core.oauth
arbor.core.tools
arbor.core.tools.web
arbor.events
arbor.events.subscribers
arbor.executor
arbor.mcp
arbor.plugins
arbor.report
arbor.search_agent
arbor.webui
arbor.zoo
arbor.skills_suite
```

The specifically checked OAuth members were:

```text
arbor/core/oauth/__init__.py
arbor/core/oauth/openai.py
arbor/core/oauth/anthropic.py
```

The exact installed console-script metadata was:

```ini
[console_scripts]
arbor = arbor.cli.app:main
aros = arbor.cli.aros_app:main
coordinator = arbor.coordinator.main:cli
executor = arbor.executor.main:cli
review-research = arbor.review:cli
run-research = arbor.run:cli
```

No archive member came from `src/`, `build/`, `tests/`, `.git`, egg-info,
editable-install artifacts, or Python bytecode.

## Fresh install and package resolution

The wheel was installed without dependency resolution into a new ordinary
venv, with no system-site-packages inheritance:

```bash
/workspace/Arbor/.venv/bin/python -m venv \
  /tmp/aros-wave1-clean.pLySEu7hOE/venv
/tmp/aros-wave1-clean.pLySEu7hOE/venv/bin/python -m pip install \
  --no-deps --force-reinstall \
  /tmp/aros-wave1-clean.pLySEu7hOE/dist/arbor_agent-0.1.1.dev254+g6b3595f3b-py3-none-any.whl
/tmp/aros-wave1-clean.pLySEu7hOE/venv/bin/python -m pip show arbor-agent
```

```text
Successfully installed arbor-agent-0.1.1.dev254+g6b3595f3b
Version: 0.1.1.dev254+g6b3595f3b
Location: /tmp/aros-wave1-clean.pLySEu7hOE/venv/lib/python3.12/site-packages
```

A dependency-free import exposed the expected runtime prerequisite:

```bash
env -u PYTHONPATH \
  HOME=/tmp/aros-wave1-clean.pLySEu7hOE/home \
  PYTHONNOUSERSITE=1 \
  /tmp/aros-wave1-clean.pLySEu7hOE/venv/bin/python \
  -c 'import arbor.cli.aros_app'
```

```text
ModuleNotFoundError: No module named 'typer'
```

The required `--no-deps` install was preserved. The main venv's physical
site-packages was then used only as an offline runtime dependency pool:

```text
PYTHONPATH=/tmp/aros-wave1-clean.pLySEu7hOE/venv/lib/python3.12/site-packages:/workspace/Arbor/.venv/lib/python3.12/site-packages
```

The temporary wheel site is first. `.pth` files from the dependency pool were
not processed, user site-packages was disabled, and the isolated `HOME` avoided
reading real OAuth state. The resolution assertion used default metadata lookup
and normal imports:

```bash
env \
  HOME=/tmp/aros-wave1-clean.pLySEu7hOE/home \
  PYTHONNOUSERSITE=1 \
  PYTHONPATH=/tmp/aros-wave1-clean.pLySEu7hOE/venv/lib/python3.12/site-packages:/workspace/Arbor/.venv/lib/python3.12/site-packages \
  /tmp/aros-wave1-clean.pLySEu7hOE/venv/bin/python - <<'PY'
from importlib.metadata import distribution
from pathlib import Path
import sys

import arbor
import arbor.cli.aros_app as entry
import arbor.core.oauth as oauth
import arbor.core.oauth.anthropic as anthropic_oauth
import arbor.core.oauth.openai as openai_oauth

temp_site = Path(
    '/tmp/aros-wave1-clean.pLySEu7hOE/venv/lib/python3.12/site-packages'
).resolve()
dependency_pool = Path(
    '/workspace/Arbor/.venv/lib/python3.12/site-packages'
).resolve()
modules = {
    'arbor': arbor,
    'entry_module': entry,
    'oauth_package': oauth,
    'openai_oauth_module': openai_oauth,
    'anthropic_oauth_module': anthropic_oauth,
}
dist = distribution('arbor-agent')
entries = {
    ep.name: ep.value for ep in dist.entry_points
    if ep.group == 'console_scripts'
}
assert all(Path(module.__file__).resolve().is_relative_to(temp_site)
           for module in modules.values())
assert Path(dist.locate_file('')).resolve() == temp_site
assert Path(dist._path).resolve().is_relative_to(temp_site)
assert dist.version == '0.1.1.dev254+g6b3595f3b'
assert entries['aros'] == 'arbor.cli.aros_app:main'
assert entries['arbor'] == 'arbor.cli.app:main'
assert sys.path.index(str(temp_site)) < sys.path.index(str(dependency_pool))
assert not any('/workspace/Arbor/src' in value for value in sys.path)
assert not any(name.startswith('__editable___arbor_agent')
               for name in sys.modules)
print('python=' + sys.executable)
print('pythonpath_order=' + str(temp_site) + ':' + str(dependency_pool))
for name, module in modules.items():
    print(name + '=' + str(Path(module.__file__).resolve()))
print('distribution_version=' + dist.version)
print('distribution_root=' + str(Path(dist.locate_file('')).resolve()))
print('distribution_metadata=' + str(Path(dist._path).resolve()))
print('aros_entry=' + entries['aros'])
print('arbor_entry=' + entries['arbor'])
print('editable_source_on_sys_path=false')
print('main_editable_finder_loaded=false')
PY
```

It produced the following resolution receipt:

```text
python=/tmp/aros-wave1-clean.pLySEu7hOE/venv/bin/python
pythonpath_order=/tmp/aros-wave1-clean.pLySEu7hOE/venv/lib/python3.12/site-packages:/workspace/Arbor/.venv/lib/python3.12/site-packages
arbor=/tmp/aros-wave1-clean.pLySEu7hOE/venv/lib/python3.12/site-packages/arbor/__init__.py
entry_module=/tmp/aros-wave1-clean.pLySEu7hOE/venv/lib/python3.12/site-packages/arbor/cli/aros_app.py
oauth_package=/tmp/aros-wave1-clean.pLySEu7hOE/venv/lib/python3.12/site-packages/arbor/core/oauth/__init__.py
openai_oauth_module=/tmp/aros-wave1-clean.pLySEu7hOE/venv/lib/python3.12/site-packages/arbor/core/oauth/openai.py
anthropic_oauth_module=/tmp/aros-wave1-clean.pLySEu7hOE/venv/lib/python3.12/site-packages/arbor/core/oauth/anthropic.py
distribution_version=0.1.1.dev254+g6b3595f3b
distribution_root=/tmp/aros-wave1-clean.pLySEu7hOE/venv/lib/python3.12/site-packages
distribution_metadata=/tmp/aros-wave1-clean.pLySEu7hOE/venv/lib/python3.12/site-packages/arbor_agent-0.1.1.dev254+g6b3595f3b.dist-info
aros_entry=arbor.cli.aros_app:main
arbor_entry=arbor.cli.app:main
editable_source_on_sys_path=false
main_editable_finder_loaded=false
```

The generated `aros` and `arbor` scripts both used this shebang:

```text
#!/tmp/aros-wave1-clean.pLySEu7hOE/venv/bin/python
```

## Installed-command smoke

Every installed Python command below used the isolated home and this ordered
runtime path:

```bash
env \
  HOME=/tmp/aros-wave1-clean.pLySEu7hOE/home \
  PYTHONNOUSERSITE=1 \
  PYTHONPATH=/tmp/aros-wave1-clean.pLySEu7hOE/venv/lib/python3.12/site-packages:/workspace/Arbor/.venv/lib/python3.12/site-packages
```

Direct and mounted help were exercised with the installed scripts:

```bash
/tmp/aros-wave1-clean.pLySEu7hOE/venv/bin/aros --help
/tmp/aros-wave1-clean.pLySEu7hOE/venv/bin/aros run --help
```

Captured assertions reported:

```text
direct_surface=init,boot,status,start,run
nested_aros_count=0
run_usage=Usage: aros run [OPTIONS] COMMAND [ARGS]...
run_surface=start,status,list,tail,stop
```

The packaged legacy CLI then exercised both OAuth provider imports:

```bash
/tmp/aros-wave1-clean.pLySEu7hOE/venv/bin/arbor login status
```

It exited 1, the command's expected unauthenticated status, after printing:

```text
not signed in — run `arbor login openai` or `arbor login claude`
```

There was no `ModuleNotFoundError`; both OAuth modules resolved from the wheel
as shown above.

A new Git workspace was initialized and configured:

```bash
mkdir /tmp/aros-wave1-clean.pLySEu7hOE/workspace-temp-first
git -C /tmp/aros-wave1-clean.pLySEu7hOE/workspace-temp-first init -q
git -C /tmp/aros-wave1-clean.pLySEu7hOE/workspace-temp-first \
  config user.email aros-smoke@example.invalid
git -C /tmp/aros-wave1-clean.pLySEu7hOE/workspace-temp-first \
  config user.name "AROS Smoke"
/tmp/aros-wave1-clean.pLySEu7hOE/venv/bin/aros init \
  --cwd /tmp/aros-wave1-clean.pLySEu7hOE/workspace-temp-first \
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
  "root": "/tmp/aros-wave1-clean.pLySEu7hOE/workspace-temp-first",
  "updated": []
}
```

Status and boot used the installed direct entry:

```bash
/tmp/aros-wave1-clean.pLySEu7hOE/venv/bin/aros status \
  --cwd /tmp/aros-wave1-clean.pLySEu7hOE/workspace-temp-first --json
/tmp/aros-wave1-clean.pLySEu7hOE/venv/bin/aros boot \
  --cwd /tmp/aros-wave1-clean.pLySEu7hOE/workspace-temp-first
```

The status receipt established an initialized repository with unborn `master`,
the four expected untracked files, no runs, a durable mission and working-memory
view, and no frontier view:

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
        "path": "/tmp/aros-wave1-clean.pLySEu7hOE/workspace-temp-first"
      }
    ],
    "worktrees_truncated": false
  },
  "initialized": true,
  "root": "/tmp/aros-wave1-clean.pLySEu7hOE/workspace-temp-first",
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

Boot reproduced the durable mission and current workspace state. Its leading
receipt was:

```text
# AROS Boot

## Mission and constraints — AROS.md

# AROS Project

## Mission

Verify direct AROS entry
```

The compatibility route was invoked separately with the installed `arbor`
script. A captured-output assertion compared both command surfaces and stderr:

```bash
/tmp/aros-wave1-clean.pLySEu7hOE/venv/bin/arbor aros --help
```

```text
direct_surface=init,boot,status,start,run
legacy_surface=init,boot,status,start,run
nested_aros_count=0
deprecation_warning_count=1
deprecation_warning=warning: arbor aros is deprecated; use aros directly
```

Finally, a path/type assertion after boot produced:

```text
required_files=AROS.md,AGENTS.md,memory/NOW.md
required_directories=memory,.aros,.worktree
mission=Verify direct AROS entry
.arbor_present=false
```

The real initialized workspace therefore has `AROS.md`, `AGENTS.md`, durable
`memory/NOW.md`, `.aros`, and `.worktree`, and it does not create `.arbor`.

## Superseded evidence and preserved historical observation

The immediately preceding clean-wheel receipt at source commit
`2b4ae0866d494822a98caa553ce1df3615359aa3` used
`arbor_agent-0.1.1.dev252+g2b4ae0866-py3-none-any.whl`, size 701247 bytes,
and SHA-256
`784e654185ebec1df1bb98411fc6c9fd30b17445b4c43d9b2cc5719ec398dfd3`.
It used PEP 517 build isolation and remains a valid historical artifact, but
its source, version, hash, and temporary paths are superseded by the current
receipt above.

An earlier clean-wheel receipt at source commit
`c59a4d51299ebf8b10f3e5523c87d50961e7c040` used
`arbor_agent-0.1.1.dev248+gc59a4d512-py3-none-any.whl`, size 700819 bytes,
and SHA-256
`4c55e2ff6c5ef12aca41f259e75592d97584c96a908c7d2dc761bcaa97c7af5b`.
It used PEP 517 build isolation and remains a valid historical artifact, but
its source, version, hash, and temporary paths are superseded by the current
receipt above.

The earlier pre-isolation receipt at source commit
`46a68583a6031ce9dec36a740be0ed8f3e8e0fc3` used
`arbor_agent-0.0.0-py3-none-any.whl`, SHA-256
`c1e2e7e401f0963d7116cbf9ba8cf8dea8f64d1162b065d29bf8314816e449a4`, and
`--no-build-isolation`. That source hash, wheel, version, hash, build method,
and its temporary paths are superseded historical evidence; they are not
current commissioning receipts.

During that earlier evidence run, one intermediate full-suite repeat produced
`614 passed, 1 failed, 6 skipped` in the pre-existing stop-delivery timing
test. Preserved artifacts showed that TERM was delivered and the run was
cancelled before the client recorded `delivered=false`. No Wave 1 source or
test change was made. The test then passed 10/10 isolated reruns, and subsequent
full-suite runs passed. This preserves the observation; it does not claim that
the timing race was fixed. The current clean-source pre-verification above
completed with `789 passed, 6 skipped`.

## Limits and prerequisites

- PEP 517 build dependencies were isolated. Runtime dependencies were not:
  the wheel itself was installed with `--no-deps`, and third-party imports were
  supplied by the main venv's physical site-packages after the temporary wheel
  site.
- The smoke proves the installed `aros` command surface, mounted `run` group,
  packaged OAuth status imports, compatibility warning, and real
  init/status/boot path. It does not authenticate a provider, run `aros start`,
  or launch an experiment with `aros run start`.
- Durable launch requires a clean committed Git HEAD and `tmux`. The default
  `isolated-linux` run profile additionally requires supported x86_64/aarch64
  Linux, exactly Landlock ABI 4, `libseccomp`, and `O_PATH`; `trusted-local` is
  explicitly not a security sandbox.
- Wave 1 does not claim child task substrate, deterministic/protected
  evaluation, migration adapters, MCP parity, semantic migration, Arbor
  retirement, or full Design Book commissioning.

## Exit result

At source commit `6b3595f3b55028cea09746da5df8193953a85583`, a wheel built
from a genuinely clean committed-source clone with PEP 517 build isolation
contains every configured package, including the complete OAuth package. Its
setuptools-scm version is `0.1.1.dev254+g6b3595f3b`, not `0.0.0`. The installed
direct entry creates and boots a fresh AROS workspace without `.arbor` state,
and the transitional route exposes the same five-command surface with exactly
one warning. This satisfies the narrow Wave 1 public-entry gate only, subject
to the runtime dependency-seeding limitation above.
