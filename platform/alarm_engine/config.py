"""
MCS Stream B — Alarm Engine Configuration

ISA-18.2 alarm lifecycle states, priority definitions, and tuning parameters.
"""

from enum import Enum, IntEnum
from dataclasses import dataclass, field
from typing import Optional


# ── ISA-18.2 Alarm States ────────────────────────────────────────────────
class AlarmState(str, Enum):
    """
    ISA-18.2 alarm lifecycle states.

    State transitions:
        CLEARED ──(threshold crossed)──→ ACTIVE
        ACTIVE  ──(operator ack)──────→ ACKED
        ACKED   ──(value returns)─────→ CLEARED
        ACTIVE  ──(value returns)─────→ RTN_UNACK  (returned but not acknowledged)
        RTN_UNACK ─(operator ack)─────→ CLEARED
        *any*   ──(operator shelve)───→ SHELVED
        SHELVED ──(timer expires)─────→ CLEARED  (or re-evaluates)
    """
    ACTIVE = "ACTIVE"           # Alarm condition present, not acknowledged
    ACKED = "ACKED"             # Alarm condition present, acknowledged by operator
    RTN_UNACK = "RTN_UNACK"     # Alarm condition cleared but not yet acknowledged
    CLEARED = "CLEARED"         # Alarm resolved and acknowledged
    SHELVED = "SHELVED"         # Temporarily suppressed by operator
    SUPPRESSED = "SUPPRESSED"   # Suppressed by cascade logic (auto)


class AlarmPriority(IntEnum):
    """
    ISA-18.2 alarm priorities with response time targets.

    P0: CRITICAL  — immediate response, safety risk
    P1: HIGH      — 15-minute response, SLA impact
    P2: MEDIUM    — 4-hour response, maintenance needed
    P3: LOW       — next business day, informational
    """
    P0 = 0   # CRITICAL
    P1 = 1   # HIGH
    P2 = 2   # MEDIUM
    P3 = 3   # LOW

    @classmethod
    def from_string(cls, s: str) -> "AlarmPriority":
        return cls[s]


# ── Response time targets (seconds) ─────────────────────────────────────
RESPONSE_TARGETS = {
    AlarmPriority.P0: 0,          # Immediate
    AlarmPriority.P1: 15 * 60,    # 15 minutes
    AlarmPriority.P2: 4 * 3600,   # 4 hours
    AlarmPriority.P3: 8 * 3600,   # Next business day (~8 hours)
}


# ── Alarm engine tuning ─────────────────────────────────────────────────
@dataclass
class AlarmEngineConfig:
    """Tuning parameters for the alarm engine."""

    # Shelving
    max_shelve_duration_hours: int = 24       # Maximum time an alarm can be shelved
    default_shelve_duration_hours: int = 8    # Default if operator doesn't specify
    shelve_requires_reason: bool = True       # Must provide reason to shelve

    # Deadband (hysteresis)
    # Alarm clears when value returns inside threshold by this percentage
    # Prevents chattering at threshold boundaries
    deadband_percent: float = 2.0  # 2% of threshold value

    # Flood suppression
    # If more than N alarms raise in M seconds for the same block,
    # suppress lower-priority alarms and raise a single flood alarm
    flood_threshold_count: int = 20
    flood_threshold_seconds: int = 60

    # ISA-18.2 target: max standing alarms per operator per hour
    target_alarms_per_operator_hour: int = 6

    # Stale alarm detection: auto-clear alarms with no new readings
    stale_alarm_timeout_minutes: int = 30

    # Re-evaluation interval for shelved alarms
    shelve_reeval_interval_seconds: int = 300  # Check every 5 min if still in alarm


# ── Cascade suppression rules ────────────────────────────────────────────
@dataclass
class CascadeRule:
    """
    Defines a suppression relationship between alarms.

    When the 'cause' sensor enters alarm, all 'effect' sensor alarms
    are suppressed (state = SUPPRESSED) until the cause clears.

    Example: Pump trip suppresses downstream flow alarms.
    """
    cause_tag_pattern: str          # Regex or exact tag of the cause sensor
    cause_subsystem: str            # Subsystem of the cause
    effect_tag_patterns: list[str]  # Tags that get suppressed
    effect_subsystems: list[str]    # Subsystems that get suppressed
    description: str = ""


# Default cascade rules for a 1MW MicroLink block
DEFAULT_CASCADE_RULES = [
    CascadeRule(
        cause_tag_pattern=r"ML-PUMP-[AB]-SPEED",
        cause_subsystem="thermal-l2",
        effect_tag_patterns=[r"ML-FLOW", r"PHX-01-.*", r"HOST-FLOW"],
        effect_subsystems=["thermal-l2", "thermal-l3"],
        description="Primary pump trip suppresses downstream flow and heat exchanger alarms",
    ),
    CascadeRule(
        cause_tag_pattern=r"CDU-\d{2}-PUMP-SPEED",
        cause_subsystem="thermal-l1",
        effect_tag_patterns=[r"CDU-\d{2}-FLOW", r"CDU-\d{2}-P-DIFF", r"RK-\d{2}-T-OUT"],
        effect_subsystems=["thermal-l1"],
        description="CDU pump trip suppresses CDU flow, pressure, and rack outlet temp alarms",
    ),
    CascadeRule(
        cause_tag_pattern=r"V-MSB-L[123]",
        cause_subsystem="electrical",
        effect_tag_patterns=[r"UPS-\d{2}-.*", r"P-MSB-TOTAL"],
        effect_subsystems=["electrical"],
        description="Mains voltage loss suppresses UPS and power meter alarms",
    ),
    CascadeRule(
        cause_tag_pattern=r"LSH-0[12]-LEAK-.*",
        cause_subsystem="thermal-safety",
        effect_tag_patterns=[r".*-FLOW", r".*-P-.*"],
        effect_subsystems=["thermal-l1", "thermal-l2", "thermal-l3"],
        description="Leak detection suppresses flow and pressure alarms (isolation valve closing)",
    ),
    CascadeRule(
        cause_tag_pattern=r"WAN-.*|VPN-STATUS",
        cause_subsystem="network",
        effect_tag_patterns=[r"SW-\d{2}-.*"],
        effect_subsystems=["network"],
        description="WAN/VPN loss suppresses switch alarms (unreachable, not failed)",
    ),
]
