import asyncio
import contextlib
import logging
from datetime import UTC, datetime

from faststream.rabbit import RabbitBroker
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.broker.rabbit import Topology
from app.config import OutboxSettings
from app.models import OutboxEvent, OutboxStatus

log = logging.getLogger(__name__)


class OutboxRelay:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        broker: RabbitBroker,
        topology: Topology,
        settings: OutboxSettings,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._broker = broker
        self._topology = topology
        self._settings = settings
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="outbox-relay")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        try:
            await asyncio.wait_for(self._task, timeout=5)
        except TimeoutError:
            self._task.cancel()
        self._task = None

    async def _run(self) -> None:
        log.info("Outbox relay started (poll=%.2fs)", self._settings.poll_interval_seconds)
        while not self._stop.is_set():
            try:
                processed = await self._process_batch()
            except Exception:
                log.exception("Outbox relay iteration failed")
                processed = 0
            if processed == 0:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._stop.wait(),
                        timeout=self._settings.poll_interval_seconds,
                    )
        log.info("Outbox relay stopped")

    async def _process_batch(self) -> int:
        async with self._sessionmaker() as session:
            async with session.begin():
                result = await session.execute(
                    select(OutboxEvent)
                    .where(OutboxEvent.status == OutboxStatus.PENDING)
                    .order_by(OutboxEvent.created_at)
                    .limit(self._settings.batch_size)
                    .with_for_update(skip_locked=True)
                )
                events = list(result.scalars())
                for event in events:
                    await self._publish(session, event)
            return len(events)

    async def _publish(self, session: AsyncSession, event: OutboxEvent) -> None:
        extra = {"event_id": str(event.id), "routing_key": event.routing_key}
        try:
            await self._broker.publish(
                event.payload,
                exchange=self._topology.payments_exchange,
                routing_key=event.routing_key,
            )
        except Exception as exc:
            event.attempts += 1
            event.last_error = str(exc)[:2000]
            if event.attempts >= self._settings.max_publish_attempts:
                event.status = OutboxStatus.FAILED
                log.error(
                    "Outbox event exceeded %s attempts, marking FAILED",
                    self._settings.max_publish_attempts,
                    extra={**extra, "attempts": event.attempts},
                )
            else:
                log.exception(
                    "Failed to publish outbox event",
                    extra={**extra, "attempts": event.attempts},
                )
            return

        event.status = OutboxStatus.PUBLISHED
        event.published_at = datetime.now(UTC)
        event.attempts += 1
        log.debug("Published outbox event", extra=extra)
