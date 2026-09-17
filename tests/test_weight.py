"""
Leighton Weight: the curve, three trajectories (success, failure, idle),
asymmetric reinforcement, the sharing rule, and one curve across domains.
Run from the project root: python tests/test_weight.py
"""

import sys
from datetime import datetime, timedelta, timezone
from math import exp, isclose, log
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from weight import UNKNOWN, Opinion, blend, decay, decay_constant

NOW = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)
IDLE_DAYS = (7, 30, 60, 90)


def hours(n: float) -> datetime:
    return NOW + timedelta(hours=n)


def days(n: float) -> datetime:
    return NOW + timedelta(days=n)


def after_outcomes(*outcomes: bool) -> Opinion:
    o = Opinion()
    for i, success in enumerate(outcomes):
        o.observe(success=success, now=hours(i))
    return o


def idle(o: Opinion) -> list[Opinion]:
    """Snapshots of o after ticking to each of IDLE_DAYS in turn."""
    out = []
    for d in IDLE_DAYS:
        o.tick(now=days(d))
        out.append(Opinion(o.value, o.weight, o.evidence_count, o.last_tested))
    return out


# --- the curve ---------------------------------------------------------------

def test_decay_is_exponential():
    assert decay(10.0, 0.05, 0) == 10.0
    assert isclose(decay(10.0, 0.05, 20), 10.0 * exp(-1))
    half_life = log(2) / 0.05
    assert isclose(decay(1.0, 0.05, half_life), 0.5)
    assert decay(1.0, 0.0, 1000) == 1.0          # k = 0: no decay


def test_decay_rejects_negative_inputs():
    for args in ((1.0, -0.1, 1.0), (1.0, 0.1, -1.0)):
        try:
            decay(*args)
            raise AssertionError(f"decay{args} must be rejected")
        except ValueError:
            pass


def test_evidence_and_stakes_slow_decay():
    assert decay_constant(0.05, 0) == 0.05
    assert isclose(decay_constant(0.05, 9), 0.005)
    assert isclose(decay_constant(0.05, 0, stakes_factor=2.0), 0.025)
    ks = [decay_constant(0.05, n) for n in (0, 1, 10, 100)]
    assert ks == sorted(ks, reverse=True)
    for bad in ({"k0": -1.0}, {"k0": 0.05, "evidence_count": -1}, {"k0": 0.05, "stakes_factor": 0}):
        try:
            decay_constant(**bad)
            raise AssertionError(f"decay_constant({bad}) must be rejected")
        except ValueError:
            pass


# --- three trajectories -----------------------------------------------------

def test_success_path_rises_then_fades_to_unknown():
    a = after_outcomes(True, True, True)
    assert isclose(a.value, 1.3) and a.weight == 3.0 and a.evidence_count == 3
    assert a.stance == "leaning_trusted"

    snaps = idle(a)
    values = [s.value for s in snaps]
    weights = [s.weight for s in snaps]
    assert values == sorted(values, reverse=True) and all(v > UNKNOWN for v in values)
    assert weights == sorted(weights, reverse=True)
    assert snaps[0].stance == "leaning_trusted"      # a week idle: still leaning
    assert snaps[-1].stance == "unknown"             # 90 days idle: back to unknown
    assert snaps[-1].evidence_count == 3             # idling never erases evidence
    # 3 successes: k = 0.05 / 4; last tested 2 hours after NOW
    ratio = exp(-0.0125 * (90 - 2 / 24))
    assert isclose(snaps[-1].weight, 3.0 * ratio)
    assert isclose(snaps[-1].value, UNKNOWN + 0.3 * ratio)


def test_failure_path_drops_then_fades_to_unknown():
    b = after_outcomes(False)
    assert isclose(b.value, 0.8) and b.weight == 3.0 and b.stance == "unclear"

    snaps = idle(b)
    values = [s.value for s in snaps]
    assert values == sorted(values) and all(v < UNKNOWN for v in values)
    assert snaps[-1].stance == "unknown"
    ratio = exp(-0.025 * 90)                         # 1 failure: k = 0.05 / 2
    assert isclose(snaps[-1].weight, 3.0 * ratio)
    assert isclose(snaps[-1].value, UNKNOWN - 0.2 * ratio)


