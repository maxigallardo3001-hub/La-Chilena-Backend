import os
import ssl
import sys
import uuid

import httpx

AUTH_URL = os.environ["AUTH_URL"]
GATEWAY = {"X-Service-Credential": os.environ["GW_AUTH_SECRET"]}
CLIENTS = {"X-Service-Credential": os.environ["CLIENTS_AUTH_SECRET"]}
ADMIN = {**GATEWAY, "X-Authenticated-User": "admin-de-prueba", "X-Authenticated-Roles": "administrador"}
CASHIER = {**GATEWAY, "X-Authenticated-User": "cajero-de-prueba", "X-Authenticated-Roles": "cajero"}

client = httpx.Client(base_url=AUTH_URL, verify=ssl.create_default_context(cafile=os.environ["CA_FILE"]), timeout=10)
failures = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(("PASS " if condition else "FAIL ") + name + (f"  ({detail})" if detail and not condition else ""))
    if not condition:
        failures.append(name)


def make_account(password: str = "Inicial#2026"):
    account_id = str(uuid.uuid4())
    email = f"prueba-{account_id[:8]}@example.cl"
    response = client.post("/internal/accounts", headers=CLIENTS, json={
        "id": account_id, "email": email, "displayName": "Cliente de Prueba", "role": "cliente", "password": password})
    return account_id, email, response


check("health responde 200", client.get("/health").status_code == 200)
check("login sin credencial de servicio: 403", client.post("/login", json={"email": "a@b.cl", "password": "x"}).status_code == 403)
check("login con credencial incorrecta: 403", client.post(
    "/login", headers={"X-Service-Credential": "incorrecta"}, json={"email": "a@b.cl", "password": "x"}).status_code == 403)
check("la credencial de clients no sirve en /login: 403", client.post(
    "/login", headers=CLIENTS, json={"email": "a@b.cl", "password": "x"}).status_code == 403)

account_id, email, created = make_account()
check("crear cuenta interna: 201", created.status_code == 201, created.text)
check("crear cuenta con la credencial del gateway: 403", client.post(
    "/internal/accounts", headers=GATEWAY, json={"id": str(uuid.uuid4()), "email": "x@example.cl",
                                                   "displayName": "X Y", "role": "cliente"}).status_code == 403)
check("correo repetido: 409", client.post("/internal/accounts", headers=CLIENTS, json={
    "id": str(uuid.uuid4()), "email": email, "displayName": "Otro", "role": "cliente", "password": "Otra#2026a"}).status_code == 409)

bad = client.post("/login", headers=GATEWAY, json={"email": email, "password": "mala"})
check("login con clave mala: 401", bad.status_code == 401)
check("el error no incluye detalles internos", set(bad.json()["error"].keys()) >= {"code", "message", "correlationId"})

ok = client.post("/login", headers=GATEWAY, json={"email": email, "password": "Inicial#2026"})
check("login correcto: 200", ok.status_code == 200, ok.text)
body = ok.json()
token, csrf = body["token"], body["csrfToken"]
check("la respuesta no trae el hash", "passwordHash" not in ok.text)
check("rol cliente y sin cambio obligatorio", body["user"]["role"] == "cliente" and body["mustChangePassword"] is False)

intro = client.post("/introspect", headers=GATEWAY, json={"token": token, "csrf": csrf}).json()
check("introspección activa con CSRF válido", intro.get("active") is True and intro.get("csrfValid") is True)
bad_csrf = client.post("/introspect", headers=GATEWAY, json={"token": token, "csrf": "falso"}).json()
check("introspección con CSRF falso: csrfValid=false", bad_csrf.get("csrfValid") is False)
check("token desconocido: active=false", client.post("/introspect", headers=GATEWAY, json={"token": "x" * 30}).json() == {"active": False})

