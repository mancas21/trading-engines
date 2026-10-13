"""The two rules.json files share rule IDs with DIFFERENT meanings.

atas-bridge/rules.json rule 2 is CVD exhaustion; scanner-engine/rules.json
rule 2 is single-venue flow dominance. This trap almost bit the 2026-10-11
audit twice (docs/FIXES-gate-DOM.md Fix 4). These tests pin the
disambiguation: distinct namespaces declared in both files, and every report
surface that cites a rule naming its file.
"""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]

sys.path.insert(0, str(ROOT / "scanner-engine"))


def _load(rel):
    return json.loads((ROOT / rel).read_text(encoding="utf-8"))


def test_namespaces_declared_and_distinct():
    atas = _load("atas-bridge/rules.json")
    scanner = _load("scanner-engine/rules.json")
    assert atas.get("namespace") == "atas"
    assert scanner.get("namespace") == "scanner"
    assert atas["namespace"] != scanner["namespace"]


def test_overlapping_ids_mean_different_things():
    atas = _load("atas-bridge/rules.json")
    scanner = _load("scanner-engine/rules.json")
    a2 = next(r for r in atas["rules"] if r["id"] == 2)
    s2 = scanner["rules"]["2"]
    assert "agotamiento" in a2["title"].lower() or "exhaustion" in a2["title"].lower()
    assert "dominancia" in s2["name"].lower() or "flow" in s2["name"].lower()


def test_audit_render_cites_its_rules_file():
    import expectancy as E
    import recorder as RC
    db = RC.open_db(":memory:")
    out = E.render(E.audit(db))
    assert "scanner-engine/rules.json" in out
    assert "namespace: scanner" in out
