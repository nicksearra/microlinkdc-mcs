"""
MCS Stream B — Metrics

Exposes Prometheus-compatible /metrics endpoint for Grafana dashboards.
Runs on a separate port so it doesn't interfere with the ingestion hot path.
"""

import asyncio
import logging
from aiohttp import web

from .config import settings

logger = logging.getLogger("mcs.metrics")

# Metrics state — populated by the ingestor
_metrics: dict = {}


def update_metrics(data: dict) -> None:
    """Called periodically by the ingestor to refresh metric values."""
    _metrics.update(data)


async def _handle_metrics(request: web.Request) -> web.Response:
    """Prometheus text exposition format."""
    lines = []

    # Ingestion throughput
    lines.append(f'mcs_telemetry_rows_written_total {_metrics.get("rows_written", 0)}')
    lines.append(f'mcs_telemetry_rows_dropped_total {_metrics.get("rows_dropped", 0)}')
    lines.append(f'mcs_telemetry_flush_count_total {_metrics.get("flushes", 0)}')
    lines.append(f'mcs_telemetry_flush_errors_total {_metrics.get("flush_errors", 0)}')
    lines.append(f'mcs_telemetry_last_flush_ms {_metrics.get("last_flush_ms", 0)}')
    lines.append(f'mcs_telemetry_last_flush_rows {_metrics.get("last_flush_rows", 0)}')

    # MQTT
    lines.append(f'mcs_mqtt_messages_received_total {_metrics.get("mqtt_received", 0)}')

    # Cache
    lines.append(f'mcs_sensor_cache_hits_total {_metrics.get("cache_hits", 0)}')
    lines.append(f'mcs_sensor_cache_misses_total {_metrics.get("cache_misses", 0)}')
    lines.append(f'mcs_sensor_cache_hit_rate {_metrics.get("cache_hit_rate", 0)}')
    lines.append(f'mcs_sensor_cache_local_size {_metrics.get("cache_local_size", 0)}')

    # DLQ
    lines.append(f'mcs_dead_letter_count_total {_metrics.get("dlq_count", 0)}')

    # Alarms
    lines.append(f'mcs_alarm_signals_published_total {_metrics.get("alarms_published", 0)}')

    # Buffer
    lines.append(f'mcs_write_buffer_size {_metrics.get("buffer_size", 0)}')

    body = "\n".join(lines) + "\n"
    return web.Response(text=body, content_type="text/plain")


async def _handle_health(request: web.Request) -> web.Response:
    """Health check endpoint."""
    return web.json_response({"status": "ok", "metrics": _metrics})


async def start_metrics_server() -> web.AppRunner:
    """Start the metrics HTTP server on a background port."""
    app = web.Application()
    app.router.add_get("/metrics", _handle_metrics)
    app.router.add_get("/health", _handle_health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", settings.METRICS_PORT)
    await site.start()
    logger.info("Metrics server listening on :%d", settings.METRICS_PORT)
    return runner
