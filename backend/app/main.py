import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.deps import get_ollama
from app.api.documents import router as documents_router
from app.api.routes import router
from app.core.config import get_settings
from app.llm.ollama import OllamaError

log = logging.getLogger(__name__)


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
    yield
    get_ollama().close()


def create_app() -> FastAPI:
    logging.basicConfig(
        level=get_settings().log_level, format="%(levelname)s %(name)s: %(message)s"
    )
    # No docs UI assets are fetched from a CDN at runtime: disable Swagger/ReDoc
    # (they load JS/CSS from jsdelivr). The OpenAPI JSON stays available.
    app = FastAPI(
        title="rag-onprem",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.include_router(router)
    app.include_router(documents_router)
    return app


app = create_app()
