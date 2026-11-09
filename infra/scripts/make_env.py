import secrets
import sys
from pathlib import Path

RANDOM_KEYS = [
    "VAULT_DEV_ROOT_TOKEN", "MONGO_ROOT_PASSWORD",
    "DB_PASS_AUTH", "DB_PASS_CLIENTS", "DB_PASS_INVENTORY", "DB_PASS_ORDERS", "DB_PASS_PAYMENTS",
    "GW_AUTH_SECRET", "GW_CLIENTS_SECRET", "GW_INVENTORY_SECRET", "GW_ORDERS_SECRET",
    "GW_PAYMENTS_SECRET", "GW_GRAPHQL_SECRET",
    "GQL_ORDERS_SECRET", "GQL_INVENTORY_SECRET", "GQL_PAYMENTS_SECRET",
    "ORDERS_INVENTORY_SECRET", "ORDERS_CLIENTS_SECRET", "ORDERS_PAYMENTS_SECRET",
    "PAYMENTS_ORDERS_SECRET", "CLIENTS_AUTH_SECRET",
    "AUTH_NOTIFY_SECRET", "PAYMENTS_NOTIFY_SECRET",
    "SERVIPAG_KEY", "SERVIPAG_WEBHOOK_HMAC", "MAILCHECK_KEY", "SMTP_USER", "SMTP_PASSWORD",
    "AUTH_CSRF_KEY", "SIM_CONTROL_KEY",
]

DEMO_ROLES = ["ADMIN", "CAJERO", "DESPACHO", "DUENO", "CLIENTE"]


def main() -> None:
    target = Path(__file__).resolve().parents[2] / ".env"
    if target.exists():
        sys.exit(f"{target} ya existe. Bórralo si quieres generar uno nuevo.")
    lines = ["MONGO_ROOT_USER=root", "BOOTSTRAP_ADMIN_EMAIL=admin@lachilena.local"]
    lines += [f"{key}={secrets.token_hex(24)}" for key in RANDOM_KEYS]
    lines.append("BOOTSTRAP_ADMIN_PASSWORD=" + secrets.token_urlsafe(12))
    lines += [f"DEMO_PASSWORD_{role}=" + secrets.token_urlsafe(9) for role in DEMO_ROLES]
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Creado {target}")
    print("La contraseña inicial del administrador está en BOOTSTRAP_ADMIN_PASSWORD (debe cambiarse al ingresar).")
    print("Las contraseñas de las cuentas de demostración están en DEMO_PASSWORD_<ROL>.")


if __name__ == "__main__":
    main()
