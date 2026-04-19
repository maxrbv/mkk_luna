import asyncio
import contextlib
import logging
import signal
from pathlib import Path

from app.broker.dlq_monitor import DLQMonitor
from app.broker.rabbit import build_broker, build_topology, declare_dlq
from app.config import load_settings
from app.database import create_database
from app.healthcheck import Heartbeat
from app.logging_config import setup_logging
from app.outbox.relay import OutboxRelay

HEARTBEAT_PATH = Path("/tmp/relay.healthy")

log = logging.getLogger(__name__)


async def main() -> None:
    settings = load_settings()
    setup_logging(settings.app.log_level)

    database = create_database(settings.database)
    topology = build_topology(settings.rabbitmq)
    broker = build_broker(settings.rabbitmq)

    await broker.connect()
    await declare_dlq(broker, topology)
    await broker.declare_exchange(topology.payments_exchange)
    await broker.declare_queue(topology.payments_queue)

    relay = OutboxRelay(database.sessionmaker, broker, topology, settings.outbox)
    relay.start()

    dlq_monitor = DLQMonitor(
        settings.rabbitmq.url.get_secret_value(),
        settings.rabbitmq.dlq_queue,
        settings.rabbitmq.dlq_alert_threshold,
        settings.rabbitmq.dlq_check_interval_seconds,
    )
    dlq_monitor.start()

    heartbeat = Heartbeat(HEARTBEAT_PATH)
    heartbeat.start()
    log.info("Outbox relay service ready")

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        # Windows: add_signal_handler is not supported; rely on KeyboardInterrupt
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)

    try:
        await stop_event.wait()
    finally:
        log.info("Shutting down outbox relay service")
        await heartbeat.stop()
        await dlq_monitor.stop()
        await relay.stop()
        await broker.close()
        await database.dispose()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt, SystemExit):
        asyncio.run(main())
