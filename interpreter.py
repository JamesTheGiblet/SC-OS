import json, uuid
from datetime import datetime, timezone
from primitive import (
    Capsule, Semantics, Claim, ClaimType, Relation,
    Uncertainty, Provenance, ActionHints, Epistemic, Intent, Trigger,
)
from validator import validate


def to_wire(c: Capsule) -> dict:
    return {
        "capsule_version": c.capsule_version,
        "id": c.id,
        "created": c.created.isoformat(),
        "from": c.sender,
        "to": c.receiver,
        "intent": c.intent.value,
        "trigger": c.trigger.value,
        "semantics": {
            "topic": c.semantics.topic,
            "claims": [
                {
                    "statement": cl.statement,
                    "type": cl.type.value,
                    "confidence": cl.confidence,
                    "evidence": list(cl.evidence),
                    "valid_until": cl.valid_until.isoformat() if cl.valid_until else None,
                } for cl in c.semantics.claims
            ],
            "relations": [
                {"subject": r.subject, "predicate": r.predicate, "object": r.object}
                for r in c.semantics.relations
            ],
            "uncertainty": {
                "known_unknowns": list(c.semantics.uncertainty.known_unknowns),
                "assumptions": list(c.semantics.uncertainty.assumptions),
            },
        },
        "provenance": {
            "derived_from": list(c.provenance.derived_from),
            "method": c.provenance.method,
            "signature": c.provenance.signature,
        },
        "action_hints": {
            "priority": c.action_hints.priority,
            "ttl_seconds": c.action_hints.ttl_seconds,
            "requires_ack": c.action_hints.requires_ack,
        },
        "epistemic": {
            "value": c.epistemic.value,
            "weight": c.epistemic.weight,
            "evidence_count": c.epistemic.evidence_count,
            "last_tested": c.epistemic.last_tested.isoformat()
                if c.epistemic.last_tested else None,
        },
    }


def from_wire(d: dict) -> Capsule:
    s = d["semantics"]
    ep = d.get("epistemic", {})
    return Capsule(
        id=d["id"],
        created=datetime.fromisoformat(d["created"]),
        sender=d["from"],
        receiver=d["to"],
        intent=Intent(d["intent"]),
        trigger=Trigger(d.get("trigger", "none")),
        semantics=Semantics(
            topic=s["topic"],
            claims=tuple(
                Claim(
                    statement=cl["statement"],
                    type=ClaimType(cl["type"]),
                    confidence=cl["confidence"],
                    evidence=tuple(cl.get("evidence", ())),
                    valid_until=datetime.fromisoformat(cl["valid_until"])
                        if cl.get("valid_until") else None,
                ) for cl in s.get("claims", ())
            ),
            relations=tuple(Relation(**r) for r in s.get("relations", ())),
            uncertainty=Uncertainty(
                known_unknowns=tuple(s.get("uncertainty", {}).get("known_unknowns", ())),
                assumptions=tuple(s.get("uncertainty", {}).get("assumptions", ())),
            ),
        ),
        provenance=Provenance(
            derived_from=tuple(d.get("provenance", {}).get("derived_from", ())),
            method=d.get("provenance", {}).get("method", "synthesis"),
            signature=d.get("provenance", {}).get("signature"),
        ),
        action_hints=ActionHints(**d.get("action_hints", {})),
        epistemic=Epistemic(
            value=ep.get("value", 1.0),
            weight=ep.get("weight", 0.0),
            evidence_count=ep.get("evidence_count", 0),
            last_tested=datetime.fromisoformat(ep["last_tested"])
                if ep.get("last_tested") else None,
        ),
        capsule_version=d["capsule_version"],
    )


def ingest(payload: dict) -> Capsule:
    validate(payload)
    return from_wire(payload)


def is_expired(c: Capsule, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    if (now - c.created).total_seconds() > c.action_hints.ttl_seconds:
        return True
    return any(cl.valid_until and cl.valid_until < now for cl in c.semantics.claims)


def actionable(c: Capsule) -> bool:
    return (
        c.intent in (Intent.REQUEST, Intent.QUERY)
        and not is_expired(c)
        and c.action_hints.priority in ("high", "normal")
    )


def merge(a: Capsule, b: Capsule) -> Capsule:
    if a.semantics.topic != b.semantics.topic:
        raise ValueError("cannot merge across topics")
    seen: dict[str, Claim] = {}
    for cl in (*a.semantics.claims, *b.semantics.claims):
        prev = seen.get(cl.statement)
        if prev is None or cl.confidence > prev.confidence:
            seen[cl.statement] = cl
    return Capsule(
        id=f"urn:uuid:{uuid.uuid4()}",
        created=datetime.now(timezone.utc),
        sender=f"{a.sender}+{b.sender}",
        receiver=a.receiver,
        intent=Intent.INFORM,
        trigger=a.trigger if a.trigger != Trigger.NONE else b.trigger,
        semantics=Semantics(
            topic=a.semantics.topic,
            claims=tuple(seen.values()),
            relations=a.semantics.relations + b.semantics.relations,
            uncertainty=Uncertainty(
                known_unknowns=a.semantics.uncertainty.known_unknowns
                             + b.semantics.uncertainty.known_unknowns,
                assumptions=a.semantics.uncertainty.assumptions
                          + b.semantics.uncertainty.assumptions,
            ),
        ),
        provenance=Provenance(derived_from=(a.id, b.id), method="merge"),
    )


def render(c: Capsule) -> str:
    lines = [f"[{c.intent.value.upper()}] {c.semantics.topic} "
             f"({c.sender} -> {c.receiver})  trigger={c.trigger.value}"]
    for cl in c.semantics.claims:
        lines.append(f"  • ({cl.confidence:.2f}) {cl.statement}")
    for r in c.semantics.relations:
        lines.append(f"  → {r.subject} {r.predicate} {r.object}")
    if c.semantics.uncertainty.known_unknowns:
        lines.append(f"  ? unknowns: {', '.join(c.semantics.uncertainty.known_unknowns)}")
    lines.append(f"  epistemic: value={c.epistemic.value:+.2f} "
                 f"weight={c.epistemic.weight:.2f} n={c.epistemic.evidence_count}")
    return "\n".join(lines)