# SC-OS — Semantic Capsule System

Agents exchange **capsules**: typed, signed, validated, content-addressed units of meaning.
A capsule carries claims with confidence, relations between entities, what the sender
doesn't know, where the claim came from, and how urgently to act on it.
The "OS" is the kernel that routes capsules between agents; the capsule protocol is the core.

**Status: v0.1, single process.** Everything runs in one Python process. Nothing has
crossed a real network yet. Read [the good, the bad, and the ugly](#the-good-the-bad-and-the-ugly)
before building on it.

## Quick start

Requires Python 3.10+.

```sh
pip install -r requirements.txt
python demo.py                     # end-to-end run, 11 steps
python -m store store/demo.log     # print the ledger the demo wrote (add --full for JSON)
```

`demo.py` deletes `store/demo.log` at startup so each run begins empty.
Digests differ between runs because every capsule gets a fresh UUID and timestamp.

Weight model walkthrough (prints trajectories, no asserts):

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

## The good, the bad, and the ugly

### The good — works, and the demo proves it

- **Trust boundary.** Ed25519 over canonical JSON (sorted keys, no whitespace).
  A signature verifies with the right key and fails with the wrong one.
- **Exact store.** `store.get(digest)` returns exactly what was hashed. Duplicate appends
  are no-ops. The index survives reopening the file.
- **Two-way ledger.** Incoming capsules and outgoing replies are both stored.
  Scheduler replies carry `derived_from` pointing at their parent.
- **Real validation.** Schema, version, clock skew, unknown predicates, incoherent
  intents (an `inform` with no claims), expired claims, merges with fewer than 2 parents.
- **Merge.** Same-statement claims keep the higher confidence. Relations and unknowns
  combine. Trigger survives.
- **Honest epistemics.** Receipt leaves an opinion at unknown; an observed outcome moves it;
  idle time returns it to unknown.
- **Edge round-trip.** A stripped ESP32 message upgrades to a full capsule and
  downgrades back to the identical stripped form.
- **Small dependency surface.** `jsonschema` and `cryptography`, nothing else.

### The bad — known limits, by design for now

- **Single process only.** `SocketTransport` exists but nothing uses it.
  The kernel doesn't import `hal/` at all.
- **Identity isn't bound to keys.** A signature proves *some key* signed a capsule,
  not that the key belongs to `agent://alice`. No registry, no trust-on-first-use, no root of trust.
  Genesis doesn't create or announce a key.
- **The ledger doesn't keep signatures.** The store saves the capsule, not the envelope.
  You can prove content from the store, but not who signed it.
- **Outcomes have nowhere to come from.** `record_outcome` is only called by the demo.
  Capsules have no field for task success or failure.
- **Stubs.** `_escalate`, `_handle_task_result` and `_handle_threshold` all just ACK.
  `boot/discovery.py` (reads `peers.json`), `RelayAgent` and `hal/clock.py` are unused.
- **No real tests.** `tests/test_weight.py` prints trajectories and asserts nothing.
  Correctness is currently "the demo trace looks right".
- **Scale.** `store.get` scans the file line by line. There's no file locking, so
  two processes must not share one store file.
- **Tunables with no definition yet.** `stakes_factor` means nothing concrete. The stance bands are
  lopsided: from unknown, 2 successes reach `leaning_trusted` but 1 failure reaches `unclear`.

### The ugly — will bite you without warning

- **Socket framing drops data.** `SocketTransport.recv` has no buffer between calls.
  Two messages in one TCP read fail to parse; bytes after the first newline are lost.
  The listener accepts one connection, ever, and ignores the `peer` argument.
- **`recv` trusts the sender's own label.** It returns the envelope's self-declared `pubkey_id`,
  and nothing checks that against the capsule's `from`.
- **Edge downgrade emits an invalid message.** `to_edge_wire` writes `tr: "none"` for
  untriggered capsules, but `sc_edge.json` doesn't allow `none`. Nothing validates edge
  messages, so it passes silently. It also drops the sender.
- **Pruning rewrites history.** `Store.prune_expired` resets every surviving record's
  `stored_at` to the time of the prune.
- **Some provenance is missing.** Agent replies (`EchoAgent`) and `from_edge_wire` don't set
  `derived_from`. `Provenance.signature` is always `null` and unrelated to the envelope signature.
- **Merged sender is a string join.** `merge` produces `agent://alice+agent://bob`,
  which isn't an addressable agent.
- **Theory and code disagree in one place.** `leighton_weight_readme.py` says value does not
  decay; `Opinion.tick` moves value toward +1 at the same rate weight decays. The code is the
  model; the prose needs fixing.

## Roadmap

1. Fix `SocketTransport` framing (its own commit, nothing else changed).
2. Two-process test: `run_alice.py` listens, `run_bob.py` connects.
3. Identity binding: key ↔ agent id.
4. Outcome field on `task_result` capsules, wired into `record_outcome`.
5. Real tests with asserts.

See [CHANGELOG.md](CHANGELOG.md) for history and [NOTES.md](NOTES.md) for open design questions.
