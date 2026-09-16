# Changelog

Each release lists what changed, then an honest verdict: the good, the bad, and the ugly.

## [Unreleased]

### Added

- `README.md` and this changelog.
- **Two-process session.** `run_alice.py` listens and `run_bob.py` connects over TCP.
  They exchange hellos, then a signed capsule and a signed ACK that references it.
- **`peer.py`: identity by trust on first use.** A hello carries the sender's Ed25519 public
  key and is signed by it; the receiver pins agent id → key in `store/<name>.pins.json`.
  `Peer.recv` rejects:
  - a capsule from an agent with no pinned key
  - a changed key for a pinned agent
  - a bad signature
  - an envelope signer that isn't the capsule's `from`
  - a capsule addressed to someone else
  - a capsule that fails validation

  `Peer.send` refuses to send as another agent. Both directions are stored with signatures.
  Keys persist in `keys/<name>.ed25519`. `keys/` and `store/*.json` are git-ignored.
- `tests/test_peer.py`: nine asserting tests of pinning and rejections.
- **Replay protection.** `Peer.recv` rejects a verified capsule whose digest is already in the
  ledger, which also works after a restart. It rejects expired capsules, so a replay can't
  outlive the record that catches it.
- **Multi-peer server.** `hal.transport.SocketListener` accepts many connections, each wrapped
  as its own `SocketTransport`. `run_alice.py` serves concurrent sessions, one thread each,
  sharing one node and one scheduler. Replies are routed by `to` to that agent's open session.
  `--sessions N` exits after N sessions.
- **Clients as any agent.** `run_bob.py` takes `--name` (e.g. `carol`), `--hold` to overlap
  sessions, and `--replay` to resend a signed capsule and expect rejection.
- **Session binding.** A session must open with a hello, even from an already pinned agent,
  and then carries only that agent's capsules. `Peer.send` refuses other receivers.
  An agent can hold one open session on Alice at a time.
- **Thread safety.** `Store` locks appends, reads and prunes; `records()` iterates a snapshot.
  `SocketTransport.send` is locked so concurrent replies can't interleave on the wire.
- Tests: replay (same session, replayed hello, after restart), expiry, session binding,
  pin poisoning, 8 concurrent sessions on one node, concurrent store appends, and a
  listener serving 6 peers at once with interleaving-proof sends.
- `Peer.last_pin` is `"new"` or `"known"` after a hello. The run scripts log
  `first contact, key pinned` or `key matches pin` instead of `key pinned` every time.

- `tests/test_transport.py`: five asserting tests over real localhost TCP.
- `tests/test_store.py`: exact round-trip, duplicate appends, reopen, pruning, signatures.
- **Signatures in the ledger.** `Store.append(capsule, envelope=wire)` saves `sig`, `alg` and
  `pubkey_id` with the record. `Store.get_record(digest)` returns the full record;
  `Store.envelope_of(digest)` returns an envelope ready for `open_envelope` / `verify`.
  An envelope wrapping a different capsule raises `ValueError`. A capsule stored unsigned
  and appended again with a signature gets a new line that supersedes the old one;
  a signed record is never replaced. `python -m store` shows `signed:<pubkey_id>` or `unsigned`.
  The demo stores Alice's capsule signed and verifies it from the store.

### Changed

- `peer.py` split into `Node` (one agent's key, pins, ledger and lock, shared by all its
  sessions) and `Peer` (one session). Create sessions with `Node(...).session(transport)`.
