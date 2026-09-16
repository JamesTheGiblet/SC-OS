"""
Capsule store on SQLite. Content-addressed by digest; one database file per node.

Each capsule is one row: the capsule as JSON (readable with sqlite3 and
json_extract), its digest, when it was stored, the envelope signature if any,
and indexed columns for lookups (sender, receiver, topic, intent, expiry).

A capsule stored unsigned and appended again with its envelope gains the
signature in place; a signed record is never replaced. Pruning deletes rows
whose created + ttl_seconds has passed.

WAL mode: readers don't block the writer, and several processes may open the
same file (SQLite serialises their writes).

    python -m store <db> [--full]            dump records in storage order
    python -m store import <jsonl> <db>      import a pre-SQLite .log ledger
"""

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from envelope import digest

SCHEMA_VERSION = 1
DEFAULT_TTL_SECONDS = 3600

_SCHEMA = """
CREATE TABLE IF NOT EXISTS capsules (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    digest     TEXT NOT NULL UNIQUE,
    stored_at  TEXT NOT NULL,
    capsule    TEXT NOT NULL,
    sig        TEXT,
    alg        TEXT,
    pubkey_id  TEXT,
    capsule_id TEXT,
    sender     TEXT,
    receiver   TEXT,
    topic      TEXT,
    intent     TEXT,
    expires_at REAL
);
CREATE INDEX IF NOT EXISTS capsules_sender   ON capsules(sender);
CREATE INDEX IF NOT EXISTS capsules_receiver ON capsules(receiver);
CREATE INDEX IF NOT EXISTS capsules_topic    ON capsules(topic);
CREATE INDEX IF NOT EXISTS capsules_id       ON capsules(capsule_id);
CREATE INDEX IF NOT EXISTS capsules_expires  ON capsules(expires_at);
"""


def _expires_at(capsule: dict) -> float | None:
    try:
        created = datetime.fromisoformat(capsule["created"])
    except (KeyError, TypeError, ValueError):
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    ttl = (capsule.get("action_hints") or {}).get("ttl_seconds", DEFAULT_TTL_SECONDS)
    return created.timestamp() + ttl


