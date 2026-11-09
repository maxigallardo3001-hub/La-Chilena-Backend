import asyncio
import contextlib
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable

from .logging_setup import log

logger = logging.getLogger("outbox")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Outbox:
    def __init__(self, collection, sender: Callable[[dict], Awaitable[bool]],
                 interval: float = 10.0, max_attempts: int = 5, batch: int = 20):
        self.collection = collection
        self.sender = sender
        self.interval = interval
        self.max_attempts = max_attempts
        self.batch = batch
        self._task: asyncio.Task | None = None

    async def enqueue(self, message: dict) -> None:
        await self.collection.insert_one(
            {"message": message, "attempts": 0, "status": "pending", "createdAt": _now()}
        )

    async def flush_once(self) -> None:
        cursor = self.collection.find({"status": "pending"}).sort("createdAt", 1).limit(self.batch)
        async for doc in cursor:
            try:
                delivered = await self.sender(doc["message"])
            except Exception:
                delivered = False
            if delivered:
                await self.collection.delete_one({"_id": doc["_id"]})
                continue
            attempts = doc["attempts"] + 1
            update = {"attempts": attempts}
            if attempts >= self.max_attempts:
                update["status"] = "failed"
            await self.collection.update_one({"_id": doc["_id"]}, {"$set": update})

    async def _loop(self) -> None:
        while True:
            try:
                await self.flush_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log(logger, "outbox_flush_failed")
            await asyncio.sleep(self.interval)

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
