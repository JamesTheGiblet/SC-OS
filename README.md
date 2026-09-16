# SC-OS — Semantic Capsule System

Agents exchange **capsules**: typed, signed, validated, content-addressed units of meaning.
A capsule carries claims with confidence, relations between entities, what the sender
doesn't know, where the claim came from, and how urgently to act on it.
The "OS" is the kernel that routes capsules between agents; the capsule protocol is the core.

**Status: v0.1 plus multi-node sessions.** A server node handles several peers at once over TCP,
with signed capsules, pinned keys, replay protection and a SQLite ledger. Tested with three
nodes on one machine; it hasn't crossed two machines yet.
Read [the good, the bad, and the ugly](#the-good-the-bad-and-the-ugly) before building on it.

## Why capsules

A plain JSON message tells you what someone said, not how far to believe it. A capsule makes
that part of the message: each claim has a confidence, evidence and optionally an expiry, the sender lists
what it doesn't know, and provenance says what the capsule was derived from. Capsules are
content-addressed, so the same capsule has the same digest on every node. That lets any node
spot duplicates and replays, and follow `derived_from` chains across machines. Each node's
ledger stores the signature with the capsule, proving who said what without the original
traffic. Trust comes from outcomes: receiving a claim isn't evidence that it's true. An agent's
opinion moves only when acting on something works or fails, and fades back to "unknown" when
left untested (Leighton Weight). For ESP-NOW radios, a single-claim capsule shrinks to a stripped
form and upgrades back to a full capsule at the gateway.

## Quick start

Requires Python 3.10+.

```sh
pip install -r requirements.txt
python demo.py                     # single-process walkthrough, 11 steps
python -m store store/demo.db      # print the ledger the demo wrote (add --full for JSON)
```

`demo.py` deletes `store/demo.db` at startup so each run begins empty.
Digests differ between runs because every capsule gets a fresh UUID and timestamp.

### Multiple nodes: Alice, Bob, Carol

Alice first, then any number of clients, each in its own terminal:

```sh
python run_alice.py                           # serves many peers on 127.0.0.1:7707 until Ctrl+C
python run_bob.py                             # agent://bob: hello, capsule, wait for ACK
python run_bob.py --name carol --hold 2       # agent://carol, waits 2 s so sessions overlap
python run_bob.py --name carol --replay       # then resends the same signed capsule; Alice must reject it
```

All accept `--host` and `--port`. `run_alice.py --sessions N` exits after N sessions end.
Clients retry the connection for 10 seconds. A session goes:

1. Bob sends a hello carrying his public key. Alice pins it, or checks it against her pin.
2. Alice answers with her own hello and key. Bob does the same.
3. Bob sends a signed capsule. Alice verifies, validates and replay-checks it, then runs it
   through the scheduler.
4. Alice sends a signed ACK that points back to Bob's capsule. Bob verifies it.

Every connection starts with exactly one hello in each direction. That's deliberate: a peer
may have upgraded its versions since it last connected, and the hello is where its key is checked
against the pin. Several hellos in a ledger mean several sessions.

Alice runs one thread per connection, sharing one node (key, pins, ledger) and one scheduler.
Replies are routed by `to` through the table of open sessions. An agent can have one open
session at a time, and any rejection closes that session only. Logged timings cover signing,
sending, verifying and dispatch, not just network time.

### Where state lives

| Path | Contents | In git |
| --- | --- | --- |
| `store/<name>.db` | SQLite ledger: every capsule sent or received, with signatures | no |
| `store/<name>.pins.json` | Agent id → pinned public key. Delete to forget a peer. | no |
| `keys/<name>.ed25519` | The node's private key | no |

Ledgers and pins persist, so later runs add to them and peers must present the same keys.
The run log says `first contact, key pinned` or `key matches pin`. Ledgers from before the
SQLite switch import with `python -m store import store/<name>.log store/<name>.db`, keeping
stored times and signatures, and with them replay protection.

Query a ledger directly:

```sh
sqlite3 store/alice.db "SELECT stored_at, sender, receiver, topic, intent, pubkey_id FROM capsules ORDER BY seq"
sqlite3 store/alice.db "SELECT json_extract(capsule, '$.semantics.claims[0].statement') FROM capsules WHERE topic = 'supply_chain_risk'"
```

### Verified with real processes

- **Restarts:** same keys are accepted. A Bob with a new key is rejected
  (`key for agent://bob changed since first contact`). Deleted pins lead to a fresh first contact.
- **Three nodes:** Bob and Carol's sessions overlapped, and each got the ACK for its own capsule.
  Carol was pinned on first contact while Bob matched his pin.
- **Replay:** Carol resent a byte-identical signed capsule, and Alice rejected it
  (`replay: capsule … was already received`) and closed that session. A capsule Bob sent before
  the SQLite migration was also rejected when replayed against the migrated ledger.

### Tests

```sh
python tests/test_store.py        # 14: round-trip, signatures, pruning and disk space, import, concurrent writers
python tests/test_transport.py    # 9:  framing, many peers at once, concurrent sends (real localhost TCP)
python tests/test_peer.py         # 17: pinning, rejections, replay, session binding, concurrent sessions
```

Weight model walkthrough (prints trajectories; the sharing-rule section asserts):

```sh
PYTHONPATH=. python tests/test_weight.py            # bash
$env:PYTHONPATH="."; python tests\test_weight.py    # PowerShell
```

## What a capsule looks like

Wire form (as produced by `interpreter.to_wire`, validated by `sc.schema.json`):

```json
{
  "capsule_version": "1.0",
  "id": "urn:uuid:…",
  "created": "2026-09-16T21:11:03+00:00",
  "from": "agent://alice",
  "to": "agent://bob",
  "intent": "inform",
  "trigger": "threshold",
  "semantics": {
    "topic": "supply_chain_risk",
    "claims": [{ "statement": "Supplier X has 40% capacity reduction",
                 "type": "observation", "confidence": 0.82,
                 "evidence": ["source:reuters-2026-09-14"], "valid_until": "…" }],
    "relations": [{ "subject": "SupplierX", "predicate": "affects", "object": "ProductY" }],
    "uncertainty": { "known_unknowns": ["recovery_timeline"], "assumptions": ["demand_stable"] }
  },
  "provenance": { "derived_from": [], "method": "observation", "signature": null },
  "action_hints": { "priority": "high", "ttl_seconds": 3600, "requires_ack": true },
  "epistemic": { "value": 1.0, "weight": 0.0, "evidence_count": 0, "last_tested": null }
}
```

On the wire it travels inside an envelope: `{capsule, sig, alg: "ed25519", pubkey_id}`.

Vocabulary (`vocab.json`, `sc.schema.json`):

- **Intents:** inform, request, query, confirm, refuse, ack
- **Triggers:** none, task, task_result, threshold, stuck, heartbeat, announce
- **Claim types:** observation, inference, assumption, directive
- **Predicates:** affects, causes, depends_on, contradicts, supports
- **Provenance methods:** synthesis, merge, relay, observation, reply

## How it fits together

```text
sender                                   receiver
──────                                   ────────
Capsule ─to_wire─► dict ─sign─► envelope ──TCP──► Peer.recv
         (Peer.send also stores it signed)          │  signer label = from?  addressed to me?
                                                    │  session bound to this peer?  hello first?
                                                    │  key pinned, or first-contact pin after verify
                                                    │  verify signature
                                                    │  ingest: schema, versions, clock, vocab, coherence
                                                    │  expired?  already in ledger (replay)?
                                                    ▼
                                            store signed in ledger
                                                    │
                                            Scheduler.dispatch ─► route by trigger / agent
                                                    │
                                            replies ─► Peer.send to that agent's open session
```

| File | Role |
| --- | --- |
| `primitive.py` | Frozen dataclasses: `Capsule`, `Claim`, `Relation`, `Provenance`, enums |
| `validator.py` | JSON Schema + version, clock skew (30s future, 7d past), vocab, coherence, staleness checks |
| `interpreter.py` | `to_wire` / `from_wire`, `ingest`, `merge`, `actionable`, `is_expired`, `render` |
| `envelope.py` | Canonical JSON, SHA-256 `digest`, Ed25519 `sign` / `verify` |
| `store.py` | SQLite ledger keyed by digest, indexed sender/receiver/topic/expiry; `python -m store <db>` dumps it, `python -m store import <jsonl> <db>` migrates old ledgers |
| `peer.py` | `Node` (key, pins, ledger, lock) and `Peer` (one session): trust-on-first-use pins, verify, validate, replay check, store signed |
| `scheduler.py` | Kernel: stores in and out, routes by trigger, `record_outcome` feeds opinions |
| `weight.py` | Leighton Weight: exponential decay, `Opinion` (value + weight), `blend` |
| `handshake.py` | Hello capsule, version and predicate negotiation |
| `run_alice.py`, `run_bob.py` | Multi-peer server; client that runs as any `--name` |
| `demo.py` | Single-process walkthrough of the whole pipeline |
| `boot/genesis.py` | A node's first capsule |
| `edge/upgrade.py`, `edge/sc_edge.json` | Stripped ESP-NOW wire form (≤16/32/120-char fields) and conversion |
| `agents/` | `EchoAgent`, `RelayAgent` |
| `hal/` | `FileTransport`, `SocketTransport`, `SocketListener` (many peers), clock, storage re-export |
| `leighton_weight_readme.py` | The decay theory, draft |
| `NOTES.md` | Design decisions, open questions, what's next |

### Leighton Weight in one paragraph

`W(t) = W0 · e^(−k·t)`, with `k = k0 / (1 + evidence) / stakes`. Each agent keeps a
private `Opinion` per topic: `value` on [−2, +2] where **+1 means unknown**, and `weight`
(conviction) ≥ 0. A success adds +0.1 value and +1 weight; a failure −0.2 and +3.
Over idle time weight decays and value slides back toward +1 at the same rate.
**Receiving a capsule is not evidence.** Only `Scheduler.record_outcome()` moves an opinion.

**Opinions travel as hints; belief is earned locally.** `blend(own, peer, trust=0.1)` gives
a decision-time view: the peer's weight counts at `trust` of its face value, and the view keeps
your own evidence count and decay clock. Never store it as your opinion.

## The good, the bad, and the ugly

### The good — works, and tests or real runs show it

- **Trust boundary.** Ed25519 over canonical JSON (sorted keys, no whitespace).
  A signature verifies with the right key and fails with the wrong one.
- **Exact, indexed store.** One SQLite file per node. `store.get(digest)` returns exactly what
  was hashed, by index: 0.06 ms at 20,000 capsules, where the old JSON Lines file took 48 ms
  to scan. Duplicate appends are no-ops. Capsules stay plain JSON, so `sqlite3` and
  `json_extract` can query them. Several processes can safely open one store.
- **Two-way, signed ledger.** Capsules sent and received are both stored with their signatures,
  so `store.envelope_of(digest)` verifies from the ledger alone. Scheduler replies carry
  `derived_from` pointing at their parent.
- **Real validation.** Schema, version, clock skew, unknown predicates, incoherent
  intents (an `inform` with no claims), expired claims, merges with fewer than 2 parents.
  Edge messages are checked against `sc_edge.json` on upgrade and downgrade.
- **Several nodes talk at once.** Alice serves concurrent sessions over TCP. Each peer pins
  keys, exchanges a signed capsule and a signed ACK, and gets its own reply. Both ends compute
  identical digests for the same capsules.
- **Rejections are tested.** `Peer.recv` rejects, and `tests/test_peer.py` proves:
  - capsules before a hello, even from a pinned agent
  - a hello whose signature doesn't match its offered key (it never gets pinned)
  - tampered content
  - a new key for a pinned agent
  - a signer label that differs from `from`
  - capsules addressed to someone else
  - another agent's capsule on a session bound to a different peer
  - replays, including a replayed hello and a replay after a restart
  - expired capsules
- **Thread-safe core.** Store, pins and socket sends are locked. Tests hammer a shared store,
  two connections to one database, and a shared node from 8 threads, and check every record.
- **Merge.** Same-statement claims keep the higher confidence. Relations and unknowns
  combine. Trigger survives.
- **Honest epistemics.** Receipt leaves an opinion at unknown; an observed outcome moves it;
  idle time returns it to unknown.
- **Edge round-trip.** A stripped ESP32 message upgrades to a full capsule and
  downgrades back to the identical stripped form.
- **Small dependency surface.** `jsonschema` and `cryptography`, plus Python's own `sqlite3`.

### The bad — known limits, by design for now

- **One machine, star topology.** Everything so far ran on localhost. Clients talk only to Alice.
  There's no relaying between Bob and Carol, and a reply for an agent with no open session is
  stored but not delivered (no queue). Clients don't reconnect mid-session.
- **One lock for dispatch.** Alice runs the scheduler under a single node lock, so capsules are
  processed one at a time. One thread per connection is fine for a handful of peers, not hundreds.
- **Trust on first use is only as good as first contact.** Whoever says hello first as
  `agent://bob` gets pinned as Bob. There's no registry or root of trust, and no way to rotate a key.
  Genesis doesn't create or announce a key; `peer.py` does.
- **Replay protection depends on the ledger.** A capsule is a replay if its digest is already
  stored. Pruning only drops capsules past their TTL, and expired capsules are rejected anyway, so
  the window stays closed while the ledger is intact. Delete or edit a ledger and replays inside
  their TTL get through.
- **Storage grows until pruned.** SQLite makes lookups fast, not files small: at 20,000 capsules
  the database is about 25 MB, roughly 1.3× the old JSON Lines file, because of its indexes.
  `Store.prune_expired()` deletes expired rows and gives the space back, but nothing calls it on
  a schedule yet. Pins are still a JSON file per node, which two processes must not share.
- **Signing lives outside the kernel.** `Peer` signs and stores signed records. `demo.py` and
  `Scheduler` used alone still store unsigned replies.
- **Outcomes have nowhere to come from.** `record_outcome` is only called by the demo.
  Capsules have no field for task success or failure, so opinions never move from real traffic.
- **Hints aren't wired in.** Nothing fills a capsule's `epistemic` block from the sender's
  opinion, and the scheduler never calls `blend`.
- **Stubs.** `_escalate`, `_handle_task_result` and `_handle_threshold` all just ACK.
  `boot/discovery.py` (reads `peers.json`), `RelayAgent` and `hal/clock.py` are unused.
- **Tests cover the edges, not the kernel.** Store, transport and peer tests assert.
  `tests/test_weight.py` mostly prints; only its sharing-rule section asserts. Validator,
  interpreter, merge and scheduler have no asserting tests; their check is the demo trace.
- **Tunables with no definition yet.** `stakes_factor` means nothing concrete. The stance bands are
  lopsided: from unknown, 2 successes reach `leaning_trusted` but 1 failure reaches `unclear`.

### The ugly — will bite you without warning

- **`SocketTransport.recv` on its own trusts the sender's label.** It returns the envelope's
  self-declared `pubkey_id`. `Peer.recv` does the checking; call the transport directly
  and you get none of it.
- **Some provenance is missing.** Agent replies (`EchoAgent`) and `from_edge_wire` don't set
  `derived_from`. `Provenance.signature` is always `null` and unrelated to the envelope signature.
- **Merged sender is a string join.** `merge` produces `agent://alice+agent://bob`,
  which isn't an addressable agent.

## Roadmap

1. ~~Fix `SocketTransport` framing.~~ Done.
2. ~~Two-process test.~~ ~~Three nodes.~~ Done on one machine. **Next: two machines.**
3. Identity binding: ~~trust on first use~~ done; key registry or root of trust, key rotation.
4. ~~Replay protection.~~ Done.
5. ~~SQLite ledger.~~ Done. Next: prune on a schedule.
6. Outcome field on `task_result` capsules, wired into `record_outcome`.
7. Relaying between peers and a queue for undelivered replies.
8. Asserting tests for validator, interpreter, merge and scheduler.

See [CHANGELOG.md](CHANGELOG.md) for history and [NOTES.md](NOTES.md) for design decisions and open questions.
