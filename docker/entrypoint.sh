#!/bin/sh
# Arbor entrypoint: generate ~/.arbor/config.yaml from env (if absent), then
# dispatch on ARBOR_MODE (default: mcp). Extra CMD args are forwarded to the
# subcommand as "$@".
set -eu

CONFIG_DIR="${HOME}/.arbor"
CONFIG_FILE="${CONFIG_DIR}/config.yaml"
mkdir -p "$CONFIG_DIR"

# Build the config from env vars. Wrapped in a function so the `set --` used to
# assemble args does not clobber the script's own "$@" (CMD args). POSIX sh
# gives each function its own positional parameters.
gen_config() {
    set -- --force --provider "$ARBOR_PROVIDER"
    [ -n "${ARBOR_MODEL:-}" ]    && set -- "$@" --model "$ARBOR_MODEL"
    [ -n "${ARBOR_BASE_URL:-}" ] && set -- "$@" --base-url "$ARBOR_BASE_URL"
    [ -n "${ARBOR_API_KEY:-}" ]  && set -- "$@" --api-key "$ARBOR_API_KEY"
    arbor config init "$@"
}

# The home dir is tmpfs in compose, so any key written here lives in RAM only.
# Redirect config-init's stdout to stderr: `arbor config init` echoes the config
# to stdout, which would corrupt the MCP JSON-RPC stream (MCP owns stdout).
if [ ! -f "$CONFIG_FILE" ] && [ -n "${ARBOR_PROVIDER:-}" ]; then
    gen_config 1>&2
elif [ ! -f "$CONFIG_FILE" ]; then
    echo "arbor: no config at $CONFIG_FILE and ARBOR_PROVIDER is unset." >&2
    echo "       Set ARBOR_PROVIDER/ARBOR_MODEL/ARBOR_API_KEY or mount a config." >&2
fi

mode="${ARBOR_MODE:-mcp}"

case "$mode" in
    mcp)
        # stdio transport — an MCP client drives this as a one-shot process.
        exec arbor mcp "$@"
        ;;
    web)
        # `arbor web` binds 127.0.0.1 only (src/webui/server.py). socat re-exposes
        # it on 0.0.0.0:${WEB_PORT} so the host-mapped port is reachable, without
        # weakening isolation with host networking.
        : "${ARBOR_SESSION:=}"
        : "${WEB_PORT:=8765}"
        : "${WEB_INTERNAL_PORT:=12765}"
        : "${ARBOR_CWD:=/workspace}"
        if [ -z "$ARBOR_SESSION" ]; then
            echo "arbor web: ARBOR_SESSION (session name) is required." >&2
            exit 2
        fi
        arbor web --no-open --port "$WEB_INTERNAL_PORT" --cwd "$ARBOR_CWD" "$ARBOR_SESSION" &
        webpid=$!
        socat TCP-LISTEN:"$WEB_PORT",fork,reuseaddr,bind=0.0.0.0 \
              TCP4:127.0.0.1:"$WEB_INTERNAL_PORT" &
        socatpid=$!
        trap 'kill "$webpid" "$socatpid" 2>/dev/null || true; exit 0' INT TERM
        echo "arbor web: read-only monitor on 0.0.0.0:${WEB_PORT} (session: ${ARBOR_SESSION})" >&2
        wait "$webpid"
        ;;
    run|doctor|version|quickstart|idea-check|benchmark|replay|export|report|setup|config|login|install|uninstall)
        exec arbor "$mode" "$@"
        ;;
    *)
        echo "arbor: unknown ARBOR_MODE='$mode'." >&2
        echo "       expected: mcp|run|web|doctor|version|config|replay|report|export|idea-check|quickstart|benchmark|setup|install|uninstall|login" >&2
        exec arbor --help
        ;;
esac
