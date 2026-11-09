from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .logging_setup import correlation_id_var


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, extra: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra or {}


def envelope(status: int, code: str, message: str, extra: dict | None = None) -> JSONResponse:
    error = {"code": code, "message": message, "correlationId": correlation_id_var.get()}
    if extra:
        error.update(extra)
    return JSONResponse(status_code=status, content={"error": error})


_CODES = {
    400: "BAD_REQUEST", 401: "UNAUTHENTICATED", 403: "FORBIDDEN", 404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED", 413: "PAYLOAD_TOO_LARGE", 429: "TOO_MANY_REQUESTS",
}
_MESSAGES = {404: "Recurso no encontrado", 405: "Método no permitido"}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def api_error(_request, exc: ApiError):
        return envelope(exc.status, exc.code, exc.message, exc.extra)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request, exc: RequestValidationError):
        fields = [
            {"field": ".".join(str(p) for p in e.get("loc", ()) if p != "body"), "message": e.get("msg", "")}
            for e in exc.errors()
        ]
        return envelope(400, "VALIDATION_ERROR", "Datos inválidos", {"fields": fields})

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_request, exc: StarletteHTTPException):
        code = _CODES.get(exc.status_code, "HTTP_ERROR")
        return envelope(exc.status_code, code, _MESSAGES.get(exc.status_code, "Solicitud no válida"))
