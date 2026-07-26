# syntax=docker/dockerfile:1.7
#
# Arbor — security-hardened container. Default mode: MCP over stdio.
#
# Build from the repo root:   docker build -t arbor .
# Or via compose:             docker compose build   (then see docker-compose.yml)
# MCP (default, stdio):       docker compose run --rm arbor
# Web monitor:                ARBOR_MODE=web ARBOR_SESSION=… docker compose up

FROM python:3.12-slim

# ── unprivileged user (the container never runs as root) ───────────────────
ARG ARBOR_UID=1000
ARG ARBOR_GID=1000
# The host GID (e.g. macOS 20=dialout) can collide with an existing Debian group,
# so reuse it instead of trying to create it.
RUN if ! getent group "${ARBOR_GID}" >/dev/null; then groupadd --gid "${ARBOR_GID}" arbor; fi \
 && useradd --uid "${ARBOR_UID}" --gid "${ARBOR_GID}" --create-home --shell /bin/sh arbor

# ── runtime deps ───────────────────────────────────────────────────────────
# git   — worktree isolation for `arbor run` (and a `doctor` check)
# tini  — PID 1: reaps zombies, forwards INT/TERM cleanly
# socat — re-exposes `arbor web` (binds 127.0.0.1 only) on 0.0.0.0 without
#         resorting to host networking (preserves network isolation)
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates tini socat \
 && rm -rf /var/lib/apt/lists/*

# ── install Arbor from the repo source + the MCP SDK ───────────────────────
# The default mode is `arbor mcp` (stdio); the MCP SDK is the project's optional
# `[mcp]` extra (pyproject.toml). `.dockerignore` keeps this context lean.
COPY . /srv/arbor
RUN pip install --no-cache-dir "/srv/arbor[mcp]"

# ── environment ────────────────────────────────────────────────────────────
ENV HOME=/home/arbor \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ARBOR_MODE=mcp

WORKDIR /workspace

COPY docker/entrypoint.sh /usr/local/bin/arbor-entrypoint
RUN chmod 0755 /usr/local/bin/arbor-entrypoint

# Drop privileges; tini re-execs the entrypoint as PID 1.
USER arbor
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/arbor-entrypoint"]
# Extra args forwarded to the subcommand (e.g. `run --rm arbor --help`).
CMD []
