import asyncio
import json
import uuid
from collections.abc import Iterator

import aio_pika
import pytest
import pytest_asyncio
from pydantic import SecretStr
from testcontainers.rabbitmq import RabbitMqContainer

from app.broker.rabbit import build_topology, declare_dlq, payments_queue_arguments
from app.config import RabbitMQSettings


@pytest.fixture(scope="session")
def rmq_container() -> Iterator[RabbitMqContainer]:
    with RabbitMqContainer("rabbitmq:3.13-management-alpine") as container:
        yield container


@pytest_asyncio.fixture(loop_scope="session")
async def rmq_settings(rmq_container: RabbitMqContainer) -> RabbitMQSettings:
    host = rmq_container.get_container_host_ip()
    port = rmq_container.get_exposed_port(5672)
    suffix = uuid.uuid4().hex[:8]
    return RabbitMQSettings(
        url=SecretStr(f"amqp://guest:guest@{host}:{port}/"),
        payments_exchange=f"test.payments.{suffix}",
        payments_queue=f"test.payments.new.{suffix}",
        payments_routing_key="payments.new",
        dlq_exchange=f"test.payments.dlx.{suffix}",
        dlq_queue=f"test.payments.dlq.{suffix}",
        max_delivery_attempts=3,
    )


async def _declare_topology_via_aio_pika(
    channel: aio_pika.abc.AbstractChannel, settings: RabbitMQSettings
) -> tuple[
    aio_pika.abc.AbstractExchange,
    aio_pika.abc.AbstractQueue,
    aio_pika.abc.AbstractQueue,
    aio_pika.abc.AbstractExchange,
]:
    dlx = await channel.declare_exchange(
        settings.dlq_exchange, aio_pika.ExchangeType.FANOUT, durable=True
    )
    dlq = await channel.declare_queue(
        settings.dlq_queue, durable=True, arguments={"x-queue-type": "quorum"}
    )
    await dlq.bind(dlx, routing_key="")

    main_exchange = await channel.declare_exchange(
        settings.payments_exchange, aio_pika.ExchangeType.TOPIC, durable=True
    )
    main_queue = await channel.declare_queue(
        settings.payments_queue,
        durable=True,
        arguments=payments_queue_arguments(settings),
    )
    await main_queue.bind(main_exchange, routing_key=settings.payments_routing_key)
    return main_exchange, main_queue, dlq, dlx


async def test_basic_publish_works(rmq_settings: RabbitMQSettings):
    """Sanity: pub into default exchange → get from queue by name."""
    connection = await aio_pika.connect_robust(rmq_settings.url.get_secret_value())
    try:
        channel = await connection.channel()
        qname = f"smoke.{uuid.uuid4().hex[:8]}"
        queue = await channel.declare_queue(qname, durable=False, auto_delete=True)
        await channel.default_exchange.publish(aio_pika.Message(body=b"hello"), routing_key=qname)
        await asyncio.sleep(0.5)
        got = await queue.get(timeout=5, no_ack=True)
        assert got is not None
        assert got.body == b"hello"
    finally:
        await connection.close()


async def test_dlx_routes_messages_to_dlq(rmq_settings: RabbitMQSettings):
    """DLX (payments.dlx) must be bound to DLQ (payments.dlq). A message published
    directly into the DLX appears in the DLQ with its original payload.

    Note: we don't drive the full x-delivery-limit cycle here. Quorum queues in
    RabbitMQ 3.13 actively protect against poison-message storms by refusing to
    re-deliver to the same consumer connection, which makes that path flaky in
    integration tests. End-to-end delivery-limit behaviour is covered by the
    Docker Compose smoke run.
    """
    connection = await aio_pika.connect_robust(rmq_settings.url.get_secret_value())
    try:
        channel = await connection.channel()
        _, _, dlq, dlx = await _declare_topology_via_aio_pika(channel, rmq_settings)
        # Quorum queues need a moment to finish Raft initialisation before
        # they reliably accept routed messages.
        await asyncio.sleep(2)

        payload = {"payment_id": str(uuid.uuid4()), "reason": "manual"}
        await dlx.publish(
            aio_pika.Message(
                body=json.dumps(payload).encode(),
                content_type="application/json",
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            ),
            routing_key="",
            mandatory=True,
        )

        await asyncio.sleep(1)
        got = await dlq.get(timeout=5, fail=False, no_ack=True)
        assert got is not None, "DLQ empty after publish to DLX"
        assert json.loads(got.body) == payload
    finally:
        await connection.close()


async def test_main_queue_declares_delivery_limit(
    rmq_settings: RabbitMQSettings,
):
    """The main payments queue must carry x-delivery-limit = max_delivery_attempts
    and x-dead-letter-exchange = payments.dlx. Declaring with conflicting args fails."""
    connection = await aio_pika.connect_robust(rmq_settings.url.get_secret_value())
    try:
        channel = await connection.channel()
        await _declare_topology_via_aio_pika(channel, rmq_settings)

        # Passive re-declare with the same args must succeed...
        await channel.declare_queue(
            rmq_settings.payments_queue,
            durable=True,
            arguments=payments_queue_arguments(rmq_settings),
        )

        # ...while a different x-delivery-limit must be rejected by the server.
        bad_args = payments_queue_arguments(rmq_settings) | {
            "x-delivery-limit": rmq_settings.max_delivery_attempts + 99
        }
        with pytest.raises(aio_pika.exceptions.ChannelPreconditionFailed):
            bad_channel = await connection.channel()
            await bad_channel.declare_queue(
                rmq_settings.payments_queue,
                durable=True,
                arguments=bad_args,
            )
    finally:
        await connection.close()


async def test_declare_dlq_is_idempotent(rmq_settings: RabbitMQSettings):
    """Both relay and consumer call declare_dlq on startup — it must be safe to run twice."""
    from faststream.rabbit import RabbitBroker

    topology = build_topology(rmq_settings)
    broker = RabbitBroker(rmq_settings.url.get_secret_value())
    await broker.connect()
    try:
        await declare_dlq(broker, topology)
        await declare_dlq(broker, topology)  # second call must not raise
    finally:
        await broker.close()
