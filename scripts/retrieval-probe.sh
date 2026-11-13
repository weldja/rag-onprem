#!/usr/bin/env bash
# Show what each eval question retrieves (cosine + rerank scores, expected
# sources marked *), without calling the LLM. For tuning MIN_SIMILARITY and
# RERANK_MIN_SCORE. Run scripts/eval.sh once first (it creates the corpus).
source "$(dirname "$0")/_common.sh"
require_env

docker run --rm --network rag-onprem_backend --entrypoint python \
    -v "$(native_path "$ROOT/backend/evals"):/app/evals:ro" \
    -e DATABASE_URL="postgresql+psycopg://rag:${POSTGRES_PASSWORD}@db:5432/rag" \
    -e OLLAMA_URL=http://ollama:11434 \
    -e EMBED_MODEL="${EMBED_MODEL:-bge-m3}" \
    -e LLM_MODEL="${LLM_MODEL:-qwen3:4b-instruct-2507-q4_K_M}" \
    -e RERANKER_MODEL="${RERANKER_MODEL:-}" \
    -e MIN_SIMILARITY="${MIN_SIMILARITY:-0.35}" \
    -e RERANK_MIN_SCORE="${RERANK_MIN_SCORE:-}" \
    rag-onprem/api:dev -m evals.retrieval_probe "$@"
