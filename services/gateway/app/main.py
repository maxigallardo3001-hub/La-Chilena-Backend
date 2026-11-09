import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

from fastapi import Request, Response
from fastapi.responses import JSONResponse

from lachilena_common.app_factory import create_app
from lachilena_common.errors import ApiError, envelope
from lachilena_common.http import ServiceClient
from lachilena_common.logging_setup import log
from lachilena_common.security import Identity
from lachilena_common.vault import SecretsUnavailable, VaultSecrets

from .ratelimit import RateLimiter
from .routes import match

logger = logging.getLogger("gateway")

SESSION_COOKIE = "__Host-sid"
UNSAFE_METHODS = {"POST", "PUT", "DELETE", "PATCH"}
ALLOWED_ORIGINS = {o.strip() for o in os.getenv("ALLOWED_ORIGINS", "https://localhost:8443").split(",") if o.strip()}
TARGETS = {
    "auth": "AUTH_URL", "clients": "CLIENTS_URL", "inventory": "INVENTORY_URL",
    "orders": "ORDERS_URL", "payments": "PAYMENTS_URL", "graphql": "GRAPHQL_URL",
}
IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
WEBHOOK_TOLERANCE_SECONDS = 300
MAX_QUERY_LENGTH = 2048

secrets_store = VaultSecrets(os.environ.get("VAULT_SECRET_PATH", "gateway"), ttl=300)
state = SimpleNamespace(clients={}, limiter=None)


class ClearSession(ApiError):
    pass


@asynccontextmanager
async def lifespan(_app):
    await secrets_store.load()
    for name, env in TARGETS.items():
        key = f"{name}_credential"
        state.clients[name] = ServiceClient(os.environ[env], lambda key=key: str(secrets_store.get(key, "")),
                                            timeout=5.0)
    state.limiter = RateLimiter(float(os.getenv("RATE_LIMIT_MULTIPLIER", "1")))
    yield
    for client in state.clients.values():
        await client.aclose()


app = create_app("gateway", lifespan=lifespan)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Strict-Transport-Security"] = "max-age=31536000"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(SESSION_COOKIE, token, httponly=True, secure=True, samesite="strict", path="/")


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="strict")


def check_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin is not None and origin not in ALLOWED_ORIGINS:
        raise ApiError(403, "ORIGIN_NOT_ALLOWED", "Origen no permitido")


def check_webhook_headers(request: Request) -> None:
    signature = request.headers.get("x-signature", "")
    timestamp = request.headers.get("x-timestamp", "")
    if not signature or not timestamp.isdigit():
        raise ApiError(401, "INVALID_SIGNATURE", "Firma ausente o inválida")
    if abs(time.time() - int(timestamp)) > WEBHOOK_TOLERANCE_SECONDS:
        raise ApiError(401, "INVALID_SIGNATURE", "Firma ausente o inválida")


async def read_body(route, request: Request) -> bytes:
    if request.method in ("GET", "DELETE"):
        return b""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > route.max_body:
        raise ApiError(413, "PAYLOAD_TOO_LARGE", "El contenido es demasiado grande")
    body = await request.body()
    if len(body) > route.max_body:
        raise ApiError(413, "PAYLOAD_TOO_LARGE", "El contenido es demasiado grande")
    if body and route.kind != "webhook" and "application/json" not in request.headers.get("content-type", "").lower():
        raise ApiError(400, "BAD_REQUEST", "El contenido debe ser JSON")
    return body


async def introspect(token: str, csrf: str | None) -> dict:
    payload = {"token": token}
    if csrf is not None:
        payload["csrf"] = csrf
    try:
        response = await state.clients["auth"].request("POST", "/introspect", json=payload)
    except ApiError:
        raise ApiError(503, "AUTH_UNAVAILABLE", "No se pudo verificar la sesión")
    if response.status_code != 200:
        log(logger, "introspect_failed", status=response.status_code)
        raise ApiError(503, "AUTH_UNAVAILABLE", "No se pudo verificar la sesión")
    return response.json()


