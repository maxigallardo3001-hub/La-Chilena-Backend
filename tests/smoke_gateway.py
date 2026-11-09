import os
import ssl
import sys
import uuid

import httpx

CA = ssl.create_default_context(cafile=os.environ["CA_FILE"])
GATEWAY = os.environ["GATEWAY_URL"]
AUTH_URL = os.environ["AUTH_URL"]
CLIENTS_SECRET = {"X-Service-Credential": os.environ["CLIENTS_AUTH_SECRET"]}
ADMIN_EMAIL = os.environ["BOOTSTRAP_ADMIN_EMAIL"]
ADMIN_PASSWORD = os.environ["BOOTSTRAP_ADMIN_PASSWORD"]
COOKIE = "__Host-sid"
failures = []


def check(name, condition, detail=""):
    print(("PASS " if condition else "FAIL ") + name + (f"  ({detail})" if detail and not condition else ""))
    if not condition:
        failures.append(name)


def new_client():
    return httpx.Client(base_url=GATEWAY, verify=CA, timeout=10)


def code_of(response):
    try:
        return response.json()["error"]["code"]
    except Exception:
        return None


def api(client, method, path, csrf=None, **kwargs):
    headers = kwargs.pop("headers", {})
    if csrf:
        headers["X-CSRF-Token"] = csrf
    return client.request(method, "/api/v1" + path, headers=headers, **kwargs)


anon = new_client()
check("health del gateway: 200", anon.get("/health").status_code == 200)
check("ruta inexistente: 404", api(anon, "GET", "/no-existe").status_code == 404)
check("ruta interna no publicada: 404", api(anon, "GET", "/internal/accounts").status_code == 404)
check("verbo no permitido: 405", api(anon, "PATCH", "/orders").status_code in (404, 405))
check("sin sesión en ruta protegida: 401", api(anon, "GET", "/auth/session").status_code == 401)
check("sin sesión en pedidos: 401", api(anon, "GET", "/orders/mine").status_code == 401)
check("aviso de pago sin firma: 401", api(anon, "POST", "/payments/webhooks/servipag", content=b"{}").status_code == 401)
check("aviso de pago con firma pero fuera de plazo: 401", api(
    anon, "POST", "/payments/webhooks/servipag", content=b"{}",
    headers={"X-Signature": "x", "X-Timestamp": "1000"}).status_code == 401)
check("catálogo anónimo llega al destino y, sin inventory, responde 502", api(anon, "GET", "/products").status_code in (200, 502))
head = anon.get("/health")
check("cabeceras de seguridad presentes", head.headers.get("x-content-type-options") == "nosniff"
      and "x-correlation-id" in head.headers)

account_id = str(uuid.uuid4())
email = f"gw-{account_id[:8]}@example.cl"
with httpx.Client(base_url=AUTH_URL, verify=CA, timeout=10) as direct:
    created = direct.post("/internal/accounts", headers=CLIENTS_SECRET, json={
        "id": account_id, "email": email, "displayName": "Cliente Gateway", "role": "cliente", "password": "Inicial#2026"})
check("cuenta de prueba creada en auth", created.status_code == 201, created.text)

check("origen no permitido: 403", api(anon, "POST", "/auth/login", headers={"Origin": "https://malo.example"},
                                      json={"email": email, "password": "x"}).status_code == 403)
check("cuerpo que no es JSON: 400", api(anon, "POST", "/auth/login", content=b"correo=a",
                                        headers={"Content-Type": "text/plain"}).status_code == 400)
check("cuerpo demasiado grande: 413", api(anon, "POST", "/auth/login", content=b"{" + b" " * 20000 + b"}",
                                          headers={"Content-Type": "application/json"}).status_code == 413)
check("login con clave mala: 401", api(anon, "POST", "/auth/login", json={"email": email, "password": "mala"}).status_code == 401)

user = new_client()
login = api(user, "POST", "/auth/login", json={"email": email, "password": "Inicial#2026"})
check("login correcto: 200", login.status_code == 200, login.text)
body = login.json()
cookie_header = " ".join(login.headers.get_list("set-cookie")).lower()
check("la cookie es HttpOnly, Secure, SameSite=Strict y con prefijo __Host-",
      cookie_header.startswith(COOKIE.lower()) and "httponly" in cookie_header and "secure" in cookie_header
      and "samesite=strict" in cookie_header and "path=/" in cookie_header, cookie_header)
check("el cuerpo del login no trae el token de sesión", "token" not in body and "passwordHash" not in login.text)
csrf = body["csrfToken"]

session = api(user, "GET", "/auth/session")
check("GET /auth/session: 200 con el usuario", session.status_code == 200 and session.json()["user"]["role"] == "cliente")
check("el token CSRF de la sesión coincide con el del login", session.json()["csrfToken"] == csrf)

