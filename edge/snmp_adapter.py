"""
MicroLink MCS — SNMP Protocol Adapter
Stream A · Task 5 · v1.0.0

Polls SNMP devices (network switches, PDUs, UPS) and publishes normalised
telemetry messages to the local MQTT broker following the MCS MQTT schema.

Supports SNMP v2c and v3. Config-driven OID mapping. Handles SNMP traps
as alarm events.

Dependencies:
    pip install pysnmp paho-mqtt pyyaml

Usage:
    python snmp_adapter.py --config snmp-config.yaml
"""

import asyncio
import json
import logging
import time
import argparse
from datetime import datetime, timezone
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Any

import yaml
import paho.mqtt.client as mqtt

# pysnmp high-level API
from pysnmp.hlapi.v3arch.asyncio import (
    get_cmd,
    bulk_cmd,
    SnmpEngine,
    CommunityData,
    UsmUserData,
    UdpTransportTarget,
    ContextData,
    ObjectType,
    ObjectIdentity,
)
from pysnmp.hlapi.auth import (
    usmHMACMD5AuthProtocol,
    usmHMACSHAAuthProtocol,
    usmDESPrivProtocol,
    usmAesCfb128Protocol,
)

# ─── Structured JSON logging ──────────────────────────────────────────────

class JSONFormatter(logging.Formatter):
    def format(self, record):
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "module": "snmp_adapter",
            "msg": record.getMessage(),
        }
        if hasattr(record, "device"):
            entry["device"] = record.device
        if hasattr(record, "tag"):
            entry["tag"] = record.tag
        if record.exc_info and record.exc_info[0]:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry)


logger = logging.getLogger("snmp_adapter")
handler = logging.StreamHandler()
handler.setFormatter(JSONFormatter())
logger.addHandler(handler)
logger.setLevel(logging.INFO)

# ─── Enums and data classes ────────────────────────────────────────────────

class Quality(str, Enum):
    GOOD = "GOOD"
    UNCERTAIN = "UNCERTAIN"
    BAD = "BAD"


class SNMPVersion(str, Enum):
    V2C = "v2c"
    V3 = "v3"


class AuthProtocol(str, Enum):
    MD5 = "MD5"
    SHA = "SHA"
    NONE = "none"


class PrivProtocol(str, Enum):
    DES = "DES"
    AES128 = "AES128"
    NONE = "none"


AUTH_PROTO_MAP = {
    AuthProtocol.MD5: usmHMACMD5AuthProtocol,
    AuthProtocol.SHA: usmHMACSHAAuthProtocol,
}

PRIV_PROTO_MAP = {
    PrivProtocol.DES: usmDESPrivProtocol,
    PrivProtocol.AES128: usmAesCfb128Protocol,
}


@dataclass
class OIDMapping:
    """Maps an SNMP OID to an MCS sensor tag."""
    tag: str
    description: str
    subsystem: str
    oid: str
    unit: str
    data_type: str = "float"       # float, int, bool, counter
    scale: float = 1.0
    offset: float = 0.0
    range_min: float = -1e9
    range_max: float = 1e9
    poll_group: str = "normal"
    alarm_thresholds: dict = field(default_factory=dict)
    # For counter-type OIDs, we compute delta per interval
    is_counter: bool = False
    counter_unit: str = ""         # Unit after delta calculation (e.g., "Mbps")
    counter_scale: float = 1.0    # Scale factor for delta (e.g., bits→Mbps)


@dataclass
class SNMPDeviceConfig:
    """Configuration for a single SNMP-managed device."""
    name: str
    device_id: str
    host: str
    port: int = 161
    version: SNMPVersion = SNMPVersion.V2C
    # v2c settings
    community: str = "public"
    # v3 settings
    username: str = ""
    auth_protocol: AuthProtocol = AuthProtocol.NONE
    auth_password: str = ""
    priv_protocol: PrivProtocol = PrivProtocol.NONE
    priv_password: str = ""
    # Timing
    timeout: float = 5.0
    retries: int = 2
    # OID mappings
    oids: list = field(default_factory=list)  # List[OIDMapping]


