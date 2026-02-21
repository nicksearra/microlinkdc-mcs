"""
MicroLink MCS — Edge Orchestrator
Stream A · Task 7 · v1.0.0

Master process that runs on the edge controller (Raspberry Pi 5 / Intel NUC).
Coordinates all protocol adapters, manages the store-and-forward buffer,
publishes heartbeat messages, and monitors edge health.

This is the single entry point for the edge stack. In Docker, this is the
CMD for the `edge-orchestrator` container.

Dependencies:
    pip install paho-mqtt pyyaml psutil

Usage:
    python edge_orchestrator.py --config edge-config.yaml
"""

import asyncio
import json
import logging
import os
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Dict, List

import yaml
import psutil
import paho.mqtt.client as mqtt

# ─── Structured logging ───────────────────────────────────────────────────

class JSONFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps({
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "module": "edge_orchestrator",
            "msg": record.getMessage(),
        })

logger = logging.getLogger("edge_orchestrator")
handler = logging.StreamHandler()
handler.setFormatter(JSONFormatter())
logger.addHandler(handler)
logger.setLevel(logging.INFO)


# ═══════════════════════════════════════════════════════════════════════════
# STORE-AND-FORWARD BUFFER
# SQLite ring buffer for MQTT messages when cloud connection is lost.
# Capacity: 72 hours at full data rate (~3 GB for 1MW block).
# ═══════════════════════════════════════════════════════════════════════════

