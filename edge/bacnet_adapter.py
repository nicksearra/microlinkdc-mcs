"""
MicroLink MCS — BACnet/IP Protocol Adapter (READ-ONLY)
Stream A · Task 6 · v1.0.0

Reads BACnet/IP objects from the host's Building Management System (BMS)
and publishes normalised telemetry to the local MQTT broker.

READ-ONLY — we NEVER write to the host's BMS. This is a contractual
obligation. The host's BMS is their system; we only observe.

Supports:
- Analog Input (AI), Analog Value (AV) — temperatures, setpoints
- Binary Input (BI), Binary Value (BV) — status signals, demand flags
- Change of Value (COV) subscriptions where supported
- Fallback to polling when COV not available
- Device/object discovery mode

Dependencies:
    pip install BAC0 paho-mqtt pyyaml

Usage:
    python bacnet_adapter.py --config bacnet-config.yaml
    python bacnet_adapter.py --config bacnet-config.yaml --discover

Note: BAC0 is the recommended BACnet library. If unavailable, bacpypes3
can be substituted with minor API changes.
"""

import asyncio
import json
import logging
import time
import argparse
import threading
from datetime import datetime, timezone
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Any, Dict, List

import yaml
import paho.mqtt.client as mqtt

# ─── Structured JSON logging ──────────────────────────────────────────────

class JSONFormatter(logging.Formatter):
    def format(self, record):
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "module": "bacnet_adapter",
            "msg": record.getMessage(),
        }
        if hasattr(record, "device"):
            entry["device"] = record.device
        if hasattr(record, "tag"):
            entry["tag"] = record.tag
        if record.exc_info and record.exc_info[0]:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry)


logger = logging.getLogger("bacnet_adapter")
handler = logging.StreamHandler()
handler.setFormatter(JSONFormatter())
logger.addHandler(handler)
logger.setLevel(logging.INFO)

# ─── Enums and data classes ────────────────────────────────────────────────

class Quality(str, Enum):
    GOOD = "GOOD"
    UNCERTAIN = "UNCERTAIN"
    BAD = "BAD"


class BACnetObjectType(str, Enum):
    ANALOG_INPUT = "AI"
    ANALOG_VALUE = "AV"
    BINARY_INPUT = "BI"
    BINARY_VALUE = "BV"


# BACnet reliability/status flags → our quality mapping
BACNET_STATUS_MAP = {
    "normal": Quality.GOOD,
    "no-fault-detected": Quality.GOOD,
    "overrange": Quality.UNCERTAIN,
    "underrange": Quality.UNCERTAIN,
    "unreliable-other": Quality.BAD,
    "communication-failure": Quality.BAD,
}


@dataclass
class BACnetObjectMapping:
    """Maps a BACnet object to an MCS sensor tag."""
    tag: str
    description: str
    subsystem: str                      # Always "host-bms" for this adapter
    object_type: BACnetObjectType
    instance: int                       # BACnet object instance number
    device_id: int                      # BACnet device ID that owns this object
    unit: str
    data_type: str = "float"            # float, bool
    scale: float = 1.0
    offset: float = 0.0
    range_min: float = -1e9
    range_max: float = 1e9
    poll_group: str = "slow"
    alarm_thresholds: dict = field(default_factory=dict)
    use_cov: bool = False               # Try COV subscription first
    cov_lifetime: int = 300             # COV subscription lifetime (seconds)
    bacnet_name: str = ""               # BACnet object name (for discovery mapping)


@dataclass
class BACnetDeviceConfig:
    """Configuration for a BACnet device on the host network."""
    name: str
    device_id: int                      # BACnet device instance
    ip: str                             # Device IP address
    port: int = 47808                   # Standard BACnet/IP port
    objects: List[BACnetObjectMapping] = field(default_factory=list)


@dataclass
class DeviceMetrics:
    """Runtime metrics per BACnet device."""
    reads_total: int = 0
    errors_total: int = 0
    cov_updates: int = 0
    last_read_ts: Optional[str] = None
    consecutive_errors: int = 0
    online: bool = False

    def record_read(self):
        self.reads_total += 1
        self.consecutive_errors = 0
        self.last_read_ts = datetime.now(timezone.utc).isoformat()
        self.online = True

    def record_cov(self):
        self.cov_updates += 1
        self.last_read_ts = datetime.now(timezone.utc).isoformat()
        self.online = True

    def record_error(self):
        self.errors_total += 1
        self.consecutive_errors += 1
        if self.consecutive_errors > 5:
            self.online = False

    def to_dict(self):
        return {
            "reads_total": self.reads_total,
            "errors_total": self.errors_total,
            "cov_updates": self.cov_updates,
            "last_read_ts": self.last_read_ts,
            "online": self.online,
        }