async def authenticate(route, request: Request):
    if route.kind == "anonymous":
        return None, None
    if route.kind == "webhook":
        check_webhook_headers(request)
        return None, None

    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        if route.kind == "optional_session":
            return None, None
        raise ApiError(401, "UNAUTHENTICATED", "Se requiere una sesión válida")
    if not 10 <= len(token) <= 128:
        if route.kind == "optional_session":
            return None, None
        raise ClearSession(401, "UNAUTHENTICATED", "Tu sesión expiró")

    needs_csrf = request.method in UNSAFE_METHODS and (route.csrf or route.kind == "optional_session")
    info = await introspect(token, request.headers.get("x-csrf-token", "") if needs_csrf else None)
    if not info.get("active"):
        if route.kind == "optional_session":
            return None, None
        raise ClearSession(401, "UNAUTHENTICATED", "Tu sesión expiró")
    if needs_csrf and info.get("csrfValid") is not True:
        raise ApiError(403, "CSRF_INVALID", "Token CSRF ausente o inválido")
    if info.get("mustChangePassword") and not route.allow_must_change:
        raise ApiError(403, "PASSWORD_CHANGE_REQUIRED", "Debes cambiar tu contraseña antes de continuar")
    roles = tuple(info.get("roles") or ())
    if route.roles and not set(route.roles) & set(roles):
        raise ApiError(403, "FORBIDDEN", "No tienes permiso para esta acción")
    return Identity(user_id=info["user_id"], roles=roles, caller="api-gateway"), info


async def forward(route, params: dict, request: Request, identity, body: bytes, json_body=None):
    headers = {}
    content_type = request.headers.get("content-type")
    if json_body is None and body and content_type:
        headers["Content-Type"] = content_type
    idempotency = request.headers.get("idempotency-key")
    if idempotency is not None:
        if not IDEMPOTENCY_RE.match(idempotency):
            raise ApiError(400, "VALIDATION_ERROR", "Idempotency-Key inválida")
        headers["Idempotency-Key"] = idempotency
    if route.kind == "webhook":
        headers["X-Signature"] = request.headers["x-signature"]
        headers["X-Timestamp"] = request.headers["x-timestamp"]
    query = request.url.query
    if len(query) > MAX_QUERY_LENGTH:
        raise ApiError(400, "BAD_REQUEST", "La consulta es demasiado larga")
    try:
        upstream = await state.clients[route.target].request(
            request.method, route.internal.format(**params), identity=identity,
            content=(body or None) if json_body is None else None, json=json_body,
            params=list(request.query_params.multi_items()), headers=headers,
        )
    except ApiError as exc:
        if exc.status == 503:
            raise ApiError(502, "BAD_GATEWAY", "El servicio no está disponible")
        raise
    if upstream.status_code == 500:
        raise ApiError(502, "BAD_GATEWAY", "El servicio no pudo procesar la solicitud")
    return upstream


def relay(upstream) -> Response:
    if upstream.status_code == 204:
        return Response(status_code=204)
    media_type = upstream.headers.get("content-type")
    return Response(content=upstream.content, status_code=upstream.status_code, media_type=media_type)


@app.api_route("/api/v1/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def gateway(path: str, request: Request):
    route, params = match(request.method, "/" + path)
    if route is None:
        raise ApiError(404, "NOT_FOUND", "Recurso no encontrado")
    try:
        return await handle(route, params, request)
    except ClearSession as exc:
        response = envelope(exc.status, exc.code, exc.message)
        clear_session_cookie(response)
        return response


async def handle(route, params: dict, request: Request):
    client_ip = request.client.host if request.client else "desconocido"
    wait = state.limiter.check(route.policy, client_ip)
    if wait:
        response = envelope(429, "TOO_MANY_REQUESTS", "Demasiadas solicitudes. Espera unos minutos.")
        response.headers["Retry-After"] = str(wait)
        return response
    if request.method in UNSAFE_METHODS:
        check_origin(request)
    try:
        await secrets_store.refresh_if_needed()
    except SecretsUnavailable:
        raise ApiError(503, "SECRETS_UNAVAILABLE", "El servicio no está disponible")

    identity, info = await authenticate(route, request)
    body = await read_body(route, request)

    if route.special == "session":
        return JSONResponse({
            "user": {"id": info["user_id"], "name": info["name"], "role": info["roles"][0]},
            "mustChangePassword": info["mustChangePassword"],
            "csrfToken": info["csrfToken"],
            "expiresAt": info["expiresAt"],
        })

    if route.special == "logout":
        token = request.cookies[SESSION_COOKIE]
        try:
            await state.clients["auth"].request("POST", "/logout", json={"token": token})
        except ApiError:
            raise ApiError(502, "BAD_GATEWAY", "El servicio no está disponible")
        response = Response(status_code=204)
        clear_session_cookie(response)
        return response

    if route.special == "password":
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise ApiError(400, "VALIDATION_ERROR", "Datos inválidos")
        data["token"] = request.cookies[SESSION_COOKIE]
        return relay(await forward(route, params, request, identity, b"", json_body=data))

    upstream = await forward(route, params, request, identity, body)

    if route.special == "login" and upstream.status_code == 200:
        data = upstream.json()
        token = data.pop("token")
        response = JSONResponse(data)
        set_session_cookie(response, token)
        return response
    return relay(upstream)

