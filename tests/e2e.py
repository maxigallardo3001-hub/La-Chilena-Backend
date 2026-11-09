import os
import re
import sys
import uuid
from datetime import date, timedelta

import httpx

from helpers import Api, CA, CLIENT_PASSWORD, admin_api, register_client, staff_api, wait_mail

SIM_URL = os.environ["SIM_URL"]
SIM_HEADERS = {"X-Service-Credential": os.environ["SIM_CONTROL_KEY"]}
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


def place_order(api, items, method="deposito", key=None):
    return api.post("/orders", headers={"Idempotency-Key": key or uuid.uuid4().hex},
                    json={"items": [{"productId": p, "quantity": q} for p, q in items], "paymentMethod": method})


def simulate(order_id, **options):
    with httpx.Client(base_url=SIM_URL, verify=CA, timeout=15) as sim:
        return sim.post("/v1/simulate/payment", headers=SIM_HEADERS, json={"orderRef": order_id, **options}).json()


print("== Preparación")
admin = admin_api()
cashier, _ = staff_api(admin, "cajero")
dispatcher, _ = staff_api(admin, "despacho")
owner, _ = staff_api(admin, "dueno")
client_a, email_a, id_a = register_client()
client_b, email_b, id_b = register_client()
client_c, _, _ = register_client(comuna="Ñuñoa")
anon = Api()
print("Cuentas creadas por la API real; claves temporales leídas de Mailpit")

print("== V1 y V10: catálogo, promociones y precios finales")
catalog = anon.get("/products")
check("V1 catálogo anónimo: 200 con productos", catalog.status_code == 200 and len(catalog.json()["items"]) >= 8, catalog.text)
by_name = {p["name"]: p for p in catalog.json()["items"]}
check("el visitante ve la oferta para todos (queso 15 %: 1870)", by_name["Empanada de queso"]["finalPrice"] == 1870)
check("el visitante no ve la promoción exclusiva (carne 3500)", by_name["Empanada de carne"]["finalPrice"] == 3500)
with_client = {p["name"]: p for p in client_a.get("/products").json()["items"]}
check("el cliente con sesión ve la promoción exclusiva (carne 3150)", with_client["Empanada de carne"]["finalPrice"] == 3150)
check("filtro onOffer devuelve solo productos con promoción", {p["name"] for p in client_a.get("/products", params={"onOffer": "true"}).json()["items"]}
      == {"Empanada de queso", "Empanada de carne"})
check("filtro de categoría inválido: 400", anon.get("/products", params={"category": "pizza"}).status_code == 400)

print("== Administración de productos y promociones (S11 a S14, S25 a S28)")
name = "Empanada de prueba " + uuid.uuid4().hex[:6]
created = admin.post("/admin/products", json={"name": name, "category": "frita", "price": 3000, "description": "Prueba"})
check("administrador crea producto: 201", created.status_code == 201, created.text)
pid = created.json()["id"]
check("nombre repetido: 409", admin.post("/admin/products", json={"name": name, "category": "frita", "price": 3000}).status_code == 409)
check("dueño también administra productos: 200", owner.get("/admin/products").status_code == 200)
check("V3 cliente intenta administrar productos: 403", code(client_a.delete(f"/admin/products/{pid}")) == "FORBIDDEN")
renamed = admin.put(f"/admin/products/{pid}", json={"name": name + " editada", "category": "horno", "price": 3200, "description": "Editada"})
check("S30 modificar producto: 200 con los datos nuevos", renamed.status_code == 200 and renamed.json()["price"] == 3200
      and renamed.json()["category"] == "horno", renamed.text)
check("S30 modificar con nombre de otro producto: 409", admin.put(f"/admin/products/{pid}", json={
    "name": "Empanada de pino", "category": "horno", "price": 3200}).status_code == 409)
check("S30 cliente no modifica productos: 403", code(client_a.put(f"/admin/products/{pid}", json={
    "name": "X1", "category": "horno", "price": 1})) == "FORBIDDEN")
