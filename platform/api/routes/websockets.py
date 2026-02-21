"""
WebSocket endpoints for real-time data streaming to Stream D dashboards.

WS /ws/telemetry/{block_slug} — live sensor values for a block
WS /ws/alarms — real-time alarm events (raise, ack, clear, shelve)

Both use Redis pub/sub as the transport from the ingestion/alarm services.
"""

import asyncio
import json
import logging
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from starlette.websockets import WebSocketState

router = APIRouter()
logger = logging.getLogger("mcs.ws")


@router.websocket("/ws/telemetry/{block_slug}")
async def ws_telemetry(
    websocket: WebSocket,
    block_slug: str,
    subsystem: Optional[str] = Query(None),
):
    """
    Real-time telemetry stream for a block.

    Subscribes to Redis channel mcs:telemetry:{block_slug} and forwards
    messages to the WebSocket client. Stream D's NOC dashboard connects here.

    Optional `subsystem` filter to reduce bandwidth (e.g., only thermal-l1).
    """
    await websocket.accept()
    redis = websocket.app.state.redis
    channel = f"mcs:telemetry:{block_slug}"

    logger.info("WS telemetry connected: block=%s subsystem=%s", block_slug, subsystem)

    pubsub = redis.pubsub()
    await pubsub.subscribe(channel)

    try:
        async for message in pubsub.listen():
            if websocket.client_state != WebSocketState.CONNECTED:
                break
            if message["type"] != "message":
                continue

            # Optional subsystem filter
            if subsystem:
                try:
                    data = json.loads(message["data"])
                    if data.get("subsystem") != subsystem:
                        continue
                except (json.JSONDecodeError, TypeError):
                    pass

            await websocket.send_text(message["data"])
    except WebSocketDisconnect:
        logger.info("WS telemetry disconnected: block=%s", block_slug)
    except Exception:
        logger.exception("WS telemetry error: block=%s", block_slug)
    finally:
        await pubsub.unsubscribe(channel)


@router.websocket("/ws/alarms")
async def ws_alarms(
    websocket: WebSocket,
    block_slug: Optional[str] = Query(None),
    min_priority: Optional[str] = Query(None, description="Minimum priority: P0, P1, P2, P3"),
):
    """
    Real-time alarm event stream.

    Subscribes to Redis channel mcs:alarms:outbound (published by the alarm engine)
    and forwards to WebSocket clients. Stream D's alarm banner connects here.

    Optional filters:
      - block_slug: only alarms for this block
      - min_priority: P0 = only critical, P1 = critical+high, etc.
    """
    await websocket.accept()
    redis = websocket.app.state.redis
    channel = "mcs:alarms:outbound"

    priority_levels = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    min_level = priority_levels.get(min_priority, 3) if min_priority else 3

    logger.info("WS alarms connected: block=%s min_priority=%s", block_slug, min_priority)

    pubsub = redis.pubsub()
    await pubsub.subscribe(channel)

    try:
        async for message in pubsub.listen():
            if websocket.client_state != WebSocketState.CONNECTED:
                break
            if message["type"] != "message":
                continue

            try:
                data = json.loads(message["data"])
                alarm = data.get("alarm", {})

                # Block filter
                if block_slug and alarm.get("block_id") != block_slug:
                    continue

                # Priority filter
                alarm_priority = alarm.get("priority", "P3")
                alarm_level = priority_levels.get(alarm_priority, 3)
                if alarm_level > min_level:
                    continue

            except (json.JSONDecodeError, TypeError):
                pass

            await websocket.send_text(message["data"])
    except WebSocketDisconnect:
        logger.info("WS alarms disconnected")
    except Exception:
        logger.exception("WS alarms error")
    finally:
        await pubsub.unsubscribe(channel)


@router.websocket("/ws/events/{block_slug}")
async def ws_events(websocket: WebSocket, block_slug: str):
    """
    Real-time event stream for a block — mode changes, equipment state,
    operator actions. Used by Stream D's event timeline widget.
    """
    await websocket.accept()
    redis = websocket.app.state.redis
    channel = f"mcs:events:{block_slug}"

    pubsub = redis.pubsub()
    await pubsub.subscribe(channel)

    try:
        async for message in pubsub.listen():
            if websocket.client_state != WebSocketState.CONNECTED:
                break
            if message["type"] != "message":
                continue
            await websocket.send_text(message["data"])
    except WebSocketDisconnect:
        pass
    finally:
        await pubsub.unsubscribe(channel)
