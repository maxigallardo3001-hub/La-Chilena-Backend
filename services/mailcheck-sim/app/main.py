import os
import re
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends
from pydantic import BaseModel, StringConstraints

from lachilena_common.app_factory import create_app
from lachilena_common.security import Guard, ServiceAuth
from lachilena_common.vault import VaultSecrets

secrets_store = VaultSecrets(os.environ.get("VAULT_SECRET_PATH", "mailcheck-sim"))
guard = Guard(ServiceAuth(secrets_store))
BLOCKED_DOMAINS = {"invalid.test", "noexiste.cl", "example.invalid"}
FORMAT = re.compile(r"^[^@\s]+@([^@\s]+\.[^@\s]+)$")


class ValidateIn(BaseModel):
    email: Annotated[str, StringConstraints(max_length=254)]


@asynccontextmanager
async def lifespan(_app):
    await secrets_store.load()
    yield


app = create_app("mailcheck-sim", lifespan=lifespan)


@app.post("/v1/validate")
async def validate(body: ValidateIn, _caller: str = Depends(guard.caller("clients"))):
    found = FORMAT.match(body.email.strip().lower())
    local = body.email.split("@")[0].lower()
    deliverable = bool(found) and found.group(1) not in BLOCKED_DOMAINS and not local.startswith("noexiste")
    return {"deliverable": deliverable}
