from collections.abc import AsyncIterator

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.api.health import router as health_router
from app.api.payments import router as payments_router
from app.api.rate_limit import TokenBucket
from app.config import (
    AppSettings,
    DatabaseSettings,
    RabbitMQSettings,
    Settings,
)
from app.database import Database

TEST_API_KEY = "test-api-key"


@pytest_asyncio.fixture(loop_scope="session")
async def settings() -> Settings:
    return Settings(
        app=AppSettings(
            api_key=TEST_API_KEY,
            rate_limit_capacity=1000,
            rate_limit_refill_per_second=1000.0,
        ),
        database=DatabaseSettings(dsn="postgresql+asyncpg://unused"),
        rabbitmq=RabbitMQSettings(url="amqp://unused"),
    )


@pytest_asyncio.fixture(loop_scope="session")
async def api_client(
    settings: Settings,
    database: Database,
    engine: AsyncEngine,
) -> AsyncIterator[AsyncClient]:
    app = FastAPI()
    app.state.settings = settings
    app.state.database = database
    app.state.rate_limiter = TokenBucket(
        settings.app.rate_limit_capacity,
        settings.app.rate_limit_refill_per_second,
    )
    app.include_router(health_router)
    app.include_router(payments_router)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE payments, outbox RESTART IDENTITY CASCADE"))
