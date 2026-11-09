import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Annotated, Literal, Optional
from uuid import uuid4

from fastapi import Depends, Query, Request
from pydantic import BaseModel, StringConstraints
from pymongo.errors import DuplicateKeyError

from lachilena_common.app_factory import create_app
from lachilena_common.db import get_database
from lachilena_common.errors import ApiError
from lachilena_common.http import ServiceClient
from lachilena_common.logging_setup import log
from lachilena_common.security import Guard, Identity, ServiceAuth
from lachilena_common.vault import VaultSecrets

from .rut import valid_run

logger = logging.getLogger("clients")
VERIFICATION_LIFETIME = timedelta(minutes=15)

secrets_store = VaultSecrets(os.environ.get("VAULT_SECRET_PATH", "clients"))
guard = Guard(ServiceAuth(secrets_store))
state = SimpleNamespace(db=None, auth=None, mailcheck=None)

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=120)]
Email = Annotated[str, StringConstraints(strip_whitespace=True, to_lower=True, min_length=5, max_length=254,
                                         pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]


def now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class EmailVerificationIn(BaseModel):
    email: Email


class ClientIn(BaseModel):
    run: Annotated[str, StringConstraints(strip_whitespace=True, max_length=12)]
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=80)]
    address: Text
    comuna: Text
    provincia: Text
    region: Text
    birthDate: Annotated[str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}$")]
    sex: Literal["F", "M", "O"]
    email: Email
    phone: Annotated[str, StringConstraints(pattern=r"^\+569\d{8}$")]
    password: Optional[Annotated[str, StringConstraints(min_length=8, max_length=128)]] = None
    verificationId: Annotated[str, StringConstraints(min_length=8, max_length=64)]


class ClientUpdateIn(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=80)]
    address: Text
    comuna: Text
    provincia: Text
    region: Text
    phone: Annotated[str, StringConstraints(pattern=r"^\+569\d{8}$")]


def profile_view(client: dict) -> dict:
    return {"id": client["_id"], "run": client["run"], "name": client["name"], "email": client["email"],
            "address": client["address"], "comuna": client["comuna"], "provincia": client["provincia"],
            "region": client["region"], "phone": client["phone"]}


async def seed_demo_clients() -> None:
    if os.getenv("SEED_DEMO_DATA", "true").lower() != "true":
        return
    if await state.db.clients.find_one({"_id": "c-demo-01"}):
        return
    await state.db.clients.insert_one({
        "_id": "c-demo-01",
        "run": "18.765.432-7",
        "name": "Constanza Rivas",
        "address": "Av. Pajaritos 3450",
        "comuna": "Maipú",
        "provincia": "Santiago",
        "region": "Metropolitana",
        "birthDate": "1996-04-12",
        "sex": "F",
        "phone": "+56912345678",
        "email": "constanza@gmail.com",
        "createdAt": now(),
    })


@asynccontextmanager
async def lifespan(_app):
    await secrets_store.load()
    state.db = get_database(secrets_store.get("mongo_uri"))
    await state.db.clients.create_index("run", unique=True)
    await state.db.clients.create_index("email", unique=True)
    await state.db.email_verifications.create_index("expiresAt", expireAfterSeconds=0)
    await seed_demo_clients()
    state.auth = ServiceClient(os.environ["AUTH_URL"], lambda: str(secrets_store.get("auth_credential", "")))
    state.mailcheck = ServiceClient(os.environ["MAILCHECK_URL"], lambda: str(secrets_store.get("mailcheck_key", "")))
    yield
    await state.auth.aclose()
    await state.mailcheck.aclose()


app = create_app("clients", lifespan=lifespan)
from_gateway = guard.caller("api-gateway")
from_orders = guard.caller("orders")
admin_only = guard.user("administrador", callers=("api-gateway",))
profile_reader = guard.user("cliente", "administrador", callers=("api-gateway",))


@app.post("/email-verifications")
async def verify_email(body: EmailVerificationIn, _caller: str = Depends(from_gateway)):
    try:
        response = await state.mailcheck.request("POST", "/v1/validate", json={"email": body.email})
    except ApiError:
        raise ApiError(502, "VALIDATOR_UNAVAILABLE", "No se pudo verificar el correo. Intenta de nuevo.")
    if response.status_code != 200:
        raise ApiError(502, "VALIDATOR_UNAVAILABLE", "No se pudo verificar el correo. Intenta de nuevo.")
    deliverable = bool(response.json().get("deliverable"))
    verification_id = "v-" + uuid4().hex[:16]
    await state.db.email_verifications.insert_one({
        "_id": verification_id, "email": body.email, "valid": deliverable, "consumed": False,
        "expiresAt": now() + VERIFICATION_LIFETIME})
    return {"verificationId": verification_id, "valid": deliverable}


def validate_client(body: ClientIn) -> None:
    if not valid_run(body.run):
        raise ApiError(400, "INVALID_RUN", "El RUN no es válido")
    try:
        born = date.fromisoformat(body.birthDate)
    except ValueError:
        raise ApiError(400, "INVALID_BIRTH_DATE", "La fecha de nacimiento no es válida")
    if born > now().date() or born.year < 1900:
        raise ApiError(400, "INVALID_BIRTH_DATE", "La fecha de nacimiento no es válida")