check("S30 precio no positivo: 400", admin.put(f"/admin/products/{pid}", json={"name": "X1", "category": "horno", "price": 0}).status_code == 400)
check("cambiar disponibilidad: 200", admin.put(f"/admin/products/{pid}/availability", json={"available": False}).json()["available"] is False)
today = date.today()
promo = admin.post("/admin/promotions", json={"name": "Prueba", "productId": pid, "discountPercent": 20, "audience": "todos",
                                              "startsAt": today.isoformat(), "endsAt": (today + timedelta(days=5)).isoformat()})
check("crear promoción: 201", promo.status_code == 201, promo.text)
check("promoción solapada del mismo producto: 409", admin.post("/admin/promotions", json={
    "name": "Otra", "productId": pid, "discountPercent": 10, "audience": "todos", "startsAt": today.isoformat(),
    "endsAt": (today + timedelta(days=2)).isoformat()}).status_code == 409)
check("fechas invertidas: 400", admin.post("/admin/promotions", json={
    "name": "X", "productId": pid, "discountPercent": 10, "audience": "todos", "startsAt": "2026-12-10", "endsAt": "2026-12-01"}).status_code == 400)
check("desactivar promoción: 200", admin.put(f"/admin/promotions/{promo.json()['id']}/active", json={"active": False}).status_code == 200)
second_promo = admin.post("/admin/promotions", json={"name": "Reemplazo", "productId": pid, "discountPercent": 5, "audience": "todos",
                                                     "startsAt": today.isoformat(), "endsAt": (today + timedelta(days=3)).isoformat()})
check("con la primera inactiva se puede crear otra promoción: 201", second_promo.status_code == 201, second_promo.text)
check("reactivar una promoción que se solapa: 409", code(admin.put(f"/admin/promotions/{promo.json()['id']}/active",
                                                               json={"active": True})) == "PROMOTION_OVERLAP")
check("eliminar promoción: 204", admin.delete(f"/admin/promotions/{promo.json()['id']}").status_code == 204)
check("eliminar producto: 204", admin.delete(f"/admin/products/{pid}").status_code == 204)

print("== Pedidos (S15 a S19)")
key = uuid.uuid4().hex
order = place_order(client_a, [("p-0001", 2), ("p-0007", 1)], key=key)
check("crear pedido: 201 pendiente_pago", order.status_code == 201 and order.json()["status"] == "pendiente_pago", order.text)
order_id = order.json()["id"]
check("el total lo calcula el servidor: 6800", order.json()["total"] == 6800)
check("misma Idempotency-Key devuelve el mismo pedido", place_order(client_a, [("p-0001", 2), ("p-0007", 1)], key=key).json()["id"] == order_id)
check("sin Idempotency-Key: 400", client_a.post("/orders", json={"items": [{"productId": "p-0001", "quantity": 1}], "paymentMethod": "deposito"}).status_code == 400)
tampered = client_a.post("/orders", headers={"Idempotency-Key": uuid.uuid4().hex}, json={
    "items": [{"productId": "p-0001", "quantity": 1, "unitPrice": 1}], "paymentMethod": "deposito", "total": 1, "status": "pagado"})
check("V6 precio, total y estado manipulados se ignoran", tampered.status_code == 201 and tampered.json()["total"] == 2500
      and tampered.json()["status"] == "pendiente_pago", tampered.text)