def test_idle_path_stays_unknown():
    c = Opinion()
    for d in (7, 30, 90, 365):
        c.tick(now=days(d))
        assert (c.value, c.weight, c.evidence_count, c.stance) == (UNKNOWN, 0.0, 0, "unknown")


def test_negligible_weight_snaps_to_unknown():
    b = after_outcomes(False)
    b.tick(now=days(2000))
    assert (b.value, b.weight) == (UNKNOWN, 0.0)


def test_ticking_in_steps_equals_one_tick():
    stepped, once = after_outcomes(True, False, True), after_outcomes(True, False, True)
    for d in IDLE_DAYS:
        stepped.tick(now=days(d))
    once.tick(now=days(IDLE_DAYS[-1]))
    assert isclose(stepped.weight, once.weight) and isclose(stepped.value, once.value)
    stepped.tick(now=days(IDLE_DAYS[-1]))            # ticking to the same time again is a no-op
    assert isclose(stepped.weight, once.weight)


def test_outcome_after_idle_decays_first_then_learns():
    o = after_outcomes(True)
    o.tick(now=days(30))
    faded = o.weight
    o.observe(success=True, now=days(30))
    assert isclose(o.weight, faded + 1.0)
    o.tick(now=days(31))                             # decay restarts from the new outcome
    assert isclose(o.weight, (faded + 1.0) * exp(-decay_constant(0.05, 2) * 1))


def test_value_is_bounded():
    up = after_outcomes(*[True] * 30)
    down = after_outcomes(*[False] * 30)
    assert up.value == 2.0 and down.value == -2.0
    assert up.stance == "trusted" and down.stance == "distrusted"
    try:
        Opinion(value=2.5)
        raise AssertionError("value outside [-2, 2] must be rejected")
    except ValueError:
        pass


# --- asymmetric reinforcement -----------------------------------------------

def test_failure_counts_more_than_success():
    ups, downs = after_outcomes(*[True] * 5), after_outcomes(*[False] * 5)
    assert abs(downs.value - UNKNOWN) > abs(ups.value - UNKNOWN)
    assert downs.weight > ups.weight
    assert after_outcomes(True).stance == "unknown"          # one success is not yet an opinion
    assert after_outcomes(False).stance == "unclear"         # one failure already is


# --- sharing rule -----------------------------------------------------------

def test_peer_hint_moves_value_but_is_not_evidence():
    peer = Opinion(value=1.8, weight=50.0, evidence_count=50, last_tested=NOW)
    own = after_outcomes(True, False)
    own_before = (own.value, own.weight, own.evidence_count, own.last_tested)

    view = blend(own, peer)                          # trust=0.1: peer counts as weight 5
    assert isclose(view.weight, own.weight + 5.0)
    assert view.evidence_count == own.evidence_count
    assert view.last_tested == own.last_tested
    assert own.value < view.value < peer.value
    assert view.stance != "trusted"
    assert (own.value, own.weight, own.evidence_count, own.last_tested) == own_before


def test_blend_edges():
    own = after_outcomes(True, False)
    peer = Opinion(value=1.8, weight=50.0, evidence_count=50, last_tested=NOW)
    assert blend(own, peer, trust=0).value == own.value
    assert blend(Opinion(), Opinion()).value == UNKNOWN
    full = blend(own, peer, trust=1.0)
    assert isclose(full.value, (own.value * own.weight + peer.value * peer.weight)
                   / (own.weight + peer.weight))
    try:
        blend(own, peer, trust=1.5)
        raise AssertionError("trust > 1 must be rejected")
    except ValueError:
        pass


# --- one curve, four domains ------------------------------------------------

def test_same_curve_different_half_lives():
    domains = {                       # k0, evidence, stakes
        "BlockForge": (0.02, 0, 1.0),
        "ChatterPet": (0.10, 0, 1.0),
        "HABITAT":    (0.03, 0, 1.0),
        "SC-OS":      (0.05, 10, 2.0),
    }
    for name, (k0, evidence, stakes) in domains.items():
        k = decay_constant(k0, evidence, stakes)
        half_life = log(2) / k
        assert isclose(decay(1.0, k, half_life), 0.5), name
        assert isclose(decay(1.0, k, 2 * half_life), 0.25), name


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"{len(tests)} passed")
