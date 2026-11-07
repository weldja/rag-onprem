#!/usr/bin/env bash
# Shared helpers for scripts/*.sh. Source this, don't run it.
set -euo pipefail

# Git Bash (MSYS) rewrites arguments that look like POSIX paths, which breaks
# container paths such as /tmp/x. Turn that off and convert host paths
# explicitly with native_path instead.
export MSYS_NO_PATHCONV=1

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

native_path() {
    if command -v cygpath >/dev/null 2>&1; then
        cygpath -m "$1"
    else
        realpath "$1"
    fi
}

ROOT_NATIVE="$(native_path "$ROOT")"

if [ -f "$ROOT/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    . "$ROOT/.env"
    set +a
fi
HTTPS_PORT="${HTTPS_PORT:-443}"
BASE_URL="https://127.0.0.1:${HTTPS_PORT}"
# Dev certificates are self-signed, so the helper scripts skip verification.
CURL=(curl -sS -k)

compose() {
    docker compose \
        --env-file "$ROOT_NATIVE/.env" \
        -f "$ROOT_NATIVE/deploy/docker-compose.yml" \
        -f "$ROOT_NATIVE/deploy/docker-compose.dev.yml" \
        "$@"
}

require_env() {
    if [ ! -f "$ROOT/.env" ]; then
        echo "No .env found. Run scripts/dev-up.sh first (it creates one)." >&2
        exit 1
    fi
}