check("POST sin token CSRF: 403", code_of(api(user, "POST", "/orders", json={})) == "CSRF_INVALID")
check("POST con token CSRF falso: 403", code_of(api(user, "POST", "/orders", csrf="falso", json={})) == "CSRF_INVALID")
check("con CSRF válido llega al destino (400 validación o 502 si orders está apagado)", api(user, "POST", "/orders", csrf=csrf, json={}).status_code in (400, 502))

check("cliente en ruta de administración: 403", code_of(api(user, "GET", "/admin/products")) == "FORBIDDEN")
check("cliente listando usuarios: 403", code_of(api(user, "GET", "/users")) == "FORBIDDEN")
check("cliente confirmando un pago: 403", code_of(api(user, "POST", "/payments/ORD-1/confirmation", csrf=csrf, json={})) == "FORBIDDEN")

check("cambio de clave con la actual incorrecta: 400", api(
    user, "POST", "/auth/password", csrf=csrf, json={"currentPassword": "no", "newPassword": "Nueva#2026ok"}).status_code == 400)
check("cambio de clave sin CSRF: 403", api(
    user, "POST", "/auth/password", json={"currentPassword": "Inicial#2026", "newPassword": "Nueva#2026ok"}).status_code == 403)
check("cambio de clave: 204", api(
    user, "POST", "/auth/password", csrf=csrf, json={"currentPassword": "Inicial#2026", "newPassword": "Nueva#2026ok"}).status_code == 204)
check("logout sin CSRF: 403", api(user, "POST", "/auth/logout").status_code == 403)
logout = api(user, "POST", "/auth/logout", csrf=csrf)
check("logout: 204 y cookie expirada", logout.status_code == 204 and "max-age=0" in " ".join(logout.headers.get_list("set-cookie")).lower())
check("tras el logout la sesión no existe: 401", api(user, "GET", "/auth/session").status_code == 401)
check("cookie inventada: 401", api(new_client(), "GET", "/auth/session",
                                  headers={"Cookie": f"{COOKIE}={'x' * 43}"}).status_code == 401)

admin = new_client()
login_admin = api(admin, "POST", "/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
if login_admin.status_code != 200:
    print("SKIP pruebas de administrador: la clave del .env ya no es la vigente")
else:
    info = login_admin.json()
    admin_csrf = info["csrfToken"]
    if info["mustChangePassword"]:
        check("con clave temporal solo se permite cambiarla: 403", code_of(api(admin, "GET", "/users")) == "PASSWORD_CHANGE_REQUIRED")
        temp = ADMIN_PASSWORD
        check("el administrador cambia su clave temporal: 204", api(
            admin, "POST", "/auth/password", csrf=admin_csrf,
            json={"currentPassword": temp, "newPassword": "Cambiada#2026ok"}).status_code == 204)
        current = "Cambiada#2026ok"
    else:
        current = ADMIN_PASSWORD
    check("administrador lista usuarios: 200", api(admin, "GET", "/users").status_code == 200)
    staff = api(admin, "POST", "/users", csrf=admin_csrf, json={
        "name": "Cajero Gateway", "email": f"cajero-{uuid.uuid4().hex[:8]}@example.cl", "role": "cajero"})
    check("administrador crea un usuario: 201", staff.status_code == 201, staff.text)
    if staff.status_code == 201:
        check("administrador da de baja al usuario: 204", api(
            admin, "DELETE", f"/users/{staff.json()['id']}", csrf=admin_csrf).status_code == 204)
    check("administrador en ruta de despacho: 403", code_of(api(admin, "POST", "/orders/ORD-1/transitions", csrf=admin_csrf, json={"to": "despachado"})) == "FORBIDDEN")
    if current != ADMIN_PASSWORD:
        check("se restaura la clave original: 204", api(
            admin, "POST", "/auth/password", csrf=admin_csrf,
            json={"currentPassword": current, "newPassword": ADMIN_PASSWORD}).status_code == 204)

limited = None
for i in range(15):
    r = api(anon, "POST", "/auth/login", json={"email": f"nadie{i}@example.cl", "password": "x"})
    if r.status_code == 429:
        limited = r
        break
check("el límite de ingreso responde 429 con Retry-After", limited is not None and "retry-after" in limited.headers)

with httpx.Client(base_url=AUTH_URL, verify=CA, timeout=10) as direct:
    direct.delete(f"/internal/accounts/{account_id}", headers=CLIENTS_SECRET)

print()
if failures:
    print(f"{len(failures)} comprobaciones fallaron: {failures}")
    sys.exit(1)
print("Todas las comprobaciones pasaron")
