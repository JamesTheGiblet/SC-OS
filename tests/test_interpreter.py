"""
Interpreter: wire round-trip, digest stability, ingest, expiry, actionable,
and merge.
Run from the project root: python tests/test_interpreter.py
"""

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from envelope import digest
from interpreter import actionable, from_wire, ingest, is_expired, merge, render, to_wire
from primitive import (
    ActionHints, Capsule, Claim, ClaimType, Epistemic, Intent, Outcome, Provenance, Relation,
    Semantics, Trigger, Uncertainty,
)
from validator import CapsuleRejected, validate

BOB, CAROL, ALICE = "agent://bob", "agent://carol", "agent://alice"


def uid() -> str:
    return f"urn:uuid:{uuid.uuid4()}"


def full_capsule(now: datetime | None = None) -> Capsule:
    """Every optional field set, so the round-trip covers them all."""
    now = now or datetime.now(timezone.utc)
    return Capsule(
        id=uid(), created=now, sender=BOB, receiver=ALICE,
        intent=Intent.INFORM, trigger=Trigger.TASK_RESULT,
        semantics=Semantics(
            topic="supply_chain_risk",
            claims=(Claim("Supplier X down 40%", ClaimType.OBSERVATION, 0.82,
                          evidence=("source:reuters",), valid_until=now + timedelta(days=14)),),
            relations=(Relation("SupplierX", "affects", "ProductY"),),
            uncertainty=Uncertainty(known_unknowns=("recovery_timeline",), assumptions=("no strike",)),
        ),
        provenance=Provenance(derived_from=(uid(),), method="observation"),
        action_hints=ActionHints(priority="high", ttl_seconds=600, requires_ack=True),
        epistemic=Epistemic(value=1.3, weight=3.0, evidence_count=3, last_tested=now),
        outcome=Outcome("success", "checked"),
    )


def claim_capsule(sender=BOB, claims=(), trigger=Trigger.NONE, topic="risk",
                  intent=Intent.INFORM, **kw) -> Capsule:
    return Capsule(id=uid(), created=datetime.now(timezone.utc), sender=sender, receiver=ALICE,
                   intent=intent, trigger=trigger, semantics=Semantics(topic, claims=tuple(claims), **kw))


# --- wire -------------------------------------------------------------------

def test_round_trip_is_exact():
    c = full_capsule()
    assert from_wire(to_wire(c)) == c
    assert to_wire(from_wire(to_wire(c))) == to_wire(c)


def test_digest_is_stable_across_round_trips():
    w = to_wire(full_capsule())
    assert digest(w) == digest(to_wire(from_wire(w)))


def test_outcome_key_absent_unless_set():
    c = claim_capsule(claims=(Claim("x", ClaimType.OBSERVATION, 1.0),))
    assert "outcome" not in to_wire(c)
    assert from_wire(to_wire(c)).outcome is None


def test_from_wire_fills_defaults():
    w = to_wire(claim_capsule(claims=(Claim("x", ClaimType.OBSERVATION, 1.0),)))
    for key in ("trigger", "provenance", "action_hints", "epistemic"):
        del w[key]
    c = from_wire(w)
    assert c.trigger == Trigger.NONE
    assert c.provenance == Provenance()
    assert c.action_hints == ActionHints()
    assert c.epistemic == Epistemic()


def test_ingest_validates():
    w = to_wire(full_capsule())
    assert ingest(w) == from_wire(w)
    w["semantics"]["claims"][0]["confidence"] = 2
    try:
        ingest(w)
        raise AssertionError("invalid capsule must be rejected")
    except CapsuleRejected as e:
        assert e.code == "schema"


# --- expiry and actionable --------------------------------------------------

def test_expiry_by_ttl_and_by_claim():
    c = full_capsule()                                   # ttl 600 s, claim valid 14 days
    assert not is_expired(c, now=c.created + timedelta(seconds=599))
    assert is_expired(c, now=c.created + timedelta(seconds=601))
    short = Capsule(**{**c.__dict__, "action_hints": ActionHints(ttl_seconds=10**9),
                       "semantics": Semantics("x", claims=(Claim("y", ClaimType.OBSERVATION, 1.0,
                                                                 valid_until=c.created + timedelta(hours=1)),))})
    assert not is_expired(short, now=c.created + timedelta(minutes=59))
    assert is_expired(short, now=c.created + timedelta(minutes=61))


