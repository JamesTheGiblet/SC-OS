"""
Edge outcomes and the gateway: the stripped format's outcome fields, and a device
that reports a threshold, receives the rule's task and reports the outcome,
through a real Alice server over localhost TCP.
Run from the project root: python tests/test_gateway.py
"""

import json
import queue
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents.echo import EchoAgent
from edge.gateway import EdgeGateway, short_id
from edge.upgrade import from_edge_wire, to_edge_wire, validate_edge
from hal.transport import SocketListener, SocketTransport
from interpreter import to_wire
from peer import Node
from primitive import Outcome
from rules.engine import RuleEngine
import run_alice
from run_alice import Server
from scheduler import Scheduler
from sensing import SensorObserver
from store import Store
from validator import CapsuleRejected, validate

ALICE = "agent://alice"
run_alice.log = lambda msg: None          # keep the server quiet during tests
VERIFY_RULE = {
    "when": {"topic": "*_risk", "trigger": "threshold", "min_confidence": 0.8},
    "then": [{"intent": "request", "to": "{from}", "trigger": "task", "topic": "{topic}",
              "claims": [{"type": "directive", "statement": "verify: {claim}"}], "ttl_seconds": 600}],
}


def edge(**kw) -> dict:
    d = {"v": "1.0", "id": "r1", "to": "alice", "i": "inform", "t": "tilt_risk", "c": 0.9,
         "s": "tilted 40 degrees", "tr": "threshold"}
    d.update(kw)
    return d


def rejected(fn, *args, **kw) -> str:
    try:
        fn(*args, **kw)
    except CapsuleRejected as e:
        return e.code
    raise AssertionError("expected CapsuleRejected")


# --- stripped format --------------------------------------------------------

def test_outcome_fields_only_on_task_result():
    validate_edge(edge())
    validate_edge(edge(tr="task_result", re="abc", o="success", od="checked"))
    validate_edge(edge(re="abc"))                                  # re alone is fine anywhere
    assert rejected(validate_edge, edge(tr="task_result")) == "edge_schema"            # no o, re
    assert rejected(validate_edge, edge(tr="task_result", re="abc")) == "edge_schema"  # no o
    assert rejected(validate_edge, edge(tr="task_result", o="success")) == "edge_schema"
    assert rejected(validate_edge, edge(o="success")) == "edge_schema"                 # not a result
    assert rejected(validate_edge, edge(od="why")) == "edge_schema"
    assert rejected(validate_edge, edge(tr="task_result", re="abc", o="maybe")) == "edge_schema"
    assert rejected(validate_edge, edge(re="x" * 17)) == "edge_schema"


def test_upgraded_task_result_is_a_valid_capsule():
    task_id = "urn:uuid:12345678-1234-1234-1234-123456789abc"
    c = from_edge_wire(edge(tr="task_result", re="123456781234", o="failure", od="not tilted"),
                       sender="agent://m5-a1b2c3", receiver=ALICE, derived_from=(task_id,))
    assert c.outcome == Outcome("failure", "not tilted")
    assert c.provenance.derived_from == (task_id,)
    validate(to_wire(c))
    assert rejected(from_edge_wire, edge(tr="task_result", re="abc", o="success"),
                    sender="agent://m5", receiver=ALICE) == "edge_unknown_task"


def test_downgrade_carries_re_and_outcome():
    c = from_edge_wire(edge(tr="task_result", re="abc", o="success", od="ok"),
                       sender="agent://m5", receiver=ALICE, derived_from=("urn:uuid:" + "0" * 8 + "-0000-0000-0000-" + "0" * 12,))
    wire = to_edge_wire(c, id_short="r9", re="abc")
    assert (wire["re"], wire["o"], wire["od"]) == ("abc", "success", "ok")
    plain = to_edge_wire(from_edge_wire(edge(), sender="agent://m5", receiver=ALICE), id_short="r1")
    assert "o" not in plain and "re" not in plain


# --- gateway against a real Alice -------------------------------------------

class AliceServer:
    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = Store(str(self.tmp / "alice.db"))
        self.node = Node(ALICE, self.store, key_dir=str(self.tmp / "keys"),
                         pins_path=str(self.tmp / "alice.pins.json"))
        self.engine = RuleEngine(ALICE, self.store, self.node.key)
        self.rule_id = self.engine.issue("verify-risk", VERIFY_RULE["when"], VERIFY_RULE["then"])
        self.sched = Scheduler({ALICE: EchoAgent()}, self.store, rules=self.engine,
                               observers=(SensorObserver(self.store),))
        self.server = Server(self.node, self.sched)
        self.listener = SocketListener("127.0.0.1", 0, poll_seconds=0.2)
        self.port = self.listener.sock.getsockname()[1]
        self.running = True
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while self.running:
            try:
                accepted = self.listener.accept()
            except OSError:                  # listener closed
                return
            if accepted:
                threading.Thread(target=self.server.handle, args=accepted, daemon=True).start()

    def close(self):
        self.running = False
        self.listener.close()


