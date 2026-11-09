import hashlib
import hmac
import json
import os
import random
import ssl
import time
import uuid
from contextlib import asynccontextmanager
from typing import Annotated, Literal, Optional

import httpx
from fastapi import Depends
from pydantic import BaseModel, Field, StringConstraints

from lachilena_common.app_factory import create_app
from lachilena_common.errors import ApiError
from lachilena_common.security import Guard, ServiceAuth
from lachilena_common.vault import VaultSecrets

secrets_store = VaultSecrets(os.environ.get("VAULT_SECRET_PATH", "servipag-sim"))
guard = Guard(ServiceAuth(secrets_store))
charges: dict = {}
Identifier = Annotated[str, StringConstraints(min_length=1, max_length=64)]


class ChargeIn(BaseModel):
    orderRef: Identifier
    amount: int = Field(gt=0)


class SimulateIn(BaseModel):
    orderRef: Identifier
    status: Literal["approved", "rejected"] = "approved"
    amount: Optional[int] = None
    eventId: Optional[Identifier] = None
    badSignature: bool = False
    staleTimestamp: bool = False


@asynccontextmanager
async def lifespan(_app):
    await secrets_store.load()
    yield


app = create_app("servipag-sim", lifespan=lifespan)


@app.post("/v1/charges")
async def create_charge(body: ChargeIn, _caller: str = Depends(guard.caller("payments"))):
    charge = {"chargeId": "chg-" + uuid.uuid4().hex[:12], "code": f"{random.randint(0, 999999):06d}", "amount": body.amount}
    charges[body.orderRef] = charge
    return {"chargeId": charge["chargeId"], "code": charge["code"]}


@app.post("/v1/simulate/payment")
async def simulate_payment(body: SimulateIn, _caller: str = Depends(guard.caller("tests"))):
    charge = charges.get(body.orderRef)
    amount = body.amount if body.amount is not None else (charge["amount"] if charge else None)
    if amount is None:
        raise ApiError(404, "NOT_FOUND", "No hay un cobro para ese pedido")
    event_id = body.eventId or "evt-" + uuid.uuid4().hex[:12]
    payload = json.dumps({"eventId": event_id, "orderRef": body.orderRef, "amount": amount, "status": body.status},
                         separators=(",", ":")).encode()
    timestamp = str(int(time.time()) - (3600 if body.staleTimestamp else 0))
    key = str(secrets_store.get("webhook_hmac", "")).encode()
    signature = hmac.new(key, timestamp.encode() + b"." + payload, hashlib.sha256).hexdigest()
    if body.badSignature:
        signature = "0" * 64
    context = ssl.create_default_context(cafile=os.environ["CA_FILE"])
    async with httpx.AsyncClient(verify=context, timeout=10.0) as client:
        response = await client.post(
            os.environ["GATEWAY_URL"] + "/api/v1/payments/webhooks/servipag", content=payload,
            headers={"Content-Type": "application/json", "X-Signature": signature, "X-Timestamp": timestamp})
    try:
        gateway_body = response.json()
    except ValueError:
        gateway_body = None
    return {"eventId": event_id, "gatewayStatus": response.status_code, "gatewayBody": gateway_body}
