import logging
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.config import WebhookSettings

log = logging.getLogger(__name__)


class WebhookDeliveryError(Exception):
    pass


class WebhookSender:
    def __init__(self, client: httpx.AsyncClient, settings: WebhookSettings) -> None:
        self._client = client
        self._settings = settings

    async def send(self, url: str, payload: dict[str, Any]) -> None:
        retrying = AsyncRetrying(
            stop=stop_after_attempt(self._settings.max_retries),
            wait=wait_exponential(multiplier=self._settings.backoff_base_seconds, min=0, max=30),
            retry=retry_if_exception_type(WebhookDeliveryError),
            reraise=True,
        )
        async for attempt in retrying:
            with attempt:
                await self._attempt(url, payload)

    async def _attempt(self, url: str, payload: dict[str, Any]) -> None:
        extra = {"webhook_url": url, "payment_id": payload.get("payment_id")}
        try:
            response = await self._client.post(
                url, json=payload, timeout=self._settings.timeout_seconds
            )
        except httpx.HTTPError as exc:
            log.warning("Webhook request failed: %s", exc, extra=extra)
            raise WebhookDeliveryError(str(exc)) from exc

        status = response.status_code
        if status >= 500:
            log.warning("Webhook returned %s", status, extra=extra)
            raise WebhookDeliveryError(f"HTTP {status}")
        if status >= 400:
            log.error("Webhook rejected with %s, giving up", status, extra=extra)
            return
        log.info("Webhook delivered", extra={**extra, "status": status})
