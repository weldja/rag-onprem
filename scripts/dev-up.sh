#!/usr/bin/env bash
# Build and start the dev stack, then wait until the API reports healthy.
# The first build downloads the models into the ollama image (several GB).
source "$(dirname "$0")/_common.sh"

if [ ! -f "$ROOT/.env" ]; then
    password="$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 32 || true)"
    sed "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=${password}/" "$ROOT/.env.example" >"$ROOT/.env"
    echo "Created .env with a random POSTGRES_PASSWORD"
    set -a; . "$ROOT/.env"; set +a
    HTTPS_PORT="${HTTPS_PORT:-443}"
    BASE_URL="https://127.0.0.1:${HTTPS_PORT}"
fi

bash "$ROOT/scripts/make-dev-cert.sh"

compose up -d --build "$@"

echo "Waiting for ${BASE_URL}/api/health (the first start warms up the model)..."
for _ in $(seq 1 120); do
    if "${CURL[@]}" -f "${BASE_URL}/api/health" >/dev/null 2>&1; then
        "${CURL[@]}" "${BASE_URL}/api/health"
        echo
        echo "Open ${BASE_URL}/ (create the first admin with: bash scripts/create-admin.sh USERNAME)"
        exit 0
    fi
    sleep 5
done

echo "API not healthy after 10 minutes." >&2
compose ps
compose logs --tail=50 api ollama proxy
exit 1
