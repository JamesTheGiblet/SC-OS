"""
Scheduler: routing by trigger and receiver, the ledger, replies, learning only
from verified outcomes, and topic opinions that survive a restart.
Run from the project root: python tests/test_scheduler.py
"""

import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from math import exp, isclose
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.echo import EchoAgent
from envelope import digest
from interpreter import to_wire
from primitive import (
    ActionHints, Capsule, Claim, ClaimType, Intent, Outcome, Provenance, Semantics, Trigger,
)
from scheduler import Scheduler
from store import Store
from validator import validate
from weight import UNKNOWN, decay_constant

ALICE, BOB, CAROL = "agent://alice", "agent://bob", "agent://carol"


def setup(path: Path | None = None) -> tuple[Store, Scheduler]:
    store = Store(str(path or Path(tempfile.mkdtemp()) / "alice.db"))
    return store, Scheduler({ALICE: EchoAgent()}, store)


def capsule(sender=BOB, receiver=ALICE, intent=Intent.INFORM, trigger=Trigger.NONE,
            topic="supply_chain_risk", claim_type=ClaimType.OBSERVATION, **kw) -> Capsule:
    return Capsule(id=f"urn:uuid:{uuid.uuid4()}", created=kw.pop("created", datetime.now(timezone.utc)),
                   sender=sender, receiver=receiver, intent=intent, trigger=trigger,
                   semantics=Semantics(topic, claims=(Claim("Supplier X down 40%", claim_type, 0.82),)),
                   **kw)


def sent_task(store: Store, to: str = BOB, topic: str = "supply_chain_risk") -> Capsule:
    """A task this node (Alice) sent, recorded in her ledger as Peer.send would."""
    t = capsule(sender=ALICE, receiver=to, intent=Intent.REQUEST, trigger=Trigger.TASK,
                topic=topic, claim_type=ClaimType.DIRECTIVE)
    store.append(to_wire(t))
    return t


def result_for(task: Capsule, status: str = "success", sender: str = BOB) -> Capsule:
    return capsule(sender=sender, trigger=Trigger.TASK_RESULT, topic=task.semantics.topic,
                   provenance=Provenance((task.id,), "observation"), outcome=Outcome(status))


def only(replies: list[Capsule]) -> Capsule:
    assert len(replies) == 1, replies
    return replies[0]


# --- routing ----------------------------------------------------------------

def test_inform_is_acked():
    _, sched = setup()
    r = only(sched.dispatch(capsule()))
    assert r.intent == Intent.ACK


def test_request_goes_to_the_agent():
    _, sched = setup()
    c = capsule(intent=Intent.REQUEST)
    r = only(sched.dispatch(c))
    assert r.intent == Intent.CONFIRM and r.receiver == BOB
    assert r.semantics.claims[0].statement == "echo: Supplier X down 40%"


def test_expired_or_low_priority_request_is_only_acked():
    _, sched = setup()
    old = capsule(intent=Intent.REQUEST, created=datetime.now(timezone.utc) - timedelta(hours=2))
    low = capsule(intent=Intent.REQUEST, action_hints=ActionHints(priority="low"))
    assert only(sched.dispatch(old)).intent == Intent.ACK
    assert only(sched.dispatch(low)).intent == Intent.ACK


def test_unknown_receiver_is_refused():
    _, sched = setup()
    r = only(sched.dispatch(capsule(receiver=CAROL)))
    assert r.intent == Intent.REFUSE and r.semantics.claims[0].statement == "no_such_agent"


def test_trigger_routing():
    _, sched = setup()
    stuck = capsule(trigger=Trigger.STUCK)
    assert only(sched.dispatch(stuck)).intent == Intent.ACK
    assert sched.escalations == [stuck]
    assert only(sched.dispatch(capsule(trigger=Trigger.THRESHOLD))).intent == Intent.ACK
    assert sched.dispatch(capsule(trigger=Trigger.HEARTBEAT)) == []
    # trigger routing comes before receiver checks: a stuck capsule for anyone is escalated
    assert only(sched.dispatch(capsule(receiver=CAROL, trigger=Trigger.STUCK))).intent == Intent.ACK


def test_replies_are_valid_and_derive_from_their_parent():
    _, sched = setup()
    c = capsule()
    r = only(sched.dispatch(c))
    assert r.sender == ALICE and r.receiver == BOB
    assert r.semantics.topic == c.semantics.topic
    assert r.provenance.derived_from == (c.id,) and r.provenance.method == "reply"
    validate(to_wire(r))


