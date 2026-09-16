import uuid
from datetime import datetime, timezone
from primitive import (
    Capsule, Semantics, Claim, ClaimType, Intent, Provenance, ActionHints
)
from validator import SUPPORTED_VERSIONS, SUPPORTED_VOCAB_VERSION, SUPPORTED_PREDICATES

TOPIC = "__handshake__"


def make_hello(sender: str, receiver: str) -> Capsule:
    return Capsule(
        id=f"urn:uuid:{uuid.uuid4()}",
        created=datetime.now(timezone.utc),
        sender=sender,
        receiver=receiver,
        intent=Intent.INFORM,
        semantics=Semantics(
            topic=TOPIC,
            claims=(
                Claim(
                    statement="handshake",
                    type=ClaimType.OBSERVATION,
                    confidence=1.0,
                    evidence=(
                        f"capsule_versions={','.join(sorted(SUPPORTED_VERSIONS))}",
                        f"vocab_version={SUPPORTED_VOCAB_VERSION}",
                        f"predicates={','.join(sorted(SUPPORTED_PREDICATES))}",
                    ),
                ),
            ),
        ),
        provenance=Provenance(method="observation"),
        action_hints=ActionHints(priority="high", ttl_seconds=300, requires_ack=True),
    )


def _extract(c: Capsule, prefix: str) -> set[str]:
    for cl in c.semantics.claims:
        for e in cl.evidence:
            if e.startswith(prefix):
                return set(e.split("=", 1)[1].split(","))
    return set()


def negotiate(local: Capsule, remote: Capsule) -> dict:
    shared_versions = _extract(local, "capsule_versions=") & _extract(remote, "capsule_versions=")
    shared_predicates = _extract(local, "predicates=") & _extract(remote, "predicates=")

    if not shared_versions:
        raise RuntimeError("no shared capsule version")
    if not shared_predicates:
        raise RuntimeError("no shared vocabulary")

    return {
        "capsule_version": sorted(shared_versions)[-1],
        "predicates": shared_predicates,
    }