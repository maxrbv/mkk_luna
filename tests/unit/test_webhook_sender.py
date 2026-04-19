import httpx
import pytest

from app.config import WebhookSettings
from app.webhook.sender import WebhookDeliveryError, WebhookSender


def _settings(max_retries: int = 3) -> WebhookSettings:
    return WebhookSettings(
        timeout_seconds=1.0,
        max_retries=max_retries,
        backoff_base_seconds=0.0,  # no actual backoff in tests
    )


def _mock_client(handler) -> httpx.AsyncClient:
    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport)


async def test_delivers_on_2xx():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200)

    async with _mock_client(handler) as client:
        sender = WebhookSender(client, _settings())
        await sender.send("https://h.example/hook", {"payment_id": "x"})

    assert calls == 1


async def test_retries_on_5xx_then_succeeds():
    responses = iter([500, 502, 200])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(next(responses))

    async with _mock_client(handler) as client:
        sender = WebhookSender(client, _settings(max_retries=3))
        await sender.send("https://h.example/hook", {"payment_id": "x"})


async def test_raises_after_exhausted_retries_on_5xx():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async with _mock_client(handler) as client:
        sender = WebhookSender(client, _settings(max_retries=2))
        with pytest.raises(WebhookDeliveryError):
            await sender.send("https://h.example/hook", {"payment_id": "x"})


async def test_4xx_is_terminal_not_retried():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(404)

    async with _mock_client(handler) as client:
        sender = WebhookSender(client, _settings(max_retries=3))
        await sender.send("https://h.example/hook", {"payment_id": "x"})

    assert calls == 1


async def test_network_error_raises_delivery_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    async with _mock_client(handler) as client:
        sender = WebhookSender(client, _settings(max_retries=1))
        with pytest.raises(WebhookDeliveryError):
            await sender.send("https://h.example/hook", {"payment_id": "x"})
