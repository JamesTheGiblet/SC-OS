"""
Execute the theory. Three trajectories side by side:
  - success path
  - failure path
  - idle path

If the numbers move the way the one-pager says, the theory holds.
"""

from datetime import datetime, timezone, timedelta
from weight import Opinion, decay, decay_constant, blend, UNKNOWN

NOW = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)


def hours(n):
    return NOW + timedelta(hours=n)


def days(n):
    return NOW + timedelta(days=n)


def show(label, o: Opinion):
    print(f"  {label:24s} value={o.value:+.3f}  weight={o.weight:6.3f}  "
          f"n={o.evidence_count:3d}  stance={o.stance}")


print("=" * 72)
print("Leighton Weight — trajectory test")
print("=" * 72)

# ---------------------------------------------------------------------------
print("\n[1] Core curve sanity")
print("-" * 72)
w0, k = 10.0, 0.05
for t in (0, 1, 10, 50, 100):
    print(f"  W(t={t:3d}) = {decay(w0, k, t):8.4f}")

print("\n  decay_constant with evidence:")
for n in (0, 1, 10, 100):
    print(f"    evidence={n:3d}  k={decay_constant(0.05, n):.5f}")

# ---------------------------------------------------------------------------
print("\n[2] Three trajectories over 90 days")
print("-" * 72)

# A: reinforced success — tested 3 times, then idle
a = Opinion()
for i in range(3):
    a.observe(success=True, now=hours(i))
print("\n  SUCCESS PATH (3 successes, then idle)")
show("after 3 successes", a)
for d in (7, 30, 60, 90):
    a.tick(now=days(d))
    show(f"idle {d}d", a)

# B: one failure — then idle
b = Opinion()
b.observe(success=False, now=hours(0))
print("\n  FAILURE PATH (1 failure, then idle)")
show("after 1 failure", b)
for d in (7, 30, 60, 90):
    b.tick(now=days(d))
    show(f"idle {d}d", b)

# C: never touched — stays unknown forever
c = Opinion()
print("\n  IDLE PATH (never tested)")
show("fresh", c)
for d in (7, 30, 90, 365):
    c.tick(now=days(d))
    show(f"idle {d}d", c)

# ---------------------------------------------------------------------------
print("\n[3] Asymmetric reinforcement")
print("-" * 72)

d1 = Opinion()
for _ in range(5):
    d1.observe(success=True, now=NOW)
show("5 successes", d1)

d2 = Opinion()
for _ in range(5):
    d2.observe(success=False, now=NOW)
show("5 failures", d2)

print("\n  Note: failures push value further and add more weight.")

# ---------------------------------------------------------------------------
print("\n[4] Sharing rule — blend peer hint with own experience")
print("-" * 72)

peer = Opinion(value=1.8, weight=50.0, evidence_count=50, last_tested=NOW)
own  = Opinion()
own.observe(success=True, now=NOW)
own.observe(success=False, now=NOW)
show("peer hint (50 tests)", peer)
show("own experience (1+1)", own)

own_before = (own.value, own.weight, own.evidence_count, own.last_tested)
combined = blend(own, peer)              # trust=0.1: peer counts as weight 5
print()
show("view (trust=0.1)", combined)
print("\n  The hint moves the value, but a 50-test peer counts like 5 of your own")
print("  weight. Evidence count and decay clock stay yours: a hint is not evidence.")

assert combined.evidence_count == own.evidence_count
assert combined.last_tested == own.last_tested
assert own.value < combined.value < peer.value
assert combined.stance != "trusted"
assert blend(own, peer, trust=0).value == own.value
assert blend(Opinion(), Opinion()).value == UNKNOWN
full = blend(own, peer, trust=1.0)
assert abs(full.value - (own.value * own.weight + peer.value * peer.weight)
           / (own.weight + peer.weight)) < 1e-12
assert (own.value, own.weight, own.evidence_count, own.last_tested) == own_before
try:
    blend(own, peer, trust=1.5)
    raise AssertionError("trust > 1 must be rejected")
except ValueError:
    pass

# ---------------------------------------------------------------------------
print("\n[5] Invariance check — same curve, different domains")
print("-" * 72)

def simulate(domain, w0, k0, evidence=0, stakes=1.0, days_out=(0, 30, 90)):
    print(f"\n  {domain}:  W0={w0}  k0={k0}  evidence={evidence}  stakes={stakes}")
    k = decay_constant(k0, evidence, stakes)
    for d in days_out:
        w = decay(w0, k, d)
        print(f"    day {d:3d}  W={w:8.4f}  ({100*w/w0:5.1f}% of original)")

simulate("BlockForge (block influence)", w0=1.0, k0=0.02)
simulate("ChatterPet (word relevance)",   w0=1.0, k0=0.10)
simulate("HABITAT    (biome resource)",   w0=1.0, k0=0.03)
simulate("SC-OS      (skill confidence)", w0=1.0, k0=0.05, evidence=10, stakes=2.0)

print("\n" + "=" * 72)
print("If the curves look like exponential decay with different half-lives,")
print("the invariance claim holds. Same shape. Different k. One theory.")
print("=" * 72)