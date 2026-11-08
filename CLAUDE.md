# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Project

**rag-onprem** is a retrieval-augmented generation (RAG) application for small enterprises. Users upload
internal documents and ask questions in natural language. Answers are grounded in those documents and cite
their sources.

It is installed **inside the customer's own server environment** and must work fully air-gapped.

**Status:** milestone 1 of `docs/build-plan.md` (see *Current state* below). The stack below is the
chosen default. Flag it if a decision looks wrong rather than quietly working around it.

## Non-negotiable constraints

These override convenience, defaults, and library examples. If a task seems to need breaking one, stop and ask.

1. **No outbound network calls at runtime.** No cloud LLMs, hosted embedding APIs, SaaS vector DBs, CDNs,
   license servers, update checks, or "phone home" behaviour of any kind.
2. **No telemetry.** Disable it explicitly in every dependency that has it (see *Offline hardening*).
   Never add analytics, crash reporting, or error-tracking SDKs (Sentry, PostHog, etc.).
3. **Local models only.** The LLM, the embedding model and the reranker (if used) run on the customer's hardware.
   Model weights ship with the deployment. Nothing is downloaded on first start.
4. **Local vector store.** Vectors and document data never leave the customer's database.
5. **Deployable with Docker.** The whole system starts with `docker compose up` from pre-built images
   and pre-staged model files, with no internet connection.
6. **Customer data stays inside.** Don't log document contents or prompts at INFO level or above. Logs are
   written locally only.

Downloading things is allowed **at build time** (dependency install, image builds, fetching model weights
into an offline bundle). It is **never** allowed at container start or at request time.

## Proposed default stack

| Concern | Default | Why / notes |
|---|---|---|
| LLM runtime | **Ollama** (own container) | Simple to run, GPU or CPU, handles GGUF quantisation, local HTTP API. vLLM is the upgrade path for customers with larger GPUs. |
| LLM model | 7–9B instruct model, Q4_K_M GGUF (e.g. Qwen or Llama family) | Runs on a single 8–12 GB GPU or a strong CPU. Model name is config, not code. **Check the licence permits commercial redistribution before you ship it.** |
| Embeddings | **bge-m3** (or `nomic-embed-text`) served by Ollama | Multilingual, good retrieval quality. One model runtime for both LLM and embeddings. Embedding dimension is config. |
| Reranker (optional) | fastembed ONNX cross-encoder (e.g. `Xenova/ms-marco-MiniLM-L-6-v2`) inside the API container | Weights built into the image. Off by default (see `docs/eval-results.md`). Chosen over sentence-transformers to avoid pulling PyTorch (about 1 GB) into the image. |
| Vector store + DB | **PostgreSQL 16 + pgvector** | One stateful service holds documents, chunks, vectors, users, ACLs and job queue. Gives hybrid search (pgvector + `tsvector` full-text) and a single backup target. Qdrant is the alternative if vector scale outgrows Postgres. |
| Backend | **Python 3.12, FastAPI**, Pydantic v2, SQLAlchemy 2 + psycopg 3, Alembic | |
| Dependency mgmt | **uv** (`pyproject.toml` + `uv.lock`) | Reproducible locked installs. `uv run` avoids venv-activation differences on Windows. |
| Document parsing | `pypdf`, `python-docx`, `openpyxl`, `beautifulsoup4`, plain text/Markdown | Permissive licences. **Do not use PyMuPDF** (AGPL). OCR and layout-aware parsing (e.g. Docling) are phase 2. Their models must be baked into the image. |
| Background jobs | Postgres-backed queue (`SELECT … FOR UPDATE SKIP LOCKED`) run by a worker process | No Redis or Celery to deploy and support. |
| Frontend | **React + TypeScript + Vite**, built to static files | No runtime CDN. Fonts and icons are bundled. |
| Reverse proxy | **nginx** | Serves the frontend, proxies `/api`, terminates TLS using a customer-supplied certificate. Avoid Caddy's automatic HTTPS, which contacts ACME. |
| Auth | Local accounts (argon2 hashes) + session cookies; LDAP/AD later | Document-level ACLs are enforced **in the retrieval SQL**, not after retrieval. |
| Tests | pytest, pytest-asyncio, **pytest-socket** (`--disable-socket`, allow only localhost/compose hosts) | The test suite itself enforces "no egress". |
| Lint/format/types | ruff, mypy (strict on `app/`); eslint + prettier for frontend | |