check("producto no disponible: 409 con la lista", code(place_order(client_a, [("p-0003", 1)])) == "PRODUCTS_UNAVAILABLE")
zone = place_order(client_c, [("p-0001", 1)])
check("V13 comuna sin reparto: 422 con comunas habilitadas", code(zone) == "DELIVERY_OUT_OF_ZONE" and "Maipú" in zone.json()["error"]["enabledComunas"], zone.text)
mine = client_a.get("/orders/mine", params={"status": "pendiente_pago"}).json()["items"]
check("S16 lista los pedidos propios", any(o["id"] == order_id and o["canCancel"] for o in mine))
check("V4 cliente B consulta el pedido de A: 403", code(client_b.get(f"/orders/{order_id}")) == "FORBIDDEN")
check("V4 el pedido ajeno no aparece en la lista de B", all(o["id"] != order_id for o in client_b.get("/orders/mine").json()["items"]))
check("el propietario ve su pedido con dirección", "deliveryAddress" in client_a.get(f"/orders/{order_id}").json())
check("administrador ve cualquier pedido", admin.get(f"/orders/{order_id}").status_code == 200)
check("cajero lista pedidos por pagar", any(o["id"] == order_id for o in cashier.get("/orders", params={"status": "pendiente_pago"}).json()["items"]))
check("cajero no ve pedidos en otro estado: 403", cashier.get("/orders", params={"status": "pagado"}).status_code == 403)
check("dueño no consulta pedidos individuales: 403", owner.get(f"/orders/{order_id}").status_code == 403)

print("== Pago por depósito y boleta (S20, S22)")
reference = "DEP-" + uuid.uuid4().hex[:8]
check("el cliente no puede confirmar un pago: 403", code(client_a.post(f"/payments/{order_id}/confirmation", json={"reference": reference})) == "FORBIDDEN")
paid = cashier.post(f"/payments/{order_id}/confirmation", json={"reference": reference, "status": "pagado", "amount": 1})
check("V7 el cajero confirma e ignora campos extra: 200 pagado con boleta", paid.status_code == 200 and paid.json()["status"] == "pagado"
      and paid.json()["receiptId"].startswith("BOL-"), paid.text)
check("repetir la confirmación es idempotente", cashier.post(f"/payments/{order_id}/confirmation", json={"reference": reference}).json()["receiptId"] == paid.json()["receiptId"])
check("otra referencia sobre un pago confirmado: 409", cashier.post(f"/payments/{order_id}/confirmation", json={"reference": reference + "X"}).status_code == 409)
check("el pedido quedó pagado", client_a.get(f"/orders/{order_id}").json()["status"] == "pagado")
receipt = client_a.get(f"/orders/{order_id}/receipt")
check("el propietario ve la boleta con neto + IVA = total", receipt.status_code == 200 and receipt.json()["net"] + receipt.json()["iva"] == receipt.json()["total"] == 6800
      and "run" not in receipt.text.lower(), receipt.text)
check("otro cliente no ve la boleta: 403", client_b.get(f"/orders/{order_id}/receipt").status_code == 403)
check("la boleta llega por correo", wait_mail(email_a, "boleta") is not None)
second = place_order(client_a, [("p-0002", 1)])
check("una referencia ya usada en otro pago: 409", code(cashier.post(f"/payments/{second.json()['id']}/confirmation", json={"reference": reference})) == "REFERENCE_REUSED")

print("== Pago con Servipag y aviso del proveedor (S21)")
pedido = place_order(client_a, [("p-0004", 2)], method="servipag")
servipag_id = pedido.json()["id"]
code_match = re.search(r"(\d{6})", pedido.json()["payment"]["instructions"])
check("el cobro de Servipag trae un código", pedido.status_code == 201 and code_match, pedido.text)
check("el total usa la promoción exclusiva: 2 x 3150", pedido.json()["total"] == 6300)
check("confirmar antes del aviso: 422", code(cashier.post(f"/payments/{servipag_id}/confirmation", json={"reference": code_match.group(1)})) == "REFERENCE_NOT_VERIFIED")
check("V8 aviso con firma inválida: 401", simulate(servipag_id, badSignature=True)["gatewayStatus"] == 401)
check("aviso con marca de tiempo vencida: 401", simulate(servipag_id, staleTimestamp=True)["gatewayStatus"] == 401)
check("aviso con monto distinto: 409", simulate(servipag_id, amount=1)["gatewayStatus"] == 409)
event = simulate(servipag_id, eventId="evt-" + uuid.uuid4().hex[:10])
check("aviso aprobado y firmado: 200", event["gatewayStatus"] == 200, event)
check("V9 el mismo aviso otra vez: 200 sin reprocesar", simulate(servipag_id, eventId=event["eventId"])["gatewayStatus"] == 200)
check("el aviso no cambia el estado del pedido", client_a.get(f"/orders/{servipag_id}").json()["status"] == "pendiente_pago")
confirmed = cashier.post(f"/payments/{servipag_id}/confirmation", json={"reference": code_match.group(1)})
check("con el aviso registrado el cajero confirma: 200", confirmed.status_code == 200, confirmed.text)