def start(alice: AliceServer, root: Path | None = None) -> tuple[EdgeGateway, queue.Queue]:
    frames: queue.Queue = queue.Queue()
    gw = EdgeGateway(ALICE, lambda: SocketTransport("connect", "127.0.0.1", alice.port),
                     root or Path(tempfile.mkdtemp()), frames.put, log=lambda m: None)
    return gw, frames


def next_frame(frames: queue.Queue, timeout: float = 5.0) -> dict:
    return frames.get(timeout=timeout)


def test_device_outcome_teaches_alice_rule():
    alice = AliceServer()
    gw, frames = start(alice)
    try:
        sent = gw.handle_frame({"src": "m5-a1b2c3", "cap": edge(id="r1")})
        assert sent.sender == "agent://m5-a1b2c3"

        down = {}
        for _ in range(2):                     # ACK for our report, and the rule's task
            f = next_frame(frames)
            assert f["dst"] == "m5-a1b2c3"
            down[f["cap"]["i"]] = f["cap"]
        assert down["ack"]["re"] == "r1"       # the ACK names the device's own message
        task = down["request"]
        assert task["tr"] == "task" and task["s"].startswith("verify: tilted 40 degrees")
        validate_edge(task)

        result = gw.handle_frame({"src": "m5-a1b2c3", "cap": edge(
            id="r2", i="inform", tr="task_result", re=task["id"], o="success", od="re-checked")})
        assert result.outcome.success
        assert next_frame(frames)["cap"]["i"] == "ack"

        deadline = time.monotonic() + 5
        while alice.store.get_opinion(f"rule:{alice.rule_id}")["evidence_count"] == 0:
            assert time.monotonic() < deadline, "Alice never learned from the device's outcome"
            time.sleep(0.05)
        rule = alice.store.get_opinion(f"rule:{alice.rule_id}")
        assert rule["evidence_count"] == 1 and rule["value"] > 1.0
        topic = alice.sched.opinion("tilt_risk")
        assert topic.evidence_count == 1

        # the task and result are in Alice's ledger, signed as the device's agent
        records = [r for r in alice.store.records() if r["capsule"]["from"] == "agent://m5-a1b2c3"]
        assert records and all(r["envelope"]["pubkey_id"] == "agent://m5-a1b2c3" for r in records)
    finally:
        gw.close()
        alice.close()


def test_gateway_rejects_what_it_cannot_vouch_for():
    alice = AliceServer()
    gw, frames = start(alice)
    try:
        cases = {
            "edge_frame": [{"cap": edge()}, {"src": "M5 BAD", "cap": edge()}, {"src": "m5"}, "text"],
            "edge_schema": [{"src": "m5", "cap": {"v": "1.0"}}],
            "edge_route": [{"src": "m5", "cap": edge(to="bob")}],
            "edge_unknown_task": [{"src": "m5", "cap": edge(id="r5", tr="task_result", re="nosuchtask", o="success")}],
        }
        for code, frames_in in cases.items():
            for f in frames_in:
                assert rejected(gw.handle_frame, f) == code, (code, f)

        gw.handle_frame({"src": "m5", "cap": edge(id="once", t="status", tr="none")})
        assert rejected(gw.handle_frame, {"src": "m5", "cap": edge(id="once", t="status", tr="none")}) \
            == "edge_duplicate"
        gw.handle_frame({"src": "m5-other", "cap": edge(id="once", t="status", tr="none")})  # other device: fine
    finally:
        gw.close()
        alice.close()


def test_result_counts_after_the_session_drops():
    alice = AliceServer()
    root = Path(tempfile.mkdtemp())
    gw, frames = start(alice, root)
    try:
        gw.handle_frame({"src": "m5", "cap": edge(id="a1")})
        caps = [next_frame(frames)["cap"] for _ in range(2)]
        task = next(c for c in caps if c["tr"] == "task")

        device = gw.devices["m5"]
        with device.lock:
            gw._drop(device, "test: simulated idle timeout")
        assert device.peer is None

        gw.handle_frame({"src": "m5", "cap": edge(id="a2", tr="task_result", re=task["id"], o="failure")})
        assert device.peer is not None                     # a new session was opened
        assert next_frame(frames)["cap"]["i"] == "ack"
        deadline = time.monotonic() + 5
        while alice.store.get_opinion(f"rule:{alice.rule_id}")["evidence_count"] == 0:
            assert time.monotonic() < deadline
            time.sleep(0.05)
        assert alice.store.get_opinion(f"rule:{alice.rule_id}")["value"] < 1.0
    finally:
        gw.close()
        alice.close()


