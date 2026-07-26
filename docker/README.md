# Running Arbor in Docker

A security-hardened container image. Default mode is **MCP over stdio**; every
`arbor` subcommand is selectable via the `ARBOR_MODE` env var.

## Security posture

| Measure | How |
|---|---|
| Non-root | Image builds an `arbor` user; compose runs as `user: <uid>:<gid>` |
| Immutable rootfs | `read_only: true` — only the mounts below are writable |
| Secrets in RAM only | `~/.arbor` is tmpfs; the API key written by `config init` never touches disk |
| No capabilities | `cap_drop: [ALL]` |
| No escalation | `security_opt: [no-new-privileges:true]` |
| Resource caps | `memory: 2g`, `cpus: 2.0` (tune in `.env`) |
| Isolated network | custom `arbor-net` bridge (no host networking) |
| PID 1 hygiene | `tini` reaps zombies and forwards signals |

## Quick start

```bash
cp docker/.env.example .env
# edit .env: set ARBOR_API_KEY and ARBOR_UID/ARBOR_GID (run `id -u` / `id -g`)
mkdir -p workspace

docker compose build
```

### MCP (default) — stdio, driven by an MCP client

```bash
docker compose run --rm arbor          # stdin attached; speaks MCP over stdio
```

Point an MCP client (e.g. Claude Code) at:
```json
{ "mcpServers": { "arbor": { "command": "docker", "args": ["compose", "-f", "/abs/path/to/Arbor/docker-compose.yml", "run", "--rm", "arbor"] } } }
```

### Other modes

```bash
# doctor / version — one-shot
ARBOR_MODE=doctor docker compose run --rm arbor
ARBOR_MODE=version docker compose run --rm arbor

# run — research session (needs a git repo in ./workspace)
ARBOR_MODE=run docker compose run --rm arbor --yes "improve accuracy" --yes-cwd /workspace

# web — read-only monitor (long-running, exposes WEB_PORT)
ARBOR_MODE=web ARBOR_SESSION=my-run docker compose up
# → http://localhost:8765
```

## Mode reference

| `ARBOR_MODE` | Invocation | Notes |
|---|---|---|
| `mcp` *(default)* | `run --rm arbor` | stdio; the MCP client spawns it |
| `run` | `run --rm arbor [args]` | research session; workspace must be a git repo |
| `web` | `up` | needs `ARBOR_SESSION`; serves `WEB_PORT` |
| `doctor` | `run --rm arbor` | checks PATH, git, API keys |
| `version` | `run --rm arbor` | |
| `config` | `run --rm arbor show` | `show` / `init` via extra args |
| `replay`/`report`/`export` | `run --rm arbor <session> [args]` | operate on a past session |
| `idea-check`/`quickstart`/`benchmark`/`setup`/`install`/`uninstall`/`login` | `run --rm arbor [args]` | pass subcommand args |

Extra args after the mode are forwarded to `arbor <mode>`.

## Notes

- **UID matching.** The workspace bind mount must be writable by the container
  user. Set `ARBOR_UID`/`ARBOR_GID` in `.env` to your host uid/gid (`id -u`/`id -g`).
  The host GID (e.g. macOS 20) is reused if it already exists in the image.
- **`arbor run` may need a writable site-packages** (if an experiment does
  `pip install`). The rootfs is read-only by default; either let the project
  install with `--user` (writes to the tmpfs home) or relax with a
  `docker-compose.override.yml` setting `read_only: false`.
- **`arbor web` binds 127.0.0.1.** The entrypoint runs `socat` to re-expose it on
  `0.0.0.0:${WEB_PORT}` so the host-mapped port works — no host networking needed.
- **Config from env.** If `~/.arbor/config.yaml` is absent and `ARBOR_PROVIDER`
  is set, the entrypoint runs `arbor config init` to generate it from env vars
  (key included). To use a pre-built config instead, mount it over
  `/home/arbor/.arbor/config.yaml`.
