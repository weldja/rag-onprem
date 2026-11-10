# rag-onprem — phased build plan

> **Status (2026-11-10):** Milestones 1–5 are implemented, plus file shares, the Indexing page and
> small-GPU tuning. See `CLAUDE.md` ("Current state") for what was
> built and how it differs from this plan, `docs/eval-results.md` for answer quality, and
> `docs/operations.md` for installation and operations.

## Context
`rag-onprem` only has `CLAUDE.md` and `.gitignore` so far. The first goal is a thin end-to-end slice:
`docker compose up` brings up Postgres+pgvector, Ollama and the API. We ingest one PDF, ask a
question, and get back an answer that cites document and page. Nothing reaches the internet at runtime.

The user's earlier repo `C:\Users\jamesw\Downloads\rag-claude` has an on-prem mode we can reuse.
It contains plenty of good offline code, but it is tangled with the Anthropic SDK, licensing code and a
4,200-line `rag_core.py`. Approach: **copy small, provider-neutral pieces and adapt them. Don't fork
the repo.** Where the old repo and `CLAUDE.md` disagree, `CLAUDE.md` wins:
- psycopg 3, not psycopg2
- Ollama embeddings (bge-m3), not fastembed
- uv with a lock file, not `requirements.txt`
- Python 3.12, not 3.11

### Never copy from rag-claude
- `.env`, `Claude_API_Keys.txt`, `certs/`
- `license.py` and `generate_license.py`
- the `weldai-*.tar` and zip artifacts
- anything that imports `anthropic` or references api.anthropic.com: `rag_core.py:55, 1762–1769,
  2504, 2559, 3759, 3829–3846, 374–488`, `llm.py:238` (`_probe`/`internet_ok`), and
  `main.py:314–436, 1065–1077, 1314`
- `extract-msg`, which is probably GPL-3.0

## Reuse map (rag-claude → rag-onprem)
| Source | Target | Change |
|---|---|---|
| `backend/llm.py` `local_chat_stream`, `local_check`, `_model_matches`, `LocalLLMError` | `backend/app/llm/ollama.py` | Keep httpx with `trust_env=False`. Switch to native `/api/chat`, add `/api/embed`. Drop provider modes and internet probes. |
| `rag_core.py:158` `RecursiveTextSplitter`, `Document` | `backend/app/ingestion/chunking.py` | As-is. A token-aware size can come later. |
| `rag_core.py:944` `load_document`, PDF branch (969–979, pypdf per page) | `backend/app/ingestion/loaders.py` | PDF only in M1. Other formats come in M2. |
| `rag_core.py:1194–1268` `SYSTEM_PROMPT`, `USER_PROMPT_TEMPLATE`, `NOT_FOUND_MARKERS`, `is_not_found_answer` | `backend/app/llm/prompts/` | Remove Claude wording. Switch to numbered `[n]` citations. |
| `rag_core.py:3685–3732` context building (`<document source page>` wrapping plus escaping) and `_build_sources` (3304) | `backend/app/retrieval/answer.py` | Keep the injection escaping. |
| `rag_core.py:3166` `_vector_candidates` | `backend/app/retrieval/search.py` | Rewrite against the new schema. M2 adds `_keyword_candidates` (3200) and `_rrf_merge` (3139). |
| `ollama/Dockerfile`, `ollama/entrypoint.sh` | `deploy/ollama/` | Bake the LLM and bge-m3 in at build time. Keep `OLLAMA_NO_CLOUD=1`. |
| `docker-compose.yaml` `internal: true` network, `docker-compose.gpu.yaml` | `deploy/` | Adopt the pattern. GPU becomes a compose profile. |
| `tests/test_llm.py` fake Ollama server | `backend/tests/` | Drop the Claude-mode tests. |

---

## Milestone 1 — thin end-to-end slice
**Done when:** a fresh clone runs `bash scripts/dev-up.sh`, then
`bash scripts/ingest.sh samples/sample.pdf`, then
`curl -X POST localhost:8000/api/ask -d '{"question":"…"}'`, and gets back
`{answer, sources:[{document, page, chunk_id, snippet}]}`. The answer contains `[1]`-style markers
that point to real pages, and the API container cannot reach the internet.

1. **Repo hygiene:**
   - `.gitattributes` with `eol=lf` for `*.sh *.py Dockerfile* *.yml *.conf`
   - `.env.example`
   - `samples/` holding a small public-domain PDF
2. **Compose** (`deploy/docker-compose.yml` + `docker-compose.dev.yml`):
   - `db`: `pgvector/pgvector:pg16`, with a volume
   - `ollama`: built from `deploy/ollama/Dockerfile`, models pre-pulled at build time, `OLLAMA_NO_CLOUD=1`
   - `api`
   - Networks: `backend` (`internal: true`) for db, ollama and api; `edge` (dev only) for api so port 8000 can be published on the host. nginx replaces this in M3.
   - Healthchecks, and `api` waits on db and ollama.