short = client.post("/password", headers=GATEWAY, json={"token": token, "currentPassword": "Inicial#2026", "newPassword": "corta"})
check("clave nueva corta: 400", short.status_code == 400)
same = client.post("/password", headers=GATEWAY, json={"token": token, "currentPassword": "Inicial#2026", "newPassword": "Inicial#2026"})
check("clave nueva igual a la actual: 400", same.status_code == 400)
wrong = client.post("/password", headers=GATEWAY, json={"token": token, "currentPassword": "no-es", "newPassword": "Nueva#2026ok"})
check("clave actual incorrecta: 400", wrong.status_code == 400)
changed = client.post("/password", headers=GATEWAY, json={"token": token, "currentPassword": "Inicial#2026", "newPassword": "Nueva#2026ok"})
check("cambio de clave: 204", changed.status_code == 204, changed.text)
check("la clave anterior ya no sirve: 401", client.post("/login", headers=GATEWAY, json={"email": email, "password": "Inicial#2026"}).status_code == 401)
check("la clave nueva sirve: 200", client.post("/login", headers=GATEWAY, json={"email": email, "password": "Nueva#2026ok"}).status_code == 200)

check("logout: 204", client.post("/logout", headers=GATEWAY, json={"token": token}).status_code == 204)
check("la sesión cerrada queda inactiva", client.post("/introspect", headers=GATEWAY, json={"token": token}).json() == {"active": False})

lock_id, lock_email, _ = make_account()
codes = [client.post("/login", headers=GATEWAY, json={"email": lock_email, "password": "mala"}).status_code for _ in range(6)]
check("tras 5 intentos fallidos responde 429", codes[-1] == 429, str(codes))
check("con la clave correcta sigue bloqueado: 429", client.post(
    "/login", headers=GATEWAY, json={"email": lock_email, "password": "Inicial#2026"}).status_code == 429)

staff_email = f"cajero-{uuid.uuid4().hex[:8]}@example.cl"
check("listar usuarios sin identidad: 401", client.get("/users", headers=GATEWAY).status_code == 401)
check("listar usuarios con rol cajero: 403", client.get("/users", headers=CASHIER).status_code == 403)
check("listar usuarios como administrador: 200", client.get("/users", headers=ADMIN).status_code == 200)
created_staff = client.post("/users", headers=ADMIN, json={"name": "Cajero Prueba", "email": staff_email, "role": "cajero"})
check("crear usuario: 201", created_staff.status_code == 201, created_staff.text)
check("crear usuario con rol inválido: 400", client.post(
    "/users", headers=ADMIN, json={"name": "Raro", "email": "raro@example.cl", "role": "superusuario"}).status_code == 400)
check("crear usuario con correo repetido: 409", client.post(
    "/users", headers=ADMIN, json={"name": "Cajero Prueba", "email": staff_email, "role": "cajero"}).status_code == 409)
check("la respuesta no incluye credenciales", "passwordHash" not in created_staff.text and "Tmp-" not in created_staff.text)
staff_id = created_staff.json()["id"]
second_admin = client.post("/users", headers=ADMIN, json={
    "name": "Admin Prueba", "email": f"admin-{uuid.uuid4().hex[:8]}@example.cl", "role": "administrador"}).json()["id"]
self_headers = {**GATEWAY, "X-Authenticated-User": second_admin, "X-Authenticated-Roles": "administrador"}
check("un administrador no puede darse de baja a sí mismo: 409", client.delete(
    f"/users/{second_admin}", headers=self_headers).status_code == 409)
check("dar de baja a otro administrador: 204", client.delete(f"/users/{second_admin}", headers=ADMIN).status_code == 204)
check("dar de baja un usuario: 204", client.delete(f"/users/{staff_id}", headers=ADMIN).status_code == 204)

for _id in (account_id, lock_id):
    client.delete(f"/internal/accounts/{_id}", headers=CLIENTS)

print()
if failures:
    print(f"{len(failures)} comprobaciones fallaron: {failures}")
    sys.exit(1)
print("Todas las comprobaciones pasaron")
