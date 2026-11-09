import os
import random
import re
import ssl
import time
import uuid

import httpx

CA = ssl.create_default_context(cafile=os.environ["CA_FILE"])
GATEWAY = os.environ["GATEWAY_URL"]
MAILPIT = os.environ.get("MAILPIT_URL", "http://mailpit:8025")
ADMIN_EMAIL = os.environ["BOOTSTRAP_ADMIN_EMAIL"]
ADMIN_PASSWORD = os.environ["BOOTSTRAP_ADMIN_PASSWORD"]
ADMIN_PASSWORD_AFTER = ADMIN_PASSWORD + "Aa#1"
STAFF_PASSWORD = "Staff#2026ok"
CLIENT_PASSWORD = "Cliente#2026ok"


def make_run() -> str:
    digits = str(random.randint(10_000_000, 24_999_999))
    total = sum(int(d) * f for d, f in zip(reversed(digits), [2, 3, 4, 5, 6, 7] * 2))
    remainder = 11 - total % 11
    dv = "0" if remainder == 11 else "K" if remainder == 10 else str(remainder)
    return f"{digits[:-6]}.{digits[-6:-3]}.{digits[-3:]}-{dv}"


class Api:
    def __init__(self):
        self.http = httpx.Client(base_url=GATEWAY, verify=CA, timeout=20)
        self.csrf = None

    def login(self, email: str, password: str):
        response = self.http.post("/api/v1/auth/login", json={"email": email, "password": password})
        if response.status_code == 200:
            self.csrf = response.json()["csrfToken"]
        return response

    def call(self, method: str, path: str, **kwargs):
        headers = kwargs.pop("headers", {})
        if self.csrf and method not in ("GET", "HEAD"):
            headers["X-CSRF-Token"] = self.csrf
        return self.http.request(method, "/api/v1" + path, headers=headers, **kwargs)

    def get(self, path, **kw):
        return self.call("GET", path, **kw)

    def post(self, path, **kw):
        return self.call("POST", path, **kw)

    def put(self, path, **kw):
        return self.call("PUT", path, **kw)

    def delete(self, path, **kw):
        return self.call("DELETE", path, **kw)

    def graphql(self, query: str, variables=None, operation=None):
        name = operation or (re.search(r"(?:query|mutation)\s+(\w+)", query) or [None, None])[1]
        body = {"query": query, "variables": variables or {}}
        if name:
            body["operationName"] = name
        return self.post("/graphql", json=body)


def wait_mail(to: str, subject_contains: str, timeout: float = 30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        listing = httpx.get(f"{MAILPIT}/api/v1/messages", timeout=10).json().get("messages", [])
        for message in listing:
            recipients = [r.get("Address", "").lower() for r in message.get("To", [])]
            if to.lower() in recipients and subject_contains.lower() in message.get("Subject", "").lower():
                return httpx.get(f"{MAILPIT}/api/v1/message/{message['ID']}", timeout=10).json().get("Text", "")
        time.sleep(1)
    return None


def register_client(comuna: str = "Maipú"):
    api = Api()
    email = f"cli-{uuid.uuid4().hex[:8]}@example.cl"
    verification = api.post("/clients/email-verifications", json={"email": email})
    assert verification.status_code == 200 and verification.json()["valid"], verification.text
    created = api.post("/clients", json={
        "run": make_run(), "name": "Cliente de Prueba", "address": "Av. Pajaritos 3450", "comuna": comuna,
        "provincia": "Santiago", "region": "Metropolitana", "birthDate": "1996-04-12", "sex": "F", "email": email,
        "phone": "+569" + str(random.randint(10_000_000, 99_999_999)), "password": CLIENT_PASSWORD,
        "verificationId": verification.json()["verificationId"]})
    assert created.status_code == 201, created.text
    assert api.login(email, CLIENT_PASSWORD).status_code == 200
    return api, email, created.json()["id"]


def admin_api() -> Api:
    api = Api()
    response = api.login(ADMIN_EMAIL, ADMIN_PASSWORD)
    current = ADMIN_PASSWORD
    if response.status_code != 200:
        response = api.login(ADMIN_EMAIL, ADMIN_PASSWORD_AFTER)
        current = ADMIN_PASSWORD_AFTER
    assert response.status_code == 200, "No se pudo ingresar como administrador: " + response.text
    if response.json()["mustChangePassword"]:
        changed = api.post("/auth/password", json={"currentPassword": current, "newPassword": ADMIN_PASSWORD_AFTER})
        assert changed.status_code == 204, changed.text
    return api


def staff_api(admin: Api, role: str):
    email = f"{role}-{uuid.uuid4().hex[:8]}@example.cl"
    created = admin.post("/users", json={"name": f"Personal {role}", "email": email, "role": role})
    assert created.status_code == 201, created.text
    text = wait_mail(email, "clave temporal")
    assert text, "No llegó el correo con la clave temporal"
    temporary = re.search(r"Tmp-[A-Za-z0-9_-]+", text).group(0)
    api = Api()
    first = api.login(email, temporary)
    assert first.status_code == 200 and first.json()["mustChangePassword"] is True
    changed = api.post("/auth/password", json={"currentPassword": temporary, "newPassword": STAFF_PASSWORD})
    assert changed.status_code == 204, changed.text
    return api, created.json()["id"]
