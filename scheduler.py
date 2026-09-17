"""
Kernel loop. Capsule in, capsules out.
Routes by trigger, dispatches to agents, fires rules, records epistemic state.

Topic opinions live in the store's opinions table as "topic:<topic>", so they
survive restarts. Each is stored as of its last outcome and decayed on read.
"""

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Protocol
from primitive import Capsule, Intent, Trigger
from interpreter import actionable
from store import Store
from weight import Opinion

TOPIC_KEY = "topic:"

if TYPE_CHECKING:
    from rules.engine import RuleEngine


class Agent(Protocol):
    name: str
    def respond(self, c: Capsule, store: Store) -> tuple[Capsule, ...]: ...


class Scheduler:
    def __init__(self, agents: dict[str, Agent], store: Store,
                 rules: "RuleEngine | None" = None):
        self.agents = agents
        self.store = store
        self.rules = rules
        self.escalations: list[Capsule] = []
        self.task_results: list[Capsule] = []
        self.learned: list[str] = []              # what each counted outcome changed

    def dispatch(self, c: Capsule) -> list[Capsule]:
        """
        Store the incoming capsule, route it, fire rules on it, and store every
        capsule that produces: the store is a full ledger.
        """
        from interpreter import to_wire
        self.store.append(to_wire(c))

        # receipt is not evidence; see _handle_task_result()
        replies = self._route(c)
        if self.rules is not None:
            replies += self.rules.evaluate(c)
        for r in replies:
            self.store.append(to_wire(r))
        return replies

    def _route(self, c: Capsule) -> list[Capsule]:
        # trigger-based routing
        if c.trigger == Trigger.STUCK:
            self.escalations.append(c)
            return list(self._escalate(c))
        if c.trigger == Trigger.TASK_RESULT:
            self.task_results.append(c)
            return list(self._handle_task_result(c))
        if c.trigger == Trigger.THRESHOLD:
            return list(self._handle_threshold(c))
        if c.trigger == Trigger.HEARTBEAT:
            return []   # liveness only, don't act

        # normal agent dispatch
        if c.receiver not in self.agents:
            return [self._refuse(c, "no_such_agent")]
        if not actionable(c):
            return [self._ack(c)]

        agent = self.agents[c.receiver]
        return list(agent.respond(c, self.store))

    def record_outcome(
        self, topic: str, success: bool, now: datetime | None = None
    ) -> Opinion:
        """Evidence = an observed outcome of acting on a topic, not receipt."""
        now = now or datetime.now(timezone.utc)
        opinion = self.opinion(topic, now)       # decay to now, then learn
        opinion.observe(success=success, now=now)
        self.store.put_opinion(TOPIC_KEY + topic, value=opinion.value, weight=opinion.weight,
                               evidence_count=opinion.evidence_count,
                               last_tested=now.isoformat())
        return opinion

    def opinion(self, topic: str, now: datetime | None = None) -> Opinion:
        """This node's opinion of a topic, decayed to now. Unknown if never tested."""
        row = self.store.get_opinion(TOPIC_KEY + topic)
        if row is None:
            return Opinion()
        opinion = Opinion(value=row["value"], weight=row["weight"],
                          evidence_count=row["evidence_count"],
                          last_tested=datetime.fromisoformat(row["last_tested"])
                              if row["last_tested"] else None)
        opinion.tick(now=now)
        return opinion

    def opinions(self, now: datetime | None = None) -> dict[str, Opinion]:
        """Every topic this node has an opinion on, decayed to now."""
        return {key[len(TOPIC_KEY):]: self.opinion(key[len(TOPIC_KEY):], now)
                for key in self.store.opinions(TOPIC_KEY)}

    def _escalate(self, c: Capsule) -> tuple[Capsule, ...]:
        # placeholder: real system routes to an LLM/human agent
        return (self._ack(c),)

    def _handle_task_result(self, c: Capsule) -> tuple[Capsule, ...]:
        """
        An outcome is evidence only if it answers a task this node sent, and comes
        from the agent the task was sent to. Each task's outcome counts once.
        """
        task = self._task_answered_by(c)
        if task is None or c.outcome is None:
            return (self._refuse(c, "unknown_task"),)
        success = c.outcome.success
        if self.store.mark_outcome_counted(task["id"], success):
            topic = task["semantics"]["topic"]
            op = self.record_outcome(topic, success)
            self.learned.append(f"topic {topic}: {c.outcome.status} -> value={op.value:+.2f}")
            if self.rules is not None:
                learned = self.rules.record_rule_outcome(task, success)
                if learned:
                    rule_id, rop = learned
                    self.learned.append(f"rule {rule_id[-12:]}: {c.outcome.status} -> "
                                        f"value={rop.value:+.2f} weight={rop.weight:.2f} "
                                        f"stance={rop.stance}")
        return (self._ack(c),)

    def _task_answered_by(self, c: Capsule) -> dict | None:
        local = set(self.agents) | ({self.rules.owner} if self.rules is not None else set())
        for parent in c.provenance.derived_from:
            for rec in self.store.find(capsule_id=parent):
                task = rec["capsule"]
                if task["from"] in local and task["to"] == c.sender:
                    return task
        return None

    def _handle_threshold(self, c: Capsule) -> tuple[Capsule, ...]:
        return (self._ack(c),)

    def _ack(self, c: Capsule) -> Capsule:
        return self._reply(c, Intent.ACK, "acknowledged")

    def _refuse(self, c: Capsule, reason: str) -> Capsule:
        return self._reply(c, Intent.REFUSE, reason)

    def _reply(self, c: Capsule, intent: Intent, text: str) -> Capsule:
        from primitive import Capsule as Cap, Semantics, Claim, ClaimType, Provenance
        import uuid
        return Cap(
            id=f"urn:uuid:{uuid.uuid4()}",
            created=datetime.now(timezone.utc),
            sender=c.receiver,
            receiver=c.sender,
            intent=intent,
            semantics=Semantics(
                topic=c.semantics.topic,
                claims=(Claim(text, ClaimType.OBSERVATION, 1.0),),
            ),
            provenance=Provenance(derived_from=(c.id,), method="reply"),
        )