def test_device_firmware_protocol_through_gateway():
    """The M5 firmware's protocol module (no hardware), driving the gateway and a real Alice."""
    sys.path.insert(0, str(ROOT / "firmware" / "m5stickc_plus2"))
    from sctalk import Talk

    alice = AliceServer()
    gw, frames = start(alice)
    try:
        talk = Talk("m5-00aa11", "b00t", lambda line: gw.handle_frame(json.loads(line)))
        assert talk.complete(True) is None                 # no task yet: nothing sent

        rid = talk.report("tilt_risk", "tilted 52 degrees", 0.9)
        seen = [talk.receive_line(json.dumps(next_frame(frames))) for _ in range(2)]
        assert sorted(seen) == ["ack", "task"] and rid in talk.acked
        assert talk.task["s"].startswith("verify: tilted 52 degrees")

        assert talk.receive_line("# a note for a person") is None
        assert talk.receive_line(chr(0) + chr(255) + json.dumps({"dst": "m5-00aa11", "cap": {"i": "ack"}})) == "ack"
        assert talk.receive_line(json.dumps({"dst": "m5-other", "cap": {}})) is None

        done = talk.complete(False, "denied by button B")
        assert talk.task is None
        assert talk.receive_line(json.dumps(next_frame(frames))) == "ack" and done in talk.acked

        deadline = time.monotonic() + 5
        while alice.store.get_opinion(f"rule:{alice.rule_id}")["evidence_count"] == 0:
            assert time.monotonic() < deadline
            time.sleep(0.05)
        assert alice.store.get_opinion(f"rule:{alice.rule_id}")["value"] < 1.0   # failure counted

        # a rebooted device uses a new boot tag, so its ids don't collide with the old ones
        again = Talk("m5-00aa11", "c0de", lambda line: gw.handle_frame(json.loads(line)))
        again.report("status", "m5-00aa11 online", 1.0, trigger="announce")
    finally:
        gw.close()
        alice.close()


def sensor(**kw) -> dict:
    s = {"id": "imu_temp", "type": "temperature", "bus": "i2c0:0x68", "pin": "21,22", "unit": "C",
         "min": -40, "max": 85, "margin_low": 0, "margin_high": 65, "sample_ms": 200}
    s.update(kw)
    return s


def test_sensor_list_checks():
    from edge.sensors import validate_sensor_list
    validate_sensor_list({"src": "m5", "sensors": [sensor()], "absent": ["mic: no PDM"]})
    bad = {
        "no sensors": {"src": "m5", "sensors": []},
        "bad id": {"src": "m5", "sensors": [sensor(id="Temp!")]},
        "missing field": {"src": "m5", "sensors": [{k: v for k, v in sensor().items() if k != "unit"}]},
        "extra field": {"src": "m5", "sensors": [sensor(colour="red")]},
        "bad src": {"src": "M5 BAD", "sensors": [sensor()]},
        "duplicate id": {"src": "m5", "sensors": [sensor(), sensor()]},
        "margin below min": {"src": "m5", "sensors": [sensor(margin_low=-50)]},
        "margins crossed": {"src": "m5", "sensors": [sensor(margin_low=70, margin_high=60)]},
        "margin above max": {"src": "m5", "sensors": [sensor(margin_high=90)]},
        "sample_ms zero": {"src": "m5", "sensors": [sensor(sample_ms=0)]},
    }
    for label, frame in bad.items():
        assert rejected(validate_sensor_list, frame) == "edge_sensors", label


def test_sensors_capsule_shape():
    from edge.sensors import FIELDS, TOPIC, parse_evidence, sensors_capsule
    frame = {"src": "m5", "sensors": [sensor(), sensor(id="battery", type="voltage", bus="adc", pin="38",
                                                     unit="V", min=0, max=5, margin_low=3.3,
                                                     margin_high=4.35)],
             "absent": ["mic: no PDM input"]}
    c = sensors_capsule(frame, sender="agent://m5", receiver=ALICE)
    validate(to_wire(c))
    assert c.semantics.topic == TOPIC and c.trigger.value == "announce"
    assert [cl.statement for cl in c.semantics.claims] == ["sensor:imu_temp", "sensor:battery"]
    assert c.semantics.claims[1].evidence == (
        "id=battery", "type=voltage", "bus=adc", "pin=38", "unit=V", "min=0", "max=5",
        "margin_low=3.3", "margin_high=4.35", "sample_ms=200")
    assert all(len(cl.evidence) == len(FIELDS) for cl in c.semantics.claims)
    assert c.semantics.uncertainty.known_unknowns == ("mic: no PDM input",)
    back = parse_evidence(to_wire(c)["semantics"]["claims"][1])
    assert back["margin_low"] == 3.3 and back["sample_ms"] == 200 and back["unit"] == "V"