print("== Despacho (S19, S23)")
check("despacho ve el pedido pagado con dirección", any(o["id"] == order_id and "deliveryAddress" in o for o in dispatcher.get("/orders").json()["items"]))
check("el cajero no avanza estados: 403", cashier.post(f"/orders/{order_id}/transitions", json={"to": "en_preparacion"}).status_code == 403)
check("salto de estado de pagado a entregado: 409", code(dispatcher.post(f"/orders/{order_id}/transitions", json={"to": "entregado"})) == "INVALID_TRANSITION")
for target in ("en_preparacion", "despachado", "entregado"):
    check(f"despacho avanza a {target}", dispatcher.post(f"/orders/{order_id}/transitions", json={"to": target}).json().get("status") == target)
check("un pedido entregado no puede anularse: 409", code(client_a.post(f"/orders/{order_id}/cancellation", json={"reason": "Otro motivo"})) == "ORDER_NOT_CANCELLABLE")

print("== Anulación (S18)")
cancel_target = place_order(client_a, [("p-0001", 1)])
cancelled = client_a.post(f"/orders/{cancel_target.json()['id']}/cancellation", json={"reason": "Solicitud del cliente"})
check("el cliente anula su pedido: 200", cancelled.status_code == 200 and cancelled.json()["status"] == "anulado", cancelled.text)
check("anular dos veces: 409", client_a.post(f"/orders/{cancel_target.json()['id']}/cancellation", json={"reason": "Otro motivo"}).status_code == 409)
check("cliente B no anula pedidos ajenos: 403", client_b.post(f"/orders/{cancel_target.json()['id']}/cancellation", json={"reason": "Otro motivo"}).status_code == 403)
late = cashier.post(f"/payments/{cancel_target.json()['id']}/confirmation", json={"reference": "DEP-" + uuid.uuid4().hex[:8]})
check("pagar un pedido ya anulado: 409 con el cobro anulado", code(late) == "ORDER_CANCELLED", late.text)
voided = place_order(client_a, [("p-0001", 1)], method="servipag")
client_a.post(f"/orders/{voided.json()['id']}/cancellation", json={"reason": "Solicitud del cliente"})
check("aviso aprobado de un cobro anulado se recibe sin pagar el pedido", simulate(voided.json()["id"])["gatewayStatus"] == 200
      and client_a.get(f"/orders/{voided.json()['id']}").json()["status"] == "anulado")
check("el historial conserva la anulación", client_a.get(f"/orders/{cancel_target.json()['id']}").json()["history"][-1]["status"] == "anulado")
admin_target = place_order(client_a, [("p-0002", 1)])
cashier.post(f"/payments/{admin_target.json()['id']}/confirmation", json={"reference": "DEP-" + uuid.uuid4().hex[:8]})
dispatcher.post(f"/orders/{admin_target.json()['id']}/transitions", json={"to": "en_preparacion"})
check("el cliente no anula un pedido en preparación: 409", client_a.post(f"/orders/{admin_target.json()['id']}/cancellation", json={"reason": "Otro motivo"}).status_code == 409)
check("el administrador sí lo anula (CU7): 200", admin.post(f"/orders/{admin_target.json()['id']}/cancellation", json={"reason": "Error en el pedido"}).status_code == 200)

