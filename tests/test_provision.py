"""
Operator setup: checking a setup against the device's description, issuing it
signed, the keeper sending it until the device's description matches, the
gateway's setup frame, the firmware applying it, and the whole loop through a
real Alice.
Run from the project root: python tests/test_provision.py
"""

import importlib.util
import json
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agents.echo import EchoAgent
from edge.sensors import readings_capsule, sensors_capsule
from interpreter import to_wire
from provision import (RESEND_SECONDS, SetupKeeper, check_setup, issue, latest_setup, mismatches,
                       parse_setup, setup_capsule)
from scheduler import Scheduler
from store import Store
from validator import CapsuleRejected, validate

ALICE, DEVICE = "agent://alice", "agent://m5-00aa11"
T0 = datetime.now(timezone.utc).replace(microsecond=0)


def firmware_module(name):
    spec = importlib.util.spec_from_file_location(f"fw_{name}", ROOT / "firmware" / "m5stickc_plus2" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def firmware_description():
    from test_gateway import firmware_description as fd
    return fd()


def sensors_frame(description=None) -> dict:
    return {"src": "m5-00aa11", "sensors": list(description or firmware_description()[0])}


def parsed(description) -> dict:
    return {d["id"]: {k: (float(v) if k not in ("id", "type", "bus", "pin", "unit") else v)
                      for k, v in d.items()} for d in description}


def rejected(fn, *args) -> str:
    try:
        fn(*args)
    except CapsuleRejected as e:
        return e.detail
    raise AssertionError("expected CapsuleRejected")


# --- checking and issuing ----------------------------------------------------

def test_check_setup():
    desc = parsed(firmware_description()[0])
    check_setup({"device": "m5-00aa11", "sensors": {"tilt": {"margin_high": 30}}}, desc)
    check_setup({"device": "m5-00aa11", "sensors": {"battery": {"margin_low": 3.5, "sample_ms": 1000}}}, desc)
    check_setup({"device": "m5-00aa11", "sensors": {"anything": {"margin_high": 1}}}, None)   # unknown device
    for setup, words in [
        ({"sensors": {"tilt": {"margin_high": 30}}}, "device name"),
        ({"device": "m5-00aa11", "sensors": {}}, "at least one sensor"),
        ({"device": "m5-00aa11", "sensors": {"tilt": {"pin": "5"}}}, "can't be set"),
        ({"device": "m5-00aa11", "sensors": {"tilt": {"margin_high": "30"}}}, "must be a number"),
        ({"device": "m5-00aa11", "sensors": {"sonar": {"margin_high": 3}}}, "no sensor"),
        ({"device": "m5-00aa11", "sensors": {"tilt": {"margin_high": 200}}}, "margin_high"),
        ({"device": "m5-00aa11", "sensors": {"battery": {"margin_low": 4.4}}}, "margin_low"),    # above 4.35 high
        ({"device": "m5-00aa11", "sensors": {"gyro": {"sample_ms": 5}}}, "sample_ms"),
    ]:
        assert words in rejected(check_setup, setup, desc), (setup, words)


def test_issue_signs_and_stores_and_checks_ranges():
    store = Store(str(Path(tempfile.mkdtemp()) / "alice.db"))
    key = Ed25519PrivateKey.generate()
    store.append(to_wire(sensors_capsule(sensors_frame(), sender=DEVICE, receiver=ALICE)))
    c = issue(store, key, ALICE, {"device": "m5-00aa11", "sensors": {"tilt": {"margin_high": 30}}})
    rec = store.get_record(store.find(capsule_id=c.id)[0]["digest"])
    assert rec["envelope"]["pubkey_id"] == ALICE
    validate(rec["capsule"])
    assert rec["capsule"]["to"] == DEVICE and rec["capsule"]["trigger"] == "task"
    assert parse_setup(rec["capsule"]) == {"tilt": {"margin_high": 30.0}}
    assert latest_setup(store, ALICE, DEVICE)["id"] == c.id
    try:
        issue(store, key, ALICE, {"device": "m5-00aa11", "sensors": {"tilt": {"margin_high": 181}}})
        raise AssertionError("a margin past the physical range must be refused")
    except CapsuleRejected:
        pass


def test_newer_setup_replaces_older():
    store = Store(str(Path(tempfile.mkdtemp()) / "alice.db"))
    key = Ed25519PrivateKey.generate()
    issue(store, key, ALICE, {"device": "m5-00aa11", "sensors": {"tilt": {"margin_high": 30}}}, now=T0)
    newer = issue(store, key, ALICE, {"device": "m5-00aa11", "sensors": {"tilt": {"margin_high": 25}}},
                  now=T0 + timedelta(seconds=1))
    assert latest_setup(store, ALICE, DEVICE)["id"] == newer.id


def test_mismatches():
    desc = parsed(firmware_description()[0])
    assert mismatches({"tilt": {"margin_high": 40.0}}, desc) == []
    assert mismatches({"tilt": {"margin_high": 30.0}}, desc) == ["tilt.margin_high"]
    assert mismatches({"tilt": {"margin_high": 30.0}}, None) == ["tilt.margin_high"]


# --- delivery ----------------------------------------------------------------

def test_keeper_sends_until_description_matches():
    store = Store(str(Path(tempfile.mkdtemp()) / "alice.db"))
    key = Ed25519PrivateKey.generate()
    keeper = SetupKeeper(store, ALICE)
    sched = Scheduler({ALICE: EchoAgent()}, store, observers=(keeper,))
    description = firmware_description()[0]
    first = to_wire(sensors_capsule(sensors_frame(description), sender=DEVICE, receiver=ALICE))
    first["created"] = (T0 - timedelta(seconds=1)).isoformat()      # dated explicitly: order is what's tested
    store.append(first)
    issued = issue(store, key, ALICE, {"device": "m5-00aa11", "sensors": {"tilt": {"margin_high": 30}}})

    readings = to_wire(readings_capsule({"src": "m5-00aa11", "readings": {"battery": 4.1}},
                                        sender=DEVICE, receiver=ALICE))
    sent = keeper.respond(readings, sched, now=T0)
    assert len(sent) == 1
    copy = sent[0]
    validate(to_wire(copy))
    assert copy.receiver == DEVICE and copy.provenance.method == "relay"
    assert copy.provenance.derived_from == (issued.id,)
    assert parse_setup(to_wire(copy)) == {"tilt": {"margin_high": 30.0}}

    assert keeper.respond(readings, sched, now=T0 + timedelta(seconds=10)) == []          # throttled
    assert len(keeper.respond(readings, sched, now=T0 + timedelta(seconds=RESEND_SECONDS + 1))) == 1

    applied = [dict(d, margin_high=30) if d["id"] == "tilt" else d for d in description]
    later = T0 + timedelta(seconds=2)
    described = sensors_capsule(sensors_frame(applied), sender=DEVICE, receiver=ALICE)
    wire = to_wire(described)
    wire["created"] = later.isoformat()
    store.append(wire)
    assert keeper.respond(readings, sched, now=T0 + timedelta(seconds=3 * RESEND_SECONDS)) == []


def test_keeper_ignores_other_capsules_and_devices_without_setup():
    store = Store(str(Path(tempfile.mkdtemp()) / "alice.db"))
    keeper = SetupKeeper(store, ALICE)
    sched = Scheduler({ALICE: EchoAgent()}, store, observers=(keeper,))
    readings = to_wire(readings_capsule({"src": "m5-00aa11", "readings": {"battery": 4.1}},
                                        sender=DEVICE, receiver=ALICE))
    assert keeper.respond(readings, sched) == []                    # no setup issued
    other = to_wire(setup_capsule({"device": "m5-00aa11", "sensors": {"tilt": {"margin_high": 30}}},
                                  sender=ALICE))
    assert keeper.respond(other, sched) == []                       # not readings or a description


# --- firmware ------------------------------------------------------------------

def test_firmware_applies_setup_or_refuses_whole():
    sctalk = firmware_module("sctalk")
    description = list(firmware_description()[0])
    new, err = sctalk.apply_setup(description, {"tilt": {"margin_high": 30}, "gyro": {"sample_ms": 100}})
    assert err is None
    by_id = {d["id"]: d for d in new}
    assert by_id["tilt"]["margin_high"] == 30 and by_id["gyro"]["sample_ms"] == 100
    assert {d["id"]: d for d in description}["tilt"]["margin_high"] == 40          # original untouched
    for bad in ({"sonar": {"margin_high": 1}}, {"tilt": {"pin": 3}}, {"tilt": {"margin_high": 181}},
                {"battery": {"margin_low": 4.4}}, {"gyro": {"sample_ms": 1}}):
        new, err = sctalk.apply_setup(description, bad)
        assert new is None and err, bad


def test_firmware_protocol_reads_setup_frames():
    sctalk = firmware_module("sctalk")
    sent = []
    talk = sctalk.Talk("m5-00aa11", "b0b0", sent.append)
    frame = {"dst": "m5-00aa11", "setup": {"re": "abc123abc123", "sensors": {"tilt": {"margin_high": 30}}}}
    assert talk.receive_line(json.dumps(frame)) == "setup"
    assert talk.setup == {"re": "abc123abc123", "sensors": {"tilt": {"margin_high": 30}}}
    assert talk.receive_line(json.dumps(dict(frame, dst="m5-other"))) is None
    talk.result("abc123abc123", "__setup__", "apply setup", False, "margins outside range")
    cap = json.loads(sent[-1])["cap"]
    assert (cap["tr"], cap["re"], cap["o"], cap["t"]) == ("task_result", "abc123abc123", "failure", "__setup__")


# --- the whole loop ----------------------------------------------------------

def test_setup_loop_through_gateway_and_real_alice():
    from peer import load_or_create_key
    from test_gateway import AliceServer, next_frame, start
    sctalk = firmware_module("sctalk")
    description, absent = firmware_description()

    alice = AliceServer()
    alice.sched.observers += (SetupKeeper(alice.store, ALICE),)
    gw, frames = start(alice)
    try:
        talk = sctalk.Talk("m5-00aa11", "f0f0", lambda line: gw.handle_frame(json.loads(line)))
        talk.describe(description, absent)
        assert next_frame(frames)["cap"]["i"] == "ack"
        deadline = time.monotonic() + 5
        while not alice.store.find(topic="__sensors__", sender=DEVICE):
            assert time.monotonic() < deadline
            time.sleep(0.05)

        issue(alice.store, alice.node.key, ALICE, {"device": "m5-00aa11", "sensors": {"tilt": {"margin_high": 30}}})
        talk.readings({"battery": 4.1})                          # the next report triggers delivery
        setup_frame = next_frame(frames)
        assert talk.receive_line(json.dumps(setup_frame)) == "setup"
        new, err = sctalk.apply_setup(description, talk.setup["sensors"])
        assert err is None
        talk.result(talk.setup["re"], "__setup__", "apply setup", True, "applied tilt")
        assert next_frame(frames)["cap"]["i"] == "ack"
        talk.describe(new, absent)
        assert next_frame(frames)["cap"]["i"] == "ack"

        deadline = time.monotonic() + 5
        while alice.sched.opinion("__setup__").evidence_count == 0:
            assert time.monotonic() < deadline, "Alice never counted the setup outcome"
            time.sleep(0.05)
        assert alice.sched.opinion("__setup__").value > 1.0
        time.sleep(0.2)
        talk.readings({"battery": 4.11})                          # description matches now: nothing sent
        time.sleep(0.5)
        assert frames.empty()
    finally:
        gw.close()
        alice.close()


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"{len(tests)} passed")