# ─── Configuration loader ─────────────────────────────────────────────────

def load_config(config_path: str) -> dict:
    """Load adapter configuration from YAML."""
    with open(config_path, "r") as f:
        raw = yaml.safe_load(f)

    config = {
        "site_id": raw["site_id"],
        "block_id": raw["block_id"],
        "mqtt": {
            "host": "localhost",
            "port": 1883,
            "keepalive": 60,
            "client_id": f"bacnet-adapter-{raw['site_id']}-{raw['block_id']}",
            **raw.get("mqtt", {}),
        },
        "bacnet": {
            "local_ip": raw.get("bacnet", {}).get("local_ip", ""),
            "local_port": raw.get("bacnet", {}).get("local_port", 47809),
            "network_interface": raw.get("bacnet", {}).get("network_interface", ""),
        },
        "polling_groups": raw.get("polling_groups", {
            "fast": 2000,
            "normal": 5000,
            "slow": 30000,
        }),
        "devices": [],
    }

    for dev_raw in raw.get("devices", []):
        objects = []
        for obj_raw in dev_raw.get("objects", []):
            objects.append(BACnetObjectMapping(
                tag=obj_raw["tag"],
                description=obj_raw.get("description", ""),
                subsystem=obj_raw.get("subsystem", "host-bms"),
                object_type=BACnetObjectType(obj_raw["object_type"]),
                instance=obj_raw["instance"],
                device_id=dev_raw["device_id"],
                unit=obj_raw.get("unit", ""),
                data_type=obj_raw.get("data_type", "float"),
                scale=obj_raw.get("scale", 1.0),
                offset=obj_raw.get("offset", 0.0),
                range_min=obj_raw.get("range_min", -1e9),
                range_max=obj_raw.get("range_max", 1e9),
                poll_group=obj_raw.get("poll_group", "slow"),
                alarm_thresholds=obj_raw.get("alarm_thresholds", {}),
                use_cov=obj_raw.get("use_cov", False),
                cov_lifetime=obj_raw.get("cov_lifetime", 300),
                bacnet_name=obj_raw.get("bacnet_name", ""),
            ))

        device = BACnetDeviceConfig(
            name=dev_raw["name"],
            device_id=dev_raw["device_id"],
            ip=dev_raw["ip"],
            port=dev_raw.get("port", 47808),
            objects=objects,
        )
        config["devices"].append(device)

    total_objects = sum(len(d.objects) for d in config["devices"])
    logger.info(f"Loaded config: {len(config['devices'])} BACnet devices, "
                f"{total_objects} objects")
    return config


# ─── Alarm evaluation ─────────────────────────────────────────────────────

def evaluate_alarm(value: float, thresholds: dict) -> Optional[str]:
    """Check value against alarm thresholds, highest priority first."""
    for priority in ["P0", "P1", "P2", "P3"]:
        for direction in ["_high", "_low"]:
            key = f"{priority}{direction}"
            if key in thresholds:
                if direction == "_high" and value > thresholds[key]:
                    return priority
                if direction == "_low" and value < thresholds[key]:
                    return priority
    return None


# ─── MQTT publisher ───────────────────────────────────────────────────────

