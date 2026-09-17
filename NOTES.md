# SC-OS — design notes

Working notes: decisions made and why, open questions, what's next.
[README.md](README.md) describes the system; [CHANGELOG.md](CHANGELOG.md) records what changed.

Last updated 2026-09-17, after checking the target design against the code.

## Thesis

SC-OS is a seed, not a blueprint: a small invariant kernel plus one decay law. What it grows
into is decided by the environment it lands in and the hardware it runs on. Two identical seeds
in different places grow into different systems, and both are correct.

## Target design

Where SC-OS is heading, checked against the code on 2026-09-17. **Built** means code and tests
exist; **partial** and **not built** say what's missing. Decisions below override this section
where they disagree.

1. **Kernel.** Knows capsules in, capsules out, validate, sign, store, dispatch; no hardware,
   topology, roles or config. *Partial.* The core is `sc.schema.json`, `vocab.json`,
   `validator.py`, `envelope.py`, `primitive.py`, `interpreter.py`, `scheduler.py`, `store.py`,
   `handshake.py`, `weight.py` and `peer.py` (signing and identity), plus `hal/`. `hal/` is not
   interface-only yet: `hal/transport.py` holds the file and socket implementations. The
   scheduler doesn't sign; `Peer` does.
2. **The law.** `W(t) = W0 · e^(−k·t)` with `k = k0 / (1 + evidence) / stakes`, one curve for
   BlockForge, ChatterPet, HABITAT and SC-OS. *Built* (`weight.py`, `tests/test_weight.py`).
3. **Epistemic axis.** `value` ∈ [−2, +2], +1 = unknown; `weight` ∈ [0, ∞). Weight decays, and
   value's distance from +1 decays at the same rate, so unknown is the attractor. Stances: trusted
   ≥ 1.5, leaning_trusted ≥ 1.2, unknown above 0.8, unclear above 0, wary above −1.5, distrusted
   below. *Built.*
4. **Sharing.** Skills (rules) are shared, and a node adopts them at unknown. Opinions travel only
   as hints. Identity and store are never shared. *Partial:* nothing sends hints yet.
5. **Boundary.** Hardware capsule is kernel-adjacent, sensor capsule agent-adjacent, setup
   capsule operator-supplied. HAL is the only code that touches pins; the kernel never does.
   *Partial:* transport is the only hardware in HAL; there is no sensor interface.
6. **`__hardware__` capsule.** Detected at boot. `key=value` evidence strings for `cpu_class`,
   `cpu_cores`, `cpu_mhz`, `ram_bytes`, `flash_bytes`, `storage_bytes`, `clock`, `transports`,
   `crypto`. *Not built.* `boot/genesis.py` still lists fixed capabilities.
7. **`__sensors__` capsule.** One claim per sensor; evidence carries `id`, `type`, `bus`, `pin`,
   `unit`, `min`, `max`, `margin_low`, `margin_high`, `sample_ms`. Margins are operating
   bounds, separate from physical min/max. *Not built.*
8. **`__setup__` capsule.** Pinout, buses, margins and sample rates, supplied by the operator,
   signed and stored at provision time; send a new one to reconfigure. *Not built.*
9. **Read path.** Query `__sensors__`, parse evidence, read through HAL, compare against the
   margins, record an outcome for `sensor:<id>`, emit a reading when it changes or on schedule.
   *Not built.*
10. **Bootstrap.** Key and genesis (signed, stored) → hardware → setup → sensors → discovery →
    handshake → idle loop. *Partial:* genesis is built but not signed or stored; discovery
    reads `peers.json`; the handshake and idle loop exist.
11. **Edge.** ESP32 bots over ESP-NOW with the stripped wire format; autonomous unless a task,
    threshold or stuck condition escalates; a gateway ESP32 bridges ESP-NOW ↔ UART to the
    master, which is a node, not the brain; bots talk peer to peer. *Partial:* wire format and
    upgrade/downgrade only.
