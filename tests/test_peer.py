"""
Peer sessions: TOFU key pinning, replay protection, session binding,
and the rejections they must make.
Run from the project root: python tests/test_peer.py
"""

import base64
import copy
import sys
import tempfile
import threading
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from envelope import sign
from interpreter import to_wire
from peer import KEY_PREFIX, KEY_STATEMENT, Node, PeerRejected, public_b64
from primitive import ActionHints, Capsule, Claim, ClaimType, Intent, Semantics
from store import Store

ALICE, BOB, CAROL, MALLORY = "agent://alice", "agent://bob", "agent://carol", "agent://mallory"


class Pipe:
    """In-memory Transport: what one end sends, the other end receives."""
    def __init__(self):
        self.inbox: deque = deque()
        self.other: "Pipe | None" = None

    def send(self, peer, envelope):
        assert self.other is not None
        self.other.inbox.append(copy.deepcopy(envelope))

    def recv(self):
        if not self.inbox:
            raise ConnectionError("empty")
        env = self.inbox.popleft()
        return env.get("pubkey_id", "unknown"), env

    def inject(self, envelope):
        """Put a raw envelope in this end's inbox, as if it came off the wire."""
        self.inbox.append(copy.deepcopy(envelope))


def pipes():
    a, b = Pipe(), Pipe()
    a.other, b.other = b, a
    return a, b


def make_node(agent_id, root: Path, key_dir: str = "keys") -> Node:
    name = agent_id[8:]
    return Node(agent_id, Store(str(root / f"{name}.log")),
                key_dir=str(root / key_dir), pins_path=str(root / f"{name}.pins.json"))


def inform(sender, receiver, text="hi", created=None, ttl=3600) -> Capsule:
    return Capsule(
        id=f"urn:uuid:{uuid.uuid4()}",
        created=created or datetime.now(timezone.utc),
        sender=sender, receiver=receiver, intent=Intent.INFORM,
        semantics=Semantics(topic="t", claims=(Claim(text, ClaimType.OBSERVATION, 0.5),)),
        action_hints=ActionHints(ttl_seconds=ttl),
    )


def rejects(fn, needle):
    try:
        fn()
    except PeerRejected as e:
        assert needle in str(e), f"expected '{needle}' in '{e}'"
        return
    raise AssertionError(f"expected PeerRejected containing '{needle}'")


def session(root=None):
    """Bob connects to Alice and they exchange hellos. Returns sessions and pipes."""
    root = root or Path(tempfile.mkdtemp())
    alice_node, bob_node = make_node(ALICE, root), make_node(BOB, root)
    at_alice, at_bob = pipes()
    alice, bob = alice_node.session(at_alice), bob_node.session(at_bob)
    bob.send(bob.hello(ALICE))
    alice.recv()
    alice.send(alice.hello(BOB))
    bob.recv()
    return root, alice, bob, at_alice, at_bob


# --- acceptance and pinning ---

def test_signed_capsule_accepted_and_stored_signed():
    _, alice, bob, _, _ = session()
    c = inform(BOB, ALICE)
    bob.send(c)
    assert alice.recv().id == c.id
    recs = [r for r in alice.store.records() if r["capsule"]["id"] == c.id]
    assert len(recs) == 1 and recs[0]["envelope"]["pubkey_id"] == BOB


def test_pins_persist_across_restart():
    root, alice, _, _, _ = session()
    assert alice.last_pin == "new"
    alice2_node = make_node(ALICE, root)
    assert alice2_node.pins == alice.node.pins
    at_alice, at_bob = pipes()
    alice2, bob2 = alice2_node.session(at_alice), make_node(BOB, root).session(at_bob)
    bob2.send(bob2.hello(ALICE))
    alice2.recv()
    assert alice2.last_pin == "known"


# --- rejections before trust ---

def test_capsule_before_hello_rejected():
    root = Path(tempfile.mkdtemp())
    at_alice, at_bob = pipes()
    alice, bob = make_node(ALICE, root).session(at_alice), make_node(BOB, root).session(at_bob)
    bob.send(inform(BOB, ALICE))
    rejects(alice.recv, "expected a hello")


def test_capsule_before_hello_rejected_even_if_pinned():
    root, _, _, _, _ = session()
    at_alice, at_bob = pipes()
    alice2, bob2 = make_node(ALICE, root).session(at_alice), make_node(BOB, root).session(at_bob)
    bob2.send(inform(BOB, ALICE))
    rejects(alice2.recv, "expected a hello")


def test_hello_with_bad_signature_does_not_pin():
    root = Path(tempfile.mkdtemp())
    alice_node = make_node(ALICE, root)
    alice = alice_node.session(Pipe())
    bob_node = make_node(BOB, root)
    hello = to_wire(bob_node.hello(ALICE))
    other_key = Ed25519PrivateKey.generate()      # signs, but isn't the offered key
    alice.transport.inject(sign(hello, other_key, pubkey_id=BOB).to_wire())
    rejects(alice.recv, "bad signature")
    assert BOB not in alice_node.pins


