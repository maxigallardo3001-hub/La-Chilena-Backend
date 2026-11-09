import os
import ssl
from typing import Callable

import httpx

from .errors import ApiError
from .logging_setup import correlation_id_var
from .security import Identity


def _ssl_context():
    if os.getenv("INTERNAL_TLS", "true").lower() != "true":
        return True
    return ssl.create_default_context(cafile=os.environ["CA_FILE"])


class ServiceClient:
    def __init__(self, base_url: str, credential: Callable[[], str], timeout: float = 3.0):
        self._credential = credential
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout, verify=_ssl_context())

    async def request(self, method: str, path: str, *, identity: Identity | None = None,
                      json=None, params=None, content=None, headers=None) -> httpx.Response:
        merged = {
            "X-Service-Credential": self._credential(),
            "X-Correlation-Id": correlation_id_var.get(),
        }
        if identity is not None:
            merged["X-Authenticated-User"] = identity.user_id
            merged["X-Authenticated-Roles"] = ",".join(identity.roles)
        if headers:
            merged.update(headers)
        try:
            return await self._client.request(method, path, json=json, params=params,
                                              content=content, headers=merged)
        except httpx.TimeoutException:
            raise ApiError(504, "UPSTREAM_TIMEOUT", "El servicio tardó demasiado en responder")
        except httpx.RequestError:
            raise ApiError(503, "SERVICE_UNAVAILABLE", "El servicio no está disponible")

    async def aclose(self) -> None:
        await self._client.aclose()
