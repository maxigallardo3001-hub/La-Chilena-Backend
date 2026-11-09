import json
import os
import socket
import sys
import uuid
from urllib.parse import urlparse

import httpx

from helpers import Api, CA, GATEWAY

PHASES = ("bordes", "sin-inventory", "sin-payments", "sin-auth", "recuperado", "antes-reinicio", "tras-reinicio")
CLIENT_EMAIL = "constanza@gmail.com"
CLIENT_PASSWORD = os.environ["DEMO_PASSWORD_CLIENTE"]
DELIVERED_ORDER = "ORD-2026-0819"
STATE_FILE = "/evidencias/.resilience-state.json"
SUMMARY = (
    "query R($id: ID!) { resumenPedido(orderId: $id) { pedido { estado total items { nombre producto { descripcion } } } "
    "pago { estado } } }"
)
failures = []


def check(name, condition, detail=""):
    print(("PASS " if condition else "FAIL ") + name + (f"  ({str(detail)[:200]})" if detail and not condition else ""))
    if not condition:
        failures.append(name)


def code(response):
    try:
        return response.json()["error"]["code"]
    except Exception:
        return None


def client():
    api = Api()
    response = api.login(CLIENT_EMAIL, CLIENT_PASSWORD)
    assert response.status_code == 200, response.text
    return api


def new_order(api, product="p-0002", quantity=1, key=None):
    return api.post("/orders", headers={"Idempotency-Key": key or uuid.uuid4().hex},
                    json={"items": [{"productId": product, "quantity": quantity}], "paymentMethod": "deposito"})


def my_order_ids(api):
    return [order["id"] for order in api.get("/orders/mine").json()["items"]]


def edges():
    anon = Api()
    api = client()
    headers = anon.get("/products").headers
    check("R01 cabeceras de seguridad: nosniff, DENY, HSTS, no-store y no-referrer",
          headers.get("x-content-type-options") == "nosniff" and headers.get("x-frame-options") == "DENY"
          and "max-age" in headers.get("strict-transport-security", "") and headers.get("cache-control") == "no-store"
          and headers.get("referrer-policy") == "no-referrer", dict(headers))
    host = urlparse(GATEWAY)
    with socket.create_connection((host.hostname, host.port)) as raw, CA.wrap_socket(raw, server_hostname=host.hostname) as tls:
        version = tls.version()
    check("R02 TLS 1.2 o superior, verificado con la CA interna en modo estricto", version in ("TLSv1.2", "TLSv1.3"), version)
    response = anon.post("/auth/login", json={"email": {"$ne": None}, "password": {"$ne": None}})
    check("R03 inyección NoSQL en el ingreso: 400 y sin cookie de sesión",
          response.status_code == 400 and "set-cookie" not in response.headers, response.text)
    response = anon.post("/auth/login", json={"email": CLIENT_EMAIL, "password": "incorrecta-123"})
    check("R04 clave incorrecta: 401 con mensaje genérico", response.status_code == 401 and "constanza" not in response.text.lower(),
          response.text)
    response = anon.get("/orders/mine", headers={"X-Authenticated-User": "c-demo-01", "X-Authenticated-Roles": "cliente",
                                                 "X-Service-Credential": "inventada"})
    check("R05 cabeceras de identidad interna enviadas desde afuera: se ignoran (401)", response.status_code == 401, response.text)
    check("R06 cookie de sesión inventada: 401", anon.get("/orders/mine", headers={"Cookie": "__Host-sid=" + "A" * 43}).status_code == 401)
    response = anon.post("/auth/login", headers={"Origin": "https://atacante.example"},
                         json={"email": CLIENT_EMAIL, "password": CLIENT_PASSWORD})
    check("R07 POST desde un origen no permitido: 403 ORIGIN_NOT_ALLOWED",
          response.status_code == 403 and code(response) == "ORIGIN_NOT_ALLOWED", response.text)
    response = api.post("/orders", content="items=1", headers={"Content-Type": "text/plain", "Idempotency-Key": uuid.uuid4().hex})
    check("R08 cuerpo que no es JSON: 400", response.status_code == 400, response.text)
    response = api.post("/orders", headers={"Idempotency-Key": uuid.uuid4().hex},
                        json={"items": [{"productId": "p-0001", "quantity": 1}], "paymentMethod": "deposito", "relleno": "x" * 40000})
    check("R09 cuerpo sobre el máximo de la ruta: 413", response.status_code == 413, response.text)
    check("R10 verbo no declarado en la lista de rutas (DELETE /products): 404", anon.delete("/products").status_code == 404)
    check("R11 rutas internas no se publican por el gateway: 404",
          anon.get("/internal/orders/x").status_code == 404 and anon.get("/%2e%2e/internal/clients/c-demo-01").status_code == 404)
    response = api.post("/graphql", json={"operationName": "I", "query": "query I { __schema { types { name } } }"})
    check("R12 introspección GraphQL deshabilitada", "__schema" not in json.dumps(response.json().get("data") or {}), response.text)
    response = api.post("/orders", headers={"Idempotency-Key": uuid.uuid4().hex},
                        json={"items": [{"productId": "p-0001", "quantity": 0}], "paymentMethod": "deposito"})
    check("R13 cantidad 0: 400 VALIDATION_ERROR", response.status_code == 400 and code(response) == "VALIDATION_ERROR", response.text)
    response = api.post("/orders", headers={"Idempotency-Key": uuid.uuid4().hex},
                        json={"items": [{"productId": "p-0001", "quantity": 1}], "paymentMethod": "bitcoin"})
    check("R14 medio de pago inexistente: 400", response.status_code == 400, response.text)
    check("R15 consulta de más de 2 KB: 400", api.get(f"/orders/{DELIVERED_ORDER}", params={"q": "x" * 3000}).status_code == 400)
    check("R16 filtro inválido en el catálogo: 400", anon.get("/products", params={"available": "quizas"}).status_code == 400)


