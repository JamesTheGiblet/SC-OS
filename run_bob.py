"""
Client side. Start run_alice.py first.

    python run_bob.py [--name bob] [--host 127.0.0.1] [--port 7707]
                      [--hold SECONDS] [--replay]

Connects as agent://<name>, sends a hello with its key, checks Alice's
hello against the pin, sends a signed capsule, and waits for Alice's signed
ACK referencing it. Run several with different --name values at once for a
multi-node test.

--hold waits between the handshake and the capsule, so sessions overlap.
--replay then resends the exact same signed capsule; Alice must reject it
and close the session.
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
from peer import Node, PeerRejected
from primitive import (
    ActionHints, Capsule, Claim, ClaimType, Intent, Provenance, Relation,
    Semantics, Trigger, Uncertainty,
)
from store import Store
from validator import CapsuleRejected

ALICE = "agent://alice"


def connect(host: str, port: int, wait_s: float = 10.0) -> SocketTransport:
    deadline = time.monotonic() + wait_s
    while True:
        try:
            return SocketTransport("connect", host, port)
        except OSError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.2)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="bob")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7707)
    ap.add_argument("--hold", type=float, default=0.0)
    ap.add_argument("--replay", action="store_true")
    args = ap.parse_args()
    socket.setdefaulttimeout(30)

    me = f"agent://{args.name}"
    prefix = f"[{args.name}]".ljust(8)

    def log(msg: str) -> None:
        print(f"{prefix}{msg}", flush=True)

    log(f"genesis: {render(genesis(args.name)).splitlines()[0]}")
    store = Store(f"store/{args.name}.db")
    transport = connect(args.host, args.port)
    peer = Node(me, store).session(transport)
    log(f"connected to {args.host}:{args.port}")

    try:
        local_hello = peer.hello(ALICE)
        peer.send(local_hello)
        remote_hello = peer.recv()
        if remote_hello.semantics.topic != HELLO_TOPIC or remote_hello.sender != ALICE:
            log(f"REJECT expected hello from {ALICE}")
            return 1
        agreed = negotiate(local_hello, remote_hello)
        pin = "first contact, key pinned" if peer.last_pin == "new" else "key matches pin"
        log(f"hello from {ALICE}, {pin}; agreed capsule_version={agreed['capsule_version']}")

        if args.hold:
            time.sleep(args.hold)

        now = datetime.now(timezone.utc)
        msg = Capsule(
            id=f"urn:uuid:{uuid.uuid4()}",
            created=now,
            sender=me,
            receiver=ALICE,
            intent=Intent.INFORM,
            trigger=Trigger.THRESHOLD,
            semantics=Semantics(
                topic="supply_chain_risk",
                claims=(
                    Claim(
                        statement=f"Supplier X has 40% capacity reduction (seen by {args.name})",
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
        wire = peer.send(msg)
        log(f"sent signed {msg.intent.value.upper()} id=…{msg.id[-12:]}")

        reply = peer.recv()
        cycle = (datetime.now(timezone.utc) - now).total_seconds()
        log(f"recv verified ({cycle * 1000:.1f} ms cycle): {render(reply).splitlines()[0]}")
        if msg.id not in reply.provenance.derived_from:
            log(f"FAIL reply references {reply.provenance.derived_from}, not our {msg.id}")
            return 1
        log("reply references our capsule: OK")

        if args.replay:
            transport.send(ALICE, wire)       # byte-identical resend, same signature
            log("replayed the same signed capsule")
            try:
                extra = peer.recv()
                log(f"FAIL replay was answered: {render(extra).splitlines()[0]}")
                return 1
            except ConnectionError:
                log("replay rejected, Alice closed the session: OK")
    except (PeerRejected, CapsuleRejected) as e:
        log(f"REJECT {type(e).__name__}: {e}")
        return 1
    except ConnectionError as e:
        log(f"FAIL {ALICE} closed the connection ({e}); check her log, she may have rejected us")
        return 1
    finally:
        transport.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
