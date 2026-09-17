"""
Validator: schema, version, time, vocabulary, coherence, provenance and outcome
rules. Every rejection names its code.
Run from the project root: python tests/test_validator.py
"""

import copy
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from interpreter import to_wire
from primitive import (
    Capsule, Claim, ClaimType, Intent, Outcome, Provenance, Relation, Semantics, Trigger,
)
from validator import CLOCK_SKEW_TOLERANCE_SECONDS, CapsuleRejected, validate

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
BOB, ALICE = "agent://bob", "agent://alice"


def uid() -> str:
    return f"urn:uuid:{uuid.uuid4()}"


def wire(**kw) -> dict:
    fields = dict(
        id=uid(), created=NOW, sender=BOB, receiver=ALICE, intent=Intent.INFORM,
        semantics=Semantics(topic="supply_chain_risk",
                            claims=(Claim("Supplier X down 40%", ClaimType.OBSERVATION, 0.82),),
                            relations=(Relation("SupplierX", "affects", "ProductY"),)),
    )
    fields.update(kw)
    return to_wire(Capsule(**fields))


def rejected(d: dict, now: datetime = NOW) -> str:
    try:
        validate(d, now=now)
    except CapsuleRejected as e:
        return e.code
    raise AssertionError("expected CapsuleRejected")


def task(**kw) -> dict:
    return wire(intent=Intent.REQUEST, trigger=Trigger.TASK,
                semantics=Semantics("check", claims=(Claim("verify X", ClaimType.DIRECTIVE, 1.0),)), **kw)


# --- accepted ---------------------------------------------------------------

def test_valid_capsules_pass():
    validate(wire(), now=NOW)
    validate(task(), now=NOW)
    validate(wire(intent=Intent.ACK, semantics=Semantics("x")), now=NOW)   # ack needs no claims
    validate(wire(trigger=Trigger.TASK_RESULT, provenance=Provenance((uid(),), "observation"),
                  outcome=Outcome("success")), now=NOW)
    validate(wire(provenance=Provenance((uid(), uid()), "merge")), now=NOW)
    validate(wire(provenance=Provenance((uid(), uid()), "rule")), now=NOW)


# --- schema -----------------------------------------------------------------

def test_schema_rejections():
    cases = []
    d = wire(); del d["semantics"]; cases.append(d)
    d = wire(); d["id"] = "not-a-urn"; cases.append(d)
    d = wire(); d["from"] = "bob"; cases.append(d)
    d = wire(); d["to"] = "agent://has space"; cases.append(d)
    d = wire(); d["intent"] = "shout"; cases.append(d)
    d = wire(); d["trigger"] = "whenever"; cases.append(d)
    d = wire(); d["extra"] = 1; cases.append(d)
    d = wire(); d["semantics"]["claims"][0]["confidence"] = 1.5; cases.append(d)
    d = wire(); d["semantics"]["claims"][0]["type"] = "rumour"; cases.append(d)
    d = wire(); d["semantics"]["topic"] = ""; cases.append(d)
    d = wire(); d["provenance"]["method"] = "guess"; cases.append(d)
    d = wire(); d["outcome"] = {"status": "maybe"}; cases.append(d)
    for i, d in enumerate(cases):
        assert rejected(d) == "schema", f"case {i}"


def test_unsupported_version():
    d = wire(); d["capsule_version"] = "2.0"
    assert rejected(d) == "version"


# --- time -------------------------------------------------------------------

def test_clock_skew_tolerance():
    ok = CLOCK_SKEW_TOLERANCE_SECONDS - 1
    validate(wire(created=NOW + timedelta(seconds=ok)), now=NOW)
    assert rejected(wire(created=NOW + timedelta(seconds=CLOCK_SKEW_TOLERANCE_SECONDS + 1))) == "temporal"


def test_older_than_a_week_rejected():
    validate(wire(created=NOW - timedelta(days=6)), now=NOW)
    assert rejected(wire(created=NOW - timedelta(days=7, seconds=1))) == "temporal"


def test_naive_timestamp_rejected():
    d = wire(); d["created"] = NOW.replace(tzinfo=None).isoformat()
    assert rejected(d) in ("schema", "temporal")


def test_expired_claim_is_stale():
    past = Claim("old news", ClaimType.OBSERVATION, 0.9, valid_until=NOW - timedelta(seconds=1))
    assert rejected(wire(semantics=Semantics("news", claims=(past,)))) == "stale"


# --- vocabulary and coherence -----------------------------------------------

def test_unknown_predicate():
    d = wire(); d["semantics"]["relations"][0]["predicate"] = "loves"
    assert rejected(d) == "vocab"


def test_task_needs_a_directive():
    d = task(); d["semantics"]["claims"][0]["type"] = "observation"
    assert rejected(d) == "coherence"


def test_inform_needs_claims():
    assert rejected(wire(semantics=Semantics("empty"))) == "coherence"


def test_merge_and_rule_need_two_parents():
    for method in ("merge", "rule"):
        assert rejected(wire(provenance=Provenance((uid(),), method))) == "provenance", method


def test_outcome_rules():
    parent = Provenance((uid(),), "observation")
    no_outcome = wire(trigger=Trigger.TASK_RESULT, provenance=parent)
    assert rejected(no_outcome) == "coherence"
    orphan = wire(trigger=Trigger.TASK_RESULT, outcome=Outcome("failure"))
    assert rejected(orphan) == "coherence"
    stray = wire(provenance=parent, outcome=Outcome("success"))
    assert rejected(stray) == "coherence"


def test_validate_does_not_modify_input():
    d = wire()
    before = copy.deepcopy(d)
    validate(d, now=NOW)
    assert d == before


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"{len(tests)} passed")
