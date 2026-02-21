"""
MCS Stream B — Ingestion Service Configuration
Centralises all environment-driven settings with sensible defaults.
"""

from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    """All config sourced from environment / .env file."""

    # ── MQTT ─────────────────────────────────────────────────────────────
    MQTT_BROKER: str = "mosquitto"
    MQTT_PORT: int = 1883
    MQTT_USERNAME: Optional[str] = None
    MQTT_PASSWORD: Optional[str] = None
    MQTT_CLIENT_ID: str = "mcs-ingestor-01"
    MQTT_TOPIC_ROOT: str = "microlink/#"
    MQTT_QOS: int = 1
    MQTT_KEEPALIVE: int = 60

    # ── TimescaleDB ──────────────────────────────────────────────────────
    DB_HOST: str = "timescaledb"
    DB_PORT: int = 5432
    DB_NAME: str = "mcs"
    DB_USER: str = "mcs_ingestor"
    DB_PASSWORD: str = "changeme"
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.DB_USER}:{self.DB_PASSWORD}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"
        )

    @property
    def database_url_sync(self) -> str:
        return (
            f"postgresql+psycopg2://{self.DB_USER}:{self.DB_PASSWORD}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"
        )

    # ── Redis (sensor cache + alarm pub/sub) ─────────────────────────────
    REDIS_URL: str = "redis://redis:6379/0"
    SENSOR_CACHE_TTL: int = 300  # seconds — cache sensor PK lookups

    # ── Batch writer tuning ──────────────────────────────────────────────
    BATCH_SIZE: int = 2000          # flush after N rows
    BATCH_FLUSH_INTERVAL: float = 1.0  # flush every N seconds (whichever first)
    BATCH_MAX_PENDING: int = 50_000    # backpressure limit — drop if exceeded

    # ── Dead letter ──────────────────────────────────────────────────────
    DLQ_ENABLED: bool = True

    # ── Alarm detection ──────────────────────────────────────────────────
    ALARM_REDIS_CHANNEL: str = "mcs:alarms:inbound"

    # ── Observability ────────────────────────────────────────────────────
    LOG_LEVEL: str = "INFO"
    METRICS_PORT: int = 9090  # Prometheus /metrics

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
