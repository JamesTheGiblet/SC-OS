"""
The `when` half of a rule: a small pattern matched against an incoming capsule.

Every key given must match. Keys:

    topic           glob on semantics.topic ("temp", "*_risk")
    intent          one intent or a list ("request", ["request", "query"])
    trigger         one trigger or a list
    from, to        glob on the sender / receiver ("agent://sensor-*")
    claim_type      some claim has this type (or one of a list)
    min_confidence  some claim has at least this confidence
                    (with claim_type: the same claim must satisfy both)
    predicate       some relation uses this predicate

Handshake topics (starting "__") never match unless the topic pattern itself
starts with "__".
"""

from fnmatch import fnmatchcase

from primitive import Capsule, ClaimType, Intent, Trigger

ALLOWED_KEYS = {"topic", "intent", "trigger", "from", "to", "claim_type", "min_confidence", "predicate"}
_ENUMS = {"intent": Intent, "trigger": Trigger, "claim_type": ClaimType}


def _as_list(value) -> list:
    return value if isinstance(value, list) else [value]


def validate_when(when: dict) -> None:
    """Raise ValueError if the pattern is malformed."""
    if not isinstance(when, dict) or not when:
        raise ValueError("when must be a non-empty object")
    unknown = set(when) - ALLOWED_KEYS
    if unknown:
        raise ValueError(f"unknown pattern keys: {sorted(unknown)}")
    for key in ("topic", "from", "to", "predicate"):
        if key in when and not (isinstance(when[key], str) and when[key]):
            raise ValueError(f"{key} must be a non-empty string")
    for key, enum in _ENUMS.items():
        if key in when:
            values = _as_list(when[key])
            if not values or not all(isinstance(v, str) for v in values):
                raise ValueError(f"{key} must be a string or list of strings")
            valid = {e.value for e in enum}
            bad = [v for v in values if v not in valid]
            if bad:
                raise ValueError(f"{key} has unknown values {bad}; expected {sorted(valid)}")
    if "min_confidence" in when:
        v = when["min_confidence"]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 1:
            raise ValueError("min_confidence must be a number in [0, 1]")


def matches(when: dict, c: Capsule) -> bool:
    topic = c.semantics.topic
    if topic.startswith("__") and not str(when.get("topic", "")).startswith("__"):
        return False
    if "topic" in when and not fnmatchcase(topic, when["topic"]):
        return False
    if "intent" in when and c.intent.value not in _as_list(when["intent"]):
        return False
    if "trigger" in when and c.trigger.value not in _as_list(when["trigger"]):
        return False
    if "from" in when and not fnmatchcase(c.sender, when["from"]):
        return False
    if "to" in when and not fnmatchcase(c.receiver, when["to"]):
        return False
    if "predicate" in when and not any(r.predicate == when["predicate"] for r in c.semantics.relations):
        return False
    if "claim_type" in when or "min_confidence" in when:
        types = _as_list(when["claim_type"]) if "claim_type" in when else None
        floor = when.get("min_confidence", 0.0)
        if not any((types is None or cl.type.value in types) and cl.confidence >= floor
                   for cl in c.semantics.claims):
            return False
    return True
