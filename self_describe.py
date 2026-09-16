"""
SC-OS describes itself in capsules.

Reads the codebase and its docs and records what it finds as signed capsules
from agent://sc-os in store/self.db:

    self.identity       what SC-OS is, dependencies, size, git commit
    self.module         one per Python file: purpose, classes, functions, constants,
                        and depends_on relations to local modules and packages
    self.tests          one per test file: each test it defines
    self.test_results   one per test file: PASSED/FAILED from actually running it
                        (skipped with --no-tests, which leaves earlier results alone)
    self.vocabulary     intents, triggers, claim types, predicates, provenance methods
    self.decisions      design decisions from NOTES.md, as directive claims
    self.open_questions open questions from NOTES.md, as known unknowns
    self.limits         known limits from README.md (the bad and the ugly)
    self.next           planned next steps from NOTES.md

Every capsule is validated with ingest() before it is stored, so the
self-description obeys the same schema as any other capsule.

Capsule ids come from their content. Run again with nothing changed and nothing
new is stored; change a file and only its capsule gets a new version, whose
provenance.derived_from points at the version it replaces. Capsules live for
7 days (the schema maximum), so knowledge about the code that isn't refreshed
expires, like any untested opinion.

    python self_describe.py                 describe, run the tests, store
    python self_describe.py --no-tests      skip running the tests
    python self_describe.py --dry-run       print what would be stored, store nothing
    python self_describe.py --show [TOPIC]  print the current self-description
"""

import argparse
import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from envelope import sign  # noqa: E402
from interpreter import from_wire, ingest, render, to_wire  # noqa: E402
from peer import load_or_create_key  # noqa: E402
from primitive import (  # noqa: E402
    ActionHints, Capsule, Claim, ClaimType, Intent, Provenance, Relation, Semantics,
    Uncertainty,
)
from store import Store  # noqa: E402

ME = "agent://sc-os"
TTL_SECONDS = 7 * 24 * 3600
ID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://semantic-capsule.dev/sc-os/self")
MAX_CLAIMS = 256
MAX_STATEMENT = 2000
SKIP_DIRS = {"__pycache__", ".git", "store", "keys", ".vscode"}


# --- helpers ---------------------------------------------------------------

