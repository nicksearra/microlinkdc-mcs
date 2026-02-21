"""
MCS Stream B — Task 5: REST API + WebSocket

FastAPI application serving the data platform API that all other streams consume.
This is the contract boundary — Stream C (billing/SLA) and Stream D (dashboards)
build against these endpoints.

Endpoints:
  GET  /sites, /sites/{slug}
  GET  /blocks, /blocks/{slug}
  GET  /equipment/{block_slug}
  GET  /sensors/{block_slug}
  GET  /telemetry?sensor_id=X&start=T1&end=T2&agg=5min
  GET  /telemetry/latest?block_slug=X
  GET  /alarms?state=ACTIVE&priority=P0,P1
  POST /alarms/{sensor_id}/acknowledge
  POST /alarms/{sensor_id}/shelve
  GET  /events?block_slug=X&type=mode_change
  GET  /billing/kwh?block_slug=X&start=T1&end=T2
  GET  /billing/kwht?block_slug=X&start=T1&end=T2
  GET  /health
  GET  /stats
  WS   /ws/telemetry/{block_slug}
  WS   /ws/alarms
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
import redis.asyncio as aioredis

from ingestion.config import settings

logger = logging.getLogger("mcs.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown lifecycle — create DB pool, Redis, alarm engine ref."""
    logger.info("MCS API starting...")

    # Database
    engine = create_async_engine(
        settings.database_url,
        pool_size=20,
        max_overflow=10,
        pool_pre_ping=True,
        pool_recycle=300,
    )
    app.state.db_engine = engine
    app.state.db_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    # Redis (for WebSocket pub/sub)
    app.state.redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)

    logger.info("MCS API ready — pool_size=20")
    yield

    # Shutdown
    await app.state.redis.aclose()
    await engine.dispose()
    logger.info("MCS API stopped")


def create_app() -> FastAPI:
    app = FastAPI(
        title="MicroLink Control System — Data Platform API",
        description=(
            "Stream B data platform API. Provides telemetry, alarms, "
            "billing views, and real-time WebSocket feeds for all MCS consumers."
        ),
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # CORS — allow Stream D frontends
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # Tighten in production
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Register routers
    from .routes import sites, blocks, telemetry, alarms, events, billing, health, websockets
    app.include_router(sites.router, prefix="/api/v1", tags=["Sites"])
    app.include_router(blocks.router, prefix="/api/v1", tags=["Blocks"])
    app.include_router(telemetry.router, prefix="/api/v1", tags=["Telemetry"])
    app.include_router(alarms.router, prefix="/api/v1", tags=["Alarms"])
    app.include_router(events.router, prefix="/api/v1", tags=["Events"])
    app.include_router(billing.router, prefix="/api/v1", tags=["Billing"])
    app.include_router(health.router, tags=["Health"])
    app.include_router(websockets.router, prefix="/api/v1", tags=["WebSocket"])

    return app


app = create_app()
