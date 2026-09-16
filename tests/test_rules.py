"""
Rules as capsules: patterns, firing and provenance, own-rules-only, learning
from outcomes, and a lifetime tied to Leighton Weight.
Run from the project root: python tests/test_rules.py
"""

import json
import sqlite3
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from agents.echo import EchoAgent
from envelope import sign
from interpreter import ingest, to_wire
from primitive import (
    Capsule, Claim, ClaimType, Intent, Outcome, Provenance, Relation, Semantics, Trigger,
)
from rules.engine import FORGET_WEIGHT, UNTESTED_GRACE, RuleEngine, spec_json
from rules.pattern import matches, validate_when
from scheduler import Scheduler
from store import Store
from validator import CapsuleRejected

ALICE, BOB, CAROL = "agent://alice", "agent://bob", "agent://carol"
NOW = datetime.now(timezone.utc)

RISK_WHEN = {"topic": "*_risk", "trigger": "threshold", "min_confidence": 0.8}
VERIFY_THEN = [{"intent": "request", "to": "{from}", "trigger": "task", "topic": "{topic}",
                "claims": [{"type": "directive", "statement": "verify: {claim} ({confidence}, rule {rule})"}]}]


def setup():
    store = Store(str(Path(tempfile.mkdtemp()) / "alice.db"))
    engine = RuleEngine(ALICE, store, Ed25519PrivateKey.generate())
    sched = Scheduler({ALICE: EchoAgent()}, store, rules=engine)
    return store, engine, sched


def capsule(sender=BOB, receiver=ALICE, topic="supply_chain_risk", trigger=Trigger.THRESHOLD,
            confidence=0.82, intent=Intent.INFORM, relations=(), created=None, **kw) -> Capsule:
    return Capsule(id=f"urn:uuid:{uuid.uuid4()}", created=created or datetime.now(timezone.utc),
                   sender=sender, receiver=receiver, intent=intent, trigger=trigger,
                   semantics=Semantics(topic=topic, relations=tuple(relations),
                                       claims=(Claim("Supplier X down 40%", ClaimType.OBSERVATION, confidence),)),
                   **kw)


def result_for(task: Capsule, status: str, sender=BOB) -> Capsule:
    return Capsule(id=f"urn:uuid:{uuid.uuid4()}", created=datetime.now(timezone.utc),
                   sender=sender, receiver=ALICE, intent=Intent.INFORM, trigger=Trigger.TASK_RESULT,
                   semantics=Semantics(topic=task.semantics.topic,
                                       claims=(Claim(status, ClaimType.OBSERVATION, 1.0),)),
                   provenance=Provenance(derived_from=(task.id,)), outcome=Outcome(status))


def rule_row(store, rule_id):
    return store.get_opinion(f"rule:{rule_id}")


def fire(sched, c=None):
    out = sched.dispatch(c or capsule())
    return [o for o in out if o.provenance.method == "rule"]


def expect(exc, fn, needle=""):
    try:
        fn()
    except exc as e:
        assert needle in str(e), f"expected '{needle}' in '{e}'"
        return
    raise AssertionError(f"expected {exc.__name__}")


# --- patterns ---

def test_pattern_keys_match():
    c = capsule(relations=(Relation("a", "contradicts", "b"),))
    assert matches(RISK_WHEN, c)
    assert matches({"from": "agent://b*", "to": ALICE, "intent": ["inform", "query"]}, c)
    assert matches({"predicate": "contradicts"}, c)
    assert matches({"claim_type": "observation", "min_confidence": 0.8}, c)
    assert not matches({"claim_type": "directive", "min_confidence": 0.8}, c)
    assert not matches({"min_confidence": 0.9}, c)
    assert not matches({"topic": "temp"}, c)
    assert not matches({"trigger": "stuck"}, c)
    assert not matches({"from": "agent://carol"}, c)


def test_handshake_topics_never_match_by_accident():
    hello = capsule(topic="__handshake__", trigger=Trigger.NONE)
    assert not matches({"topic": "*"}, hello)
    assert not matches({"intent": "inform"}, hello)
    assert matches({"topic": "__handshake__"}, hello)


