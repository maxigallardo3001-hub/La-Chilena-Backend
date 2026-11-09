import asyncio
import hashlib
import hmac
import logging
import os
import secrets as pysecrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Annotated, Literal, Optional
from uuid import uuid4

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import Depends, Response
from pydantic import BaseModel, StringConstraints
from pymongo.errors import DuplicateKeyError

from lachilena_common.app_factory import create_app
from lachilena_common.db import get_database
from lachilena_common.errors import ApiError
from lachilena_common.http import ServiceClient
from lachilena_common.logging_setup import log
from lachilena_common.outbox import Outbox
from lachilena_common.security import Guard, Identity, ServiceAuth
from lachilena_common.vault import VaultSecrets

logger = logging.getLogger("auth")

IDLE_TIMEOUT = timedelta(minutes=15)
ABSOLUTE_TIMEOUT = timedelta(hours=8)
TEMP_PASSWORD_LIFETIME = timedelta(hours=24)
FAILURE_LIMIT = 5
FAILURE_WINDOW = timedelta(minutes=15)
STAFF_ROLES = ("administrador", "cajero", "despacho", "dueno")

secrets_store = VaultSecrets(os.environ.get("VAULT_SECRET_PATH", "auth"))
guard = Guard(ServiceAuth(secrets_store))
state = SimpleNamespace(db=None, notifier=None, outbox=None)
hasher = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)
DUMMY_HASH = hasher.hash("contrasena-ficticia-para-igualar-tiempos")


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds") + "Z"


Email = Annotated[str, StringConstraints(strip_whitespace=True, to_lower=True, min_length=5, max_length=254,
                                         pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
Secret = Annotated[str, StringConstraints(min_length=1, max_length=128)]
Token = Annotated[str, StringConstraints(min_length=10, max_length=128)]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=80)]


class LoginIn(BaseModel):
    email: Email
    password: Secret


class IntrospectIn(BaseModel):
    token: Token
    csrf: Optional[Annotated[str, StringConstraints(max_length=128)]] = None


class LogoutIn(BaseModel):
    token: Token


class PasswordIn(BaseModel):
    token: Token
    currentPassword: Secret
    newPassword: Secret


class UserIn(BaseModel):
    name: Name
    email: Email
    role: Literal["administrador", "cajero", "despacho", "dueno"]


class UserUpdateIn(BaseModel):
    name: Name
    role: Literal["administrador", "cajero", "despacho", "dueno"]
    active: bool


class ProfileIn(BaseModel):
    displayName: Name


class InternalAccountIn(BaseModel):
    id: Annotated[str, StringConstraints(min_length=8, max_length=64)]
    email: Email
    displayName: Name
    role: Literal["cliente"]
    password: Optional[Secret] = None


class SessionUser(BaseModel):
    id: str
    name: str
    role: str


class LoginOut(BaseModel):
    token: str
    user: SessionUser
    mustChangePassword: bool
    csrfToken: str
    expiresIn: int


class IntrospectOut(BaseModel):
    active: bool
    user_id: Optional[str] = None
    name: Optional[str] = None
    roles: Optional[list[str]] = None
    mustChangePassword: Optional[bool] = None
    csrfToken: Optional[str] = None
    csrfValid: Optional[bool] = None
    expiresAt: Optional[str] = None


class UserOut(BaseModel):
    id: str
    name: str
    email: str
    role: str
    active: bool


class AccountCreated(BaseModel):
    id: str


async def hash_password(password: str) -> str:
    return await asyncio.to_thread(hasher.hash, password)