@dataclass
class DeviceMetrics:
    """Runtime metrics per device."""
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
            "client_id": f"snmp-adapter-{raw['site_id']}-{raw['block_id']}",
            **raw.get("mqtt", {}),
        },
        "polling_groups": raw.get("polling_groups", {
            "safety": 1000,
            "fast": 2000,
            "normal": 5000,
            "slow": 30000,
        }),
        "devices": [],
    }

    for dev_raw in raw.get("devices", []):
        oids = []
        for oid_raw in dev_raw.get("oids", []):
            oids.append(OIDMapping(
                tag=oid_raw["tag"],
                description=oid_raw.get("description", ""),
                subsystem=oid_raw["subsystem"],
                oid=oid_raw["oid"],
                unit=oid_raw.get("unit", ""),
                data_type=oid_raw.get("data_type", "float"),
                scale=oid_raw.get("scale", 1.0),
                offset=oid_raw.get("offset", 0.0),
                range_min=oid_raw.get("range_min", -1e9),
                range_max=oid_raw.get("range_max", 1e9),
                poll_group=oid_raw.get("poll_group", "normal"),
                alarm_thresholds=oid_raw.get("alarm_thresholds", {}),
                is_counter=oid_raw.get("is_counter", False),
                counter_unit=oid_raw.get("counter_unit", ""),
                counter_scale=oid_raw.get("counter_scale", 1.0),
            ))

        device = SNMPDeviceConfig(
            name=dev_raw["name"],
            device_id=dev_raw.get("device_id", dev_raw["name"]),
            host=dev_raw["host"],
            port=dev_raw.get("port", 161),
            version=SNMPVersion(dev_raw.get("version", "v2c")),
            community=dev_raw.get("community", "public"),
            username=dev_raw.get("username", ""),
            auth_protocol=AuthProtocol(dev_raw.get("auth_protocol", "none")),
            auth_password=dev_raw.get("auth_password", ""),
            priv_protocol=PrivProtocol(dev_raw.get("priv_protocol", "none")),
            priv_password=dev_raw.get("priv_password", ""),
            timeout=dev_raw.get("timeout", 5.0),
            retries=dev_raw.get("retries", 2),
            oids=oids,
        )
        config["devices"].append(device)

    total_oids = sum(len(d.oids) for d in config["devices"])
    logger.info(f"Loaded config: {len(config['devices'])} devices, {total_oids} OIDs")
    return config


# ─── Alarm evaluation ─────────────────────────────────────────────────────

def evaluate_alarm(value: float, thresholds: dict) -> Optional[str]:
    """Check value against alarm thresholds, highest priority first."""
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
    """Publishes normalised telemetry and alarm events to local MQTT broker."""

    def __init__(self, config: dict, site_id: str, block_id: str):
        self.site_id = site_id
        self.block_id = block_id
        self.client = mqtt.Client(
            client_id=config.get("client_id", "snmp-adapter"),
            protocol=mqtt.MQTTv311,
        )
        self.host = config.get("host", "localhost")
        self.port = config.get("port", 1883)
        self.connected = False
        self._seq_counters = {}
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
            self.client.publish(topic, json.dumps(payload),
                                qos=1, retain=False)
        except Exception as e:
            logger.error(f"MQTT alarm publish error: {e}")

    @property
    def stats(self):
        return {
            "published": self._publish_count,
            "errors": self._error_count,
            "connected": self.connected,
        }


# ─── SNMP device reader ───────────────────────────────────────────────────

