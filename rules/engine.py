"""
Rules are capsules. A node runs only rules it signed itself; trust in a rule is
this node's own opinion of it, earned from outcomes, and a rule lives only as
long as that trust does.

A rule capsule:
    topic      rule.<name>
    from, to   the owning node
    claims     [directive: the spec as JSON {"when": {...}, "then": [...]},
                observation: a description (optional)]
    id         derived from owner + name + spec, so re-issuing an unchanged rule
               keeps its id; editing a rule makes a new rule that starts at unknown

`when` is a pattern (rules/pattern.py). `then` is a list of capsules to emit:

    {"intent": "request", "to": "{from}", "trigger": "task", "topic": "{topic}",
     "claims": [{"type": "directive", "statement": "verify: {claim}", "confidence": 1.0}],
     "ttl_seconds": 3600, "priority": "high", "requires_ack": true}

Placeholders: {from} {to} {topic} {id} {claim} (the most confident claim)
{confidence} {rule}. Every emitted capsule has provenance.method "rule" and
derived_from = (rule id, input id), so any output answers "why does this exist?".

Firing
    A rule fires on a capsule addressed to the owner, not sent by the owner, while
    the rule's value is above 0 (unknown, unclear and trusted rules fire; wary and
    distrusted don't). The same rule fires at most once per input: output ids are
    derived from (rule, input, position) and the ledger is checked.

Chaining
    A rule may fire on this node's own rule outputs, so behaviour can build on
    behaviour, bounded three ways: a chain is at most `max_depth` rule steps long
    (2 by default), a rule never fires on a capsule its own chain produced (no
    A->A or A->B->A), and identical outputs are already blocked by their derived
    ids. Nothing else the node sends can trigger its rules.

Learning
    When a task_result with an outcome answers a rule's output, sent by the agent
    the output went to, the scheduler hands it here once per task. The rule that
    emitted the task decays to now, then observes the outcome in full. Rules
    further back in the chain share the credit, each step worth CREDIT_SHARE of
    the one after it: an assist counts, but less than the shot.

Lifetime (tied to Leighton Weight)
    maintain() re-issues an active rule's capsule before its 7-day TTL runs out,
    but only while the rule is alive:
        tested rules:   weight >= FORGET_WEIGHT after decay
        untested rules: within UNTESTED_GRACE of being issued
    A rule that isn't alive is forgotten: never re-issued or fired again, and its
    capsule expires normally. Issuing a forgotten rule again needs force=True.
    Run maintain() before pruning the store.

Families and arbitration
    A rule named "family--variant" belongs to that family: variants of one job that
    differ in a parameter. When several rules of a family match the same capsule,
    only one fires, so variants compete instead of all answering at once:
        exploit   the most trusted (highest value, then weight)
        explore   with probability `explore`, one of the least tested instead
    Rules in different families still fire independently: they are different jobs.
    Each choice is recorded in `choices`. Variants are ordinary rules, so an
    outcome credits the one that fired, and the losers fade and are forgotten.

Adopting
    A rule capsule from a peer never runs. adopt() re-issues its spec as this
    node's own rule, which starts at unknown and earns its own trust.
"""

import dataclasses
import json
import random
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from envelope import open_envelope, sign, verify
from interpreter import from_wire, ingest, to_wire
from primitive import (
    ActionHints, Capsule, Claim, ClaimType, Intent, Provenance, Semantics, Trigger,
)
from rules.pattern import matches, validate_when
from store import Store
from validator import CapsuleRejected, validate
from weight import Opinion

