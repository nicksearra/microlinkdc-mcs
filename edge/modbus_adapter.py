"""
MicroLink MCS — Modbus RTU/TCP Protocol Adapter
Stream A · Task 4 · v1.0.0

Reads Modbus registers from configured devices and publishes normalised
telemetry messages to the local MQTT broker following the MCS MQTT schema.

Supports both RTU (serial RS-485) and TCP modes. Config-driven register
mapping loaded from YAML. Async design with polling groups.

Dependencies:
    pip install pymodbus paho-mqtt pyyaml

Usage:
    python modbus_adapter.py --config modbus-config.yaml
"""

import asyncio
import json
import logging
import struct
import time
import argparse
from datetime import datetime, timezone
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

import yaml
from pymodbus.client import AsyncModbusTcpClient, AsyncModbusSerialClient
from pymodbus.exceptions import ModbusException, ConnectionException
import paho.mqtt.client as mqtt

# ─── Structured JSON logging ───────────────────────────────────────────────

class JSONFormatter(logging.Formatter):
    def format(self, record):
        log_entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "module": "modbus_adapter",
            "msg": record.getMessage(),
        }
        if hasattr(record, "device"):
            log_entry["device"] = record.device
        if hasattr(record, "tag"):
            log_entry["tag"] = record.tag
        if record.exc_info and record.exc_info[0]:
            log_entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_entry)


logger = logging.getLogger("modbus_adapter")
handler = logging.StreamHandler()
handler.setFormatter(JSONFormatter())
logger.addHandler(handler)
logger.setLevel(logging.INFO)

# ─── Enums and data classes ────────────────────────────────────────────────

class Quality(str, Enum):
    GOOD = "GOOD"
    UNCERTAIN = "UNCERTAIN"
    BAD = "BAD"


class DataType(str, Enum):
    UINT16 = "UINT16"
    INT16 = "INT16"
    UINT32 = "UINT32"
    INT32 = "INT32"
    FLOAT32 = "FLOAT32"


class ByteOrder(str, Enum):
    BIG = "big"                 # AB CD  (Modbus default)
    LITTLE = "little"           # CD AB
    BIG_WORD_SWAP = "big_ws"    # CD AB  (big endian bytes, swapped words)
    LITTLE_WORD_SWAP = "lit_ws" # AB CD  (little endian bytes, swapped words)


@dataclass
class RegisterMapping:
    """A single Modbus register → MQTT sensor mapping."""
    tag: str
    description: str
    subsystem: str
    register: int           # Modbus register address (40001+ for holding)
    data_type: DataType
    unit: str
    scale: float = 1.0      # Multiply raw value by this
    offset: float = 0.0     # Add this after scaling
    range_min: float = -1e9
    range_max: float = 1e9
    poll_group: str = "normal"
    alarm_thresholds: dict = field(default_factory=dict)


@dataclass
class DeviceConfig:
    """Configuration for a single Modbus device."""
    name: str
    device_id: str
    mode: str               # "tcp" or "rtu"
    host: str = ""          # TCP host
    port: int = 502         # TCP port
    serial_port: str = ""   # RTU serial port (e.g., /dev/ttyUSB0)
    baudrate: int = 9600    # RTU baud
    slave_id: int = 1       # Modbus unit/slave ID
    byte_order: ByteOrder = ByteOrder.BIG
    timeout: float = 3.0
    registers: list = field(default_factory=list)  # List[RegisterMapping]


@dataclass
class DeviceMetrics:
    """Runtime metrics for a device."""
    reads_total: int = 0
    errors_total: int = 0
    last_read_ts: Optional[str] = None
    avg_latency_ms: float = 0.0
    consecutive_errors: int = 0
    _latency_samples: list = field(default_factory=list)

    def record_read(self, latency_ms: float):
        self.reads_total += 1
        self.consecutive_errors = 0
        self.last_read_ts = datetime.now(timezone.utc).isoformat()
        self._latency_samples.append(latency_ms)
        if len(self._latency_samples) > 100:
            self._latency_samples = self._latency_samples[-100:]
        self.avg_latency_ms = sum(self._latency_samples) / len(self._latency_samples)

    def record_error(self):
        self.errors_total += 1
        self.consecutive_errors += 1

    def to_dict(self):
        return {
            "reads_total": self.reads_total,
            "errors_total": self.errors_total,
            "last_read_ts": self.last_read_ts,
            "avg_latency_ms": round(self.avg_latency_ms, 1),
            "consecutive_errors": self.consecutive_errors,
        }


