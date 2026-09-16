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
| SC-OS      | skill confidence | successful use   | untested time|

## The SC-OS Application

A capsule is a skill. Each agent holds a private opinion:

    value  in [-2, +2]   +1 = UNKNOWN, +2 = known-good,
                          0 = unclear, -2 = known-bad
    weight in [0, inf)   conviction

The value does not decay. The weight does.
As weight decays, value drifts toward +1 (unknown).

Unknown is the attractor. Everything else is temporary.

## The Sharing Rule

Skills are shared. Opinions are not.
Capability transfers. Belief is earned locally.

## Running the test

    python test_weight.py

## Open questions

- Is `value` one axis, or does it need a separate `unclear` axis?
- How does contradiction propagate between agents?
- What is `stakes_factor` precisely?
- Can `k` ever be zero (immortal opinion)?

## Status

Draft 1 — 2026-09-16. For stress-testing, not citation.
"""