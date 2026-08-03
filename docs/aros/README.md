# AROS

AROS is the Agent-centric research operating system being commissioned in this repository.

## Available now

The exposed command surface for the direct `aros` entry is:

- `aros init`
- `aros boot`
- `aros status`
- `aros start`
- `aros run start|status|list|tail|stop`
- `aros task create|start|status|list|message|stop|collect|preserve|prune`

### Runtime requirements

- Durable launch with `aros run start` requires a clean committed Git HEAD and `tmux`.
- `aros task start` requires its immutable brief to be committed at a clean parent HEAD and requires `tmux`; collection records reviewed B-C-R pointers without merging or cherry-picking the child.
- The default `isolated-linux` profile requires a supported Linux architecture (x86_64 or aarch64), exactly Landlock ABI 4, `libseccomp`, and `O_PATH` support; it fails closed instead of downgrading.
- `trusted-local` is explicitly not a security sandbox and must be selected explicitly.
- Child tasks currently run as `trusted-local` with application-level isolation and `capabilities_enforced=false`; brief capability flags are durable audit declarations, not an OS security boundary. Launch and final receipts record whether the filesystem enforces requested file modes. On a mode-normalizing filesystem, integrity checks remain active but `filesystem_permissions_enforced=false`, so untrusted adapters or secrets are out of scope.

## Not yet implemented

- deterministic/protected evaluation
- migration adapters
- MCP parity
- Arbor retirement

## Compatibility

The direct `aros` command is the public AROS entry. `arbor aros` is a temporary forwarding compatibility route.

CI hard-gates machine-decidable boundaries: transitive project-import reachability from every `src/aros/**/*.py` module and the direct adapters, source-path growth, the compatibility-shim hash, and legacy LOC non-increase. The conservative module-scope static graph indexes every configured local Python package and follows project-local imports through `arbor.core`, `arbor.cli.user_config`, and `arbor._app`; direct AROS modules and adapters also retain their dynamic-execution checks, with a fresh runtime import as a secondary check.

Within `src/`, only `src/aros/`, `src/cli/aros_app.py`, and `src/cli/commands/aros_cmd.py` may gain source lines or files. All non-allowlisted legacy source paths under `src/` reject added lines and paths; `src/core/` remains legacy-frozen, so legacy source LOC may only stay level or decrease. The especially frozen paths are `src/coordinator`, `src/executor`, `src/run.py`, `src/review.py`, and `src/cli/commands/run.py`. Pure deletion is allowed. The warning in `src/cli/app.py` is not generally growth-allowlisted: the named sunset gate `AROS_RETIREMENT_GATE_E4` accepts only its exact approved Git blob until that shim is deleted.

These mechanical results do not establish semantic duplication or equivalence. A new or padded copy under `src/aros/` can pass the mechanical gate and still requires module commissioning review. A padded copy under `src/core/` or another non-allowlisted source path fails path growth independently of Git similarity. The path gate treats an exact `R100` move from `src/` to outside `src/` as no growth inside its guarded source scope; commissioning review must confirm that the destination is not configured as a Python package and that no remaining entry or import refers to the moved module. Other `arbor` commands remain legacy implementations until migrated.