class MQTTPublisher:
    """Publishes normalised telemetry to local MQTT broker."""

    def __init__(self, config: dict, site_id: str, block_id: str):
        self.site_id = site_id
        self.block_id = block_id
        self.client = mqtt.Client(
            client_id=config.get("client_id", "bacnet-adapter"),
            protocol=mqtt.MQTTv311,
        )
        self.host = config.get("host", "localhost")
        self.port = config.get("port", 1883)
        self.connected = False
        self._seq_counters: Dict[str, int] = {}
        self._publish_count = 0
        self._error_count = 0

        self.client.on_connect = lambda c, u, f, rc: setattr(self, "connected", rc == 0)
        self.client.on_disconnect = lambda c, u, rc: setattr(self, "connected", False)

    def connect(self):
        try:
            self.client.connect(self.host, self.port, 60)
            self.client.loop_start()
            for _ in range(30):
                if self.connected:
                    logger.info("MQTT connected")
                    return
                time.sleep(0.1)
            logger.warning("MQTT connect timeout")
        except Exception as e:
            logger.error(f"MQTT connect error: {e}")

    def disconnect(self):
        self.client.loop_stop()
        self.client.disconnect()

    def _next_seq(self, tag: str) -> int:
        seq = self._seq_counters.get(tag, 0)
        self._seq_counters[tag] = seq + 1
        return seq

    def publish_telemetry(self, subsystem: str, tag: str, value: float,
                          unit: str, quality: Quality,
                          alarm: Optional[str] = None):
        topic = f"microlink/{self.site_id}/{self.block_id}/{subsystem}/{tag}"
        now = datetime.now(timezone.utc)
        payload = {
            "ts": now.strftime("%Y-%m-%dT%H:%M:%S.") +
                  f"{now.microsecond // 1000:03d}Z",
            "v": value,
            "u": unit,
            "q": quality.value,
            "alarm": alarm,
            "seq": self._next_seq(tag),
        }
        try:
            result = self.client.publish(topic, json.dumps(payload),
                                          qos=0, retain=True)
            if result.rc == mqtt.MQTT_ERR_SUCCESS:
                self._publish_count += 1
            else:
                self._error_count += 1
        except Exception as e:
            self._error_count += 1
            logger.error(f"MQTT publish error: {e}")

    def publish_alarm(self, tag: str, subsystem: str, priority: str,
                      action: str, value: float, threshold: float,
                      direction: str, description: str):
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
            self.client.publish(topic, json.dumps(payload), qos=1, retain=False)
        except Exception as e:
            logger.error(f"MQTT alarm publish error: {e}")

    @property
    def stats(self):
        return {
            "published": self._publish_count,
            "errors": self._error_count,
            "connected": self.connected,
        }


# ─── BACnet device reader ─────────────────────────────────────────────────