print("== Reporte de ventas (S24)")
report = admin.get("/reports/sales", params={"from": (today - timedelta(days=1)).isoformat(), "to": today.isoformat()})
check("administrador consulta el reporte: 200 con indicadores", report.status_code == 200 and report.json()["kpis"]["orders"] >= 2 and report.json()["kpis"]["cancelled"] >= 2, report.text)
check("el reporte no incluye direcciones ni RUN", "deliveryAddress" not in report.text and "run" not in report.text.lower())
check("el dueño consulta el reporte: 200", owner.get("/reports/sales", params={"from": today.isoformat(), "to": today.isoformat()}).status_code == 200)
check("el cajero no consulta el reporte: 403", cashier.get("/reports/sales", params={"from": today.isoformat(), "to": today.isoformat()}).status_code == 403)
check("rango mayor a 366 días: 400", admin.get("/reports/sales", params={"from": "2025-01-01", "to": today.isoformat()}).status_code == 400)

print("== Clientes y usuarios (S04 a S09)")
check("administrador lista clientes: 200 sin datos de nacimiento", admin.get("/clients", params={"q": email_a[:8]}).status_code == 200
      and "birthDate" not in admin.get("/clients").text)
check("cliente no lista clientes: 403", client_a.get("/clients").status_code == 403)
check("RUN inválido: 400", code(Api().post("/clients", json={"run": "12.345.678-9", "name": "Ana Pérez", "address": "Calle 1", "comuna": "Maipú",
      "provincia": "Santiago", "region": "Metropolitana", "birthDate": "1990-01-01", "sex": "F", "email": "x@example.cl",
      "phone": "+56912345678", "password": CLIENT_PASSWORD, "verificationId": "v-inexistente"})) == "INVALID_RUN")
check("correo sin verificar: 422", code(Api().post("/clients", json={"run": "12.345.678-5", "name": "Ana Pérez", "address": "Calle 1",
      "comuna": "Maipú", "provincia": "Santiago", "region": "Metropolitana", "birthDate": "1990-01-01", "sex": "F",
      "email": "x@example.cl", "phone": "+56912345678", "password": CLIENT_PASSWORD, "verificationId": "v-inexistente"})) == "EMAIL_NOT_VERIFIED")
check("correo inexistente según el validador: no se puede verificar", Api().post(
    "/clients/email-verifications", json={"email": "noexiste1@example.cl"}).json()["valid"] is False)
profile = client_a.get(f"/clients/{id_a}")
check("S32 el cliente consulta su ficha: 200 sin fecha de nacimiento ni sexo", profile.status_code == 200
      and profile.json()["email"] == email_a and "birthDate" not in profile.text and "sex" not in profile.json(), profile.text)
check("S32 cliente B no consulta la ficha de A: 403", code(client_b.get(f"/clients/{id_a}")) == "FORBIDDEN")
updated_profile = dict(name="Cliente Editado", address="Av. 5 de Abril 100", comuna="Santiago", provincia="Santiago",
                       region="Metropolitana", phone="+56987654321")
saved = client_a.put(f"/clients/{id_a}", json=updated_profile)
check("S33 el cliente modifica su dirección: 200", saved.status_code == 200 and saved.json()["comuna"] == "Santiago", saved.text)
check("S33 el nombre nuevo se refleja en la sesión", client_a.get("/auth/session").json()["user"]["name"] == "Cliente Editado")
check("S33 cliente B no modifica la ficha de A: 403", code(client_b.put(f"/clients/{id_a}", json=updated_profile)) == "FORBIDDEN")
check("S33 teléfono inválido: 400", client_a.put(f"/clients/{id_a}", json={**updated_profile, "phone": "123"}).status_code == 400)
check("S33 no permite cambiar RUN ni correo (campos ignorados)", client_a.put(f"/clients/{id_a}", json={
    **updated_profile, "run": "1.111.111-4", "email": "otro@example.cl"}).json()["email"] == email_a)
check("S32 el administrador consulta y modifica la ficha: 200", admin.get(f"/clients/{id_b}").status_code == 200
      and admin.put(f"/clients/{id_b}", json=updated_profile).status_code == 200)
