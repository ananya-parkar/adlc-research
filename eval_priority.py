# eval_priority.py
"""
Evaluates the Priority Agent in two parts.

A. Fixture tests (no DB, no LLM): edge cases for the deterministic ranking —
   dependency order, blocked items, missing estimates, cycles, done dependencies,
   ties at a label boundary, missing business value, risk reduction, inheritance.
B. Run checks: the latest storage/priority_results/*.json is complete and consistent
   (plus a few KAN seed expectations when those tickets exist).

Usage
    python eval_priority.py              # fixtures + latest run
    python eval_priority.py --last 3     # fixtures + last 3 runs (consistency)
    python eval_priority.py --fixtures   # fixtures only
"""

import argparse
import json
import sys
from pathlib import Path

from agents.priority import policy
from agents.priority.scoring import score_backlog

RESULTS_DIR = Path(__file__).resolve().parent / "storage" / "priority_results"


# ----------------------------------------------------------------------
# A. fixtures
# ----------------------------------------------------------------------
def story(key, prio="Medium", hours=16.0, kind="story", deps=(), conf="medium", cwi=None, labels=()):
    return {"work_item_id": f"id-{key}", "key": key, "title": f"Story {key}", "kind": kind,
            "cwi_artifact_id": cwi or f"cwi-{key}", "depends_on": list(deps),
            "ticket": {"priority": prio, "labels": list(labels), "status": "To Do"} if prio is not None else None,
            "estimate": {"expected_hours": hours, "confidence": conf} if hours is not None else None}


def run(stories, risks=(), status=None):
    return score_backlog(stories, list(risks), status or {}, policy)


def pos(result, key):
    return next((it["rank"] for it in result["ranked"] if it["key"] == key), None)


def item(result, key):
    return next((it for it in result["ranked"] if it["key"] == key), None)


def unranked_reason(result, key):
    return next((u["reason"] for u in result["unranked"] if u["key"] == key), "")


