import json
import os
import re
import uuid
from datetime import date, timedelta

import httpx

from helpers import Api, CA, CLIENT_PASSWORD, admin_api, make_run, register_client, staff_api

SIM_URL = os.environ["SIM_URL"]
SIM_HEADERS = {"X-Service-Credential": os.environ["SIM_CONTROL_KEY"]}
OUTPUT = os.environ.get("CONTRACTS_FILE", "/evidencias/contratos.json")
examples = []


def trim(value, items=2):
    if isinstance(value, list):
        return [trim(v, items) for v in value[:items]]
    if isinstance(value, dict):
        return {k: trim(v, items) for k, v in value.items()}
    if isinstance(value, str) and len(value) > 60 and looks_like_token(value):
        return value[:12] + "…"
    return value


def looks_like_token(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{40,}", value))


def record(service, api, method, path, body=None, params=None, headers=None):
    response = api.call(method, path, json=body, params=params, headers=dict(headers or {}))
    try:
        payload = response.json()
    except ValueError:
        payload = None
    shown_headers = {k: v for k, v in (headers or {}).items()}
    examples.append({"id": service, "method": method, "path": "/api/v1" + path, "params": params, "headers": shown_headers,
                     "request": body, "status": response.status_code, "response": trim(payload)})
    return response


def main():
    anon = Api()
    record("S10", anon, "GET", "/products", params={"category": "horno"})
    email = f"ana.perez-{uuid.uuid4().hex[:6]}@example.cl"
    verification = record("S05", anon, "POST", "/clients/email-verifications", {"email": email}).json()
    run = make_run()
    created = record("S04", anon, "POST", "/clients", {
        "run": run, "name": "Ana Pérez Soto", "address": "Av. Pajaritos 1200", "comuna": "Maipú", "provincia": "Santiago",
        "region": "Metropolitana", "birthDate": "1994-03-21", "sex": "F", "email": email, "phone": "+56987654321",
        "password": CLIENT_PASSWORD, "verificationId": verification["verificationId"]}).json()
    client = Api()
    record("S01", client, "POST", "/auth/login", {"email": email, "password": CLIENT_PASSWORD})
    client.csrf = client.http.get("/api/v1/auth/session").json()["csrfToken"]
    record("S03", client, "GET", "/auth/session")
    record("S32", client, "GET", f"/clients/{created['id']}")
    record("S33", client, "PUT", f"/clients/{created['id']}", {
        "name": "Ana Pérez Soto", "address": "Av. 5 de Abril 450", "comuna": "Maipú", "provincia": "Santiago",
        "region": "Metropolitana", "phone": "+56987654321"})
    order = record("S15", client, "POST", "/orders", {"items": [{"productId": "p-0001", "quantity": 2}, {"productId": "p-0004", "quantity": 1}],
                                                       "paymentMethod": "deposito"}, headers={"Idempotency-Key": uuid.uuid4().hex}).json()
    record("S16", client, "GET", "/orders/mine", params={"size": "2"})
    record("S17", client, "GET", f"/orders/{order['id']}")
    record("GQL", client, "POST", "/graphql", {
        "operationName": "Resumen",
        "query": "query Resumen($id: ID!) { resumenPedido(orderId: $id) { pedido { id estado total items { nombre cantidad precioUnitario "
                 "producto { descripcion disponible } } } medioPago pago { estado boletaDisponible } historial { estado en } direccionEntrega } }",
        "variables": {"id": order["id"]}})

    admin = admin_api()
    cashier, _ = staff_api(admin, "cajero")
    dispatcher, _ = staff_api(admin, "despacho")
    record("S19", cashier, "GET", "/orders", params={"status": "pendiente_pago", "size": "2"})
    record("S20", cashier, "POST", f"/payments/{order['id']}/confirmation", {"reference": "DEP-2026-" + uuid.uuid4().hex[:6].upper()})
    record("S22", client, "GET", f"/orders/{order['id']}/receipt")
    record("S23", dispatcher, "POST", f"/orders/{order['id']}/transitions", {"to": "en_preparacion"})

    servipag = client.post("/orders", headers={"Idempotency-Key": uuid.uuid4().hex},
                           json={"items": [{"productId": "p-0002", "quantity": 3}], "paymentMethod": "servipag"}).json()
    with httpx.Client(base_url=SIM_URL, verify=CA, timeout=15) as sim:
        event = sim.post("/v1/simulate/payment", headers=SIM_HEADERS, json={"orderRef": servipag["id"]}).json()
    examples.append({"id": "S21", "method": "POST", "path": "/api/v1/payments/webhooks/servipag", "params": None,
                     "headers": {"X-Signature": "<HMAC-SHA256 hex>", "X-Timestamp": "<epoch s>"},
                     "request": {"eventId": event["eventId"], "orderRef": servipag["id"], "amount": servipag["total"], "status": "approved"},
                     "status": event["gatewayStatus"], "response": event["gatewayBody"]})
    record("S18", client, "POST", f"/orders/{servipag['id']}/cancellation", {"reason": "Solicitud del cliente"})

    record("S06", admin, "GET", "/clients", params={"q": "ana.perez", "size": "2"})
    record("S07", admin, "GET", "/users")
    user = record("S08", admin, "POST", "/users", {"name": "Rosa Tapia", "email": f"rosa-{uuid.uuid4().hex[:6]}@lachilena.cl",
                                                   "role": "despacho"}).json()
    record("S31", admin, "PUT", f"/users/{user['id']}", {"name": "Rosa Tapia", "role": "cajero", "active": True})
    record("S09", admin, "DELETE", f"/users/{user['id']}")
    record("S11", admin, "GET", "/admin/products", params={"q": "pino"})
    product = record("S12", admin, "POST", "/admin/products", {"name": "Empanada de pollo " + uuid.uuid4().hex[:4], "category": "horno",
                                                               "price": 2600, "description": "Pollo, pimentón y cebolla."}).json()
    record("S30", admin, "PUT", f"/admin/products/{product['id']}", {"name": product["name"], "category": "horno", "price": 2700,
                                                                     "description": "Pollo, pimentón, cebolla y aceituna."})
    record("S13", admin, "PUT", f"/admin/products/{product['id']}/availability", {"available": False})
    today = date.today()
    promo = record("S26", admin, "POST", "/admin/promotions", {
        "name": "Martes de pollo", "description": "Descuento para clientes registrados.", "productId": product["id"],
        "discountPercent": 20, "audience": "registrados", "startsAt": today.isoformat(),
        "endsAt": (today + timedelta(days=7)).isoformat()}).json()
    record("S25", admin, "GET", "/admin/promotions", params={"q": "pollo"})
    record("S27", admin, "PUT", f"/admin/promotions/{promo['id']}/active", {"active": False})
    record("S28", admin, "DELETE", f"/admin/promotions/{promo['id']}")
    record("S14", admin, "DELETE", f"/admin/products/{product['id']}")
    record("S24", admin, "GET", "/reports/sales", params={"from": today.isoformat(), "to": today.isoformat(), "size": "2"})

    record("S29", client, "POST", "/auth/password", {"currentPassword": CLIENT_PASSWORD, "newPassword": "NuevaClave#2026"})
    record("S02", client, "POST", "/auth/logout")

    order_of = {e["id"]: i for i, e in enumerate(examples)}
    ordered = sorted(examples, key=lambda e: (e["id"] == "GQL", int(e["id"][1:]) if e["id"] != "GQL" else 0, order_of[e["id"]]))
    with open(OUTPUT, "w", encoding="utf-8") as handle:
        json.dump(ordered, handle, ensure_ascii=False, indent=2)
    print(f"{len(ordered)} contratos registrados en {OUTPUT}")
    for e in ordered:
        print(f"{e['id']:4} {e['method']:6} {e['path']:45} -> {e['status']}")


if __name__ == "__main__":
    main()
