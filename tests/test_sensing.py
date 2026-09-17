"""
Sensor trust from plausibility checks: each check passing, failing and staying
silent; one outcome per sensor per window; the scheduler hook; readings capsules
from the gateway; and the firmware's readings through a real Alice.
Run from the project root: python tests/test_sensing.py
"""

import json
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from agents.echo import EchoAgent
from edge.sensors import (READINGS_TOPIC, parse_readings, readings_capsule, sensors_capsule,
                          validate_readings)
from interpreter import to_wire
from primitive import Trigger
from scheduler import Scheduler
from sensing import WINDOW_SECONDS, SensorObserver, check
from store import Store
from validator import CapsuleRejected, validate

T0 = datetime(2026, 9, 17, 12, 0, 0, tzinfo=timezone.utc)
ALICE, DEVICE = "agent://alice", "agent://m5-00aa11"
FLAT = (0.0, 0.0, 1.0)


def still(**extra) -> dict:
    r = {"accel": FLAT, "gyro": (0.5, -0.3, 0.2), "tilt": 0.0, "imu_temp": 40.0,
         "chip_temp": 45.0, "battery": 4.1, "clock": "2026-09-17T12:00:00Z"}
    r.update(extra)
    return r


def verdict(v, sid):
    return v.get(sid, (None, ""))[0]


# --- checks -----------------------------------------------------------------

def test_still_device_passes_every_applicable_check():
    first = check(still(), T0)
    assert verdict(first, "accel") is True
    assert verdict(first, "tilt") is True
    assert verdict(first, "battery") is True
    assert verdict(first, "clock") is True
    assert "gyro" not in first and "imu_temp" not in first    # need a previous reading
    second = check(still(imu_temp=40.2), T0 + timedelta(seconds=5), (still(), T0))
    assert verdict(second, "gyro") is True
    assert verdict(second, "imu_temp") is True and verdict(second, "chip_temp") is True


def test_accel_only_judged_while_still():
    assert verdict(check(still(accel=(0.0, 0.0, 1.6)), T0), "accel") is False
    moving = check(still(accel=(0.8, 0.0, 1.6), gyro=(120.0, 0.0, 0.0)), T0)
    assert "accel" not in moving                               # moving: no verdict


def test_gyro_judged_when_accelerometer_is_steady():
    spinning = check(still(gyro=(45.0, 0.0, 0.0)), T0 + timedelta(seconds=5), (still(), T0))
    assert verdict(spinning, "gyro") is False
    shaken = check(still(accel=(0.2, 0.0, 0.98), gyro=(45.0, 0.0, 0.0)), T0 + timedelta(seconds=5), (still(), T0))
    assert "gyro" not in shaken                                # accelerometer changed: no verdict


def test_tilt_must_agree_with_accelerometer():
    side = (1.0, 0.0, 0.0)
    assert verdict(check(still(accel=side, tilt=90.0, gyro=(0.0, 0.0, 0.0)), T0), "tilt") is True
    assert verdict(check(still(accel=side, tilt=10.0, gyro=(0.0, 0.0, 0.0)), T0), "tilt") is False


def test_temperature_rate():
    later = T0 + timedelta(seconds=10)
    assert verdict(check(still(imu_temp=44.0), later, (still(), T0)), "imu_temp") is True     # 0.4 C/s
    assert verdict(check(still(imu_temp=70.0), later, (still(), T0)), "imu_temp") is False    # 3 C/s
    assert "imu_temp" not in check(still(imu_temp=70.0), T0 + timedelta(milliseconds=500), (still(), T0))


def test_battery_range_and_jumps():
    assert verdict(check(still(battery=2.1), T0), "battery") is False
    assert verdict(check(still(battery=4.9), T0), "battery") is False
    later = T0 + timedelta(seconds=5)
    assert verdict(check(still(battery=3.6), later, (still(), T0)), "battery") is False       # 0.5 V jump
    assert verdict(check(still(battery=4.0), later, (still(), T0)), "battery") is True


