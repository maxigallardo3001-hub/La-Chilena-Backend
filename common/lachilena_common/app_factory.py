import logging
import os
import re
import time
import uuid

from fastapi import FastAPI

from .errors import envelope, install_error_handlers
from .logging_setup import correlation_id_var, log, setup_logging

CORRELATION_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")
logger = logging.getLogger("request")


def create_app(service_name: str, lifespan=None) -> FastAPI:
    setup_logging(service_name)
    docs = os.getenv("ENABLE_DOCS", "false").lower() == "true"
    app = FastAPI(
        title=service_name,
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs else None,
    )
    install_error_handlers(app)

    @app.middleware("http")
    async def correlation(request, call_next):
        incoming = request.headers.get("x-correlation-id", "")
        cid = incoming if CORRELATION_RE.match(incoming) else uuid.uuid4().hex
        token = correlation_id_var.set(cid)
        started = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
        except Exception as exc:
            logger.error("unhandled_error", exc_info=(type(exc), exc, None))
            response = envelope(500, "INTERNAL_ERROR", "Error interno del servicio")
        response.headers["X-Correlation-Id"] = cid
        if request.url.path != "/health":
            log(
                logger, "request",
                method=request.method, path=request.url.path, status=status,
                ms=round((time.perf_counter() - started) * 1000, 1),
            )
        correlation_id_var.reset(token)
        return response

    @app.get("/health")
    async def health():
        return {"status": "ok", "service": service_name}

    return app