3. **Backend skeleton** (`backend/`):
   - `pyproject.toml` + `uv.lock`: fastapi, uvicorn, pydantic-settings, sqlalchemy, psycopg[binary], pgvector, alembic, httpx, pypdf; dev: pytest, pytest-asyncio, pytest-socket, ruff, mypy
   - `Dockerfile` (python:3.12-slim, `uv sync --frozen`)
   - `app/core/config.py`: settings for the Ollama URL, model names, embedding dimension (1024), chunk size and overlap, top-k
   - `app/main.py` with `/api/health`, which reports db and model readiness
4. **Schema** (first Alembic migration):
   - `documents(id, filename, sha256, page_count, created_at)`
   - `chunks(id, document_id, page, ordinal, text, embedding vector(1024), embed_model, tsv generated)`
   - HNSW cosine index on `embedding`, GIN index on `tsv` (the column is unused until M2)
5. **Model client** (`app/llm/ollama.py`): `chat()` (non-streaming in M1), `embed(texts)`, and `check_models()`. At startup it confirms the configured models exist and fails clearly if they don't. It never calls `/api/pull`. It sits behind the `LLMClient` and `Embedder` protocols.
6. **Ingestion** (`app/ingestion/`):
   - Synchronous CLI, `python -m app.ingestion.cli <pdf>`: load each page → split → embed in batches → write all chunks in one transaction.
   - Skip the file if its sha256 already exists.
7. **Retrieval and answer** (`app/retrieval/`):
   - Embed the question → top-k vector search → build numbered context → call `chat()` → parse the `[n]` markers.
   - Return only the sources that were actually cited.
   - If the best similarity score is below a threshold, or the model returns a not-found marker, reply "not found in documents" rather than answering.
8. **Tests:**
   - pytest-socket enabled globally
   - unit tests for the splitter, PDF loader, citation parsing, and the answer flow using a fake LLM and embedder
   - one integration test marked `@pytest.mark.compose` that runs against the live stack

## Milestone 2 — real ingestion and better retrieval
- `POST /api/documents` upload; originals stored in a volume; Postgres job queue (`FOR UPDATE SKIP LOCKED`) worked by a `worker` service; status endpoint
- Port the DOCX, PPTX, XLSX/CSV (`_load_spreadsheet` + header detection), HTML and text loaders, plus `_is_toc_chunk`
- Hybrid search: port `_keyword_candidates` and `_rrf_merge`; later `_rare_term_hits`
- SSE streaming `/api/ask`, using the event protocol from `ask_stream` (status/token/sources/done/error)
- Delete and re-index documents. Store the embedding model per chunk and refuse to mix models.

## Milestone 3 — usable product
- React + Vite frontend: upload, document list, chat with clickable citations
- nginx: static files, `/api` proxy, TLS from a customer certificate, CSP `'self'`, read timeout longer than the LLM timeout
- Auth: local accounts with argon2, sessions stored in Postgres so the API can run more than one worker; first admin created at install time with no default password
- Groups and document ACLs enforced in the retrieval SQL, with a test proving one group can't see another group's documents

## Milestone 4 — answer quality
- Evaluation set (questions plus expected sources) and a `scripts/eval.sh` reporting recall@k and citation accuracy
- Optional reranker (bge-reranker-v2-m3 baked into the image, `HF_HUB_OFFLINE=1` always set)
- Versioned prompts, conversation history, document-summary mode (port `_retrieve_document_spread`)

## Milestone 5 — ship it
- Offline bundle: `docker save` of digest-pinned images plus model files, an install script and a preflight check (GPU detection idea taken from rag-claude)
- Backup and restore (`pg_dump` plus the uploads volume), upgrade path, admin documentation
- Egress test in CI: on the compose network, verify that outbound connections fail

## Verification (M1)
1. `bash scripts/dev-up.sh` → all services healthy; `curl localhost:8000/api/health` shows the db and both models ready.
2. `cd backend && uv run pytest` passes with sockets disabled.
3. `bash scripts/ingest.sh samples/sample.pdf` → reports N chunks; running it again reports it was skipped as a duplicate.
4. Ask a question whose answer is on a known page → the answer cites `[n]`, and `sources[n].page` matches that page. Ask an off-topic question → "not found".
5. Egress check: `docker compose exec api python -c "import httpx; httpx.get('https://example.com', timeout=5)"` must fail. Repeat in `ollama`.

## Before starting
- Rotate the key that was in rag-claude's `.env`. It was committed in `b06b819` and pushed to GitHub.
- Confirm that the rag-claude licence holder (Weld AI) permits reuse in this project. It does if you own it.
