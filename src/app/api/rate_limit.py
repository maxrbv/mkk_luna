import asyncio
import time
from typing import Annotated, cast

from fastapi import Depends, HTTPException, Request, status


class TokenBucket:
    def __init__(self, capacity: int, refill_per_second: float) -> None:
        self._capacity = float(capacity)
        self._refill = refill_per_second
        self._tokens = float(capacity)
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def try_consume(self, n: int = 1) -> bool:
        async with self._lock:
            now = time.monotonic()
            self._tokens = min(self._capacity, self._tokens + (now - self._last) * self._refill)
            self._last = now
            if self._tokens >= n:
                self._tokens -= n
                return True
            return False


def get_rate_limiter(request: Request) -> TokenBucket:
    return cast(TokenBucket, request.app.state.rate_limiter)


async def enforce_rate_limit(
    limiter: Annotated[TokenBucket, Depends(get_rate_limiter)],
) -> None:
    if not await limiter.try_consume():
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests",
        )


RateLimitDep = Depends(enforce_rate_limit)
