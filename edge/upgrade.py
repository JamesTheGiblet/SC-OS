"""
Upgrade a stripped edge capsule to a full capsule, and downgrade back.

Stripped form: {v, id, to, i, t, c, s, tr} plus optional
  re  the short id of the message this one answers
  o   outcome, "success" | "failure" (required, with re, on task_result; nowhere else)
  od  outcome detail
"""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from jsonschema import Draft202012Validator
from primitive import (
    Capsule, Semantics, Claim, ClaimType, Intent, Outcome, Trigger, Provenance
)
from validator import CapsuleRejected

EDGE_SCHEMA = json.loads((Path(__file__).parent / "sc_edge.json").read_text())
_EDGE_V = Draft202012Validator(EDGE_SCHEMA)


def validate_edge(d: dict) -> None:
    errors = sorted(_EDGE_V.iter_errors(d), key=lambda e: list(e.path))
    if errors:
        e = errors[0]
        raise CapsuleRejected("edge_schema", f"{list(e.path)}: {e.message}")


def from_edge_wire(d: dict, *, sender: str, receiver: str,
                   derived_from: tuple[str, ...] = ()) -> Capsule:
    """
    d is the stripped wire form. sender is filled by the gateway (link address ->
    agent id). derived_from holds full capsule ids the gateway resolved from short
    ones; a task_result must have the task it answers there.
    """
    validate_edge(d)
    if d["tr"] == "task_result" and not derived_from:
        raise CapsuleRejected("edge_unknown_task", f"no task known for re={d.get('re')!r}")
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
        provenance=Provenance(derived_from=tuple(derived_from), method="observation"),
        outcome=Outcome(d["o"], d.get("od", "")) if "o" in d else None,
    )


def to_edge_wire(c: Capsule, *, id_short: str, re: str | None = None) -> dict:
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
    if re:
        wire["re"] = re[:16]
    if c.outcome is not None:
        wire["o"] = c.outcome.status
        if c.outcome.detail:
            wire["od"] = c.outcome.detail[:60]
    validate_edge(wire)
    return wire