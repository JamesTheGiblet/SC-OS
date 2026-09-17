"""
Sensor descriptions from edge devices.

A device can't send a full __sensors__ capsule: the stripped format carries one
short claim and no evidence. It sends a compact list instead, and the gateway,
which is the trust boundary, checks it and builds the capsule:

    device -> gateway   {"src": "m5-96c048",
                         "sensors": [{"id": "imu_temp", "type": "temperature", "bus": "i2c0:0x68",
                                      "pin": "21,22", "unit": "C", "min": -40, "max": 85,
                                      "margin_low": 0, "margin_high": 60, "sample_ms": 200}, ...],
                         "absent": ["mic: PDM input not supported"]}

The capsule has topic __sensors__, one observation claim per sensor with the
statement "sensor:<id>" (the key its opinion will use), and one evidence string
per field, key=value. "absent" lists hardware the device has but can't read; it
becomes known unknowns.

Margins are operating bounds, inside the physical min/max. For now the device's
firmware supplies them; a __setup__ capsule from the operator is meant to later.
"""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from jsonschema import Draft202012Validator

from primitive import ActionHints, Capsule, Claim, ClaimType, Intent, Provenance, Semantics, Trigger, Uncertainty
from validator import CapsuleRejected

TOPIC = "__sensors__"
FIELDS = ("id", "type", "bus", "pin", "unit", "min", "max", "margin_low", "margin_high", "sample_ms")
TTL_SECONDS = 86400

SCHEMA = json.loads((Path(__file__).parent / "sc_sensors.json").read_text())
_V = Draft202012Validator(SCHEMA)


def validate_sensor_list(frame: dict) -> None:
    errors = sorted(_V.iter_errors(frame), key=lambda e: list(e.path))
    if errors:
        e = errors[0]
        raise CapsuleRejected("edge_sensors", f"{list(e.path)}: {e.message}")
    seen = set()
    for s in frame["sensors"]:
        if s["id"] in seen:
            raise CapsuleRejected("edge_sensors", f"sensor id {s['id']!r} appears twice")
        seen.add(s["id"])
        if not s["min"] <= s["margin_low"] <= s["margin_high"] <= s["max"]:
            raise CapsuleRejected("edge_sensors", f"{s['id']}: need min <= margin_low <= margin_high <= max")


def _value(v) -> str:
    return f"{v:g}" if isinstance(v, float) else str(v)


def sensors_capsule(frame: dict, *, sender: str, receiver: str) -> Capsule:
    """The __sensors__ capsule for a checked sensor list."""
    validate_sensor_list(frame)
    claims = tuple(
        Claim(
            statement=f"sensor:{s['id']}",
            type=ClaimType.OBSERVATION,
            confidence=1.0,
            evidence=tuple(f"{k}={_value(s[k])}" for k in FIELDS),
        )
        for s in frame["sensors"]
    )
    return Capsule(
        id=f"urn:uuid:{uuid.uuid4()}",
        created=datetime.now(timezone.utc),
        sender=sender,
        receiver=receiver,
        intent=Intent.INFORM,
        trigger=Trigger.ANNOUNCE,
        semantics=Semantics(
            topic=TOPIC,
            claims=claims,
            uncertainty=Uncertainty(known_unknowns=tuple(frame.get("absent", ()))),
        ),
        provenance=Provenance(method="observation"),
        action_hints=ActionHints(priority="normal", ttl_seconds=TTL_SECONDS),
    )


def parse_evidence(claim: dict) -> dict:
    """A __sensors__ claim's evidence back to a dict, numbers as floats (sample_ms as int)."""
    out = {}
    for item in claim.get("evidence", []):
        key, _, value = item.partition("=")
        if key in ("min", "max", "margin_low", "margin_high"):
            out[key] = float(value)
        elif key == "sample_ms":
            out[key] = int(value)
        else:
            out[key] = value
    return out
