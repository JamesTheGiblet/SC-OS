"""Upgrade a stripped edge capsule to a full capsule."""

import uuid
from datetime import datetime, timezone
from primitive import (
    Capsule, Semantics, Claim, ClaimType, Intent, Trigger, Provenance
)


def from_edge_wire(d: dict, *, sender: str, receiver: str) -> Capsule:
    """
    d is the stripped wire form: {v,id,to,i,t,c,s,tr}
    sender is filled by the gateway (ESP-NOW MAC -> agent id).
    """
    return Capsule(
        id=f"urn:uuid:{uuid.uuid4()}",
        created=datetime.now(timezone.utc),
        sender=sender,
        receiver=receiver,
        intent=Intent(d["i"]),
        trigger=Trigger(d["tr"]),
        semantics=Semantics(
            topic=d["t"],
            claims=(
                Claim(
                    statement=d["s"],
                    type=ClaimType.OBSERVATION,
                    confidence=float(d["c"]),
                    evidence=(f"edge_id={d['id']}",),
                ),
            ),
        ),
        provenance=Provenance(method="observation"),
    )


def to_edge_wire(c: Capsule, *, id_short: str) -> dict:
    """Downgrade a capsule for the ESP-NOW link."""
    first = c.semantics.claims[0] if c.semantics.claims else None
    return {
        "v": "1.0",
        "id": id_short[:16],
        "to": c.receiver.replace("agent://", "")[:16],
        "i": c.intent.value,
        "t": c.semantics.topic[:32],
        "c": round(first.confidence, 2) if first else 0.0,
        "s": (first.statement if first else "")[:120],
        "tr": c.trigger.value,
    }