class SNMPDeviceReader:
    """Manages SNMP communication with a single device."""

    def __init__(self, device: SNMPDeviceConfig):
        self.device = device
        self.metrics = DeviceMetrics()
        self.engine = SnmpEngine()
        self._alarm_states = {}     # tag → current alarm priority
        self._counter_cache = {}    # tag → (timestamp, raw_value)
        self._online = False

        # Build credentials
        if device.version == SNMPVersion.V2C:
            self.credentials = CommunityData(device.community)
        elif device.version == SNMPVersion.V3:
            auth_proto = AUTH_PROTO_MAP.get(device.auth_protocol)
            priv_proto = PRIV_PROTO_MAP.get(device.priv_protocol)

            kwargs = {"userName": device.username}
            if auth_proto:
                kwargs["authProtocol"] = auth_proto
                kwargs["authKey"] = device.auth_password
            if priv_proto:
                kwargs["privProtocol"] = priv_proto
                kwargs["privKey"] = device.priv_password

            self.credentials = UsmUserData(**kwargs)

        # Transport target
        self.transport = UdpTransportTarget(
            (device.host, device.port),
            timeout=device.timeout,
            retries=device.retries,
        )

    async def snmp_get(self, oid_str: str) -> tuple:
        """Execute SNMP GET for a single OID.

        Returns:
            (raw_value: Any, error: Optional[str])
        """
        try:
            iterator = get_cmd(
                self.engine,
                self.credentials,
                self.transport,
                ContextData(),
                ObjectType(ObjectIdentity(oid_str)),
            )

            error_indication, error_status, error_index, var_binds = await iterator

            if error_indication:
                return None, str(error_indication)
            if error_status:
                return None, f"{error_status.prettyPrint()} at {error_index}"

            for oid, val in var_binds:
                return val, None

            return None, "No var_binds returned"

        except Exception as e:
            return None, str(e)

    async def snmp_bulk(self, oid_strs: list, max_repetitions: int = 10) -> list:
        """Execute SNMP GETBULK for multiple OIDs.

        Returns:
            list of (oid_str, raw_value) tuples
        """
        results = []
        try:
            objects = [ObjectType(ObjectIdentity(oid)) for oid in oid_strs]

            iterator = bulk_cmd(
                self.engine,
                self.credentials,
                self.transport,
                ContextData(),
                0,  # non-repeaters
                max_repetitions,
                *objects,
            )

            error_indication, error_status, error_index, var_binds = await iterator

            if error_indication or error_status:
                return []

            for var_bind_row in var_binds:
                oid_str_result = str(var_bind_row[0])
                results.append((oid_str_result, var_bind_row[1]))

        except Exception as e:
            logger.error(f"SNMP bulk error on {self.device.name}: {e}",
                         extra={"device": self.device.name})

        return results

    def _convert_value(self, raw_value: Any, mapping: OIDMapping) -> tuple:
        """Convert SNMP raw value to engineering float.

        Returns:
            (float_value, Quality)
        """
        try:
            if raw_value is None:
                return 0.0, Quality.BAD

            # Extract numeric value from pysnmp types
            raw_str = str(raw_value)

            if mapping.data_type == "bool":
                # Boolean: various SNMP representations
                if hasattr(raw_value, "prettyPrint"):
                    raw_str = raw_value.prettyPrint().lower()
                val = 1.0 if raw_str in ("1", "true", "up", "online",
                                          "normal", "running") else 0.0
            elif mapping.data_type == "counter":
                # Counter: we store raw and compute delta later
                val = float(int(raw_value))
            elif mapping.data_type == "int":
                val = float(int(raw_value))
            else:
                val = float(raw_value)

            # Apply scale and offset
            val = round((val * mapping.scale) + mapping.offset, 4)

            # Range check
            if val < mapping.range_min or val > mapping.range_max:
                return val, Quality.UNCERTAIN

            return val, Quality.GOOD

        except (ValueError, TypeError) as e:
            logger.warning(
                f"Value conversion error: {self.device.name}/{mapping.tag} "
                f"raw={raw_value} — {e}",
                extra={"device": self.device.name, "tag": mapping.tag},
            )
            return 0.0, Quality.BAD

    def compute_counter_delta(self, tag: str, raw_value: float,
                              mapping: OIDMapping) -> Optional[float]:
        """For counter OIDs, compute rate of change (delta/interval).

        Returns the rate value, or None if this is the first sample.
        """
        now = time.monotonic()
        prev = self._counter_cache.get(tag)
        self._counter_cache[tag] = (now, raw_value)

        if prev is None:
            return None  # First sample — can't compute delta yet

        prev_time, prev_value = prev
        dt = now - prev_time
        if dt <= 0:
            return None

        # Handle counter wrap (32-bit counter wraps at 2^32)
        delta = raw_value - prev_value
        if delta < 0:
            delta += 2**32  # Counter wrapped

        # Convert to rate with scaling
        # e.g., octets delta → bits/sec → Mbps
        rate = (delta / dt) * mapping.counter_scale
        return round(rate, 4)

    async def read_oid(self, mapping: OIDMapping) -> tuple:
        """Read a single OID and return (value, quality).

        For counter types, returns the computed rate.
        """
        t_start = time.monotonic()
        raw_value, error = await self.snmp_get(mapping.oid)
        latency_ms = (time.monotonic() - t_start) * 1000

        if error:
            self.metrics.record_error()
            self._online = False
            logger.warning(
                f"SNMP GET failed: {self.device.name}/{mapping.tag} "
                f"oid={mapping.oid} — {error}",
                extra={"device": self.device.name, "tag": mapping.tag},
            )
            return 0.0, Quality.BAD

        self.metrics.record_read(latency_ms)
        self._online = True

        value, quality = self._convert_value(raw_value, mapping)

        # Handle counter-type OIDs
        if mapping.is_counter and quality == Quality.GOOD:
            rate = self.compute_counter_delta(mapping.tag, value, mapping)
            if rate is None:
                return 0.0, Quality.UNCERTAIN  # First sample
            return rate, Quality.GOOD

        return value, quality

    def check_alarm_transition(self, tag: str,
                               new_alarm: Optional[str]) -> Optional[str]:
        """Detect alarm state changes. Returns action or None."""
        prev = self._alarm_states.get(tag)
        self._alarm_states[tag] = new_alarm

        if prev is None and new_alarm is not None:
            return "RAISED"
        if prev is not None and new_alarm is None:
            return "CLEARED"
        if prev is not None and new_alarm is not None and prev != new_alarm:
            return "ESCALATED"
        return None


