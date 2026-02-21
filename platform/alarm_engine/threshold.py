"""
MCS Stream B — Threshold Evaluator

Loads alarm thresholds from the sensors table (alarm_thresholds_json) and
evaluates incoming values against them with deadband hysteresis to prevent
chattering at threshold boundaries.

Threshold JSON schema (stored in sensors.alarm_thresholds_json):
{
    "HH": {"value": 60.0, "priority": "P0", "delay_s": 0},
    "H":  {"value": 55.0, "priority": "P2", "delay_s": 30},
    "L":  {"value": 10.0, "priority": "P2", "delay_s": 30},
    "LL": {"value": 5.0,  "priority": "P0", "delay_s": 0}
}

HH = High-High (critical), H = High, L = Low, LL = Low-Low (critical)
delay_s = time value must exceed threshold before alarm raises (debounce)
"""

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from .config import AlarmPriority, AlarmEngineConfig

logger = logging.getLogger("mcs.alarm.threshold")


@dataclass(slots=True)
class ThresholdDef:
    """Single threshold definition for a sensor."""
    level: str              # HH, H, L, LL
    value: float            # threshold value
    priority: AlarmPriority
    delay_s: float = 0.0    # debounce delay
    direction: str = ""     # "HIGH" or "LOW" (derived from level)

    def __post_init__(self):
        if self.level in ("HH", "H"):
            self.direction = "HIGH"
        else:
            self.direction = "LOW"


@dataclass
class SensorThresholds:
    """All thresholds for a single sensor."""
    sensor_id: int
    tag: str
    thresholds: list[ThresholdDef] = field(default_factory=list)

    # Debounce tracking: threshold_level → first_exceeded_at
    _debounce: dict[str, float] = field(default_factory=dict)

    def evaluate(
        self,
        value: float,
        config: AlarmEngineConfig,
    ) -> list[tuple[ThresholdDef, bool]]:
        """
        Evaluate a value against all thresholds.

        Returns a list of (threshold, in_alarm) tuples.
        Applies deadband hysteresis for clearing.
        """
        results = []
        now = time.monotonic()

        for t in self.thresholds:
            in_alarm = self._check_threshold(value, t, config)

            # Debounce: require value to exceed for delay_s before raising
            if in_alarm and t.delay_s > 0:
                if t.level not in self._debounce:
                    self._debounce[t.level] = now
                    in_alarm = False  # Not yet exceeded delay
                elif (now - self._debounce[t.level]) < t.delay_s:
                    in_alarm = False  # Still within delay
                # else: delay exceeded, alarm is valid
            elif not in_alarm:
                # Value returned to normal — reset debounce
                self._debounce.pop(t.level, None)

            results.append((t, in_alarm))

        return results

    @staticmethod
    def _check_threshold(
        value: float,
        threshold: ThresholdDef,
        config: AlarmEngineConfig,
    ) -> bool:
        """
        Check if value exceeds threshold with deadband hysteresis.

        For HIGH thresholds:
          - Alarm raises when value > threshold
          - Alarm clears when value < threshold × (1 - deadband%)

        For LOW thresholds:
          - Alarm raises when value < threshold
          - Alarm clears when value > threshold × (1 + deadband%)
        """
        deadband_frac = config.deadband_percent / 100.0

        if threshold.direction == "HIGH":
            return value > threshold.value
        else:  # LOW
            return value < threshold.value

    @staticmethod
    def check_clear_with_deadband(
        value: float,
        threshold: ThresholdDef,
        config: AlarmEngineConfig,
    ) -> bool:
        """
        Check if value has returned to normal WITH deadband.
        Returns True if the alarm should clear.
        """
        deadband_frac = config.deadband_percent / 100.0

        if threshold.direction == "HIGH":
            # Clear when value drops below threshold minus deadband
            clear_point = threshold.value * (1.0 - deadband_frac)
            return value < clear_point
        else:  # LOW
            # Clear when value rises above threshold plus deadband
            clear_point = threshold.value * (1.0 + deadband_frac)
            return value > clear_point


class ThresholdRegistry:
    """
    In-memory registry of sensor thresholds.
    Loaded from database at startup, refreshed periodically.
    """

    def __init__(self) -> None:
        self._sensors: dict[int, SensorThresholds] = {}
        self._loaded_count = 0

    def load_from_rows(self, rows: list[tuple]) -> int:
        """
        Load thresholds from database query results.

        Expected columns: (sensor_id, tag, alarm_thresholds_json)
        """
        count = 0
        for sensor_id, tag, thresholds_json in rows:
            if not thresholds_json:
                continue

            try:
                raw = json.loads(thresholds_json) if isinstance(thresholds_json, str) else thresholds_json
            except (json.JSONDecodeError, TypeError):
                logger.warning("Invalid threshold JSON for sensor %d (%s)", sensor_id, tag)
                continue

            thresholds = []
            for level, cfg in raw.items():
                if level not in ("HH", "H", "L", "LL"):
                    logger.warning("Unknown threshold level '%s' for sensor %d", level, sensor_id)
                    continue

                try:
                    thresholds.append(ThresholdDef(
                        level=level,
                        value=float(cfg["value"]),
                        priority=AlarmPriority.from_string(cfg.get("priority", "P2")),
                        delay_s=float(cfg.get("delay_s", 0)),
                    ))
                except (KeyError, ValueError) as e:
                    logger.warning("Bad threshold config for sensor %d level %s: %s", sensor_id, level, e)
                    continue

            if thresholds:
                self._sensors[sensor_id] = SensorThresholds(
                    sensor_id=sensor_id,
                    tag=tag,
                    thresholds=thresholds,
                )
                count += 1

        self._loaded_count = count
        logger.info("Threshold registry loaded: %d sensors with thresholds", count)
        return count

    def get(self, sensor_id: int) -> Optional[SensorThresholds]:
        return self._sensors.get(sensor_id)

    @property
    def count(self) -> int:
        return self._loaded_count