def test_ledger_holds_incoming_and_replies():
    store, sched = setup()
    c = capsule(intent=Intent.REQUEST)
    replies = sched.dispatch(c)
    assert store.has(digest(to_wire(c)))
    for r in replies:
        assert store.has(digest(to_wire(r)))
    assert len(store) == 1 + len(replies)
    assert sched.dispatch(capsule(trigger=Trigger.HEARTBEAT)) == []
    assert len(store) == 2 + len(replies)          # heartbeats are recorded too


# --- learning ---------------------------------------------------------------

def test_receipt_is_not_evidence():
    _, sched = setup()
    for _ in range(5):
        sched.dispatch(capsule(trigger=Trigger.THRESHOLD))
        sched.dispatch(capsule(intent=Intent.CONFIRM))
    assert sched.opinions() == {}
    op = sched.opinion("supply_chain_risk")
    assert (op.value, op.weight, op.evidence_count) == (UNKNOWN, 0.0, 0)


def test_verified_outcome_moves_the_topic_opinion():
    store, sched = setup()
    task = sent_task(store)
    r = only(sched.dispatch(result_for(task, "failure")))
    assert r.intent == Intent.ACK
    op = sched.opinion("supply_chain_risk")
    assert op.evidence_count == 1 and isclose(op.value, 0.8, abs_tol=1e-6)
    assert sched.learned and "failure" in sched.learned[0]


def test_outcome_counts_once_per_task():
    store, sched = setup()
    task = sent_task(store)
    sched.dispatch(result_for(task, "success"))
    again = only(sched.dispatch(result_for(task, "failure")))
    assert again.intent == Intent.ACK                  # answered, but not counted
    assert sched.opinion("supply_chain_risk").evidence_count == 1
    assert len(sched.learned) == 1


def test_outcomes_that_are_not_evidence_are_refused():
    store, sched = setup()
    task = sent_task(store, to=BOB)
    cases = {
        "not our task": result_for(capsule(sender=CAROL, receiver=BOB)),
        "from the wrong agent": result_for(task, sender=CAROL),
        "task never stored": result_for(capsule(sender=ALICE, receiver=BOB, intent=Intent.REQUEST)),
    }
    for label, c in cases.items():
        r = only(sched.dispatch(c))
        assert r.intent == Intent.REFUSE, label
        assert r.semantics.claims[0].statement == "unknown_task", label
    assert sched.opinions() == {}


def test_topic_opinions_survive_a_restart():
    path = Path(tempfile.mkdtemp()) / "alice.db"
    store, sched = setup(path)
    sched.dispatch(result_for(sent_task(store), "success"))
    sched.dispatch(result_for(sent_task(store), "success"))
    before = sched.opinion("supply_chain_risk")
    store.close()

    store2, sched2 = setup(path)
    after = sched2.opinion("supply_chain_risk")
    assert after.evidence_count == 2
    assert isclose(after.value, before.value, abs_tol=1e-4)
    assert set(sched2.opinions()) == {"supply_chain_risk"}
    # the task's outcome was counted before the restart, so a repeat still doesn't count
    task = sent_task(store2)
    sched2.dispatch(result_for(task))
    sched2.dispatch(result_for(task))
    assert sched2.opinion("supply_chain_risk").evidence_count == 3


def test_record_outcome_decays_first_and_reads_decay():
    _, sched = setup()
    t0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
    sched.record_outcome("sensor:temp1", True, now=t0)
    sched.record_outcome("sensor:temp1", True, now=t0)       # weight 2, n=2
    k = decay_constant(0.05, 2)
    later = sched.opinion("sensor:temp1", now=t0 + timedelta(days=30))
    assert isclose(later.weight, 2.0 * exp(-k * 30))
    op = sched.record_outcome("sensor:temp1", False, now=t0 + timedelta(days=30))
    assert isclose(op.weight, 2.0 * exp(-k * 30) + 3.0)
    assert op.evidence_count == 3
    # reading twice at the same time doesn't decay twice
    t = t0 + timedelta(days=60)
    assert isclose(sched.opinion("sensor:temp1", now=t).weight, sched.opinion("sensor:temp1", now=t).weight)


def test_topic_and_rule_opinions_do_not_mix():
    store, sched = setup()
    store.put_opinion("rule:urn:uuid:1", value=2.0, weight=9.0, evidence_count=9, last_tested=None)
    sched.record_outcome("urn:uuid:1", False)                # a topic with the same name
    assert store.get_opinion("rule:urn:uuid:1")["value"] == 2.0
    assert set(sched.opinions()) == {"urn:uuid:1"}           # rule opinions aren't topics


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"{len(tests)} passed")
