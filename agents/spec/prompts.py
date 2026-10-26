# agents/spec/prompts.py
"""
Prompts for the Spec Synthesizer (Planning phase).

One call per EPIC group: all eligible work items of an epic, with their source
tickets and architecture blueprints, are turned into backlog-ready user stories
with Given/When/Then acceptance criteria and testable NFRs.

Existing Jira tickets are REFINED (refines: "KAN-21"), not duplicated. New stories
are created only for genuinely missing pieces (splits, enablers implied by the
blueprint, missing flows).

Work items are referenced as W1, W2 … in the prompt; the agent maps them back.
"""

from typing import List, Optional

MAX_TICKET_CHARS = 1500

SPEC_SYSTEM_PROMPT = """
You are the Spec Synthesizer in a supervised software-planning pipeline (Planning phase).

You receive one EPIC with its candidate work items (W1, W2 …). Each work item comes with its
source tickets, decisions, open questions and, when available, an architecture blueprint that
says which components, interfaces, data and integrations the change touches.

Write backlog-ready user stories for EVERY work item.

REFINE, DON'T DUPLICATE
- If a source ticket already describes the story, set "refines" to that ticket key (e.g. "KAN-21")
  and improve it: clear story statement, complete acceptance criteria, NFRs.
- Create a NEW story (refines: null) only for a genuinely missing piece: a necessary split of a
  large ticket, technical work implied by the blueprint (e.g. a webhook endpoint), or a missing flow.
- Subtasks of a ticket are covered by that ticket's story (as acceptance criteria), not separate stories.
- Each ticket key may be refined by at most ONE story.

STORY RULES
- Story statement: as_a / i_want / so_that, from the user's point of view. Technical stories use the
  system or an operator as the actor (kind "enabler").
- kind: "story" (user-facing), "enabler" (technical groundwork), "bug_fix".
- acceptance_criteria: at least 2 for stories and enablers, at least 1 for bug fixes. Each is
  Given / When / Then, concrete and testable — real values from the evidence (e.g. "within 10 seconds",
  "after 5 minutes"). Include the main failure/edge case.
- nfrs: only testable requirements implied by the tickets, decisions, blueprint or baseline
  (e.g. "No card numbers are stored; only gateway tokens"). category: performance|security|
  reliability|scalability|compliance|usability|other.
- components: names of baseline/blueprint components this story touches — exact names only.
- Decisions recorded on the work item are binding: reflect them in AC or NFRs.
- Never resolve an open question or conflict yourself: copy it into the story's open_questions.
- COVER THE TICKET: every requirement stated in a source ticket (each sentence of the description
  and each existing acceptance criterion, e.g. "include all visible columns", "respect active
  filters") must be tested by at least one acceptance criterion.
- VALUES: only numbers, limits and timings written in the source tickets or decisions are facts.
  Values that appear only in the blueprint or baseline are design PROPOSALS — write them as
  "<proposed: 2 seconds>". Never make up a value; if one is needed, write "<to be confirmed>".
- OPEN QUESTIONS: if an open question affects behaviour (e.g. "email or direct download?"), keep the
  acceptance criteria neutral on that point instead of choosing an answer.
- Acceptance criteria describe OBSERVABLE behaviour (what a user, API caller or tester can check),
  not implementation (no table, index, class or library names — those belong in the design).
- NFRs must be relevant to THIS work item. Do not copy unrelated constraints (e.g. payment rules
  onto a reporting story).
- confidence 0-1: 0.9+ only when tickets and blueprint are explicit.

Return ONLY a JSON object in exactly this shape:
{
  "stories": [
    {
      "work_item": "W1",
      "refines": "KAN-21",
      "kind": "story|enabler|bug_fix",
      "title": "short imperative title",
      "as_a": "...", "i_want": "...", "so_that": "...",
      "acceptance_criteria": [{"given": "...", "when": "...", "then": "..."}],
      "nfrs": [{"category": "...", "requirement": "..."}],
      "components": [],
      "open_questions": [],
      "out_of_scope": [],
      "confidence": 0.0
    }
  ]
}
Keep each field short. Use [] when nothing applies.
""".strip()


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + " ...[truncated]"