class Store:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()      # one connection, shared by threads
        self._db = sqlite3.connect(
            self.path, check_same_thread=False, isolation_level=None, timeout=10.0
        )
        self._db.row_factory = sqlite3.Row
        with self._lock:
            # auto_vacuum only takes effect on a new, empty database file
            self._db.execute("PRAGMA auto_vacuum=INCREMENTAL")
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.executescript(_SCHEMA)
            self._db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    # --- writing ---

    def append(self, capsule_wire: dict, envelope: dict | None = None) -> str:
        """
        Store a capsule. Pass its envelope wire form ({capsule, sig, alg,
        pubkey_id}) to keep the signature. Same capsule again is a no-op,
        unless it was stored unsigned and a signature is now available.
        """
        d = digest(capsule_wire)
        if envelope is not None and digest(envelope["capsule"]) != d:
            raise ValueError("envelope does not wrap this capsule")
        self._insert(d, datetime.now(timezone.utc).isoformat(), capsule_wire, envelope)
        return d

    def _insert(self, d: str, stored_at: str, capsule: dict, envelope: dict | None) -> None:
        sig = alg = pubkey_id = None
        if envelope is not None:
            sig, alg, pubkey_id = envelope["sig"], envelope.get("alg", "ed25519"), envelope["pubkey_id"]
        semantics = capsule.get("semantics") or {}
        with self._lock:
            self._db.execute(
                """INSERT INTO capsules (digest, stored_at, capsule, sig, alg, pubkey_id,
                                         capsule_id, sender, receiver, topic, intent, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(digest) DO UPDATE SET
                       sig = excluded.sig, alg = excluded.alg, pubkey_id = excluded.pubkey_id
                   WHERE capsules.sig IS NULL AND excluded.sig IS NOT NULL""",
                (
                    d, stored_at, json.dumps(capsule, separators=(",", ":"), ensure_ascii=False),
                    sig, alg, pubkey_id,
                    capsule.get("id"), capsule.get("from"), capsule.get("to"),
                    semantics.get("topic"), capsule.get("intent"), _expires_at(capsule),
                ),
            )

    def prune_expired(self, now: datetime | None = None) -> int:
        """
        Delete capsules whose created + action_hints.ttl_seconds has passed,
        then hand the freed pages back to the filesystem.
        """
        cutoff = (now or datetime.now(timezone.utc)).timestamp()
        with self._lock:
            cur = self._db.execute(
                "DELETE FROM capsules WHERE expires_at IS NOT NULL AND expires_at < ?", (cutoff,)
            )
            dropped = cur.rowcount
            if dropped:
                # frees one page per result row, so every row must be read
                self._db.execute("PRAGMA incremental_vacuum").fetchall()
                self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            return dropped

    # --- reading ---

    @staticmethod
    def _record(row: sqlite3.Row) -> dict:
        rec = {"digest": row["digest"], "stored_at": row["stored_at"],
               "capsule": json.loads(row["capsule"])}
        if row["sig"] is not None:
            rec["envelope"] = {"sig": row["sig"], "alg": row["alg"], "pubkey_id": row["pubkey_id"]}
        return rec

    def get_record(self, digest_hex: str) -> dict | None:
        """Full record: digest, stored_at, capsule, and envelope if signed."""
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM capsules WHERE digest = ?", (digest_hex,)
            ).fetchone()
        return self._record(row) if row else None

    def get(self, digest_hex: str) -> dict | None:
        rec = self.get_record(digest_hex)
        return rec["capsule"] if rec else None

    def envelope_of(self, digest_hex: str) -> dict | None:
        """Envelope wire form for open_envelope/verify, or None if unsigned."""
        rec = self.get_record(digest_hex)
        if not rec or "envelope" not in rec:
            return None
        return {"capsule": rec["capsule"], **rec["envelope"]}

    def has(self, digest_hex: str) -> bool:
        with self._lock:
            return self._db.execute(
                "SELECT 1 FROM capsules WHERE digest = ?", (digest_hex,)
            ).fetchone() is not None

    def records(self) -> Iterator[dict]:
        """All records in storage order (a snapshot; iterating holds no lock)."""
        with self._lock:
            rows = self._db.execute("SELECT * FROM capsules ORDER BY seq").fetchall()
        for row in rows:
            yield self._record(row)

    def all(self) -> Iterator[dict]:
        for rec in self.records():
            yield rec["capsule"]

    def __len__(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM capsules").fetchone()[0]

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # --- migration ---

    def import_jsonl(self, jsonl_path: str) -> int:
        """
        Import a pre-SQLite JSON Lines ledger. Keeps each record's stored_at and
        signature; a later signed line for the same digest adds its signature.
        Returns the number of records now in this store.
        """
        with open(jsonl_path, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                    capsule = rec["capsule"]
                except (json.JSONDecodeError, KeyError):
                    continue
                d = digest(capsule)
                if rec.get("digest") not in (None, d):
                    raise ValueError(f"digest mismatch in {jsonl_path}: {rec.get('digest')} != {d}")
                env = rec.get("envelope")
                wire = {"capsule": capsule, **env} if env else None
                self._insert(d, rec.get("stored_at") or datetime.now(timezone.utc).isoformat(),
                             capsule, wire)
        return len(self)


if __name__ == "__main__":
    import sys

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) == 3 and args[0] == "import":
        src, dst = args[1], args[2]
        if not Path(src).exists():
            sys.exit(f"no such ledger: {src}")
        store = Store(dst)
        before = len(store)
        after = store.import_jsonl(src)
        print(f"imported {src} -> {dst}: {after - before} new records, {after} total")
        sys.exit(0)
    if len(args) != 1:
        sys.exit("usage: python -m store <db> [--full]\n"
                 "       python -m store import <jsonl> <db>")
    if not Path(args[0]).exists():
        sys.exit(f"no such store: {args[0]}")
    full = "--full" in sys.argv
    for i, rec in enumerate(Store(args[0]).records()):
        c = rec["capsule"]
        signed = f"signed:{rec['envelope']['pubkey_id']}" if "envelope" in rec else "unsigned"
        print(f"[{i}] {rec['digest'][:12]}  {rec['stored_at']}  "
              f"{c.get('intent', '?').upper():7} {c.get('from')} -> {c.get('to')}  "
              f"topic={c.get('semantics', {}).get('topic')}  "
              f"trigger={c.get('trigger')}  {signed}")
        if full:
            print(json.dumps(rec, indent=2, sort_keys=True))
