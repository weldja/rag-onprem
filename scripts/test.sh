#!/usr/bin/env bash
# Run lint, type checks and unit tests in the backend dev image, with the
# container's network disabled entirely (--network none).
#   bash scripts/test.sh            # everything
#   bash scripts/test.sh -k chunk   # extra args go to pytest
source "$(dirname "$0")/_common.sh"

docker build --target dev -t rag-onprem/api:test "$ROOT_NATIVE/backend"
docker run --rm --network none rag-onprem/api:test \
    sh -c 'ruff check . && ruff format --check . && mypy && pytest "$@"' -- "$@"