def baseline_component_text(baseline: Optional[dict]) -> str:
    if not baseline:
        return "No architecture baseline available."
    origin = {"discovered": "existing system (from client documents)",
              "proposed": "proposed target design"}.get(baseline.get("origin"), "")
    lines = [f"Architecture baseline — {origin}", "Components:"]
    lines += [f"- {c['name']}: {c.get('responsibility') or ''}" for c in baseline.get("components") or []]
    if baseline.get("integrations"):
        lines.append("Integrations: " + ", ".join(baseline["integrations"]))
    if baseline.get("constraints"):
        lines.append("Constraints: " + "; ".join(baseline["constraints"]))
    if baseline.get("nfrs"):
        lines.append("Project NFRs: " + "; ".join(f"{n['category']}: {n['requirement']}" for n in baseline["nfrs"]))
    return "\n".join(lines)


def _blueprint_text(bp: Optional[dict]) -> List[str]:
    if not bp:
        return ["Blueprint: none (design not available — keep technical AC generic)"]
    lines = [f"Blueprint ({bp.get('mode')}): {bp.get('summary') or ''}"]
    if bp.get("components"):
        lines.append("  Components: " + "; ".join(
            f"{c['name']} [{c.get('change')}] {c.get('what_changes') or ''}".strip() for c in bp["components"]))
    if bp.get("interfaces"):
        lines.append("  Interfaces: " + "; ".join(
            f"{i['name']} ({i.get('kind')}): {i.get('purpose') or ''}" for i in bp["interfaces"]))
    if bp.get("data_changes"):
        lines.append("  Data: " + "; ".join(f"{d.get('store')}: {d.get('change')}" for d in bp["data_changes"]))
    if bp.get("integrations"):
        lines.append("  Integrations: " + "; ".join(f"{i['system']}: {i.get('change')}" for i in bp["integrations"]))
    if bp.get("nfrs"):
        lines.append("  NFRs: " + "; ".join(f"{n['category']}: {n['requirement']}" for n in bp["nfrs"]))
    if bp.get("recommended_option"):
        lines.append(f"  Recommended option: {bp['recommended_option']}")
    if bp.get("risks"):
        lines.append("  Risks: " + "; ".join(bp["risks"]))
    if bp.get("open_questions"):
        lines.append("  Design questions: " + "; ".join(bp["open_questions"]))
    return lines


def build_spec_prompt(epic: dict, work_items: List[dict], aliases: List[str], baseline: Optional[dict]) -> str:
    lines = [baseline_component_text(baseline), "",
             f"EPIC: {epic.get('title')}" + (f" ({epic['ref']})" if epic.get("ref") else ""),
             f"Epic intent: {epic.get('intent') or '-'}", ""]
    for alias, w in zip(aliases, work_items):
        lines += [
            "=" * 60,
            f"{alias} | {w.get('signal_type')} | {w['title']} | readiness: {w.get('readiness')}",
            f"Intent: {w.get('business_intent') or '-'}",
            f"Summary: {w.get('summary') or '-'}",
        ]
        for key in ("decisions", "conflicts", "gaps", "open_questions"):
            if w.get(key):
                lines.append(f"{key.replace('_', ' ').capitalize()}: {'; '.join(w[key])}")
        if w.get("related"):
            lines.append("Depends on / related: " + "; ".join(f"{r['ref']} {r['title']}" for r in w["related"]))
        lines += _blueprint_text(w.get("blueprint"))
        lines.append("Source tickets:")
        for m in w.get("members") or []:
            head = f"- {m.get('ref')} [{m.get('issue_type') or '-'}"
            head += f", subtask of {m['parent_key']}" if m.get("parent_key") and m.get("parent_key") != m.get("epic_key") else ""
            lines.append(head + f"] {m.get('title')}")
            lines.append("  " + _clip(m.get("content"), MAX_TICKET_CHARS).replace("\n", "\n  "))
    lines.append("\nReturn the JSON object with stories for EVERY work item: "
                 + ", ".join(aliases) + ".")
    return "\n".join(lines)