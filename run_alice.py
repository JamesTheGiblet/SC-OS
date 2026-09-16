"""
Multi-peer server, listening side.

    python run_alice.py [--host 127.0.0.1] [--port 7707] [--sessions N]

Alice accepts any number of peers at once, one thread per connection. Each
session must open with a hello; Alice pins or checks the key, answers with
her own hello, then dispatches every capsule through one shared scheduler.
Replies are routed by receiver to that agent's open session.

--sessions N exits after N sessions have ended (0 = run until Ctrl+C).
"""

import argparse
import socket
import threading
from datetime import datetime, timezone

from agents.echo import EchoAgent
from boot.genesis import genesis
from hal.transport import SocketListener
from handshake import negotiate
from interpreter import render
from peer import Node, Peer, PeerRejected
from scheduler import Scheduler
from store import Store
from validator import CapsuleRejected

ME = "agent://alice"
_print_lock = threading.Lock()


def log(msg: str) -> None:
    with _print_lock:
        print(f"[alice] {msg}", flush=True)


class Server:
    def __init__(self, node: Node, sched: Scheduler):
        self.node = node
        self.sched = sched
        self.sessions: dict[str, Peer] = {}     # agent id -> open session
        self.sessions_lock = threading.Lock()
        self.ended = 0
        self.ended_cond = threading.Condition()

    def route(self, reply) -> None:
        with self.sessions_lock:
            target = self.sessions.get(reply.receiver)
        if target is None:
            log(f"  undeliverable {reply.intent.value.upper()} -> {reply.receiver} (no open session)")
            return
        target.send(reply)
        log(f"  sent signed {reply.intent.value.upper()} -> {reply.receiver}")

    def handle(self, transport, addr) -> None:
        peer = self.node.session(transport)
        tag = f"{addr[0]}:{addr[1]}"
        remote = None
        try:
            hello = peer.recv()
            remote = hello.sender
            with self.sessions_lock:
                if remote in self.sessions:
                    log(f"[{tag}] REJECT {remote} already has an open session")
                    remote = None
                    return
                self.sessions[remote] = peer
            pin = "first contact, key pinned" if peer.last_pin == "new" else "key matches pin"
            local_hello = self.node.hello(remote)
            agreed = negotiate(local_hello, hello)
            log(f"[{remote}] hello from {tag}, {pin}; agreed capsule_version="
                f"{agreed['capsule_version']} vocab={agreed['vocab_version']}")
            peer.send(local_hello)

            while True:
                c = peer.recv()
                age = (datetime.now(timezone.utc) - c.created).total_seconds()
                log(f"[{remote}] recv verified ({age * 1000:.1f} ms since created): "
                    f"{render(c).splitlines()[0]}")
                with self.node.lock:          # one shared scheduler, one ledger
                    replies = self.sched.dispatch(c)
                for r in replies:
                    self.route(r)
        except ConnectionError:
            log(f"[{remote or tag}] disconnected")
        except (PeerRejected, CapsuleRejected) as e:
            log(f"[{remote or tag}] REJECT {type(e).__name__}: {e}; closing session")
        except OSError as e:
            log(f"[{remote or tag}] connection error: {e}")
        finally:
            with self.sessions_lock:
                if remote is not None and self.sessions.get(remote) is peer:
                    del self.sessions[remote]
            transport.close()
            with self.ended_cond:
                self.ended += 1
                self.ended_cond.notify_all()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7707)
    ap.add_argument("--sessions", type=int, default=0,
                    help="exit after this many sessions end (0 = run until Ctrl+C)")
    args = ap.parse_args()
    socket.setdefaulttimeout(30)
    started = datetime.now(timezone.utc).isoformat()

    log(f"genesis: {render(genesis('alice')).splitlines()[0]}")
    store = Store("store/alice.db")
    node = Node(ME, store)
    server = Server(node, Scheduler(agents={ME: EchoAgent()}, store=store))
    listener = SocketListener(args.host, args.port)
    log(f"listening on {args.host}:{args.port}"
        + (f", stopping after {args.sessions} sessions" if args.sessions else ""))

    threads: list[threading.Thread] = []
    try:
        while True:
            with server.ended_cond:
                if args.sessions and server.ended >= args.sessions:
                    break
            accepted = listener.accept()
            if accepted is None:
                continue
            t = threading.Thread(target=server.handle, args=accepted, daemon=True)
            t.start()
            threads.append(t)
    except KeyboardInterrupt:
        log("stopping")
    finally:
        listener.close()
    for t in threads:
        t.join(timeout=5)

    this_run = [r for r in store.records() if r["stored_at"] >= started]
    log(f"ledger {store.path}: {len(store)} records, {len(this_run)} from this run:")
    for rec in this_run:
        c = rec["capsule"]
        signed = rec["envelope"]["pubkey_id"] if "envelope" in rec else "UNSIGNED"
        log(f"  {rec['stored_at'][11:23]}  {rec['digest'][:12]}  {c['intent']:6} "
            f"{c['from']} -> {c['to']}  topic={c['semantics']['topic']}  signed_by={signed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
