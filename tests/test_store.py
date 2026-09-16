"""
Store: exact round-trip, duplicates, reopen, pruning.
Run from the project root: python tests/test_store.py
"""

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from store import Store

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)


def capsule(n: int, ttl: int, created: datetime = T0) -> dict:
    return {
        "id": f"urn:uuid:{n}",
        "created": created.isoformat(),
        "confidence": 0.82,
        "action_hints": {"ttl_seconds": ttl},
    }


def new_store() -> Store:
    return Store(str(Path(tempfile.mkdtemp()) / "s.log"))


def test_get_returns_what_was_appended():
    s = new_store()
    c = capsule(1, 60)
    assert s.get(s.append(c)) == c


def test_duplicate_append_is_noop():
    s = new_store()
    d1 = s.append(capsule(1, 60))
    d2 = s.append(capsule(1, 60))
    assert d1 == d2
    assert len(s) == 1
    assert len(list(s.records())) == 1


def test_index_survives_reopen_and_later_appends():
    s = new_store()
    a = s.append(capsule(1, 60))
    s.append(capsule(1, 60))
    s2 = Store(str(s.path))
    b = s2.append(capsule(2, 60))
    assert s2.get(a) == capsule(1, 60)
    assert s2.get(b) == capsule(2, 60)


def test_prune_drops_expired_and_keeps_stored_at():
    s = new_store()
    s.append(capsule(1, ttl=60))          # expires T0+60s
    keep = s.append(capsule(2, ttl=86400))
    before = {r["digest"]: r["stored_at"] for r in s.records()}

    dropped = s.prune_expired(now=T0 + timedelta(hours=1))

    assert dropped == 1
    assert len(s) == 1
    after = {r["digest"]: r["stored_at"] for r in s.records()}
    assert after == {keep: before[keep]}
    assert s.get(keep) == capsule(2, 86400)


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"{len(tests)} passed")
