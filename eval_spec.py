# eval_spec.py
"""
Scores Spec Synthesizer runs against the cases planted by seed_jira.py.

Every Spec run (dry or real) writes a JSON snapshot to storage/spec_results/.
This script checks those snapshots, like eval_discovery.py does for Discovery.

Usage:
    python eval_spec.py              # score the latest run
    python eval_spec.py --last 3     # last 3 runs + pass rate per check (consistency)
    python eval_spec.py path/to/run.json
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import List, Optional

RESULTS_DIR = Path(__file__).resolve().parent / "storage" / "spec_results"

NOT_READY = ("Improve performance", "Refund processing", "SMS OTP", "Task 2", "Password reset via email")
EPICS = ("User Authentication", "Reporting & Analytics", "Payments", "Notifications")
PCI = re.compile(r"\b(pci|card numbers?|card data|cardholder)\b", re.I)


class Run:
    def __init__(self, data: dict):
        self.items = data["work_items"]
        self.skipped = data.get("skipped_work_items") or []

    def item(self, title_part: str) -> Optional[dict]:
        t = title_part.lower()
        return next((i for i in self.items if t in (i.get("title") or "").lower()), None)

    def stories(self, title_part: Optional[str] = None) -> List[dict]:
        if title_part is None:
            return [s for i in self.items for s in i["stories"]]
        i = self.item(title_part)
        return i["stories"] if i else []

    @staticmethod
    def text(s: dict) -> str:
        parts = [s.get("title") or ""]
        parts += [f"{a['given']} {a['when']} {a['then']}" for a in s.get("acceptance_criteria") or []]
        parts += [n["requirement"] for n in s.get("nfrs") or []]
        return " ".join(parts)

    def ac_text(self, title_part: str) -> str:
        return " ".join(f"{a['given']} {a['when']} {a['then']}"
                        for s in self.stories(title_part) for a in s["acceptance_criteria"]).lower()

    def refs_of(self, title_part: str) -> List[str]:
        return [s["refines"] for s in self.stories(title_part) if s.get("refines")]

    def depends(self, title_part: str, on_title: str) -> bool:
        targets = set(self.refs_of(on_title))
        return bool(targets) and any(targets & set(s.get("depends_on") or []) for s in self.stories(title_part))


def _min_ac(s):
    return 1 if s.get("kind") == "bug_fix" else 2


CHECKS = [
    # completeness / eligibility
    ("coverage", "Every specified work item has at least one story", 2,
     lambda r: r.items and all(i["stories"] for i in r.items)),
    ("coverage", "No story for not_ready items", 2,
     lambda r: not any(r.item(t) for t in NOT_READY)),
    ("coverage", "Epics are not specified as stories", 1,
     lambda r: not any(r.item(e) and (r.item(e).get("title") or "").strip().lower() == e.lower() for e in EPICS)),

    # structure
    ("structure", "Every story has enough complete Given/When/Then AC", 2,
     lambda r: all(len(s["acceptance_criteria"]) >= _min_ac(s)
                   and all(a.get(k) for a in s["acceptance_criteria"] for k in ("given", "when", "then"))
                   for s in r.stories())),
    ("structure", "User stories have a full as-a / I-want / so-that", 1,
     lambda r: all(s.get("as_a") and s.get("i_want") and s.get("so_that")
                   for s in r.stories() if s.get("kind") != "bug_fix")),
    ("structure", "No duplicated 'Given/When/Then' keywords in AC", 1,
     lambda r: not any(re.match(r"\s*(given|when|then)\b", a[k], re.I)
                       for s in r.stories() for a in s["acceptance_criteria"] for k in ("given", "when", "then"))),

    # refine, don't duplicate
    ("refines", "Each ticket is refined by at most one story", 2,
     lambda r: len(r.refs_of_all()) == len(set(r.refs_of_all()))),
    ("refines", "Every work item refines at least one of its tickets", 1,
     lambda r: all(any(s.get("refines") for s in i["stories"]) for i in r.items)),
    ("refines", "No work item has unrefined (uncovered) tickets", 2,
     lambda r: not any(i.get("uncovered_tickets") for i in r.items)),
    ("refines", "Google SSO ~ Gmail: duplicate reported (or kept as separate items)", 1,
     lambda r: bool((r.item("Google") or {}).get("duplicate_tickets")) or bool(r.item("Gmail"))),

    # values
    ("values", "UPI keeps the ticket's real values (10 s, 5 min) unmarked", 2,
     lambda r: (lambda t: bool(re.search(r"(?<!proposed: )\b10\s*(s|sec|seconds)\b", t))
                and bool(re.search(r"(?<!proposed: )\b5\s*(min|minutes)\b", t)))(r.ac_text("UPI"))),
    ("values", "Every unverified value is marked <proposed: …> in the text", 2,
     lambda r: all(all(f"<proposed: {v}>" in Run.text(s) for v in s.get("unverified_values") or [])
                   for s in r.stories())),
    ("values", "Stories with unverified values have confidence <= 0.75", 1,
     lambda r: all(s["confidence"] <= 0.75 for s in r.stories() if s.get("unverified_values"))),

    # relevance
    ("relevance", "No PCI / card content on CSV and PDF export stories", 2,
     lambda r: not any(PCI.search(Run.text(s)) for t in ("CSV", "PDF") for s in r.stories(t))),
    ("relevance", "Saved cards keeps the no-card-storage / tokenization rule", 2,
     lambda r: any(re.search(r"token|card number|not store", Run.text(s), re.I) for s in r.stories("Save cards"))),

    # ticket requirements
    ("requirements", "CSV AC test visible columns AND active filters", 2,
     lambda r: "column" in r.ac_text("CSV") and "filter" in r.ac_text("CSV")),
    ("requirements", "No story left with uncovered ticket requirements", 1,
     lambda r: not any(s.get("uncovered_requirements") for s in r.stories())),

    # dependencies & questions
    ("deps", "UPI depends on the Razorpay ticket", 1, lambda r: r.depends("UPI", "Razorpay")),
    ("deps", "Saved cards depends on the Razorpay ticket", 1, lambda r: r.depends("Save cards", "Razorpay")),
    ("deps", "Weekly report emails depend on SendGrid and PDF export", 1,
     lambda r: r.depends("weekly report", "SendGrid") and r.depends("weekly report", "PDF")),
    ("deps", "Saved cards carries the MVP-vs-phase-2 question", 1,
     lambda r: any("mvp" in q.lower() for s in r.stories("Save cards") for q in s.get("open_questions") or [])),

    # output hygiene
    ("output", "All components match baseline/blueprint names", 1,
     lambda r: not any(s.get("unmatched_components") for s in r.stories())),
    ("output", "No leaked prompt ids (W1, W2) in story text", 1,
     lambda r: not any(re.search(r"\bW\d+\b", Run.text(s)) for s in r.stories())),
    ("output", "No duplicate open questions within a story", 1,
     lambda r: all(len({q.lower()[:30] for q in s.get("open_questions") or []}) == len(s.get("open_questions") or [])
                   for s in r.stories())),
]


def _refs_of_all(self):
    return [s["refines"] for s in self.stories() if s.get("refines")]


Run.refs_of_all = _refs_of_all


def score(path: Path) -> List[bool]:
    run = Run(json.loads(path.read_text(encoding="utf-8")))
    results = []
    for _, _, _, fn in CHECKS:
        try:
            results.append(bool(fn(run)))
        except Exception:
            results.append(False)
    got = sum(w for (_, _, w, _), ok in zip(CHECKS, results) if ok)
    total = sum(w for _, _, w, _ in CHECKS)
    n_stories = len(run.stories())
    review = sum(1 for s in run.stories() if s.get("needs_review"))
    print(f"\n{path.name}: {got}/{total} points  ({got / total:.0%})   "
          f"work_items={len(run.items)}  stories={n_stories}  review={review}")
    return results


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("files", nargs="*", type=Path)
    p.add_argument("--last", type=int, default=1)
    args = p.parse_args()

    files = args.files or sorted(RESULTS_DIR.glob("*.json"))[-args.last:]
    if not files:
        sys.exit(f"No results in {RESULTS_DIR}. Run the Spec Synthesizer first (a --dry-run is enough).")

    all_results = [score(f) for f in files]
    print("\n" + "-" * 88)
    print(f"{'check':<60}{'weight':>7}   " + ("result" if len(files) == 1 else f"pass rate ({len(files)} runs)"))
    print("-" * 88)
    category = None
    for idx, (cat, desc, weight, _) in enumerate(CHECKS):
        if cat != category:
            category = cat
            print(f"[{cat}]")
        passes = sum(r[idx] for r in all_results)
        mark = ("PASS" if passes else "FAIL") if len(files) == 1 else \
            f"{passes}/{len(files)}" + ("" if passes in (0, len(files)) else "   <- inconsistent")
        print(f"  {desc:<58}{weight:>7}   {mark}")
    print()


if __name__ == "__main__":
    main()