## Planned architecture

```
browser ──HTTPS──> nginx ──> api (FastAPI) ──> postgres+pgvector
                                │   ▲
                                │   └── worker (ingestion: parse → chunk → embed → store)
                                └────> ollama (LLM + embeddings)
```

- **Ingestion:** upload → store the original file in a volume → job row → worker parses, chunks
  (token-aware, roughly 500–800 tokens with overlap, keeping heading/page metadata), embeds in batches, then writes
  chunks and vectors in a single transaction. Re-ingestion is idempotent, keyed on content hash.
- **Query:** embed question → hybrid retrieval (vector + full-text, merged with reciprocal rank fusion),
  filtered by the user's ACL → optional rerank → prompt with numbered context → stream answer (SSE)
  with citations back to document and page.
- **Model access goes through small interfaces** (`LLMClient`, `Embedder`, `Reranker`) in `app/llm/`.
  Implement them with plain `httpx` against Ollama's API. Don't pull in cloud-provider SDKs, even if they
  can point at a local endpoint, so it stays obvious that nothing goes external. Tests use fake implementations.
- **Store embedding model name and dimension alongside each vector.** Changing the embedding model means
  re-indexing. Never mix vectors from different models in one index.

### Layout (the release kit is `deploy/release/`, built by `scripts/build-release.sh`)

```
backend/
  app/            # api/, core/ (config, logging), db/, ingestion/, retrieval/, llm/, auth/
  migrations/     # Alembic
  tests/
  pyproject.toml, uv.lock, Dockerfile
frontend/         # Vite + React + TS, Dockerfile builds static assets into nginx image
deploy/
  docker-compose.yml           # production-style, internal network
  docker-compose.dev.yml       # dev overrides (bind mounts, hot reload, exposed ports)
  nginx/
  offline-bundle/              # scripts to docker save images + package model weights
scripts/          # bash helper scripts (dev up/down, pull models, export bundle)
docs/
```

## Offline hardening checklist

Apply these whenever you add the matching component:

- Compose: backend services sit on a network with `internal: true`. Only nginx publishes a port.
- Hugging Face / sentence-transformers: `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`,
  `HF_HUB_DISABLE_TELEMETRY=1`. Load models from a local path, never from a hub ID at runtime.
- onnxruntime (used by the fastembed reranker) starts 1DS telemetry **on import** unless
  `ORT_DISABLE_TELEMETRY=1` is set. It's set in the backend base image and enforced by
  `tests/test_offline_env.py`. Add any new library's switch to both.
- Ollama: models pre-pulled into the `ollama` volume, or imported from GGUF in the offline bundle. The app
  must **never** call `/api/pull`. On startup, check that the configured models exist and fail with a clear error if they don't.
- Avoid libraries with on-by-default telemetry (e.g. Chroma, LangChain tracing, Gradio). If one is
  unavoidable, disable it via env and add a test.
- Pin image tags **by digest** in production compose. No `:latest`.
- Frontend: no Google Fonts, CDNs, or external `<script>`/`<link>`. Use a CSP header that allows `'self'` only.
- Use no new dependency without checking its licence (permissive preferred, no AGPL/SSPL) and whether it makes
  network calls on import or first use.

## Configuration

- All settings come from environment variables via `pydantic-settings` (`app/core/config.py`), with
  `.env.example` committed and `.env` git-ignored.
- Model names, embedding dimension, chunk sizes, top-k, and the Ollama URL are configuration, never hard-coded.
- Defaults must work for a CPU-only server. GPU is an opt-in compose profile (`--profile gpu`).
- **Response-time target: answers within 10 seconds.** That needs the whole 7B model on an NVIDIA
  GPU with 8 GB+ (decided 2026-11-08; measurements in `docs/eval-results.md`, "Latency"). CPU and
  smaller GPUs still work, slowly; preflight warns about them. Don't trade answer quality for speed
  (e.g. a smaller model) without running the eval: `phi4-mini` was tried and dropped citations.