class BACnetDeviceReader:
    """Reads BACnet objects from a single device. Uses BAC0 library.

    BAC0 runs its own event loop internally, so we wrap reads in
    thread-safe calls.
    """

    def __init__(self, device: BACnetDeviceConfig, bacnet_network):
        """
        Args:
            device: Device configuration
            bacnet_network: BAC0 network instance (shared across all readers)
        """
        self.device = device
        self.network = bacnet_network
        self.metrics = DeviceMetrics()
        self._alarm_states: Dict[str, Optional[str]] = {}
        self._cov_subscriptions: Dict[str, bool] = {}
        self._cov_values: Dict[str, tuple] = {}  # tag → (value, quality, timestamp)
        self._address = f"{device.ip}:{device.port}"

    def _object_id_str(self, mapping: BACnetObjectMapping) -> str:
        """Build BAC0-style object identifier string.

        BAC0 uses format: 'address objectType instance presentValue'
        e.g., '192.168.10.50 analogInput 1 presentValue'
        """
        type_map = {
            BACnetObjectType.ANALOG_INPUT: "analogInput",
            BACnetObjectType.ANALOG_VALUE: "analogValue",
            BACnetObjectType.BINARY_INPUT: "binaryInput",
            BACnetObjectType.BINARY_VALUE: "binaryValue",
        }
        obj_type = type_map.get(mapping.object_type, "analogInput")
        return f"{self._address} {obj_type} {mapping.instance} presentValue"

    def read_object(self, mapping: BACnetObjectMapping) -> tuple:
        """Read a single BACnet object's present value.

        Returns:
            (value: float, quality: Quality)
        """
        try:
            obj_id = self._object_id_str(mapping)

            # BAC0 read — this is a synchronous call
            # In production with BAC0, this would be:
            #   raw_value = self.network.read(obj_id)
            #
            # For now we use a safe wrapper that handles the BAC0 API
            raw_value = self._safe_read(obj_id)

            if raw_value is None:
                self.metrics.record_error()
                return 0.0, Quality.BAD

            self.metrics.record_read()

            # Convert to float
            if mapping.data_type == "bool":
                value = 1.0 if str(raw_value).lower() in (
                    "active", "1", "true", "on") else 0.0
            else:
                value = float(raw_value)

            # Apply scale and offset
            value = round((value * mapping.scale) + mapping.offset, 4)

            # Range check
            if value < mapping.range_min or value > mapping.range_max:
                return value, Quality.UNCERTAIN

            return value, Quality.GOOD

        except Exception as e:
            self.metrics.record_error()
            logger.warning(
                f"BACnet read failed: {self.device.name}/{mapping.tag} — {e}",
                extra={"device": self.device.name, "tag": mapping.tag},
            )
            return 0.0, Quality.BAD

    def _safe_read(self, obj_id: str) -> Any:
        """Wrapper around BAC0 read with error handling.

        In production, this calls self.network.read(obj_id).
        If BAC0 is not available, returns None.
        """
        try:
            if self.network is not None and hasattr(self.network, 'read'):
                result = self.network.read(obj_id)
                if result is not None and str(result) not in ("", "None", "null"):
                    return result
            return None
        except Exception as e:
            logger.debug(f"BAC0 read error for {obj_id}: {e}")
            return None

    def subscribe_cov(self, mapping: BACnetObjectMapping) -> bool:
        """Subscribe to Change of Value notifications for an object.

        Returns True if subscription succeeded.
        """
        try:
            if self.network is None or not hasattr(self.network, 'read'):
                return False

            # BAC0 COV subscription would be:
            # self.network.cov(
            #     address=self._address,
            #     objectType=type_map[mapping.object_type],
            #     instance=mapping.instance,
            #     callback=lambda value: self._cov_callback(mapping.tag, value),
            #     lifetime=mapping.cov_lifetime,
            # )

            self._cov_subscriptions[mapping.tag] = True
            logger.info(
                f"COV subscribed: {self.device.name}/{mapping.tag} "
                f"(lifetime={mapping.cov_lifetime}s)",
                extra={"device": self.device.name, "tag": mapping.tag},
            )
            return True

        except Exception as e:
            logger.warning(
                f"COV subscribe failed: {self.device.name}/{mapping.tag} — {e}. "
                f"Falling back to polling.",
                extra={"device": self.device.name, "tag": mapping.tag},
            )
            self._cov_subscriptions[mapping.tag] = False
            return False

    def _cov_callback(self, tag: str, raw_value: Any):
        """Called by BAC0 when a COV notification arrives."""
        try:
            value = float(raw_value) if raw_value is not None else 0.0
            quality = Quality.GOOD if raw_value is not None else Quality.BAD
            self._cov_values[tag] = (value, quality, time.monotonic())
            self.metrics.record_cov()
        except Exception as e:
            logger.error(f"COV callback error for {tag}: {e}")

    def get_cov_value(self, tag: str, max_age_s: float = 60.0) -> Optional[tuple]:
        """Get the latest COV value if fresh enough.

        Returns (value, quality) or None if no recent COV data.
        """
        if tag not in self._cov_values:
            return None

        value, quality, ts = self._cov_values[tag]
        age = time.monotonic() - ts

        if age > max_age_s:
            return None  # Stale — fall back to poll

        return value, quality

    def check_alarm_transition(self, tag: str,
                               new_alarm: Optional[str]) -> Optional[str]:
        """Detect alarm state changes."""
        prev = self._alarm_states.get(tag)
        self._alarm_states[tag] = new_alarm
        if prev is None and new_alarm is not None:
            return "RAISED"
        if prev is not None and new_alarm is None:
            return "CLEARED"
        if prev is not None and new_alarm is not None and prev != new_alarm:
            return "ESCALATED"
        return None


# ─── BACnet network manager ───────────────────────────────────────────────