- Run-script timings are labelled for what they measure: Bob logs a full `cycle` (sign, send,
  Alice's verify, dispatch and sign, receive, verify). Alice logs time since the sender created
  the capsule. Neither is network latency.
- `run_bob.py` reports `FAIL agent://alice closed the connection` when Alice drops him
  (for example after rejecting his key), instead of a traceback.
- **Sharing rule decided: opinions travel as hints; belief is earned locally.**
  `blend(a, b, alpha)` became `blend(own, peer, trust=0.1)`. Before, it took on the peer's
  whole weight and evidence count, so two tests of your own plus a 50-test peer came out
  `trusted` with n=52 and slow decay. It also never used `alpha`. Now the peer's weight
  counts at `trust` (0..1), and the result keeps your own `evidence_count` and `last_tested`.
  The same example gives value +1.40, `leaning_trusted`, n=2. `tests/test_weight.py`
  asserts the rule. The argument order changed; the weight test was the only caller.
- `Store.records()` and `Store.all()` yield only current records: superseded and
  unreadable lines are skipped, matching the index. `--full` dumps the whole record.
- `SocketTransport.close()`.

### Fixed

- **A forged hello could poison a pin.** `peer.py` pinned the key offered in a hello before
  checking the hello's signature. A hello for `agent://bob` offering any key, signed by anyone,
  was rejected but left that key pinned on disk, locking the real Bob out. Pins are now written
  only after the signature verifies against the offered key and the capsule validates.
  Confirmed against the previous code before fixing.
- **Every reply failed validation.** Scheduler replies set `provenance.method="reply"`, which
  `sc.schema.json` didn't allow. The single-process demo never validated a reply; the first
  two-process run rejected Alice's ACK. `reply` is now an allowed method.
- **Handshake vocab version.** `make_hello` sent Python's set repr (`vocab_version={'1.0'}`).
  It now sends `vocab_versions=1.0`, matching `capsule_versions`. `negotiate` compares
  vocab versions, raises `no shared vocab version` on mismatch, and returns `vocab_version`.
- **Version choice compares numbers, not strings.** `10.0` now beats `9.0`.
- **Edge downgrade emitted invalid messages.** `sc_edge.json` now allows `tr: "none"`
  (as `vocab.json` does). `from_edge_wire` and `to_edge_wire` validate against the
  schema and raise `CapsuleRejected("edge_schema", …)` on a bad message.
- **Socket framing lost messages.** `SocketTransport.recv` kept no buffer between calls:
  two messages in one TCP read raised `JSONDecodeError: Extra data`, and bytes after the
  first newline were dropped. Leftover bytes are now kept for the next `recv`.
  Frames are capped at 1 MiB. A listener whose peer disconnects accepts the next connection.
- **Leighton Weight draft contradicted the code.** `leighton_weight_readme.py` said value
  does not decay. It now gives the rule `weight.py` implements: value's distance from +1
  shrinks by the same `exp(-k·t)` as weight. It also gives the step sizes, says only observed
  outcomes reinforce, and fixes the command for running the test.
- **Pruning rewrote history.** `Store.prune_expired` reset `stored_at` on every record it kept.
  It now rewrites surviving records unchanged.

## [v0.1] — 2026-09-16

First tagged state. Single-process kernel; the interface is frozen until the
two-process socket test passes.

### Added

- `envelope.py`: canonical JSON, SHA-256 `digest`, Ed25519 `sign`, `open_envelope`,
  `verify`, and an `Envelope` type with `to_wire()`. The module was imported by the
  store and demo but didn't exist, so Python found an unrelated `envelope` email library instead.
- `Scheduler.record_outcome(topic, success)`: the only way an opinion changes.
- `python -m store <path> [--full]` to dump a store; `Store.records()` for full records.
- `NOTES.md`, `.gitignore`, git repository.

### Changed

- `Scheduler.dispatch` stores every reply as well as the incoming capsule.
  Routing moved unchanged into `_route`.
- Scheduler replies set `provenance.derived_from` to the parent id, `method="reply"`.
- `dispatch` no longer records a success for every capsule it receives.
- `merge` carries the trigger: the first parent's, or the second's if the first is `none`.
- `demo.py` moved to the project root, starts from an empty store, and shows an
  opinion before and after a recorded outcome.

### Fixed

- **Store returned the wrong capsule.** The index stored the count of distinct digests
  where it needed line numbers. Once a duplicate was appended, the two went out of step,
  and on the next run `store.get` read an older capsule (`retrieved matches: False`).
  The index now tracks physical lines, and duplicate appends are skipped.
- `edge/upgrate.py` → `edge/upgrade.py`, `hal/storgae.py` → `hal/storage.py`
  (imports were failing).
- `demo.py` put the wrong directory on `sys.path` after the move.

### The good

- The demo runs end to end, and repeated runs no longer disagree with each other.
- The store is exact and content-addressed, so `derived_from` chains can be trusted.
- Receiving a capsule and seeing an outcome are separate events, as the theory requires.

### The bad

- Still one process. Identity isn't bound to keys. Signatures aren't stored.
- `record_outcome` has no real caller; capsules can't say whether a task succeeded.
- No asserting tests; the demo trace is the test.

### The ugly

- Found while writing these docs, **not fixed in v0.1**:
  - `SocketTransport.recv` loses or garbles messages that share a TCP read.
  - `make_hello` sends a Python set repr as the vocab version.
  - `to_edge_wire` emits `tr: "none"`, which its own schema rejects.
  - `prune_expired` overwrites `stored_at` on every record it keeps.
- `leighton_weight_readme.py` contradicts `weight.py` on whether value decays.