check("el pedido ya creado conserva su dirección original", "Pajaritos" in client_a.get(f"/orders/{order_id}").json()["deliveryAddress"])
extra_cashier, cashier_id = staff_api(admin, "cajero")
promoted = admin.put(f"/users/{cashier_id}", json={"name": "Cajero Promovido", "role": "despacho", "active": True})
check("S31 el administrador cambia el perfil de un usuario: 200", promoted.status_code == 200 and promoted.json()["role"] == "despacho", promoted.text)
check("S31 el cambio de perfil cierra las sesiones del usuario: 401", extra_cashier.get("/orders", params={"status": "pendiente_pago"}).status_code == 401)
admin_id = admin.get("/auth/session").json()["user"]["id"]
check("S31 el administrador no puede quitarse su propio perfil: 409", code(admin.put(f"/users/{admin_id}", json={
    "name": "Yo", "role": "cajero", "active": True})) == "CANNOT_DEMOTE_SELF")
check("S31 un no administrador no edita usuarios: 403", code(owner.put(f"/users/{cashier_id}", json={
    "name": "X1", "role": "cajero", "active": True})) == "FORBIDDEN")
check("S31 reactivar una cuenta con PUT: 200", admin.put(f"/users/{cashier_id}", json={"name": "Cajero Promovido", "role": "cajero",
      "active": True}).status_code == 200)
check("V12 S06 y S17 no devuelven fecha de nacimiento, sexo ni contraseña", all(
    word not in (admin.get("/clients").text + client_a.get(f"/orders/{order_id}").text) for word in ("birthDate", "sex", "passwordHash")))

print("== GraphQL: agregación desde orders, inventory y payments (G1 a G10)")
LIST = "query MisPedidos($n: Int) { misPedidos(primeros: $n) { edges { node { id estado total puedeAnular items { nombre cantidad precioUnitario producto { descripcion disponible } } } } pageInfo { hasNextPage endCursor } } }"
listing = client_a.graphql(LIST, {"n": 5})
body = listing.json()
check("misPedidos combina orders e inventory", listing.status_code == 200 and body["data"]["misPedidos"]["edges"]
      and body["data"]["misPedidos"]["edges"][0]["node"]["items"][0]["producto"]["descripcion"], listing.text)
check("G1 sin sesión: 401", anon.graphql(LIST, {"n": 5}).status_code == 401)
no_csrf = Api(); no_csrf.http.cookies.update(client_a.http.cookies)
check("sin token CSRF: 403", no_csrf.graphql(LIST, {"n": 5}).status_code == 403)
check("G2 el GraphQL Gateway no responde sin credencial de servicio: 403", httpx.post(
    "https://graphql:8700/graphql", verify=CA, json={"query": "{ __typename }"}, timeout=10).status_code == 403)
detail = client_a.graphql("query Resumen($id: ID!) { resumenPedido(orderId: $id) { pedido { estado total } medioPago historial { estado en } direccionEntrega "
                          "pago { estado boletaDisponible } } }", {"id": order_id})
summary = detail.json()["data"]["resumenPedido"]
check("resumenPedido combina orders y payments", summary and summary["pago"]["estado"] == "CONFIRMADO" and summary["pago"]["boletaDisponible"]
      and summary["direccionEntrega"], detail.text)
foreign = client_b.graphql("query Resumen($id: ID!) { resumenPedido(orderId: $id) { pedido { total } } }", {"id": order_id}).json()
check("G3 pedido ajeno: FORBIDDEN y sin datos", foreign["data"]["resumenPedido"] is None and foreign["errors"][0]["extensions"]["code"] == "FORBIDDEN", foreign)
check("G4 pedir clientId: el campo no existe", client_a.graphql("query Q { misPedidos(primeros: 1) { edges { node { clientId } } } }").status_code == 400)
check("G4 pedir la referencia de pago: el campo no existe", client_a.graphql(
    "query Q($id: ID!) { resumenPedido(orderId: $id) { pago { reference } } }", {"id": order_id}).status_code == 400)
