"""Cold start. The first capsule a node ever emits."""

import uuid
from datetime import datetime, timezone
from primitive import (
    Capsule, Semantics, Claim, ClaimType, Intent, Provenance, ActionHints
)


def genesis(node_id: str) -> Capsule:
    return Capsule(
        id=f"urn:uuid:{uuid.uuid4()}",
        created=datetime.now(timezone.utc),
        sender=f"agent://{node_id}",
        receiver=f"agent://{node_id}",
        intent=Intent.INFORM,
        semantics=Semantics(
            topic="__genesis__",
            claims=(
                Claim(
                    statement="node_identity",
                    type=ClaimType.OBSERVATION,
                    confidence=1.0,
                    evidence=(f"node_id={node_id}",),
                ),
                Claim(
                    statement="capabilities",
                    type=ClaimType.OBSERVATION,
                    confidence=1.0,
                    evidence=("storage=append_only", "transport=bus"),
                ),
            ),
        ),
        provenance=Provenance(method="observation"),
        action_hints=ActionHints(priority="high", ttl_seconds=604800),
    )