FIXTURES = [
    ("Dependency always ranked before its dependent (even if lower value)",
     lambda: (lambda r: pos(r, "A") < pos(r, "B"))(
         run([story("A", "Low", 40), story("B", "Highest", 4, deps=["A"])]))),
    ("Missing estimate -> unranked, not ranked low",
     lambda: (lambda r: pos(r, "A") is None and "No estimate" in unranked_reason(r, "A"))(
         run([story("A", hours=None), story("B")]))),
    ("Dependent of an unestimated story is blocked",
     lambda: (lambda r: "Blocked" in unranked_reason(r, "B"))(
         run([story("A", hours=None), story("B", deps=["A"])]))),
    ("Dependency outside the backlog -> blocked with reason",
     lambda: (lambda r: "not in the specified backlog" in unranked_reason(r, "B"))(
         run([story("B", deps=["X-99"])]))),
    ("Blocking propagates down a chain",
     lambda: (lambda r: "Blocked" in unranked_reason(r, "C"))(
         run([story("A", hours=None), story("B", deps=["A"]), story("C", deps=["B"])]))),
    ("Dependency already Done in Jira is ignored",
     lambda: (lambda r: pos(r, "B") is not None and item(r, "B")["satisfied_dependencies"])(
         run([story("B", deps=["X-1"])], status={"X-1": "Done"}))),
    ("Circular dependency: both ranked, both flagged, note written",
     lambda: (lambda r: pos(r, "A") and pos(r, "B") and r["notes"]
              and all(any("Circular" in x for x in item(r, k)["review_reasons"]) for k in "AB"))(
         run([story("A", deps=["B"]), story("B", deps=["A"])]))),
    ("Higher Jira priority wins at equal effort",
     lambda: (lambda r: pos(r, "H") < pos(r, "L"))(
         run([story("L", "Low"), story("H", "High")]))),
    ("Smaller job wins at equal value (WSJF)",
     lambda: (lambda r: pos(r, "S") < pos(r, "BIG"))(
         run([story("BIG", hours=100), story("S", hours=6)]))),
    ("No Jira priority -> default value + review reason",
     lambda: (lambda r: item(r, "A")["factor_breakdown"]["business_value"]["score"] == policy.DEFAULT_VALUE
              and item(r, "A")["needs_review"])(
         run([story("A", prio=None), story("B")]))),
    ("NEW story inherits priority from a refined sibling in the same work item",
     lambda: (lambda r: "inherited" in item(r, None)["factor_breakdown"]["business_value"]["source"])(
         run([story("A", "Highest", cwi="w1"), {**story("Z", None, cwi="w1"), "key": None}]))),
    ("Story reducing a high-exposure risk scores higher than an equal one",
     lambda: (lambda r: pos(r, "R") < pos(r, "N"))(
         run([story("N"), story("R")],
             risks=[{"risk_id": "RISK-001", "likelihood": "high", "impact": "critical",
                     "affected_work_items": ["id-R"]}]))),
    ("Enabler that unblocks others gets enablement > 1",
     lambda: (lambda r: item(r, "E")["factor_breakdown"]["enablement"]["score"] >= 3)(
         run([story("E", kind="enabler"), story("U1", deps=["E"]), story("U2", deps=["E"])]))),
    ("Low estimate confidence -> review",
     lambda: (lambda r: any("confidence" in x for x in item(r, "A")["review_reasons"]))(
         run([story("A", conf="low")]))),
    ("Urgent label raises time criticality to max",
     lambda: (lambda r: item(r, "A")["factor_breakdown"]["time_criticality"]["score"]
              == policy.URGENT_TIME_CRITICALITY)(run([story("A", labels=["hotfix"])]))),
    ("Lowest-priority bug is not treated as time-critical",
     lambda: (lambda r: item(r, "T")["factor_breakdown"]["time_criticality"]["score"]
              == policy.DEFAULT_TIME_CRITICALITY)(run([story("T", "Lowest", kind="bug_fix")]))),
    ("Low-value tiny job does not jump above higher-value work",
     lambda: (lambda r: pos(r, "BIG") < pos(r, "TINY"))(
         run([story("TINY", "Lowest", 2, kind="bug_fix"), story("BIG", "High", 60)]))),
    ("Low-value item still goes first when a higher-value story depends on it",
     lambda: (lambda r: pos(r, "LOWDEP") < pos(r, "TOP"))(
         run([story("LOWDEP", "Low", 20), story("TOP", "Highest", 8, deps=["LOWDEP"])]))),
    ("Tie across a P-label boundary is flagged",
     lambda: (lambda r: any("Same score" in x for it in r["ranked"] for x in it["review_reasons"]))(
         run([story(k) for k in "ABCDE"]))),
    ("Ranks are 1..n and labels never improve going down",
     lambda: (lambda r: [it["rank"] for it in r["ranked"]] == list(range(1, len(r["ranked"]) + 1))
              and [it["priority_label"] for it in r["ranked"]] == sorted(it["priority_label"] for it in r["ranked"]))(
         run([story(k, p, h) for k, p, h in [("A", "High", 8), ("B", "Low", 30), ("C", "Medium", 12),
                                              ("D", "Highest", 60), ("E", "Lowest", 4)]]))),
]


def run_fixtures() -> bool:
    print("\nA. FIXTURE TESTS (deterministic ranking)")
    ok_all = True
    for name, fn in FIXTURES:
        try:
            ok = bool(fn())
        except Exception as exc:  # a crash is a failure
            ok, name = False, f"{name}  [error: {exc}]"
        ok_all &= ok
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    passed = sum(1 for _, fn in FIXTURES if _safe(fn))
    print(f"  -> {passed}/{len(FIXTURES)} passed")
    return ok_all


def _safe(fn):
    try:
        return bool(fn())
    except Exception:
        return False


# ----------------------------------------------------------------------
# B. run checks
# ----------------------------------------------------------------------
def _key_rank(d, key):
    return next((it["rank"] for it in d["ranked_items"] if it.get("key") == key), None)


def _has(d, key):
    return any(it.get("key") == key for it in d["ranked_items"] + d["unranked_or_blocked_items"])


