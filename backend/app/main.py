import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from app.api.auth import router as auth_router
from app.api.deps import get_ollama
from app.api.documents import router as documents_router
from app.api.routes import router
from app.auth.sessions import purge_expired
from app.core.config import get_settings
from app.db.engine import get_engine
from app.llm.ollama import OllamaError

log = logging.getLogger(__name__)

# Every state-changing API request must carry this header. Browsers won't send a
# custom header cross-origin without a CORS preflight, which this API never
# grants, so a forged form post or fetch from another site is rejected. The
# SameSite=Strict session cookie is the first line of defence; this is the second.
CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "rag-onprem"
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def wait_for_models() -> None:
    """Fail clearly at startup if the configured models aren't in Ollama.
    The app never downloads models; they must be baked into the ollama image."""
    settings = get_settings()
    client = get_ollama()
    deadline = time.monotonic() + settings.startup_wait_s
    last_problem = ""
    while True:
        try:
            missing = client.missing_models()
            if not missing:
                log.info("Models ready: %s, %s", settings.llm_model, settings.embed_model)
                return
            last_problem = f"models not present in Ollama: {', '.join(missing)}"
        except OllamaError as exc:
            last_problem = str(exc)
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"Startup check failed ({last_problem}). Rebuild the ollama image with "
                "LLM_MODEL/EMBED_MODEL matching the API settings; see deploy/ollama/Dockerfile."
            )
        time.sleep(2)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    wait_for_models()
    purged = purge_expired(get_engine())
    if purged:
        log.info("Removed %d expired session(s)", purged)
    yield
    get_ollama().close()


def create_app() -> FastAPI:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level, format="%(levelname)s %(name)s: %(message)s")
    # httpx logs every request URL at INFO; keep model traffic out of normal logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # No docs UI assets are fetched from a CDN at runtime: disable Swagger/ReDoc
    # (they load JS/CSS from jsdelivr). The OpenAPI JSON stays available.
    app = FastAPI(
        title="rag-onprem",
        version="0.3.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )

    @app.middleware("http")
    async def csrf_guard(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if (
            request.method not in _SAFE_METHODS
            and request.url.path.startswith("/api/")
            and request.headers.get(CSRF_HEADER) != CSRF_VALUE
        ):
            return JSONResponse({"detail": "Missing CSRF header"}, status_code=403)
        return await call_next(request)

    app.include_router(router)
    app.include_router(auth_router)
    app.include_router(documents_router)
    return app


app = create_app()
