import asyncio

import pytest

from app.api.rate_limit import TokenBucket


async def test_allows_up_to_capacity_without_refill():
    bucket = TokenBucket(capacity=3, refill_per_second=0.0)
    assert await bucket.try_consume() is True
    assert await bucket.try_consume() is True
    assert await bucket.try_consume() is True
    assert await bucket.try_consume() is False


async def test_refills_over_time():
    bucket = TokenBucket(capacity=2, refill_per_second=100.0)
    await bucket.try_consume()
    await bucket.try_consume()
    assert await bucket.try_consume() is False
    await asyncio.sleep(0.05)
    assert await bucket.try_consume() is True


async def test_refill_caps_at_capacity():
    bucket = TokenBucket(capacity=2, refill_per_second=1000.0)
    await asyncio.sleep(0.1)  # would accumulate 100 tokens uncapped
    assert await bucket.try_consume() is True
    assert await bucket.try_consume() is True
    assert await bucket.try_consume() is False


async def test_concurrent_consumers_respect_capacity():
    bucket = TokenBucket(capacity=5, refill_per_second=0.0)
    results = await asyncio.gather(*[bucket.try_consume() for _ in range(10)])
    assert sum(results) == 5


@pytest.mark.parametrize("n", [1, 2, 3])
async def test_consume_n_tokens(n: int):
    bucket = TokenBucket(capacity=3, refill_per_second=0.0)
    assert await bucket.try_consume(n) is True
    assert await bucket.try_consume(3 - n + 1) is False
