#!/usr/bin/env bash
# Answer-quality evaluation against the running stack. Reports retrieval
# recall, citation recall/precision, answer accuracy and abstention, and writes
# a JSON report to eval-results/ (git-ignored).
#   bash scripts/eval.sh [--detach] [--label NAME] [--only CASE_ID ...]
# --detach runs the eval in a named container (rag-onprem-eval) that survives
# this shell; follow it with `docker logs -f rag-onprem-eval`. A full run on a
# CPU-only machine takes 10-15 minutes.
source "$(dirname "$0")/_common.sh"
require_env

run_flags=(--rm)
if [ "${1:-}" = "--detach" ]; then
    shift
    docker rm -f rag-onprem-eval >/dev/null 2>&1 || true
    run_flags=(-d --rm --name rag-onprem-eval)
fi

password="$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 24 || true)"
printf '%s\n' "$password" | compose exec -T api \
    python -m app.admin create-user e2e-admin --admin --password-stdin --reset-if-exists >/dev/null

mkdir -p "$ROOT/eval-results"
docker build -q --target dev -t rag-onprem/api:test "$ROOT_NATIVE/backend" >/dev/null
docker run "${run_flags[@]}" --network rag-onprem_edge \
    -v "$(native_path "$ROOT/eval-results"):/results" \
    -e PYTHONUNBUFFERED=1 \
    -e RAG_BASE_URL=https://proxy \
    -e RAG_ADMIN_USER=e2e-admin \
    -e RAG_ADMIN_PASSWORD="$password" \
    rag-onprem/api:test python -m evals.run_eval "$@"