@app.post("/clients", status_code=201)
async def register_client(body: ClientIn, request: Request, _caller: str = Depends(from_gateway)):
    roles = [r for r in request.headers.get("x-authenticated-roles", "").split(",") if r]
    assisted = "administrador" in roles
    if roles and not assisted:
        raise ApiError(403, "FORBIDDEN", "No tienes permiso para esta acción")
    validate_client(body)
    if not assisted and not body.password:
        raise ApiError(400, "VALIDATION_ERROR", "La contraseña es obligatoria", {"fields": [{"field": "password"}]})
    verification = await state.db.email_verifications.find_one({"_id": body.verificationId})
    if (not verification or verification["email"] != body.email or not verification["valid"]
            or verification["consumed"] or verification["expiresAt"] <= now()):
        raise ApiError(422, "EMAIL_NOT_VERIFIED", "Debes verificar el correo electrónico antes de registrarte")

    client_id = str(uuid4())
    account = {"id": client_id, "email": body.email, "displayName": body.name, "role": "cliente"}
    if not assisted:
        account["password"] = body.password
    response = await state.auth.request("POST", "/internal/accounts", json=account)
    if response.status_code == 409:
        raise ApiError(409, "EMAIL_TAKEN", "Ya existe una cuenta con ese correo")
    if response.status_code != 201:
        log(logger, "account_creation_failed", status=response.status_code)
        raise ApiError(502, "BAD_GATEWAY", "No se pudo crear la cuenta")

    document = {"_id": client_id, "run": body.run.upper(), "name": body.name, "address": body.address,
                "comuna": body.comuna, "provincia": body.provincia, "region": body.region,
                "birthDate": body.birthDate, "sex": body.sex, "phone": body.phone, "email": body.email,
                "createdAt": now()}
    try:
        await state.db.clients.insert_one(document)
    except DuplicateKeyError:
        await compensate(client_id)
        raise ApiError(409, "RUN_TAKEN", "Ya existe un cliente con ese RUN o correo")
    except Exception:
        await compensate(client_id)
        raise
    await state.db.email_verifications.update_one({"_id": body.verificationId}, {"$set": {"consumed": True}})
    return {"id": client_id, "name": body.name, "email": body.email}


async def compensate(client_id: str) -> None:
    try:
        await state.auth.request("DELETE", f"/internal/accounts/{client_id}")
    except ApiError:
        log(logger, "compensation_failed")


@app.get("/clients")
async def list_clients(q: Optional[Annotated[str, StringConstraints(max_length=60)]] = None,
                       page: int = Query(1, ge=1, le=10000), size: int = Query(20, ge=1, le=50),
                       _identity=Depends(admin_only)):
    query: dict = {}
    if q:
        pattern = {"$regex": re.escape(q), "$options": "i"}
        query["$or"] = [{"name": pattern}, {"email": pattern}, {"run": pattern}]
    total = await state.db.clients.count_documents(query)
    cursor = state.db.clients.find(query).sort("name", 1).skip((page - 1) * size).limit(size)
    items = [{"id": c["_id"], "run": c["run"], "name": c["name"], "email": c["email"], "comuna": c["comuna"],
              "phone": c["phone"]} async for c in cursor]
    return {"items": items, "page": page, "size": size, "total": total}


def check_profile_access(client_id: str, identity: Identity) -> None:
    if "administrador" not in identity.roles and client_id != identity.user_id:
        raise ApiError(403, "FORBIDDEN", "No tienes acceso a este cliente")


@app.get("/clients/{client_id}")
async def get_client(client_id: str, identity: Identity = Depends(profile_reader)):
    check_profile_access(client_id, identity)
    client = await state.db.clients.find_one({"_id": client_id})
    if not client:
        raise ApiError(404, "NOT_FOUND", "Cliente no encontrado")
    return profile_view(client)


@app.put("/clients/{client_id}")
async def update_client(client_id: str, body: ClientUpdateIn, identity: Identity = Depends(profile_reader)):
    check_profile_access(client_id, identity)
    client = await state.db.clients.find_one({"_id": client_id})
    if not client:
        raise ApiError(404, "NOT_FOUND", "Cliente no encontrado")
    changes = body.model_dump()
    changes["updatedAt"] = now()
    await state.db.clients.update_one({"_id": client_id}, {"$set": changes})
    if body.name != client["name"]:
        try:
            synced = await state.auth.request("PUT", f"/internal/accounts/{client_id}/profile",
                                              json={"displayName": body.name})
            if synced.status_code != 204:
                log(logger, "account_name_sync_failed", status=synced.status_code)
        except ApiError:
            log(logger, "account_name_sync_failed")
    client.update(changes)
    return profile_view(client)


@app.get("/internal/clients/{client_id}/delivery-info")
async def delivery_info(client_id: str, _caller: str = Depends(from_orders)):
    client = await state.db.clients.find_one({"_id": client_id})
    if not client:
        raise ApiError(404, "NOT_FOUND", "Cliente no encontrado")
    return {"id": client["_id"], "name": client["name"], "address": client["address"], "comuna": client["comuna"],
            "email": client["email"]}
