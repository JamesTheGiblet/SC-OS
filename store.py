"""
Append-only capsule store. Content-addressed by digest.
One file per store, JSON Lines format.

A record may carry the envelope signature ({sig, alg, pubkey_id}) so the
ledger alone proves who signed a capsule. Appending a signed copy of a
capsule stored unsigned adds a new line that supersedes the old one.
"""

import json
from pathlib import Path
from typing import Iterator
from datetime import datetime, timezone
from envelope import digest


class Store:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch()
        self._index: dict[str, int] = {}   # digest -> physical line number
        self._next_line = 0
        self._load_index()

    def _load_index(self) -> None:
        self._next_line = 0
        with self.path.open() as f:
            for lineno, line in enumerate(f):
                self._next_line = lineno + 1
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                    self._index[rec["digest"]] = lineno
                except (json.JSONDecodeError, KeyError):
                    continue

    def append(self, capsule_wire: dict, envelope: dict | None = None) -> str:
        """
        Store a capsule. Pass its envelope wire form ({capsule, sig, alg,
        pubkey_id}) to keep the signature. Same capsule again is a no-op,
        unless it was stored unsigned and a signature is now available.
        """
        d = digest(capsule_wire)
        if envelope is not None and digest(envelope["capsule"]) != d:
            raise ValueError("envelope does not wrap this capsule")
        if d in self._index:
            rec = self.get_record(d)
            if envelope is None or (rec is not None and "envelope" in rec):
                return d   # content-addressed: same bytes, already stored
        rec = {
            "digest": d,
            "stored_at": datetime.now(timezone.utc).isoformat(),
            "capsule": capsule_wire,
        }
        if envelope is not None:
            rec["envelope"] = {
                "sig": envelope["sig"],
                "alg": envelope.get("alg", "ed25519"),
                "pubkey_id": envelope["pubkey_id"],
            }
        with self.path.open("a") as f:
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")
        self._index[d] = self._next_line
        self._next_line += 1
        return d

    def get_record(self, digest_hex: str) -> dict | None:
        """Full record: digest, stored_at, capsule, and envelope if signed."""
        if digest_hex not in self._index:
            return None
        target = self._index[digest_hex]
        with self.path.open() as f:
            for i, line in enumerate(f):
                if i == target:
                    return json.loads(line)
        return None

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
        return digest_hex in self._index

    def all(self) -> Iterator[dict]:
        for rec in self.records():
            yield rec["capsule"]

    def prune_expired(self, now: datetime | None = None) -> int:
        """Remove capsules whose action_hints.ttl_seconds has passed."""
        now = now or datetime.now(timezone.utc)
        keep: list[dict] = []
        dropped = 0
        for rec in self.records():
            capsule = rec["capsule"]
            created = datetime.fromisoformat(capsule["created"])
            ttl = capsule.get("action_hints", {}).get("ttl_seconds", 3600)
            if (now - created).total_seconds() > ttl:
                dropped += 1
            else:
                keep.append(rec)
        if dropped:
            self._rewrite(keep)
        return dropped

    def _rewrite(self, records: list[dict]) -> None:
        """Rewrite the log with these records, unchanged (stored_at is history)."""
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w") as f:
            for rec in records:
                f.write(json.dumps(rec, separators=(",", ":")) + "\n")
        tmp.replace(self.path)
        self._index.clear()
        self._load_index()

    def records(self) -> Iterator[dict]:
        """Current full records in log order; superseded and unreadable lines skipped."""
        with self.path.open() as f:
            for lineno, line in enumerate(f):
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if self._index.get(rec.get("digest")) == lineno:
                    yield rec

    def __len__(self) -> int:
        return len(self._index)


if __name__ == "__main__":
    # python -m store store/demo.log [--full]
    import sys

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) != 1:
        sys.exit("usage: python -m store <path> [--full]")
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