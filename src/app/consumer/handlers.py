import asyncio
import logging
import random
import uuid
from datetime import UTC, datetime

from faststream.rabbit import RabbitBroker
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.broker.rabbit import Topology
from app.config import PaymentProcessorSettings
from app.database import Database
from app.models import Payment, PaymentStatus
from app.webhook.sender import WebhookDeliveryError, WebhookSender

log = logging.getLogger(__name__)


class PaymentMessage(BaseModel):
    payment_id: uuid.UUID
    webhook_url: str


def register_handlers(
    broker: RabbitBroker,
    topology: Topology,
    database: Database,
    webhook_sender: WebhookSender,
    processor: PaymentProcessorSettings,
) -> None:
    """Wire the payments.new subscriber into the given broker.

    Delivery attempts and DLQ routing are enforced by the queue itself
    (quorum queue with x-delivery-limit + x-dead-letter-exchange).
    """

    @broker.subscriber(
        topology.payments_queue,
        topology.payments_exchange,
    )
    async def handle_payment(msg: PaymentMessage) -> None:
        async with database.sessionmaker() as session:
            await _process(session, msg, webhook_sender, processor)


async def _process(
    session: AsyncSession,
    msg: PaymentMessage,
    webhook_sender: WebhookSender,
    processor: PaymentProcessorSettings,
) -> None:
    extra = {"payment_id": str(msg.payment_id)}
    async with session.begin():
        payment = await session.get(Payment, msg.payment_id, with_for_update=True)
        if payment is None:
            log.error("Payment not found, dropping message", extra=extra)
            return

        if payment.status == PaymentStatus.PENDING:
            delay = random.uniform(processor.min_delay_seconds, processor.max_delay_seconds)
            log.info(
                "Processing payment (simulated delay %.2fs)",
                delay,
                extra=extra,
            )
            await asyncio.sleep(delay)

            is_success = random.random() < processor.success_rate
            payment.status = PaymentStatus.SUCCEEDED if is_success else PaymentStatus.FAILED
            payment.processed_at = datetime.now(UTC)
        else:
            log.info(
                "Payment already in terminal state, resending webhook only",
                extra={**extra, "status": payment.status.value},
            )

        status_value = payment.status.value
        webhook_url = payment.webhook_url

    try:
        await webhook_sender.send(
            webhook_url,
            {"payment_id": extra["payment_id"], "status": status_value},
        )
    except WebhookDeliveryError:
        log.exception("Webhook delivery exhausted retries", extra=extra)
        raise
