from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.health import router as health_router
from app.api.payments import router as payments_router
from app.api.rate_limit import TokenBucket
from app.config import load_settings
from app.database import create_database
from app.logging_config import setup_logging


def create_app() -> FastAPI:
    settings = load_settings()
    setup_logging(settings.app.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        database = create_database(settings.database)
        app.state.settings = settings
        app.state.database = database
        app.state.rate_limiter = TokenBucket(
            settings.app.rate_limit_capacity,
            settings.app.rate_limit_refill_per_second,
        )
        try:
            yield
        finally:
            await database.dispose()

    app = FastAPI(title=settings.app.name, lifespan=lifespan)
    app.include_router(health_router)
    app.include_router(payments_router)
    return app


app = create_app()
