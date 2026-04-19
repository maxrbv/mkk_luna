import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import OutboxEvent, Payment
from app.schemas.payment import PaymentCreate

PAYMENT_CREATED_EVENT = "payment.created"


async def create_payment(
    session: AsyncSession,
    *,
    idempotency_key: str,
    data: PaymentCreate,
    routing_key: str,
) -> tuple[Payment, bool]:
    """Create payment + outbox event in one transaction.

    Returns (payment, created). If a payment with the same idempotency_key
    already exists, returns the existing row with created=False.
    """
    payment_id = uuid.uuid4()
    stmt = (
        pg_insert(Payment)
        .values(
            id=payment_id,
            amount=data.amount,
            currency=data.currency,
            description=data.description,
            payment_metadata=data.metadata,
            idempotency_key=idempotency_key,
            webhook_url=str(data.webhook_url),
        )
        .on_conflict_do_nothing(index_elements=["idempotency_key"])
        .returning(Payment)
    )
    result = await session.execute(stmt)
    payment = result.scalar_one_or_none()

    if payment is None:
        existing = await session.execute(
            select(Payment).where(Payment.idempotency_key == idempotency_key)
        )
        return existing.scalar_one(), False

    session.add(
        OutboxEvent(
            event_type=PAYMENT_CREATED_EVENT,
            routing_key=routing_key,
            payload={
                "payment_id": str(payment.id),
                "webhook_url": payment.webhook_url,
            },
        )
    )
    await session.flush()
    return payment, True


async def get_payment(session: AsyncSession, payment_id: uuid.UUID) -> Payment | None:
    result = await session.execute(select(Payment).where(Payment.id == payment_id))
    return result.scalar_one_or_none()