12. **Growth.** Children inherit skills, vocabulary and hints at unknown, never identity, opinions
    or store; they detect their own hardware. Replicate for capacity, differentiate for
    capability. Pressure → decision → spawn → boot → handshake → trial (mandatory) → graduation
    → lineage. Death by starvation, merging or inheritance. Spawning costs weight, and
    coordination cost bounds growth. *Not built* (a `lineage` table is in progress).
13. **Build order.** Each step proves one layer: (1) `weight.py` with trajectory tests;
    (2) store and scheduler; (3) laptop ↔ phone, two machines; (4) one ESP32 → gateway →
    master; (5) one hand-spawned child; (6) then automate.

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
- **Behavior is capsules too.** A rule is a signed `rule.<name>` capsule with a JSON spec in its
  directive claim. No new claim type (peers would reject it) and no YAML (no new dependency).
- **A node runs only rules it signed.** Running peers' rules would let any peer change a node's
  behavior. A peer's rule is a hint until the node adopts it, and adoption starts it at unknown,
  the same as "belief is earned locally".
- **Rule trust is local and lives in the node's database**, keyed by rule id, never in the rule
  capsule: capsules are signed and content-addressed, so they can't carry mutable state.
- **A rule lives as long as its trust (Leighton Weight).** Live rules are re-issued before the
  7-day TTL; tested rules are forgotten below weight 0.05, untested ones after 30 days. Chosen over
  a schema change for longer TTLs: forgetting is the theory's own answer to stale knowledge.
- **Outcomes are evidence only from the agent that did the work.** A `task_result` counts if it
  answers a task this node sent to that agent, once per task. ACKs never count: the scheduler ACKs
  automatically, so counting them would be receipt-as-evidence.
- **Rules don't chain.** A rule never fires on another rule's output, so no loops. Revisit with a
  depth limit if multi-step rules are needed.
- **Merge keeps the trigger** of the first parent, or the second's if the first is `none`.
  No strength ordering between triggers yet.
- **Version choice is numeric.** Highest shared version wins by number, not string sort.
- **Identity comes first at boot.** Key and genesis come before the hardware, setup and sensor
  capsules, because those capsules must be signed by someone.
- **Descriptive capsules use `key=value` evidence strings.** Hardware, sensor and setup capsules
  put one fact per evidence string, so they stay readable and queryable without nested JSON.
  Rules are the exception: a rule is a program, and its directive claim holds a JSON spec.
- **Topic opinions persist** in the opinions table as `topic:<topic>`, like `rule:<id>`. Each
  outcome decays the stored opinion to now, then observes. Losing a node's database still loses its
  belief; that is local by design.
- **Node files live under the project root,** not the working directory. A run script started
  from another directory must find the same key, or the node would come back with a new identity
  and its peers would reject it.

## Open questions

- **How far to trust a reported outcome?** A rule learns from what the worker says happened.
  Should outcomes from a peer be weighted by trust in that peer, or confirmed by a second observer?
- **Rule chaining.** Allow rules to fire on rule outputs with a depth limit?
- **Edge outcomes.** The stripped ESP-NOW format has no outcome field. Add one, or have the
  gateway report outcomes for edge devices?
- **Merged sender.** `merge` builds `agent://alice+agent://bob`, which isn't addressable and fails
  schema validation, so no merged capsule is valid today.
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

1. **Two machines.** Scripts are ready (`--host 0.0.0.0`, `peers.json`, clock-offset warnings,
   node files found from the project root) and a session over this machine's network address
   passes. Still to do: an actual second machine, laptop ↔ phone. Expect firewalls, NAT, real
   disconnects and Windows/Linux differences.
2. **Make `hal/` an interface.** Protocols for transport, clock and a sensor bus; move
   implementations out; a simulated sensor bus for the laptop.
3. **Bootstrap:** signed, stored genesis, then `__hardware__`, `__setup__`, `__sensors__`.
4. **Merged sender** decision (merge output currently fails validation).
5. **Prune on a schedule** in `run_alice.py`.
6. **Relaying and a reply queue**, which turn the star into a network.

## Housekeeping

- Old `store/*.log` ledgers are no longer read. Import them with
  `python -m store import <jsonl> <db>`, then delete them.
