import asyncio
import logging
import os
import smtplib
from contextlib import asynccontextmanager
from email.message import EmailMessage
from typing import Annotated, Literal

from fastapi import Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, StringConstraints

from lachilena_common.app_factory import create_app
from lachilena_common.errors import ApiError
from lachilena_common.logging_setup import log
from lachilena_common.security import Guard, ServiceAuth
from lachilena_common.vault import VaultSecrets

logger = logging.getLogger("notifications")
secrets_store = VaultSecrets(os.environ.get("VAULT_SECRET_PATH", "notifications"))
guard = Guard(ServiceAuth(secrets_store))
SENDER = "La Chilena <no-responder@lachilena.local>"


class MessageIn(BaseModel):
    to: Annotated[str, StringConstraints(max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
    template: Literal["temp_password", "receipt"]
    data: dict


def money(amount) -> str:
    return "$" + f"{int(amount):,}".replace(",", ".")


def render(template: str, data: dict) -> tuple[str, str]:
    if template == "temp_password":
        return ("Tu clave temporal de La Chilena",
                f"Hola {data.get('name', '')},\n\nTu clave temporal es: {data.get('tempPassword', '')}\n\n"
                "Ingresa con tu correo y esta clave. El sistema te pedirá cambiarla en tu primer ingreso. "
                "Vence en 24 horas.\n\nLa Chilena · Empanadas artesanales\n")
    lines = [f"  {i['quantity']} x {i['name']}  {money(i['unitPrice'] * i['quantity'])}" for i in data.get("items", [])]
    return (f"Tu boleta {data.get('receiptId', '')} de La Chilena",
            f"Gracias por tu compra. Pedido {data.get('orderId', '')}\n\n" + "\n".join(lines) +
            f"\n\nNeto: {money(data.get('net', 0))}\nIVA: {money(data.get('iva', 0))}\nTotal: {money(data.get('total', 0))}\n"
            f"\nEmitida el {data.get('issuedAt', '')}\nLa Chilena · Empanadas artesanales\n")


def deliver(message: EmailMessage) -> None:
    with smtplib.SMTP(os.environ.get("SMTP_HOST", "mailpit"), int(os.environ.get("SMTP_PORT", "1025")), timeout=10) as smtp:
        smtp.ehlo()
        user, password = secrets_store.get("smtp_user"), secrets_store.get("smtp_password")
        if user and password and smtp.has_extn("auth"):
            smtp.login(str(user), str(password))
        smtp.send_message(message)


@asynccontextmanager
async def lifespan(_app):
    await secrets_store.load()
    yield


app = create_app("notifications", lifespan=lifespan)


@app.post("/internal/messages", status_code=202)
async def send_message(body: MessageIn, _caller: str = Depends(guard.caller("auth", "payments"))):
    subject, text = render(body.template, body.data)
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = SENDER, body.to, subject
    message.set_content(text)
    try:
        await asyncio.to_thread(deliver, message)
    except Exception:
        log(logger, "delivery_failed", template=body.template)
        raise ApiError(502, "DELIVERY_FAILED", "No se pudo entregar el mensaje")
    return JSONResponse({"status": "sent"}, status_code=202)