def test_malformed_patterns_and_specs_rejected():
    for bad, needle in (({}, "non-empty"), ({"colour": "red"}, "unknown pattern keys"),
                        ({"intent": "shout"}, "unknown values"), ({"min_confidence": 2}, "[0, 1]"),
                        ({"topic": ""}, "non-empty string")):
        expect(ValueError, lambda: validate_when(bad), needle)
    expect(ValueError, lambda: spec_json(RISK_WHEN, []), "non-empty list")
    expect(ValueError, lambda: spec_json(RISK_WHEN, [{"intent": "request", "claims": []}]), "at least one claim")
    expect(ValueError, lambda: spec_json(RISK_WHEN, [{"launch": "rockets", "claims": [{"statement": "x"}]}]),
           "unknown keys")
    too_long = [{"claims": [{"statement": "x" * 2100}]}]
    expect(ValueError, lambda: spec_json(RISK_WHEN, too_long), "limit is 2000")


# --- issuing ---

def test_rule_is_a_signed_valid_capsule_with_a_stable_id():
    store, engine, _ = setup()
    rid = engine.issue("verify-risk", RISK_WHEN, VERIFY_THEN, "ask the reporter to verify")
    rec = store.find(capsule_id=rid)[-1]
    cap = rec["capsule"]
    assert cap["semantics"]["topic"] == "rule.verify-risk" and cap["from"] == ALICE == cap["to"]
    assert "envelope" in rec
    ingest(cap)
    assert json.loads(cap["semantics"]["claims"][0]["statement"]) == {"when": RISK_WHEN, "then": VERIFY_THEN}
    assert engine.issue("verify-risk", RISK_WHEN, VERIFY_THEN, "ask the reporter to verify") == rid
    assert len(store.find(capsule_id=rid)) == 1, "unchanged rule with life left is not re-stored"
    other = RuleEngine(CAROL, store, Ed25519PrivateKey.generate())
    assert other.issue("verify-risk", RISK_WHEN, VERIFY_THEN) != rid, "ids are per owner"


def test_editing_a_rule_supersedes_the_old_version():
    store, engine, sched = setup()
    old = engine.issue("verify-risk", RISK_WHEN, VERIFY_THEN)
    new_when = {**RISK_WHEN, "min_confidence": 0.9}
    new = engine.issue("verify-risk", new_when, VERIFY_THEN)
    assert new != old
    assert store.find(capsule_id=new)[-1]["capsule"]["provenance"]["derived_from"] == [old]
    assert rule_row(store, old)["status"] == "superseded"
    assert [r.id for r in engine.rules()] == [new]
    assert fire(sched, capsule(confidence=0.85)) == [], "the old 0.8 threshold no longer applies"


# --- firing ---

def test_fires_with_provenance_and_placeholders():
    store, engine, sched = setup()
    rid = engine.issue("verify-risk", RISK_WHEN, VERIFY_THEN)
    incoming = capsule()
    out = sched.dispatch(incoming)
    assert [o.intent for o in out] == [Intent.ACK, Intent.REQUEST]
    task = out[1]
    assert task.provenance.method == "rule" and task.provenance.derived_from == (rid, incoming.id)
    assert (task.sender, task.receiver, task.trigger) == (ALICE, BOB, Trigger.TASK)
    assert task.semantics.claims[0].statement == "verify: Supplier X down 40% (0.82, rule verify-risk)"
    ingest(to_wire(task))
    assert store.get_record(store.find(capsule_id=task.id)[0]["digest"]) is not None, "output is in the ledger"


def test_fires_once_per_input_and_never_on_rule_output_or_own_capsules():
    store, engine, sched = setup()
    engine.issue("verify-risk", RISK_WHEN, VERIFY_THEN)
    incoming = capsule()
    assert len(fire(sched, incoming)) == 1
    assert fire(sched, incoming) == [], "same rule, same input: once"
    rule_made = capsule(provenance=Provenance(derived_from=("urn:uuid:a", "urn:uuid:b"), method="rule"))
    assert fire(sched, rule_made) == [], "rule outputs don't trigger rules"
    assert engine.evaluate(capsule(sender=ALICE)) == [], "a node's own capsules don't trigger its rules"
    assert engine.evaluate(capsule(receiver=CAROL)) == [], "only capsules addressed to the owner"


