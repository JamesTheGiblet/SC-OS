"""
Sensor trust from plausibility checks.

A sensor earns trust when its readings are physically plausible, not when they
sit inside its margins: a working thermometer in a hot room is still right.
Margins describe the world (crossing one is a threshold report, like tilt);
these checks describe the sensor.

The master runs the checks itself on each __readings__ capsule, using the
device's __sensors__ description for physical ranges. A check result is the
master's own observation of the readings, so it counts as an outcome; merely
receiving a reading does not.

Opinions: "sensor:<device>/<id>", e.g. "sensor:m5-96c048/battery". At most one
outcome per sensor per WINDOW_SECONDS, a failure if any check in that window
failed. Without the window a steady stream of readings would drive every opinion
to the maximum within minutes.

Checks (a sensor gets no verdict when its check can't apply):
- range      every value, or each axis, inside the description's physical min..max
- accel      |a| within 0.9..1.1 g while the gyroscope says the device is still (< 5 dps)
- gyro       every axis under 10 dps while the accelerometer is steady across two readings
- tilt       within 5 degrees of the tilt computed from the accelerometer
- imu_temp, chip_temp   change no faster than 0.5 C per second
- battery    3.0..4.5 V, and no jump over 0.3 V between readings
- clock      within 120 s of when the reading capsule was created
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from edge.sensors import READINGS_TOPIC, TOPIC as SENSORS_TOPIC, parse_evidence, parse_readings

if TYPE_CHECKING:
    from scheduler import Scheduler

KEY_PREFIX = "sensor:"
WINDOW_SECONDS = 60.0

STILL_GYRO_DPS = 5.0
ACCEL_AT_REST_G = (0.9, 1.1)
STEADY_ACCEL_G = 0.02
GYRO_STILL_MAX_DPS = 10.0
TILT_TOLERANCE_DEG = 5.0
TEMP_RATE_C_PER_S = 0.5
BATTERY_V = (3.0, 4.5)
BATTERY_JUMP_V = 0.3
CLOCK_TOLERANCE_S = 120.0


def _mag(v) -> float:
    return math.sqrt(sum(c * c for c in v))


def _parse_time(text: str) -> datetime | None:
    try:
        t = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def check(readings: dict, created: datetime, previous: tuple[dict, datetime] | None = None,
          description: dict | None = None) -> dict[str, tuple[bool, str]]:
    """
    Plausibility verdicts for one set of readings: {sensor id: (passed, why)}.
    previous: the last readings from the same device and when they were created.
    description: {sensor id: parsed __sensors__ evidence} for physical ranges.
    """
    verdicts: dict[str, tuple[bool, str]] = {}
    description = description or {}
    prev, prev_at = previous if previous else ({}, None)
    dt = (created - prev_at).total_seconds() if prev_at else None

    def fail(sid, why):
        verdicts[sid] = (False, why)

    def ok(sid, why):
        verdicts.setdefault(sid, (True, why))       # a failure already recorded wins

    # range, from the description
    for sid, value in readings.items():
        d = description.get(sid)
        if d is None or isinstance(value, str):
            continue
        values = value if isinstance(value, tuple) else (value,)
        if any(not d["min"] <= v <= d["max"] for v in values):
            fail(sid, f"outside physical range {d['min']:g}..{d['max']:g}")

    accel, gyro = readings.get("accel"), readings.get("gyro")
    if isinstance(accel, tuple) and isinstance(gyro, tuple):
        if max(abs(g) for g in gyro) < STILL_GYRO_DPS:
            m = _mag(accel)
            if ACCEL_AT_REST_G[0] <= m <= ACCEL_AT_REST_G[1]:
                ok("accel", f"|a|={m:.2f} g at rest")
            else:
                fail("accel", f"|a|={m:.2f} g while still")
        prev_accel = prev.get("accel")
        if isinstance(prev_accel, tuple) and abs(_mag(accel) - 1.0) < 0.03 \
                and max(abs(a - b) for a, b in zip(accel, prev_accel)) < STEADY_ACCEL_G:
            worst = max(abs(g) for g in gyro)
            if worst < GYRO_STILL_MAX_DPS:
                ok("gyro", f"{worst:.1f} dps while steady")
            else:
                fail("gyro", f"{worst:.1f} dps while the accelerometer is steady")

    tilt = readings.get("tilt")
    if isinstance(accel, tuple) and isinstance(tilt, float) and _mag(accel) > 0.5:
        expected = math.degrees(math.acos(max(-1.0, min(1.0, accel[2] / _mag(accel)))))
        if abs(tilt - expected) <= TILT_TOLERANCE_DEG:
            ok("tilt", f"{tilt:.0f} deg matches accelerometer")
        else:
            fail("tilt", f"{tilt:.0f} deg but accelerometer says {expected:.0f}")

    for sid in ("imu_temp", "chip_temp"):
        now_v, prev_v = readings.get(sid), prev.get(sid)
        if isinstance(now_v, float) and isinstance(prev_v, float) and dt and dt >= 1:
            rate = abs(now_v - prev_v) / dt
            if rate <= TEMP_RATE_C_PER_S:
                ok(sid, f"{rate:.2f} C/s")
            else:
                fail(sid, f"changed {rate:.2f} C/s")

    battery = readings.get("battery")
    if isinstance(battery, float):
        if not BATTERY_V[0] <= battery <= BATTERY_V[1]:
            fail("battery", f"{battery:.2f} V outside {BATTERY_V[0]}..{BATTERY_V[1]} V")
        elif isinstance(prev.get("battery"), float) and abs(battery - prev["battery"]) > BATTERY_JUMP_V:
            fail("battery", f"jumped {battery - prev['battery']:+.2f} V")
        else:
            ok("battery", f"{battery:.2f} V")

    clock = readings.get("clock")
    if isinstance(clock, str):
        t = _parse_time(clock)
        if t is None:
            fail("clock", f"unreadable time {clock!r}")
        else:
            off = (t - created).total_seconds()
            if abs(off) <= CLOCK_TOLERANCE_S:
                ok("clock", f"{off:+.0f} s")
            else:
                fail("clock", f"{off:+.0f} s from the capsule's time")

    return verdicts


class SensorObserver:
    """Runs the checks on every __readings__ capsule dispatched and records sensor outcomes."""

    def __init__(self, store):
        self.store = store
        self.last: dict[str, tuple[dict, datetime]] = {}      # device agent -> (readings, created)
        self.windows: dict[str, dict] = {}                     # opinion key -> window state

    def description(self, agent: str) -> dict:
        """The device's latest __sensors__ description, {id: evidence dict}."""
        records = self.store.find(topic=SENSORS_TOPIC, sender=agent)
        if not records:
            return {}
        latest = max(records, key=lambda r: r["stored_at"])
        return {parse_evidence(cl)["id"]: parse_evidence(cl)
                for cl in latest["capsule"]["semantics"].get("claims", [])}

    def observe(self, capsule: dict, scheduler: "Scheduler", now: datetime | None = None) -> list[str]:
        """Check one dispatched capsule (wire form). Returns what the outcomes changed."""
        if capsule.get("semantics", {}).get("topic") != READINGS_TOPIC:
            return []
        agent = capsule["from"]
        created = datetime.fromisoformat(capsule["created"])
        now = now or datetime.now(timezone.utc)
        readings = parse_readings(capsule)
        verdicts = check(readings, created, self.last.get(agent), self.description(agent))
        self.last[agent] = (readings, created)

        learned = []
        device = agent.removeprefix("agent://")
        for sid, (passed, why) in sorted(verdicts.items()):
            key = f"{KEY_PREFIX}{device}/{sid}"
            w = self.windows.get(key)
            if w is None:                                   # first verdict: counts at once
                op = scheduler.record_observation(key, passed, now)
                self.windows[key] = {"since": now, "failed": False, "why": "", "pending": False}
                learned.append(self._line(key, passed, why, op))
                continue
            w["pending"] = True
            if not passed and not w["failed"]:
                w["failed"], w["why"] = True, why
            if (now - w["since"]).total_seconds() >= WINDOW_SECONDS:
                outcome = not w["failed"]
                op = scheduler.record_observation(key, outcome, now)
                learned.append(self._line(key, outcome, w["why"] if w["failed"] else why, op))
                self.windows[key] = {"since": now, "failed": False, "why": "", "pending": False}
        return learned

    @staticmethod
    def _line(key, passed, why, op) -> str:
        return (f"{key}: {'plausible' if passed else 'IMPLAUSIBLE'} ({why}) -> "
                f"value={op.value:+.2f} weight={op.weight:.2f} stance={op.stance}")
