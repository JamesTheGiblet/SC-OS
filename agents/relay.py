"""Relay agent: forwards capsules to a configured next hop."""

import uuid
from datetime import datetime, timezone
from primitive import Capsule, Semantics, Claim, ClaimType, Intent, Provenance


class RelayAgent:
    name = "relay"

    def __init__(self, next_hop: str):
        self.next_hop = next_hop

    def respond(self, c: Capsule, store) -> tuple[Capsule, ...]:
        return (
            Capsule(
                id=f"urn:uuid:{uuid.uuid4()}",
                created=datetime.now(timezone.utc),
                sender=c.receiver,
                receiver=self.next_hop,
                intent=Intent.INFORM,
                semantics=c.semantics,
                provenance=Provenance(derived_from=(c.id,), method="relay"),
            ),
        )