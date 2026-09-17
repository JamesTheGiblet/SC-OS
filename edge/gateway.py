"""
Edge gateway: bridges stripped edge capsules on a local link (USB serial now,
ESP-NOW through a gateway radio later) to signed sessions with a master node.

Link frames are one JSON object per line:

    device -> gateway   {"src": "m5-a1b2c3", "cap": <stripped capsule>}
    device -> gateway   {"src": "m5-a1b2c3", "sensors": [...], "absent": [...]}   (see edge/sensors.py)
    gateway -> device   {"dst": "m5-a1b2c3", "cap": <stripped capsule>}
    gateway -> device   {"dst": "*", "cmd": "describe"}    (ask any device to send its sensor list)

Each device is its own agent, agent://<src>. The gateway holds that agent's key
and opens one session to the master per device, so a task reaches the device
through its own session and the device's outcome arrives signed as the agent
the task was sent to. Devices don't sign; the gateway is the trust boundary.

Short ids: the edge format allows 16 characters, capsule ids are 45. The gateway
gives each capsule it forwards to a device a short id and remembers it, so a
task_result's `re` resolves to the full task id. A reply from the master carries
`re` set to the device's own id for the message it answers.

Replays: a device message id seen before from that device is rejected. Upgraded
capsules get fresh ids and times, so the master's digest check can't catch a
repeated radio frame; the gateway must.

Sessions drop when idle (the master times out) or on error; the next frame from
that device opens a new one. Short-id tables belong to the device, not the
session, so a result can arrive on a later session than its task.
"""

from __future__ import annotations

import re
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from edge.sensors import sensors_capsule, validate_sensor_list
from edge.upgrade import from_edge_wire, to_edge_wire, validate_edge
from handshake import TOPIC as HELLO_TOPIC, negotiate
from peer import Node, Peer, PeerRejected
from primitive import Capsule, Trigger
from store import Store
from validator import CapsuleRejected

SRC_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,15}$")   # agent name, fits the 16-char "to" field
SEEN_LIMIT = 1024


def short_id(capsule_id: str) -> str:
    """First 12 hex digits of a urn:uuid capsule id."""
    return capsule_id.removeprefix("urn:uuid:").replace("-", "")[:12]


def _remember(table: OrderedDict, key, value, limit: int = SEEN_LIMIT) -> None:
    table[key] = value
    table.move_to_end(key)
    while len(table) > limit:
        table.popitem(last=False)


@dataclass
class Device:
    """What the gateway knows about one device. Outlives any one session."""
    name: str
    node: Node
    lock: threading.Lock = field(default_factory=threading.Lock)
    peer: Peer | None = None
    transport: object | None = None
    tasks: OrderedDict = field(default_factory=OrderedDict)      # short id -> (task id, expires)
    edge_ids: OrderedDict = field(default_factory=OrderedDict)   # upgraded capsule id -> device's id
    seen: OrderedDict = field(default_factory=OrderedDict)       # device ids already forwarded

    @property
    def agent(self) -> str:
        return self.node.agent_id