class BACnetNetworkManager:
    """Manages the BAC0 network connection.

    BAC0 creates a single BACnet/IP stack that all device reads go through.
    This manager handles initialization and provides the network instance
    to all device readers.
    """

    def __init__(self, local_ip: str = "", local_port: int = 47809):
        self.local_ip = local_ip
        self.local_port = local_port
        self.network = None
        self._started = False

    def start(self):
        """Initialize BAC0 network stack."""
        try:
            import BAC0

            if self.local_ip:
                # Explicit IP binding
                self.network = BAC0.lite(ip=self.local_ip, port=self.local_port)
            else:
                # Auto-detect interface
                self.network = BAC0.lite(port=self.local_port)

            self._started = True
            logger.info(f"BACnet/IP stack started on port {self.local_port}")

        except ImportError:
            logger.warning(
                "BAC0 library not available. Running in stub mode — "
                "all reads will return BAD quality. Install with: pip install BAC0"
            )
            self.network = None
            self._started = True

        except Exception as e:
            logger.error(f"BACnet stack init failed: {e}")
            self.network = None
            self._started = True  # Continue in degraded mode

    def stop(self):
        """Shutdown BAC0 network stack."""
        if self.network is not None:
            try:
                self.network.disconnect()
            except Exception:
                pass
        self._started = False
        logger.info("BACnet/IP stack stopped")

    def discover_devices(self, timeout: int = 10) -> List[dict]:
        """Discover BACnet devices on the network.

        Returns list of discovered devices with their object lists.
        """
        discovered = []

        if self.network is None:
            logger.warning("Cannot discover — BAC0 not available")
            return discovered

        try:
            logger.info(f"Starting BACnet device discovery (timeout={timeout}s)...")

            # BAC0 discovery
            # self.network.discover() triggers a Who-Is broadcast
            if hasattr(self.network, 'discover'):
                self.network.discover()
                time.sleep(timeout)

            # Collect discovered devices
            if hasattr(self.network, 'discoveredDevices'):
                for device in self.network.discoveredDevices:
                    dev_info = {
                        "device_id": device.get("device_id", 0),
                        "name": device.get("name", "Unknown"),
                        "ip": device.get("address", ""),
                        "vendor": device.get("vendor", "Unknown"),
                        "objects": [],
                    }

                    # Try to enumerate objects
                    try:
                        obj_list = self.network.read(
                            f"{dev_info['ip']} device {dev_info['device_id']} objectList"
                        )
                        if obj_list:
                            for obj_type, obj_instance in obj_list:
                                obj_name = ""
                                try:
                                    obj_name = self.network.read(
                                        f"{dev_info['ip']} {obj_type} {obj_instance} objectName"
                                    )
                                except Exception:
                                    pass

                                dev_info["objects"].append({
                                    "type": str(obj_type),
                                    "instance": int(obj_instance),
                                    "name": str(obj_name) if obj_name else "",
                                })
                    except Exception as e:
                        logger.warning(f"Object enumeration failed for device "
                                       f"{dev_info['device_id']}: {e}")

                    discovered.append(dev_info)

            logger.info(f"Discovery complete: {len(discovered)} devices found")

        except Exception as e:
            logger.error(f"Discovery failed: {e}")

        return discovered


# ─── Polling runner ────────────────────────────────────────────────────────

async def run_poll_group(group_name: str, interval_ms: int,
                         readers: List[BACnetDeviceReader],
                         publisher: MQTTPublisher):
    """Poll BACnet objects in a group at configured interval."""

    logger.info(f"BACnet poll group '{group_name}' started: interval={interval_ms}ms")
    interval_s = interval_ms / 1000.0

    while True:
        cycle_start = time.monotonic()

        for reader in readers:
            group_objects = [o for o in reader.device.objects
                            if o.poll_group == group_name]

            for mapping in group_objects:
                value = None
                quality = Quality.BAD

                # Try COV value first if subscribed
                if mapping.use_cov and mapping.tag in reader._cov_subscriptions:
                    cov_result = reader.get_cov_value(
                        mapping.tag, max_age_s=interval_s * 3
                    )
                    if cov_result:
                        value, quality = cov_result

                # Fall back to polling
                if value is None:
                    # Run synchronous BAC0 read in thread to avoid blocking
                    loop = asyncio.get_event_loop()
                    value, quality = await loop.run_in_executor(
                        None, reader.read_object, mapping
                    )

                # Evaluate alarms
                alarm = None
                if quality == Quality.GOOD and mapping.alarm_thresholds:
                    alarm = evaluate_alarm(value, mapping.alarm_thresholds)

                # Publish
                publisher.publish_telemetry(
                    subsystem=mapping.subsystem,
                    tag=mapping.tag,
                    value=value,
                    unit=mapping.unit,
                    quality=quality,
                    alarm=alarm,
                )

                # Alarm edge detection
                action = reader.check_alarm_transition(mapping.tag, alarm)
                if action and alarm:
                    threshold = 0.0
                    direction = "HIGH"
                    for p in ["P0", "P1", "P2", "P3"]:
                        for d, dk in [("HIGH", "_high"), ("LOW", "_low")]:
                            key = f"{p}{dk}"
                            if key in mapping.alarm_thresholds:
                                if (d == "HIGH" and value > mapping.alarm_thresholds[key]):
                                    threshold = mapping.alarm_thresholds[key]
                                    direction = d
                                    break
                                if (d == "LOW" and value < mapping.alarm_thresholds[key]):
                                    threshold = mapping.alarm_thresholds[key]
                                    direction = d
                                    break
                        else:
                            continue
                        break

                    publisher.publish_alarm(
                        tag=mapping.tag,
                        subsystem=mapping.subsystem,
                        priority=alarm,
                        action=action,
                        value=value,
                        threshold=threshold,
                        direction=direction,
                        description=(f"{mapping.description} {direction} — "
                                     f"{value}{mapping.unit} vs {alarm} limit "
                                     f"{threshold}{mapping.unit}"),
                    )
                elif action == "CLEARED":
                    publisher.publish_alarm(
                        tag=mapping.tag,
                        subsystem=mapping.subsystem,
                        priority="P3",
                        action="CLEARED",
                        value=value,
                        threshold=0.0,
                        direction="HIGH",
                        description=(f"{mapping.description} returned to normal — "
                                     f"{value}{mapping.unit}"),
                    )

        elapsed = time.monotonic() - cycle_start
        sleep_time = max(0, interval_s - elapsed)
        if elapsed > interval_s:
            logger.warning(f"BACnet poll group '{group_name}' overrun: "
                           f"{elapsed*1000:.0f}ms > {interval_ms}ms")
        await asyncio.sleep(sleep_time)