# ─── Configuration loader ─────────────────────────────────────────────────

def load_config(config_path: str) -> dict:
    """Load and validate adapter configuration from YAML."""
    with open(config_path, "r") as f:
        raw = yaml.safe_load(f)

    config = {
        "site_id": raw["site_id"],
        "block_id": raw["block_id"],
        "mqtt": raw.get("mqtt", {}),
        "polling_groups": raw.get("polling_groups", {
            "safety": 1000,
            "fast": 2000,
            "normal": 5000,
            "slow": 30000,
        }),
        "devices": [],
    }

    mqtt_defaults = {
        "host": "localhost",
        "port": 1883,
        "keepalive": 60,
        "client_id": f"modbus-adapter-{raw['site_id']}-{raw['block_id']}",
    }
    config["mqtt"] = {**mqtt_defaults, **config["mqtt"]}

    for dev_raw in raw.get("devices", []):
        byte_order = ByteOrder(dev_raw.get("byte_order", "big"))

        registers = []
        for reg_raw in dev_raw.get("registers", []):
            registers.append(RegisterMapping(
                tag=reg_raw["tag"],
                description=reg_raw.get("description", ""),
                subsystem=reg_raw["subsystem"],
                register=reg_raw["register"],
                data_type=DataType(reg_raw.get("data_type", "FLOAT32")),
                unit=reg_raw.get("unit", ""),
                scale=reg_raw.get("scale", 1.0),
                offset=reg_raw.get("offset", 0.0),
                range_min=reg_raw.get("range_min", -1e9),
                range_max=reg_raw.get("range_max", 1e9),
                poll_group=reg_raw.get("poll_group", "normal"),
                alarm_thresholds=reg_raw.get("alarm_thresholds", {}),
            ))

        device = DeviceConfig(
            name=dev_raw["name"],
            device_id=dev_raw.get("device_id", dev_raw["name"]),
            mode=dev_raw["mode"],
            host=dev_raw.get("host", ""),
            port=dev_raw.get("port", 502),
            serial_port=dev_raw.get("serial_port", ""),
            baudrate=dev_raw.get("baudrate", 9600),
            slave_id=dev_raw.get("slave_id", 1),
            byte_order=byte_order,
            timeout=dev_raw.get("timeout", 3.0),
            registers=registers,
        )
        config["devices"].append(device)

    logger.info(f"Loaded config: {len(config['devices'])} devices, "
                f"{sum(len(d.registers) for d in config['devices'])} registers")
    return config


# ─── Register value decoder ───────────────────────────────────────────────

def decode_registers(raw_registers: list, data_type: DataType,
                     byte_order: ByteOrder, scale: float, offset: float) -> float:
    """Decode raw Modbus register(s) to an engineering value."""

    if data_type == DataType.UINT16:
        value = raw_registers[0]

    elif data_type == DataType.INT16:
        value = raw_registers[0]
        if value > 32767:
            value -= 65536

    elif data_type in (DataType.UINT32, DataType.INT32, DataType.FLOAT32):
        if len(raw_registers) < 2:
            raise ValueError(f"Need 2 registers for {data_type}, got {len(raw_registers)}")

        r0, r1 = raw_registers[0], raw_registers[1]

        if byte_order == ByteOrder.BIG:
            # AB CD — standard big-endian (MSW first)
            raw_bytes = struct.pack(">HH", r0, r1)
        elif byte_order == ByteOrder.LITTLE:
            # CD AB — word-swapped
            raw_bytes = struct.pack(">HH", r1, r0)
        elif byte_order == ByteOrder.BIG_WORD_SWAP:
            # CD AB with big-endian bytes within words
            raw_bytes = struct.pack(">HH", r1, r0)
        elif byte_order == ByteOrder.LITTLE_WORD_SWAP:
            # AB CD with little-endian bytes within words
            raw_bytes = struct.pack("<HH", r0, r1)
        else:
            raw_bytes = struct.pack(">HH", r0, r1)

        if data_type == DataType.FLOAT32:
            value = struct.unpack(">f", raw_bytes)[0]
        elif data_type == DataType.UINT32:
            value = struct.unpack(">I", raw_bytes)[0]
        elif data_type == DataType.INT32:
            value = struct.unpack(">i", raw_bytes)[0]
    else:
        raise ValueError(f"Unsupported data type: {data_type}")

    return round((value * scale) + offset, 4)


