from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import OutboxSettings
from app.models import OutboxEvent, OutboxStatus
from app.outbox.relay import OutboxRelay


@dataclass
class FakeBroker:
    fail_times: int = 0
    published: list[tuple[Any, str]] = None  # type: ignore[assignment]
    calls: int = 0

    def __post_init__(self) -> None:
        if self.published is None:
            self.published = []

    async def publish(self, payload: Any, exchange: Any, routing_key: str) -> None:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError(f"broker down (call {self.calls})")
        self.published.append((payload, routing_key))


def _topology() -> MagicMock:
    t = MagicMock()
    t.payments_exchange = MagicMock(name="payments-exchange")
    return t


async def _insert_pending(session: AsyncSession, payload: dict) -> OutboxEvent:
    event = OutboxEvent(
        event_type="payment.created",
        routing_key="payments.new",
        payload=payload,
    )
    session.add(event)
    await session.flush()
    return event


async def _get(session: AsyncSession, event_id) -> OutboxEvent:
    return (
        await session.execute(select(OutboxEvent).where(OutboxEvent.id == event_id))
    ).scalar_one()


@pytest.fixture
def outbox_settings() -> OutboxSettings:
    return OutboxSettings(
        poll_interval_seconds=0.01,
        batch_size=10,
        max_publish_attempts=3,
    )


async def test_successful_publish_marks_event_published(
    session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    outbox_settings: OutboxSettings,
):
    async with session.begin():
        event = await _insert_pending(session, {"payment_id": "abc"})

    broker = FakeBroker()
    relay = OutboxRelay(sessionmaker, broker, _topology(), outbox_settings)
    processed = await relay._process_batch()

    assert processed == 1
    assert broker.published == [({"payment_id": "abc"}, "payments.new")]

    async with sessionmaker() as s:
        stored = await _get(s, event.id)
        assert stored.status is OutboxStatus.PUBLISHED
        assert stored.published_at is not None
        assert stored.attempts == 1


async def test_transient_failure_increments_attempts_keeps_pending(
    session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    outbox_settings: OutboxSettings,
):
    async with session.begin():
        event = await _insert_pending(session, {"payment_id": "x"})

    broker = FakeBroker(fail_times=1)
    relay = OutboxRelay(sessionmaker, broker, _topology(), outbox_settings)
    await relay._process_batch()

    async with sessionmaker() as s:
        stored = await _get(s, event.id)
        assert stored.status is OutboxStatus.PENDING
        assert stored.attempts == 1
        assert "broker down" in (stored.last_error or "")


async def test_exceed_max_attempts_marks_failed(
    session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    outbox_settings: OutboxSettings,
):
    async with session.begin():
        event = await _insert_pending(session, {"payment_id": "y"})

    broker = FakeBroker(fail_times=100)
    relay = OutboxRelay(sessionmaker, broker, _topology(), outbox_settings)
    for _ in range(outbox_settings.max_publish_attempts):
        await relay._process_batch()

    async with sessionmaker() as s:
        stored = await _get(s, event.id)
        assert stored.status is OutboxStatus.FAILED
        assert stored.attempts == outbox_settings.max_publish_attempts


async def test_failed_events_not_picked_up(
    session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    outbox_settings: OutboxSettings,
):
    async with session.begin():
        event = OutboxEvent(
            event_type="payment.created",
            routing_key="payments.new",
            payload={"payment_id": "z"},
            status=OutboxStatus.FAILED,
            attempts=10,
        )
        session.add(event)

    broker = FakeBroker()
    relay = OutboxRelay(sessionmaker, broker, _topology(), outbox_settings)
    processed = await relay._process_batch()

    assert processed == 0
    assert broker.published == []
