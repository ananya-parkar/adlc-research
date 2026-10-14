# eval_discovery.py
"""
Scores Discovery Agent runs against the cases planted by seed_jira.py.

Every Discovery run (dry or real) writes a JSON snapshot to
storage/discovery_results/. This script checks those snapshots, so you
can see whether a prompt/code change actually helped — and, across
several runs, how CONSISTENT the agent is (LLM output varies per run).

Usage:
    python eval_discovery.py              # score the latest run
    python eval_discovery.py --last 3     # score the last 3 runs + pass rate per check
    python eval_discovery.py path/to/run.json
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Callable, List, Optional

RESULTS_DIR = Path(__file__).resolve().parent / "storage" / "discovery_results"


# ----------------------------------------------------------------------
# helpers over one run
# ----------------------------------------------------------------------
class Run:
    def __init__(self, data: dict):
        self.items = data["work_items"]
        self.patterns = data["patterns"]

    def item(self, title_part: str) -> Optional[dict]:
        """Work item that contains a source artifact whose title includes title_part."""
        t = title_part.lower()
        for i in self.items:
            if any(t in (s or "").lower() for s in i["source_titles"]):
                return i
        return None

    def refs(self, title_part: str) -> List[str]:
        """Source refs of the artifact(s) whose title includes title_part."""
        t = title_part.lower()
        out = []
        for i in self.items:
            for ref, title in zip(i["source_refs"], i["source_titles"]):
                if t in (title or "").lower():
                    out.append(ref)
        return out

    def same_or_flagged(self, a: str, b: str) -> bool:
        """True if a and b were merged into one item, or flagged as possible duplicates."""
        ia, ib = self.item(a), self.item(b)
        if not ia or not ib:
            return False
        if ia is ib:
            return True
        return ib["title"] in ia["possible_duplicates"] or ia["title"] in ib["possible_duplicates"]

    def depends_on(self, item_title: str, dep_title: str) -> bool:
        i = self.item(item_title)
        dep_refs = self.refs(dep_title)
        text = " ".join(i["dependencies"]) if i else ""
        return bool(i and dep_refs and any(r in text for r in dep_refs))

    def text_of(self, i: dict) -> str:
        return " ".join([i.get("title") or "", i.get("reasoning") or ""] +
                        [x for f in ("gaps", "conflicts", "decisions", "dependencies", "open_questions")
                         for x in i.get(f) or []])


# ----------------------------------------------------------------------
# checks: (category, description, weight, fn)
# ----------------------------------------------------------------------
def _field(run: Run, title: str, field: str) -> Optional[str]:
    i = run.item(title)
    return i.get(field) if i else None


def _has(run: Run, title: str, field: str, word: str = "") -> bool:
    i = run.item(title)
    return bool(i and any(word.lower() in x.lower() for x in i.get(field) or []))


CHECKS: List[tuple] = [
    # duplicates / merging
    ("dedup", "Google SSO ~ Gmail sign-in merged or flagged", 2,
     lambda r: r.same_or_flagged("Google SSO login", "sign in using Gmail")),
    ("dedup", "Dashboard fails ~ Dashboard 30+ s merged or flagged", 2,
     lambda r: r.same_or_flagged("Dashboard fails to load", "Dashboard takes 30+")),
    ("dedup", "UPI subtasks merged into UPI story", 2,
     lambda r: r.item("Generate dynamic UPI QR") is r.item("Pay with UPI")
     and r.item("Handle UPI payment timeout") is r.item("Pay with UPI") and r.item("Pay with UPI") is not None),
    ("dedup", "Epic NOT merged with its stories", 1,
     lambda r: r.item("User Authentication") is not None
     and r.item("User Authentication") is not r.item("Email and password login")),

    # conflicts & decisions
    ("evidence", "Password reset 24h vs 15min conflict caught", 3,
     lambda r: _has(r, "Password reset via email", "conflicts")),
    ("evidence", "Saved cards: tokenization decision captured", 2,
     lambda r: _has(r, "Save cards", "decisions", "token")),
    ("evidence", "Google SSO: MVP-only decision captured", 1,
     lambda r: _has(r, "Google SSO login", "decisions", "mvp")),

    # dependencies
    ("deps", "UPI blocked by Razorpay", 1, lambda r: r.depends_on("Pay with UPI", "Razorpay")),
    ("deps", "Saved cards blocked by Razorpay", 1, lambda r: r.depends_on("Save cards", "Razorpay")),
    ("deps", "Refund blocked by Razorpay", 1, lambda r: r.depends_on("Refund processing", "Razorpay")),
    ("deps", "Password reset blocked by SendGrid", 1, lambda r: r.depends_on("Password reset via email", "SendGrid")),
    ("deps", "Weekly report blocked by SendGrid", 1, lambda r: r.depends_on("Scheduled weekly report", "SendGrid")),
    ("deps", "Pattern: Razorpay is a shared blocker", 2,
     lambda r: any(set(r.refs("Razorpay")) & set(p["source_refs"]) or "razorpay" in (p["pattern"] + p["description"]).lower()
                   for p in r.patterns)),

    # readiness & classification
    ("readiness", "'Improve performance' is not_ready", 2, lambda r: _field(r, "Improve performance", "readiness") == "not_ready"),
    ("readiness", "'Refund processing' (empty) is not_ready", 2, lambda r: _field(r, "Refund processing", "readiness") == "not_ready"),
    ("readiness", "Password reset (conflict) is not_ready", 1, lambda r: _field(r, "Password reset via email", "readiness") == "not_ready"),
    ("readiness", "'Email and password login' is ready", 1, lambda r: _field(r, "Email and password login", "readiness") == "ready"),
    ("readiness", "'Export monthly report as PDF' is ready", 1, lambda r: _field(r, "Export monthly report as PDF", "readiness") == "ready"),
    ("readiness", "Safari bug (has repro) is ready", 1, lambda r: _field(r, "Safari", "readiness") == "ready"),
    ("readiness", "All 4 epics are ready", 1,
     lambda r: all(_field(r, e, "readiness") == "ready"
                   for e in ("User Authentication", "Reporting & Analytics", "Payments", "Notifications"))),
    ("classify", "Razorpay + SendGrid typed as enabler", 1,
     lambda r: _field(r, "Razorpay", "signal_type") == "enabler" and _field(r, "SendGrid", "signal_type") == "enabler"),
    ("classify", "Vague items have confidence < 0.7", 1,
     lambda r: all((_field(r, t, "confidence") or 1) < 0.7 for t in ("Improve performance", "Refund processing"))),

    # hygiene of the output itself
    ("output", "No leaked batch ids (A4 / C11) in text", 2,
     lambda r: not any(re.search(r"\b[AC]\d+\b", r.text_of(i)) for i in r.items)
     and not any(re.search(r"\b[AC]\d+\b", p["pattern"] + p["description"]) for p in r.patterns)),
    ("output", "No ownership noise in gaps", 1,
     lambda r: not any(re.search(r"unassigned|owner|assignee", g, re.I) for i in r.items for g in i["gaps"])),
]


# ----------------------------------------------------------------------
def score(path: Path) -> List[bool]:
    run = Run(json.loads(path.read_text(encoding="utf-8")))
    results = []
    for _, _, _, fn in CHECKS:
        try:
            results.append(bool(fn(run)))
        except Exception:
            results.append(False)
    review = sum(1 for i in run.items if i.get("needs_review"))
    got = sum(w for (_, _, w, _), ok in zip(CHECKS, results) if ok)
    total = sum(w for _, _, w, _ in CHECKS)
    print(f"\n{path.name}: {got}/{total} points  ({got / total:.0%})   "
          f"items={len(run.items)}  review={review}  patterns={len(run.patterns)}")
    return results


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("files", nargs="*", type=Path)
    p.add_argument("--last", type=int, default=1, help="score the last N runs")
    args = p.parse_args()

    files = args.files or sorted(RESULTS_DIR.glob("*.json"))[-args.last:]
    if not files:
        sys.exit(f"No results in {RESULTS_DIR}. Run the Discovery Agent first (a --dry-run is enough).")

    all_results = [score(f) for f in files]

    print("\n" + "-" * 86)
    print(f"{'check':<56}{'weight':>7}   " + ("result" if len(files) == 1 else f"pass rate ({len(files)} runs)"))
    print("-" * 86)
    category = None
    for idx, (cat, desc, weight, _) in enumerate(CHECKS):
        if cat != category:
            category = cat
            print(f"[{cat}]")
        passes = sum(r[idx] for r in all_results)
        if len(files) == 1:
            mark = "PASS" if passes else "FAIL"
        else:
            mark = f"{passes}/{len(files)}" + ("" if passes in (0, len(files)) else "   <- inconsistent")
        print(f"  {desc:<54}{weight:>7}   {mark}")
    print()


if __name__ == "__main__":
    main()