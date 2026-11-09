import logging
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Annotated, Literal, Optional
from uuid import uuid4

from fastapi import Depends, Query, Request, Response
from pydantic import BaseModel, Field, StringConstraints
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from lachilena_common.app_factory import create_app
from lachilena_common.db import get_database
from lachilena_common.errors import ApiError
from lachilena_common.logging_setup import log
from lachilena_common.security import Guard, Identity, ServiceAuth
from lachilena_common.vault import VaultSecrets

logger = logging.getLogger("inventory")
CACHE_TTL = 30.0
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

secrets_store = VaultSecrets(os.environ.get("VAULT_SECRET_PATH", "inventory"))
guard = Guard(ServiceAuth(secrets_store))
state = SimpleNamespace(db=None, cache=None)

Category = Literal["horno", "frita", "bebida"]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=80)]
Description = Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)]
IsoDate = Annotated[str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}$")]
Identifier = Annotated[str, StringConstraints(min_length=1, max_length=64)]


def now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds") + "Z"


def today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


class ProductIn(BaseModel):
    name: Name
    category: Category
    price: int = Field(gt=0, le=1_000_000)
    description: Description = ""


class AvailabilityIn(BaseModel):
    available: bool


class PromotionIn(BaseModel):
    name: Name
    description: Description = ""
    productId: Identifier
    discountPercent: int = Field(ge=1, le=90)
    audience: Literal["todos", "registrados"]
    startsAt: IsoDate
    endsAt: IsoDate


class ActiveIn(BaseModel):
    active: bool


class CheckItem(BaseModel):
    productId: Identifier
    quantity: int = Field(ge=1, le=50)


class CheckIn(BaseModel):
    items: list[CheckItem] = Field(min_length=1, max_length=50)
    forRegisteredClient: bool = True


async def load_catalog():
    if state.cache and time.monotonic() - state.cache[0] < CACHE_TTL:
        return state.cache[1], state.cache[2]
    products = [p async for p in state.db.products.find({"archived": False}).sort([("createdAt", 1), ("_id", 1)])]
    promotions = [p async for p in state.db.promotions.find({"archived": False, "active": True})]
    state.cache = (time.monotonic(), products, promotions)
    return products, promotions


def invalidate() -> None:
    state.cache = None


def applicable(promotions: list, product_id: str, for_client: bool):
    current = today()
    for promo in promotions:
        if (promo["productId"] == product_id and promo["startsAt"] <= current <= promo["endsAt"]
                and (promo["audience"] == "todos" or for_client)):
            return promo
    return None


def final_price(price: int, promo) -> int:
    return price if not promo else (price * (100 - promo["discountPercent"]) + 50) // 100


def public_product(product: dict, promotions: list, for_client: bool) -> dict:
    promo = applicable(promotions, product["_id"], for_client)
    return {
        "id": product["_id"], "name": product["name"], "category": product["category"],
        "price": product["price"], "finalPrice": final_price(product["price"], promo),
        "promotion": {"id": promo["_id"], "name": promo["name"], "discountPercent": promo["discountPercent"]} if promo else None,
        "description": product["description"], "available": product["available"],
    }


def admin_product(product: dict) -> dict:
    return {
        "id": product["_id"], "name": product["name"], "category": product["category"], "price": product["price"],
        "description": product["description"], "available": product["available"], "updatedAt": iso(product["updatedAt"]),
    }


def valid_dates(starts: str, ends: str) -> None:
    try:
        first, last = date.fromisoformat(starts), date.fromisoformat(ends)
    except ValueError:
        raise ApiError(400, "INVALID_DATES", "Las fechas no son válidas")
    if last <= first:
        raise ApiError(400, "INVALID_DATES", "La fecha de término debe ser posterior a la de inicio")


DEMO_PRODUCTS = [
    ("p-0001", "Empanada de pino", "horno", 2500, "Carne molida, cebolla, huevo duro, aceituna y pasas. La clásica chilena.", True),
    ("p-0002", "Empanada de queso", "horno", 2200, "Queso gauda derretido, horneada hasta dorar.", True),
    ("p-0003", "Empanada napolitana", "horno", 2800, "Jamón, queso, tomate y orégano.", False),
    ("p-0004", "Empanada de carne", "frita", 3500, "Rellena con carne molida, cebolla y ajo, frita al momento.", True),
    ("p-0005", "Empanada de camarón", "frita", 3500, "Camarón, queso, cebolla y pimentón.", True),
    ("p-0006", "Empanada de mariscos", "frita", 3900, "Mezcla de mariscos salteados con salsa verde.", True),
    ("p-0007", "Mote con huesillo", "bebida", 1800, "Bebida tradicional bien helada.", True),
    ("p-0008", "Bebida 350ml", "bebida", 1200, "A elección: Coca-Cola, Sprite o Fanta.", True),
]


