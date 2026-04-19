from dataclasses import dataclass
from typing import Any

from faststream.rabbit import (
    ExchangeType,
    QueueType,
    RabbitBroker,
    RabbitExchange,
    RabbitQueue,
)

from app.config import RabbitMQSettings


@dataclass(slots=True, frozen=True)
class Topology:
    payments_exchange: RabbitExchange
    payments_queue: RabbitQueue
    payments_routing_key: str
    dlx: RabbitExchange
    dlq: RabbitQueue


def payments_queue_arguments(settings: RabbitMQSettings) -> dict[str, Any]:
    """Arguments for the main payments queue. Kept in one place so tests can
    declare an identical queue via aio_pika without duplicating constants."""
    return {
        "x-queue-type": "quorum",
        "x-dead-letter-exchange": settings.dlq_exchange,
        "x-dead-letter-strategy": "at-least-once",
        "x-overflow": "reject-publish",
        "x-delivery-limit": settings.max_delivery_attempts,
    }


def build_topology(settings: RabbitMQSettings) -> Topology:
    dlx = RabbitExchange(
        settings.dlq_exchange,
        type=ExchangeType.FANOUT,
        durable=True,
    )
    dlq = RabbitQueue(
        settings.dlq_queue,
        queue_type=QueueType.QUORUM,
        durable=True,
    )
    payments_exchange = RabbitExchange(
        settings.payments_exchange,
        type=ExchangeType.TOPIC,
        durable=True,
    )
    # FastStream types arguments as a narrow TypedDict per queue kind.
    # Our dict[str, Any] is deliberately shape-agnostic — overload matching
    # rejects it, so we silence the call here.
    payments_queue = RabbitQueue(  # type: ignore[call-overload]
        settings.payments_queue,
        queue_type=QueueType.QUORUM,
        durable=True,
        routing_key=settings.payments_routing_key,
        arguments=payments_queue_arguments(settings),
    )
    return Topology(
        payments_exchange=payments_exchange,
        payments_queue=payments_queue,
        payments_routing_key=settings.payments_routing_key,
        dlx=dlx,
        dlq=dlq,
    )


def build_broker(settings: RabbitMQSettings) -> RabbitBroker:
    return RabbitBroker(settings.url.get_secret_value())


async def declare_dlq(broker: RabbitBroker, topology: Topology) -> None:
    """DLX + DLQ must exist before the main queue declares x-dead-letter-exchange."""
    await broker.declare_exchange(topology.dlx)
    await broker.declare_queue(topology.dlq)
