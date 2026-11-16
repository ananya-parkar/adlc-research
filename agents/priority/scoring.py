# agents/priority/scoring.py
"""
Deterministic ranking for the Priority Agent. No LLM in this file.

score_backlog(stories, risks, ticket_status, policy) ->
    {"ranked": [...], "unranked": [...], "notes": [...]}

Rules
  * A story without an estimate is NOT ranked low — it goes to `unranked` with the reason.
  * A story whose dependency is missing / unestimated / itself blocked is `unranked` (blocked).
  * Dependencies whose Jira ticket is already done are ignored (and noted).
  * Circular dependencies are broken for ordering and flagged for review.
  * A story is never ranked above a story it depends on.
  * Ties are broken by value, then smaller effort, then title — and flagged.
"""

import heapq
from typing import Dict, List, Optional


def _value(story: dict, inherited: Dict[str, tuple], policy) -> tuple:
    """(value 1-5, source text, review reason or None)"""
    ticket = story.get("ticket") or {}
    prio = (ticket.get("priority") or "").strip()
    if prio and prio.lower() in policy.JIRA_PRIORITY_VALUE:
        return policy.JIRA_PRIORITY_VALUE[prio.lower()], f"Jira priority {prio}", None
    inh = inherited.get(story.get("cwi_artifact_id") or "")
    if inh:
        v, src_key, src_prio = inh
        return v, f"Jira priority {src_prio} (inherited from {src_key}, same work item)", None
    reason = (f"Unknown Jira priority '{prio}' — value defaulted to {policy.DEFAULT_VALUE}" if prio
              else f"No Jira priority — business value defaulted to {policy.DEFAULT_VALUE}")
    return policy.DEFAULT_VALUE, "default, no Jira priority", reason


def _time_criticality(story: dict, value: int, policy) -> tuple:
    ticket = story.get("ticket") or {}
    labels = {str(l).lower() for l in ticket.get("labels") or []}
    urgent = sorted(labels & policy.URGENT_LABELS)
    if urgent:
        return policy.URGENT_TIME_CRITICALITY, f"label {', '.join(urgent)}"
    if ticket.get("due_date"):
        return policy.DUE_DATE_TIME_CRITICALITY, f"due {ticket['due_date']}"
    kind = story.get("kind") or ""
    if kind == "bug_fix" and value < policy.BUG_URGENCY_MIN_VALUE:
        return policy.DEFAULT_TIME_CRITICALITY, "low-priority bug"
    return policy.TIME_CRITICALITY_BY_KIND.get(kind, policy.DEFAULT_TIME_CRITICALITY), kind or "default"


def _risk_reduction(story_id: str, risks: List[dict], policy) -> tuple:
    best, ids = 0, []
    for r in risks:
        if story_id in (r.get("affected_work_items") or []):
            exp = (policy.LIKELIHOOD_POINTS.get((r.get("likelihood") or "").lower(), 1)
                   * policy.IMPACT_POINTS.get((r.get("impact") or "").lower(), 1))
            best = max(best, exp)
            if r.get("risk_id"):
                ids.append(r["risk_id"])
    score = 1
    for min_exp, s in policy.RISK_EXPOSURE_BANDS:
        if best >= min_exp:
            score = s
    return (score if ids else 1), ids, best


def _size(hours: float, policy) -> int:
    for max_h, size in policy.SIZE_BUCKETS:
        if hours <= max_h:
            return size
    return policy.SIZE_BUCKETS[-1][1]


def _sccs(nodes: List[str], edges: Dict[str, set]) -> List[List[str]]:
    """Tarjan's strongly connected components (iterative)."""
    index, low, on, stack, out, counter = {}, {}, set(), [], [], [0]
    for root in nodes:
        if root in index:
            continue
        work = [(root, iter(sorted(edges.get(root, ()))))]
        index[root] = low[root] = counter[0]; counter[0] += 1
        stack.append(root); on.add(root)
        while work:
            v, it = work[-1]
            nxt = next(it, None)
            if nxt is not None:
                if nxt not in index:
                    index[nxt] = low[nxt] = counter[0]; counter[0] += 1
                    stack.append(nxt); on.add(nxt)
                    work.append((nxt, iter(sorted(edges.get(nxt, ())))))
                elif nxt in on:
                    low[v] = min(low[v], index[nxt])
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[v])
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop(); on.discard(w); comp.append(w)
                    if w == v:
                        break
                out.append(comp)
    return out


