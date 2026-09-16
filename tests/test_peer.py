"""
Peer session: TOFU key pinning and the rejections it must make.
Run from the project root: python tests/test_peer.py
"""

import copy
import sys
import tempfile
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from envelope import sign
from interpreter import to_wire
from peer import Peer, PeerRejected
from primitive import Capsule, Claim, ClaimType, Intent, Semantics
from store import Store

ALICE, BOB, MALLORY = "agent://alice", "agent://bob", "agent://mallory"


class Pipe:
    """In-memory Transport: what one end sends, the other end receives."""
    def __init__(self):
        self.inbox: deque = deque()
        self.other: "Pipe | None" = None

    def send(self, peer, envelope):
        assert self.other is not None
        self.other.inbox.append(envelope)

    def recv(self):
        if not self.inbox:
            raise ConnectionError("empty")
        env = self.inbox.popleft()
        return env.get("pubkey_id", "unknown"), env


def pipes():
    a, b = Pipe(), Pipe()
    a.other, b.other = b, a
    return a, b


def node(agent_id, transport, root: Path) -> Peer:
    return Peer(agent_id, transport, Store(str(root / f"{agent_id[8:]}.log")),
                key_dir=str(root / "keys"), pins_path=str(root / f"{agent_id[8:]}.pins.json"))


def inform(sender, receiver, text="hi") -> Capsule:
    return Capsule(
        id=f"urn:uuid:{uuid.uuid4()}",
        created=datetime.now(timezone.utc),
        sender=sender, receiver=receiver, intent=Intent.INFORM,
        semantics=Semantics(topic="t", claims=(Claim(text, ClaimType.OBSERVATION, 0.5),)),
    )


def rejects(fn, needle):
    try:
        fn()
    except PeerRejected as e:
        assert needle in str(e), f"expected '{needle}' in '{e}'"
        return
    raise AssertionError(f"expected PeerRejected containing '{needle}'")


def session():
    root = Path(tempfile.mkdtemp())
    ta, tb = pipes()
    alice, bob = node(ALICE, ta, root), node(BOB, tb, root)
    bob.send(bob.hello(ALICE))
    alice.recv()
    alice.send(alice.hello(BOB))
    bob.recv()
    return root, alice, bob, ta, tb


def test_signed_capsule_accepted_and_stored_signed():
    _, alice, bob, _, _ = session()
    c = inform(BOB, ALICE)
    bob.send(c)
    got = alice.recv()
    assert got.id == c.id
    recs = [r for r in alice.store.records() if r["capsule"]["id"] == c.id]
    assert len(recs) == 1 and recs[0]["envelope"]["pubkey_id"] == BOB


def test_capsule_before_hello_rejected():
    root = Path(tempfile.mkdtemp())
    ta, tb = pipes()
    alice, bob = node(ALICE, ta, root), node(BOB, tb, root)
    bob.send(inform(BOB, ALICE))
    rejects(alice.recv, "no pinned key")


def test_tampered_capsule_rejected():
    _, alice, bob, _, tb = session()
    bob.send(inform(BOB, ALICE, "original"))
    alice_inbox = tb.other.inbox
    alice_inbox[-1]["capsule"]["semantics"]["claims"][0]["statement"] = "tampered"
    rejects(alice.recv, "bad signature")


def test_impostor_with_new_key_rejected():
    root, alice, _, ta, _ = session()
    # same agent id, but a fresh key from a different key directory
    fake_bob = Peer(BOB, ta.other, Store(str(root / "fake.log")),
                    key_dir=str(root / "mallory_keys"), pins_path=str(root / "fake.pins.json"))
    fake_bob.send(fake_bob.hello(ALICE))
    rejects(alice.recv, "key for agent://bob changed")
    fake_bob.send(inform(BOB, ALICE))
    rejects(alice.recv, "bad signature")


def test_label_mismatch_rejected():
    root, alice, bob, _, tb = session()
    c = inform(MALLORY, ALICE)
    wire = sign(to_wire(c), bob.key, pubkey_id=BOB).to_wire()
    tb.send(ALICE, wire)
    rejects(alice.recv, "signed as agent://bob but capsule is from agent://mallory")


def test_misaddressed_capsule_rejected():
    _, alice, bob, _, tb = session()
    c = inform(BOB, MALLORY)
    wire = sign(to_wire(c), bob.key, pubkey_id=BOB).to_wire()
    tb.send(ALICE, wire)
    rejects(alice.recv, "addressed to agent://mallory")


def test_replay_of_hello_is_harmless():
    _, alice, bob, _, tb = session()
    hello = bob.hello(ALICE)
    bob.send(hello)
    tb.other.inbox.append(copy.deepcopy(tb.other.inbox[-1]))
    alice.recv()
    alice.recv()
    assert alice.pins[BOB]


def test_cannot_send_as_someone_else():
    _, _, bob, _, _ = session()
    try:
        bob.send(inform(ALICE, BOB))
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_pins_persist_across_restart():
    root, alice, bob, ta, _ = session()
    alice2 = node(ALICE, ta, root)
    assert alice2.pins == alice.pins
    bob.send(inform(BOB, ALICE))
    assert alice2.recv().sender == BOB


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"{len(tests)} passed")
