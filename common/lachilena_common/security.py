import hmac
from dataclasses import dataclass

from fastapi import Depends, Header, Request

from .errors import ApiError
from .vault import VaultSecrets

ASSERTERS = ("api-gateway", "graphql-gateway")


@dataclass(frozen=True)
class Identity:
    user_id: str
    roles: tuple
    caller: str


def _match(candidate: str, accepted: str) -> bool:
    return hmac.compare_digest(candidate.encode("utf-8"), accepted.encode("utf-8"))


class ServiceAuth:
    def __init__(self, secrets: VaultSecrets):
        self.secrets = secrets

    def caller_of(self, credential: str, allowed) -> str | None:
        if not credential:
            return None
        found = None
        for caller in allowed:
            for value in str(self.secrets.get(f"accept_{caller}", "")).split(","):
                value = value.strip()
                if value and _match(credential, value):
                    found = caller
        return found


class Guard:
    def __init__(self, auth: ServiceAuth):
        self.auth = auth

    def caller(self, *allowed: str):
        async def dependency(x_service_credential: str = Header(default="")) -> str:
            caller = self.auth.caller_of(x_service_credential, allowed)
            if caller is None:
                raise ApiError(403, "FORBIDDEN", "Llamada no autorizada")
            return caller

        return dependency

    def user(self, *roles: str, callers=ASSERTERS):
        caller_dependency = self.caller(*callers)

        async def dependency(request: Request, caller: str = Depends(caller_dependency)) -> Identity:
            user_id = request.headers.get("x-authenticated-user", "")
            if not user_id:
                raise ApiError(401, "UNAUTHENTICATED", "Se requiere una sesión válida")
            granted = tuple(r for r in request.headers.get("x-authenticated-roles", "").split(",") if r)
            if roles and not set(roles) & set(granted):
                raise ApiError(403, "FORBIDDEN", "No tienes permiso para esta acción")
            return Identity(user_id=user_id, roles=granted, caller=caller)

        return dependency
