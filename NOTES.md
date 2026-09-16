# SC-OS v0.1 — state of the system

Reference run: `python demo.py`, then `python -m store store/demo.log`.

## What works (proven by the demo.py trace)
- genesis, handshake, sign, verify (and rejects a wrong key), ingest, merge, dispatch
- store is content-addressed and exact: `store.get(digest)` returns what was hashed,
  duplicate appends are no-ops, index survives reopening
- store is a two-way ledger: incoming capsules and outgoing replies
- replies carry `provenance.derived_from = (parent id,)`, method `reply`
- merge keeps the higher-confidence claim and carries trigger (first non-`none`)
- epistemic: receipt is not evidence; only `Scheduler.record_outcome()` moves an opinion
- Leighton Weight: weight decays, value drifts back toward unknown (+1)
- edge format round-trips (stripped ESP32 wire -> full capsule -> identical wire)

## Deliberately deferred
- peer identity binding (pubkey -> agent id); see socket gaps below
- socket transport in real use (next phase)
- reconnect / retry
- garbage collection beyond TTL (`prune_expired`)
- LLM/human escalation (`Scheduler._escalate` is a stub)
- nothing calls `record_outcome` outside the demo; capsules have no field
  for task success/failure yet

## Open design questions
- stance bands: `unknown` covers 0.8 < value < 1.2. From +1.0 that is 2 successes
  to `leaning_trusted`, but 1 failure to `unclear`. Intended asymmetry?
- `stakes_factor`: needs a definition
- outbound replies are stored but not signed — should they be?
- merge trigger rule is "first non-none wins"; no strength ordering
- `store.get` is O(n) line scan; fine for now

## Known gaps in hal/transport.py SocketTransport (read, not yet exercised)
- identity: `recv` returns the envelope's self-declared `pubkey_id`. Nothing binds
  a key to an agent id, and nothing checks `pubkey_id` against capsule `from`.
  A stranger can sign as `agent://alice`. This is a trust-model question.
- framing: newline-delimited JSON is fine (json.dumps escapes `\n` in strings),
  but `recv` keeps no buffer between calls — two messages in one TCP read fail
  to parse, and bytes past the first `\n` are dropped.
- one connection only: listener accepts once; `peer` argument is ignored;
  `ConnectionError` on close with no reconnect.
- ordering: in-order per connection; only reconnects could reorder.
- clock: validator rejects `created` > 30s in the future; past skew is covered
  (7-day limit, `Opinion.tick` ignores negative dt).

## Next phase
Two-process socket test (`run_alice.py`, `run_bob.py`). Same kernel, different
transport. Change one thing at a time. Expected to force: identity binding,
framing buffer, reconnect.

## Housekeeping
- `tests/test_weight.py` needs the project root on `PYTHONPATH`.
