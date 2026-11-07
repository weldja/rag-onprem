#!/usr/bin/env bash
# End-to-end check of milestone 1 against the running stack:
# ingest the sample PDF, then run the compose-marked tests (health, cited
# answer on the right page, off-topic question -> not found).
source "$(dirname "$0")/_common.sh"
require_env

bash "$ROOT/scripts/ingest.sh" "$ROOT/samples/sample.pdf"

docker build --target dev -t rag-onprem/api:test "$ROOT_NATIVE/backend"
docker run --rm --network rag-onprem_edge -e RAG_BASE_URL=http://proxy \
    rag-onprem/api:test pytest -m compose -v "$@"