def test_actionable():
    q = claim_capsule(intent=Intent.QUERY)
    r = claim_capsule(intent=Intent.REQUEST)
    assert actionable(q) and actionable(r)
    assert not actionable(claim_capsule(intent=Intent.INFORM))
    assert not actionable(Capsule(**{**r.__dict__, "action_hints": ActionHints(priority="low")}))
    old = Capsule(**{**r.__dict__, "created": r.created - timedelta(hours=2)})   # past 3600 s ttl
    assert not actionable(old)


# --- merge ------------------------------------------------------------------

def test_merge_keeps_highest_confidence_per_statement():
    a = claim_capsule(claims=(Claim("X down", ClaimType.OBSERVATION, 0.6),
                              Claim("Y fine", ClaimType.OBSERVATION, 0.9)))
    b = claim_capsule(sender=CAROL, claims=(Claim("X down", ClaimType.INFERENCE, 0.8),))
    m = merge(a, b)
    by = {cl.statement: cl for cl in m.semantics.claims}
    assert set(by) == {"X down", "Y fine"}
    assert by["X down"].confidence == 0.8 and by["X down"].type == ClaimType.INFERENCE
    assert by["Y fine"].confidence == 0.9
    tie = merge(claim_capsule(claims=(Claim("Z", ClaimType.OBSERVATION, 0.5),)),
                claim_capsule(claims=(Claim("Z", ClaimType.ASSUMPTION, 0.5),)))
    assert tie.semantics.claims[0].type == ClaimType.OBSERVATION   # a tie keeps the first


def test_merge_provenance_and_contents():
    a = claim_capsule(claims=(Claim("X", ClaimType.OBSERVATION, 0.6),),
                      relations=(Relation("A", "affects", "B"),),
                      uncertainty=Uncertainty(known_unknowns=("when",)))
    b = claim_capsule(sender=CAROL, claims=(Claim("Y", ClaimType.OBSERVATION, 0.7),),
                      relations=(Relation("B", "causes", "C"),),
                      uncertainty=Uncertainty(assumptions=("stable",)))
    m = merge(a, b)
    assert m.provenance.method == "merge"
    assert m.provenance.derived_from == (a.id, b.id)
    assert m.intent == Intent.INFORM and m.receiver == a.receiver
    assert m.semantics.relations == a.semantics.relations + b.semantics.relations
    assert m.semantics.uncertainty == Uncertainty(known_unknowns=("when",), assumptions=("stable",))
    assert m.id not in (a.id, b.id)


def test_merge_trigger():
    stuck = claim_capsule(trigger=Trigger.STUCK, claims=(Claim("X", ClaimType.OBSERVATION, 1.0),))
    hot = claim_capsule(trigger=Trigger.THRESHOLD, claims=(Claim("Y", ClaimType.OBSERVATION, 1.0),))
    plain = claim_capsule(claims=(Claim("Z", ClaimType.OBSERVATION, 1.0),))
    assert merge(stuck, hot).trigger == Trigger.STUCK        # first parent's
    assert merge(plain, hot).trigger == Trigger.THRESHOLD    # second's if the first is none


def test_merge_rejects_different_topics():
    try:
        merge(claim_capsule(topic="a"), claim_capsule(topic="b"))
        raise AssertionError("merge across topics must be rejected")
    except ValueError:
        pass


def test_merge_is_valid_apart_from_its_sender():
    a = claim_capsule(claims=(Claim("X", ClaimType.OBSERVATION, 0.6),))
    b = claim_capsule(sender=CAROL, claims=(Claim("Y", ClaimType.OBSERVATION, 0.7),))
    w = to_wire(merge(a, b))
    # merge joins senders ("agent://bob+agent://carol", even "agent://bob+agent://bob"),
    # which the schema rejects, so no merged capsule validates today. Who the sender should
    # be is an open question in NOTES.md; everything else about the merge must be valid.
    w["from"] = ALICE
    validate(w)


def test_render_mentions_topic_and_claims():
    text = render(full_capsule())
    assert "supply_chain_risk" in text and "Supplier X down 40%" in text and "recovery_timeline" in text


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"{len(tests)} passed")