def test_clock_against_capsule_time():
    assert verdict(check(still(clock="2026-09-17T12:01:30Z"), T0), "clock") is True
    assert verdict(check(still(clock="2026-09-17T13:00:00Z"), T0), "clock") is False     # an hour off
    assert verdict(check(still(clock="not a time"), T0), "clock") is False


def test_physical_range_from_description_wins():
    description = {"imu_temp": {"min": -40.0, "max": 85.0}, "accel": {"min": -2.0, "max": 2.0}}
    v = check(still(imu_temp=120.0), T0 + timedelta(seconds=300), (still(imu_temp=119.0), T0), description)
    assert verdict(v, "imu_temp") is False and "physical range" in v["imu_temp"][1]
    v = check(still(accel=(0.0, 2.5, 1.0)), T0, None, description)
    assert verdict(v, "accel") is False


def test_sensor_without_dedicated_check_passes_on_range_alone():
    description = {"tof": {"min": 0.0, "max": 2000.0}, "accel": {"min": -2.0, "max": 2.0}}
    assert verdict(check({"tof": 234.0}, T0, None, description), "tof") is True
    assert verdict(check({"tof": 2500.0}, T0, None, description), "tof") is False
    assert "tof" not in check({"tof": 234.0}, T0)                          # no description: no verdict
    moving = check(still(accel=(0.8, 0.0, 1.6), gyro=(120.0, 0.0, 0.0)), T0, None, description)
    assert "accel" not in moving                                            # dedicated checks aren't bypassed


# --- readings capsule -------------------------------------------------------

def test_readings_frame_and_capsule():
    frame = {"src": "m5-00aa11", "readings": {"accel": [0.01, -0.02, 1.0], "battery": 4.16,
                                               "clock": "2026-09-17T12:00:00Z"}}
    c = readings_capsule(frame, sender=DEVICE, receiver=ALICE)
    validate(to_wire(c))
    assert c.semantics.topic == READINGS_TOPIC and c.trigger == Trigger.HEARTBEAT
    assert parse_readings(to_wire(c)) == {"accel": (0.01, -0.02, 1.0), "battery": 4.16,
                                          "clock": "2026-09-17T12:00:00Z"}
    for bad in ({"src": "m5", "readings": {}},
                {"src": "m5", "readings": {"accel": [1, 2]}},
                {"src": "m5", "readings": {"Bad Id": 1}},
                {"src": "m5", "readings": {"x": {"nested": 1}}}):
        try:
            validate_readings(bad)
            raise AssertionError(f"{bad} must be rejected")
        except CapsuleRejected as e:
            assert e.code == "edge_readings"


# --- observer and scheduler -------------------------------------------------

def readings_wire(created: datetime, **values) -> dict:
    frame = {"src": "m5-00aa11", "readings": {k: list(v) if isinstance(v, tuple) else v
                                               for k, v in still(**values).items()}}
    wire = to_wire(readings_capsule(frame, sender=DEVICE, receiver=ALICE))
    wire["created"] = created.isoformat()
    return wire


def setup():
    store = Store(str(Path(tempfile.mkdtemp()) / "alice.db"))
    observer = SensorObserver(store)
    sched = Scheduler({ALICE: EchoAgent()}, store, observers=(observer,))
    return store, observer, sched


