"""
Self-description: every generated capsule is valid, ids follow content,
versions chain through derived_from, and a rerun with nothing changed stores nothing.
Run from the project root: python tests/test_self_describe.py
"""

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import self_describe as sd
from interpreter import ingest, to_wire
from primitive import Claim, ClaimType, Relation


def test_every_description_is_a_valid_capsule():
    descriptions = sd.describe_all(run_tests=False)
    topics = {d[0] for d in descriptions}
    assert {"self.identity", "self.vocabulary", "self.module", "self.tests",
            "self.decisions", "self.open_questions", "self.limits", "self.next"} <= topics
    subjects = [d[1] for d in descriptions]
    assert len(subjects) == len(set(subjects)), "subjects must be unique"
    for topic, subject, claims, relations, unknowns in descriptions:
        cap = sd.build(topic, subject, sd.with_subject(claims, subject), relations, unknowns, None)
        ingest(to_wire(cap))                       # raises CapsuleRejected if invalid


def test_module_description_reads_the_code():
    topic, subject, claims, relations, _ = sd.describe_module(ROOT / "store.py")
    assert (topic, subject) == ("self.module", "store.py")
    text = "\n".join(c.statement for c in claims)
    assert "class Store" in text and "prune_expired" in text and "find(" in text
    assert Relation("store.py", "depends_on", "envelope.py") in relations
    assert any(e.startswith("sha256:") for e in claims[0].evidence)

    _, _, _, peer_relations, _ = sd.describe_module(ROOT / "peer.py")
    assert Relation("peer.py", "depends_on", "pkg:cryptography") in peer_relations


def test_every_python_file_is_described():
    described = {d[1] for d in sd.describe_all(run_tests=False)}
    for f in sd.python_files():
        assert sd.rel(f) in described, sd.rel(f)


def test_docs_become_claims():
    _, _, decisions, _, _ = sd.describe_doc_list("self.decisions", "decisions", "NOTES.md",
                                                 "## Decisions", ClaimType.DIRECTIVE, 1.0)
    notes = (ROOT / "NOTES.md").read_text(encoding="utf-8")
    decision_block = notes.split("## Decisions", 1)[1].split("\n## ", 1)[0]
    assert len(decisions) == len(re.findall(r"^- ", decision_block, flags=re.M)) > 0
    assert all(c.type == ClaimType.DIRECTIVE for c in decisions)

    _, _, claims, _, unknowns = sd.describe_doc_list(
        "self.open_questions", "open_questions", "NOTES.md", "## Open questions",
        ClaimType.OBSERVATION, 1.0, as_unknowns=True)
    assert len(unknowns) > 0 and str(len(unknowns)) in claims[0].statement


def test_id_follows_content_and_versions_chain():
    claims = sd.with_subject([Claim("x", ClaimType.OBSERVATION, 1.0)], "thing")
    a1 = sd.build("self.module", "thing", claims, [], [], None)
    a2 = sd.build("self.module", "thing", claims, [], [], None)
    assert a1.id == a2.id and a1.created <= a2.created

    changed = sd.with_subject([Claim("y", ClaimType.OBSERVATION, 1.0)], "thing")
    b = sd.build("self.module", "thing", changed, [], [], a1.id)
    assert b.id != a1.id
    assert b.provenance.derived_from == (a1.id,)
    same = sd.build("self.module", "thing", claims, [], [], a1.id)
    assert same.provenance.derived_from == (), "unchanged content must not derive from itself"


def test_end_to_end_store_then_rerun_stores_nothing():
    db = Path(tempfile.mkdtemp()) / "self.db"
    env = dict(os.environ, PYTHONIOENCODING="utf-8")

    def run():
        out = subprocess.run([sys.executable, str(ROOT / "self_describe.py"), "--no-tests",
                              "--db", str(db)], cwd=ROOT, env=env, capture_output=True,
                             text=True, encoding="utf-8", timeout=120)
        assert out.returncode == 0, out.stderr
        return re.search(r"stored (\d+) capsules.*?(\d+) unchanged", out.stdout).groups()

    stored, unchanged = map(int, run())
    assert stored >= 30 and unchanged == 0
    stored, unchanged = map(int, run())
    assert stored == 0 and unchanged >= 30

    from store import Store
    s = Store(str(db))
    recs = s.find(sender=sd.ME)
    assert len(recs) >= 30 and all("envelope" in r for r in recs)


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"{len(tests)} passed")