def test_hello_for_someone_elses_key_cannot_poison_pin():
    """Offering a key you don't hold must not pin it."""
    root = Path(tempfile.mkdtemp())
    alice_node = make_node(ALICE, root)
    alice = alice_node.session(Pipe())
    victim_pub = public_b64(Ed25519PrivateKey.generate().public_key())
    mallory_key = Ed25519PrivateKey.generate()
    hello = to_wire(make_node(BOB, root).hello(ALICE))
    for cl in hello["semantics"]["claims"]:
        if cl["statement"] == KEY_STATEMENT:
            cl["evidence"] = [KEY_PREFIX + victim_pub]
    alice.transport.inject(sign(hello, mallory_key, pubkey_id=BOB).to_wire())
    rejects(alice.recv, "bad signature")
    assert BOB not in alice_node.pins


# --- rejections after trust ---

def test_tampered_capsule_rejected():
    _, alice, bob, at_alice, _ = session()
    bob.send(inform(BOB, ALICE, "original"))
    at_alice.inbox[-1]["capsule"]["semantics"]["claims"][0]["statement"] = "tampered"
    rejects(alice.recv, "bad signature")


def test_impostor_with_new_key_rejected():
    root, _, _, _, _ = session()
    at_alice, at_bob = pipes()
    alice2 = make_node(ALICE, root).session(at_alice)
    fake_bob = make_node(BOB, root, key_dir="mallory_keys").session(at_bob)
    fake_bob.send(fake_bob.hello(ALICE))
    rejects(alice2.recv, "key for agent://bob changed")


def test_label_mismatch_rejected():
    _, alice, bob, at_alice, _ = session()
    wire = sign(to_wire(inform(MALLORY, ALICE)), bob.node.key, pubkey_id=BOB).to_wire()
    at_alice.inject(wire)
    rejects(alice.recv, "signed as agent://bob but capsule is from agent://mallory")


def test_misaddressed_capsule_rejected():
    _, alice, bob, at_alice, _ = session()
    wire = sign(to_wire(inform(BOB, MALLORY)), bob.node.key, pubkey_id=BOB).to_wire()
    at_alice.inject(wire)
    rejects(alice.recv, "addressed to agent://mallory")


def test_session_bound_to_one_remote():
    root, alice, _, at_alice, _ = session()
    carol_node = make_node(CAROL, root)
    alice.node._save_pin(CAROL, public_b64(carol_node.key.public_key()))
    wire = sign(to_wire(inform(CAROL, ALICE)), carol_node.key, pubkey_id=CAROL).to_wire()
    at_alice.inject(wire)
    rejects(alice.recv, "session is with agent://bob, not agent://carol")


def test_cannot_send_as_someone_else_or_to_someone_else():
    _, _, bob, _, _ = session()
    for bad, needle in ((inform(ALICE, BOB), "cannot send as"), (inform(BOB, CAROL), "session is with")):
        try:
            bob.send(bad)
            raise AssertionError("expected ValueError")
        except ValueError as e:
            assert needle in str(e)


# --- replay ---

def test_replay_in_same_session_rejected():
    _, alice, bob, at_alice, _ = session()
    wire = bob.send(inform(BOB, ALICE))
    alice.recv()
    at_alice.inject(wire)
    rejects(alice.recv, "replay")


def test_replayed_hello_rejected():
    _, alice, bob, at_alice, _ = session()
    hello_wire = next(r for r in bob.store.records()
                      if r["capsule"]["semantics"]["topic"] == "__handshake__"
                      and r["capsule"]["from"] == BOB)
    at_alice.inject({"capsule": hello_wire["capsule"], **hello_wire["envelope"]})
    rejects(alice.recv, "replay")


def test_replay_after_restart_rejected():
    root, alice, bob, _, _ = session()
    wire = bob.send(inform(BOB, ALICE))
    alice.recv()
    at_alice, at_bob = pipes()
    alice2 = make_node(ALICE, root).session(at_alice)      # fresh process, same ledger
    bob2 = make_node(BOB, root).session(at_bob)
    bob2.send(bob2.hello(ALICE))
    alice2.recv()
    at_alice.inject(wire)
    rejects(alice2.recv, "replay")


def test_expired_capsule_rejected():
    _, alice, bob, _, _ = session()
    old = datetime.now(timezone.utc) - timedelta(minutes=10)
    bob.send(inform(BOB, ALICE, created=old, ttl=60))
    rejects(alice.recv, "expired")


# --- concurrency ---

def test_concurrent_sessions_share_one_node():
    root = Path(tempfile.mkdtemp())
    alice_node = make_node(ALICE, root)
    names = [f"agent://n{i}" for i in range(8)]
    errors: list[str] = []

    def run(name):
        try:
            at_alice, at_client = pipes()
            alice = alice_node.session(at_alice)
            client = make_node(name, root).session(at_client)
            client.send(client.hello(ALICE))
            alice.recv()
            for i in range(20):
                client.send(inform(name, ALICE, f"{name} #{i}"))
                assert alice.recv().sender == name
        except Exception as e:                      # surfaced below
            errors.append(f"{name}: {type(e).__name__}: {e}")

    threads = [threading.Thread(target=run, args=(n,)) for n in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert set(alice_node.pins) == set(names)
    reopened = Store(str(alice_node.store.path))
    assert len(reopened) == len(names) * 21
    for rec in reopened.records():
        assert reopened.get(rec["digest"]) == rec["capsule"]


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"{len(tests)} passed")
