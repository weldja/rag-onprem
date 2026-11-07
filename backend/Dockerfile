# syntax=docker/dockerfile:1
# Dependencies are installed at build time from uv.lock; nothing is downloaded
# when the container runs.
FROM python:3.12-slim AS base
COPY --from=ghcr.io/astral-sh/uv:0.8.4 /uv /bin/uv
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/app
WORKDIR /app
COPY pyproject.toml uv.lock ./

# --- runtime image ---
FROM base AS runtime
RUN uv sync --frozen --no-dev --no-install-project
COPY . .
RUN sed -i 's/\r$//' docker-entrypoint.sh && chmod +x docker-entrypoint.sh \
    && useradd --system --uid 10001 --home-dir /app app
USER app
EXPOSE 8000
ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

# --- dev/test image: adds pytest, ruff, mypy ---
FROM base AS dev
RUN uv sync --frozen --no-install-project
COPY . .
CMD ["pytest"]
