"""
Kernel loop. Capsule in, capsules out.
Routes by trigger, dispatches to agents, records epistemic state.
"""

from datetime import datetime, timezone
from typing import Protocol
from primitive import Capsule, Intent, Trigger
from interpreter import actionable
from store import Store
from weight import Opinion


class Agent(Protocol):
    name: str
    def respond(self, c: Capsule, store: Store) -> tuple[Capsule, ...]: ...


class Scheduler:
    def __init__(self, agents: dict[str, Agent], store: Store):
        self.agents = agents
        self.store = store
        self.opinions: dict[str, Opinion] = {}   # topic -> opinion
        self.escalations: list[Capsule] = []
        self.task_results: list[Capsule] = []

    def dispatch(self, c: Capsule) -> list[Capsule]:
        """Store the incoming capsule and every reply: the store is a full ledger."""
        from interpreter import to_wire
        self.store.append(to_wire(c))

        # receipt is not evidence; see record_outcome()
        replies = self._route(c)
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
        opinion = self.opinions.setdefault(topic, Opinion())
        opinion.observe(success=success, now=now)
        return opinion

    def _escalate(self, c: Capsule) -> tuple[Capsule, ...]:
        # placeholder: real system routes to an LLM/human agent
        return (self._ack(c),)

    def _handle_task_result(self, c: Capsule) -> tuple[Capsule, ...]:
        return (self._ack(c),)

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