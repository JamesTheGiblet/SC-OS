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


def test_prune_after_supersede_keeps_one_signed_line():
    s = new_store()
    c = capsule(1, 86400)
    wire, _ = signed(c)
    d = s.append(c)
    s.append(c, envelope=wire)
    s.append(capsule(2, 60))
    assert s.prune_expired(now=T0 + timedelta(hours=1)) == 1
    lines = [l for l in s.path.read_text().splitlines() if l.strip()]
    assert len(lines) == 1
    assert s.envelope_of(d) == wire


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
