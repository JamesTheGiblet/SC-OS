"""
Leighton Weight — unified exponential decay.

W(t) = W0 * exp(-k * t)

Applied to influence, relevance, resource, or confidence.
See: one-pager, "Leighton Weight — A Unified Theory of Decay"
"""

from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from math import exp
from typing import Optional

UNKNOWN = 1.0        # SC-OS resting value; other domains ignore
VALUE_MIN = -2.0
VALUE_MAX = 2.0


# --- the core curve ---------------------------------------------------------

def decay(w0: float, k: float, t: float) -> float:
    """
    W(t) = W0 * exp(-k * t)

    w0: starting weight
    k:  decay constant (per unit time)
    t:  elapsed time (same units as 1/k)
    """
    if k < 0:
        raise ValueError("k must be non-negative")
    if t < 0:
        raise ValueError("t must be non-negative")
    return w0 * exp(-k * t)


def decay_constant(
    k0: float,
    evidence_count: int = 0,
    stakes_factor: float = 1.0,
) -> float:
    """
    k = k0 / (1 + evidence) / stakes

    More evidence -> slower decay.
    Higher stakes -> slower decay.
    """
    if k0 < 0:
        raise ValueError("k0 must be non-negative")
    if evidence_count < 0:
        raise ValueError("evidence_count must be non-negative")
    if stakes_factor <= 0:
        raise ValueError("stakes_factor must be positive")
    return k0 / (1.0 + evidence_count) / stakes_factor


# --- opinion: value + weight ------------------------------------------------

@dataclass
class Opinion:
    """
    An agent's private opinion of a skill / capsule / rule.

    value:  position on [-2, +2], +1 = unknown
    weight: conviction on [0, inf)
    evidence_count: how many reinforcements so far
    last_tested: when reinforcement last occurred
    """
    value: float = UNKNOWN
    weight: float = 0.0
    evidence_count: int = 0
    last_tested: Optional[datetime] = None

    def __post_init__(self):
        if not (VALUE_MIN <= self.value <= VALUE_MAX):
            raise ValueError(f"value must be in [{VALUE_MIN}, {VALUE_MAX}]")
        if self.weight < 0:
            raise ValueError("weight must be non-negative")

    # --- observation ---

    def observe(
        self,
        success: bool,
        now: Optional[datetime] = None,
        *,
        success_step: float = 0.1,
        failure_step: float = 0.2,
        success_weight: float = 1.0,
        failure_weight: float = 3.0,
    ) -> None:
        """
        Reinforce with evidence. Failure weights more than success.
        Both reset the decay clock.
        """
        now = now or datetime.now(timezone.utc)

        if success:
            self.value = min(VALUE_MAX, self.value + success_step)
            self.weight += success_weight
        else:
            self.value = max(VALUE_MIN, self.value - failure_step)
            self.weight += failure_weight

        self.evidence_count += 1
        self.last_tested = now

    # --- drift ---

    def tick(
        self,
        now: Optional[datetime] = None,
        *,
        k0: float = 0.05,
        stakes_factor: float = 1.0,
        dt_unit_seconds: float = 86400.0,
    ) -> None:
        """
        Advance time. Weight decays; value drifts toward UNKNOWN in proportion.
        """
        if self.last_tested is None:
            return
        now = now or datetime.now(timezone.utc)
        dt = (now - self.last_tested).total_seconds()
        if dt <= 0:
            return
        t = dt / dt_unit_seconds

        k = decay_constant(k0, self.evidence_count, stakes_factor)
        ratio = exp(-k * t)

        # weight decays to 0
        self.weight *= ratio

        # value drifts toward UNKNOWN by the same ratio
        self.value = UNKNOWN + (self.value - UNKNOWN) * ratio

        # snap to unknown if weight is negligible
        if self.weight < 1e-6:
            self.value = UNKNOWN
            self.weight = 0.0

    # --- reporting ---

    @property
    def stance(self) -> str:
        if self.weight < 1e-3:
            return "unknown"
        if self.value >= 1.5:
            return "trusted"
        if self.value >= 1.2:
            return "leaning_trusted"
        if self.value > 0.8:
            return "unknown"        # near +1 with weight = weak opinion
        if self.value > 0.0:
            return "unclear"
        if self.value > -1.5:
            return "wary"
        return "distrusted"


# --- convenience ------------------------------------------------------------

def blend(a: Opinion, b: Opinion, alpha: float = 0.5) -> Opinion:
    """
    Combine two opinions (e.g., peer hint + own experience).
    Weighted by their respective weights.
    """
    if alpha < 0 or alpha > 1:
        raise ValueError("alpha must be in [0,1]")
    total = a.weight + b.weight
    if total == 0:
        return Opinion()
    w = a.weight / total if total > 0 else 0.5
    return Opinion(
        value=a.value * w + b.value * (1 - w),
        weight=total,
        evidence_count=a.evidence_count + b.evidence_count,
        last_tested=max(
            (d for d in (a.last_tested, b.last_tested) if d is not None),
            default=None,
        ),
    )