- **On a GPU that can't hold both models, keep the embedding model on the CPU (`EMBED_NUM_GPU=0`)**,
  or Ollama swaps the LLM out on every question. That alone took the 7B on a 4 GB card from ~30 s
  to a 9.9 s median. `install.sh` sets it, with `LLM_NUM_CTX=4096` and `LLM_MAX_TOKENS=768`, on
  cards under 6 GB. Check placement with `ollama ps` and timings with the api's "answer timing"
  log line.
- **Two bundles:** the standard one (7B, for 8 GB+ GPUs) and a small-GPU one with
  `qwen3:4b-instruct-2507` (Apache 2.0), which fits a 4 GB card and matched the 7B on the eval at
  ~5 s per question. Any model change needs the full eval, including the summary cases.

## Development environment (Windows 11 + Git Bash)

- Docker Desktop with the WSL2 backend. GPU passthrough needs a recent NVIDIA driver plus WSL2 CUDA support.
- **Line endings:** add a `.gitattributes` forcing `eol=lf` for `*.sh`, `*.py`, `Dockerfile*`, `*.yml`,
  `*.conf`, so scripts work inside Linux containers. Never commit CRLF shell scripts.
- **Path mangling:** Git Bash (MSYS) rewrites arguments that look like POSIX paths. Prefix with
  `MSYS_NO_PATHCONV=1` for commands such as `docker run -v "$(pwd)":/app …` or
  `docker exec … /bin/sh`, or use `//app`-style paths.
- **Interactive containers:** use `winpty docker exec -it …` if the TTY misbehaves.
- `make` isn't available by default. Put helpers in `scripts/*.sh` (with `#!/usr/bin/env bash` and
  `set -euo pipefail`) and run them as `bash scripts/<name>.sh`.
- Python: use `uv run …` rather than activating venvs (`.venv/Scripts/activate` differs on Windows).
- Bind-mounting the Windows filesystem into containers is slow. That's fine for dev hot-reload, but don't
  benchmark on it.
- Containers are Linux. Write code and scripts for Linux, not Windows-specific paths or behaviour.

## Commands

Run from the repo root in Git Bash. Everything runs in containers, so uv and Python aren't needed on the host.

```bash
bash scripts/dev-up.sh                  # build + start stack (creates .env on first run), wait for /api/health
bash scripts/dev-down.sh [-v]           # stop (-v also deletes the database volume)
bash scripts/test.sh                    # ruff + mypy + pytest in the dev image with --network none
bash scripts/test.sh -k citations       # extra args go to pytest
bash scripts/test-db.sh                 # DB tests (ACLs, sessions, queue) against a throwaway rag_test database
bash scripts/create-admin.sh alice      # first administrator (hidden password prompt)
bash scripts/ingest.sh samples/sample.pdf   # CLI ingest, shared with "All staff" (--group to change)
RAG_USER=alice RAG_PASSWORD=... bash scripts/ask.sh "How many days of leave can I carry over?"
bash scripts/e2e.sh                     # DB tests + ingest sample + HTTPS end-to-end tests (as e2e-admin)
bash scripts/make-dev-cert.sh           # self-signed cert for dev (dev-up.sh runs it if missing)
bash scripts/eval.sh --detach --label X # answer-quality eval (~15 min), JSON report in eval-results/
bash scripts/retrieval-probe.sh         # what each eval question retrieves, with scores; no LLM
bash scripts/build-release.sh 1.0.0     # offline bundle in release/ (customer default models from .env.example)
RELEASE_LLM_MODEL=qwen3:4b-instruct-2507-q4_K_M bash scripts/build-release.sh 1.0.0-small   # small-GPU bundle (< 8 GB)
bash scripts/egress-check.sh            # api/ollama/db must NOT reach the internet
```