def clip(text: str, n: int) -> str:
    text = " ".join(text.replace("**", "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def first_paragraph(doc: str | None) -> str:
    if not doc:
        return ""
    return clip(doc.strip().split("\n\n")[0], 400)


def first_line(doc: str | None) -> str:
    return clip(doc.strip().splitlines()[0], 300) if doc and doc.strip() else ""


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def claim(statement: str, evidence: list[str], type_: ClaimType = ClaimType.OBSERVATION,
          confidence: float = 1.0) -> Claim:
    return Claim(clip(statement, MAX_STATEMENT), type_, confidence,
                 tuple(clip(e, 500) for e in evidence))


def python_files() -> list[Path]:
    out = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(filenames):
            if name.endswith(".py"):
                out.append(Path(dirpath) / name)
    return out


def local_module_path(module: str) -> str | None:
    parts = module.split(".")
    for candidate in (ROOT.joinpath(*parts).with_suffix(".py"),
                      ROOT.joinpath(*parts, "__init__.py")):
        if candidate.exists():
            return rel(candidate)
    return None


def signature(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    args = ast.unparse(fn.args)
    ret = f" -> {ast.unparse(fn.returns)}" if fn.returns else ""
    return f"{fn.name}({args}){ret}"


def section(md: str, heading: str) -> list[tuple[int, str]]:
    """Lines (1-based number, text) under a markdown heading, up to the next heading of that level."""
    lines = md.splitlines()
    level = heading.split(" ")[0]
    out, inside = [], False
    for i, line in enumerate(lines, 1):
        if line.startswith("#"):
            if inside and line.split(" ")[0] in (level, "#" * (len(level) - 1)):
                break
            if line.strip().startswith(heading):
                inside = True
                continue
        if inside:
            out.append((i, line))
    return out


def bullets(lines: list[tuple[int, str]]) -> list[tuple[int, str, str]]:
    """Top-level '- ' or '1. ' items as (line, bold title, full text)."""
    items: list[list] = []
    for n, line in lines:
        m = re.match(r"^(?:- |\d+\. )(.*)", line)
        if m:
            items.append([n, m.group(1)])
        elif items and line.startswith((" ", "\t")) and line.strip():
            items[-1][1] += " " + line.strip()
    out = []
    for n, text in items:
        m = re.match(r"\*\*(.+?)\*\*\s*(.*)", text)
        title = (m.group(1) if m else text).rstrip(".")
        out.append((n, clip(title, 200), clip(text.replace("**", ""), MAX_STATEMENT)))
    return out


# --- describers: each returns (topic, subject, claims, relations, unknowns) ---

def describe_module(path: Path) -> tuple:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    name = rel(path)
    ev = [f"file:{name}", f"sha256:{file_sha(path)}"]
    lines = source.count("\n") + 1

    purpose = first_paragraph(ast.get_docstring(tree)) or "no module docstring"
    claims = [claim(f"{name} ({lines} lines): {purpose}", ev)]
    relations, packages = [], set()

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            bases = ", ".join(ast.unparse(b) for b in node.bases)
            doc = first_line(ast.get_docstring(node))
            methods = [signature(m) for m in node.body
                       if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
                       and (not m.name.startswith("_") or m.name == "__init__")]
            text = f"class {node.name}" + (f"({bases})" if bases else "")
            if doc:
                text += f": {doc}"
            if methods:
                text += " Methods: " + "; ".join(methods)
            claims.append(claim(text, [f"{name}:{node.lineno}"]))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
            doc = first_line(ast.get_docstring(node))
            claims.append(claim(f"def {signature(node)}" + (f": {doc}" if doc else ""),
                                [f"{name}:{node.lineno}"]))
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name) and node.targets[0].id.isupper():
            value = clip(ast.unparse(node.value), 120)
            claims.append(claim(f"constant {node.targets[0].id} = {value}", [f"{name}:{node.lineno}"]))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            for mod in mods:
                local = local_module_path(mod)
                if local and local != name:
                    relations.append(Relation(name, "depends_on", local))
                elif not local and mod and mod.split(".")[0] not in sys.stdlib_module_names:
                    packages.add(mod.split(".")[0])

    for pkg in sorted(packages):
        relations.append(Relation(name, "depends_on", f"pkg:{pkg}"))
    relations = list(dict.fromkeys(relations))
    return "self.module", name, claims[:MAX_CLAIMS], relations, []


def run_test_file(path: Path) -> tuple[bool, str]:
    env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8")
    try:
        proc = subprocess.run([sys.executable, str(path)], cwd=ROOT, env=env,
                              capture_output=True, text=True, timeout=600, encoding="utf-8")
    except subprocess.TimeoutExpired:
        return False, "timed out after 600 s"
    out = (proc.stdout + proc.stderr).strip().splitlines()
    tail = out[-1] if out else ""
    if proc.returncode == 0:
        m = re.search(r"(\d+) passed", proc.stdout)
        return True, f"{m.group(1)} passed" if m else "exit 0"
    return False, f"exit {proc.returncode}: {clip(tail, 300)}"


def describe_test_result(path: Path) -> tuple:
    """Run one test file. The result is its own capsule, so --no-tests never erases it."""
    name = rel(path)
    ok, detail = run_test_file(path)
    ev = [f"file:{name}", f"sha256:{file_sha(path)}", f"python:{sys.version.split()[0]}"]
    claims = [claim(f"running {name}: {'PASSED' if ok else 'FAILED'} ({detail})", ev)]
    return "self.test_results", f"result:{name}", claims, [], []


def describe_tests(path: Path) -> tuple:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    name = rel(path)
    tests = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")]
    ev = [f"file:{name}", f"sha256:{file_sha(path)}"]
    summary = first_paragraph(ast.get_docstring(tree))
    claims = [claim(f"{name} defines {len(tests)} asserting test functions"
                    + (f": {summary}" if summary else ""), ev)]
    for t in tests:
        doc = first_line(ast.get_docstring(t))
        claims.append(claim(f"{t.name}" + (f": {doc}" if doc else ""), [f"{name}:{t.lineno}"]))
    relations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            local = local_module_path(node.module)
            if local and local != name:
                relations.append(Relation(name, "supports", local))
    return "self.tests", name, claims[:MAX_CLAIMS], list(dict.fromkeys(relations)), []


def git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True,
                              timeout=30).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def describe_identity(files: list[Path]) -> tuple:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    intro = next((l for l in readme.split("\n\n")[1:] if l.strip() and not l.startswith("#")), "")
    why = " ".join(t for _, t in section(readme, "## Why capsules")).strip()
    commit, dirty = git("rev-parse", "--short", "HEAD"), bool(git("status", "--porcelain"))
    code = [f for f in files if not rel(f).startswith("tests/")]
    tests = [f for f in files if rel(f).startswith("tests/")]
    count = lambda fs: sum(f.read_text(encoding="utf-8").count("\n") + 1 for f in fs)
    reqs = [l.strip() for l in (ROOT / "requirements.txt").read_text().splitlines() if l.strip()]
    ev_readme = ["file:README.md", f"sha256:{file_sha(ROOT / 'README.md')}"]
    claims = [
        claim(f"SC-OS: {intro}", ev_readme),
        claim(f"Why: {why}", ev_readme),
        claim(f"Source: {len(code)} Python files ({count(code)} lines) and {len(tests)} test files "
              f"({count(tests)} lines)", [f"git:{commit}" + ("-dirty" if dirty else "")]),
        claim(f"Runtime dependencies: {', '.join(reqs)}; storage uses Python's sqlite3",
              ["file:requirements.txt"]),
        claim(f"Describing commit {commit}" + (" with uncommitted changes" if dirty else ""),
              [f"git:{commit}"]),
    ]
    relations = [Relation("sc-os", "depends_on", f"pkg:{re.split(r'[<>=!~ ]', r)[0]}") for r in reqs]
    return "self.identity", "sc-os", claims, relations, []


def describe_vocabulary() -> tuple:
    vocab = json.loads((ROOT / "vocab.json").read_text(encoding="utf-8"))
    schema = json.loads((ROOT / "sc.schema.json").read_text(encoding="utf-8"))
    methods = schema["$defs"]["provenance"]["properties"]["method"]["enum"]
    ev_v = ["file:vocab.json", f"sha256:{file_sha(ROOT / 'vocab.json')}"]
    ev_s = ["file:sc.schema.json", f"sha256:{file_sha(ROOT / 'sc.schema.json')}"]
    claims = [
        claim(f"intents: {', '.join(vocab['intents'])}", ev_v),
        claim(f"triggers: {', '.join(vocab['triggers'])}", ev_v),
        claim(f"claim types: {', '.join(vocab['claim_types'])}", ev_v),
        claim("predicates: " + "; ".join(f"{p} ({d['subject']} -> {d['object']})"
                                         for p, d in vocab["predicates"].items()), ev_v),
        claim(f"provenance methods: {', '.join(methods)}", ev_s),
        claim(f"capsule required fields: {', '.join(schema['required'])}", ev_s),
    ]
    return "self.vocabulary", "vocabulary", claims, [], []


def describe_doc_list(topic: str, subject: str, doc: str, heading: str,
                      type_: ClaimType, confidence: float, as_unknowns: bool = False) -> tuple:
    path = ROOT / doc
    items = bullets(section(path.read_text(encoding="utf-8"), heading))
    base = [f"file:{doc}", f"sha256:{file_sha(path)}"]
    if as_unknowns:
        claims = [claim(f"{len(items)} open questions recorded in {doc}", base)]
        return topic, subject, claims, [], [text for _, _, text in items]
    claims = [claim(text, [f"{doc}:{n}"], type_, confidence) for n, _, text in items]
    return topic, subject, claims[:MAX_CLAIMS], [], []


# --- building and storing --------------------------------------------------

def build(topic: str, subject: str, claims: list[Claim], relations: list[Relation],
          unknowns: list[str], previous_id: str | None) -> Capsule:
    semantics = Semantics(topic=topic, claims=tuple(claims), relations=tuple(relations),
                          uncertainty=Uncertainty(known_unknowns=tuple(unknowns)))
    probe = Capsule(id="urn:uuid:00000000-0000-0000-0000-000000000000",
                    created=datetime(2000, 1, 1, tzinfo=timezone.utc),
                    sender=ME, receiver=ME, intent=Intent.INFORM, semantics=semantics)
    wire = to_wire(probe)
    body = json.dumps({"subject": subject, "semantics": wire["semantics"]}, sort_keys=True)
    cid = f"urn:uuid:{uuid.uuid5(ID_NAMESPACE, body)}"
    return Capsule(
        id=cid,
        created=datetime.now(timezone.utc),
        sender=ME,
        receiver=ME,
        intent=Intent.INFORM,
        semantics=semantics,
        provenance=Provenance(derived_from=(previous_id,) if previous_id and previous_id != cid else (),
                              method="observation"),
        action_hints=ActionHints(priority="low", ttl_seconds=TTL_SECONDS),
    )


def subject_of(record: dict) -> str | None:
    for cl in record["capsule"]["semantics"].get("claims", [])[:1]:
        for e in cl.get("evidence", []):
            if e.startswith("subject:"):
                return e[len("subject:"):]
    return None


def with_subject(claims: list[Claim], subject: str) -> list[Claim]:
    head = claims[0]
    tagged = Claim(head.statement, head.type, head.confidence,
                   head.evidence + (f"subject:{subject}",), head.valid_until)
    return [tagged, *claims[1:]]


def describe_all(run_tests: bool) -> list[tuple]:
    files = python_files()
    out = [describe_identity(files), describe_vocabulary()]
    for f in files:
        if rel(f).startswith("tests/test_"):
            out.append(describe_tests(f))
            if run_tests:
                out.append(describe_test_result(f))
        else:
            out.append(describe_module(f))
    out += [
        describe_doc_list("self.decisions", "decisions", "NOTES.md", "## Decisions",
                          ClaimType.DIRECTIVE, 1.0),
        describe_doc_list("self.open_questions", "open_questions", "NOTES.md", "## Open questions",
                          ClaimType.OBSERVATION, 1.0, as_unknowns=True),
        describe_doc_list("self.next", "next", "NOTES.md", "## Next", ClaimType.DIRECTIVE, 0.7),
        describe_doc_list("self.limits", "limits.bad", "README.md", "### The bad",
                          ClaimType.OBSERVATION, 0.9),
        describe_doc_list("self.limits", "limits.ugly", "README.md", "### The ugly",
                          ClaimType.OBSERVATION, 0.9),
    ]
    return [d for d in out if d[2]]


def current(store: Store) -> dict[str, dict]:
    """Latest unexpired record per subject."""
    latest: dict[str, dict] = {}
    for rec in store.find(sender=ME, unexpired_at=datetime.now(timezone.utc)):
        subject = subject_of(rec)
        if subject:
            latest[subject] = rec
    return latest


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[1],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="store/self.db")
    ap.add_argument("--no-tests", action="store_true", help="don't run the test files")
    ap.add_argument("--dry-run", action="store_true", help="print, don't store")
    ap.add_argument("--show", nargs="?", const="", metavar="TOPIC",
                    help="print the current self-description (optionally one topic)")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    store = Store(str(ROOT / args.db))

    if args.show is not None:
        recs = sorted(current(store).values(),
                      key=lambda r: (r["capsule"]["semantics"]["topic"], subject_of(r) or ""))
        recs = [r for r in recs if not args.show or r["capsule"]["semantics"]["topic"] == args.show]
        if not recs:
            print("no current self-description; run python self_describe.py")
            return 1
        for rec in recs:
            print(render(from_wire(rec["capsule"])))
            print()
        print(f"{len(recs)} capsules")
        return 0

    started = time.perf_counter()
    latest = current(store)
    key = load_or_create_key(ME, str(ROOT / "keys"))
    stored = unchanged = 0
    by_topic: dict[str, int] = {}

    for topic, subject, claims, relations, unknowns in describe_all(run_tests=not args.no_tests):
        claims = with_subject(claims, subject)
        prev = latest.get(subject)
        cap = build(topic, subject, claims, relations, unknowns,
                    prev["capsule"]["id"] if prev else None)
        if prev and prev["capsule"]["id"] == cap.id:
            unchanged += 1
            continue
        wire = to_wire(cap)
        ingest(wire)                                  # same validation as any received capsule
        by_topic[topic] = by_topic.get(topic, 0) + 1
        if args.dry_run:
            print(render(cap))
            print()
            stored += 1
            continue
        env = sign(wire, key, pubkey_id=ME).to_wire()
        store.append(env["capsule"], envelope=env)
        stored += 1
        change = "new version of" if prev else "new"
        print(f"  {change:15} {topic:20} {subject:32} {len(cap.semantics.claims):3} claims  "
              f"{len(cap.semantics.relations):2} relations")

    verb = "would store" if args.dry_run else "stored"
    print(f"{verb} {stored} capsules ({', '.join(f'{t} {n}' for t, n in sorted(by_topic.items())) or 'none'}), "
          f"{unchanged} unchanged, in {time.perf_counter() - started:.1f} s -> {args.db}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