def test_one_outcome_per_sensor_per_window():
    store, observer, sched = setup()
    key = "sensor:m5-00aa11/battery"
    observer.observe(readings_wire(T0), sched, now=T0)
    assert store.get_opinion(key)["evidence_count"] == 1          # first verdict counts at once
    for i in range(1, 6):                                          # five more readings inside a minute
        t = T0 + timedelta(seconds=5 * i)
        observer.observe(readings_wire(t, clock=t.strftime("%Y-%m-%dT%H:%M:%SZ")), sched, now=t)
    assert store.get_opinion(key)["evidence_count"] == 1

    bad = T0 + timedelta(seconds=40)
    observer.observe(readings_wire(bad, battery=2.0, clock=bad.strftime("%Y-%m-%dT%H:%M:%SZ")), sched, now=bad)
    end = T0 + timedelta(seconds=WINDOW_SECONDS + 1)
    learned = observer.observe(readings_wire(end, battery=2.0, clock=end.strftime("%Y-%m-%dT%H:%M:%SZ")),
                               sched, now=end)
    row = store.get_opinion(key)
    assert row["evidence_count"] == 2 and row["value"] < 1.1       # the window's failure counted once
    assert any("battery" in line and "IMPLAUSIBLE" in line for line in learned)


def test_scheduler_runs_observers_and_uses_descriptions():
    store, observer, sched = setup()
    description = {"src": "m5-00aa11", "sensors": [
        {"id": "imu_temp", "type": "temperature", "bus": "i2c0:0x68", "pin": "21,22", "unit": "C",
         "min": -40, "max": 85, "margin_low": 0, "margin_high": 65, "sample_ms": 200}]}
    sched.dispatch(sensors_capsule(description, sender=DEVICE, receiver=ALICE))
    now = datetime.now(timezone.utc)
    frame = {"src": "m5-00aa11", "readings": {"imu_temp": 99.0, "battery": 4.1,
                                               "clock": now.strftime("%Y-%m-%dT%H:%M:%SZ")}}
    replies = sched.dispatch(readings_capsule(frame, sender=DEVICE, receiver=ALICE))
    assert replies == []                                            # heartbeat: no reply to the device
    assert store.get_opinion("sensor:m5-00aa11/imu_temp")["value"] < 1.0   # 99 C is past its 85 C maximum
    assert store.get_opinion("sensor:m5-00aa11/battery")["value"] > 1.0
    assert any("imu_temp" in line for line in sched.learned)


def test_receipt_of_other_capsules_changes_no_sensor_opinion():
    store, observer, sched = setup()
    sched.dispatch(sensors_capsule({"src": "m5-00aa11", "sensors": [
        {"id": "battery", "type": "voltage", "bus": "adc", "pin": "38", "unit": "V",
         "min": 0, "max": 5, "margin_low": 3.3, "margin_high": 4.35, "sample_ms": 200}]},
        sender=DEVICE, receiver=ALICE))
    assert store.opinions("sensor:") == {}


# --- through the gateway ----------------------------------------------------

def test_firmware_readings_through_gateway_teach_alice():
    import importlib.util
    from test_gateway import AliceServer, start
    spec = importlib.util.spec_from_file_location("fw_sctalk", ROOT / "firmware" / "m5stickc_plus2" / "sctalk.py")
    sctalk = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sctalk)

    alice = AliceServer()
    gw, frames = start(alice)
    try:
        talk = sctalk.Talk("m5-00aa11", "e0e0", lambda line: gw.handle_frame(json.loads(line)))
        now = datetime.now(timezone.utc)
        talk.readings({"accel": [0.0, 0.01, 1.0], "gyro": [0.3, 0.1, 0.0], "tilt": 0.6,
                       "battery": 4.12, "clock": now.strftime("%Y-%m-%dT%H:%M:%SZ")})
        deadline = time.monotonic() + 5
        while alice.store.get_opinion("sensor:m5-00aa11/battery") is None:
            assert time.monotonic() < deadline, "no sensor opinion formed"
            time.sleep(0.05)
        for sid in ("accel", "tilt", "battery", "clock"):
            row = alice.store.get_opinion(f"sensor:m5-00aa11/{sid}")
            assert row is not None and row["value"] > 1.0, sid
        assert frames.empty()                                       # nothing sent back to the device
    finally:
        gw.close()
        alice.close()


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"{len(tests)} passed")
