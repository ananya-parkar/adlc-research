# agents/priority/prompts.py
"""
The LLM does NOT rank. Code has already ranked the backlog; the LLM writes a short,
grounded rationale per item and lists questions that block a confident ranking.
Items are referenced as R1, R2 … (rank order) and mapped back in code.
"""

import json
from typing import List

MAX_RATIONALE_WORDS = 40

RATIONALE_SYSTEM_PROMPT = f"""
You explain a prioritized software backlog to a Product Owner.

The ranking is FINAL and was computed by a transparent WSJF policy:
score = (business value + time criticality + risk reduction + enablement) / job size.
You receive each item (R1 = rank 1) with its factor scores and where each score came from.

For EVERY item write one rationale (max {MAX_RATIONALE_WORDS} words) that:
- explains the position using only the supplied factors and facts (e.g. "Highest Jira priority,
  unblocks UPI and saved cards, mid-size effort");
- mentions a dependency when the item was placed after one, and states the placement_note when present;
- refers to other items by their ticket key (e.g. KAN-20), never by R-numbers;
- never invents business facts, deadlines, customers or numbers that are not supplied;
- never disagrees with or changes the order.

Return ONLY JSON:
{{"rationales": [{{"item": "R1", "rationale": "..."}}]}}
""".strip()


def build_rationale_prompt(ranked: List[dict], aliases: List[str]) -> str:
    rows = []
    for alias, it in zip(aliases, ranked):
        f = it["factor_breakdown"]
        rows.append({
            "item": alias,
            "label": it["priority_label"],
            "title": it["title"],
            "key": it.get("key"),
            "kind": it.get("kind"),
            "score": it["score"],
            "business_value": f"{f['business_value']['score']} ({f['business_value']['source']})",
            "time_criticality": f"{f['time_criticality']['score']} ({f['time_criticality']['source']})",
            "risk_reduction": f["risk_reduction"]["score"],
            "unblocks": f["enablement"]["unblocks"],
            "effort_hours": f["job_size"]["expected_hours"],
            "depends_on": it.get("dependencies") or [],
            "placement_note": it.get("ordering_note"),
            "notes": it.get("review_reasons") or [],
        })
    return ("RANKED BACKLOG (final order):\n" + json.dumps(rows, ensure_ascii=False, indent=1)
            + f"\n\nReturn rationales for every item: {', '.join(aliases)}.")