import asyncio
import os
import ssl
import time

import httpx


class SecretsUnavailable(RuntimeError):
    pass


class VaultSecrets:
    def __init__(self, path: str, ttl: float | None = None):
        self.path = path
        self.ttl = ttl
        self._data: dict = {}
        self._loaded_at = 0.0

    def _token(self) -> str:
        token_file = os.getenv("VAULT_TOKEN_FILE")
        if token_file:
            with open(token_file, encoding="utf-8") as handle:
                return handle.read().strip()
        token = os.getenv("VAULT_TOKEN")
        if not token:
            raise RuntimeError("No hay token de Vault configurado")
        return token

    async def _fetch(self) -> dict:
        addr = os.getenv("VAULT_ADDR", "https://vault:8201")
        ca_file = os.getenv("CA_FILE")
        verify = ssl.create_default_context(cafile=ca_file) if addr.startswith("https") and ca_file else True
        async with httpx.AsyncClient(timeout=5.0, verify=verify) as client:
            response = await client.get(
                f"{addr}/v1/secret/data/{self.path}",
                headers={"X-Vault-Token": self._token()},
            )
        if response.status_code != 200:
            raise SecretsUnavailable(f"Vault respondió {response.status_code}")
        return response.json()["data"]["data"]

    async def load(self, retries: int = 30, delay: float = 2.0) -> None:
        last: Exception | None = None
        for _ in range(retries):
            try:
                self._data = await self._fetch()
                self._loaded_at = time.monotonic()
                return
            except Exception as exc:
                last = exc
                await asyncio.sleep(delay)
        raise RuntimeError("No se pudieron cargar los secretos de Vault") from last

    async def refresh_if_needed(self) -> None:
        if self.ttl and time.monotonic() - self._loaded_at > self.ttl:
            try:
                self._data = await self._fetch()
                self._loaded_at = time.monotonic()
            except Exception as exc:
                raise SecretsUnavailable("Vault no responde y la copia local venció") from exc

    def get(self, key: str, default=None):
        return self._data.get(key, default)