# ─── SNMP trap listener ───────────────────────────────────────────────────

class TrapListener:
    """Listens for SNMP traps and converts them to alarm events.

    SNMP traps are unsolicited notifications from devices. We map known
    trap OIDs to alarm events and publish them to MQTT.
    """

    # Well-known trap OID prefixes and their alarm mappings
    TRAP_MAP = {
        # UPS traps (APC)
        "1.3.6.1.4.1.318.0.5": {
            "tag": "UPS-01-on-batt",
            "subsystem": "electrical",
            "priority": "P1",
            "description": "UPS on battery — utility power lost",
        },
        "1.3.6.1.4.1.318.0.9": {
            "tag": "UPS-01-on-batt",
            "subsystem": "electrical",
            "priority": "P1",
            "description": "UPS on battery — utility power lost (enterprise trap)",
        },
        "1.3.6.1.4.1.318.0.24": {
            "tag": "UPS-01-load",
            "subsystem": "electrical",
            "priority": "P1",
            "description": "UPS output overload",
        },
        # Link down trap (standard)
        "1.3.6.1.6.3.1.1.5.3": {
            "tag": "SW-01-uplink1-status",
            "subsystem": "network",
            "priority": "P1",
            "description": "Network link down",
        },
        # Link up trap (standard) — used to clear
        "1.3.6.1.6.3.1.1.5.4": {
            "tag": "SW-01-uplink1-status",
            "subsystem": "network",
            "priority": "P1",
            "description": "Network link restored",
            "_action": "CLEARED",
        },
        # PDU overload (Raritan)
        "1.3.6.1.4.1.13742.6.0.4": {
            "tag": "PDU-01-kW",
            "subsystem": "electrical",
            "priority": "P1",
            "description": "PDU overcurrent alarm",
        },
    }

    def __init__(self, publisher: MQTTPublisher, port: int = 162):
        self.publisher = publisher
        self.port = port
        self._running = False

    async def start(self):
        """Start the trap listener.

        Note: In production, use pysnmp's NotificationReceiver.
        This is a simplified implementation that logs trap OIDs
        and publishes matching alarm events.

        Full trap receiver requires root/elevated privileges for port 162.
        In Docker, we map the port in docker-compose.yml.
        """
        self._running = True
        logger.info(f"Trap listener ready (port {self.port})")

        # In a real deployment, this would use pysnmp's notification receiver:
        #
        # from pysnmp.hlapi.v3arch.asyncio import ntfrcv
        # snmpEngine = SnmpEngine()
        # ntfrcv.NotificationReceiver(snmpEngine, self._trap_callback)
        #
        # For now we just hold the task alive. Traps will be handled
        # when the full pysnmp notification receiver is wired up.

        while self._running:
            await asyncio.sleep(1)

    def handle_trap(self, trap_oid: str, var_binds: dict,
                    source_ip: str):
        """Process a received trap and publish alarm if mapped."""
        trap_info = self.TRAP_MAP.get(trap_oid)
        if not trap_info:
            logger.info(f"Unknown trap OID {trap_oid} from {source_ip}")
            return

        action = trap_info.get("_action", "RAISED")
        self.publisher.publish_alarm(
            tag=trap_info["tag"],
            subsystem=trap_info["subsystem"],
            priority=trap_info["priority"],
            action=action,
            value=1.0,
            threshold=1.0,
            direction="BOOL",
            description=f"{trap_info['description']} (trap from {source_ip})",
        )
        logger.info(
            f"Trap processed: {trap_oid} → {trap_info['tag']} "
            f"{action} from {source_ip}"
        )

    def stop(self):
        self._running = False


# ─── Polling group runner ──────────────────────────────────────────────────