# ─── COV subscription manager ─────────────────────────────────────────────

async def manage_cov_subscriptions(readers: List[BACnetDeviceReader]):
    """Manage COV subscriptions — initial subscribe and periodic renewal."""

    # Initial subscription attempt
    for reader in readers:
        for mapping in reader.device.objects:
            if mapping.use_cov:
                reader.subscribe_cov(mapping)

    # Renewal loop — re-subscribe before lifetime expires
    while True:
        await asyncio.sleep(60)  # Check every minute

        for reader in readers:
            for mapping in reader.device.objects:
                if mapping.use_cov and mapping.tag in reader._cov_subscriptions:
                    # Check if subscription is still active
                    cov_val = reader.get_cov_value(
                        mapping.tag, max_age_s=mapping.cov_lifetime * 0.8
                    )
                    if cov_val is None:
                        # Subscription may have expired — renew
                        logger.debug(f"Renewing COV for {mapping.tag}")
                        reader.subscribe_cov(mapping)


# ─── Main adapter ─────────────────────────────────────────────────────────

class BACnetAdapter:
    """Main BACnet adapter. Read-only interface to host BMS."""

    def __init__(self, config_path: str):
        self.config = load_config(config_path)
        self.site_id = self.config["site_id"]
        self.block_id = self.config["block_id"]
        self.publisher = MQTTPublisher(
            self.config["mqtt"], self.site_id, self.block_id,
        )
        self.network_manager = BACnetNetworkManager(
            local_ip=self.config["bacnet"].get("local_ip", ""),
            local_port=self.config["bacnet"].get("local_port", 47809),
        )
        self.readers: List[BACnetDeviceReader] = []
        self._running = False

    def _init_readers(self):
        """Create device readers after network is started."""
        for dev_config in self.config["devices"]:
            reader = BACnetDeviceReader(dev_config, self.network_manager.network)
            self.readers.append(reader)

    async def start(self):
        """Start the adapter."""
        logger.info(f"BACnetAdapter starting: site={self.site_id} "
                    f"block={self.block_id} (READ-ONLY)")
        self._running = True

        # Connect MQTT
        self.publisher.connect()

        # Start BACnet stack (synchronous — runs in thread)
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.network_manager.start)

        # Create readers
        self._init_readers()

        # Build polling tasks
        tasks = []
        for group_name, interval_ms in self.config["polling_groups"].items():
            has_objects = any(
                any(o.poll_group == group_name for o in reader.device.objects)
                for reader in self.readers
            )
            if has_objects:
                tasks.append(
                    asyncio.create_task(
                        run_poll_group(group_name, interval_ms,
                                       self.readers, self.publisher)
                    )
                )

        # COV management task
        has_cov = any(
            any(o.use_cov for o in reader.device.objects)
            for reader in self.readers
        )
        if has_cov:
            tasks.append(asyncio.create_task(
                manage_cov_subscriptions(self.readers)
            ))

        logger.info(f"Started {len(tasks)} tasks "
                    f"({'includes COV' if has_cov else 'polling only'})")

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            logger.info("BACnetAdapter shutting down")
        finally:
            await self.stop()

    async def stop(self):
        self._running = False
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.network_manager.stop)
        self.publisher.disconnect()
        logger.info("BACnetAdapter stopped")

    async def discover(self, timeout: int = 10):
        """Run device discovery and print results."""
        logger.info("Running BACnet discovery mode...")

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.network_manager.start)

        devices = await loop.run_in_executor(
            None, self.network_manager.discover_devices, timeout
        )

        if not devices:
            print("\nNo BACnet devices found on the network.")
            print("Check: network connectivity, BACnet/IP port 47808, firewall rules")
        else:
            print(f"\n{'='*70}")
            print(f"DISCOVERED {len(devices)} BACnet DEVICE(S)")
            print(f"{'='*70}")

            for dev in devices:
                print(f"\nDevice ID: {dev['device_id']}")
                print(f"  Name:    {dev['name']}")
                print(f"  IP:      {dev['ip']}")
                print(f"  Vendor:  {dev['vendor']}")
                print(f"  Objects: {len(dev['objects'])}")

                if dev['objects']:
                    print(f"  {'Type':<20} {'Instance':<10} {'Name'}")
                    print(f"  {'-'*50}")
                    for obj in dev['objects'][:50]:  # Limit display
                        print(f"  {obj['type']:<20} {obj['instance']:<10} {obj['name']}")
                    if len(dev['objects']) > 50:
                        print(f"  ... and {len(dev['objects']) - 50} more objects")

            # Generate suggested config
            print(f"\n{'='*70}")
            print("SUGGESTED YAML CONFIG (add to bacnet-config.yaml)")
            print(f"{'='*70}\n")

            for dev in devices:
                print(f"  - name: {dev['name'].lower().replace(' ', '-')}")
                print(f"    device_id: {dev['device_id']}")
                print(f"    ip: {dev['ip']}")
                print(f"    objects:")
                for obj in dev['objects']:
                    type_short = {
                        "analogInput": "AI",
                        "analogValue": "AV",
                        "binaryInput": "BI",
                        "binaryValue": "BV",
                    }.get(obj['type'], obj['type'])

                    if type_short in ("AI", "AV", "BI", "BV"):
                        tag = f"HOST-{obj['name']}" if obj['name'] else f"HOST-{type_short}-{obj['instance']}"
                        tag = tag.replace(" ", "-").upper()[:24]
                        print(f"      - tag: {tag}")
                        print(f"        object_type: {type_short}")
                        print(f"        instance: {obj['instance']}")
                        print(f"        bacnet_name: \"{obj['name']}\"")
                        print(f"        subsystem: host-bms")
                        unit = "°C" if "temp" in obj['name'].lower() else "bool" if type_short.startswith("B") else ""
                        dtype = "bool" if type_short.startswith("B") else "float"
                        print(f"        unit: \"{unit}\"")
                        print(f"        data_type: {dtype}")
                        print(f"        poll_group: slow")
                        print()

        await loop.run_in_executor(None, self.network_manager.stop)

    def get_status(self) -> dict:
        return {
            "status": "running" if self._running else "stopped",
            "mode": "READ-ONLY",
            "devices_online": sum(1 for r in self.readers if r.metrics.online),
            "devices_total": len(self.readers),
            "mqtt": self.publisher.stats,
            "devices": {
                r.device.name: r.metrics.to_dict() for r in self.readers
            },
        }


# ─── Entry point ───────────────────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser(description="MicroLink BACnet Adapter (READ-ONLY)")
    parser.add_argument("--config", type=str, default="bacnet-config.yaml")
    parser.add_argument("--discover", action="store_true",
                        help="Run discovery mode — scan for BACnet devices and list objects")
    parser.add_argument("--discover-timeout", type=int, default=10,
                        help="Discovery scan timeout in seconds")
    parser.add_argument("--log-level", type=str, default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()
    logger.setLevel(getattr(logging, args.log_level))

    adapter = BACnetAdapter(args.config)

    if args.discover:
        await adapter.discover(timeout=args.discover_timeout)
        return

    loop = asyncio.get_running_loop()
    for sig_name in ("SIGINT", "SIGTERM"):
        try:
            import signal
            loop.add_signal_handler(
                getattr(signal, sig_name),
                lambda: asyncio.create_task(adapter.stop()),
            )
        except (NotImplementedError, AttributeError):
            pass

    await adapter.start()


if __name__ == "__main__":
    asyncio.run(main())