costly = client_a.graphql(LIST, {"n": 20}).json()
check("G6 consulta de costo superior al máximo: QUERY_TOO_COSTLY", costly["errors"][0]["extensions"]["code"] == "QUERY_TOO_COSTLY", costly)
check("G6 primeros fuera de rango: BAD_USER_INPUT", client_a.graphql(LIST, {"n": 500}).json()["errors"][0]["extensions"]["code"] in ("QUERY_TOO_COSTLY", "BAD_USER_INPUT"))
check("consulta sin operationName: rechazada", client_a.post("/graphql", json={"query": "{ misPedidos(primeros: 1) { edges { cursor } } }"}).json().get("errors"))
check("consulta de más de 4 KB: 413", client_a.post("/graphql", json={"query": "{ " + "x" * 5000 + " }", "operationName": "A"}).status_code == 413)
check("G9 despacho no ve un pedido en pendiente_pago: FORBIDDEN", dispatcher.graphql(
    "query Resumen($id: ID!) { resumenPedido(orderId: $id) { pedido { total } } }", {"id": tampered.json()["id"]}).json()["errors"][0]["extensions"]["code"] == "FORBIDDEN")
check("G10 el cajero pide la dirección: FORBIDDEN con el campo en null", (lambda r: r["data"]["resumenPedido"]["direccionEntrega"] is None
      and r["errors"][0]["extensions"]["code"] == "FORBIDDEN")(cashier.graphql(
          "query Resumen($id: ID!) { resumenPedido(orderId: $id) { direccionEntrega pedido { total } } }", {"id": tampered.json()["id"]}).json()))

print("== V5: acceso directo a los servicios internos (desde la red privada)")
for service, port, path in [("orders", 8400, "/orders/mine"), ("inventory", 8300, "/admin/products"),
                            ("clients", 8200, "/clients"), ("payments", 8500, "/receipts/x"), ("auth", 8100, "/users")]:
    direct = httpx.get(f"https://{service}:{port}{path}", verify=CA, timeout=10)
    wrong = httpx.get(f"https://{service}:{port}{path}", verify=CA, timeout=10, headers={"X-Service-Credential": "incorrecta",
                      "X-Authenticated-User": "admin", "X-Authenticated-Roles": "administrador"})
    check(f"{service}: sin credencial de servicio 403 y con una incorrecta 403", direct.status_code == 403 and wrong.status_code == 403,
          (direct.status_code, wrong.status_code))
gql_orders = {"X-Service-Credential": os.environ["GQL_ORDERS_SECRET"], "X-Authenticated-User": id_a,
              "X-Authenticated-Roles": "cliente", "Idempotency-Key": uuid.uuid4().hex}
check("V16 la credencial del GraphQL Gateway lee pedidos pero no puede crearlos (solo lectura)",
      httpx.get("https://orders:8400/orders/mine", verify=CA, timeout=10, headers=gql_orders).status_code == 200
      and httpx.post("https://orders:8400/orders", verify=CA, timeout=10, headers=gql_orders, json={
          "items": [{"productId": "p-0001", "quantity": 1}], "paymentMethod": "deposito"}).status_code == 403)
check("V16 la credencial del GraphQL Gateway no lee boletas directamente", httpx.get(
    f"https://payments:8500/receipts/{order_id}", verify=CA, timeout=10, headers={
        "X-Service-Credential": os.environ["GQL_PAYMENTS_SECRET"], "X-Authenticated-User": id_a,
        "X-Authenticated-Roles": "cliente"}).status_code == 403)
spoof = client_a.get("/orders/mine", headers={"X-Authenticated-User": id_b, "X-Authenticated-Roles": "administrador"})
check("cabeceras de identidad enviadas por el navegador se descartan", spoof.status_code == 200
      and spoof.json() == client_a.get("/orders/mine").json())

print()
if failures:
    print(f"{len(failures)} comprobaciones fallaron:")
    for name in failures:
        print(" -", name)
    sys.exit(1)
print("Todas las comprobaciones pasaron")
