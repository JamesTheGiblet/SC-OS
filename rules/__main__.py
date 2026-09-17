"""
Manage a node's rules.

    python -m rules list     --node alice
    python -m rules issue    --node alice rules/builtin.json [--force]
    python -m rules maintain --node alice
    python -m rules vary     --node alice verify-high-confidence-risk when.min_confidence 0.6 0.8 0.95

The node's ledger is store/<node>.db and its key keys/<node>.ed25519.
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from peer import load_or_create_key  # noqa: E402
from rules.engine import RuleEngine  # noqa: E402
from store import Store  # noqa: E402


def load_rule_file(engine: RuleEngine, path: str, force: bool = False) -> list[tuple[str, str | None]]:
    """Issue every rule in a JSON file. Returns (name, id or None if forgotten)."""
    results = []
    for r in json.loads(Path(path).read_text(encoding="utf-8")):
        rule_id = engine.issue(r["name"], r["when"], r["then"], r.get("description", ""), force=force)
        results.append((r["name"], rule_id))
    return results


def engine_for(node: str) -> RuleEngine:
    agent = f"agent://{node}"
    return RuleEngine(agent, Store(str(ROOT / "store" / f"{node}.db")),
                      load_or_create_key(agent, str(ROOT / "keys")))


def main() -> int:
    ap = argparse.ArgumentParser(description="Manage a node's rules.")
    ap.add_argument("command", choices=["list", "issue", "maintain", "vary"])
    ap.add_argument("file", nargs="?", help="rule file for issue; rule name for vary")
    ap.add_argument("path", nargs="?", help="vary: dotted path in the spec, e.g. when.min_confidence")
    ap.add_argument("values", nargs="*", help="vary: the values to try")
    ap.add_argument("--node", default="alice")
    ap.add_argument("--force", action="store_true", help="issue: revive forgotten rules")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    engine = engine_for(args.node)

    if args.command == "issue":
        if not args.file:
            ap.error("issue needs a rule file")
        for name, rule_id in load_rule_file(engine, args.file, args.force):
            print(f"  {name:32} {'issued ' + rule_id[-12:] if rule_id else 'forgotten (use --force)'}")
    elif args.command == "vary":
        if not (args.file and args.path and args.values):
            ap.error("vary needs a rule name, a dotted path and at least one value")
        try:
            values = [json.loads(v) for v in args.values]        # numbers, strings, lists
        except json.JSONDecodeError:
            values = args.values
        try:
            issued = engine.vary(args.file, args.path, values, force=args.force)
        except (ValueError, KeyError, IndexError, TypeError) as e:
            print(f"  cannot vary {args.file}: {e}")
            return 1
        for name, rule_id in issued:
            print(f"  {name:32} {'issued ' + rule_id[-12:] if rule_id else 'forgotten (use --force)'}")
    elif args.command == "maintain":
        for what, names in engine.maintain().items():
            print(f"  {what:10} {', '.join(names) or '-'}")

    for r in engine.status():
        print(f"  {r['status']:10} {r['name']:32} value={r['value']:+.2f} weight={r['weight']:.2f} "
              f"n={r['evidence_count']} stance={r['stance']:16} fires={'yes' if r['fires'] else 'no'}")
    for p in engine.problems:
        print(f"  problem: {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
