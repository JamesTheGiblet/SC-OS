# Changelog

Each release lists what changed, then an honest verdict: the good, the bad, and the ugly.

## [Unreleased]

### Added

- `README.md` and this changelog.
- `tests/test_transport.py`: five asserting tests over real localhost TCP.
- `SocketTransport.close()`.

### Fixed

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
