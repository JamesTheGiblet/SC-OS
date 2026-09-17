"""
Multi-peer server, listening side.

    python run_alice.py [--host 127.0.0.1] [--port 7707] [--sessions N]

Alice accepts any number of peers at once, one thread per connection. Each
session must open with a hello; Alice pins or checks the key, answers with
her own hello, then dispatches every capsule through one shared scheduler.
Replies are routed by receiver to that agent's open session.

Rules: at start Alice issues the rules in --rules (default rules/builtin.json)
as her own signed rule capsules, then runs maintenance: rules whose trust has
faded are forgotten, live ones are re-issued before they expire. Maintenance
repeats hourly. Rules fire on incoming capsules; outcomes reported in
task_result capsules teach the rule that asked for the task.

--sessions N exits after N sessions have ended (0 = run until Ctrl+C).

Two machines: run with --host 0.0.0.0 so other machines can connect; Alice
prints the addresses to give them. Allow the port through the firewall.
Keys, pins, the ledger and the default rule file are found next to this
script, whatever directory it's started from.
"""

import argparse
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from agents.echo import EchoAgent
from boot.genesis import genesis
from hal.transport import SocketListener, local_addresses
from handshake import clock_offset, negotiate
from interpreter import render
from peer import Node, Peer, PeerRejected
from rules.__main__ import load_rule_file
from rules.engine import RuleEngine
from scheduler import Scheduler
from sensing import KEY_PREFIX as SENSOR_KEY, SensorObserver
from store import Store
from validator import CapsuleRejected

ME = "agent://alice"
ROOT = Path(__file__).resolve().parent
CLOCK_WARN_SECONDS = 5
MAINTAIN_EVERY_SECONDS = 3600
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
        origin = " (rule output)" if reply.provenance.method == "rule" else ""
        log(f"  sent signed {reply.intent.value.upper()} -> {reply.receiver}{origin}")

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
            offset = clock_offset(hello)
            log(f"[{remote}] hello from {tag}, {pin}; agreed capsule_version="
                f"{agreed['capsule_version']} vocab={agreed['vocab_version']}; clock offset {offset:+.1f} s")
            if abs(offset) > CLOCK_WARN_SECONDS:
                log(f"[{remote}] WARNING clock is {abs(offset):.1f} s {'ahead of' if offset > 0 else 'behind'} "
                    f"ours; capsules more than 30 s in the future are rejected")
            peer.send(local_hello)

            while True:
                c = peer.recv()
                age = (datetime.now(timezone.utc) - c.created).total_seconds()
                log(f"[{remote}] recv verified ({age * 1000:.1f} ms since created, by its clock): "
                    f"{render(c).splitlines()[0]}")
                with self.node.lock:          # one shared scheduler, one ledger
                    learned_before = len(self.sched.learned)
                    replies = self.sched.dispatch(c)
                    learned = self.sched.learned[learned_before:]
                for line in learned:
                    log(f"[{remote}] learned: {line}")
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
    ap.add_argument("--host", default="127.0.0.1",
                    help="address to listen on; 0.0.0.0 for other machines")
    ap.add_argument("--port", type=int, default=7707)
    ap.add_argument("--sessions", type=int, default=0,
                    help="exit after this many sessions end (0 = run until Ctrl+C)")
    ap.add_argument("--rules", default=str(ROOT / "rules" / "builtin.json"),
                    help="rule file to issue at start")
    args = ap.parse_args()
    socket.setdefaulttimeout(30)
    started = datetime.now(timezone.utc).isoformat()

    log(f"genesis: {render(genesis('alice')).splitlines()[0]}")
    store = Store(str(ROOT / "store" / "alice.db"))
    node = Node(ME, store, key_dir=str(ROOT / "keys"), pins_path=str(ROOT / "store" / "alice.pins.json"))
    engine = RuleEngine(ME, store, node.key)
    for name, rule_id in load_rule_file(engine, args.rules):
        if rule_id is None:
            log(f"rule {name}: forgotten earlier, not revived (python -m rules issue --force)")
    report = engine.maintain()
    if report["forgotten"]:
        log(f"rules forgotten (trust faded): {', '.join(report['forgotten'])}")
    for r in engine.status():
        if r["status"] == "active":
            log(f"rule {r['name']}: value={r['value']:+.2f} weight={r['weight']:.2f} "
                f"n={r['evidence_count']} fires={'yes' if r['fires'] else 'no'}")
    sched = Scheduler(agents={ME: EchoAgent()}, store=store, rules=engine,
                      observers=(SensorObserver(store),))
    server = Server(node, sched)
    try:
        listener = SocketListener(args.host, args.port)
    except OSError as e:
        log(f"FAIL cannot listen on {args.host}:{args.port}: {e}")
        return 1
    log(f"listening on {args.host}:{args.port}"
        + (f", stopping after {args.sessions} sessions" if args.sessions else ""))
    if args.host in ("0.0.0.0", ""):
        addrs = local_addresses()
        log("other machines can connect to: "
            + (", ".join(f"{a}:{args.port}" for a in addrs) if addrs else "(no non-loopback address found)"))
    elif args.host.startswith("127.") or args.host == "localhost":
        log("only this machine can connect; use --host 0.0.0.0 for others")

    threads: list[threading.Thread] = []
    last_maintained = time.monotonic()
    try:
        while True:
            if time.monotonic() - last_maintained > MAINTAIN_EVERY_SECONDS:
                with node.lock:
                    report = engine.maintain()
                    pruned = store.prune_expired()   # expired capsules can't be replayed, so dropping them is safe
                last_maintained = time.monotonic()
                if report["refreshed"] or report["forgotten"]:
                    log(f"rule maintenance: refreshed {report['refreshed']}, forgotten {report['forgotten']}")
                if pruned:
                    log(f"pruned {pruned} expired capsules from the ledger")
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
    log("rules now:")
    for r in engine.status():
        log(f"  {r['status']:10} {r['name']:28} value={r['value']:+.2f} weight={r['weight']:.2f} "
            f"n={r['evidence_count']} stance={r['stance']}")
    for p in engine.problems:
        log(f"  rule problem: {p}")
    sensors = store.opinions(SENSOR_KEY)
    if sensors:
        log("sensor trust now (from plausibility checks):")
        for key in sensors:
            op = sched.opinion_of(key)
            log(f"  {key:34} value={op.value:+.2f} weight={op.weight:.2f} n={op.evidence_count} stance={op.stance}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