def registers_needed(data_type: DataType) -> int:
    """How many 16-bit registers does this data type occupy."""
    if data_type in (DataType.UINT16, DataType.INT16):
        return 1
    return 2  # 32-bit types


# ─── Alarm evaluation ─────────────────────────────────────────────────────

def evaluate_alarm(value: float, thresholds: dict) -> Optional[str]:
    """Check value against alarm thresholds. Returns priority or None.

    Thresholds dict may contain: P0_high, P0_low, P1_high, P1_low, etc.
    Check highest priority first.
    """
    for priority in ["P0", "P1", "P2", "P3"]:
        high_key = f"{priority}_high"
        low_key = f"{priority}_low"
        if high_key in thresholds and value > thresholds[high_key]:
            return priority
        if low_key in thresholds and value < thresholds[low_key]:
            return priority
    return None


# ─── MQTT publisher ───────────────────────────────────────────────────────

class MQTTPublisher:
    """Manages connection to local Mosquitto broker and publishes messages."""

    def __init__(self, config: dict, site_id: str, block_id: str):
        self.site_id = site_id
        self.block_id = block_id
        self.client = mqtt.Client(
            client_id=config.get("client_id", "modbus-adapter"),
            protocol=mqtt.MQTTv311,
        )
        self.host = config.get("host", "localhost")
        self.port = config.get("port", 1883)
        self.keepalive = config.get("keepalive", 60)
        self.connected = False
        self._seq_counters = {}  # tag → sequence number
        self._publish_count = 0
        self._error_count = 0

        # Callbacks
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self.connected = True
            logger.info("MQTT connected to broker")
        else:
            logger.error(f"MQTT connection failed: rc={rc}")

    def _on_disconnect(self, client, userdata, rc):
        self.connected = False
        if rc != 0:
            logger.warning(f"MQTT disconnected unexpectedly: rc={rc}")

    def connect(self):
        """Connect to MQTT broker with retry."""
        try:
            self.client.connect(self.host, self.port, self.keepalive)
            self.client.loop_start()
            # Wait briefly for connection
            for _ in range(30):
                if self.connected:
                    return
                time.sleep(0.1)
            logger.warning("MQTT connect timeout — will retry in background")
        except Exception as e:
            logger.error(f"MQTT connect error: {e}")

    def disconnect(self):
        """Clean disconnect."""
        self.client.loop_stop()
        self.client.disconnect()

    def _next_seq(self, tag: str) -> int:
        seq = self._seq_counters.get(tag, 0)
        self._seq_counters[tag] = seq + 1
        return seq

    def publish_telemetry(self, subsystem: str, tag: str, value: float,
                          unit: str, quality: Quality,
                          alarm: Optional[str] = None):
        """Publish a telemetry message to the MQTT topic."""
        topic = f"microlink/{self.site_id}/{self.block_id}/{subsystem}/{tag}"
        payload = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") +
                  f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z",
            "v": value,
            "u": unit,
            "q": quality.value,
            "alarm": alarm,
            "seq": self._next_seq(tag),
        }

        try:
            result = self.client.publish(
                topic,
                json.dumps(payload),
                qos=0,      # Telemetry: QoS 0 for throughput
                retain=True, # Last known value available to new subscribers
            )
            if result.rc == mqtt.MQTT_ERR_SUCCESS:
                self._publish_count += 1
            else:
                self._error_count += 1
                logger.warning(f"MQTT publish failed: topic={topic} rc={result.rc}")
        except Exception as e:
            self._error_count += 1
            logger.error(f"MQTT publish exception: {e}")

    def publish_alarm(self, tag: str, subsystem: str, priority: str,
                      action: str, value: float, threshold: float,
                      direction: str, description: str):
        """Publish an alarm event message."""
        alarm_id = (f"{self.block_id}-{tag}-"
                    f"{int(datetime.now(timezone.utc).timestamp() * 1000)}")
        topic = f"microlink/{self.site_id}/{self.block_id}/alarms/{priority}"
        payload = {
            "ts": datetime.now(timezone.utc).isoformat() + "Z",
            "alarm_id": alarm_id,
            "action": action,
            "priority": priority,
            "sensor_tag": tag,
            "subsystem": subsystem,
            "value": value,
            "threshold": threshold,
            "direction": direction,
            "description": description,
        }

        try:
            self.client.publish(
                topic,
                json.dumps(payload),
                qos=1,       # Alarms: QoS 1 — must not be lost
                retain=False, # Alarms are events, not state
            )
        except Exception as e:
            logger.error(f"MQTT alarm publish error: {e}")

    @property
    def stats(self):
        return {
            "published": self._publish_count,
            "errors": self._error_count,
            "connected": self.connected,
        }


