import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, SecretStr


class AppSettings(BaseModel):
    name: str = "payments-service"
    api_key: SecretStr
    log_level: str = "INFO"
    rate_limit_capacity: int = 20
    rate_limit_refill_per_second: float = 10.0


class DatabaseSettings(BaseModel):
    dsn: SecretStr
    pool_size: int = 10
    max_overflow: int = 5


class RabbitMQSettings(BaseModel):
    url: SecretStr
    payments_exchange: str = "payments"
    payments_queue: str = "payments.new"
    payments_routing_key: str = "payments.new"
    dlq_exchange: str = "payments.dlx"
    dlq_queue: str = "payments.dlq"
    max_delivery_attempts: int = 3
    dlq_alert_threshold: int = 10
    dlq_check_interval_seconds: float = 30.0


class OutboxSettings(BaseModel):
    poll_interval_seconds: float = 1.0
    batch_size: int = 50
    max_publish_attempts: int = 10


class PaymentProcessorSettings(BaseModel):
    min_delay_seconds: float = 2.0
    max_delay_seconds: float = 5.0
    success_rate: float = Field(0.9, ge=0.0, le=1.0)


class WebhookSettings(BaseModel):
    timeout_seconds: float = 5.0
    max_retries: int = 3
    backoff_base_seconds: float = 1.0


class Settings(BaseModel):
    app: AppSettings
    database: DatabaseSettings
    rabbitmq: RabbitMQSettings
    outbox: OutboxSettings = OutboxSettings()
    payment_processor: PaymentProcessorSettings = PaymentProcessorSettings()
    webhook: WebhookSettings = WebhookSettings()


def load_settings(path: str | Path | None = None) -> Settings:
    config_path = Path(path or os.environ.get("CONFIG_PATH", "config.yaml"))
    if not config_path.is_file():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    return Settings.model_validate(raw)
