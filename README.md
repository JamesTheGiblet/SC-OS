# SC-OS — Semantic Capsule System

Agents exchange **capsules**: typed, signed, validated, content-addressed units of meaning.
A capsule carries claims with confidence, relations between entities, what the sender
doesn't know, where the claim came from, and how urgently to act on it.
The "OS" is the kernel that routes capsules between agents; the capsule protocol is the core.

**Status: v0.1 plus a working two-process session.** Alice and Bob run as separate processes
over TCP on one machine, with signed capsules and pinned keys. It hasn't crossed two machines yet.
Read [the good, the bad, and the ugly](#the-good-the-bad-and-the-ugly) before building on it.

## Quick start

Requires Python 3.10+.

```sh
pip install -r requirements.txt
python demo.py                     # end-to-end run, 11 steps
python -m store store/demo.log     # print the ledger the demo wrote (add --full for JSON)
```

`demo.py` deletes `store/demo.log` at startup so each run begins empty.
Digests differ between runs because every capsule gets a fresh UUID and timestamp.

### Two processes: Alice and Bob

Two terminals, Alice first:

```sh
python run_alice.py               # listens on 127.0.0.1:7707
python run_bob.py                 # connects, hello, sends a capsule, waits for the ACK
```

Both accept `--host` and `--port`. Bob retries the connection for 10 seconds.
A session goes:

1. Bob sends a hello carrying his public key. Alice pins it.
2. Alice answers with her own hello and key. Bob pins it.
3. Bob sends a signed capsule. Alice verifies and validates it, then runs it through the scheduler.
4. Alice sends a signed ACK that points back to Bob's capsule. Bob verifies it.

Each side keeps its ledger in `store/<name>.log`, its pins in `store/<name>.pins.json`,
and its private key in `keys/<name>.ed25519` (git-ignored). Ledgers and pins persist,
so later runs append and must present the same keys. The log says `first contact, key pinned`
or `key matches pin`. Delete `store/<name>.pins.json` to forget a peer.

Every connection starts with exactly one hello in each direction. That's deliberate: a peer
may have upgraded its versions between connections, and the hello is where its key is checked
against the pin. Several hellos in a ledger mean several sessions. Timings in the log cover
signing, sending, verifying and dispatch, not just network time.

Verified restarts: same keys are accepted, a Bob with a new key is rejected by Alice
(`key for agent://bob changed since first contact`), and deleted pins lead to a fresh first contact.

### Tests

Asserting tests (transport tests use real localhost TCP):

```sh
python tests/test_store.py
python tests/test_transport.py
python tests/test_peer.py         # key pinning and rejections
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

Vocabulary (`vocab.json`):

- **Intents:** inform, request, query, confirm, refuse, ack
- **Triggers:** none, task, task_result, threshold, stuck, heartbeat, announce
- **Claim types:** observation, inference, assumption, directive
- **Predicates:** affects, causes, depends_on, contradicts, supports

## How it fits together

```
capsule ──to_wire──► dict ──sign──► envelope {capsule, sig, alg, pubkey_id}
                                        │
                                  open_envelope + verify
                                        │
                              ingest (validate + from_wire)
                                        │
                                Scheduler.dispatch
                    stores incoming ─┐   │   ┌─ stores every reply
                                     ▼   ▼   ▼
                             route by trigger / agent
```

| File | Role |
|---|---|
| `primitive.py` | Frozen dataclasses: `Capsule`, `Claim`, `Relation`, `Provenance`, enums |
| `validator.py` | JSON Schema + version, clock skew (30s future, 7d past), vocab, coherence, staleness checks |
| `interpreter.py` | `to_wire` / `from_wire`, `ingest`, `merge`, `actionable`, `render` |
| `envelope.py` | Canonical JSON, SHA-256 `digest`, Ed25519 `sign` / `verify` |
| `store.py` | Append-only JSON Lines ledger keyed by digest; `python -m store <path>` dumps it |
| `scheduler.py` | Kernel: stores in and out, routes by trigger, `record_outcome` feeds opinions |
| `weight.py` | Leighton Weight: exponential decay, `Opinion` (value + weight), `blend` |
| `handshake.py` | Hello capsule, version and predicate negotiation |
| `peer.py` | Signed session over any transport: trust-on-first-use key pins, verify, validate, store signed |
| `run_alice.py`, `run_bob.py` | Two-process session: Alice listens, Bob connects |
| `boot/genesis.py` | A node's first capsule |
| `edge/upgrade.py`, `edge/sc_edge.json` | Stripped ESP-NOW wire form (≤16/32/120-char fields) and conversion |
| `agents/` | `EchoAgent`, `RelayAgent` |
| `hal/` | `FileTransport`, `SocketTransport`, clock, storage re-export |
| `NOTES.md` | Design state, deferred work, open questions |

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

### The good — works, and the demo proves it

- **Trust boundary.** Ed25519 over canonical JSON (sorted keys, no whitespace).
  A signature verifies with the right key and fails with the wrong one.
- **Exact store.** `store.get(digest)` returns exactly what was hashed. Duplicate appends
  are no-ops. The index survives reopening the file.
- **Two-way ledger.** Incoming capsules and outgoing replies are both stored.
  Scheduler replies carry `derived_from` pointing at their parent. Signed capsules keep
  their signature in the store, and `store.envelope_of(digest)` verifies from the ledger alone.
- **Real validation.** Schema, version, clock skew, unknown predicates, incoherent
  intents (an `inform` with no claims), expired claims, merges with fewer than 2 parents.
  Edge messages are checked against `sc_edge.json` on upgrade and downgrade.
- **Merge.** Same-statement claims keep the higher confidence. Relations and unknowns
  combine. Trigger survives.
- **Honest epistemics.** Receipt leaves an opinion at unknown; an observed outcome moves it;
  idle time returns it to unknown.
- **Edge round-trip.** A stripped ESP32 message upgrades to a full capsule and
  downgrades back to the identical stripped form.
- **Two processes talk.** `run_alice.py` and `run_bob.py` handshake over TCP, pin each other's
  keys, and exchange a signed capsule and a signed ACK. Every ledger record on both sides is
  signed, and both sides compute identical digests for the same capsules.
- **Rejections are tested.** `Peer.recv` rejects, and `tests/test_peer.py` proves:
  - capsules before a hello
  - tampered content
  - a new key for a pinned agent
  - a signer label that differs from `from`
  - capsules addressed to someone else
  - sending as another agent
- **Small dependency surface.** `jsonschema` and `cryptography`, nothing else.

### The bad — known limits, by design for now

- **One machine, one peer, one session.** The two-process run uses localhost. `run_alice.py`
  serves one peer and exits when it disconnects. The socket link is point-to-point, and `peer`
  in `send()` isn't used for routing. Bob doesn't reconnect mid-session.
- **Trust on first use is only as good as first contact.** Whoever says hello first as
  `agent://bob` gets pinned as Bob. There's no registry or root of trust, and no way to rotate a key.
  Genesis doesn't create or announce a key; `peer.py` does.
- **Replays aren't detected.** A captured signed capsule verifies again if resent. The store
  ignores the duplicate, but the scheduler would dispatch it again. Nonces, sequence numbers or
  seen-id tracking would fix it.
- **Signing lives outside the kernel.** `Peer` signs and stores signed records. `demo.py` and
  `Scheduler` used alone still store unsigned replies.
- **Outcomes have nowhere to come from.** `record_outcome` is only called by the demo.
  Capsules have no field for task success or failure.
- **Stubs.** `_escalate`, `_handle_task_result` and `_handle_threshold` all just ACK.
  `boot/discovery.py` (reads `peers.json`), `RelayAgent` and `hal/clock.py` are unused.
- **Tests cover the edges, not the kernel.** Store, transport and peer tests assert.
  `tests/test_weight.py` mostly prints; only its sharing-rule section asserts. Validator,
  interpreter, merge and scheduler have no asserting tests; their check is the demo trace.
- **Hints aren't wired in.** Nothing fills a capsule's `epistemic` block from the sender's
  opinion, and the scheduler never calls `blend`.
- **Scale.** `store.get` scans the file line by line. There's no file locking, so
  two processes must not share one store file.
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
2. ~~Two-process test.~~ Done on one machine; next, two machines.
3. Identity binding: ~~trust on first use~~ done; key registry or root of trust, key rotation.
4. Replay protection.
5. Outcome field on `task_result` capsules, wired into `record_outcome`.
6. Asserting tests for validator, interpreter, merge and scheduler.

See [CHANGELOG.md](CHANGELOG.md) for history and [NOTES.md](NOTES.md) for open design questions.