RUN_CHECKS = [
    ("Every story appears exactly once (ranked or unranked)", 2,
     lambda d: (lambda ids: len(ids) == len(set(ids)))(
         [it["work_item_id"] for it in d["ranked_items"] + d["unranked_or_blocked_items"]])),
    ("Ranks are contiguous 1..n", 1,
     lambda d: [it["rank"] for it in d["ranked_items"]] == list(range(1, len(d["ranked_items"]) + 1))),
    ("No story ranked above one of its dependencies", 2,
     lambda d: all(_key_rank(d, dep) is None or _key_rank(d, dep) < it["rank"]
                   for it in d["ranked_items"] for dep in it.get("dependencies") or [])),
    ("Every ranked item has a rationale", 2,
     lambda d: all(str(it.get("rationale") or "").strip() for it in d["ranked_items"])),
    ("Every unranked item has a reason", 1,
     lambda d: all(str(u.get("reason") or "").strip() for u in d["unranked_or_blocked_items"])),
    ("Factor scores are within the policy ranges", 1,
     lambda d: all(1 <= it["factor_breakdown"][f]["score"] <= 5
                   for it in d["ranked_items"]
                   for f in ("business_value", "time_criticality", "risk_reduction", "enablement"))),
    ("Labels never improve going down the list", 1,
     lambda d: [it["priority_label"] for it in d["ranked_items"]]
     == sorted(it["priority_label"] for it in d["ranked_items"])),
    ("Defaulted business value is always flagged for review", 1,
     lambda d: all(it["needs_review"] for it in d["ranked_items"]
                   if it["factor_breakdown"]["business_value"]["source"].startswith("default"))),
    ("No leaked prompt ids (R1, R2) in rationales", 1,
     lambda d: not any(__import__("re").search(r"\bR\d+\b", it.get("rationale") or "") for it in d["ranked_items"])),
    ("Backlog is proposed for the PO gate", 1,
     lambda d: d.get("review_status") == "proposed"),
    # KAN seed expectations (pass automatically when the ticket is absent)
    ("KAN seed: Razorpay enabler (KAN-20) before UPI (KAN-21) and saved cards (KAN-22)", 2,
     lambda d: not _has(d, "KAN-20") or all(_key_rank(d, k) is None or (_key_rank(d, "KAN-20") or 999) < _key_rank(d, k)
                                            for k in ("KAN-21", "KAN-22"))),
    ("KAN seed: SendGrid (KAN-24) before weekly report emails (KAN-19)", 1,
     lambda d: _key_rank(d, "KAN-19") is None or (_key_rank(d, "KAN-24") or 999) < _key_rank(d, "KAN-19")),
    ("KAN seed: Lowest-priority typo (KAN-29) is not P0", 1,
     lambda d: next((it["priority_label"] != "P0" for it in d["ranked_items"] if it.get("key") == "KAN-29"), True)),
    ("KAN seed: KAN-16 <-> KAN-19 circular dependency is surfaced", 1,
     lambda d: not (_has(d, "KAN-16") and _has(d, "KAN-19"))
     or any("KAN-16" in n and "KAN-19" in n for n in d.get("notes") or [])
     or any("Circular" in r for it in d["ranked_items"] for r in it["review_reasons"])),
]


def score_run(path: Path) -> list:
    d = json.loads(path.read_text(encoding="utf-8"))
    res = []
    for _, _, fn in RUN_CHECKS:
        try:
            res.append(bool(fn(d)))
        except Exception:
            res.append(False)
    got = sum(w for (_, w, _), ok in zip(RUN_CHECKS, res) if ok)
    total = sum(w for _, w, _ in RUN_CHECKS)
    review = sum(1 for it in d["ranked_items"] if it["needs_review"])
    print(f"\n{path.name}: {got}/{total} points ({got / total:.0%})   ranked={len(d['ranked_items'])}  "
          f"unranked={len(d['unranked_or_blocked_items'])}  review={review}  rationale={d.get('rationale_source')}")
    return res


def main():
    p = argparse.ArgumentParser()
    p.add_argument("files", nargs="*", type=Path)
    p.add_argument("--last", type=int, default=1)
    p.add_argument("--fixtures", action="store_true", help="fixture tests only")
    args = p.parse_args()

    fixtures_ok = run_fixtures()
    if args.fixtures:
        sys.exit(0 if fixtures_ok else 1)

    files = args.files or sorted(RESULTS_DIR.glob("*.json"))[-args.last:]
    if not files:
        print(f"\nB. No results in {RESULTS_DIR}. Run the Priority Agent first (a --dry-run is enough).")
        sys.exit(0 if fixtures_ok else 1)

    print("\nB. RUN CHECKS")
    all_res = [score_run(f) for f in files]
    print("-" * 88)
    for i, (desc, w, _) in enumerate(RUN_CHECKS):
        passes = sum(r[i] for r in all_res)
        mark = ("PASS" if passes else "FAIL") if len(files) == 1 else f"{passes}/{len(files)}"
        print(f"  {desc:<74}{w:>3}   {mark}")
    print()


if __name__ == "__main__":
    main()