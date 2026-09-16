"""
Two-process test, connecting side. Start run_alice.py first.

    python run_bob.py [--host 127.0.0.1] [--port 7707]

Bob connects, sends a hello with his key, pins Alice's key from her hello,
sends a signed capsule, and waits for Alice's signed ACK referencing it.
"""

import argparse
import socket
import time
import uuid
from datetime import datetime, timedelta, timezone

from boot.genesis import genesis
from hal.transport import SocketTransport
from handshake import TOPIC as HELLO_TOPIC, negotiate
from interpreter import render
from peer import Peer, PeerRejected
from primitive import (
    ActionHints, Capsule, Claim, ClaimType, Intent, Provenance, Relation,
    Semantics, Trigger, Uncertainty,
)
from store import Store
from validator import CapsuleRejected

ME = "agent://bob"
ALICE = "agent://alice"


def log(msg: str) -> None:
    print(f"[bob]   {msg}", flush=True)


def connect(host: str, port: int, wait_s: float = 10.0) -> SocketTransport:
    deadline = time.monotonic() + wait_s
    while True:
        try:
            return SocketTransport("connect", host, port)
        except (ConnectionRefusedError, OSError):
            if time.monotonic() > deadline:
                raise
            time.sleep(0.2)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7707)
    args = ap.parse_args()
    socket.setdefaulttimeout(30)

    log(f"genesis: {render(genesis('bob')).splitlines()[0]}")
    store = Store("store/bob.log")
    transport = connect(args.host, args.port)
    peer = Peer(ME, transport, store)
    log(f"connected to {args.host}:{args.port}")

    try:
        local_hello = peer.hello(ALICE)
        peer.send(local_hello)
        remote_hello = peer.recv()
        if remote_hello.semantics.topic != HELLO_TOPIC or remote_hello.sender != ALICE:
            log(f"REJECT expected hello from {ALICE}")
            return 1
        agreed = negotiate(local_hello, remote_hello)
        log(f"hello from {ALICE}, key pinned; agreed capsule_version={agreed['capsule_version']}")

        now = datetime.now(timezone.utc)
        msg = Capsule(
            id=f"urn:uuid:{uuid.uuid4()}",
            created=now,
            sender=ME,
            receiver=ALICE,
            intent=Intent.INFORM,
            trigger=Trigger.THRESHOLD,
            semantics=Semantics(
                topic="supply_chain_risk",
                claims=(
                    Claim(
                        statement="Supplier X has 40% capacity reduction",
                        type=ClaimType.OBSERVATION,
                        confidence=0.82,
                        evidence=("source:reuters-2026-09-14",),
                        valid_until=now + timedelta(days=14),
                    ),
                ),
                relations=(Relation("SupplierX", "affects", "ProductY"),),
                uncertainty=Uncertainty(known_unknowns=("recovery_timeline",)),
            ),
            provenance=Provenance(method="observation"),
            action_hints=ActionHints(priority="high", ttl_seconds=3600, requires_ack=True),
        )
        peer.send(msg)
        log(f"sent signed {msg.intent.value.upper()} topic={msg.semantics.topic} id={msg.id[-12:]}")

        reply = peer.recv()
        rtt = (datetime.now(timezone.utc) - now).total_seconds()
        log(f"recv verified ({rtt * 1000:.1f} ms round trip):")
        for line in render(reply).splitlines():
            log(f"  {line}")
        if msg.id not in reply.provenance.derived_from:
            log(f"FAIL reply does not reference {msg.id}")
            return 1
        log("reply references our capsule: OK")
    except (PeerRejected, CapsuleRejected) as e:
        log(f"REJECT {type(e).__name__}: {e}")
        return 1
    finally:
        transport.close()

    log(f"ledger {store.path} ({len(store)} records):")
    for rec in store.records():
        c = rec["capsule"]
        signed = rec["envelope"]["pubkey_id"] if "envelope" in rec else "UNSIGNED"
        log(f"  {rec['digest'][:12]}  {c['intent']:6} {c['from']} -> {c['to']}  "
            f"topic={c['semantics']['topic']}  signed_by={signed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
