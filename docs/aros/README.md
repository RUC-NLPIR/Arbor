# AROS

AROS is the Agent-centric research operating system being commissioned in this repository.

## Available now

The exposed command surface for the direct `aros` entry is:

- `aros init`
- `aros boot`
- `aros status`
- `aros start`
- `aros run start|status|list|tail|stop`

### Runtime requirements

- Durable launch with `aros run start` requires a clean committed Git HEAD and `tmux`.
- The default `isolated-linux` profile requires a supported Linux architecture (x86_64 or aarch64), exactly Landlock ABI 4, `libseccomp`, and `O_PATH` support; it fails closed instead of downgrading.
- `trusted-local` is explicitly not a security sandbox and must be selected explicitly.

## Not yet implemented

- child task substrate
- deterministic/protected evaluation
- migration adapters
- MCP parity
- Arbor retirement

## Compatibility

The direct `aros` command is the public AROS entry. `arbor aros` is a temporary forwarding compatibility route.

CI freezes growth across all non-allowlisted legacy source paths under `src/`. Only `src/aros/`, `src/core/`, the direct AROS adapters `src/cli/aros_app.py` and `src/cli/commands/aros_cmd.py`, and the temporary `src/cli/app.py` shim are growth-allowlisted. The especially frozen semantic routes are `src/coordinator`, `src/executor`, `src/run.py`, `src/review.py`, and `src/cli/commands/run.py`. Other `arbor` commands remain legacy implementations until migrated.
