import uuid
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import OutboxEvent, Payment
from app.schemas.payment import PaymentCreate
from app.services import payments as service


def _payload(amount: str = "100.00") -> PaymentCreate:
    return PaymentCreate.model_validate(
        {
            "amount": amount,
            "currency": "USD",
            "description": "test",
            "metadata": {"k": "v"},
            "webhook_url": "https://example.com/hook",
        }
    )


async def _count(session: AsyncSession, model) -> int:
    return (await session.execute(select(func.count()).select_from(model))).scalar_one()


async def test_create_payment_persists_payment_and_outbox(session: AsyncSession):
    async with session.begin():
        payment, created = await service.create_payment(
            session,
            idempotency_key="k-1",
            data=_payload(),
            routing_key="payments.new",
        )

    assert created is True
    assert payment.amount == Decimal("100.00")
    assert payment.payment_metadata == {"k": "v"}
    assert await _count(session, Payment) == 1
    assert await _count(session, OutboxEvent) == 1

    event = (await session.execute(select(OutboxEvent))).scalar_one()
    assert event.payload == {
        "payment_id": str(payment.id),
        "webhook_url": "https://example.com/hook",
    }
    assert event.routing_key == "payments.new"


async def test_same_idempotency_key_returns_existing_and_no_new_outbox(
    session: AsyncSession,
):
    async with session.begin():
        first, created1 = await service.create_payment(
            session,
            idempotency_key="k-2",
            data=_payload("10.00"),
            routing_key="payments.new",
        )
    async with session.begin():
        second, created2 = await service.create_payment(
            session,
            idempotency_key="k-2",
            data=_payload("99.99"),  # different body — must be ignored
            routing_key="payments.new",
        )

    assert created1 is True
    assert created2 is False
    assert first.id == second.id
    # Returned row is the original amount; the second body is discarded.
    assert second.amount == Decimal("10.00")
    assert await _count(session, Payment) == 1
    assert await _count(session, OutboxEvent) == 1


async def test_different_keys_create_independent_payments(session: AsyncSession):
    async with session.begin():
        p1, _ = await service.create_payment(
            session,
            idempotency_key="k-a",
            data=_payload(),
            routing_key="payments.new",
        )
    async with session.begin():
        p2, _ = await service.create_payment(
            session,
            idempotency_key="k-b",
            data=_payload(),
            routing_key="payments.new",
        )

    assert p1.id != p2.id
    assert await _count(session, Payment) == 2
    assert await _count(session, OutboxEvent) == 2


async def test_get_payment_returns_none_for_missing(session: AsyncSession):
    result = await service.get_payment(session, uuid.uuid4())
    assert result is None


async def test_get_payment_finds_existing(session: AsyncSession):
    async with session.begin():
        created, _ = await service.create_payment(
            session,
            idempotency_key="k-get",
            data=_payload(),
            routing_key="payments.new",
        )
    found = await service.get_payment(session, created.id)
    assert found is not None
    assert found.id == created.id
