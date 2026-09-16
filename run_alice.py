"""
Two-process test, listening side.

    python run_alice.py [--host 127.0.0.1] [--port 7707]

Alice listens, pins Bob's key from his hello, answers with her own hello,
then dispatches every capsule through the scheduler and sends back signed
replies until Bob disconnects.
"""

import argparse
import socket
from datetime import datetime, timezone

from agents.echo import EchoAgent
from boot.genesis import genesis
from hal.transport import SocketTransport
from handshake import TOPIC as HELLO_TOPIC, negotiate
from interpreter import render
from peer import Peer, PeerRejected
from scheduler import Scheduler
from store import Store
from validator import CapsuleRejected

ME = "agent://alice"


def log(msg: str) -> None:
    print(f"[alice] {msg}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7707)
    args = ap.parse_args()
    socket.setdefaulttimeout(30)

    log(f"genesis: {render(genesis('alice')).splitlines()[0]}")
    store = Store("store/alice.log")
    transport = SocketTransport("listen", args.host, args.port)
    peer = Peer(ME, transport, store)
    sched = Scheduler(agents={ME: EchoAgent()}, store=store)
    log(f"listening on {args.host}:{args.port}")

    try:
        remote_hello = peer.recv()
        if remote_hello.semantics.topic != HELLO_TOPIC:
            log(f"REJECT first capsule must be a hello, got {remote_hello.semantics.topic}")
            return 1
        remote = remote_hello.sender
        local_hello = peer.hello(remote)
        agreed = negotiate(local_hello, remote_hello)
        log(f"hello from {remote}, key pinned; agreed capsule_version={agreed['capsule_version']} "
            f"vocab={agreed['vocab_version']} predicates={len(agreed['predicates'])}")
        peer.send(local_hello)

        while True:
            try:
                c = peer.recv()
            except ConnectionError:
                log("peer disconnected")
                break
            skew = (datetime.now(timezone.utc) - c.created).total_seconds()
            log(f"recv verified ({skew * 1000:.1f} ms after created):")
            for line in render(c).splitlines():
                log(f"  {line}")
            for r in sched.dispatch(c):
                peer.send(r)
                log(f"sent signed {r.intent.value.upper()} -> {r.receiver}")
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
