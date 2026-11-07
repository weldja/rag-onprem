#!/usr/bin/env bash
# End-to-end checks against the running stack:
#   1. database tests (ACLs, sessions, queue) on a throwaway rag_test database
#   2. ingest the sample PDF via the CLI (shared with "All staff")
#   3. the compose-marked API tests over HTTPS through nginx, signed in as an
#      `e2e-admin` account whose password is regenerated on every run
source "$(dirname "$0")/_common.sh"
require_env

bash "$ROOT/scripts/test-db.sh" -q
bash "$ROOT/scripts/ingest.sh" "$ROOT/samples/sample.pdf"

password="$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 24 || true)"
printf '%s\n' "$password" | compose exec -T api \
    python -m app.admin create-user e2e-admin --admin --password-stdin --reset-if-exists

docker build --target dev -t rag-onprem/api:test "$ROOT_NATIVE/backend"
docker run --rm --network rag-onprem_edge \
    -e RAG_BASE_URL=https://proxy \
    -e RAG_ADMIN_USER=e2e-admin \
    -e RAG_ADMIN_PASSWORD="$password" \
    rag-onprem/api:test pytest -m compose -v "$@"
