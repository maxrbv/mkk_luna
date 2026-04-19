import asyncio
import contextlib
import logging
from pathlib import Path

import httpx
from faststream import FastStream

from app.broker.rabbit import build_broker, build_topology, declare_dlq
from app.config import load_settings
from app.consumer.handlers import register_handlers
from app.database import create_database
from app.healthcheck import Heartbeat
from app.logging_config import setup_logging
from app.webhook.sender import WebhookSender

log = logging.getLogger(__name__)

HEARTBEAT_PATH = Path("/tmp/consumer.healthy")


async def main() -> None:
    settings = load_settings()
    setup_logging(settings.app.log_level)

    database = create_database(settings.database)
    topology = build_topology(settings.rabbitmq)
    broker = build_broker(settings.rabbitmq)

    async with httpx.AsyncClient() as http_client:
        webhook_sender = WebhookSender(http_client, settings.webhook)
        register_handlers(
            broker,
            topology,
            database,
            webhook_sender,
            settings.payment_processor,
        )

        # DLX/DLQ must exist before the main queue (with x-dead-letter-exchange)
        # is declared by the subscriber on broker startup.
        await broker.connect()
        await declare_dlq(broker, topology)

        app = FastStream(broker)
        heartbeat = Heartbeat(HEARTBEAT_PATH)

        @app.after_startup
        async def _after_startup() -> None:
            heartbeat.start()
            log.info("Consumer started")

        @app.on_shutdown
        async def _on_shutdown() -> None:
            await heartbeat.stop()
            await database.dispose()
            log.info("Consumer stopped")

        await app.run()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt, SystemExit):
        asyncio.run(main())