class StoreAndForwardBuffer:
    """SQLite-backed message buffer for offline resilience.

    When the MQTT bridge to cloud is disconnected, all adapter messages
    are captured here. When connection restores, messages are replayed
    in order with their original timestamps.
    """

    def __init__(self, db_path: str = "/data/buffer/message_buffer.db",
                 max_messages: int = 5_000_000):
        self.db_path = db_path
        self.max_messages = max_messages
        self._depth = 0
        self._oldest_ts = None
        self._replaying = False
        self._replay_batch_size = 500
        self._replay_delay_ms = 10  # Throttle replay to not overwhelm broker

        # Ensure directory exists
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()
        self._update_stats()

    def _init_db(self):
        """Create buffer table if not exists."""
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS buffer (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic TEXT NOT NULL,
                payload TEXT NOT NULL,
                qos INTEGER DEFAULT 0,
                retain INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_buffer_created
            ON buffer(created_at)
        """)
        self.conn.commit()

    def _update_stats(self):
        """Refresh buffer depth and oldest timestamp."""
        row = self.conn.execute("SELECT COUNT(*) FROM buffer").fetchone()
        self._depth = row[0] if row else 0

        row = self.conn.execute(
            "SELECT MIN(created_at) FROM buffer"
        ).fetchone()
        self._oldest_ts = row[0] if row and row[0] else None

    def store(self, topic: str, payload: str, qos: int = 0, retain: bool = False):
        """Store a message in the buffer."""
        now = datetime.now(timezone.utc).isoformat()
        try:
            self.conn.execute(
                "INSERT INTO buffer (topic, payload, qos, retain, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (topic, payload, qos, 1 if retain else 0, now),
            )
            self._depth += 1

            # Ring buffer: evict oldest if at capacity
            if self._depth > self.max_messages:
                self.conn.execute(
                    "DELETE FROM buffer WHERE id IN "
                    "(SELECT id FROM buffer ORDER BY id ASC LIMIT ?)",
                    (self._depth - self.max_messages,),
                )
                self._depth = self.max_messages

            # Commit in batches for performance
            if self._depth % 1000 == 0:
                self.conn.commit()

        except sqlite3.Error as e:
            logger.error(f"Buffer store error: {e}")

    def flush_commit(self):
        """Force commit any pending inserts."""
        try:
            self.conn.commit()
        except sqlite3.Error:
            pass

    async def replay(self, mqtt_client: mqtt.Client, connected_check) -> int:
        """Replay buffered messages to MQTT broker.

        Args:
            mqtt_client: Connected MQTT client
            connected_check: Callable that returns True if still connected

        Returns:
            Number of messages replayed
        """
        if self._depth == 0:
            return 0

        self._replaying = True
        replayed = 0
        logger.info(f"Starting buffer replay: {self._depth} messages")

        try:
            while connected_check():
                rows = self.conn.execute(
                    "SELECT id, topic, payload, qos, retain FROM buffer "
                    "ORDER BY id ASC LIMIT ?",
                    (self._replay_batch_size,),
                ).fetchall()

                if not rows:
                    break

                ids_to_delete = []
                for row_id, topic, payload, qos, retain in rows:
                    if not connected_check():
                        break

                    result = mqtt_client.publish(
                        topic, payload, qos=qos, retain=bool(retain),
                    )
                    if result.rc == mqtt.MQTT_ERR_SUCCESS:
                        ids_to_delete.append(row_id)
                        replayed += 1
                    else:
                        logger.warning(f"Replay publish failed: rc={result.rc}")
                        break

                    # Throttle to avoid overwhelming broker
                    if self._replay_delay_ms > 0:
                        await asyncio.sleep(self._replay_delay_ms / 1000)

                # Delete replayed messages
                if ids_to_delete:
                    placeholders = ",".join("?" * len(ids_to_delete))
                    self.conn.execute(
                        f"DELETE FROM buffer WHERE id IN ({placeholders})",
                        ids_to_delete,
                    )
                    self.conn.commit()

                if len(rows) < self._replay_batch_size:
                    break  # No more messages

        except Exception as e:
            logger.error(f"Replay error: {e}")
        finally:
            self._replaying = False
            self._update_stats()
            logger.info(f"Buffer replay complete: {replayed} messages sent, "
                        f"{self._depth} remaining")

        return replayed

    @property
    def stats(self) -> dict:
        return {
            "depth": self._depth,
            "capacity": self.max_messages,
            "oldest_ts": self._oldest_ts,
            "replay_active": self._replaying,
        }

    def close(self):
        """Clean shutdown."""
        try:
            self.conn.commit()
            self.conn.close()
        except sqlite3.Error:
            pass


# ═══════════════════════════════════════════════════════════════════════════
# CLOUD MQTT CONNECTION MANAGER
# Manages the bridge to the cloud broker with auto-reconnect.
# ═══════════════════════════════════════════════════════════════════════════

class CloudMQTTBridge:
    """Manages MQTT connection to the cloud broker.

    Subscribes to command topics and handles cloud→edge commands.
    Monitors connection state for store-and-forward triggering.
    """

    def __init__(self, config: dict, site_id: str, block_id: str,
                 buffer: StoreAndForwardBuffer):
        self.site_id = site_id
        self.block_id = block_id
        self.buffer = buffer
        self.connected = False
        self._was_connected = False

        cloud_cfg = config.get("cloud_mqtt", {})
        self.host = cloud_cfg.get("host", "mqtt.microlink.energy")
        self.port = cloud_cfg.get("port", 8883)
        self.use_tls = cloud_cfg.get("tls", True)
        self.ca_cert = cloud_cfg.get("ca_cert", "/etc/edge/certs/ca.pem")
        self.client_cert = cloud_cfg.get("client_cert", "/etc/edge/certs/edge.pem")
        self.client_key = cloud_cfg.get("client_key", "/etc/edge/certs/edge.key")

        self.client = mqtt.Client(
            client_id=f"edge-{site_id}-{block_id}",
            protocol=mqtt.MQTTv311,
            clean_session=False,  # Persistent session for QoS 1
        )

        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

        # Command handler callback (set by orchestrator)
        self.command_handler = None

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self.connected = True
            logger.info("Cloud MQTT connected")

            # Subscribe to command topics
            cmd_topic = f"microlink/{self.site_id}/{self.block_id}/command/#"
            client.subscribe(cmd_topic, qos=1)
            logger.info(f"Subscribed to: {cmd_topic}")

            # Trigger replay if we have buffered messages
            if self.buffer.stats["depth"] > 0:
                logger.info("Buffered messages detected — replay will start")
        else:
            logger.error(f"Cloud MQTT connect failed: rc={rc}")

    def _on_disconnect(self, client, userdata, rc):
        self.connected = False
        if rc != 0:
            logger.warning(f"Cloud MQTT disconnected: rc={rc}")

    def _on_message(self, client, userdata, msg):
        """Handle incoming commands from cloud."""
        try:
            payload = json.loads(msg.payload.decode())
            logger.info(f"Command received: {msg.topic} → {payload.get('cmd', '?')}")

            if self.command_handler:
                self.command_handler(msg.topic, payload)

        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            logger.error(f"Invalid command message: {e}")

    def connect(self):
        """Connect to cloud broker with TLS."""
        try:
            if self.use_tls:
                self.client.tls_set(
                    ca_certs=self.ca_cert,
                    certfile=self.client_cert,
                    keyfile=self.client_key,
                )

            self.client.connect_async(self.host, self.port, keepalive=60)
            self.client.loop_start()
            logger.info(f"Cloud MQTT connecting to {self.host}:{self.port}")

        except Exception as e:
            logger.error(f"Cloud MQTT connect error: {e}")

    def disconnect(self):
        self.client.loop_stop()
        self.client.disconnect()

    def publish_response(self, request_id: str, status: str,
                         reason: str = "", result: dict = None):
        """Publish command response back to cloud."""
        topic = f"microlink/{self.site_id}/{self.block_id}/command/response"
        payload = {
            "ts": datetime.now(timezone.utc).isoformat() + "Z",
            "request_id": request_id,
            "status": status,
            "reason": reason,
        }
        if result:
            payload["result"] = result
        self.client.publish(topic, json.dumps(payload), qos=1, retain=False)


# ═══════════════════════════════════════════════════════════════════════════
# LOCAL MQTT INTERCEPTOR
# Subscribes to all local adapter topics and forwards to cloud.
# When cloud is down, stores messages in buffer.
# ═══════════════════════════════════════════════════════════════════════════

class LocalMQTTInterceptor:
    """Intercepts messages from local adapters and forwards to cloud.

    Subscribes to all `microlink/#` topics on the local Mosquitto broker.
    If cloud is connected, forwards immediately. If not, stores in buffer.
    """

    def __init__(self, config: dict, cloud_bridge: CloudMQTTBridge,
                 buffer: StoreAndForwardBuffer):
        self.cloud = cloud_bridge
        self.buffer = buffer
        self.connected = False
        self._message_count = 0
        self._buffered_count = 0

        local_cfg = config.get("local_mqtt", {})
        self.client = mqtt.Client(
            client_id="edge-interceptor",
            protocol=mqtt.MQTTv311,
        )
        self.host = local_cfg.get("host", "localhost")
        self.port = local_cfg.get("port", 1883)

        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self.connected = True
            # Subscribe to ALL local adapter topics
            client.subscribe("microlink/#", qos=0)
            logger.info("Local MQTT interceptor connected — subscribing to microlink/#")

    def _on_message(self, client, userdata, msg):
        """Forward local messages to cloud or buffer."""
        self._message_count += 1

        # Don't forward command topics (those come FROM cloud)
        if "/command/" in msg.topic:
            return

        if self.cloud.connected:
            # Forward directly to cloud
            self.cloud.client.publish(
                msg.topic,
                msg.payload,
                qos=msg.qos,
                retain=msg.retain,
            )
        else:
            # Buffer for later replay
            self.buffer.store(
                msg.topic,
                msg.payload.decode() if isinstance(msg.payload, bytes) else msg.payload,
                qos=msg.qos,
                retain=msg.retain,
            )
            self._buffered_count += 1

    def connect(self):
        try:
            self.client.connect(self.host, self.port, 60)
            self.client.loop_start()
        except Exception as e:
            logger.error(f"Local MQTT connect error: {e}")

    def disconnect(self):
        self.client.loop_stop()
        self.client.disconnect()

    @property
    def stats(self):
        return {
            "messages_forwarded": self._message_count - self._buffered_count,
            "messages_buffered": self._buffered_count,
            "total": self._message_count,
        }


# ═══════════════════════════════════════════════════════════════════════════
# HEARTBEAT PUBLISHER
# ═══════════════════════════════════════════════════════════════════════════

class HeartbeatPublisher:
    """Publishes edge health heartbeats every 30 seconds."""

    def __init__(self, site_id: str, block_id: str, edge_id: str,
                 local_mqtt_host: str = "localhost",
                 local_mqtt_port: int = 1883):
        self.site_id = site_id
        self.block_id = block_id
        self.edge_id = edge_id
        self.start_time = time.monotonic()

        self.client = mqtt.Client(client_id="edge-heartbeat", protocol=mqtt.MQTTv311)
        self.host = local_mqtt_host
        self.port = local_mqtt_port

        # External references — set by orchestrator
        self.adapter_status_fn = None
        self.buffer_stats_fn = None
        self.cloud_connected_fn = None

    def connect(self):
        try:
            self.client.connect(self.host, self.port, 60)
            self.client.loop_start()
        except Exception as e:
            logger.error(f"Heartbeat MQTT connect error: {e}")

    def disconnect(self):
        self.client.loop_stop()
        self.client.disconnect()

    async def run(self, interval_s: int = 30):
        """Publish heartbeats at configured interval."""
        logger.info(f"Heartbeat publisher started: interval={interval_s}s")

        while True:
            try:
                self._publish()
            except Exception as e:
                logger.error(f"Heartbeat publish error: {e}")

            await asyncio.sleep(interval_s)

    def _publish(self):
        """Build and publish a heartbeat message."""
        now = datetime.now(timezone.utc)
        uptime_s = int(time.monotonic() - self.start_time)

        # Gather adapter statuses
        adapters = {}
        if self.adapter_status_fn:
            adapters = self.adapter_status_fn()

        # Buffer stats
        buffer = {"depth": 0, "capacity": 5000000, "oldest_ts": None,
                  "cloud_connected": True, "replay_active": False}
        if self.buffer_stats_fn:
            buf = self.buffer_stats_fn()
            buffer.update(buf)
        if self.cloud_connected_fn:
            buffer["cloud_connected"] = self.cloud_connected_fn()

        # System health
        system = {
            "cpu_pct": psutil.cpu_percent(interval=None),
            "mem_pct": psutil.virtual_memory().percent,
            "disk_pct": psutil.disk_usage("/data").percent if Path("/data").exists()
                        else psutil.disk_usage("/").percent,
            "temp_c": self._get_cpu_temp(),
        }

        payload = {
            "ts": now.strftime("%Y-%m-%dT%H:%M:%S.") +
                  f"{now.microsecond // 1000:03d}Z",
            "edge_id": self.edge_id,
            "uptime_s": uptime_s,
            "adapters": adapters,
            "buffer": buffer,
            "system": system,
        }

        topic = f"microlink/{self.site_id}/{self.block_id}/edge/heartbeat"
        self.client.publish(topic, json.dumps(payload), qos=1, retain=True)

    @staticmethod
    def _get_cpu_temp() -> float:
        """Read CPU temperature. Works on RPi and most Linux."""
        try:
            temps = psutil.sensors_temperatures()
            if "cpu_thermal" in temps:
                return temps["cpu_thermal"][0].current
            if "coretemp" in temps:
                return temps["coretemp"][0].current
            # Fallback: RPi thermal zone
            thermal_path = Path("/sys/class/thermal/thermal_zone0/temp")
            if thermal_path.exists():
                return float(thermal_path.read_text().strip()) / 1000.0
        except Exception:
            pass
        return 0.0


# ═══════════════════════════════════════════════════════════════════════════
# COMMAND HANDLER
# ═══════════════════════════════════════════════════════════════════════════

class CommandHandler:
    """Processes cloud→edge commands."""

    def __init__(self, cloud_bridge: CloudMQTTBridge):
        self.cloud = cloud_bridge

    def handle(self, topic: str, payload: dict):
        """Route and execute a command."""
        cmd = payload.get("cmd", "")
        request_id = payload.get("request_id", "unknown")
        params = payload.get("params", {})

        logger.info(f"Executing command: {cmd} (request_id={request_id})")

        handlers = {
            "config_reload": self._cmd_config_reload,
            "adapter_restart": self._cmd_adapter_restart,
            "buffer_flush": self._cmd_buffer_flush,
            "diagnostics_request": self._cmd_diagnostics,
        }

        handler_fn = handlers.get(cmd)
        if handler_fn:
            try:
                result = handler_fn(params)
                self.cloud.publish_response(request_id, "accepted",
                                            result=result)
            except Exception as e:
                self.cloud.publish_response(request_id, "error",
                                            reason=str(e))
        else:
            # mode_override and alarm_ack_sync go directly to PLC
            # via the Modbus adapter — not handled here
            if cmd in ("mode_override", "alarm_ack_sync"):
                self.cloud.publish_response(
                    request_id, "accepted",
                    reason=f"Command {cmd} forwarded to PLC interface",
                )
            else:
                self.cloud.publish_response(
                    request_id, "rejected",
                    reason=f"Unknown command: {cmd}",
                )

    def _cmd_config_reload(self, params: dict) -> dict:
        logger.info("Config reload requested")
        # In production: re-read YAML configs, restart adapter processes
        return {"action": "config_reload_scheduled"}

    def _cmd_adapter_restart(self, params: dict) -> dict:
        adapter = params.get("adapter", "")
        logger.info(f"Adapter restart requested: {adapter}")
        # In production: docker restart the specific adapter container
        return {"adapter": adapter, "action": "restart_scheduled"}

    def _cmd_buffer_flush(self, params: dict) -> dict:
        logger.info("Buffer flush requested")
        return {"action": "flush_triggered"}

    def _cmd_diagnostics(self, params: dict) -> dict:
        """Gather and return diagnostic information."""
        return {
            "hostname": os.uname().nodename,
            "platform": sys.platform,
            "python": sys.version,
            "cpu_count": psutil.cpu_count(),
            "mem_total_mb": round(psutil.virtual_memory().total / 1024 / 1024),
            "disk_total_gb": round(psutil.disk_usage("/").total / 1024 / 1024 / 1024, 1),
            "uptime_host_s": int(time.time() - psutil.boot_time()),
            "network_interfaces": {
                name: [addr.address for addr in addrs if addr.family == 2]
                for name, addrs in psutil.net_if_addrs().items()
            },
        }


# ═══════════════════════════════════════════════════════════════════════════
# EDGE ORCHESTRATOR — MAIN CLASS
# ═══════════════════════════════════════════════════════════════════════════

class EdgeOrchestrator:
    """Master process coordinating all edge components."""

    def __init__(self, config_path: str):
        with open(config_path, "r") as f:
            self.config = yaml.safe_load(f)

        self.site_id = self.config["site_id"]
        self.block_id = self.config["block_id"]
        self.edge_id = self.config.get("edge_id",
                                        f"edge-{self.site_id}-{self.block_id}")

        # Adapter process tracking
        self._adapter_processes: Dict[str, dict] = {}
        self._running = False

        # Initialize components
        self.buffer = StoreAndForwardBuffer(
            db_path=self.config.get("buffer", {}).get(
                "db_path", "/data/buffer/message_buffer.db"),
            max_messages=self.config.get("buffer", {}).get(
                "max_messages", 5_000_000),
        )

        self.cloud_bridge = CloudMQTTBridge(
            self.config, self.site_id, self.block_id, self.buffer,
        )

        self.interceptor = LocalMQTTInterceptor(
            self.config, self.cloud_bridge, self.buffer,
        )

        self.heartbeat = HeartbeatPublisher(
            self.site_id, self.block_id, self.edge_id,
            local_mqtt_host=self.config.get("local_mqtt", {}).get("host", "localhost"),
            local_mqtt_port=self.config.get("local_mqtt", {}).get("port", 1883),
        )

        self.command_handler = CommandHandler(self.cloud_bridge)
        self.cloud_bridge.command_handler = self.command_handler.handle

        # Wire up heartbeat data sources
        self.heartbeat.adapter_status_fn = self._get_adapter_statuses
        self.heartbeat.buffer_stats_fn = lambda: self.buffer.stats
        self.heartbeat.cloud_connected_fn = lambda: self.cloud_bridge.connected

    def _get_adapter_statuses(self) -> dict:
        """Collect status from all running adapter processes."""
        statuses = {}
        for name, info in self._adapter_processes.items():
            proc = info.get("process")
            running = proc is not None and proc.poll() is None if proc else False
            statuses[name] = {
                "status": "running" if running else "stopped",
                "pid": proc.pid if proc and running else None,
                "restarts": info.get("restarts", 0),
            }
        return statuses

    async def _start_adapter_process(self, name: str, cmd: list,
                                     restart_delay: int = 5,
                                     max_restarts: int = 10):
        """Start and supervise an adapter subprocess with auto-restart."""
        restarts = 0

        while self._running and restarts <= max_restarts:
            logger.info(f"Starting adapter: {name} (attempt {restarts + 1})")

            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                self._adapter_processes[name] = {
                    "process": proc,
                    "restarts": restarts,
                    "started_at": datetime.now(timezone.utc).isoformat(),
                }

                # Monitor process
                while self._running:
                    retcode = proc.poll()
                    if retcode is not None:
                        logger.warning(
                            f"Adapter {name} exited with code {retcode}")
                        break
                    await asyncio.sleep(1)

                if not self._running:
                    proc.terminate()
                    break

            except Exception as e:
                logger.error(f"Adapter {name} start failed: {e}")

            restarts += 1
            if restarts <= max_restarts and self._running:
                wait = min(restart_delay * restarts, 60)
                logger.info(f"Restarting {name} in {wait}s "
                            f"({restarts}/{max_restarts})")
                await asyncio.sleep(wait)

        if restarts > max_restarts:
            logger.error(f"Adapter {name} exceeded max restarts ({max_restarts})")

    async def _replay_loop(self):
        """Periodically check for buffered messages and replay when connected."""
        while self._running:
            await asyncio.sleep(10)

            if (self.cloud_bridge.connected and
                    self.buffer.stats["depth"] > 0 and
                    not self.buffer.stats["replay_active"]):
                await self.buffer.replay(
                    self.cloud_bridge.client,
                    lambda: self.cloud_bridge.connected and self._running,
                )

    async def start(self):
        """Start the entire edge stack."""
        logger.info(f"Edge Orchestrator starting: site={self.site_id} "
                    f"block={self.block_id} edge={self.edge_id}")
        self._running = True

        # Connect MQTT clients
        self.cloud_bridge.connect()
        self.interceptor.connect()
        self.heartbeat.connect()

        # Build task list
        tasks = []

        # Heartbeat
        tasks.append(asyncio.create_task(self.heartbeat.run(interval_s=30)))

        # Buffer replay loop
        tasks.append(asyncio.create_task(self._replay_loop()))

        # Start adapter subprocesses (if running standalone, not Docker)
        adapters = self.config.get("adapters", {})
        if adapters.get("mode", "docker") == "subprocess":
            for adapter_name, adapter_cfg in adapters.get("processes", {}).items():
                cmd = adapter_cfg.get("cmd", [])
                if cmd:
                    tasks.append(asyncio.create_task(
                        self._start_adapter_process(adapter_name, cmd)
                    ))

        logger.info(f"Edge Orchestrator running with {len(tasks)} tasks")

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            logger.info("Edge Orchestrator shutting down")
        finally:
            await self.stop()

    async def stop(self):
        """Graceful shutdown."""
        logger.info("Edge Orchestrator stopping...")
        self._running = False

        # Stop adapter subprocesses
        for name, info in self._adapter_processes.items():
            proc = info.get("process")
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                logger.info(f"Adapter {name} stopped")

        # Flush buffer
        self.buffer.flush_commit()
        self.buffer.close()

        # Disconnect MQTT
        self.heartbeat.disconnect()
        self.interceptor.disconnect()
        self.cloud_bridge.disconnect()

        logger.info("Edge Orchestrator stopped")


# ─── Entry point ───────────────────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser(description="MicroLink Edge Orchestrator")
    parser.add_argument("--config", type=str, default="/etc/edge/edge-config.yaml")
    parser.add_argument("--log-level", type=str, default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()
    logger.setLevel(getattr(logging, args.log_level))

    orchestrator = EdgeOrchestrator(args.config)

    loop = asyncio.get_running_loop()
    for sig_name in ("SIGINT", "SIGTERM"):
        try:
            loop.add_signal_handler(
                getattr(signal, sig_name),
                lambda: asyncio.create_task(orchestrator.stop()),
            )
        except (NotImplementedError, AttributeError):
            pass

    await orchestrator.start()


if __name__ == "__main__":
    asyncio.run(main())