async def verify_password(password_hash: str, password: str) -> bool:
    try:
        return await asyncio.to_thread(hasher.verify, password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def new_temporary_password() -> str:
    return "Tmp-" + pysecrets.token_urlsafe(9)


def check_new_password(new: str, current: str | None = None) -> None:
    if len(new) < 8:
        raise ApiError(400, "WEAK_PASSWORD", "La contraseña nueva debe tener al menos 8 caracteres")
    if current is not None and new == current:
        raise ApiError(400, "WEAK_PASSWORD", "La contraseña nueva debe ser distinta de la actual")


def sid_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def derive_csrf(token: str) -> str:
    key = str(secrets_store.get("csrf_key", "")).encode("utf-8")
    return hmac.new(key, token.encode("utf-8"), hashlib.sha256).hexdigest()


def account_document(*, account_id: str, email: str, name: str, role: str, password_hash: str,
                     must_change: bool, temp_expires: datetime | None = None) -> dict:
    document = {
        "_id": account_id, "email": email, "displayName": name, "role": role,
        "passwordHash": password_hash, "active": True,
        "mustChangePassword": must_change, "createdAt": utcnow(),
    }
    if temp_expires:
        document["tempPasswordExpiresAt"] = temp_expires
    return document


def to_user_out(account: dict) -> UserOut:
    return UserOut(id=account["_id"], name=account["displayName"], email=account["email"],
                   role=account["role"], active=account["active"])


async def check_failures(key: str) -> None:
    doc = await state.db.login_failures.find_one({"_id": key})
    if doc and doc["count"] >= FAILURE_LIMIT and doc["expiresAt"] > utcnow():
        raise ApiError(429, "TOO_MANY_ATTEMPTS", "Demasiados intentos. Espera unos minutos.")


async def register_failure(key: str) -> None:
    await state.db.login_failures.update_one(
        {"_id": key},
        {"$inc": {"count": 1}, "$setOnInsert": {"expiresAt": utcnow() + FAILURE_WINDOW}},
        upsert=True,
    )


async def clear_failures(key: str) -> None:
    await state.db.login_failures.delete_one({"_id": key})


async def create_session(account: dict) -> str:
    token = pysecrets.token_urlsafe(32)
    now = utcnow()
    await state.db.sessions.insert_one({
        "_id": sid_hash(token), "accountId": account["_id"], "createdAt": now,
        "expiresAt": now + IDLE_TIMEOUT, "absoluteExpiresAt": now + ABSOLUTE_TIMEOUT,
    })
    return token


async def lookup_session(token: str):
    session = await state.db.sessions.find_one({"_id": sid_hash(token)})
    if not session:
        return None
    now = utcnow()
    if session["expiresAt"] <= now or session["absoluteExpiresAt"] <= now:
        await state.db.sessions.delete_one({"_id": session["_id"]})
        return None
    account = await state.db.accounts.find_one({"_id": session["accountId"], "active": True})
    if not account:
        return None
    return session, account


async def send_message(message: dict) -> bool:
    try:
        response = await state.notifier.request("POST", "/internal/messages", json=message)
        return response.status_code in (200, 202)
    except ApiError:
        return False


async def queue_temporary_password(email: str, name: str, temp_password: str) -> None:
    await state.outbox.enqueue({
        "to": email, "template": "temp_password",
        "data": {"name": name, "tempPassword": temp_password},
    })


async def ensure_indexes() -> None:
    db = state.db
    await db.accounts.create_index("email", unique=True)
    await db.sessions.create_index("expiresAt", expireAfterSeconds=0)
    await db.sessions.create_index("accountId")
    await db.login_failures.create_index("expiresAt", expireAfterSeconds=0)
    await db.outbox.create_index([("status", 1), ("createdAt", 1)])
    await db.outbox.create_index("createdAt", expireAfterSeconds=int(TEMP_PASSWORD_LIFETIME.total_seconds()))


async def bootstrap_admin() -> None:
    email = secrets_store.get("bootstrap_admin_email")
    password = secrets_store.get("bootstrap_admin_password")
    if not email or not password:
        return
    if await state.db.accounts.find_one({"email": str(email).lower()}):
        return
    await state.db.accounts.insert_one(account_document(
        account_id=str(uuid4()), email=str(email).lower(), name="Administrador", role="administrador",
        password_hash=await hash_password(str(password)), must_change=True,
    ))
    log(logger, "bootstrap_admin_created")


DEMO_ACCOUNTS = [
    ("u-demo-admin", "admin@lachilena.cl", "Paula Reyes", "administrador"),
    ("u-demo-cajero", "cajero@lachilena.cl", "Ítalo Muñoz", "cajero"),
    ("u-demo-despacho", "despacho@lachilena.cl", "Marcelo Soto", "despacho"),
    ("u-demo-dueno", "dueno@lachilena.cl", "Fernanda Ibáñez", "dueno"),
    ("c-demo-01", "constanza@gmail.com", "Constanza Rivas", "cliente"),
]


async def seed_demo_accounts() -> None:
    if os.getenv("SEED_DEMO_DATA", "true").lower() != "true":
        return
    for account_id, email, name, role in DEMO_ACCOUNTS:
        password = secrets_store.get(f"demo_password_{role}")
        if not password or await state.db.accounts.find_one({"email": email}):
            continue
        await state.db.accounts.insert_one(account_document(
            account_id=account_id, email=email, name=name, role=role,
            password_hash=await hash_password(str(password)), must_change=False,
        ))


@asynccontextmanager
async def lifespan(_app):
    await secrets_store.load()
    state.db = get_database(secrets_store.get("mongo_uri"))
    await ensure_indexes()
    state.notifier = ServiceClient(os.environ["NOTIFICATIONS_URL"],
                                   lambda: str(secrets_store.get("notify_credential", "")))
    state.outbox = Outbox(state.db.outbox, send_message)
    await state.outbox.start()
    await bootstrap_admin()
    await seed_demo_accounts()
    yield
    await state.outbox.stop()
    await state.notifier.aclose()


app = create_app("auth", lifespan=lifespan)
from_gateway = guard.caller("api-gateway")
from_clients = guard.caller("clients")
admin_only = guard.user("administrador", callers=("api-gateway",))


@app.post("/login", response_model=LoginOut)
async def login(body: LoginIn, _caller: str = Depends(from_gateway)):
    key = f"login:{body.email}"
    await check_failures(key)
    account = await state.db.accounts.find_one({"email": body.email, "active": True})
    valid = await verify_password(account["passwordHash"] if account else DUMMY_HASH, body.password)
    expired = bool(account and account.get("tempPasswordExpiresAt") and account["tempPasswordExpiresAt"] < utcnow())
    if not account or not valid or expired:
        await register_failure(key)
        raise ApiError(401, "INVALID_CREDENTIALS", "Credenciales incorrectas")
    await clear_failures(key)
    token = await create_session(account)
    return LoginOut(
        token=token,
        user=SessionUser(id=account["_id"], name=account["displayName"], role=account["role"]),
        mustChangePassword=account["mustChangePassword"],
        csrfToken=derive_csrf(token),
        expiresIn=int(IDLE_TIMEOUT.total_seconds()),
    )


@app.post("/introspect", response_model=IntrospectOut, response_model_exclude_none=True)
async def introspect(body: IntrospectIn, _caller: str = Depends(from_gateway)):
    found = await lookup_session(body.token)
    if not found:
        return IntrospectOut(active=False)
    session, account = found
    csrf = derive_csrf(body.token)
    csrf_valid = None if body.csrf is None else hmac.compare_digest(body.csrf.encode(), csrf.encode())
    expires = min(utcnow() + IDLE_TIMEOUT, session["absoluteExpiresAt"])
    await state.db.sessions.update_one({"_id": session["_id"]}, {"$set": {"expiresAt": expires}})
    return IntrospectOut(
        active=True, user_id=account["_id"], name=account["displayName"], roles=[account["role"]],
        mustChangePassword=account["mustChangePassword"], csrfToken=csrf, csrfValid=csrf_valid,
        expiresAt=iso(expires),
    )


@app.post("/logout", status_code=204)
async def logout(body: LogoutIn, _caller: str = Depends(from_gateway)):
    await state.db.sessions.delete_one({"_id": sid_hash(body.token)})
    return Response(status_code=204)


@app.post("/password", status_code=204)
async def change_password(body: PasswordIn, _caller: str = Depends(from_gateway)):
    found = await lookup_session(body.token)
    if not found:
        raise ApiError(401, "UNAUTHENTICATED", "Tu sesión expiró")
    session, account = found
    key = f"password:{account['_id']}"
    await check_failures(key)
    if not await verify_password(account["passwordHash"], body.currentPassword):
        await register_failure(key)
        raise ApiError(400, "INVALID_CURRENT_PASSWORD", "La contraseña actual es incorrecta")
    check_new_password(body.newPassword, body.currentPassword)
    await state.db.accounts.update_one(
        {"_id": account["_id"]},
        {"$set": {"passwordHash": await hash_password(body.newPassword), "mustChangePassword": False},
         "$unset": {"tempPasswordExpiresAt": ""}},
    )
    await state.db.sessions.delete_many({"accountId": account["_id"], "_id": {"$ne": session["_id"]}})
    await clear_failures(key)
    return Response(status_code=204)


@app.get("/users", response_model=list[UserOut])
async def list_users(_identity: Identity = Depends(admin_only)):
    cursor = state.db.accounts.find({"role": {"$in": list(STAFF_ROLES)}}).sort("displayName", 1)
    return [to_user_out(doc) async for doc in cursor]


@app.post("/users", response_model=UserOut, status_code=201)
async def create_user(body: UserIn, _identity: Identity = Depends(admin_only)):
    temp = new_temporary_password()
    document = account_document(
        account_id=str(uuid4()), email=body.email, name=body.name, role=body.role,
        password_hash=await hash_password(temp), must_change=True,
        temp_expires=utcnow() + TEMP_PASSWORD_LIFETIME,
    )
    try:
        await state.db.accounts.insert_one(document)
    except DuplicateKeyError:
        raise ApiError(409, "EMAIL_TAKEN", "Ya existe un usuario con ese correo")
    await queue_temporary_password(body.email, body.name, temp)
    return to_user_out(document)


@app.delete("/users/{user_id}", status_code=204)
async def delete_user(user_id: str, identity: Identity = Depends(admin_only)):
    account = await state.db.accounts.find_one({"_id": user_id, "role": {"$in": list(STAFF_ROLES)}})
    if not account:
        raise ApiError(404, "NOT_FOUND", "Usuario no encontrado")
    if user_id == identity.user_id:
        raise ApiError(409, "CANNOT_DELETE_SELF", "No puedes darte de baja a ti mismo")
    if account["role"] == "administrador":
        active_admins = await state.db.accounts.count_documents({"role": "administrador", "active": True})
        if active_admins <= 1:
            raise ApiError(409, "LAST_ADMIN", "No se puede dar de baja al último administrador")
    await state.db.accounts.update_one({"_id": user_id}, {"$set": {"active": False}})
    await state.db.sessions.delete_many({"accountId": user_id})
    return Response(status_code=204)


@app.put("/users/{user_id}", response_model=UserOut)
async def update_user(user_id: str, body: UserUpdateIn, identity: Identity = Depends(admin_only)):
    account = await state.db.accounts.find_one({"_id": user_id, "role": {"$in": list(STAFF_ROLES)}})
    if not account:
        raise ApiError(404, "NOT_FOUND", "Usuario no encontrado")
    loses_access = body.role != account["role"] or not body.active
    if user_id == identity.user_id and loses_access:
        raise ApiError(409, "CANNOT_DEMOTE_SELF", "No puedes cambiar tu propio rol ni darte de baja")
    if account["role"] == "administrador" and account["active"] and (body.role != "administrador" or not body.active):
        active_admins = await state.db.accounts.count_documents({"role": "administrador", "active": True})
        if active_admins <= 1:
            raise ApiError(409, "LAST_ADMIN", "Debe quedar al menos un administrador activo")
    await state.db.accounts.update_one(
        {"_id": user_id}, {"$set": {"displayName": body.name, "role": body.role, "active": body.active}})
    if loses_access:
        await state.db.sessions.delete_many({"accountId": user_id})
    account.update(displayName=body.name, role=body.role, active=body.active)
    return to_user_out(account)


@app.post("/internal/accounts", response_model=AccountCreated, status_code=201)
async def create_account(body: InternalAccountIn, _caller: str = Depends(from_clients)):
    temp = None
    if body.password:
        check_new_password(body.password)
        password, must_change, temp_expires = body.password, False, None
    else:
        temp = new_temporary_password()
        password, must_change, temp_expires = temp, True, utcnow() + TEMP_PASSWORD_LIFETIME
    document = account_document(
        account_id=body.id, email=body.email, name=body.displayName, role="cliente",
        password_hash=await hash_password(password), must_change=must_change, temp_expires=temp_expires,
    )
    try:
        await state.db.accounts.insert_one(document)
    except DuplicateKeyError:
        raise ApiError(409, "EMAIL_TAKEN", "Ya existe una cuenta con ese correo")
    if temp:
        await queue_temporary_password(body.email, body.displayName, temp)
    return AccountCreated(id=body.id)


@app.put("/internal/accounts/{account_id}/profile", status_code=204)
async def update_account_profile(account_id: str, body: ProfileIn, _caller: str = Depends(from_clients)):
    result = await state.db.accounts.update_one({"_id": account_id, "role": "cliente"},
                                                {"$set": {"displayName": body.displayName}})
    if result.matched_count == 0:
        raise ApiError(404, "NOT_FOUND", "Cuenta no encontrada")
    return Response(status_code=204)


@app.delete("/internal/accounts/{account_id}", status_code=204)
async def delete_account(account_id: str, _caller: str = Depends(from_clients)):
    await state.db.accounts.delete_one({"_id": account_id, "role": "cliente"})
    await state.db.sessions.delete_many({"accountId": account_id})
    return Response(status_code=204)
