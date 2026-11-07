#!/bin/sh
# Start Ollama, then load and warm both models so the first real question
# doesn't pay the model-load cost. Adapted from rag-claude/ollama/entrypoint.sh.
# Never pulls: models are baked into the image.

ollama serve &
pid=$!
trap 'kill -TERM "$pid" 2>/dev/null; wait "$pid"' TERM INT

i=0
while [ "$i" -lt 60 ]; do
    ollama list >/dev/null 2>&1 && break
    i=$((i + 1))
    sleep 1
done

if ollama run "${LLM_MODEL}" "Reply with the single word: ready" >/dev/null 2>&1; then
    echo "[rag-onprem] LLM loaded and warmed up: ${LLM_MODEL}"
else
    echo "[rag-onprem] LLM warm-up failed - the first question will be slow"
fi

wait "$pid"
