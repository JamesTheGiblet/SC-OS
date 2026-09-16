"""
End-to-end demo: two nodes, handshake, sign, transmit, verify, ingest, merge.
Also exercises Leighton Weight trajectories and edge upgrade.
"""

import uuid
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, str(Path(__file__).resolve().parent))

from primitive import (
    Capsule, Semantics, Claim, ClaimType, Relation, Intent, Trigger,
    Provenance, ActionHints, Uncertainty,
)
from interpreter import to_wire, ingest, merge, render, actionable
from envelope import sign, verify, open_envelope, digest
from handshake import make_hello, negotiate
from store import Store
from scheduler import Scheduler
from agents.echo import EchoAgent
from boot.genesis import genesis
from edge.upgrade import from_edge_wire, to_edge_wire
from weight import Opinion

print("=" * 72)
print("SC-OS DEMO")
print("=" * 72)

# --- keys ---
alice_priv = Ed25519PrivateKey.generate()
bob_priv   = Ed25519PrivateKey.generate()
alice_pub  = alice_priv.public_key()
bob_pub    = bob_priv.public_key()

# --- boot ---
print("\n[1] Genesis")
alice_genesis = genesis("alice")
bob_genesis   = genesis("bob")
print(f"  alice: {render(alice_genesis)}")
print(f"  bob:   {render(bob_genesis)}")

# --- handshake ---
print("\n[2] Handshake")
alice_hello = make_hello("agent://alice", "agent://bob")
bob_hello   = make_hello("agent://bob",   "agent://alice")
agreed = negotiate(alice_hello, bob_hello)
print(f"  agreed capsule_version: {agreed['capsule_version']}")
print(f"  shared predicates: {sorted(agreed['predicates'])}")

# --- store ---
print("\n[3] Store")
demo_log = Path("./store/demo.log")
demo_log.unlink(missing_ok=True)   # demo only: start each run clean
store = Store(str(demo_log))
print(f"  store path: {store.path}")
print(f"  initial size: {len(store)}")

# --- alice builds a capsule ---
print("\n[4] Alice builds a capsule")
a = Capsule(
    id=f"urn:uuid:{uuid.uuid4()}",
    created=datetime.now(timezone.utc),
    sender="agent://alice",
    receiver="agent://bob",
    intent=Intent.INFORM,
    trigger=Trigger.THRESHOLD,
    semantics=Semantics(
        topic="supply_chain_risk",
        claims=(
            Claim(
                statement="Supplier X has 40% capacity reduction",
                type=ClaimType.OBSERVATION,
                confidence=0.82,
                evidence=("source:reuters-2026-09-14",),
                valid_until=datetime.now(timezone.utc) + timedelta(days=14),
            ),
        ),
        relations=(Relation("SupplierX", "affects", "ProductY"),),
        uncertainty=Uncertainty(
            known_unknowns=("recovery_timeline",),
            assumptions=("demand_stable",),
        ),
    ),
    provenance=Provenance(method="observation"),
    action_hints=ActionHints(priority="high", ttl_seconds=3600, requires_ack=True),
)
print(render(a))

# --- sign + transmit ---
print("\n[5] Sign + transmit")
env = sign(to_wire(a), alice_priv, pubkey_id="agent://alice")
wire = env.to_wire()
print(f"  digest: {digest(wire['capsule'])}")
print(f"  signature verifies: {verify(open_envelope(wire), alice_pub)}")
print(f"  signature verifies with wrong key: {verify(open_envelope(wire), bob_pub)}")

# --- bob receives ---
print("\n[6] Bob ingests")
b = ingest(wire["capsule"])
print(render(b))
print(f"  actionable: {actionable(b)}")

# --- store it ---
print("\n[7] Store append")
d = store.append(wire["capsule"])
print(f"  digest: {d}")
print(f"  retrieved matches: {store.get(d) == wire['capsule']}")

# --- merge ---
print("\n[8] Merge two capsules")
b2 = Capsule(
    id=f"urn:uuid:{uuid.uuid4()}",
    created=datetime.now(timezone.utc),
    sender="agent://bob",
    receiver="agent://alice",
    intent=Intent.INFORM,
    semantics=Semantics(
        topic="supply_chain_risk",
        claims=(
            Claim(
                statement="Supplier X has 40% capacity reduction",
                type=ClaimType.INFERENCE,
                confidence=0.91,
                evidence=("model:forecast-v3",),
            ),
            Claim(
                statement="Alternate supplier Z can absorb 15%",
                type=ClaimType.INFERENCE,
                confidence=0.64,
            ),
        ),
    ),
    provenance=Provenance(method="synthesis"),
)
merged = merge(b, b2)
print(render(merged))

# --- scheduler ---
print("\n[9] Scheduler dispatch")
sched = Scheduler(
    agents={"agent://bob": EchoAgent()},
    store=store,
)
replies = sched.dispatch(b)
for r in replies:
    print(f"  reply: {render(r)}")
print(f"  store size now: {len(store)}")

# --- epistemic ---
print("\n[10] Leighton Weight — epistemic state")
op = sched.opinions.get("supply_chain_risk", Opinion())
print(f"  after receipt only: value={op.value:+.3f} weight={op.weight:.3f} "
      f"n={op.evidence_count} stance={op.stance}")

# acting on the claim produced an observed outcome -- that is the evidence
op = sched.record_outcome("supply_chain_risk", success=True)
print(f"  after 1 outcome:    value={op.value:+.3f} weight={op.weight:.3f} "
      f"n={op.evidence_count} stance={op.stance}")

now = datetime.now(timezone.utc)
future = now + timedelta(days=30)
op.tick(now=future)
print(f"  30 days later: value={op.value:+.3f} weight={op.weight:.3f} "
      f"stance={op.stance}")

# --- edge bridge ---
print("\n[11] Edge upgrade (ESP32 -> master)")
edge_msg = {
    "v": "1.0",
    "id": "a1b2",
    "to": "master",
    "i": "inform",
    "t": "temp",
    "c": 0.8,
    "s": "sensor3 hot",
    "tr": "threshold",
}
print(f"  edge wire: {edge_msg}")
upgraded = from_edge_wire(edge_msg, sender="agent://bot7", receiver="agent://master")
print(f"  upgraded:  {render(upgraded)}")

downgraded = to_edge_wire(upgraded, id_short="a1b2")
print(f"  round-trip: {downgraded}")

print("\n" + "=" * 72)
print("DEMO COMPLETE")
print("=" * 72)