RULE_TOPIC_PREFIX = "rule."
RULE_TTL_SECONDS = 7 * 24 * 3600          # schema maximum
REFRESH_WITHIN = timedelta(days=2)        # re-issue when less than this is left
FORGET_WEIGHT = 0.05                      # tested rules below this weight are forgotten
UNTESTED_GRACE = timedelta(days=30)       # untested rules get this long to be tested
FIRE_ABOVE_VALUE = 0.0                    # wary (<= 0) and distrusted rules don't fire
EPSILON = 1e-9                            # 1.0 - 5 x 0.2 is 1.1e-16 in floating point, not 0
MAX_SPEC_CHARS = 2000
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
FAMILY_SEP = "--"                         # "verify-risk--conf80" is a variant of "verify-risk"
EXPLORE = 0.2                             # how often a family tries a less tested variant
MAX_DEPTH = 2                             # rule steps allowed in one chain
CREDIT_SHARE = 0.5                        # each step back up the chain earns this much of the next
ID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://semantic-capsule.dev/sc-os/rules")

EMIT_KEYS = {"intent", "to", "trigger", "topic", "claims", "ttl_seconds", "priority", "requires_ack"}
CLAIM_KEYS = {"type", "statement", "confidence"}


def validate_then(then: list) -> None:
    if not isinstance(then, list) or not then:
        raise ValueError("then must be a non-empty list of capsules to emit")
    for i, t in enumerate(then):
        if not isinstance(t, dict):
            raise ValueError(f"then[{i}] must be an object")
        unknown = set(t) - EMIT_KEYS
        if unknown:
            raise ValueError(f"then[{i}] has unknown keys {sorted(unknown)}")
        Intent(t.get("intent", "request"))
        Trigger(t.get("trigger", "task"))
        claims = t.get("claims", [])
        if not isinstance(claims, list) or not claims:
            raise ValueError(f"then[{i}] needs at least one claim")
        for cl in claims:
            if not isinstance(cl, dict) or set(cl) - CLAIM_KEYS or not cl.get("statement"):
                raise ValueError(f"then[{i}] claims need a statement and only {sorted(CLAIM_KEYS)}")
            ClaimType(cl.get("type", "directive"))


def spec_json(when: dict, then: list) -> str:
    validate_when(when)
    validate_then(then)
    text = json.dumps({"when": when, "then": then}, sort_keys=True, separators=(",", ":"))
    if len(text) > MAX_SPEC_CHARS:
        raise ValueError(f"rule spec is {len(text)} characters; the limit is {MAX_SPEC_CHARS}")
    return text


