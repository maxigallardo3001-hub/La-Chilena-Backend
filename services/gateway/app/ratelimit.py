import time
from collections import defaultdict, deque

LIMITS = {
    "login": (10, 60),
    "register": (10, 60),
    "webhook": (60, 60),
    "default": (600, 60),
}


class RateLimiter:
    def __init__(self, multiplier: float = 1.0):
        self.multiplier = multiplier
        self._hits: dict = defaultdict(deque)
        self._checks = 0

    def check(self, policy: str, client: str) -> int | None:
        limit, window = LIMITS.get(policy, LIMITS["default"])
        limit = int(limit * self.multiplier)
        now = time.monotonic()
        hits = self._hits[(policy, client)]
        while hits and now - hits[0] > window:
            hits.popleft()
        if len(hits) >= limit:
            return int(window - (now - hits[0])) + 1
        hits.append(now)
        self._checks += 1
        if self._checks % 1000 == 0:
            self._prune(now)
        return None

    def _prune(self, now: float) -> None:
        for key in [k for k, q in self._hits.items() if not q or now - q[-1] > 120]:
            del self._hits[key]
