"""Leighton Weight

A unified exponential decay model applied across four domains:
BlockForge, ChatterPet, HABITAT, and SC-OS.

## The Principle

    W(t) = W0 * exp(-k * t)

Influence fades exponentially unless reinforced.

## The Domains

| Domain     | W measures       | Reinforcement    | k driven by  |
|------------|------------------|------------------|--------------|
| BlockForge | block influence  | re-weighting     | chain age    |
| ChatterPet | word relevance   | repetition       | disuse       |
| HABITAT    | biome resource   | regrowth         | consumption  |
| SC-OS      | skill confidence | observed outcome | untested time|

## The SC-OS Application

A capsule is a skill. Each agent holds a private opinion:

    value  in [-2, +2]   +1 = UNKNOWN, +2 = known-good,
                          0 = unclear, -2 = known-bad
    weight in [0, inf)   conviction

Weight decays toward 0. Value's distance from unknown decays at the same rate:

    ratio  = exp(-k * t),   k = k0 / (1 + evidence_count) / stakes_factor
    weight = weight * ratio
    value  = 1 + (value - 1) * ratio

So value is not fixed: it slides back toward +1 exactly as fast as conviction
fades. When weight falls below 1e-6, value snaps to +1 and weight to 0.

Only observed outcomes reinforce (Scheduler.record_outcome). Receiving a
capsule is not evidence.

    success: value += 0.1, weight += 1
    failure: value -= 0.2, weight += 3

Unknown is the attractor. Everything else is temporary.

## The Sharing Rule

Skills are shared. Opinions travel only as hints.
Capability transfers. Belief is earned locally.

A capsule may carry its sender's opinion (the `epistemic` block). The
receiver can fold that hint into a decision with blend(own, peer, trust):

    peer_w = trust * peer.weight          trust in [0, 1], default 0.1
    value  = (own.value * own.weight + peer.value * peer_w) / (own.weight + peer_w)
    weight = own.weight + peer_w
    evidence_count, last_tested = own's

The result is a view, never stored. A peer can't outweigh the same amount of
your own evidence, can't add to your evidence count, and can't slow your
decay. Only your own observed outcomes change what you believe.

## Running the test

From the project root, with the root on the import path:

    PYTHONPATH=. python tests/test_weight.py

## Open questions

- Is `value` one axis, or does it need a separate `unclear` axis?
- How does contradiction propagate between agents?
- What is `stakes_factor` precisely?
- Can `k` ever be zero (immortal opinion)?

## Status

Draft 1 — 2026-09-16. For stress-testing, not citation.
"""