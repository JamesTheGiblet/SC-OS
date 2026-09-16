"""Upgrade a stripped edge capsule to a full capsule."""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from jsonschema import Draft202012Validator
from primitive import (
    Capsule, Semantics, Claim, ClaimType, Intent, Trigger, Provenance
)
from validator import CapsuleRejected

EDGE_SCHEMA = json.loads((Path(__file__).parent / "sc_edge.json").read_text())
_EDGE_V = Draft202012Validator(EDGE_SCHEMA)


def validate_edge(d: dict) -> None:
    errors = sorted(_EDGE_V.iter_errors(d), key=lambda e: list(e.path))
    if errors:
        e = errors[0]
        raise CapsuleRejected("edge_schema", f"{list(e.path)}: {e.message}")


def from_edge_wire(d: dict, *, sender: str, receiver: str) -> Capsule:
    """
    d is the stripped wire form: {v,id,to,i,t,c,s,tr}
    sender is filled by the gateway (ESP-NOW MAC -> agent id).
    """
    validate_edge(d)
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
    wire = {
        "v": "1.0",
        "id": id_short[:16],
        "to": c.receiver.replace("agent://", "")[:16],
        "i": c.intent.value,
        "t": c.semantics.topic[:32],
        "c": round(first.confidence, 2) if first else 0.0,
        "s": (first.statement if first else "")[:120],
        "tr": c.trigger.value,
    }
    validate_edge(wire)
    return wire