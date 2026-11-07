#!/usr/bin/env bash
# Shared helpers for the release scripts. Sourced, not run.
set -euo pipefail

# Git Bash on Windows rewrites POSIX-looking arguments; harmless elsewhere.
export MSYS_NO_PATHCONV=1

BUNDLE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$BUNDLE_DIR"

if [ -t 1 ]; then
    BOLD=$'\e[1m' RED=$'\e[31m' YELLOW=$'\e[33m' GREEN=$'\e[32m' RESET=$'\e[0m'
else
    BOLD="" RED="" YELLOW="" GREEN="" RESET=""
fi
info() { printf '%s\n' "${BOLD}==>${RESET} $*"; }
ok()   { printf '%s\n' "  ${GREEN}ok${RESET}    $*"; }
warn() { printf '%s\n' "  ${YELLOW}warn${RESET}  $*"; }
fail() { printf '%s\n' "  ${RED}FAIL${RESET}  $*"; }
die()  { printf '%s\n' "${RED}error:${RESET} $*" >&2; exit 1; }

# Absolute host path in a form `docker -v` understands (Windows: C:/...).
native_path() {
    if command -v cygpath >/dev/null 2>&1; then cygpath -m "$1"; else realpath "$1"; fi
}

load_env() {
    [ -f .env ] || die ".env not found in $BUNDLE_DIR (run: bash install.sh)"
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
}

dc() { docker compose "$@"; }

# Set KEY=VALUE in .env, replacing an existing line or appending.
set_env() {
    local key="$1" value="$2"
    if grep -qE "^${key}=" .env; then
        sed -i.bak "s|^${key}=.*|${key}=${value}|" .env && rm -f .env.bak
    else
        printf '%s=%s\n' "$key" "$value" >>.env
    fi
}

random_secret() { LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c "${1:-32}" || true; }

# Wait until the API reports healthy (checked from inside the container, so
# it works whatever the host firewall, ports or certificate).
wait_healthy() {
    local timeout="${1:-900}" waited=0
    while [ "$waited" -lt "$timeout" ]; do
        if dc exec -T api python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=5)" >/dev/null 2>&1; then
            return 0
        fi
        sleep 5
        waited=$((waited + 5))
    done
    return 1
}

verify_checksums() {
    local dir="${1:-.}"
    [ -f "$dir/SHA256SUMS" ] || die "$dir/SHA256SUMS missing: incomplete bundle?"
    (cd "$dir" && sha256sum --quiet -c SHA256SUMS) || die "Checksum mismatch in $dir: the bundle is damaged or was modified"
}