# ─── Modbus device reader ─────────────────────────────────────────────────

class ModbusDeviceReader:
    """Manages connection and reads for a single Modbus device."""

    def __init__(self, device: DeviceConfig):
        self.device = device
        self.client = None
        self.metrics = DeviceMetrics()
        self._connected = False
        self._backoff_s = 1.0
        self._max_backoff_s = 60.0
        # Track alarm states for edge detection (raise/clear)
        self._alarm_states = {}  # tag → current alarm priority or None

    async def connect(self):
        """Establish Modbus connection with backoff retry."""
        while True:
            try:
                if self.device.mode == "tcp":
                    self.client = AsyncModbusTcpClient(
                        host=self.device.host,
                        port=self.device.port,
                        timeout=self.device.timeout,
                    )
                elif self.device.mode == "rtu":
                    self.client = AsyncModbusSerialClient(
                        port=self.device.serial_port,
                        baudrate=self.device.baudrate,
                        timeout=self.device.timeout,
                        bytesize=8,
                        parity="N",
                        stopbits=1,
                    )
                else:
                    raise ValueError(f"Unknown mode: {self.device.mode}")

                connected = await self.client.connect()
                if connected:
                    self._connected = True
                    self._backoff_s = 1.0
                    logger.info(f"Modbus connected: {self.device.name} "
                                f"({self.device.mode})",
                                extra={"device": self.device.name})
                    return
                else:
                    raise ConnectionException("Connection returned False")

            except Exception as e:
                logger.warning(
                    f"Modbus connect failed for {self.device.name}: {e} — "
                    f"retry in {self._backoff_s}s",
                    extra={"device": self.device.name},
                )
                await asyncio.sleep(self._backoff_s)
                self._backoff_s = min(self._backoff_s * 2, self._max_backoff_s)

    async def disconnect(self):
        """Clean disconnect."""
        if self.client:
            self.client.close()
            self._connected = False

    async def read_register(self, reg: RegisterMapping) -> tuple:
        """Read a single register mapping. Returns (value, quality).

        Returns:
            (float, Quality) — decoded value and quality flag
        """
        count = registers_needed(reg.data_type)
        # Convert from point-schedule address (40001+) to zero-based
        address = reg.register - 40001 if reg.register >= 40001 else reg.register

        t_start = time.monotonic()
        try:
            if not self._connected or not self.client:
                await self.connect()

            response = await self.client.read_holding_registers(
                address=address,
                count=count,
                slave=self.device.slave_id,
            )

            latency_ms = (time.monotonic() - t_start) * 1000

            if response.isError():
                self.metrics.record_error()
                logger.warning(
                    f"Modbus read error: {self.device.name}/{reg.tag} "
                    f"addr={reg.register} — {response}",
                    extra={"device": self.device.name, "tag": reg.tag},
                )
                return 0.0, Quality.BAD

            # Decode raw registers to engineering value
            value = decode_registers(
                response.registers, reg.data_type,
                self.device.byte_order, reg.scale, reg.offset,
            )

            self.metrics.record_read(latency_ms)

            # Check range for quality
            if value < reg.range_min or value > reg.range_max:
                return value, Quality.UNCERTAIN
            return value, Quality.GOOD

        except ConnectionException:
            self.metrics.record_error()
            self._connected = False
            logger.warning(
                f"Modbus connection lost: {self.device.name} — will reconnect",
                extra={"device": self.device.name},
            )
            return 0.0, Quality.BAD

        except Exception as e:
            self.metrics.record_error()
            logger.error(
                f"Modbus read exception: {self.device.name}/{reg.tag} — {e}",
                extra={"device": self.device.name, "tag": reg.tag},
                exc_info=True,
            )
            return 0.0, Quality.BAD

    def check_alarm_transition(self, tag: str, new_alarm: Optional[str]) -> Optional[str]:
        """Detect alarm state transitions (raise/clear). Returns action or None."""
        prev = self._alarm_states.get(tag)
        self._alarm_states[tag] = new_alarm

        if prev is None and new_alarm is not None:
            return "RAISED"
        if prev is not None and new_alarm is None:
            return "CLEARED"
        if prev is not None and new_alarm is not None and prev != new_alarm:
            return "ESCALATED"
        return None


