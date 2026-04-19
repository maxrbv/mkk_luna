from httpx import AsyncClient

from app.api.rate_limit import TokenBucket
from tests.api.conftest import TEST_API_KEY


def _headers(idempotency_key: str | None = "k-1") -> dict[str, str]:
    headers = {"X-API-Key": TEST_API_KEY}
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def _body(**overrides) -> dict:
    data = {
        "amount": "100.00",
        "currency": "USD",
        "description": "test",
        "metadata": {},
        "webhook_url": "https://example.com/hook",
    }
    data.update(overrides)
    return data


async def test_health(api_client: AsyncClient):
    r = await api_client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def test_readiness(api_client: AsyncClient):
    r = await api_client.get("/readiness")
    assert r.status_code == 200


async def test_post_payment_accepted(api_client: AsyncClient):
    r = await api_client.post("/api/v1/payments", headers=_headers("post-1"), json=_body())
    assert r.status_code == 202
    data = r.json()
    assert data["status"] == "pending"
    assert "id" in data and "created_at" in data


async def test_get_payment_after_post(api_client: AsyncClient):
    post = await api_client.post("/api/v1/payments", headers=_headers("get-1"), json=_body())
    payment_id = post.json()["id"]

    r = await api_client.get(f"/api/v1/payments/{payment_id}", headers={"X-API-Key": TEST_API_KEY})
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == payment_id
    assert body["status"] == "pending"
    assert body["amount"] == "100.00"


async def test_get_unknown_payment_returns_404(api_client: AsyncClient):
    r = await api_client.get(
        "/api/v1/payments/00000000-0000-0000-0000-000000000000",
        headers={"X-API-Key": TEST_API_KEY},
    )
    assert r.status_code == 404


async def test_idempotency_same_key_returns_same_id(api_client: AsyncClient):
    r1 = await api_client.post(
        "/api/v1/payments", headers=_headers("idem-1"), json=_body(amount="1.00")
    )
    r2 = await api_client.post(
        "/api/v1/payments", headers=_headers("idem-1"), json=_body(amount="999.00")
    )
    assert r1.status_code == 202
    assert r2.status_code == 202
    assert r1.json()["id"] == r2.json()["id"]


async def test_missing_api_key_returns_401(api_client: AsyncClient):
    r = await api_client.post(
        "/api/v1/payments",
        headers={"Idempotency-Key": "no-auth"},
        json=_body(),
    )
    assert r.status_code == 401


async def test_wrong_api_key_returns_401(api_client: AsyncClient):
    r = await api_client.post(
        "/api/v1/payments",
        headers={"X-API-Key": "wrong", "Idempotency-Key": "bad-auth"},
        json=_body(),
    )
    assert r.status_code == 401


async def test_missing_idempotency_key_returns_400(api_client: AsyncClient):
    r = await api_client.post(
        "/api/v1/payments",
        headers={"X-API-Key": TEST_API_KEY},
        json=_body(),
    )
    assert r.status_code == 400


async def test_amount_above_cap_returns_422(api_client: AsyncClient):
    r = await api_client.post(
        "/api/v1/payments",
        headers=_headers("too-big"),
        json=_body(amount="9999999999.99"),
    )
    assert r.status_code == 422


async def test_unknown_currency_returns_422(api_client: AsyncClient):
    r = await api_client.post(
        "/api/v1/payments", headers=_headers("bad-cur"), json=_body(currency="BTC")
    )
    assert r.status_code == 422


async def test_rate_limit_returns_429_when_bucket_empty(api_client: AsyncClient):
    # Swap in a tiny bucket so we can exhaust it synchronously.
    api_client._transport.app.state.rate_limiter = TokenBucket(  # type: ignore[attr-defined]
        capacity=2, refill_per_second=0.0
    )
    r1 = await api_client.post("/api/v1/payments", headers=_headers("rl-1"), json=_body())
    r2 = await api_client.post("/api/v1/payments", headers=_headers("rl-2"), json=_body())
    r3 = await api_client.post("/api/v1/payments", headers=_headers("rl-3"), json=_body())
    assert r1.status_code == 202
    assert r2.status_code == 202
    assert r3.status_code == 429