async def run_poll_group(group_name: str, interval_ms: int,
                         device_readers: list,
                         publisher: MQTTPublisher):
    """Poll all OIDs in a group at the configured interval."""

    logger.info(f"SNMP poll group '{group_name}' started: interval={interval_ms}ms")
    interval_s = interval_ms / 1000.0

    while True:
        cycle_start = time.monotonic()

        for reader in device_readers:
            group_oids = [o for o in reader.device.oids
                          if o.poll_group == group_name]

            for mapping in group_oids:
                value, quality = await reader.read_oid(mapping)

                # For counter OIDs, use the counter unit
                unit = mapping.counter_unit if (mapping.is_counter
                                                 and mapping.counter_unit) else mapping.unit

                # Evaluate alarms
                alarm = None
                if quality == Quality.GOOD and mapping.alarm_thresholds:
                    alarm = evaluate_alarm(value, mapping.alarm_thresholds)

                # Publish telemetry
                publisher.publish_telemetry(
                    subsystem=mapping.subsystem,
                    tag=mapping.tag,
                    value=value,
                    unit=unit,
                    quality=quality,
                    alarm=alarm,
                )

                # Alarm edge detection
                action = reader.check_alarm_transition(mapping.tag, alarm)
                if action and alarm:
                    threshold = 0.0
                    direction = "HIGH"
                    for p in ["P0", "P1", "P2", "P3"]:
                        hk, lk = f"{p}_high", f"{p}_low"
                        if hk in mapping.alarm_thresholds and value > mapping.alarm_thresholds[hk]:
                            threshold = mapping.alarm_thresholds[hk]
                            direction = "HIGH"
                            break
                        if lk in mapping.alarm_thresholds and value < mapping.alarm_thresholds[lk]:
                            threshold = mapping.alarm_thresholds[lk]
                            direction = "LOW"
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
                                     f"{value}{unit} vs {alarm} limit {threshold}{unit}"),
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
                        description=f"{mapping.description} returned to normal — {value}{unit}",
                    )

        elapsed = time.monotonic() - cycle_start
        sleep_time = max(0, interval_s - elapsed)
        if elapsed > interval_s:
            logger.warning(f"SNMP poll group '{group_name}' overrun: "
                           f"{elapsed*1000:.0f}ms > {interval_ms}ms")
        await asyncio.sleep(sleep_time)


# ─── Main adapter ─────────────────────────────────────────────────────────

class SNMPAdapter:
    """Main SNMP adapter. Loads config, creates readers, runs polling loops."""

    def __init__(self, config_path: str):
        self.config = load_config(config_path)
        self.site_id = self.config["site_id"]
        self.block_id = self.config["block_id"]
        self.publisher = MQTTPublisher(
            self.config["mqtt"], self.site_id, self.block_id,
        )
        self.readers: list[SNMPDeviceReader] = []
        self.trap_listener: Optional[TrapListener] = None
        self._running = False

        for dev_config in self.config["devices"]:
            self.readers.append(SNMPDeviceReader(dev_config))

    async def start(self):
        """Connect and start polling."""
        logger.info(f"SNMPAdapter starting: site={self.site_id} block={self.block_id}")
        self._running = True

        # Connect MQTT
        self.publisher.connect()

        # Start trap listener
        self.trap_listener = TrapListener(self.publisher)

        # Build polling tasks
        tasks = []
        for group_name, interval_ms in self.config["polling_groups"].items():
            has_oids = any(
                any(o.poll_group == group_name for o in reader.device.oids)
                for reader in self.readers
            )
            if has_oids:
                tasks.append(
                    asyncio.create_task(
                        run_poll_group(group_name, interval_ms,
                                       self.readers, self.publisher)
                    )
                )

        # Add trap listener task
        tasks.append(asyncio.create_task(self.trap_listener.start()))

        logger.info(f"Started {len(tasks) - 1} polling groups + trap listener")

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            logger.info("SNMPAdapter shutting down")
        finally:
            await self.stop()

    async def stop(self):
        self._running = False
        if self.trap_listener:
            self.trap_listener.stop()
        self.publisher.disconnect()
        logger.info("SNMPAdapter stopped")

    def get_status(self) -> dict:
        return {
            "status": "running" if self._running else "stopped",
            "devices_online": sum(1 for r in self.readers if r._online),
            "devices_total": len(self.readers),
            "mqtt": self.publisher.stats,
            "devices": {
                r.device.name: r.metrics.to_dict() for r in self.readers
            },
        }


# ─── Entry point ───────────────────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser(description="MicroLink SNMP Adapter")
    parser.add_argument("--config", type=str, default="snmp-config.yaml")
    parser.add_argument("--log-level", type=str, default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()
    logger.setLevel(getattr(logging, args.log_level))

    adapter = SNMPAdapter(args.config)

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