def without_inventory():
    response = Api().get("/products")
    check("R17 inventory detenido: el catálogo responde 502 BAD_GATEWAY sin detalles internos",
          response.status_code == 502 and code(response) == "BAD_GATEWAY" and "inventory" not in response.text, response.text)
    response = client().graphql(SUMMARY, {"id": DELIVERED_ORDER})
    body = response.json()
    summary = (body.get("data") or {}).get("resumenPedido")
    check("R18 inventory detenido: GraphQL entrega pedido y pago, con producto en null y error parcial",
          response.status_code == 200 and summary and summary["pedido"]["estado"] == "ENTREGADO" and summary["pago"]
          and summary["pedido"]["items"][0]["producto"] is None and body.get("errors"), body)


def without_payments():
    api = client()
    response = api.get(f"/orders/{DELIVERED_ORDER}/receipt")
    check("R19 payments detenido: la boleta responde 502 sin detalles internos",
          response.status_code == 502 and "payments" not in response.text, response.text)
    before = my_order_ids(api)
    response = new_order(api)
    check("R20 payments detenido: crear un pedido falla de forma controlada (502/503)",
          response.status_code in (502, 503) and "Traceback" not in response.text, response.text)
    check("R21 el pedido que no pudo registrar su cobro no queda a medias", my_order_ids(api) == before)


def without_auth():
    check("R22 auth detenido: el catálogo público sigue respondiendo", Api().get("/products").status_code == 200)
    response = Api().get("/orders/mine", headers={"Cookie": "__Host-sid=" + "B" * 43})
    check("R23 auth detenido: las rutas con sesión responden 503 AUTH_UNAVAILABLE y no abren el acceso",
          response.status_code == 503 and code(response) == "AUTH_UNAVAILABLE", response.text)


def recovered():
    api = client()
    check("R24 inventory, payments y auth vuelven a operar sin intervención manual",
          Api().get("/products").status_code == 200 and api.get(f"/orders/{DELIVERED_ORDER}/receipt").status_code == 200
          and api.get("/orders/mine").status_code == 200)
    response = new_order(api)
    check("R25 tras la recuperación se puede volver a crear un pedido", response.status_code == 201, response.text)


def before_restart():
    key = uuid.uuid4().hex
    response = new_order(client(), product="p-0005", quantity=2, key=key)
    check("R26 pedido creado antes de reiniciar el sistema", response.status_code == 201, response.text)
    with open(STATE_FILE, "w", encoding="utf8") as handle:
        json.dump({"orderId": response.json().get("id"), "key": key}, handle)


def after_restart():
    with open(STATE_FILE, encoding="utf8") as handle:
        state = json.load(handle)
    os.remove(STATE_FILE)
    api = client()
    check("R27 tras docker compose down y up (sin -v) el sistema vuelve a responder",
          Api().get("/products").status_code == 200)
    response = api.get(f"/orders/{state['orderId']}")
    check("R28 el pedido creado antes del reinicio sigue en MongoDB", response.status_code == 200, response.text)
    response = new_order(api, product="p-0005", quantity=2, key=state["key"])
    check("R29 la misma Idempotency-Key después del reinicio devuelve el pedido original sin duplicarlo",
          response.status_code == 200 and response.json()["id"] == state["orderId"], response.text)


def main():
    phase = sys.argv[1] if len(sys.argv) > 1 else ""
    if phase not in PHASES:
        print("Uso: python resilience.py <" + "|".join(PHASES) + ">")
        sys.exit(2)
    print(f"== Fase {phase}")
    {"bordes": edges, "sin-inventory": without_inventory, "sin-payments": without_payments, "sin-auth": without_auth,
     "recuperado": recovered, "antes-reinicio": before_restart, "tras-reinicio": after_restart}[phase]()
    if failures:
        print(f"{len(failures)} comprobaciones fallaron: {failures}")
        sys.exit(1)


if __name__ == "__main__":
    main()
