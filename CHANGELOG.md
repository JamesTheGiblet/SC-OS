# Changelog

Each release lists what changed, then an honest verdict: the good, the bad, and the ugly.

## [Unreleased]

Since v0.1: several nodes over TCP, identity, replay protection, a SQLite ledger, and fixes
for bugs that only showed up across a process boundary.

### Added

- **Multi-node sessions.** `run_alice.py` serves concurrent peers over TCP, one thread per
  connection, sharing one node and one scheduler, and routes replies by `to` to that agent's
  open session. `--sessions N` exits after N sessions. `run_bob.py` connects as any
  `--name`, with `--hold` to overlap sessions and `--replay` to test replay rejection.
- **`hal.transport.SocketListener`** accepts many connections, each its own `SocketTransport`.
  `SocketTransport.close()` added; `send` is locked so concurrent replies can't interleave.
- **`peer.py`: signed sessions.** `Node` holds one agent's key, pins, ledger and lock; `Peer` is
  one session over one transport (`Node(...).session(transport)`).
  - **Trust on first use.** A hello carries the sender's Ed25519 key and is signed by it. The first
    hello that verifies and validates pins agent id → key in `store/<name>.pins.json`.
    Keys persist in `keys/<name>.ed25519`.
  - **Session binding.** A session must open with a hello, even from a pinned agent, then
    carries only that agent's capsules. `Peer.send` refuses to send as another agent or to
    another receiver.
  - **Replay protection.** A verified capsule whose digest is already in the ledger is rejected,
    across restarts too. Expired capsules are rejected.
  - `Peer.recv` also rejects: a changed key for a pinned agent, a bad signature, a signer label
    that isn't the capsule's `from`, a capsule addressed to someone else, and a capsule that
    fails validation.
  - `Peer.last_pin` is `"new"` or `"known"`; run logs say `first contact, key pinned` or
    `key matches pin`.
- **Signatures in the ledger.** `Store.append(capsule, envelope=wire)` keeps the signature;
  `Store.get_record(digest)` returns the full record and `Store.envelope_of(digest)` an envelope
  ready for `verify`. An envelope wrapping a different capsule raises `ValueError`. Adding a
  signature to a capsule stored unsigned updates it; a signed record is never replaced.
  `python -m store` shows `signed:<pubkey_id>` or `unsigned`.
- **`python -m store import <jsonl> <db>`** migrates pre-SQLite ledgers, keeping stored times and
  signatures, so migrated history still blocks replays.
- **Tests with asserts.** `tests/test_store.py` (14), `tests/test_transport.py` (9, real localhost
  TCP), `tests/test_peer.py` (17). `tests/test_weight.py` asserts the sharing rule.
- `README.md` (with a "why capsules" section), this changelog, and rewritten `NOTES.md`
  recording design decisions and open questions.

### Changed

- **Capsule storage moved to SQLite.** Same `Store` API, one database file per node
  (`store/<name>.db`, WAL mode). Each capsule is a row: JSON body, unique digest, stored time,
  signature columns, and indexed `capsule_id`, `sender`, `receiver`, `topic`, `intent`,
  `expires_at`.
  - `prune_expired` deletes rows and runs incremental vacuum, so the file shrinks.
  - Several processes can open the same store.
  - Measured at 20,000 signed capsules, JSON Lines → SQLite: lookup 48 ms → 0.06 ms,
    open 351 ms → 7 ms, append 1,646/s → 1,462/s, prune of half 941 ms → 976 ms,
    disk 19.7 MB → 25.3 MB (14.2 MB after pruning half).
  - Callers use `.db` paths; `store/*.db*`, `store/*.json` and `keys/` are git-ignored.
- **Sharing rule decided: opinions travel as hints; belief is earned locally.**
  `blend(a, b, alpha)` became `blend(own, peer, trust=0.1)`. Before, it took on the peer's
  whole weight and evidence count, so two tests of your own plus a 50-test peer came out
  `trusted` with n=52 and slow decay, and `alpha` was never used. Now the peer's weight counts
  at `trust` (0..1), and the result keeps your own `evidence_count` and `last_tested`.
  The same example gives value +1.40, `leaning_trusted`, n=2.
- Run-script timings say what they measure: Bob logs a full `cycle` (sign, send, Alice's verify,
  dispatch and sign, receive, verify); Alice logs time since the sender created the capsule.
  Neither is network latency.
- `run_bob.py` reports `FAIL agent://alice closed the connection` when dropped, instead of a
  traceback.

### Fixed

- **A forged hello could poison a pin.** `peer.py` pinned the key offered in a hello before
  checking the hello's signature. A hello for `agent://bob` offering any key, signed by anyone,
  was rejected but left that key pinned on disk, locking the real Bob out. Pins are now written
  only after the signature verifies against the offered key and the capsule validates.
  Confirmed against the previous code before fixing.
- **Every reply failed validation.** Scheduler replies set `provenance.method="reply"`, which
  `sc.schema.json` didn't allow. The single-process demo never validated a reply; the first
  two-process run rejected Alice's ACK. `reply` is now an allowed method.
- **Socket framing lost messages.** `SocketTransport.recv` kept no buffer between calls:
  two messages in one TCP read raised `JSONDecodeError: Extra data`, and bytes after the
  first newline were dropped. Leftover bytes are now kept for the next `recv`.
  Frames are capped at 1 MiB. A listener whose peer disconnects accepts the next connection.
- **Handshake vocab version.** `make_hello` sent Python's set repr (`vocab_version={'1.0'}`).
  It now sends `vocab_versions=1.0`. `negotiate` compares vocab versions, raises
  `no shared vocab version` on mismatch, and returns `vocab_version`.
- **Version choice compared strings.** `10.0` now beats `9.0`.
- **Edge downgrade emitted invalid messages.** `sc_edge.json` now allows `tr: "none"`
  (as `vocab.json` does). `from_edge_wire` and `to_edge_wire` validate against the
  schema and raise `CapsuleRejected("edge_schema", …)` on a bad message.
- **Pruning rewrote history.** The JSON Lines store reset `stored_at` on every record it kept
  after a prune. Fixed there, and SQLite pruning deletes rows without touching the rest.
- **Pruning didn't free disk space** in the first SQLite version: `PRAGMA incremental_vacuum`
  frees one page per result row and only one row was read. All rows are now read; a test checks
  the file shrinks.
- **Leighton Weight draft contradicted the code.** `leighton_weight_readme.py` said value
  does not decay. It now gives the rule `weight.py` implements, the step sizes, and the
  sharing rule, and says only observed outcomes reinforce.

### The good

- Three real processes run overlapping sessions; each peer gets its own verified ACK, and a
  byte-identical replay is rejected, including one from before the storage migration.
- The trust boundary is tested from the attacker's side: forged hellos, tampering, key changes,
  label mismatches, misaddressing, cross-session capsules, replays and expiry.
- The ledger proves who said what on its own, and lookups stay fast as it grows.

### The bad

- Only ever run on one machine, in a star; no relaying, no queue for offline agents.
- `record_outcome` still has no real caller, so opinions don't move from real traffic.
- Trust on first use trusts whoever arrives first; no registry, no key rotation.
- SQLite files are ~1.3× the old JSON Lines size, and nothing prunes on a schedule.
- Validator, interpreter, merge and scheduler still have no asserting tests.

### The ugly

- `SocketTransport.recv` used directly still trusts the sender's self-declared label;
  only `Peer.recv` checks it.
- `merge` builds an unaddressable sender (`agent://alice+agent://bob`).
- Agent replies and edge upgrades don't set `derived_from`; `Provenance.signature` is always null.

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