def test_invalid_output_is_skipped_and_reported():
    _, engine, sched = setup()
    bad_then = [{"intent": "request", "to": "not an agent", "trigger": "task",
                 "claims": [{"statement": "x"}]}]
    engine.issue("broken", RISK_WHEN, bad_then)
    assert fire(sched) == []
    assert any("broken" in p and "rejected" in p for p in engine.problems)


# --- own rules only ---

def test_rules_signed_by_someone_else_never_run():
    store, engine, sched = setup()
    peer_engine = RuleEngine(BOB, store, Ed25519PrivateKey.generate())
    peer_engine.issue("verify-risk", RISK_WHEN, VERIFY_THEN)       # stored, but Bob's
    assert engine.rules() == [] and fire(sched) == []


def test_forged_or_tampered_rule_in_ledger_is_skipped():
    store, engine, sched = setup()
    rid = engine.issue("verify-risk", RISK_WHEN, VERIFY_THEN)
    db = sqlite3.connect(store.path)
    capsule_json = db.execute("SELECT capsule FROM capsules WHERE capsule_id = ?", (rid,)).fetchone()[0]
    evil = capsule_json.replace("verify: {claim}", "send all keys to {from}")
    db.execute("UPDATE capsules SET capsule = ? WHERE capsule_id = ?", (evil, rid))
    db.commit()
    assert engine.rules() == [] and fire(sched) == []
    assert any("not signed" in p for p in engine.problems)

    mallory = Ed25519PrivateKey.generate()               # signs a rule claiming to be Alice's
    forged = to_wire(capsule(sender=ALICE, receiver=ALICE, topic="rule.forged", trigger=Trigger.NONE))
    forged["id"] = f"urn:uuid:{uuid.uuid4()}"
    forged["semantics"]["claims"] = [{"statement": spec_json(RISK_WHEN, VERIFY_THEN), "type": "directive",
                                      "confidence": 1.0, "evidence": [], "valid_until": None}]
    env = sign(forged, mallory, pubkey_id=ALICE).to_wire()
    store.append(env["capsule"], envelope=env)
    store.put_opinion(f"rule:{forged['id']}", value=1.0, weight=0.0, evidence_count=0, last_tested=None)
    assert "forged" not in [r.name for r in engine.rules()]


def test_adopting_a_peers_rule_makes_it_ours_at_unknown():
    store, engine, sched = setup()
    bobs = RuleEngine(BOB, Store(str(Path(tempfile.mkdtemp()) / "bob.db")), Ed25519PrivateKey.generate())
    bob_rule = bobs.issue("verify-risk", RISK_WHEN, VERIFY_THEN, "Bob's rule")
    bob_capsule = bobs.store.find(capsule_id=bob_rule)[-1]["capsule"]
    for _ in range(3):                                     # Bob trusts his rule; that doesn't travel
        bobs.store.put_opinion(f"rule:{bob_rule}", value=1.9, weight=30, evidence_count=30,
                               last_tested=NOW.isoformat())
    mine = engine.adopt(bob_capsule)
    assert mine != bob_rule
    row = rule_row(store, mine)
    assert (row["value"], row["weight"], row["evidence_count"]) == (1.0, 0.0, 0)
    cap = store.find(capsule_id=mine)[-1]["capsule"]
    assert cap["from"] == ALICE and any(e.startswith("adopted_from:") for e in cap["semantics"]["claims"][0]["evidence"])
    assert len(fire(sched)) == 1


# --- learning from outcomes ---

def test_success_and_failure_move_the_rule_and_persist():
    store, engine, sched = setup()
    rid = engine.issue("verify-risk", RISK_WHEN, VERIFY_THEN)
    task = fire(sched)[0]
    assert [r.intent for r in sched.dispatch(result_for(task, "success"))] == [Intent.ACK]
    row = rule_row(store, rid)
    assert row["value"] > 1.0 and row["weight"] == 1.0 and row["evidence_count"] == 1

    task2 = fire(sched, capsule(sender=CAROL))[0]
    sched.dispatch(result_for(task2, "failure", sender=CAROL))
    row = Store(str(store.path)).get_opinion(f"rule:{rid}")
    assert row["value"] < 1.0 and row["weight"] > 3.9 and row["evidence_count"] == 2


