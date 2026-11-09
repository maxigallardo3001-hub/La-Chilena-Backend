import asyncio
import contextlib
import hashlib
import hmac
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Annotated, Literal

from fastapi import Depends, Request, Response
from pydantic import BaseModel, Field, StringConstraints, ValidationError
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from lachilena_common.app_factory import create_app
from lachilena_common.db import get_database
from lachilena_common.errors import ApiError
from lachilena_common.http import ServiceClient
from lachilena_common.logging_setup import log
from lachilena_common.outbox import Outbox
from lachilena_common.security import Guard, Identity, ServiceAuth
from lachilena_common.vault import VaultSecrets

logger = logging.getLogger("payments")
SIGNATURE_TOLERANCE = 300
Identifier = Annotated[str, StringConstraints(min_length=1, max_length=64)]
SYNC_PENDING_MESSAGE = "El pago quedó registrado y el pedido se está sincronizando. Repite la operación en unos segundos."

secrets_store = VaultSecrets(os.environ.get("VAULT_SECRET_PATH", "payments"))
guard = Guard(ServiceAuth(secrets_store))
state = SimpleNamespace(db=None, orders=None, notifier=None, servipag=None, outbox=None, reconciler=None)


def moment() -> datetime:
    value = datetime.now(timezone.utc).replace(tzinfo=None)
    return value.replace(microsecond=value.microsecond // 1000 * 1000)


def iso(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds") + "Z"


def money(amount: int) -> str:
    return "$" + f"{amount:,}".replace(",", ".")


def split_vat(total: int) -> tuple[int, int]:
    net = round(total / 1.19)
    return net, total - net


class ItemIn(BaseModel):
    name: Annotated[str, StringConstraints(max_length=120)]
    quantity: int = Field(ge=1, le=50)
    unitPrice: int = Field(gt=0)


class CreatePaymentIn(BaseModel):
    orderId: Identifier
    clientId: Identifier
    clientEmail: Annotated[str, StringConstraints(max_length=254)]
    amount: int = Field(gt=0)
    method: Literal["servipag", "deposito"]
    items: list[ItemIn] = Field(min_length=1, max_length=30)


class ConfirmIn(BaseModel):
    reference: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")]


class WebhookIn(BaseModel):
    eventId: Identifier
    orderRef: Identifier
    amount: int
    status: Literal["approved", "rejected"]


async def seed_demo_payments() -> None:
    if os.getenv("SEED_DEMO_DATA", "true").lower() != "true":
        return
    if await state.db.payments.find_one({"_id": "pay-demo-0831"}):
        return
    d1 = datetime(2026, 8, 31, 20, 47, 0)
    d2 = datetime(2026, 8, 19, 16, 20, 0)
    await state.db.payments.insert_many([
        {
            "_id": "pay-demo-0831",
            "orderId": "ORD-2026-0831",
            "clientId": "c-demo-01",
            "clientEmail": "constanza@gmail.com",
            "method": "deposito",
            "amount": 6800,
            "status": "pendiente",
            "instructions": "Realiza el depósito de $6.800 y presenta el comprobante al cajero",
            "items": [
                {"name": "Empanada de pino", "quantity": 2, "unitPrice": 2500},
                {"name": "Mote con huesillo", "quantity": 1, "unitPrice": 1800},
            ],
            "createdAt": d1,
        },
        {
            "_id": "pay-demo-0819",
            "orderId": "ORD-2026-0819",
            "clientId": "c-demo-01",
            "method": "deposito",
            "amount": 16400,
            "status": "confirmado",
            "reference": "DEP-DEMO-0819",
            "confirmedBy": "u-demo-cajero",
            "paidAt": d2 + timedelta(minutes=5),
            "syncPending": False,
            "instructions": "Realiza el depósito de $16.400 y presenta el comprobante al cajero",
            "items": [
                {"name": "Empanada de carne", "quantity": 4, "unitPrice": 3500},
                {"name": "Bebida 350ml", "quantity": 2, "unitPrice": 1200},
            ],
            "createdAt": d2,
        },
    ])
    net, iva = split_vat(16400)
    await state.db.receipts.insert_one({
        "_id": "BOL-000001",
        "orderId": "ORD-2026-0819",
        "clientId": "c-demo-01",
        "issuedAt": d2 + timedelta(minutes=5),
        "issuedBy": "u-demo-cajero",
        "items": [
            {"name": "Empanada de carne", "quantity": 4, "unitPrice": 3500},
            {"name": "Bebida 350ml", "quantity": 2, "unitPrice": 1200},
        ],
        "net": net,
        "iva": iva,
        "total": 16400,
    })
    await state.db.counters.update_one({"_id": "receipt"}, {"$max": {"seq": 1}}, upsert=True)


async def send_message(message: dict) -> bool:
    try:
        response = await state.notifier.request("POST", "/internal/messages", json=message)
        return response.status_code in (200, 202)
    except ApiError:
        return False


async def reconcile_loop() -> None:
    interval = float(os.getenv("RECONCILE_INTERVAL", "30"))
    while True:
        await asyncio.sleep(interval)
        try:
            async for payment in state.db.payments.find({"status": "confirmado", "syncPending": True}).limit(20):
                with contextlib.suppress(ApiError):
                    await finalize(payment)
        except asyncio.CancelledError:
            raise
        except Exception:
            log(logger, "reconcile_failed")


@asynccontextmanager
async def lifespan(_app):
    await secrets_store.load()
    state.db = get_database(secrets_store.get("mongo_uri"))
    db = state.db
    await db.payments.create_index("orderId")
    await db.payments.create_index("reference", unique=True, partialFilterExpression={"reference": {"$type": "string"}})
    await db.receipts.create_index("orderId", unique=True)
    await db.outbox.create_index([("status", 1), ("createdAt", 1)])
    await seed_demo_payments()
    credential = lambda key: (lambda: str(secrets_store.get(key, "")))
    state.orders = ServiceClient(os.environ["ORDERS_URL"], credential("orders_credential"))
    state.notifier = ServiceClient(os.environ["NOTIFICATIONS_URL"], credential("notify_credential"))
    state.servipag = ServiceClient(os.environ["SERVIPAG_URL"], credential("servipag_key"))
    state.outbox = Outbox(db.outbox, send_message)
    await state.outbox.start()
    state.reconciler = asyncio.create_task(reconcile_loop())
    yield
    state.reconciler.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await state.reconciler
    await state.outbox.stop()
    for client in (state.orders, state.notifier, state.servipag):
        await client.aclose()


app = create_app("payments", lifespan=lifespan)
from_gateway = guard.caller("api-gateway")
from_orders = guard.caller("orders")
cashier = guard.user("cajero", callers=("api-gateway",))
receipt_reader = guard.user("cliente", "cajero", "administrador", callers=("api-gateway",))
payment_reader = guard.user("cliente", "cajero", "administrador", callers=("graphql-gateway",))


async def latest_payment(order_id: str):
    cursor = state.db.payments.find({"orderId": order_id}).sort("createdAt", -1).limit(1)
    found = await cursor.to_list(1)
    return found[0] if found else None


def may_read(payment: dict, identity: Identity) -> bool:
    roles = set(identity.roles)
    return "cajero" in roles or "administrador" in roles or ("cliente" in roles and payment["clientId"] == identity.user_id)


@app.post("/internal/payments", status_code=201)
async def create_payment(body: CreatePaymentIn, _caller: str = Depends(from_orders)):
    existing = await state.db.payments.find_one({"orderId": body.orderId, "status": "pendiente"})
    if existing:
        return {"paymentId": existing["_id"], "method": existing["method"], "status": "pendiente",
                "instructions": existing["instructions"]}
    document = {"_id": "pay-" + hashlib.sha256(os.urandom(16)).hexdigest()[:16], "orderId": body.orderId,
                "clientId": body.clientId, "clientEmail": body.clientEmail, "method": body.method,
                "amount": body.amount, "status": "pendiente", "items": [i.model_dump() for i in body.items],
                "createdAt": moment()}
    if body.method == "servipag":
        response = await state.servipag.request("POST", "/v1/charges", json={"orderRef": body.orderId, "amount": body.amount})
        if response.status_code != 200:
            raise ApiError(502, "BAD_GATEWAY", "El proveedor de pagos no pudo crear el cobro")
        charge = response.json()
        document["providerChargeId"], document["providerCode"] = charge["chargeId"], charge["code"]
        document["instructions"] = f"Paga en Servipag con el código {charge['code']}"
    else:
        document["instructions"] = f"Realiza el depósito de {money(body.amount)} y presenta el comprobante al cajero"
    await state.db.payments.insert_one(document)
    return {"paymentId": document["_id"], "method": body.method, "status": "pendiente", "instructions": document["instructions"]}


@app.post("/internal/payments/{order_id}/refund-pending", status_code=204)
async def refund_pending(order_id: str, _caller: str = Depends(from_orders)):
    await state.db.payments.update_many({"orderId": order_id, "status": "confirmado"},
                                        {"$set": {"status": "reembolso_pendiente"}})
    return Response(status_code=204)


@app.post("/internal/payments/{order_id}/void", status_code=204)
async def void_payment(order_id: str, _caller: str = Depends(from_orders)):
    await state.db.payments.update_many({"orderId": order_id, "status": "pendiente"},
                                        {"$set": {"status": "anulado", "voidedAt": moment()}})
    return Response(status_code=204)


def verify_signature(raw: bytes, timestamp: str, signature: str) -> None:
    key = str(secrets_store.get("servipag_webhook_hmac", "")).encode()
    expected = hmac.new(key, timestamp.encode() + b"." + raw, hashlib.sha256).hexdigest()
    fresh = timestamp.isdigit() and abs(time.time() - int(timestamp)) <= SIGNATURE_TOLERANCE
    if not (fresh and hmac.compare_digest(expected.encode(), signature.encode())):
        raise ApiError(401, "INVALID_SIGNATURE", "Firma ausente o inválida")


@app.post("/webhooks/servipag")
async def servipag_webhook(request: Request, _caller: str = Depends(from_gateway)):
    raw = await request.body()
    verify_signature(raw, request.headers.get("x-timestamp", ""), request.headers.get("x-signature", ""))
    try:
        event = WebhookIn.model_validate_json(raw)
    except ValidationError:
        raise ApiError(400, "VALIDATION_ERROR", "Datos inválidos")
    payment = await latest_payment(event.orderRef)
    if not payment:
        raise ApiError(404, "NOT_FOUND", "No existe un cobro para ese pedido")
    try:
        await state.db.payment_events.insert_one({
            "_id": event.eventId, "paymentId": payment["_id"], "orderRef": event.orderRef, "amount": event.amount,
            "status": event.status, "accepted": True, "receivedAt": moment()})
    except DuplicateKeyError:
        return {"received": True}
    if event.amount != payment["amount"]:
        await state.db.payment_events.update_one({"_id": event.eventId}, {"$set": {"accepted": False}})
        log(logger, "webhook_amount_mismatch", orderRef=event.orderRef)
        raise ApiError(409, "AMOUNT_MISMATCH", "El monto del aviso no coincide con el cobro")
    if event.status == "rejected":
        await state.db.payments.update_one({"_id": payment["_id"], "status": "pendiente"}, {"$set": {"status": "fallido"}})
    elif payment["status"] == "anulado":
        await state.db.payments.update_one({"_id": payment["_id"], "status": "anulado"},
                                           {"$set": {"status": "reembolso_pendiente"}})
        log(logger, "webhook_paid_after_void", orderRef=event.orderRef)
    return {"received": True}


async def ensure_receipt(payment: dict) -> dict:
    existing = await state.db.receipts.find_one({"orderId": payment["orderId"]})
    if existing:
        return existing
    sequence = await state.db.counters.find_one_and_update(
        {"_id": "receipt"}, {"$inc": {"seq": 1}}, upsert=True, return_document=ReturnDocument.AFTER)
    net, iva = split_vat(payment["amount"])
    receipt = {"_id": f"BOL-{sequence['seq']:06d}", "orderId": payment["orderId"], "clientId": payment["clientId"],
               "issuedAt": moment(), "issuedBy": payment.get("confirmedBy"),
               "items": payment["items"], "net": net, "iva": iva, "total": payment["amount"]}
    try:
        await state.db.receipts.insert_one(receipt)
    except DuplicateKeyError:
        return await state.db.receipts.find_one({"orderId": payment["orderId"]})
    email = payment.get("clientEmail")
    if email:
        await state.outbox.enqueue({"to": email, "template": "receipt", "data": {
            "receiptId": receipt["_id"], "orderId": receipt["orderId"], "issuedAt": iso(receipt["issuedAt"]),
            "items": receipt["items"], "net": net, "iva": iva, "total": receipt["total"]}})
        await state.db.payments.update_one({"_id": payment["_id"]}, {"$unset": {"clientEmail": ""}})
    return receipt


async def finalize(payment: dict) -> dict:
    if payment.get("syncPending"):
        try:
            response = await state.orders.request(
                "POST", f"/internal/orders/{payment['orderId']}/payment-confirmed",
                json={"paymentId": payment["_id"], "amount": payment["amount"]})
        except ApiError:
            raise ApiError(503, "PAYMENT_RECORDED_SYNC_PENDING", SYNC_PENDING_MESSAGE)
        if response.status_code == 409:
            await state.db.payments.update_one({"_id": payment["_id"]},
                                               {"$set": {"status": "reembolso_pendiente", "syncPending": False}})
            raise ApiError(409, "ORDER_NOT_PAYABLE",
                           "El pedido no admite el pago, por ejemplo porque fue anulado. El pago quedó marcado para reembolso.")
        if response.status_code != 200:
            raise ApiError(503, "PAYMENT_RECORDED_SYNC_PENDING", SYNC_PENDING_MESSAGE)
        await state.db.payments.update_one({"_id": payment["_id"]}, {"$set": {"syncPending": False}})
        payment = await state.db.payments.find_one({"_id": payment["_id"]})
    receipt = await ensure_receipt(payment)
    return {"orderId": payment["orderId"], "status": "pagado", "paidAt": iso(payment["paidAt"]), "receiptId": receipt["_id"]}


@app.post("/payments/{order_id}/confirmation")
async def confirm_payment(order_id: str, body: ConfirmIn, identity: Identity = Depends(cashier)):
    payment = await latest_payment(order_id)
    if not payment:
        raise ApiError(404, "NOT_FOUND", "No existe un cobro para ese pedido")
    if payment["status"] == "confirmado":
        if payment.get("reference") != body.reference:
            raise ApiError(409, "ALREADY_CONFIRMED", "El pago ya fue confirmado con otra referencia")
        return await finalize(payment)
    if payment["status"] == "anulado":
        raise ApiError(409, "ORDER_CANCELLED", "El pedido fue anulado y su cobro ya no está vigente")
    if payment["status"] != "pendiente":
        raise ApiError(409, "PAYMENT_NOT_PENDING", "El pago no está pendiente")
    if payment["method"] == "servipag":
        approved = await state.db.payment_events.find_one(
            {"paymentId": payment["_id"], "status": "approved", "accepted": True})
        if not approved or body.reference != payment.get("providerCode"):
            raise ApiError(422, "REFERENCE_NOT_VERIFIED", "La referencia no coincide con un pago aprobado por Servipag")
    try:
        result = await state.db.payments.update_one(
            {"_id": payment["_id"], "status": "pendiente"},
            {"$set": {"status": "confirmado", "reference": body.reference, "confirmedBy": identity.user_id,
                      "paidAt": moment(), "syncPending": True}})
    except DuplicateKeyError:
        raise ApiError(409, "REFERENCE_REUSED", "Esa referencia ya se usó en otro pago")
    if result.modified_count == 0:
        raise ApiError(409, "PAYMENT_NOT_PENDING", "El pago ya no está pendiente")
    return await finalize(await state.db.payments.find_one({"_id": payment["_id"]}))


@app.get("/receipts/{order_id}")
async def get_receipt(order_id: str, identity: Identity = Depends(receipt_reader)):
    payment = await latest_payment(order_id)
    if not payment:
        raise ApiError(404, "NOT_FOUND", "Pedido no encontrado")
    if not may_read(payment, identity):
        raise ApiError(403, "FORBIDDEN", "No tienes acceso a esta boleta")
    receipt = await state.db.receipts.find_one({"orderId": order_id})
    if not receipt:
        raise ApiError(409, "ORDER_NOT_PAID", "El pedido aún no tiene boleta")
    return {"receiptId": receipt["_id"], "orderId": order_id, "issuedAt": iso(receipt["issuedAt"]),
            "items": receipt["items"], "net": receipt["net"], "iva": receipt["iva"], "total": receipt["total"]}


@app.get("/internal/payments/by-order/{order_id}")
async def payment_by_order(order_id: str, identity: Identity = Depends(payment_reader)):
    payment = await latest_payment(order_id)
    if not payment:
        raise ApiError(404, "NOT_FOUND", "Pago no encontrado")
    if not may_read(payment, identity):
        raise ApiError(403, "FORBIDDEN", "No tienes acceso a este pago")
    receipt = await state.db.receipts.find_one({"orderId": order_id}, {"_id": 1})
    return {"status": payment["status"], "method": payment["method"],
            "paidAt": iso(payment["paidAt"]) if payment.get("paidAt") else None, "receiptAvailable": bool(receipt)}
