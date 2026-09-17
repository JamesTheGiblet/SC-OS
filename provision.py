"""
Operator setup for edge devices: margins and sample rates, as __setup__ capsules.

    python provision.py issue setup/m5-96c048.json [--node alice]
    python provision.py show  [--node alice]

A setup file names one device and, per sensor, the fields to set:

    {"device": "m5-96c048",
     "sensors": {"tilt": {"margin_high": 30}, "battery": {"margin_low": 3.5, "margin_high": 4.3}}}

`issue` checks the file against the device's latest __sensors__ description
(known sensors, margins inside the physical range, margin_low <= margin_high),
then signs and stores a __setup__ capsule as the node: one directive claim
"setup:<id>" per sensor, key=value evidence per field. A newer setup replaces the
older one, so list every margin you want set. Pins can't be set: on the M5 they
are wired.

Delivery (SetupKeeper, an observer on the master): whenever the device's readings
or description arrive and its description doesn't match the latest setup yet,
the master sends the device a copy of the setup as a task (method relay,
derived_from the issued setup), at most once per RESEND_SECONDS. The device
applies it, reports the outcome as a task_result, and describes itself again;
a matching description ends the resending.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from edge.sensors import READINGS_TOPIC, TOPIC as SENSORS_TOPIC, parse_evidence  # noqa: E402
from envelope import sign  # noqa: E402
from interpreter import to_wire  # noqa: E402
from primitive import (ActionHints, Capsule, Claim, ClaimType, Intent, Provenance,  # noqa: E402
                       Semantics, Trigger)
from validator import CapsuleRejected, validate  # noqa: E402

TOPIC = "__setup__"
SETTABLE = ("margin_low", "margin_high", "sample_ms")
TTL_SECONDS = 7 * 86400            # the schema maximum
RESEND_SECONDS = 60


# --- setup files ---------------------------------------------------------------

def check_setup(setup: dict, description: dict | None) -> None:
    """
    Raise CapsuleRejected("setup", ...) if the setup can't apply. description is
    {sensor id: parsed __sensors__ evidence}, or None if the device hasn't described itself.
    """
    def bad(msg):
        raise CapsuleRejected("setup", msg)

    if not isinstance(setup, dict) or not isinstance(setup.get("device"), str) or not setup["device"]:
        bad("setup needs a device name")
    sensors = setup.get("sensors")
    if not isinstance(sensors, dict) or not sensors:
        bad("setup needs at least one sensor")
    for sid, fields in sensors.items():
        if not isinstance(fields, dict) or not fields:
            bad(f"{sid}: give at least one of {', '.join(SETTABLE)}")
        for k, v in fields.items():
            if k not in SETTABLE:
                bad(f"{sid}: {k} can't be set (settable: {', '.join(SETTABLE)})")
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                bad(f"{sid}.{k} must be a number")
        if "sample_ms" in fields and (fields["sample_ms"] < 20 or fields["sample_ms"] != int(fields["sample_ms"])):
            bad(f"{sid}.sample_ms must be a whole number of at least 20")
        if description is None:
            continue
        d = description.get(sid)
        if d is None:
            bad(f"{setup['device']} has no sensor {sid!r} (it has {', '.join(sorted(description))})")
        low = fields.get("margin_low", d["margin_low"])
        high = fields.get("margin_high", d["margin_high"])
        if not d["min"] <= low <= high <= d["max"]:
            bad(f"{sid}: need {d['min']:g} <= margin_low ({low:g}) <= margin_high ({high:g}) <= {d['max']:g}")


def _value(v) -> str:
    return f"{v:g}" if isinstance(v, float) else str(v)


def setup_capsule(setup: dict, *, sender: str, now: datetime | None = None,
                  derived_from: tuple[str, ...] = (), method: str = "synthesis") -> Capsule:
    now = now or datetime.now(timezone.utc)
    claims = tuple(
        Claim(statement=f"setup:{sid}", type=ClaimType.DIRECTIVE, confidence=1.0,
              evidence=tuple(f"{k}={_value(fields[k])}" for k in SETTABLE if k in fields))
        for sid, fields in sorted(setup["sensors"].items())
    )
    return Capsule(
        id=f"urn:uuid:{uuid.uuid4()}",
        created=now,
        sender=sender,
        receiver=f"agent://{setup['device']}",
        intent=Intent.REQUEST,
        trigger=Trigger.TASK,
        semantics=Semantics(topic=TOPIC, claims=claims),
        provenance=Provenance(derived_from=derived_from, method=method),
        action_hints=ActionHints(priority="high", ttl_seconds=TTL_SECONDS, requires_ack=True),
    )


def parse_setup(capsule: dict) -> dict:
    """A __setup__ capsule (wire form) back to {sensor id: {field: number}}."""
    out = {}
    for claim in capsule.get("semantics", {}).get("claims", []):
        sid = claim.get("statement", "").removeprefix("setup:")
        fields = {}
        for item in claim.get("evidence", []):
            k, _, v = item.partition("=")
            if k in SETTABLE:
                fields[k] = int(v) if k == "sample_ms" else float(v)
        out[sid] = fields
    return out


def latest_description(store, agent: str) -> dict | None:
    records = store.find(topic=SENSORS_TOPIC, sender=agent)
    if not records:
        return None
    latest = max(records, key=lambda r: r["capsule"]["created"])
    return {parse_evidence(cl)["id"]: parse_evidence(cl) for cl in latest["capsule"]["semantics"]["claims"]}


def latest_setup(store, owner: str, agent: str, now: datetime | None = None) -> dict | None:
    """The newest setup the node issued for a device (not the copies it sent), unexpired."""
    now = now or datetime.now(timezone.utc)
    issued = [r["capsule"] for r in store.find(topic=TOPIC, sender=owner, receiver=agent, unexpired_at=now)
              if r["capsule"]["provenance"]["method"] == "synthesis"]
    return max(issued, key=lambda c: c["created"]) if issued else None


def mismatches(desired: dict, description: dict | None) -> list[str]:
    """Fields the device's description doesn't show yet, as "sensor.field"."""
    if description is None:
        return [f"{sid}.{k}" for sid, fields in desired.items() for k in fields]
    out = []
    for sid, fields in desired.items():
        applied = description.get(sid, {})
        for k, v in fields.items():
            if k not in applied or abs(float(applied[k]) - float(v)) > 1e-9:
                out.append(f"{sid}.{k}")
    return out


def issue(store, key, owner: str, setup: dict, now: datetime | None = None) -> Capsule:
    """Check, sign and store a setup as the node. Returns the issued capsule."""
    check_setup(setup, latest_description(store, f"agent://{setup.get('device')}"))
    c = setup_capsule(setup, sender=owner, now=now)
    wire = to_wire(c)
    validate(wire, now=c.created)
    env = sign(wire, key, pubkey_id=owner).to_wire()
    store.append(env["capsule"], envelope=env)
    return c


# --- delivery ----------------------------------------------------------------

class SetupKeeper:
    """Observer on the master: sends a device its latest setup until its description matches."""

    def __init__(self, store, owner: str):
        self.store = store
        self.owner = owner
        self.last_sent: dict[str, datetime] = {}       # device agent -> when a copy was last sent

    def observe(self, capsule: dict, scheduler) -> list[str]:
        return []

    def respond(self, capsule: dict, scheduler, now: datetime | None = None) -> list[Capsule]:
        topic = capsule.get("semantics", {}).get("topic")
        if topic not in (READINGS_TOPIC, SENSORS_TOPIC):
            return []
        agent = capsule["from"]
        now = now or datetime.now(timezone.utc)
        issued = latest_setup(self.store, self.owner, agent, now)
        if issued is None:
            return []
        desired = parse_setup(issued)
        missing = mismatches(desired, latest_description(self.store, agent))
        if not missing:
            return []
        sent = self.last_sent.get(agent)
        if sent is not None and now - sent < timedelta(seconds=RESEND_SECONDS):
            return []
        self.last_sent[agent] = now
        copy = setup_capsule({"device": agent.removeprefix("agent://"), "sensors": desired},
                             sender=self.owner, now=now, derived_from=(issued["id"],), method="relay")
        scheduler.learned.append(f"setup for {agent}: sending ({', '.join(missing)} not applied yet)")
        return [copy]


# --- command line --------------------------------------------------------------

def main() -> int:
    from peer import load_or_create_key
    from store import Store

    ap = argparse.ArgumentParser(description="Issue operator setups for edge devices.")
    ap.add_argument("command", choices=["issue", "show"])
    ap.add_argument("file", nargs="?", help="setup file for issue")
    ap.add_argument("--node", default="alice")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    owner = f"agent://{args.node}"
    store = Store(str(ROOT / "store" / f"{args.node}.db"))
    if args.command == "issue":
        if not args.file:
            ap.error("issue needs a setup file")
        try:
            setup = json.loads(Path(args.file).read_text(encoding="utf-8"))
            c = issue(store, load_or_create_key(owner, str(ROOT / "keys")), owner, setup)
        except (OSError, json.JSONDecodeError) as e:
            print(f"FAIL cannot read {args.file}: {e}", file=sys.stderr)
            return 1
        except CapsuleRejected as e:
            print(f"REJECT {e.detail}", file=sys.stderr)
            return 1
        if latest_description(store, c.receiver) is None:
            print(f"  note: {c.receiver} hasn't described its sensors yet, so ranges weren't checked")
        print(f"  issued setup {c.id[-12:]} for {c.receiver}; it is sent when the device next reports")

    devices = sorted({r["capsule"]["to"] for r in store.find(topic=TOPIC, sender=owner)})
    for agent in devices:
        issued = latest_setup(store, owner, agent)
        if issued is None:
            continue
        missing = mismatches(parse_setup(issued), latest_description(store, agent))
        state = "applied" if not missing else f"waiting ({', '.join(missing)})"
        print(f"  {agent:26} setup {issued['id'][-12:]}  {state}")
        for sid, fields in parse_setup(issued).items():
            print(f"      {sid:10} " + "  ".join(f"{k}={_value(v)}" for k, v in fields.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