# ─── Polling group runner ──────────────────────────────────────────────────

async def run_poll_group(group_name: str, interval_ms: int,
                         device_readers: list,
                         publisher: MQTTPublisher):
    """Continuously poll all registers in a polling group at the configured interval."""

    logger.info(f"Poll group '{group_name}' started: interval={interval_ms}ms")
    interval_s = interval_ms / 1000.0

    while True:
        cycle_start = time.monotonic()

        for reader in device_readers:
            # Collect registers belonging to this poll group
            group_regs = [r for r in reader.device.registers
                          if r.poll_group == group_name]

            for reg in group_regs:
                value, quality = await reader.read_register(reg)

                # Evaluate alarm thresholds
                alarm = None
                if quality == Quality.GOOD and reg.alarm_thresholds:
                    alarm = evaluate_alarm(value, reg.alarm_thresholds)
                elif quality == Quality.BAD:
                    # Sensor fault — may itself be an alarm condition
                    # (handled by alarm engine in Stream B, but we flag it)
                    pass

                # Publish telemetry
                publisher.publish_telemetry(
                    subsystem=reg.subsystem,
                    tag=reg.tag,
                    value=value,
                    unit=reg.unit,
                    quality=quality,
                    alarm=alarm,
                )

                # Check for alarm transitions and publish alarm events
                action = reader.check_alarm_transition(reg.tag, alarm)
                if action and alarm:
                    # Determine direction and threshold
                    threshold = 0.0
                    direction = "HIGH"
                    for priority in ["P0", "P1", "P2", "P3"]:
                        h_key = f"{priority}_high"
                        l_key = f"{priority}_low"
                        if h_key in reg.alarm_thresholds and value > reg.alarm_thresholds[h_key]:
                            threshold = reg.alarm_thresholds[h_key]
                            direction = "HIGH"
                            break
                        if l_key in reg.alarm_thresholds and value < reg.alarm_thresholds[l_key]:
                            threshold = reg.alarm_thresholds[l_key]
                            direction = "LOW"
                            break

                    desc = (f"{reg.description} {direction} — "
                            f"{value}{reg.unit} {'exceeds' if direction == 'HIGH' else 'below'} "
                            f"{alarm} limit {threshold}{reg.unit}")

                    publisher.publish_alarm(
                        tag=reg.tag,
                        subsystem=reg.subsystem,
                        priority=alarm,
                        action=action,
                        value=value,
                        threshold=threshold,
                        direction=direction,
                        description=desc,
                    )
                elif action == "CLEARED":
                    # Find previous alarm priority for the clear message
                    prev = reader._alarm_states.get(reg.tag, "P3")
                    publisher.publish_alarm(
                        tag=reg.tag,
                        subsystem=reg.subsystem,
                        priority=prev or "P3",
                        action="CLEARED",
                        value=value,
                        threshold=0.0,
                        direction="HIGH",
                        description=f"{reg.description} returned to normal — {value}{reg.unit}",
                    )

        # Sleep for remainder of interval
        elapsed = time.monotonic() - cycle_start
        sleep_time = max(0, interval_s - elapsed)
        if elapsed > interval_s:
            logger.warning(
                f"Poll group '{group_name}' overrun: {elapsed*1000:.0f}ms > {interval_ms}ms"
            )
        await asyncio.sleep(sleep_time)


