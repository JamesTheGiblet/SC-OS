import json
from datetime import datetime, timezone
from pathlib import Path
from jsonschema import Draft202012Validator, FormatChecker

_HERE = Path(__file__).parent
SCHEMA = json.loads((_HERE / "sc.schema.json").read_text())
VOCAB  = json.loads((_HERE / "vocab.json").read_text())
_V = Draft202012Validator(SCHEMA, format_checker=FormatChecker())

SUPPORTED_VERSIONS = {"1.0"}
SUPPORTED_VOCAB_VERSION = {"1.0"}
SUPPORTED_PREDICATES = set(VOCAB["predicates"].keys())
CLOCK_SKEW_TOLERANCE_SECONDS = 30


class CapsuleRejected(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def validate(d: dict, *, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)

    errors = sorted(_V.iter_errors(d), key=lambda e: list(e.path))
    if errors:
        e = errors[0]
        raise CapsuleRejected("schema", f"{list(e.path)}: {e.message}")

    if d["capsule_version"] not in SUPPORTED_VERSIONS:
        raise CapsuleRejected("version", f"unsupported {d['capsule_version']}")

    created = datetime.fromisoformat(d["created"])
    if created.tzinfo is None:
        raise CapsuleRejected("temporal", "created must be tz-aware")
    skew = (created - now).total_seconds()
    if skew > CLOCK_SKEW_TOLERANCE_SECONDS:
        raise CapsuleRejected("temporal", f"created is {skew:.0f}s in future")
    if (now - created).total_seconds() > 86400 * 7:
        raise CapsuleRejected("temporal", "capsule older than 7d")

    for r in d["semantics"].get("relations", []):
        if r["predicate"] not in SUPPORTED_PREDICATES:
            raise CapsuleRejected("vocab", f"unknown predicate '{r['predicate']}'")

    intent = d["intent"]
    trigger = d.get("trigger", "none")
    claims = d["semantics"].get("claims", [])

    if intent == "request" and trigger == "task":
        if not any(c["type"] == "directive" for c in claims):
            raise CapsuleRejected("coherence", "task has no directive claim")
    if intent == "inform" and not claims:
        raise CapsuleRejected("coherence", "inform has no claims")

    for c in claims:
        vu = c.get("valid_until")
        if vu and datetime.fromisoformat(vu) < now:
            raise CapsuleRejected("stale", f"claim expired: {c['statement'][:40]}")

    if d.get("provenance", {}).get("method") == "merge":
        if len(d["provenance"].get("derived_from", [])) < 2:
            raise CapsuleRejected("provenance", "merge requires >=2 parents")