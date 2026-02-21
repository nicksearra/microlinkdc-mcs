"""
MCS Stream B — Cascade Suppression Engine

Implements ISA-18.2 suppression-by-design. When a root-cause alarm fires
(e.g., pump trip), downstream alarms (e.g., flow loss) are automatically
suppressed to reduce alarm flood and help operators focus on the root cause.

Rules are defined in config.py (DEFAULT_CASCADE_RULES) and can be extended
per-site via the database.

How it works:
  1. When an alarm raises, check if it matches any 'cause' pattern
  2. If it does, find all currently active alarms matching 'effect' patterns
  3. Suppress the effects, recording which cause suppressed them
  4. When the cause clears, unsuppress all effects and re-evaluate

This keeps the standing alarm count low — critical for ISA-18.2's
target of <6 alarms per operator per hour.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from .config import CascadeRule, DEFAULT_CASCADE_RULES, AlarmState
from .state_machine import AlarmInstance, TransitionResult

logger = logging.getLogger("mcs.alarm.cascade")


class CascadeEngine:
    """
    Manages cascade suppression relationships between alarms.
    """

    def __init__(self, rules: Optional[list[CascadeRule]] = None) -> None:
        self._rules = rules or DEFAULT_CASCADE_RULES
        # Compile regex patterns for performance
        self._compiled_rules: list[tuple[CascadeRule, re.Pattern, list[re.Pattern]]] = []
        for rule in self._rules:
            cause_re = re.compile(rule.cause_tag_pattern)
            effect_res = [re.compile(p) for p in rule.effect_tag_patterns]
            self._compiled_rules.append((rule, cause_re, effect_res))

        self._suppressions: int = 0
        self._unsuppressions: int = 0

    def on_alarm_raised(
        self,
        cause: AlarmInstance,
        active_alarms: dict[int, AlarmInstance],
    ) -> list[AlarmInstance]:
        """
        Called when an alarm is raised. Checks if it's a cause in any
        cascade rule and suppresses matching effects.

        Returns list of alarms that were suppressed.
        """
        suppressed = []

        for rule, cause_re, effect_res in self._compiled_rules:
            # Does this alarm match a cause pattern?
            if cause.subsystem != rule.cause_subsystem:
                continue
            if not cause_re.fullmatch(cause.tag):
                continue

            # Find and suppress matching effects
            for alarm in active_alarms.values():
                if alarm.sensor_id == cause.sensor_id:
                    continue  # Don't suppress yourself
                if alarm.state in (AlarmState.CLEARED, AlarmState.SUPPRESSED):
                    continue
                if alarm.subsystem not in rule.effect_subsystems:
                    continue

                for effect_re in effect_res:
                    if effect_re.fullmatch(alarm.tag):
                        result = alarm.suppress(cause.id or cause.sensor_id, cause.raised_at)
                        if result == TransitionResult.OK:
                            suppressed.append(alarm)
                            self._suppressions += 1
                        break

        if suppressed:
            logger.info(
                "CASCADE: cause=%s (%s) suppressed %d downstream alarms",
                cause.tag, cause.subsystem, len(suppressed),
            )

        return suppressed

    def on_alarm_cleared(
        self,
        cause: AlarmInstance,
        active_alarms: dict[int, AlarmInstance],
    ) -> list[AlarmInstance]:
        """
        Called when a cause alarm clears. Unsuppresses all alarms that
        were suppressed by this cause.

        Returns list of alarms that were unsuppressed (need re-evaluation).
        """
        unsuppressed = []
        cause_id = cause.id or cause.sensor_id

        for alarm in active_alarms.values():
            if alarm.state != AlarmState.SUPPRESSED:
                continue
            if alarm.suppressed_by_alarm_id != cause_id:
                continue

            result = alarm.unsuppress(cause.cleared_at or cause.last_seen)
            if result == TransitionResult.OK:
                unsuppressed.append(alarm)
                self._unsuppressions += 1

        if unsuppressed:
            logger.info(
                "CASCADE CLEAR: cause=%s cleared, unsuppressed %d alarms (need re-eval)",
                cause.tag, len(unsuppressed),
            )

        return unsuppressed

    def would_be_suppressed(
        self,
        alarm: AlarmInstance,
        active_alarms: dict[int, AlarmInstance],
    ) -> Optional[int]:
        """
        Check if a new alarm WOULD be suppressed by any currently active cause.
        Returns the cause alarm's sensor_id if suppressed, None otherwise.

        Used to prevent raising an alarm that would immediately be suppressed.
        """
        for rule, cause_re, effect_res in self._compiled_rules:
            if alarm.subsystem not in rule.effect_subsystems:
                continue

            is_effect = False
            for effect_re in effect_res:
                if effect_re.fullmatch(alarm.tag):
                    is_effect = True
                    break
            if not is_effect:
                continue

            # Check if any active alarm matches the cause
            for active in active_alarms.values():
                if active.state not in (AlarmState.ACTIVE, AlarmState.ACKED):
                    continue
                if active.subsystem != rule.cause_subsystem:
                    continue
                if cause_re.fullmatch(active.tag):
                    return active.sensor_id

        return None

    @property
    def stats(self) -> dict:
        return {
            "rules_loaded": len(self._rules),
            "total_suppressions": self._suppressions,
            "total_unsuppressions": self._unsuppressions,
        }
