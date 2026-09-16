# SC-OS — design notes

Working notes: decisions made and why, open questions, what's next.
[README.md](README.md) describes the system; [CHANGELOG.md](CHANGELOG.md) records what changed.

Last updated 2026-09-16, after self-description capsules.

## Decisions

Each decision was made against the code as it stood; revisit only with a reason.

- **Receipt is not evidence.** An opinion moves only through `Scheduler.record_outcome()`,
  never because a capsule arrived. Receiving a claim tells you someone said it, not that it holds.
- **Opinions travel as hints; belief is earned locally.** A peer's opinion may shape a
  decision through `blend(own, peer, trust)`, with the peer's weight discounted to `trust` (≤ 1).
  The result keeps your own evidence count and decay clock and is never stored as your opinion.
  Chosen because the capsule wire already carries an `epistemic` block (sharing has a slot)
  while `Scheduler.opinions` is private (belief is local).
- **Value decays with weight.** In `Opinion.tick`, value's distance from +1 (unknown) shrinks by
  the same `exp(-k·t)` as weight. The code is the model; the theory draft follows it.
- **Identity is trust on first use.** A hello carries the sender's public key and is signed by it.
  The first hello that verifies and validates pins agent id → key; later keys must match.
  Nothing is pinned before the signature checks out (a forged hello once could poison a pin).
  Chosen for small deployments with no PKI. See open questions for the limits.
- **One hello per connection, both directions.** Negotiation isn't cached across connections:
  a peer may upgrade between sessions, and the hello is where its key meets the pin.
  Costs two messages per connection, not per capsule.
- **A session is bound to one peer.** It must open with a hello, then carries only that peer's
  capsules, and `Peer.send` refuses other receivers. An agent holds one open session per server.
  Any rejection closes the session (fail closed).
- **Replay = digest already in the ledger.** The ledger persists, so this holds across restarts.
  Expired capsules are rejected, and pruning drops only expired rows, so a replay can't outlive
  the record that would catch it. No nonces or sequence numbers needed while ledgers are intact.
- **Every capsule crossing a boundary is stored, with its signature.** Incoming and outgoing,
  so the ledger alone proves who said what. Replies set `derived_from` to their parent.
- **Storage is SQLite, one file per node.** Chosen for indexed lookups (0.06 ms vs 48 ms at 20k
  capsules), in-place pruning and safe multi-process access. It does not make files smaller:
  about 1.3× JSON Lines on disk because of indexes. Size is managed by pruning. Capsule bodies stay
  plain JSON (not compressed) so `json_extract` queries work.
- **SC-OS describes itself in its own format.** `self_describe.py` turns the code, docs and
  test runs into `self.*` capsules. Generated from the source, not written by hand, so the
  description can't drift from the code for longer than one run. Content-derived ids make reruns
  idempotent, and `derived_from` links a file's versions. Seven-day TTL: unrefreshed
  self-knowledge expires rather than going stale silently.
- **Merge keeps the trigger** of the first parent, or the second's if the first is `none`.
  No strength ordering between triggers yet.
- **Version choice is numeric.** Highest shared version wins by number, not string sort.

## Open questions

- **Where do outcomes come from?** `record_outcome` has no caller outside the demo, so opinions
  never move from real traffic. Likely a success/failure field on `task_result` capsules that the
  scheduler reads. Needs a schema decision.
- **Merged sender.** `merge` builds `agent://alice+agent://bob`, which isn't addressable.
  Proposal on the table: keep `sender` as the node doing the merge, both parents in
  `derived_from`, `method="merge"`.
- **Beyond first contact.** TOFU trusts whoever says hello first. Options: pre-shared pins,
  a key registry, a root of trust the operator controls. Key rotation isn't possible yet.
- **Stance bands.** `unknown` covers 0.8 < value < 1.2. From +1, 2 successes reach
  `leaning_trusted` but 1 failure reaches `unclear`. Is that asymmetry right under realistic
  traffic? Needs a scenario, not a formula.
- **`stakes_factor`.** Slows decay for high-stakes topics but has no concrete definition.
- **Signing in the kernel.** `Peer` signs; `Scheduler` alone stores unsigned replies. Should the
  scheduler own a key?
- **Trigger strength.** Should merge prefer `stuck` over `threshold` over the rest?
- **Pruning schedule.** Who calls `prune_expired`, and how often?
- **Undelivered replies.** A reply to an agent with no open session is stored but dropped.
  Queue it until the agent reconnects?
- **Sharing the self-description.** `self.*` capsules live only in `store/self.db`. Should a node
  send them to peers after the hello, so peers can see each other's version, modules and test
  results? Would test results count as outcomes for `record_outcome`?
- **Trust per peer.** `blend` takes one `trust` value. Now that keys are pinned, trust could be
  per agent. Where would it be stored?

## Next

1. **Two machines.** Same code, `--host`. Expect clock skew, firewalls, real disconnects and
   path differences between Windows and Linux.
2. **Outcome field** on `task_result`, wired into `record_outcome`.
3. **Prune on a schedule** in `run_alice.py`.
4. **Merged sender** decision.
5. **Relaying and a reply queue**, which turn the star into a network.
6. **Asserting tests** for validator, interpreter, merge and scheduler.

## Housekeeping

- `tests/test_weight.py` needs the project root on `PYTHONPATH`; the other tests set it themselves.
- Old `store/*.log` ledgers are no longer read. Import them with
  `python -m store import <jsonl> <db>`, then delete them.
