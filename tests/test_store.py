"""
Store (SQLite): exact round-trip, duplicates, reopen, pruning, signatures,
indexed columns, JSONL import, concurrent writers.
Run from the project root: python tests/test_store.py
"""

import sqlite3
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
    return Store(str(Path(tempfile.mkdtemp()) / "s.db"))


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


def signed(c: dict):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from envelope import sign
    key = Ed25519PrivateKey.generate()
    return sign(c, key, pubkey_id="agent://alice").to_wire(), key.public_key()


def test_signature_is_kept_and_verifies():
    from envelope import open_envelope, verify
    s = new_store()
    c = capsule(1, 60)
    wire, pub = signed(c)
    d = s.append(c, envelope=wire)
    env = s.envelope_of(d)
    assert env == wire
    assert verify(open_envelope(env), pub)
    reopened = Store(str(s.path))
    assert verify(open_envelope(reopened.envelope_of(d)), pub)


def test_unsigned_then_signed_supersedes_and_signed_is_not_downgraded():
    s = new_store()
    c = capsule(1, 60)
    wire, _ = signed(c)
    d = s.append(c)
    assert s.envelope_of(d) is None
    s.append(c, envelope=wire)
    s.append(c)                        # unsigned again: no-op
    s.append(c, envelope=signed(c)[0]) # different signer: first signature kept
    assert s.envelope_of(d) == wire
    assert len(s) == 1
    assert [r["digest"] for r in s.records()] == [d]
    assert list(s.all()) == [c]
    assert Store(str(s.path)).envelope_of(d) == wire


def test_envelope_for_other_capsule_rejected():
    s = new_store()
    wire, _ = signed(capsule(2, 60))
    try:
        s.append(capsule(1, 60), envelope=wire)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    assert len(s) == 0


def test_prune_after_signing_keeps_one_signed_row():
    s = new_store()
    c = capsule(1, 86400)
    wire, _ = signed(c)
    d = s.append(c)
    s.append(c, envelope=wire)
    s.append(capsule(2, 60))
    assert s.prune_expired(now=T0 + timedelta(hours=1)) == 1
    rows = sqlite3.connect(s.path).execute("SELECT digest, sig FROM capsules").fetchall()
    assert rows == [(d, wire["sig"])]
    assert s.envelope_of(d) == wire


def test_prune_gives_space_back():
    s = new_store()
    pad = "x" * 2000
    for i in range(1500):
        c = capsule(i, 60 if i % 3 else 86400)
        c["pad"] = pad
        s.append(c)
    s._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    full = s.path.stat().st_size
    assert s.prune_expired(now=T0 + timedelta(hours=1)) == 1000
    assert len(s) == 500
    assert s.path.stat().st_size < full * 0.5, (full, s.path.stat().st_size)


def test_signing_keeps_original_stored_at_and_order():
    s = new_store()
    a, b = capsule(1, 60), capsule(2, 60)
    da = s.append(a)
    db = s.append(b)
    before = s.get_record(da)["stored_at"]
    s.append(a, envelope=signed(a)[0])
    assert s.get_record(da)["stored_at"] == before
    assert [r["digest"] for r in s.records()] == [da, db]


def test_indexed_columns_are_queryable():
    s = new_store()
    c = {"id": "urn:uuid:x", "created": T0.isoformat(), "from": "agent://bob",
         "to": "agent://alice", "intent": "inform", "semantics": {"topic": "supply_chain_risk"},
         "action_hints": {"ttl_seconds": 60}}
    d = s.append(c)
    db = sqlite3.connect(s.path)
    row = db.execute("SELECT capsule_id, sender, receiver, topic, intent, expires_at "
                     "FROM capsules WHERE digest = ?", (d,)).fetchone()
    assert row == ("urn:uuid:x", "agent://bob", "agent://alice", "supply_chain_risk",
                   "inform", T0.timestamp() + 60)
    assert db.execute("SELECT json_extract(capsule, '$.semantics.topic') FROM capsules").fetchone() \
        == ("supply_chain_risk",)


def test_find_filters_on_indexed_columns():
    s = new_store()

    def cap(n, sender, topic, ttl=60):
        return {"id": f"urn:uuid:{n}", "created": T0.isoformat(), "from": sender,
                "to": "agent://alice", "intent": "inform", "semantics": {"topic": topic},
                "action_hints": {"ttl_seconds": ttl}}

    d1 = s.append(cap(1, "agent://bob", "a"))
    d2 = s.append(cap(2, "agent://carol", "a", ttl=86400))
    d3 = s.append(cap(3, "agent://bob", "b"))
    ids = lambda recs: [r["digest"] for r in recs]
    assert ids(s.find(topic="a")) == [d1, d2]
    assert ids(s.find(sender="agent://bob")) == [d1, d3]
    assert ids(s.find(topic="a", sender="agent://bob")) == [d1]
    assert ids(s.find(capsule_id="urn:uuid:3")) == [d3]
    assert ids(s.find(topic="a", unexpired_at=T0 + timedelta(hours=1))) == [d2]
    assert ids(s.find()) == [d1, d2, d3]
    assert s.find(topic="nope") == []


def test_import_jsonl_keeps_history_and_signatures():
    import json
    c1, c2 = capsule(1, 60), capsule(2, 60)
    wire2, _ = signed(c2)
    from envelope import digest
    d1, d2 = digest(c1), digest(c2)
    src = Path(tempfile.mkdtemp()) / "old.log"
    lines = [
        {"digest": d1, "stored_at": "2026-09-01T00:00:01+00:00", "capsule": c1},
        {"digest": d2, "stored_at": "2026-09-01T00:00:02+00:00", "capsule": c2},
        {"digest": d2, "stored_at": "2026-09-01T00:00:03+00:00", "capsule": c2,
         "envelope": {k: wire2[k] for k in ("sig", "alg", "pubkey_id")}},
        "not json",
    ]
    src.write_text("\n".join(l if isinstance(l, str) else json.dumps(l) for l in lines) + "\n")
    s = new_store()
    assert s.import_jsonl(str(src)) == 2
    assert s.get_record(d1)["stored_at"] == "2026-09-01T00:00:01+00:00"
    assert s.get_record(d2)["stored_at"] == "2026-09-01T00:00:02+00:00"
    assert s.envelope_of(d1) is None
    assert s.envelope_of(d2) == wire2
    assert s.import_jsonl(str(src)) == 2           # idempotent


def test_two_connections_to_one_file():
    import threading
    s1 = new_store()
    s2 = Store(str(s1.path))
    digests: list[str] = []
    lock = threading.Lock()

    def writer(store, base):
        for i in range(100):
            d = store.append(capsule(base + i, 60))
            with lock:
                digests.append(d)

    threads = [threading.Thread(target=writer, args=(st, base))
               for st, base in ((s1, 0), (s2, 10_000), (s1, 20_000), (s2, 30_000))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(s1) == len(s2) == 400
    assert all(s2.has(d) for d in digests)


def test_concurrent_appends_keep_index_exact():
    import threading
    s = new_store()
    digests: list[str] = []
    lock = threading.Lock()

    def writer(w):
        for i in range(50):
            d = s.append(capsule(w * 1000 + i, 60))
            s.append(capsule(w * 1000 + i, 60))          # duplicate, must be a no-op
            with lock:
                digests.append(d)

    threads = [threading.Thread(target=writer, args=(w,)) for w in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(s) == 400
    for store in (s, Store(str(s.path))):
        for d in digests:
            assert store.get(d)["id"].startswith("urn:uuid:")
        assert sorted(r["digest"] for r in store.records()) == sorted(digests)


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"{len(tests)} passed")