async def seed_demo_data() -> None:
    if os.getenv("SEED_DEMO_DATA", "true").lower() != "true" or await state.db.products.count_documents({}) > 0:
        return
    moment = now()
    for index, (pid, name, category, price, description, available) in enumerate(DEMO_PRODUCTS):
        await state.db.products.insert_one({
            "_id": pid, "name": name, "nameKey": name.lower(), "category": category, "price": price,
            "description": description, "available": available, "archived": False,
            "createdAt": moment + timedelta(seconds=index), "updatedAt": moment,
        })
    start, end = today(), (datetime.now(timezone.utc).date() + timedelta(days=90)).isoformat()
    for pid, product, name, percent, audience, desc in [
        ("pr-0001", "p-0004", "10% en empanadas de carne", 10, "registrados", "Descuento exclusivo para clientes registrados."),
        ("pr-0002", "p-0002", "15% en empanadas de queso", 15, "todos", "Oferta abierta a todos los visitantes."),
    ]:
        await state.db.promotions.insert_one({
            "_id": pid, "name": name, "description": desc, "productId": product, "discountPercent": percent,
            "audience": audience, "startsAt": start, "endsAt": end, "active": True, "archived": False, "createdAt": moment,
        })
    log(logger, "demo_data_seeded")


@asynccontextmanager
async def lifespan(_app):
    await secrets_store.load()
    state.db = get_database(secrets_store.get("mongo_uri"))
    await state.db.products.create_index("nameKey", unique=True, partialFilterExpression={"archived": False})
    await state.db.products.create_index([("createdAt", 1)])
    await state.db.promotions.create_index([("productId", 1), ("active", 1)])
    await seed_demo_data()
    yield


app = create_app("inventory", lifespan=lifespan)
from_gateway = guard.caller("api-gateway")
from_orders = guard.caller("orders")
from_graphql = guard.caller("graphql-gateway")
catalog_admin = guard.user("administrador", "dueno", callers=("api-gateway",))


@app.get("/products")
async def list_products(request: Request, category: Optional[Category] = None, available: Optional[bool] = None,
                        onOffer: Optional[bool] = None, _caller: str = Depends(from_gateway)):
    for_client = "cliente" in request.headers.get("x-authenticated-roles", "").split(",")
    products, promotions = await load_catalog()
    items = []
    for product in products:
        if category and product["category"] != category:
            continue
        if available is not None and product["available"] != available:
            continue
        item = public_product(product, promotions, for_client)
        if onOffer and item["promotion"] is None:
            continue
        items.append(item)
    return {"items": items}


@app.get("/admin/products")
async def list_admin_products(q: Optional[Annotated[str, StringConstraints(max_length=60)]] = None,
                              _identity: Identity = Depends(catalog_admin)):
    query: dict = {"archived": False}
    if q:
        query["name"] = {"$regex": re.escape(q), "$options": "i"}
    cursor = state.db.products.find(query).sort([("createdAt", 1), ("_id", 1)])
    return {"items": [admin_product(p) async for p in cursor]}


@app.post("/admin/products", status_code=201)
async def create_product(body: ProductIn, _identity: Identity = Depends(catalog_admin)):
    moment = now()
    document = {"_id": "p-" + uuid4().hex[:10], "name": body.name, "nameKey": body.name.lower(), "category": body.category,
                "price": body.price, "description": body.description, "available": True, "archived": False,
                "createdAt": moment, "updatedAt": moment}
    try:
        await state.db.products.insert_one(document)
    except DuplicateKeyError:
        raise ApiError(409, "NAME_TAKEN", "Ya existe un producto con ese nombre")
    invalidate()
    return admin_product(document)


@app.put("/admin/products/{product_id}")
async def update_product(product_id: str, body: ProductIn, _identity: Identity = Depends(catalog_admin)):
    changes = {"name": body.name, "nameKey": body.name.lower(), "category": body.category, "price": body.price,
               "description": body.description, "updatedAt": now()}
    try:
        product = await state.db.products.find_one_and_update(
            {"_id": product_id, "archived": False}, {"$set": changes}, return_document=ReturnDocument.AFTER)
    except DuplicateKeyError:
        raise ApiError(409, "NAME_TAKEN", "Ya existe un producto con ese nombre")
    if not product:
        raise ApiError(404, "NOT_FOUND", "Producto no encontrado")
    invalidate()
    return admin_product(product)


@app.put("/admin/products/{product_id}/availability")
async def set_availability(product_id: str, body: AvailabilityIn, _identity: Identity = Depends(catalog_admin)):
    result = await state.db.products.update_one({"_id": product_id, "archived": False},
                                                {"$set": {"available": body.available, "updatedAt": now()}})
    if result.matched_count == 0:
        raise ApiError(404, "NOT_FOUND", "Producto no encontrado")
    invalidate()
    return {"id": product_id, "available": body.available}


@app.delete("/admin/products/{product_id}", status_code=204)
async def archive_product(product_id: str, _identity: Identity = Depends(catalog_admin)):
    result = await state.db.products.update_one({"_id": product_id, "archived": False},
                                                {"$set": {"archived": True, "updatedAt": now()}})
    if result.matched_count == 0:
        raise ApiError(404, "NOT_FOUND", "Producto no encontrado")
    await state.db.promotions.update_many({"productId": product_id}, {"$set": {"archived": True}})
    invalidate()
    return Response(status_code=204)


