import base64
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Annotated, Literal, Optional

from fastapi import Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StringConstraints
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

logger = logging.getLogger("orders")

REVENUE_STATUSES = ("pagado", "en_preparacion", "despachado", "entregado")
CLIENT_CANCEL = ("pendiente_pago", "pagado")
ADMIN_CANCEL = CLIENT_CANCEL + ("en_preparacion", "despachado")
CASHIER_VIEW = ("pendiente_pago",)
DISPATCH_VIEW = ("pagado", "en_preparacion", "despachado")
TRANSITIONS = {"pagado": "en_preparacion", "en_preparacion": "despachado", "despachado": "entregado"}
IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
MAX_REPORT_DAYS = 366

Status = Literal["pendiente_pago", "pagado", "en_preparacion", "despachado", "entregado", "anulado"]
Identifier = Annotated[str, StringConstraints(min_length=1, max_length=64)]
IsoDate = Annotated[str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}$")]

secrets_store = VaultSecrets(os.environ.get("VAULT_SECRET_PATH", "orders"))
guard = Guard(ServiceAuth(secrets_store))
state = SimpleNamespace(db=None, inventory=None, clients=None, payments=None, outbox=None)


def moment() -> datetime:
    value = datetime.now(timezone.utc).replace(tzinfo=None)
    return value.replace(microsecond=value.microsecond // 1000 * 1000)


def iso(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds") + "Z"


class OrderItemIn(BaseModel):
    productId: Identifier
    quantity: int = Field(ge=1, le=50)


class OrderIn(BaseModel):
    items: list[OrderItemIn] = Field(min_length=1, max_length=30)
    paymentMethod: Literal["servipag", "deposito"]


class CancelIn(BaseModel):
    reason: Literal["Solicitud del cliente", "Producto sin stock", "Error en el pedido", "Otro motivo"]


class TransitionIn(BaseModel):
    to: Literal["en_preparacion", "despachado", "entregado"]


class PaymentConfirmedIn(BaseModel):
    paymentId: Identifier
    amount: int = Field(gt=0)


def item_view(item: dict) -> dict:
    return {"productId": item["productId"], "name": item["nameSnapshot"], "unitPrice": item["unitPrice"],
            "quantity": item["quantity"]}


def summary(order: dict) -> str:
    return ", ".join(f"{i['nameSnapshot']} x{i['quantity']}" for i in order["items"])


def created_view(order: dict) -> dict:
    return {"id": order["_id"], "status": order["status"], "items": [item_view(i) for i in order["items"]],
            "total": order["total"],
            "payment": {"method": order["paymentMethod"], "status": "pendiente", "instructions": order.get("paymentInstructions", "")},
            "createdAt": iso(order["createdAt"])}


def encode_cursor(order: dict) -> str:
    return base64.urlsafe_b64encode(f"{iso(order['createdAt'])}|{order['_id']}".encode()).decode()


def decode_cursor(cursor: str):
    try:
        created, order_id = base64.urlsafe_b64decode(cursor.encode()).decode().split("|", 1)
        return datetime.fromisoformat(created.rstrip("Z")), order_id
    except Exception:
        raise ApiError(400, "VALIDATION_ERROR", "Cursor inválido")


def can_view(order: dict, identity: Identity) -> bool:
    roles = set(identity.roles)
    return (
        "administrador" in roles
        or ("cliente" in roles and order["clientId"] == identity.user_id)
        or ("cajero" in roles and order["status"] in CASHIER_VIEW)
        or ("despacho" in roles and order["status"] in DISPATCH_VIEW)
    )


def full_address(order: dict) -> str:
    return f"{order['deliveryAddress']}, {order['deliveryComuna']}"


async def seed_demo_orders() -> None:
    if os.getenv("SEED_DEMO_DATA", "true").lower() != "true":
        return
    if await state.db.orders.find_one({"_id": "ORD-2026-0831"}):
        return
    d1 = datetime(2026, 8, 31, 20, 47, 0)
    d2 = datetime(2026, 8, 19, 16, 20, 0)
    await state.db.orders.insert_many([
        {
            "_id": "ORD-2026-0831",
            "clientId": "c-demo-01",
            "clientName": "Constanza Rivas",
            "deliveryAddress": "Av. Pajaritos 3450",
            "deliveryComuna": "Maipú",
            "status": "pendiente_pago",
            "paymentMethod": "deposito",
            "paymentInstructions": "Realiza el depósito de $6.800 y presenta el comprobante al cajero",
            "items": [
                {"productId": "p-0001", "nameSnapshot": "Empanada de pino", "listPrice": 2500, "unitPrice": 2500, "promotionId": None, "quantity": 2},
                {"productId": "p-0007", "nameSnapshot": "Mote con huesillo", "listPrice": 1800, "unitPrice": 1800, "promotionId": None, "quantity": 1},
            ],
            "total": 6800,
            "idempotencyKey": "seed-ord-0831",
            "createdAt": d1,
            "history": [{"status": "pendiente_pago", "at": d1, "actorId": "c-demo-01"}],
        },
        {
            "_id": "ORD-2026-0819",
            "clientId": "c-demo-01",
            "clientName": "Constanza Rivas",
            "deliveryAddress": "Av. Pajaritos 3450",
            "deliveryComuna": "Maipú",
            "status": "entregado",
            "paymentMethod": "deposito",
            "paymentId": "pay-demo-0819",
            "paidAt": d2 + timedelta(minutes=5),
            "items": [
                {"productId": "p-0004", "nameSnapshot": "Empanada de carne", "listPrice": 3500, "unitPrice": 3500, "promotionId": None, "quantity": 4},
                {"productId": "p-0008", "nameSnapshot": "Bebida 350ml", "listPrice": 1200, "unitPrice": 1200, "promotionId": None, "quantity": 2},
            ],
            "total": 16400,
            "idempotencyKey": "seed-ord-0819",
            "createdAt": d2,
            "history": [
                {"status": "pendiente_pago", "at": d2, "actorId": "c-demo-01"},
                {"status": "pagado", "at": d2 + timedelta(minutes=5), "actorId": "payments"},
                {"status": "en_preparacion", "at": d2 + timedelta(minutes=12), "actorId": "u-demo-despacho"},
                {"status": "despachado", "at": d2 + timedelta(minutes=25), "actorId": "u-demo-despacho"},
                {"status": "entregado", "at": d2 + timedelta(minutes=40), "actorId": "u-demo-despacho"},
            ],
        },
    ])


PAYMENT_ACTIONS = {"refund_pending": "refund-pending", "payment_void": "void"}


async def send_outgoing(message: dict) -> bool:
    action = PAYMENT_ACTIONS.get(message.get("type"))
    if not action:
        return True
    try:
        response = await state.payments.request("POST", f"/internal/payments/{message['orderId']}/{action}")
    except ApiError:
        return False
    return response.status_code in (200, 204, 404)


@asynccontextmanager
async def lifespan(_app):
    await secrets_store.load()
    state.db = get_database(secrets_store.get("mongo_uri"))
    db = state.db
    await db.orders.create_index([("clientId", 1), ("idempotencyKey", 1)], unique=True)
    await db.orders.create_index([("clientId", 1), ("createdAt", -1)])
    await db.orders.create_index([("status", 1), ("createdAt", 1)])
    await db.orders.create_index("createdAt")
    await db.outbox.create_index([("status", 1), ("createdAt", 1)])
    if await db.delivery_zones.count_documents({}) == 0:
        for comuna in [c.strip() for c in os.getenv("DELIVERY_COMUNAS", "Maipú,Santiago").split(",") if c.strip()]:
            await db.delivery_zones.insert_one({"_id": comuna, "enabled": True})
    await seed_demo_orders()
    credential = lambda key: (lambda: str(secrets_store.get(key, "")))
    state.inventory = ServiceClient(os.environ["INVENTORY_URL"], credential("inventory_credential"))
    state.clients = ServiceClient(os.environ["CLIENTS_URL"], credential("clients_credential"))
    state.payments = ServiceClient(os.environ["PAYMENTS_URL"], credential("payments_credential"))
    state.outbox = Outbox(db.outbox, send_outgoing, interval=5.0)
    await state.outbox.start()
    yield
    await state.outbox.stop()
    for client in (state.inventory, state.clients, state.payments):
        await client.aclose()


app = create_app("orders", lifespan=lifespan)
order_creator = guard.user("cliente", callers=("api-gateway",))
client_only = guard.user("cliente")
order_reader = guard.user("cliente", "cajero", "despacho", "administrador")
canceller = guard.user("cliente", "administrador", callers=("api-gateway",))
staff_lister = guard.user("cajero", "despacho", callers=("api-gateway",))
dispatcher = guard.user("despacho", callers=("api-gateway",))
reporter = guard.user("administrador", "dueno", callers=("api-gateway",))
from_payments = guard.caller("payments")


async def new_order_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m%d")
    for _ in range(20):
        counter = await state.db.counters.find_one_and_update(
            {"_id": f"order-{stamp}"}, {"$inc": {"seq": 1}}, upsert=True, return_document=ReturnDocument.AFTER)
        candidate = f"ORD-{stamp}-{counter['seq']:04d}"
        if not await state.db.orders.find_one({"_id": candidate}, {"_id": 1}):
            return candidate
    raise ApiError(503, "SERVICE_UNAVAILABLE", "No se pudo generar el número de pedido")


@app.post("/orders")
async def create_order(body: OrderIn, request: Request, identity: Identity = Depends(order_creator)):
    key = request.headers.get("idempotency-key", "")
    if not IDEMPOTENCY_RE.match(key):
        raise ApiError(400, "IDEMPOTENCY_KEY_REQUIRED", "Falta la cabecera Idempotency-Key")
    existing = await state.db.orders.find_one({"clientId": identity.user_id, "idempotencyKey": key})
    if existing:
        return JSONResponse(created_view(existing), status_code=200)

    quantities: dict = {}
    for item in body.items:
        quantities[item.productId] = quantities.get(item.productId, 0) + item.quantity
    if any(q > 50 for q in quantities.values()):
        raise ApiError(400, "VALIDATION_ERROR", "La cantidad máxima por producto es 50")

    info = await state.clients.request("GET", f"/internal/clients/{identity.user_id}/delivery-info")
    if info.status_code == 404:
        raise ApiError(422, "CLIENT_PROFILE_MISSING", "Tu perfil de cliente no está completo")
    if info.status_code != 200:
        raise ApiError(502, "BAD_GATEWAY", "No se pudieron obtener los datos de entrega")
    client = info.json()

    zones = [z["_id"] async for z in state.db.delivery_zones.find({"enabled": True})]
    if client["comuna"] not in zones:
        raise ApiError(422, "DELIVERY_OUT_OF_ZONE", "Tu comuna no tiene reparto", {"enabledComunas": sorted(zones)})

    check = await state.inventory.request("POST", "/internal/products/availability-check", json={
        "items": [{"productId": pid, "quantity": q} for pid, q in quantities.items()], "forRegisteredClient": True})
    if check.status_code != 200:
        raise ApiError(502, "BAD_GATEWAY", "No se pudo consultar el catálogo")
    catalog = {i["productId"]: i for i in check.json()["items"]}
    unavailable = [pid for pid in quantities if pid not in catalog or not catalog[pid]["available"]]
    if unavailable:
        raise ApiError(409, "PRODUCTS_UNAVAILABLE", "Algunos productos no están disponibles", {"unavailable": unavailable})

    items = [{"productId": pid, "nameSnapshot": catalog[pid]["name"], "listPrice": catalog[pid]["listPrice"],
              "unitPrice": catalog[pid]["unitPrice"], "promotionId": catalog[pid]["promotionId"], "quantity": q}
             for pid, q in quantities.items()]
    created = moment()
    order = {
        "_id": await new_order_id(), "clientId": identity.user_id, "clientName": client["name"],
        "deliveryAddress": client["address"], "deliveryComuna": client["comuna"], "status": "pendiente_pago",
        "paymentMethod": body.paymentMethod, "items": items, "total": sum(i["unitPrice"] * i["quantity"] for i in items),
        "idempotencyKey": key, "createdAt": created,
        "history": [{"status": "pendiente_pago", "at": created, "actorId": identity.user_id}],
    }
    try:
        await state.db.orders.insert_one(order)
    except DuplicateKeyError:
        existing = await state.db.orders.find_one({"clientId": identity.user_id, "idempotencyKey": key})
        if existing:
            return JSONResponse(created_view(existing), status_code=200)
        raise

    try:
        payment = await state.payments.request("POST", "/internal/payments", json={
            "orderId": order["_id"], "clientId": identity.user_id, "clientEmail": client["email"],
            "amount": order["total"], "method": body.paymentMethod,
            "items": [{"name": i["nameSnapshot"], "quantity": i["quantity"], "unitPrice": i["unitPrice"]} for i in items]})
        if payment.status_code != 201:
            raise ApiError(502, "BAD_GATEWAY", "No se pudo iniciar el cobro")
    except ApiError:
        await state.db.orders.delete_one({"_id": order["_id"]})
        raise
    order["paymentInstructions"] = payment.json().get("instructions", "")
    await state.db.orders.update_one({"_id": order["_id"]}, {"$set": {"paymentInstructions": order["paymentInstructions"]}})
    return JSONResponse(created_view(order), status_code=201)


@app.get("/orders/mine")
async def my_orders(status: Optional[Status] = None, size: int = Query(20, ge=1, le=20), after: Optional[str] = None,
                    include: Optional[Literal["items"]] = None, identity: Identity = Depends(client_only)):
    query: dict = {"clientId": identity.user_id}
    if status:
        query["status"] = status
    if after:
        created, order_id = decode_cursor(after)
        query["$or"] = [{"createdAt": {"$lt": created}}, {"createdAt": created, "_id": {"$lt": order_id}}]
    cursor = state.db.orders.find(query).sort([("createdAt", -1), ("_id", -1)]).limit(size + 1)
    orders = [o async for o in cursor]
    page, has_more = orders[:size], len(orders) > size
    items = []
    for order in page:
        view = {"id": order["_id"], "createdAt": iso(order["createdAt"]), "status": order["status"],
                "itemsCount": sum(i["quantity"] for i in order["items"]), "total": order["total"],
                "summary": summary(order), "canCancel": order["status"] in CLIENT_CANCEL}
        if include == "items":
            view["items"] = [item_view(i) for i in order["items"]]
        items.append(view)
    return {"items": items, "nextCursor": encode_cursor(page[-1]) if has_more and page else None}


@app.get("/orders")
async def staff_orders(status: Optional[Status] = None, page: int = Query(1, ge=1, le=10000),
                       size: int = Query(20, ge=1, le=50), identity: Identity = Depends(staff_lister)):
    roles = set(identity.roles)
    allowed = set(CASHIER_VIEW) if "cajero" in roles else set(DISPATCH_VIEW)
    if status and status not in allowed:
        raise ApiError(403, "FORBIDDEN", "No tienes permiso para ver pedidos en ese estado")
    query = {"status": status} if status else {"status": {"$in": sorted(allowed)}}
    total = await state.db.orders.count_documents(query)
    cursor = state.db.orders.find(query).sort([("createdAt", 1), ("_id", 1)]).skip((page - 1) * size).limit(size)
    items = []
    async for order in cursor:
        view = {"id": order["_id"], "createdAt": iso(order["createdAt"]), "status": order["status"], "total": order["total"],
                "paymentMethod": order["paymentMethod"], "clientName": order["clientName"],
                "items": [{"name": i["nameSnapshot"], "quantity": i["quantity"]} for i in order["items"]]}
        if "despacho" in roles:
            view["deliveryAddress"] = full_address(order)
        items.append(view)
    return {"items": items, "page": page, "size": size, "total": total}


@app.get("/orders/{order_id}")
async def get_order(order_id: str, identity: Identity = Depends(order_reader)):
    order = await state.db.orders.find_one({"_id": order_id})
    if not order:
        raise ApiError(404, "NOT_FOUND", "Pedido no encontrado")
    if not can_view(order, identity):
        raise ApiError(403, "FORBIDDEN", "No tienes acceso a este pedido")
    roles = set(identity.roles)
    view = {"id": order["_id"], "status": order["status"], "items": [item_view(i) for i in order["items"]],
            "total": order["total"], "paymentMethod": order["paymentMethod"], "createdAt": iso(order["createdAt"]),
            "history": [{"status": h["status"], "at": iso(h["at"])} for h in order["history"]]}
    if "despacho" in roles or ("cliente" in roles and order["clientId"] == identity.user_id):
        view["deliveryAddress"] = full_address(order)
    return view


@app.post("/orders/{order_id}/cancellation")
async def cancel_order(order_id: str, body: CancelIn, identity: Identity = Depends(canceller)):
    order = await state.db.orders.find_one({"_id": order_id})
    if not order:
        raise ApiError(404, "NOT_FOUND", "Pedido no encontrado")
    is_admin = "administrador" in identity.roles
    if not is_admin and order["clientId"] != identity.user_id:
        raise ApiError(403, "FORBIDDEN", "No tienes acceso a este pedido")
    if order["status"] not in (ADMIN_CANCEL if is_admin else CLIENT_CANCEL):
        raise ApiError(409, "ORDER_NOT_CANCELLABLE", "El pedido ya no se puede anular")
    cancelled_by = "administrador" if is_admin else "cliente"
    at = moment()
    result = await state.db.orders.update_one(
        {"_id": order_id, "status": order["status"]},
        {"$set": {"status": "anulado", "cancelReason": body.reason, "cancelledBy": cancelled_by, "cancelledAt": at},
         "$push": {"history": {"status": "anulado", "at": at, "actorId": identity.user_id}}})
    if result.modified_count == 0:
        raise ApiError(409, "ORDER_NOT_CANCELLABLE", "El pedido cambió de estado. Revísalo e inténtalo de nuevo.")
    message = {"type": "payment_void" if order["status"] == "pendiente_pago" else "refund_pending", "orderId": order_id}
    if not await send_outgoing(message):
        await state.outbox.enqueue(message)
    return {"id": order_id, "status": "anulado", "cancelledAt": iso(at), "cancelledBy": cancelled_by}


@app.post("/orders/{order_id}/transitions")
async def transition(order_id: str, body: TransitionIn, identity: Identity = Depends(dispatcher)):
    order = await state.db.orders.find_one({"_id": order_id})
    if not order:
        raise ApiError(404, "NOT_FOUND", "Pedido no encontrado")
    if TRANSITIONS.get(order["status"]) != body.to:
        raise ApiError(409, "INVALID_TRANSITION", f"No se puede pasar de {order['status']} a {body.to}")
    at = moment()
    result = await state.db.orders.update_one(
        {"_id": order_id, "status": order["status"]},
        {"$set": {"status": body.to}, "$push": {"history": {"status": body.to, "at": at, "actorId": identity.user_id}}})
    if result.modified_count == 0:
        raise ApiError(409, "INVALID_TRANSITION", "El pedido cambió de estado. Actualiza la lista.")
    return {"id": order_id, "status": body.to, "updatedAt": iso(at)}


@app.get("/reports/sales")
async def sales_report(date_from: IsoDate = Query(alias="from"), date_to: IsoDate = Query(alias="to"),
                       page: int = Query(1, ge=1, le=10000), size: int = Query(50, ge=1, le=200),
                       _identity: Identity = Depends(reporter)):
    try:
        first, last = date.fromisoformat(date_from), date.fromisoformat(date_to)
    except ValueError:
        raise ApiError(400, "INVALID_RANGE", "Las fechas no son válidas")
    if last < first or (last - first).days + 1 > MAX_REPORT_DAYS:
        raise ApiError(400, "INVALID_RANGE", f"El rango debe ser válido y de máximo {MAX_REPORT_DAYS} días")
    window = {"createdAt": {"$gte": datetime(first.year, first.month, first.day),
                            "$lt": datetime(last.year, last.month, last.day) + timedelta(days=1)}}
    sold = {**window, "status": {"$in": list(REVENUE_STATUSES)}}
    totals = await state.db.orders.aggregate([
        {"$match": sold}, {"$group": {"_id": None, "orders": {"$sum": 1}, "revenue": {"$sum": "$total"}}}]).to_list(1)
    orders_count, revenue = (totals[0]["orders"], totals[0]["revenue"]) if totals else (0, 0)
    by_product = await state.db.orders.aggregate([
        {"$match": sold}, {"$unwind": "$items"},
        {"$group": {"_id": "$items.productId", "name": {"$first": "$items.nameSnapshot"},
                    "units": {"$sum": "$items.quantity"},
                    "revenue": {"$sum": {"$multiply": ["$items.quantity", "$items.unitPrice"]}}}},
        {"$sort": {"revenue": -1}}, {"$limit": 20}]).to_list(20)
    total = await state.db.orders.count_documents(window)
    cursor = state.db.orders.find(window).sort([("createdAt", -1), ("_id", -1)]).skip((page - 1) * size).limit(size)
    return {
        "period": {"from": date_from, "to": date_to},
        "kpis": {"orders": orders_count, "revenue": revenue,
                 "averageTicket": round(revenue / orders_count) if orders_count else 0,
                 "cancelled": await state.db.orders.count_documents({**window, "status": "anulado"})},
        "byProduct": [{"productId": p["_id"], "name": p["name"], "units": p["units"], "revenue": p["revenue"]} for p in by_product],
        "orders": [{"id": o["_id"], "createdAt": iso(o["createdAt"]), "clientName": o["clientName"],
                    "status": o["status"], "total": o["total"]} async for o in cursor],
        "page": page, "size": size, "total": total,
    }


@app.post("/internal/orders/{order_id}/payment-confirmed")
async def payment_confirmed(order_id: str, body: PaymentConfirmedIn, _caller: str = Depends(from_payments)):
    order = await state.db.orders.find_one({"_id": order_id})
    if not order:
        raise ApiError(404, "NOT_FOUND", "Pedido no encontrado")
    if order["status"] == "pagado" and order.get("paymentId") == body.paymentId:
        return {"orderId": order_id, "status": "pagado"}
    if order["status"] != "pendiente_pago":
        raise ApiError(409, "ORDER_NOT_PAYABLE", "El pedido no admite el pago en su estado actual")
    if body.amount != order["total"]:
        raise ApiError(409, "AMOUNT_MISMATCH", "El monto no coincide con el pedido")
    at = moment()
    result = await state.db.orders.update_one(
        {"_id": order_id, "status": "pendiente_pago"},
        {"$set": {"status": "pagado", "paymentId": body.paymentId, "paidAt": at},
         "$push": {"history": {"status": "pagado", "at": at, "actorId": "payments"}}})
    if result.modified_count == 0:
        raise ApiError(409, "ORDER_NOT_PAYABLE", "El pedido no admite el pago en su estado actual")
    return {"orderId": order_id, "status": "pagado"}