def test_device_sensor_list_reaches_alice_signed():
    """The firmware's own sensor list, through the gateway, into Alice's ledger."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("fw_sctalk", ROOT / "firmware" / "m5stickc_plus2" / "sctalk.py")
    sctalk = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sctalk)
    description, absent = firmware_description()

    alice = AliceServer()
    gw, frames = start(alice)
    try:
        talk = sctalk.Talk("m5-00aa11", "d0d0", lambda line: gw.handle_frame(json.loads(line)))
        assert talk.receive_line(json.dumps({"dst": "*", "cmd": "describe"})) == "describe"
        assert talk.receive_line(json.dumps({"dst": "m5-other", "cmd": "describe"})) is None
        talk.describe(description, absent)
        assert next_frame(frames)["cap"]["i"] == "ack"

        deadline = time.monotonic() + 5
        while not alice.store.find(topic="__sensors__"):
            assert time.monotonic() < deadline, "the sensor capsule never reached Alice"
            time.sleep(0.05)
        rec = alice.store.get_record(alice.store.find(topic="__sensors__")[0]["digest"])
        cap = rec["capsule"]
        assert cap["from"] == "agent://m5-00aa11" and rec["envelope"]["pubkey_id"] == "agent://m5-00aa11"
        ids = [cl["statement"] for cl in cap["semantics"]["claims"]]
        assert ids == [f"sensor:{s['id']}" for s in description]
        assert "sensor:tilt" in ids and "sensor:battery" in ids
        assert cap["semantics"]["uncertainty"]["known_unknowns"] == list(absent)
    finally:
        gw.close()
        alice.close()


def firmware_description():
    """DESCRIPTION and ABSENT from firmware/m5stickc_plus2/sensors.py, without importing its hardware modules."""
    import ast
    tree = ast.parse((ROOT / "firmware" / "m5stickc_plus2" / "sensors.py").read_text(encoding="utf-8"))
    found = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) \
                and node.targets[0].id in ("DESCRIPTION", "ABSENT"):
            found[node.targets[0].id] = ast.literal_eval(node.value)
    return found["DESCRIPTION"], found["ABSENT"]


def test_firmware_files_compile():
    """MicroPython can't run here, but every firmware file must at least be valid Python."""
    files = sorted((ROOT / "firmware" / "m5stickc_plus2").glob("*.py"))
    assert {f.name for f in files} >= {"main.py", "sctalk.py", "sensors.py", "st7789.py", "deploy.py"}
    for f in files:
        compile(f.read_text(encoding="utf-8"), str(f), "exec")


def test_short_id():
    assert short_id("urn:uuid:12345678-9abc-def0-1234-56789abcdef0") == "123456789abc"


def test_run_gateway_stdio_end_to_end():
    """The script, as a device would drive it: frames in on stdin, frames out on stdout."""
    alice = AliceServer()
    try:
        proc = subprocess.Popen(
            [sys.executable, str(ROOT / "run_gateway.py"), "--stdio", "--port", str(alice.port)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=tempfile.mkdtemp(), env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"})
        proc.stdin.write("boot noise from the device\n")
        proc.stdin.write("# ignored 40 bytes: '{\"dst\": \"nettest-gw\", \"cap\": {\"v\"'\n")   # a note, not a frame
        proc.stdin.write(json.dumps({"src": "nettest-gw", "cap": edge(id="s1")}) + "\n")
        proc.stdin.flush()
        out = [json.loads(proc.stdout.readline()) for _ in range(2)]
        task = next(f["cap"] for f in out if f["cap"]["tr"] == "task")
        proc.stdin.write(json.dumps({"src": "nettest-gw", "cap": edge(
            id="s2", tr="task_result", re=task["id"], o="success")}) + "\n")
        proc.stdin.flush()
        ack = json.loads(proc.stdout.readline())
        assert ack["cap"]["i"] == "ack" and ack["cap"]["re"] == "s2"
        proc.stdin.close()
        err = proc.stderr.read()
        proc.wait(timeout=10)
        assert "outcome=success -> alice" in err, err
        assert "Traceback" not in err, err
    finally:
        alice.close()
        for p in ("keys/nettest-gw.ed25519", "store/nettest-gw.pins.json"):
            (ROOT / p).unlink(missing_ok=True)


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"{len(tests)} passed")