# ─── Main adapter orchestrator ─────────────────────────────────────────────

class ModbusAdapter:
    """Main adapter class. Loads config, creates readers, runs polling loops."""

    def __init__(self, config_path: str):
        self.config = load_config(config_path)
        self.site_id = self.config["site_id"]
        self.block_id = self.config["block_id"]
        self.publisher = MQTTPublisher(
            self.config["mqtt"], self.site_id, self.block_id,
        )
        self.readers: list[ModbusDeviceReader] = []
        self._running = False

        # Create a reader per device
        for dev_config in self.config["devices"]:
            self.readers.append(ModbusDeviceReader(dev_config))

    async def start(self):
        """Connect everything and start polling."""
        logger.info(f"ModbusAdapter starting: site={self.site_id} block={self.block_id}")
        self._running = True

        # Connect MQTT
        self.publisher.connect()

        # Connect all Modbus devices
        connect_tasks = [reader.connect() for reader in self.readers]
        await asyncio.gather(*connect_tasks, return_exceptions=True)

        # Build polling group tasks
        poll_tasks = []
        for group_name, interval_ms in self.config["polling_groups"].items():
            # Check if any device has registers in this group
            has_regs = any(
                any(r.poll_group == group_name for r in reader.device.registers)
                for reader in self.readers
            )
            if has_regs:
                poll_tasks.append(
                    asyncio.create_task(
                        run_poll_group(group_name, interval_ms,
                                       self.readers, self.publisher)
                    )
                )

        logger.info(f"Started {len(poll_tasks)} polling groups")

        # Run until cancelled
        try:
            await asyncio.gather(*poll_tasks)
        except asyncio.CancelledError:
            logger.info("ModbusAdapter shutting down")
        finally:
            await self.stop()

    async def stop(self):
        """Graceful shutdown."""
        self._running = False
        for reader in self.readers:
            await reader.disconnect()
        self.publisher.disconnect()
        logger.info("ModbusAdapter stopped")

    def get_status(self) -> dict:
        """Return adapter status for heartbeat reporting."""
        return {
            "status": "running" if self._running else "stopped",
            "devices_online": sum(1 for r in self.readers if r._connected),
            "devices_total": len(self.readers),
            "mqtt": self.publisher.stats,
            "devices": {
                r.device.name: r.metrics.to_dict() for r in self.readers
            },
        }


# ─── Entry point ───────────────────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser(description="MicroLink Modbus Adapter")
    parser.add_argument(
        "--config", type=str, default="modbus-config.yaml",
        help="Path to YAML configuration file",
    )
    parser.add_argument(
        "--log-level", type=str, default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    logger.setLevel(getattr(logging, args.log_level))

    adapter = ModbusAdapter(args.config)

    # Handle graceful shutdown on SIGINT/SIGTERM
    loop = asyncio.get_running_loop()
    for sig_name in ("SIGINT", "SIGTERM"):
        try:
            import signal
            loop.add_signal_handler(
                getattr(signal, sig_name),
                lambda: asyncio.create_task(adapter.stop()),
            )
        except (NotImplementedError, AttributeError):
            pass  # Windows doesn't support add_signal_handler

    await adapter.start()


if __name__ == "__main__":
    asyncio.run(main())