class EdgeGateway:
    def __init__(self, master: str, connect: Callable[[], object], root: Path,
                 write_frame: Callable[[dict], None], log: Callable[[str], None] = print):
        self.master = master
        self.connect = connect
        self.root = Path(root)
        self.store = Store(str(self.root / "store" / "gateway.db"))
        self._write_frame = write_frame
        self._write_lock = threading.Lock()
        self.log = log
        self.devices: dict[str, Device] = {}
        self._devices_lock = threading.Lock()

    # --- device -> master ---

    def handle_frame(self, frame: dict) -> Capsule:
        """Upgrade one device frame and send it to the master. Raises CapsuleRejected."""
        if isinstance(frame, dict) and "sensors" in frame:
            return self._describe(frame)
        if not isinstance(frame, dict) or not isinstance(frame.get("cap"), dict):
            raise CapsuleRejected("edge_frame", "frame must be {\"src\": ..., \"cap\": {...}}")
        src = frame.get("src")
        if not isinstance(src, str) or not SRC_PATTERN.match(src):
            raise CapsuleRejected("edge_frame", f"bad src {src!r}")
        cap = frame["cap"]
        validate_edge(cap)
        if f"agent://{cap['to']}" != self.master:
            raise CapsuleRejected("edge_route", f"to={cap['to']!r}: this gateway only reaches {self.master}")

        device = self._device(src)
        with device.lock:
            if cap["id"] in device.seen:
                raise CapsuleRejected("edge_duplicate", f"{src} already sent id={cap['id']!r}")
            derived: tuple[str, ...] = ()
            if cap.get("re"):
                task = device.tasks.get(cap["re"])
                if task is not None:
                    derived = (task[0],)
            if cap["tr"] == "task_result" and derived:
                if device.tasks[cap["re"]][1] < datetime.now(timezone.utc):
                    raise CapsuleRejected("edge_unknown_task", f"task re={cap['re']!r} has expired")
            c = from_edge_wire(cap, sender=device.agent, receiver=self.master, derived_from=derived)
            self._send(device, c)
            _remember(device.seen, cap["id"], True)
            _remember(device.edge_ids, c.id, cap["id"])
        return c

    def _describe(self, frame: dict) -> Capsule:
        """A device's sensor list becomes a __sensors__ capsule, sent as the device's agent."""
        validate_sensor_list(frame)
        device = self._device(frame["src"])
        c = sensors_capsule(frame, sender=device.agent, receiver=self.master)
        with device.lock:
            self._send(device, c)
        return c

    def _send(self, device: Device, c: Capsule) -> None:
        """Send on the device's session, opening one if needed; retry once on a fresh session."""
        peer = self._session(device)
        try:
            peer.send(c)
        except (OSError, ConnectionError):
            self._drop(device, "send failed; retrying on a new session")
            self._session(device).send(c)

    # --- master -> device ---

    def _forward(self, device: Device, c: Capsule) -> None:
        sid = short_id(c.id)
        if c.trigger == Trigger.TASK:
            expires = c.created + timedelta(seconds=c.action_hints.ttl_seconds)
            _remember(device.tasks, sid, (c.id, expires))
        answers = next((device.edge_ids[p] for p in c.provenance.derived_from if p in device.edge_ids), None)
        wire = to_edge_wire(c, id_short=sid, re=answers)
        with self._write_lock:
            self._write_frame({"dst": device.name, "cap": wire})

    def _read(self, device: Device, peer: Peer) -> None:
        try:
            while True:
                c = peer.recv()
                with device.lock:
                    self._forward(device, c)
                self.log(f"[{device.name}] {c.intent.value.upper()} from {c.sender} -> device"
                         + (" (task)" if c.trigger == Trigger.TASK else ""))
        except (ConnectionError, OSError) as e:
            with device.lock:
                if device.peer is peer:
                    self._drop(device, f"session ended ({e or type(e).__name__})")
        except (PeerRejected, CapsuleRejected) as e:
            with device.lock:
                if device.peer is peer:
                    self._drop(device, f"REJECT {type(e).__name__}: {e}")

    # --- sessions ---

    def _device(self, src: str) -> Device:
        with self._devices_lock:
            device = self.devices.get(src)
            if device is None:
                node = Node(f"agent://{src}", self.store, key_dir=str(self.root / "keys"),
                            pins_path=str(self.root / "store" / f"{src}.pins.json"))
                device = self.devices[src] = Device(src, node)
            return device

    def _session(self, device: Device) -> Peer:
        """The device's open session, or a new one after a hello in each direction."""
        if device.peer is not None:
            return device.peer
        transport = self.connect()
        peer = device.node.session(transport)
        try:
            local = peer.hello(self.master)
            peer.send(local)
            remote = peer.recv()
            if remote.semantics.topic != HELLO_TOPIC or remote.sender != self.master:
                raise PeerRejected(f"expected a hello from {self.master}")
            negotiate(local, remote)
        except Exception:
            transport.close()
            raise
        pin = "first contact, key pinned" if peer.last_pin == "new" else "key matches pin"
        self.log(f"[{device.name}] session open as {device.agent}, {pin}")
        device.peer, device.transport = peer, transport
        threading.Thread(target=self._read, args=(device, peer), daemon=True).start()
        return peer

    def _drop(self, device: Device, why: str) -> None:
        if device.transport is not None:
            try:
                device.transport.close()
            except OSError:
                pass
        device.peer = device.transport = None
        self.log(f"[{device.name}] {why}")

    def close(self) -> None:
        for device in list(self.devices.values()):
            with device.lock:
                if device.peer is not None:
                    self._drop(device, "gateway closing")
        self.store.close()
