import asyncio
import contextlib
import logging

import aio_pika

log = logging.getLogger(__name__)


class DLQMonitor:
    def __init__(
        self,
        rmq_url: str,
        dlq_name: str,
        threshold: int,
        interval_seconds: float,
    ) -> None:
        self._url = rmq_url
        self._dlq_name = dlq_name
        self._threshold = threshold
        self._interval = interval_seconds
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._conn: aio_pika.abc.AbstractRobustConnection | None = None

    def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="dlq-monitor")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        try:
            await asyncio.wait_for(self._task, timeout=5)
        except TimeoutError:
            self._task.cancel()
        self._task = None
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def _run(self) -> None:
        log.info(
            "DLQ monitor started",
            extra={
                "queue": self._dlq_name,
                "threshold": self._threshold,
                "interval_seconds": self._interval,
            },
        )
        try:
            self._conn = await aio_pika.connect_robust(self._url)
        except Exception:
            log.exception("DLQ monitor could not connect")
            return

        while not self._stop.is_set():
            try:
                await self._check()
            except Exception:
                log.exception("DLQ depth check failed")
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
        log.info("DLQ monitor stopped")

    async def _check(self) -> None:
        assert self._conn is not None
        channel = await self._conn.channel()
        try:
            queue = await channel.declare_queue(self._dlq_name, durable=True, passive=True)
            depth = queue.declaration_result.message_count or 0
            extra = {
                "queue": self._dlq_name,
                "depth": depth,
                "threshold": self._threshold,
            }
            if depth >= self._threshold:
                log.warning("DLQ depth exceeded threshold", extra=extra)
            else:
                log.debug("DLQ depth ok", extra=extra)
        finally:
            await channel.close()