def admin_promotion(promo: dict, product_name: str) -> dict:
    return {
        "id": promo["_id"], "name": promo["name"], "description": promo["description"], "productId": promo["productId"],
        "productName": product_name, "discountPercent": promo["discountPercent"], "audience": promo["audience"],
        "startsAt": promo["startsAt"], "endsAt": promo["endsAt"], "active": promo["active"],
    }


@app.get("/admin/promotions")
async def list_promotions(q: Optional[Annotated[str, StringConstraints(max_length=60)]] = None,
                          _identity: Identity = Depends(catalog_admin)):
    promotions = [p async for p in state.db.promotions.find({"archived": False}).sort("createdAt", 1)]
    names = {p["_id"]: p["name"] async for p in state.db.products.find({"_id": {"$in": [x["productId"] for x in promotions]}})}
    items = [admin_promotion(p, names.get(p["productId"], "Producto eliminado")) for p in promotions]
    if q:
        needle = q.lower()
        items = [i for i in items if needle in i["name"].lower() or needle in i["productName"].lower()]
    return {"items": items}


@app.post("/admin/promotions", status_code=201)
async def create_promotion(body: PromotionIn, _identity: Identity = Depends(catalog_admin)):
    valid_dates(body.startsAt, body.endsAt)
    product = await state.db.products.find_one({"_id": body.productId, "archived": False})
    if not product:
        raise ApiError(404, "NOT_FOUND", "El producto no existe o está archivado")
    if await find_overlap(body.productId, body.startsAt, body.endsAt):
        raise ApiError(409, "PROMOTION_OVERLAP", "Ese producto ya tiene una promoción activa en esas fechas")
    document = {"_id": "pr-" + uuid4().hex[:10], "name": body.name, "description": body.description,
                "productId": body.productId, "discountPercent": body.discountPercent, "audience": body.audience,
                "startsAt": body.startsAt, "endsAt": body.endsAt, "active": True, "archived": False, "createdAt": now()}
    await state.db.promotions.insert_one(document)
    invalidate()
    return {"id": document["_id"], "active": True}


async def find_overlap(product_id: str, starts: str, ends: str, exclude: str | None = None):
    query = {"productId": product_id, "active": True, "archived": False,
             "startsAt": {"$lte": ends}, "endsAt": {"$gte": starts}}
    if exclude:
        query["_id"] = {"$ne": exclude}
    return await state.db.promotions.find_one(query)


@app.put("/admin/promotions/{promotion_id}/active")
async def set_promotion_active(promotion_id: str, body: ActiveIn, _identity: Identity = Depends(catalog_admin)):
    promo = await state.db.promotions.find_one({"_id": promotion_id, "archived": False})
    if not promo:
        raise ApiError(404, "NOT_FOUND", "Promoción no encontrada")
    if body.active and await find_overlap(promo["productId"], promo["startsAt"], promo["endsAt"], exclude=promotion_id):
        raise ApiError(409, "PROMOTION_OVERLAP", "Ese producto ya tiene otra promoción activa en esas fechas")
    await state.db.promotions.update_one({"_id": promotion_id}, {"$set": {"active": body.active}})
    invalidate()
    return {"id": promotion_id, "active": body.active}


@app.delete("/admin/promotions/{promotion_id}", status_code=204)
async def archive_promotion(promotion_id: str, _identity: Identity = Depends(catalog_admin)):
    result = await state.db.promotions.update_one({"_id": promotion_id, "archived": False}, {"$set": {"archived": True}})
    if result.matched_count == 0:
        raise ApiError(404, "NOT_FOUND", "Promoción no encontrada")
    invalidate()
    return Response(status_code=204)


@app.post("/internal/products/availability-check")
async def availability_check(body: CheckIn, _caller: str = Depends(from_orders)):
    products, promotions = await load_catalog()
    by_id = {p["_id"]: p for p in products}
    items, missing = [], []
    for requested in dict.fromkeys(i.productId for i in body.items):
        product = by_id.get(requested)
        if not product:
            missing.append(requested)
            continue
        promo = applicable(promotions, requested, body.forRegisteredClient)
        items.append({"productId": requested, "name": product["name"], "listPrice": product["price"],
                      "unitPrice": final_price(product["price"], promo), "promotionId": promo["_id"] if promo else None,
                      "available": product["available"]})
    return {"items": items, "missing": missing}


@app.get("/internal/products")
async def products_by_ids(ids: str = Query(max_length=3500), _caller: str = Depends(from_graphql)):
    wanted = [i for i in ids.split(",") if i]
    if not wanted or len(wanted) > 50 or not all(ID_RE.match(i) for i in wanted):
        raise ApiError(400, "VALIDATION_ERROR", "Identificadores inválidos")
    cursor = state.db.products.find({"_id": {"$in": wanted}})
    return {"items": [{"id": p["_id"], "name": p["name"], "description": p["description"], "available": p["available"]}
                      async for p in cursor]}
