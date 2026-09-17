"""
Signed capsule sessions over any Transport.

Node: one agent's identity and shared state (key, pins, ledger). Safe to
share across sessions and threads.

Peer: one session over one transport, bound to one remote agent.

Identity is trust on first use (TOFU). A hello carries the sender's Ed25519
public key and is signed by it. The first valid hello from an agent pins
agent id -> key; every later capsule from that agent must verify against the
pinned key, and a different key is rejected.

Replay protection: every accepted capsule is in the ledger, so a capsule
whose digest is already there is rejected, across restarts too. Expired
capsules are rejected, so a replay can't outlive the record that catches it.
"""

import base64
import dataclasses
import json
import threading
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from envelope import digest, open_envelope, sign, verify
from handshake import TOPIC as HELLO_TOPIC, make_hello
from interpreter import ingest, is_expired, to_wire
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
    path.write_text(base64.b64encode(key.private_bytes_raw()).decode("ascii"))
    return key


def public_b64(pub: Ed25519PublicKey) -> str:
    return base64.b64encode(pub.public_bytes(Encoding.Raw, PublicFormat.Raw)).decode("ascii")


def offered_key(cap: dict) -> str | None:
    """The base64 Ed25519 public key a capsule carries in a public_key claim, if any."""
    return _offered_key(cap)


def _offered_key(cap: dict) -> str | None:
    for cl in cap.get("semantics", {}).get("claims", []):
        if cl.get("statement") == KEY_STATEMENT:
            for e in cl.get("evidence", []):
                if e.startswith(KEY_PREFIX):
                    return e[len(KEY_PREFIX):]
    return None


class Node:
    def __init__(self, agent_id: str, store: Store,
                 key_dir: str = "keys", pins_path: str | None = None):
        self.agent_id = agent_id
        self.store = store
        self.key = load_or_create_key(agent_id, key_dir)
        self.pins_path = Path(pins_path or f"store/{_name(agent_id)}.pins.json")
        self.pins: dict[str, str] = (
            json.loads(self.pins_path.read_text()) if self.pins_path.exists() else {}
        )
        # held while checking and recording anything shared: pins, replay, ledger
        self.lock = threading.RLock()

    def session(self, transport) -> "Peer":
        return Peer(self, transport)

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

    def pin(self, agent_id: str, key_b64: str) -> str:
        """
        Pin a key learned out of band (e.g. from a verified spawn bundle), so the
        first hello must match it. Returns "new" or "known"; a different key for an
        already pinned agent raises PeerRejected.
        """
        with self.lock:
            known = self.pins.get(agent_id)
            if known is not None and known != key_b64:
                raise PeerRejected(f"key for {agent_id} differs from the pinned key")
            if known is None:
                self._save_pin(agent_id, key_b64)
                return "new"
            return "known"

    def _save_pin(self, agent_id: str, key_b64: str) -> None:
        self.pins[agent_id] = key_b64
        self.pins_path.parent.mkdir(parents=True, exist_ok=True)
        self.pins_path.write_text(json.dumps(self.pins, indent=2))


class Peer:
    def __init__(self, node: Node, transport):
        self.node = node
        self.transport = transport
        self.remote: str | None = None   # the one agent this session talks to
        self.greeted = False             # received a valid hello on this session
        # result of the most recent hello: "new" (pinned now) or "known" (matched pin)
        self.last_pin: str | None = None

    @property
    def agent_id(self) -> str:
        return self.node.agent_id

    @property
    def store(self) -> Store:
        return self.node.store

    def hello(self, remote_id: str) -> Capsule:
        return self.node.hello(remote_id)

    # --- outbound ---

    def send(self, c: Capsule) -> dict:
        if c.sender != self.agent_id:
            raise ValueError(f"{self.agent_id} cannot send as {c.sender}")
        if self.remote is None:
            self.remote = c.receiver
        elif c.receiver != self.remote:
            raise ValueError(f"session is with {self.remote}, not {c.receiver}")
        with self.node.lock:
            wire = sign(to_wire(c), self.node.key, pubkey_id=self.agent_id).to_wire()
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
            is_hello = cap.get("semantics", {}).get("topic") == HELLO_TOPIC
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            raise PeerRejected(f"malformed envelope: {e}") from e

        if env.pubkey_id != sender:
            raise PeerRejected(f"envelope signed as {env.pubkey_id} but capsule is from {sender}")
        if cap.get("to") != self.agent_id:
            raise PeerRejected(f"capsule is addressed to {cap.get('to')}, not {self.agent_id}")
        if self.remote is not None and sender != self.remote:
            raise PeerRejected(f"session is with {self.remote}, not {sender}")
        if not self.greeted and not is_hello:
            raise PeerRejected(f"expected a hello from {sender} before anything else")

        with self.node.lock:
            pinned = self.node.pins.get(sender)
            offered = _offered_key(cap) if is_hello else None
            if is_hello and offered is None:
                raise PeerRejected(f"hello from {sender} carries no public key")
            if is_hello and pinned is not None and offered != pinned:
                raise PeerRejected(f"key for {sender} changed since first contact")
            key_b64 = pinned or offered
            if key_b64 is None:
                raise PeerRejected(f"no pinned key for {sender}; hello first")

            try:
                pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(key_b64))
            except ValueError as e:
                raise PeerRejected(f"unusable public key for {sender}: {e}") from e
            if not verify(env, pub):
                raise PeerRejected(f"bad signature from {sender}")

            c = ingest(cap)               # schema + semantic validation
            if is_expired(c):
                raise PeerRejected(f"capsule {c.id} from {sender} has expired")
            if self.store.has(digest(cap)):
                raise PeerRejected(f"replay: capsule {c.id} from {sender} was already received")

            # only a verified, valid, fresh hello may create a pin
            if is_hello and pinned is None:
                self.node._save_pin(sender, offered)
            self.store.append(cap, envelope=wire)

        if is_hello:
            self.greeted = True
            self.remote = sender
            self.last_pin = "new" if pinned is None else "known"
        return c
