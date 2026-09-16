"""
Signed capsule session over any Transport.

Identity: trust on first use (TOFU). A node's hello carries its Ed25519
public key and is signed by it. The receiver pins agent id -> key on the
first hello; every later capsule from that agent must verify against the
pinned key. A different key for a pinned agent is rejected.

Every capsule sent or received is stored with its envelope signature.
"""

import base64
import dataclasses
import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
)

from envelope import open_envelope, sign, verify
from handshake import TOPIC as HELLO_TOPIC, make_hello
from interpreter import ingest, to_wire
from primitive import Capsule, Claim, ClaimType
from store import Store

KEY_STATEMENT = "public_key"
KEY_PREFIX = "ed25519="


class PeerRejected(Exception):
    pass


def _name(agent_id: str) -> str:
    return agent_id.removeprefix("agent://")


def load_or_create_key(agent_id: str, key_dir: str = "keys") -> Ed25519PrivateKey:
    path = Path(key_dir) / f"{_name(agent_id)}.ed25519"
    if path.exists():
        return Ed25519PrivateKey.from_private_bytes(base64.b64decode(path.read_text()))
    key = Ed25519PrivateKey.generate()
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = key.private_bytes_raw()
    path.write_text(base64.b64encode(raw).decode("ascii"))
    return key


def public_b64(pub: Ed25519PublicKey) -> str:
    return base64.b64encode(pub.public_bytes(Encoding.Raw, PublicFormat.Raw)).decode("ascii")


class Peer:
    def __init__(self, agent_id: str, transport, store: Store,
                 key_dir: str = "keys", pins_path: str | None = None):
        self.agent_id = agent_id
        self.transport = transport
        self.store = store
        self.key = load_or_create_key(agent_id, key_dir)
        self.pins_path = Path(pins_path or f"store/{_name(agent_id)}.pins.json")
        self.pins: dict[str, str] = (
            json.loads(self.pins_path.read_text()) if self.pins_path.exists() else {}
        )
        # result of the most recent hello: "new" (first contact, pinned now)
        # or "known" (matched an existing pin)
        self.last_pin: str | None = None

    # --- outbound ---

    def hello(self, remote_id: str) -> Capsule:
        """Handshake capsule carrying this node's public key."""
        h = make_hello(self.agent_id, remote_id)
        key_claim = Claim(
            statement=KEY_STATEMENT,
            type=ClaimType.OBSERVATION,
            confidence=1.0,
            evidence=(KEY_PREFIX + public_b64(self.key.public_key()),),
        )
        return dataclasses.replace(
            h, semantics=dataclasses.replace(h.semantics, claims=h.semantics.claims + (key_claim,))
        )

    def send(self, c: Capsule) -> dict:
        if c.sender != self.agent_id:
            raise ValueError(f"{self.agent_id} cannot send as {c.sender}")
        wire = sign(to_wire(c), self.key, pubkey_id=self.agent_id).to_wire()
        self.store.append(wire["capsule"], envelope=wire)
        self.transport.send(c.receiver, wire)
        return wire

    # --- inbound ---

    def recv(self) -> Capsule:
        _, wire = self.transport.recv()
        try:
            env = open_envelope(wire)
            cap = env.capsule
            sender = cap["from"]
        except (KeyError, TypeError, ValueError) as e:
            raise PeerRejected(f"malformed envelope: {e}") from e

        if env.pubkey_id != sender:
            raise PeerRejected(f"envelope signed as {env.pubkey_id} but capsule is from {sender}")
        if cap.get("to") != self.agent_id:
            raise PeerRejected(f"capsule is addressed to {cap.get('to')}, not {self.agent_id}")

        if cap.get("semantics", {}).get("topic") == HELLO_TOPIC:
            self._pin_from_hello(sender, cap)

        pinned = self.pins.get(sender)
        if pinned is None:
            raise PeerRejected(f"no pinned key for {sender}; hello first")
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(pinned))
        if not verify(env, pub):
            raise PeerRejected(f"bad signature from {sender}")

        c = ingest(cap)                       # schema + semantic validation
        self.store.append(cap, envelope=wire)
        return c

    def _pin_from_hello(self, sender: str, cap: dict) -> None:
        offered = None
        for cl in cap["semantics"].get("claims", []):
            if cl.get("statement") == KEY_STATEMENT:
                for e in cl.get("evidence", []):
                    if e.startswith(KEY_PREFIX):
                        offered = e[len(KEY_PREFIX):]
        if offered is None:
            raise PeerRejected(f"hello from {sender} carries no public key")
        known = self.pins.get(sender)
        if known is not None and known != offered:
            raise PeerRejected(f"key for {sender} changed since first contact")
        if known is None:
            self.pins[sender] = offered
            self.pins_path.parent.mkdir(parents=True, exist_ok=True)
            self.pins_path.write_text(json.dumps(self.pins, indent=2))
            self.last_pin = "new"
        else:
            self.last_pin = "known"