- Models are baked into the ollama image at build time (`deploy/ollama/Dockerfile`). Changing
  `LLM_MODEL`/`EMBED_MODEL` in `.env` needs `docker compose build ollama`.
- Migrations run automatically on api start (`backend/docker-entrypoint.sh`).
- Update `backend/uv.lock` with:
  `docker run --rm -v "$(pwd -W)/backend:/app" -w /app ghcr.io/astral-sh/uv:0.8.4-python3.12-bookworm-slim uv lock`
- `samples/sample.pdf` is generated (fictional content) by `backend/tests/pdfgen.py`.
- `docs/build-plan.md` holds the milestone plan.

## Current state

Milestones 1–2 of `docs/build-plan.md` are implemented:
- Formats: PDF, DOCX, PPTX, XLSX, CSV, HTML, TXT/MD (`app/ingestion/loaders.py`). Spreadsheet chunks are
  `pre_chunked` (header repeated per chunk) and must not be re-split.
- Upload → original stored as `<sha256>.<ext>` in the `uploads` volume → `ingest_jobs` row → `worker`
  service (`app/worker.py`) claims it with `FOR UPDATE SKIP LOCKED`, indexes, marks the document ready.
  Re-index replaces a document's chunks in one transaction.
- Retrieval is hybrid (`app/retrieval/search.py`): pgvector + full-text, RRF-fused. Relevance gate: if the
  best cosine similarity < `MIN_SIMILARITY` the question is off-topic and the LLM isn't called.
- API: `GET /api/health`, `POST /api/ask`, `POST /api/ask/stream` (SSE: status/token/done/error; `done`
  carries the authoritative answer and sources), `POST|GET /api/documents`, `GET|DELETE /api/documents/{id}`,
  `POST /api/documents/{id}/reindex`.
- nginx (`proxy`, built from `frontend/`) serves the React app, terminates TLS (`deploy/certs/`) and
  proxies `/api`. It's the only service with published ports; api, worker, db and ollama sit on an
  `internal: true` network. CSP is `'self'` only; the nginx config is
  `deploy/nginx/templates/default.conf.template`.

