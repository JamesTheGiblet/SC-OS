from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime

class Intent(str, Enum):
    INFORM  = "inform"
    REQUEST = "request"
    QUERY   = "query"
    CONFIRM = "confirm"
    REFUSE  = "refuse"
    ACK     = "ack"

class ClaimType(str, Enum):
    OBSERVATION = "observation"
    INFERENCE   = "inference"
    ASSUMPTION  = "assumption"
    DIRECTIVE   = "directive"

class Trigger(str, Enum):
    NONE        = "none"
    TASK        = "task"
    TASK_RESULT = "task_result"
    THRESHOLD   = "threshold"
    STUCK       = "stuck"
    HEARTBEAT   = "heartbeat"
    ANNOUNCE    = "announce"

@dataclass(frozen=True)
class Claim:
    statement: str
    type: ClaimType
    confidence: float
    evidence: tuple[str, ...] = ()
    valid_until: datetime | None = None

    def __post_init__(self):
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be in [0,1]")

@dataclass(frozen=True)
class Relation:
    subject: str
    predicate: str
    object: str

@dataclass(frozen=True)
class Uncertainty:
    known_unknowns: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()

@dataclass(frozen=True)
class Provenance:
    derived_from: tuple[str, ...] = ()
    method: str = "synthesis"
    signature: str | None = None

@dataclass(frozen=True)
class Semantics:
    topic: str
    claims: tuple[Claim, ...] = ()
    relations: tuple[Relation, ...] = ()
    uncertainty: Uncertainty = field(default_factory=Uncertainty)

@dataclass(frozen=True)
class ActionHints:
    priority: str = "normal"
    ttl_seconds: int = 3600
    requires_ack: bool = False

@dataclass(frozen=True)
class Epistemic:
    value: float = 1.0
    weight: float = 0.0
    evidence_count: int = 0
    last_tested: datetime | None = None

@dataclass(frozen=True)
class Capsule:
    id: str
    created: datetime
    sender: str
    receiver: str
    intent: Intent
    semantics: Semantics
    trigger: Trigger = Trigger.NONE
    provenance: Provenance = field(default_factory=Provenance)
    action_hints: ActionHints = field(default_factory=ActionHints)
    epistemic: Epistemic = field(default_factory=Epistemic)
    capsule_version: str = "1.0"