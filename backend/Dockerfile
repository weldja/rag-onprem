# syntax=docker/dockerfile:1
# Dependencies are installed at build time from uv.lock; nothing is downloaded
# when the container runs.
# Base images are pinned by digest so release builds are reproducible.
# Update deliberately: see "Updating pinned images" in docs/operations.md.
FROM python:3.12-slim@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54 AS base
COPY --from=ghcr.io/astral-sh/uv:0.8.4@sha256:40775a79214294fb51d097c9117592f193bcfdfc634f4daa0e169ee965b10ef0 /uv /bin/uv
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/app \
    # No telemetry, no hub access at runtime (checked by tests/test_offline_env.py).
    # ORT_DISABLE_TELEMETRY stops onnxruntime's 1DS telemetry starting on import.
    HF_HUB_OFFLINE=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    ORT_DISABLE_TELEMETRY=1 \
    DO_NOT_TRACK=1
WORKDIR /app

# OCR engine and language packs, installed at build time (Apache-2.0).
# Tesseract language codes, space-separated, e.g. "eng deu fra".
ARG OCR_LANGUAGES="eng"
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr \
        $(for l in $OCR_LANGUAGES; do printf 'tesseract-ocr-%s ' "$l"; done) \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock ./

# --- runtime image ---
FROM base AS runtime
RUN uv sync --frozen --no-dev --no-install-project

# Optional reranker, downloaded HERE at build time (needs internet on the build
# machine only). Empty = no reranker. Must match RERANKER_MODEL at runtime.
ARG RERANKER_MODEL=""
RUN if [ -n "$RERANKER_MODEL" ]; then \
        HF_HUB_OFFLINE=0 python -c "import sys; \
from fastembed.rerank.cross_encoder import TextCrossEncoder as T; \
T(sys.argv[1], cache_dir='/opt/models/fastembed')" "$RERANKER_MODEL" \
        && chmod -R a+rX /opt/models; \
    fi
COPY . .
# /data/uploads is owned by the app user so a fresh named volume mounted there
# inherits that ownership.
RUN sed -i 's/\r$//' docker-entrypoint.sh && chmod +x docker-entrypoint.sh \
    && useradd --system --uid 10001 --home-dir /app app \
    && mkdir -p /data/uploads && chown app /data/uploads
USER app
EXPOSE 8000
ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

# --- dev/test image: adds pytest, ruff, mypy ---
FROM base AS dev
RUN uv sync --frozen --no-install-project
COPY . .
CMD ["pytest"]