Milestone 3 (auth and access control):
- Local accounts (argon2id), server-side sessions in Postgres (HttpOnly/SameSite=Strict/Secure cookie,
  only the token's SHA-256 is stored), lockout after repeated failures. First admin:
  `bash scripts/create-admin.sh USERNAME`. There's no default account.
- **Visibility rule** (`app/auth/acl.py`): admin, OR uploader, OR the document shares a group with the
  user. `can_see(viewer)` goes in the WHERE clause of every document or chunk query. Never filter
  results in Python. New retrieval or listing code must take a `Viewer` and have a DB test in
  `backend/tests/db/test_acl.py`.
- Every non-GET `/api/` request needs `X-Requested-With: rag-onprem` (CSRF guard in `app/main.py`).
- Migration 0003 put pre-existing documents in the "All staff" group; new users join it by default.
- Uploaded files are served as attachments except PDF/TXT, never as inline HTML.

Tests: `scripts/test.sh` (unit, offline), `scripts/test-db.sh` (real Postgres, `rag_test` DB),
`scripts/e2e.sh` (runs both DB and HTTPS end-to-end tests).

Milestone 4 (answer quality):
- **Prompt changes must go through the eval:** `bash scripts/eval.sh --detach --label X`, then compare
  with `docs/eval-results.md`. The 3B dev model is very sensitive to prompt wording. Never put example
  sentences in prompts, because the model copies them into answers. Add a case to `backend/evals/cases.json`
  for every failure found by hand.
- Conversation history: the client sends it and the server keeps none. The last `HISTORY_TURNS`
  exchanges go to the LLM with old `[n]` markers stripped, and short follow-ups also retrieve with the
  previous question.
- Summary mode: `mode: "summary"` + `document_ids` samples chunks evenly across the document.
- Retrieval drops chunks whose text duplicates an already selected chunk.
- Optional reranker (`RERANKER_MODEL`, fastembed/ONNX, built into the image) is off by default; see
  `docs/eval-results.md` for why.

Milestone 5 (shipping):
- `deploy/release/` holds the **customer-facing** kit: compose file (`pull_policy: never`, images
  `rag-onprem/*:${RAG_VERSION}`), GPU override, and `install`, `preflight`, `create-admin`, `backup`,
  `restore` and `upgrade` scripts. These run on customer Linux servers. Keep them POSIX-tool-only, run
  them via `bash x.sh`, and make sure they pass shellcheck (CI runs it).
- Customer documentation: `docs/operations.md` (shipped as the bundle's README.md). Update it with
  any operational change.
- Every base image is pinned by digest (Dockerfiles, `deploy/docker-compose.yml`,
  `scripts/make-dev-cert.sh`). Update pins deliberately (see the operations.md appendix).
- OCR (`app/ingestion/ocr.py`): Tesseract, installed in the image via apt with language packs from the
  `OCR_LANGUAGES` build arg (`OCR_IMAGE_LANGUAGES` in `.env`), and pypdfium2 for rendering. Poppler
  was avoided because it's GPL. Only PDF pages with fewer than `OCR_MIN_CHARS` characters of text
  layer are OCR'd. PNG, JPEG and TIFF uploads are OCR'd frame by frame. Chunks carry
  `metadata["ocr"]`. Tests build image-only documents in `tests/scanned.py`.
- Directory sign-in (`app/auth/ldap.py`, `accounts.authenticate`):
  - Search-then-bind with a service account.
  - Empty passwords are refused before any bind. LDAP treats them as an anonymous bind and reports
    success.
  - User input is escaped with `escape_filter_chars`.
  - TLS with certificate verification is mandatory unless `LDAP_ALLOW_PLAINTEXT` is set, for tests
    only.
  - Local accounts always use the local path, so there's a break-glass login when the directory is
    down.
  - Only app groups named in `LDAP_GROUP_MAP` are synced.
  - **ldap3 is LGPL-3.0** (used unmodified). It's the one non-permissive dependency; the alternative
    is python-ldap, which needs a C toolchain.
  - Tests: ldap3 mock (`tests/test_ldap.py`), DB (`tests/db/test_directory_accounts.py`), and a
    real OpenLDAP server over TLS (`bash scripts/test-ldap.sh`, which mints a fresh CA per run).
  - api and worker read `.env` via `env_file`, so `LDAP_*` doesn't need listing in compose.
    Single-quote values containing shell metacharacters.
- Dev GPU: `DEV_GPU=1` in `.env` adds `deploy/docker-compose.gpu.yml` (NVIDIA) to the dev stack. With
  it, the 7B customer model runs on a 4 GB laptop GPU, partly offloaded (see `docs/eval-results.md`).
- Release testing: install the bundle under `COMPOSE_PROJECT_NAME=<other>` with other ports. The
  release compose file is also named `rag-onprem` and would otherwise reuse the dev volumes.
- Shell pitfalls already hit: `tr </dev/urandom | head` under `pipefail` needs `|| true` (SIGPIPE);
  `df` on Docker's root fails on Docker Desktop; `cygpath -m` keeps relative paths relative (use
  `-am`); `COMPOSE_FILE` needs `COMPOSE_PATH_SEPARATOR=:` to work on Windows.
- CI (`.github/workflows/ci.yml`): unit tests with networking off, shellcheck, frontend build and URL
  check, then a full stack with a 0.5B model for the egress check, DB tests and an e2e smoke test.

## Working conventions

- Don't add application code beyond what a task asks for. This repo is being built up deliberately.
- Every feature touching retrieval must respect ACL filtering, with a test proving a user can't retrieve
  another group's chunks.
- Prompts live in versioned template files (`app/llm/prompts/`), not inline strings scattered across modules.
- Answers must cite sources. If retrieval finds nothing relevant, the system says so rather than letting the
  LLM answer from its own knowledge.
- Database changes always go through Alembic migrations.
- Keep the deployment footprint small. Adding a new container or service needs a clear reason.
