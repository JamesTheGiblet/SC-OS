"""Echo agent: replies to any request with its own statement."""

import uuid
from datetime import datetime, timezone
from primitive import Capsule, Semantics, Claim, ClaimType, Intent


class EchoAgent:
    name = "echo"

    def respond(self, c: Capsule, store) -> tuple[Capsule, ...]:
        return (
            Capsule(
                id=f"urn:uuid:{uuid.uuid4()}",
                created=datetime.now(timezone.utc),
                sender=c.receiver,
                receiver=c.sender,
                intent=Intent.CONFIRM,
                semantics=Semantics(
                    topic=c.semantics.topic,
                    claims=(
                        Claim(
                            statement=f"echo: {c.semantics.claims[0].statement}"
                            if c.semantics.claims else "echo: (empty)",
                            type=ClaimType.OBSERVATION,
                            confidence=1.0,
                        ),
                    ),
                ),
            ),
        )