def score_backlog(stories: List[dict], risks: List[dict], ticket_status: Dict[str, Optional[str]], policy) -> dict:
    by_id = {s["work_item_id"]: s for s in stories}
    by_key = {s["key"]: s for s in stories if s.get("key")}
    label = {s["work_item_id"]: (s.get("key") or s["title"][:40]) for s in stories}
    notes: List[str] = []

    # priority inherited by NEW stories from a refined sibling in the same work item
    inherited: Dict[str, tuple] = {}
    for s in stories:
        p = ((s.get("ticket") or {}).get("priority") or "").strip()
        if s.get("key") and p.lower() in policy.JIRA_PRIORITY_VALUE and s.get("cwi_artifact_id"):
            v = policy.JIRA_PRIORITY_VALUE[p.lower()]
            cur = inherited.get(s["cwi_artifact_id"])
            if not cur or v > cur[0]:
                inherited[s["cwi_artifact_id"]] = (v, s["key"], p)

    unranked: Dict[str, dict] = {}
    for s in stories:
        est = s.get("estimate") or {}
        if not isinstance(est.get("expected_hours"), (int, float)):
            unranked[s["work_item_id"]] = {"reason": "No estimate — run Estimation & Risk for this story"}

    # ---- dependency graph: edge a -> b means "a must come before b" ----------------
    edges: Dict[str, set] = {sid: set() for sid in by_id}
    blocked_by: Dict[str, List[str]] = {}
    satisfied: Dict[str, List[str]] = {}
    for s in stories:
        sid = s["work_item_id"]
        for dep in s.get("depends_on") or []:
            if dep == s.get("key"):
                continue
            status = (ticket_status.get(dep) or "").lower()
            if status in policy.DONE_STATUSES:
                satisfied.setdefault(sid, []).append(f"{dep} (already {status})")
                continue
            target = by_key.get(dep)
            if not target:
                blocked_by.setdefault(sid, []).append(f"{dep} is not in the specified backlog")
                continue
            if target["work_item_id"] in unranked:
                blocked_by.setdefault(sid, []).append(f"{dep} has no estimate")
                continue
            edges[target["work_item_id"]].add(sid)

    for sid, reasons in blocked_by.items():
        if sid not in unranked:
            unranked[sid] = {"reason": "Blocked: " + "; ".join(reasons)}

    # propagate: anything waiting on an unranked story is blocked too
    changed = True
    while changed:
        changed = False
        for a, outs in edges.items():
            if a in unranked:
                for b in outs:
                    if b not in unranked:
                        unranked[b] = {"reason": f"Blocked: waits on {label[a]}, which is not ranked"}
                        changed = True

    rankable = [sid for sid in by_id if sid not in unranked]
    redges = {a: {b for b in edges[a] if b not in unranked} for a in rankable}

    # ---- cycles -----------------------------------------------------------------------
    in_cycle: Dict[str, List[str]] = {}
    for comp in _sccs(rankable, redges):
        if len(comp) > 1:
            members = set(comp)
            for a in comp:
                redges[a] -= members
                in_cycle[a] = sorted(label[m] for m in comp if m != a)
            notes.append("Circular dependency broken for ordering: " + " <-> ".join(sorted(label[m] for m in comp)))

    # ---- enablement: how many stories transitively wait on each one ----------------
    def descendants(sid: str) -> set:
        seen, todo = set(), list(redges.get(sid, ()))
        while todo:
            n = todo.pop()
            if n not in seen:
                seen.add(n)
                todo.extend(redges.get(n, ()))
        return seen

    # ---- factors ---------------------------------------------------------------------
    items: Dict[str, dict] = {}
    for sid in rankable:
        s = by_id[sid]
        est = s["estimate"]
        hours = float(est["expected_hours"])
        value, value_src, value_review = _value(s, inherited, policy)
        tc, tc_src = _time_criticality(s, value, policy)
        rr, risk_ids, exposure = _risk_reduction(sid, risks, policy)
        waiting = descendants(sid)
        en = min(policy.MAX_ENABLEMENT, 1 + len(waiting))
        size = _size(hours, policy)
        w = policy.WEIGHTS
        cod = (w["business_value"] * value + w["time_criticality"] * tc
               + w["risk_reduction"] * rr + w["enablement"] * en)
        score = round(cod / size, 3)

        review = []
        if value_review:
            review.append(value_review)
        if (est.get("confidence") or "").lower() in policy.LOW_ESTIMATE_CONFIDENCE:
            review.append("Estimate confidence is low — effort (and so the score) may move")
        if sid in in_cycle:
            review.append("Circular dependency with " + ", ".join(in_cycle[sid]) + " — confirm which comes first")

        items[sid] = {
            "work_item_id": sid,
            "key": s.get("key"),
            "title": s["title"],
            "kind": s.get("kind"),
            "epic": s.get("epic_title"),
            "score": score,
            "cost_of_delay": round(cod, 2),
            "factor_breakdown": {
                "business_value": {"score": value, "source": value_src},
                "time_criticality": {"score": tc, "source": tc_src},
                "risk_reduction": {"score": rr, "risk_ids": risk_ids, "max_exposure": exposure},
                "enablement": {"score": en, "unblocks": sorted(label[x] for x in waiting)},
                "job_size": {"score": size, "expected_hours": round(hours, 2),
                             "estimate_confidence": est.get("confidence")},
            },
            "dependencies": sorted(label[a] for a in rankable if sid in redges.get(a, set())),
            "satisfied_dependencies": satisfied.get(sid, []),
            "story_needs_review": bool(s.get("story_needs_review")),
            "review_reasons": review,
        }

    # ---- order: best score first, but never before its dependencies -----------------
    def low_value(sid):
        return (getattr(policy, "DEPRIORITIZE_LOW_VALUE", False)
                and items[sid]["factor_breakdown"]["business_value"]["score"] <= policy.LOW_VALUE_MAX)

    def sort_key(sid):
        it = items[sid]
        return (1 if low_value(sid) else 0, -it["score"], -it["factor_breakdown"]["business_value"]["score"],
                it["factor_breakdown"]["job_size"]["expected_hours"], it["title"], sid)

    indeg = {sid: 0 for sid in rankable}
    for a in rankable:
        for b in redges[a]:
            indeg[b] += 1
    heap = [(sort_key(s), s) for s in rankable if indeg[s] == 0]
    heapq.heapify(heap)
    order = []
    while heap:
        _, sid = heapq.heappop(heap)
        order.append(sid)
        for b in sorted(redges[sid]):
            indeg[b] -= 1
            if indeg[b] == 0:
                heapq.heappush(heap, (sort_key(b), b))

    ranked = []
    n = len(order)
    for pos, sid in enumerate(order, 1):
        it = items[sid]
        it["rank"] = pos
        frac = pos / n if n else 1
        it["priority_label"] = next(lbl for lbl, cut in policy.LABEL_CUTS if frac <= cut + 1e-9)
        ranked.append(it)

    # held back by a dependency? (a lower-scored dependency had to go first)
    for it in ranked:
        if low_value(it["work_item_id"]):
            it["ordering_note"] = "Low Jira priority — placed after higher-value items (policy)"
    for i, it in enumerate(ranked):
        if it.get("ordering_note"):
            continue
        lower_scored_above = any(r["score"] < it["score"] for r in ranked[:i])
        if lower_scored_above and it["dependencies"]:
            it["ordering_note"] = "Placed after its dependencies " + ", ".join(it["dependencies"])
    # a tie only matters when it decides which side of a P0/P1/P2 boundary an item lands on
    for a, b in zip(ranked, ranked[1:]):
        if a["score"] == b["score"] and a["priority_label"] != b["priority_label"]:
            for x, y in ((a, b), (b, a)):
                msg = f"Same score as {y['key'] or y['title'][:30]} — order set by tie-break rule"
                if msg not in x["review_reasons"]:
                    x["review_reasons"].append(msg)

    for it in ranked:
        it["needs_review"] = bool(it["review_reasons"])

    unranked_list = [{"work_item_id": sid, "key": by_id[sid].get("key"), "title": by_id[sid]["title"],
                      "reason": u["reason"], "needs_review": True} for sid, u in unranked.items()]
    unranked_list.sort(key=lambda u: u["title"])
    return {"ranked": ranked, "unranked": unranked_list, "notes": notes}