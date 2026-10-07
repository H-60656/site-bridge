import asyncio
import time


class RateLimiter:
    def __init__(self, rpm):
        self._rate = max(1, rpm) / 60.0
        self._burst = max(1, rpm // 10)
        self._tokens = float(self._burst)
        self._updated = time.monotonic()

    async def acquire(self):
        while True:
            now = time.monotonic()
            self._tokens = min(
                float(self._burst),
                self._tokens + (now - self._updated) * self._rate,
            )
            self._updated = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
            await asyncio.sleep((1.0 - self._tokens) / self._rate)