class _Placeholders(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def _parse_time(text: str | None) -> datetime | None:
    return datetime.fromisoformat(text) if text else None


def _opinion(row: dict) -> Opinion:
    return Opinion(value=row["value"], weight=row["weight"], evidence_count=row["evidence_count"],
                   last_tested=_parse_time(row["last_tested"]))


@dataclass
class Rule:
    id: str
    name: str
    description: str
    when: dict
    then: list
    opinion: Opinion           # decayed to the time of loading; never persisted as is
    created_at: datetime


class RuleEngine:
    def __init__(self, owner: str, store: Store, key: Ed25519PrivateKey,
                 explore: float = EXPLORE, rng: random.Random | None = None,
                 max_depth: int = MAX_DEPTH):
        self.owner = owner
        self.store = store
        self.key = key
        self.public_key = key.public_key()
        self.problems: list[str] = []          # rules skipped at load, outputs that failed validation
        self.explore = explore                 # chance a family tries a less tested variant
        self.max_depth = max_depth             # rule steps allowed in one chain
        self.rng = rng or random.Random()
        self.choices: list[str] = []           # what arbitration picked, most recent last

    # --- authoring ---

    def issue(self, name: str, when: dict, then: list, description: str = "", *,
              force: bool = False, adopted_from: str | None = None,
              now: datetime | None = None) -> str | None:
        """
        Sign and store a rule as this node's own. Returns its id, or None when the
        rule was forgotten and force is False. Unchanged rules with plenty of life
        left are not re-stored.
        """
        now = now or datetime.now(timezone.utc)
        if not NAME_RE.match(name):
            raise ValueError(f"rule name {name!r} must match {NAME_RE.pattern}")
        statement = spec_json(when, then)
        rule_id = f"urn:uuid:{uuid.uuid5(ID_NAMESPACE, f'{self.owner}\n{name}\n{statement}')}"
        key = f"rule:{rule_id}"
        row = self.store.get_opinion(key)
        if row and row["status"] == "forgotten" and not force:
            return None

        previous = self._latest_by_name().get(name)
        current = self._latest_record(rule_id)
        if current and row and row["status"] == "active" and \
                self._expires(current) - now > REFRESH_WITHIN:
            return rule_id

        evidence = (f"rule:{name}",) + ((f"adopted_from:{adopted_from}",) if adopted_from else ())
        claims = [Claim(statement, ClaimType.DIRECTIVE, 1.0, evidence)]
        if description:
            claims.append(Claim(description[:2000], ClaimType.OBSERVATION, 1.0))
        derived = (previous["capsule"]["id"],) if previous and previous["capsule"]["id"] != rule_id else ()
        cap = Capsule(
            id=rule_id, created=now, sender=self.owner, receiver=self.owner,
            intent=Intent.INFORM,
            semantics=Semantics(topic=f"{RULE_TOPIC_PREFIX}{name}", claims=tuple(claims)),
            provenance=Provenance(derived_from=derived, method="synthesis"),
            action_hints=ActionHints(priority="normal", ttl_seconds=RULE_TTL_SECONDS),
        )
        self._publish(cap)

        if derived:                              # an edited rule replaces the old version
            old = self.store.get_opinion(f"rule:{derived[0]}")
            if old and old["status"] == "active":
                self.store.put_opinion(f"rule:{derived[0]}", **{**_row_values(old), "status": "superseded"})
        if row is None or row["status"] != "active":
            self.store.put_opinion(key, value=1.0, weight=0.0, evidence_count=0, last_tested=None,
                                   status="active", created_at=now.isoformat())
        return rule_id

    def adopt(self, rule_capsule: dict, now: datetime | None = None) -> str | None:
        """Re-issue a peer's rule as this node's own. It starts at unknown."""
        topic = rule_capsule["semantics"]["topic"]
        if not topic.startswith(RULE_TOPIC_PREFIX):
            raise ValueError(f"not a rule capsule: topic {topic!r}")
        spec, description = self._spec_of(rule_capsule)
        return self.issue(topic[len(RULE_TOPIC_PREFIX):], spec["when"], spec["then"], description,
                          adopted_from=f"{rule_capsule['id']} ({rule_capsule['from']})", now=now)

    # --- loading ---

    def rules(self, now: datetime | None = None) -> list[Rule]:
        """Active, signed-by-owner, unexpired rules, latest version per name."""
        now = now or datetime.now(timezone.utc)
        loaded = []
        for name, rec in sorted(self._latest_by_name(unexpired_at=now).items()):
            cap = rec["capsule"]
            row = self.store.get_opinion(f"rule:{cap['id']}")
            if row is None or row["status"] != "active":
                continue
            envelope = self.store.envelope_of(rec["digest"])
            if envelope is None or not verify(open_envelope(envelope), self.public_key):
                self.problems.append(f"rule {name}: not signed by {self.owner}; skipped")
                continue
            try:
                spec, description = self._spec_of(cap)
                spec_json(spec["when"], spec["then"])
            except (ValueError, KeyError, TypeError) as e:
                self.problems.append(f"rule {name}: invalid spec ({e}); skipped")
                continue
            opinion = _opinion(row)
            opinion.tick(now=now)
            loaded.append(Rule(cap["id"], name, description, spec["when"], spec["then"], opinion,
                               datetime.fromisoformat(row["created_at"])))
        return loaded

    # --- firing ---

    def evaluate(self, c: Capsule, now: datetime | None = None) -> list[Capsule]:
        """
        Capsules emitted by the rules that fire on c, and then by the rules that
        fire on those, up to max_depth steps.
        """
        now = now or datetime.now(timezone.utc)
        produced: list[Capsule] = []
        in_flight: dict[str, dict] = {}                  # emitted but not yet in the ledger
        queue = [to_wire(c)]
        while queue:
            wire = queue.pop(0)
            for emitted in self._fire_on(wire, now, in_flight):
                produced.append(emitted)
                in_flight[emitted.id] = to_wire(emitted)
                queue.append(in_flight[emitted.id])
        return produced

    def _fire_on(self, wire: dict, now: datetime, in_flight: dict) -> list[Capsule]:
        """One step: the rules that fire on this capsule, arbitrated per family."""
        own_rule_output = wire["from"] == self.owner and wire["provenance"]["method"] == "rule"
        if not own_rule_output and (wire["to"] != self.owner or wire["from"] == self.owner):
            return []
        depth, ancestry = self.chain(wire, in_flight)
        if depth >= self.max_depth:
            return []
        c = from_wire(wire)
        out: list[Capsule] = []
        for rule in self.arbitrate([r for r in self.rules(now)
                                    if _fires(r.opinion) and r.id not in ancestry
                                    and matches(r.when, c)]):
            for i, template in enumerate(rule.then):
                out_id = f"urn:uuid:{uuid.uuid5(ID_NAMESPACE, f'{rule.id}|{c.id}|{i}')}"
                if self.store.find(capsule_id=out_id) or out_id in in_flight:
                    continue
                try:
                    emitted = self._emit(rule, template, c, out_id, now)
                    ingest(to_wire(emitted))
                except (CapsuleRejected, ValueError) as e:
                    self.problems.append(f"rule {rule.name} output rejected: {e}")
                    continue
                out.append(emitted)
        return out

    def vary(self, name: str, path: str, values: list, *, force: bool = False,
             now: datetime | None = None) -> list[tuple[str, str | None]]:
        """
        Issue variants of an existing rule that differ in one dotted path of its spec,
        e.g. vary("verify-risk", "when.min_confidence", [0.6, 0.8, 0.95]) issues
        verify-risk--06, --08 and --095. They compete: one of a family fires at a time.
        Returns (name, id or None if that variant was forgotten earlier).
        """
        record = self._latest_by_name().get(name)
        if record is None:
            raise ValueError(f"no rule named {name!r} to vary")
        spec, description = self._spec_of(record["capsule"])
        issued = []
        for label, when, then in variants(spec, spec["when"], spec["then"], path, values):
            variant = f"{family(name)}{FAMILY_SEP}{label}"
            issued.append((variant, self.issue(variant, when, then,
                                               description or f"variant of {name}: {path}={label}",
                                               force=force, now=now)))
        return issued

    def chain(self, capsule: dict, in_flight: dict | None = None) -> tuple[int, list[str]]:
        """
        (rule steps behind this capsule, the rules that made them, nearest first).
        A capsule nothing rule-made is (0, []). in_flight holds capsules emitted in
        this pass, which aren't in the ledger yet.
        """
        in_flight = in_flight or {}
        depth, ancestry, seen = 0, [], set()
        while capsule is not None and capsule.get("provenance", {}).get("method") == "rule":
            parents = capsule["provenance"].get("derived_from") or []
            if not parents or parents[0] in seen:
                break
            seen.add(parents[0])
            ancestry.append(parents[0])
            depth += 1
            parent_id = parents[1] if len(parents) > 1 else None
            if parent_id in in_flight:
                capsule = in_flight[parent_id]
            else:
                records = self.store.find(capsule_id=parent_id) if parent_id else []
                capsule = records[-1]["capsule"] if records else None
        return depth, ancestry

    def arbitrate(self, matching: list["Rule"]) -> list["Rule"]:
        """One rule per family: the most trusted, or now and then a less tested variant."""
        families: dict[str, list[Rule]] = {}
        for rule in matching:
            families.setdefault(family(rule.name), []).append(rule)
        picked = []
        for name, candidates in sorted(families.items()):
            if len(candidates) == 1:
                picked.append(candidates[0])
                continue
            least = min(r.opinion.evidence_count for r in candidates)
            if self.rng.random() < self.explore:
                choice = self.rng.choice([r for r in candidates if r.opinion.evidence_count == least])
                why = f"exploring (n={choice.opinion.evidence_count})"
            else:
                choice = max(candidates, key=lambda r: (r.opinion.value, r.opinion.weight, r.name))
                why = f"most trusted (value={choice.opinion.value:+.2f}, n={choice.opinion.evidence_count})"
            self.choices.append(f"{name}: {choice.name} of {len(candidates)}, {why}")
            picked.append(choice)
        return picked

    def _emit(self, rule: Rule, t: dict, c: Capsule, out_id: str, now: datetime) -> Capsule:
        best = max(c.semantics.claims, key=lambda cl: cl.confidence, default=None)
        values = _Placeholders({
            "from": c.sender, "to": c.receiver, "topic": c.semantics.topic, "id": c.id,
            "claim": best.statement if best else "", "rule": rule.name,
            "confidence": f"{best.confidence:.2f}" if best else "",
        })
        fill = lambda s: str(s).format_map(values)
        claims = tuple(
            Claim(fill(cl["statement"])[:2000], ClaimType(cl.get("type", "directive")),
                  float(cl.get("confidence", 1.0)), (f"rule:{rule.name}",))
            for cl in t["claims"]
        )
        return Capsule(
            id=out_id, created=now, sender=self.owner, receiver=fill(t.get("to", "{from}")),
            intent=Intent(t.get("intent", "request")), trigger=Trigger(t.get("trigger", "task")),
            semantics=Semantics(topic=fill(t.get("topic", "{topic}"))[:128], claims=claims),
            provenance=Provenance(derived_from=(rule.id, c.id), method="rule"),
            action_hints=ActionHints(priority=t.get("priority", "normal"),
                                     ttl_seconds=int(t.get("ttl_seconds", 3600)),
                                     requires_ack=bool(t.get("requires_ack", False))),
        )

    # --- learning ---

    def record_rule_outcome(self, task: dict, success: bool,
                            now: datetime | None = None) -> list[tuple[str, Opinion, float]]:
        """
        Feed a task's outcome to the rule that emitted it, and a share of it to the
        rules whose outputs led there. `task` is the wire form of the capsule the
        outcome answers; the caller checks who reported it and counts it once.
        Returns (rule id, updated opinion, share) per rule credited, nearest first.
        """
        now = now or datetime.now(timezone.utc)
        if task.get("from") != self.owner or task.get("provenance", {}).get("method") != "rule":
            return []
        credited = []
        share = 1.0
        for rule_id in self.chain(task)[1]:
            row = self.store.get_opinion(f"rule:{rule_id}")
            if row is not None and row["status"] == "active":
                credited.append((rule_id, self._credit(rule_id, row, success, share, now), share))
            share *= CREDIT_SHARE
        return credited

    def _credit(self, rule_id: str, row: dict, success: bool, share: float, now: datetime) -> Opinion:
        """Observe an outcome worth `share` of a full one: smaller step, less weight."""
        opinion = _opinion(row)
        opinion.tick(now=now)                  # decay to now, then learn; last_tested resets
        opinion.observe(success=success, now=now, success_step=0.1 * share, failure_step=0.2 * share,
                        success_weight=1.0 * share, failure_weight=3.0 * share)
        self.store.put_opinion(f"rule:{rule_id}", value=opinion.value, weight=opinion.weight,
                               evidence_count=opinion.evidence_count,
                               last_tested=now.isoformat(), status="active")
        return opinion

    # --- lifetime ---

    def maintain(self, now: datetime | None = None) -> dict[str, list[str]]:
        """Forget rules whose trust has faded; re-issue live rules before they expire."""
        now = now or datetime.now(timezone.utc)
        report: dict[str, list[str]] = {"refreshed": [], "forgotten": [], "kept": []}
        for key, row in self.store.opinions("rule:").items():
            if row["status"] != "active":
                continue
            rule_id = key[len("rule:"):]
            record = self._latest_record(rule_id)
            name = record["capsule"]["semantics"]["topic"][len(RULE_TOPIC_PREFIX):] if record else rule_id
            opinion = _opinion(row)
            opinion.tick(now=now)
            age = now - datetime.fromisoformat(row["created_at"])
            alive = (opinion.weight >= FORGET_WEIGHT) if opinion.evidence_count \
                else age <= UNTESTED_GRACE
            if not alive or record is None:
                self.store.put_opinion(key, **{**_row_values(row), "status": "forgotten"})
                report["forgotten"].append(name)
                continue
            if self._expires(record) - now < REFRESH_WITHIN:
                cap = dataclasses.replace(from_wire(record["capsule"]), created=now)
                self._publish(cap)
                report["refreshed"].append(name)
            else:
                report["kept"].append(name)
        return report

    def status(self, now: datetime | None = None) -> list[dict]:
        """Every rule this node knows, with its decayed opinion and status."""
        now = now or datetime.now(timezone.utc)
        out = []
        for key, row in self.store.opinions("rule:").items():
            record = self._latest_record(key[len("rule:"):])
            opinion = _opinion(row)
            opinion.tick(now=now)
            out.append({
                "id": key[len("rule:"):],
                "name": record["capsule"]["semantics"]["topic"][len(RULE_TOPIC_PREFIX):] if record else "?",
                "status": row["status"], "value": opinion.value, "weight": opinion.weight,
                "evidence_count": opinion.evidence_count, "stance": opinion.stance,
                "fires": row["status"] == "active" and _fires(opinion),
                "expires": self._expires(record).isoformat() if record else None,
            })
        return sorted(out, key=lambda r: (r["status"] != "active", r["name"]))

    # --- helpers ---

    def _publish(self, cap: Capsule) -> None:
        wire = to_wire(cap)
        validate(wire, now=cap.created)          # as of its own creation time (now= may be simulated)
        env = sign(wire, self.key, pubkey_id=self.owner).to_wire()
        self.store.append(env["capsule"], envelope=env)

    def _latest_by_name(self, unexpired_at: datetime | None = None) -> dict[str, dict]:
        latest: dict[str, dict] = {}
        for rec in self.store.find(topic_prefix=RULE_TOPIC_PREFIX, sender=self.owner,
                                   unexpired_at=unexpired_at):
            latest[rec["capsule"]["semantics"]["topic"][len(RULE_TOPIC_PREFIX):]] = rec
        return latest

    def _latest_record(self, capsule_id: str) -> dict | None:
        recs = self.store.find(capsule_id=capsule_id, sender=self.owner)
        return recs[-1] if recs else None

    @staticmethod
    def _expires(record: dict) -> datetime:
        cap = record["capsule"]
        return datetime.fromisoformat(cap["created"]) + timedelta(
            seconds=cap.get("action_hints", {}).get("ttl_seconds", 3600))

    @staticmethod
    def _spec_of(cap: dict) -> tuple[dict, str]:
        claims = cap["semantics"].get("claims", [])
        directive = next(cl for cl in claims if cl["type"] == "directive")
        description = next((cl["statement"] for cl in claims if cl["type"] == "observation"), "")
        spec = json.loads(directive["statement"])
        return spec, description


def family(name: str) -> str:
    """The family a rule name belongs to: everything before the first "--"."""
    return name.split(FAMILY_SEP)[0]


def variants(specs: dict, base_when: dict, base_then: list, path: str, values: list) -> list[tuple]:
    """
    (name suffix, when, then) for each value of one dotted path in a rule's spec,
    e.g. path "when.min_confidence" or "then.0.ttl_seconds".
    """
    out = []
    for value in values:
        when, then = json.loads(json.dumps(base_when)), json.loads(json.dumps(base_then))
        target = {"when": when, "then": then}
        keys = path.split(".")
        for key in keys[:-1]:
            target = target[int(key)] if isinstance(target, list) else target[key]
        last = keys[-1]
        if isinstance(target, list):
            target[int(last)] = value
        else:
            target[last] = value
        label = str(value).replace(".", "").replace("-", "m").lower()[:16]
        out.append((label, when, then))
    return out


def _fires(opinion: Opinion) -> bool:
    return opinion.value > FIRE_ABOVE_VALUE + EPSILON


def _row_values(row: dict) -> dict:
    return {k: row[k] for k in ("value", "weight", "evidence_count", "last_tested", "status")}