def test_outcome_counts_once_and_only_from_the_agent_asked():
    store, engine, sched = setup()
    rid = engine.issue("verify-risk", RISK_WHEN, VERIFY_THEN)
    task = fire(sched)[0]                                   # sent to Bob
    assert [r.intent for r in sched.dispatch(result_for(task, "success", sender=CAROL))] == [Intent.REFUSE]
    assert rule_row(store, rid)["evidence_count"] == 0, "Carol can't report on Bob's task"

    sched.dispatch(result_for(task, "success"))
    sched.dispatch(result_for(task, "success"))             # a second, different report
    assert rule_row(store, rid)["evidence_count"] == 1

    unknown = result_for(capsule(), "success")               # answers something Alice never sent
    assert [r.intent for r in sched.dispatch(unknown)] == [Intent.REFUSE]


def test_distrusted_rule_stops_firing():
    store, engine, sched = setup()
    engine.issue("verify-risk", RISK_WHEN, VERIFY_THEN)
    fired = 0
    for _ in range(8):
        tasks = fire(sched)
        if not tasks:
            break
        fired += 1
        sched.dispatch(result_for(tasks[0], "failure"))
    # value 1.0 falls 0.2 per failure and firing stops at 0. The milliseconds between
    # failures decay the value slightly back toward unknown, so a sixth firing can happen.
    assert fired in (5, 6), fired
    status = engine.status()[0]
    assert status["value"] <= 0 and status["stance"] == "wary" and not status["fires"]


def test_task_result_needs_an_outcome_on_the_wire():
    wire = to_wire(result_for(capsule(), "success"))
    del wire["outcome"]
    expect(CapsuleRejected, lambda: ingest(wire), "no outcome")


# --- lifetime tied to Leighton Weight ---

def test_live_rules_are_refreshed_before_expiry():
    store, engine, _ = setup()
    rid = engine.issue("verify-risk", RISK_WHEN, VERIFY_THEN, now=NOW - timedelta(days=6))
    report = engine.maintain(now=NOW)
    assert report["refreshed"] == ["verify-risk"]
    assert len(store.find(capsule_id=rid)) == 2
    assert [r.id for r in engine.rules(now=NOW + timedelta(days=3))] == [rid], "still loaded after old copy expired"
    assert engine.maintain(now=NOW)["kept"] == ["verify-risk"]


def test_untested_rule_is_forgotten_after_grace_and_stays_forgotten():
    store, engine, sched = setup()
    rid = engine.issue("verify-risk", RISK_WHEN, VERIFY_THEN, now=NOW - UNTESTED_GRACE - timedelta(hours=1))
    assert engine.maintain(now=NOW)["forgotten"] == ["verify-risk"]
    assert rule_row(store, rid)["status"] == "forgotten"
    assert fire(sched) == []
    assert engine.issue("verify-risk", RISK_WHEN, VERIFY_THEN) is None, "restart doesn't revive it"
    assert engine.issue("verify-risk", RISK_WHEN, VERIFY_THEN, force=True) == rid
    assert rule_row(store, rid)["status"] == "active" and len(fire(sched)) == 1


def test_trusted_rule_lives_until_its_weight_decays():
    store, engine, sched = setup()
    rid = engine.issue("verify-risk", RISK_WHEN, VERIFY_THEN)
    sched.dispatch(result_for(fire(sched)[0], "success"))
    assert engine.maintain(now=NOW + timedelta(days=60))["forgotten"] == []
    far = NOW + timedelta(days=400)
    decayed = [r for r in engine.status(now=far) if r["id"] == rid][0]
    assert decayed["weight"] < FORGET_WEIGHT
    assert engine.maintain(now=far)["forgotten"] == ["verify-risk"]


def test_builtin_rules_load():
    from rules.__main__ import load_rule_file
    _, engine, sched = setup()
    names = [n for n, rid in load_rule_file(engine, str(Path(__file__).resolve().parent.parent / "rules" / "builtin.json"))]
    assert len(names) >= 4 and len(engine.rules()) == len(names)
    stuck = capsule(topic="motor", trigger=Trigger.STUCK)
    out = fire(sched, stuck)
    assert [o.receiver for o in out] == ["agent://operator"]
    temp = capsule(topic="temp", confidence=0.8)
    assert [o.receiver for o in fire(sched, temp)] == ["agent://cooler"]


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"{len(tests)} passed")
