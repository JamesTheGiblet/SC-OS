"""
Append-only capsule store. Content-addressed by digest.
One file per store, JSON Lines format.
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

    def append(self, capsule_wire: dict) -> str:
        d = digest(capsule_wire)
        if d in self._index:
            return d   # content-addressed: same bytes, already stored
        rec = {
            "digest": d,
            "stored_at": datetime.now(timezone.utc).isoformat(),
            "capsule": capsule_wire,
        }
        with self.path.open("a") as f:
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")
        self._index[d] = self._next_line
        self._next_line += 1
        return d

    def get(self, digest_hex: str) -> dict | None:
        if digest_hex not in self._index:
            return None
        target = self._index[digest_hex]
        with self.path.open() as f:
            for i, line in enumerate(f):
                if i == target:
                    return json.loads(line)["capsule"]
        return None

    def has(self, digest_hex: str) -> bool:
        return digest_hex in self._index

    def all(self) -> Iterator[dict]:
        with self.path.open() as f:
            for line in f:
                if line.strip():
                    yield json.loads(line)["capsule"]

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
        """Full records (digest, stored_at, capsule) in log order."""
        with self.path.open() as f:
            for line in f:
                if line.strip():
                    yield json.loads(line)

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
        print(f"[{i}] {rec['digest'][:12]}  {rec['stored_at']}  "
              f"{c.get('intent', '?').upper():7} {c.get('from')} -> {c.get('to')}  "
              f"topic={c.get('semantics', {}).get('topic')}  "
              f"trigger={c.get('trigger')}")
        if full:
            print(json.dumps(c, indent=2, sort_keys=True))