import re
from dataclasses import dataclass

ALL_STAFF = ("cajero", "despacho", "administrador", "dueno")


@dataclass(frozen=True)
class Route:
    id: str
    method: str
    path: str
    target: str
    internal: str
    kind: str
    roles: tuple = ()
    csrf: bool = False
    policy: str = "default"
    max_body: int = 16384
    allow_must_change: bool = False
    special: str = ""


def _r(*args, **kwargs) -> Route:
    return Route(*args, **kwargs)


ROUTES = [
    _r("S01", "POST", "/auth/login", "auth", "/login", "anonymous", policy="login", special="login"),
    _r("S02", "POST", "/auth/logout", "auth", "/logout", "session", csrf=True, allow_must_change=True, special="logout"),
    _r("S03", "GET", "/auth/session", "auth", "/introspect", "session", allow_must_change=True, special="session"),
    _r("S29", "POST", "/auth/password", "auth", "/password", "session", csrf=True, allow_must_change=True, special="password"),
    _r("S04", "POST", "/clients", "clients", "/clients", "optional_session", policy="register"),
    _r("S05", "POST", "/clients/email-verifications", "clients", "/email-verifications", "optional_session", policy="register"),
    _r("S06", "GET", "/clients", "clients", "/clients", "session", roles=("administrador",)),
    _r("S32", "GET", "/clients/{clientId}", "clients", "/clients/{clientId}", "session",
       roles=("cliente", "administrador")),
    _r("S33", "PUT", "/clients/{clientId}", "clients", "/clients/{clientId}", "session",
       roles=("cliente", "administrador"), csrf=True),
    _r("S07", "GET", "/users", "auth", "/users", "session", roles=("administrador",)),
    _r("S08", "POST", "/users", "auth", "/users", "session", roles=("administrador",), csrf=True),
    _r("S31", "PUT", "/users/{userId}", "auth", "/users/{userId}", "session", roles=("administrador",), csrf=True),
    _r("S09", "DELETE", "/users/{userId}", "auth", "/users/{userId}", "session", roles=("administrador",), csrf=True),
    _r("S10", "GET", "/products", "inventory", "/products", "optional_session"),
    _r("S11", "GET", "/admin/products", "inventory", "/admin/products", "session", roles=("administrador", "dueno")),
    _r("S12", "POST", "/admin/products", "inventory", "/admin/products", "session", roles=("administrador", "dueno"), csrf=True),
    _r("S30", "PUT", "/admin/products/{productId}", "inventory", "/admin/products/{productId}", "session",
       roles=("administrador", "dueno"), csrf=True),
    _r("S13", "PUT", "/admin/products/{productId}/availability", "inventory", "/admin/products/{productId}/availability",
       "session", roles=("administrador", "dueno"), csrf=True),
    _r("S14", "DELETE", "/admin/products/{productId}", "inventory", "/admin/products/{productId}", "session",
       roles=("administrador", "dueno"), csrf=True),
    _r("S25", "GET", "/admin/promotions", "inventory", "/admin/promotions", "session", roles=("administrador", "dueno")),
    _r("S26", "POST", "/admin/promotions", "inventory", "/admin/promotions", "session", roles=("administrador", "dueno"), csrf=True),
    _r("S27", "PUT", "/admin/promotions/{promotionId}/active", "inventory", "/admin/promotions/{promotionId}/active",
       "session", roles=("administrador", "dueno"), csrf=True),
    _r("S28", "DELETE", "/admin/promotions/{promotionId}", "inventory", "/admin/promotions/{promotionId}", "session",
       roles=("administrador", "dueno"), csrf=True),
    _r("S15", "POST", "/orders", "orders", "/orders", "session", roles=("cliente",), csrf=True),
    _r("S16", "GET", "/orders/mine", "orders", "/orders/mine", "session", roles=("cliente",)),
    _r("S19", "GET", "/orders", "orders", "/orders", "session", roles=("cajero", "despacho")),
    _r("S17", "GET", "/orders/{orderId}", "orders", "/orders/{orderId}", "session",
       roles=("cliente", "cajero", "despacho", "administrador")),
    _r("S18", "POST", "/orders/{orderId}/cancellation", "orders", "/orders/{orderId}/cancellation", "session",
       roles=("cliente", "administrador"), csrf=True),
    _r("S23", "POST", "/orders/{orderId}/transitions", "orders", "/orders/{orderId}/transitions", "session",
       roles=("despacho",), csrf=True),
    _r("S22", "GET", "/orders/{orderId}/receipt", "payments", "/receipts/{orderId}", "session",
       roles=("cliente", "cajero", "administrador")),
    _r("S24", "GET", "/reports/sales", "orders", "/reports/sales", "session", roles=("administrador", "dueno")),
    _r("S20", "POST", "/payments/{orderId}/confirmation", "payments", "/payments/{orderId}/confirmation", "session",
       roles=("cajero",), csrf=True),
    _r("S21", "POST", "/payments/webhooks/servipag", "payments", "/webhooks/servipag", "webhook", policy="webhook"),
    _r("GQL", "POST", "/graphql", "graphql", "/graphql", "session", csrf=True, max_body=4096),
]

_PARAM = re.compile(r"\{(\w+)\}")
_COMPILED = [
    (route, re.compile("^" + _PARAM.sub(r"(?P<\1>[A-Za-z0-9_-]{1,64})", route.path) + "$"))
    for route in ROUTES
]


def match(method: str, path: str):
    for route, pattern in _COMPILED:
        if route.method != method:
            continue
        found = pattern.match(path)
        if found:
            return route, found.groupdict()
    return None, {}
