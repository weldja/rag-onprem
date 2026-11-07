#!/usr/bin/env bash
# Run the database tests (access control, sessions, job queue) against a
# throwaway `rag_test` database on the running stack's Postgres. The test
# container joins only the internal backend network: no internet access.
source "$(dirname "$0")/_common.sh"
require_env

docker build --target dev -t rag-onprem/api:test "$ROOT_NATIVE/backend"
docker run --rm --network rag-onprem_backend \
    -e DATABASE_URL="postgresql+psycopg://rag:${POSTGRES_PASSWORD}@db:5432/rag_test" \
    rag-onprem/api:test pytest -m db "$@"
