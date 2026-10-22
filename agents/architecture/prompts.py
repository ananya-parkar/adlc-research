# agents/architecture/prompts.py
"""
Prompts for the Architecture Discovery & Blueprint Agent (v2).

Step A — ARCHITECTURE BASELINE (once per project)
  * discovery mode : the client supplied architecture material (design docs,
                     Confluence architecture pages). Reconstruct the CURRENT
                     architecture from it. Nothing may be invented.
  * creation mode  : no architecture material exists. Propose ONE high-level
                     solution architecture that all candidate work items share,
                     so every blueprint (and later every user story) designs
                     against the same components.

Step B — BLUEPRINT (once per eligible candidate work item)
  How this one change fits the baseline: which components change, new
  interfaces, data changes, integrations, NFRs, risks, decisions. Shaped for
  the Spec Synthesizer, which turns it into stories and acceptance criteria.

Short ids (D1 = document, C1 = work item, KAN-21 = ticket) are used instead
of UUIDs; the agent maps them back.
"""

from typing import Dict, List, Optional

MAX_DOC_CHARS = 6000
MAX_TICKET_CHARS = 1800

# ----------------------------------------------------------------------
# Step A — baseline
# ----------------------------------------------------------------------
_BASELINE_SCHEMA = """
{
  "summary": "2-3 sentences describing the system",
  "components": [
    {"name": "...", "type": "frontend|backend|service|database|queue|external|other",
     "responsibility": "...", "technology": "string or null", "status": "existing|planned|proposed",
     "evidence": ["D1"]}
  ],
  "data_stores": [{"name": "...", "technology": "string or null", "holds": "...", "evidence": []}],
  "integrations": [{"name": "...", "purpose": "...", "direction": "inbound|outbound|both", "evidence": []}],
  "tech_stack": [{"layer": "...", "choice": "...", "evidence": []}],
  "deployment": [{"item": "...", "evidence": []}],
  "constraints": [{"item": "...", "evidence": []}],
  "nfrs": [{"category": "performance|security|reliability|scalability|compliance|usability|other",
            "requirement": "...", "evidence": []}],
  "gaps": ["important things the material does not say"],
  "open_questions": ["questions an architect must answer"]
}
""".strip()

BASELINE_DISCOVERY_SYSTEM_PROMPT = f"""
You are the Architecture Discovery Agent in a supervised software-planning pipeline.

The client supplied architecture material (design documents, wiki pages). Reconstruct the
architecture it describes: components, data stores, integrations, technology, deployment,
constraints and non-functional requirements.

RULES
- Use ONLY what the material states. Never invent components, technologies or topology.
- Every item must cite the document id(s) it came from in "evidence" (e.g. ["D1"]).
- status: "existing" if described as current, "planned" if described as future/target.
- Anything important the material leaves out goes in "gaps"; questions for the architect go in
  "open_questions".

Return ONLY a JSON object in exactly this shape:
{_BASELINE_SCHEMA}
Keep entries short (one line each). Use [] when nothing applies.
""".strip()

BASELINE_CREATION_SYSTEM_PROMPT = f"""
You are the Architecture Design Agent in a supervised software-planning pipeline.

No architecture documentation exists for this project. From the candidate work items below,
propose ONE high-level solution architecture that all of them can share, so that later design
and user stories are consistent (e.g. one payment integration used by every payment feature,
one notification service used by every email feature).

RULES
- Every component must have status "proposed".
- In "evidence" list the work item ids (e.g. ["C3", "C7"]) that need the component.
- Name third-party systems only when a work item names them (e.g. Razorpay, SendGrid).
  Otherwise use a generic name ("Payment gateway") and technology null.
- Do not choose programming languages, clouds or databases unless a work item states them;
  put those choices in "open_questions" instead.
- Keep it high level: typically 4-10 components.

Return ONLY a JSON object in exactly this shape:
{_BASELINE_SCHEMA}
Keep entries short (one line each). Use [] when nothing applies.
""".strip()


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + " ...[truncated]"


def build_baseline_discovery_prompt(docs: List[dict], aliases: List[str]) -> str:
    lines = ["Architecture material supplied by the client:", ""]
    for alias, doc in zip(aliases, docs):
        lines += [
            f"ID: {alias}",
            f"SOURCE: {doc.get('source_type')} {doc.get('source_ref') or ''}".rstrip(),
            f"TITLE: {doc.get('title')}",
            "CONTENT:",
            _clip(doc.get("content"), MAX_DOC_CHARS),
            "-" * 60,
        ]
    lines.append("\nReturn the JSON object.")
    return "\n".join(lines)


def build_baseline_creation_prompt(cwis: List[dict], aliases: List[str]) -> str:
    lines = ["Candidate work items for this project:", ""]
    for alias, cwi in zip(aliases, cwis):
        lines.append(f"{alias} | {cwi.get('signal_type')} | {cwi.get('title')} | "
                     f"sources: {', '.join(cwi.get('source_refs') or []) or '-'}")
        if cwi.get("business_intent"):
            lines.append(f"   intent: {cwi['business_intent']}")
        if cwi.get("summary"):
            lines.append(f"   summary: {_clip(cwi['summary'], 300)}")
        if cwi.get("decisions"):
            lines.append(f"   decisions: {'; '.join(cwi['decisions'])}")
        if cwi.get("dependencies"):
            lines.append(f"   dependencies: {'; '.join(cwi['dependencies'])}")
    lines.append("\nReturn the JSON object.")
    return "\n".join(lines)


# ----------------------------------------------------------------------
# Step B — per work item blueprint
# ----------------------------------------------------------------------
BLUEPRINT_SYSTEM_PROMPT = """
You are the Architecture Blueprint Agent in a supervised software-planning pipeline.

You receive ONE candidate work item, its source tickets, related work, and the project's
ARCHITECTURE BASELINE. Describe how this change fits that architecture. Your output is used by
the Spec Synthesizer to write user stories, acceptance criteria and NFRs, so be concrete.

RULES
- Design against the baseline. Reuse its components and integrations by their exact names.
  change = "use" (calls it unchanged), "modify" (changes it) or "new" (does not exist yet).
- Do not invent technologies or systems that are neither in the baseline nor in the tickets.
- nfrs: only requirements implied by the evidence or the baseline (e.g. "status update within
  10 s", "no card data stored"). Each should be testable.
- options: include 2 only when there is a real design choice; otherwise [] and set
  recommended_option to null.
- evidence: ticket refs (e.g. "KAN-21") or document ids (e.g. "D1") that support the item.
- Unknowns become open_questions; never guess silently.
- confidence 0-1: 0.9+ only when tickets and baseline are explicit; below 0.6 when you had to
  assume most of the design.

Return ONLY a JSON object in exactly this shape:
{
  "summary": "2-3 sentences: what changes in the system for this work item",
  "components": [{"name": "...", "change": "use|modify|new", "what_changes": "...", "evidence": []}],
  "interfaces": [{"name": "...", "kind": "api|event|webhook|ui|batch|other", "purpose": "...",
                  "inputs": [], "outputs": []}],
  "data_changes": [{"store": "...", "change": "..."}],
  "integrations": [{"system": "...", "change": "...", "evidence": []}],
  "nfrs": [{"category": "performance|security|reliability|scalability|compliance|usability|other",
            "requirement": "...", "evidence": []}],
  "options": [{"name": "...", "pros": [], "cons": []}],
  "recommended_option": null,
  "adr_candidates": [{"title": "...", "decision": "...", "reason": "..."}],
  "risks": [{"risk": "...", "impact": "low|medium|high", "mitigation": "..."}],
  "assumptions": [],
  "open_questions": [],
  "confidence": 0.0
}
Keep entries short. Use [] when nothing applies.
""".strip()


def baseline_as_text(baseline: Optional[dict], origin: Optional[str]) -> str:
    """Compact, readable baseline for the blueprint prompt (not the raw JSON)."""
    if not baseline:
        return "No architecture baseline is available. Design minimally and list assumptions."
    head = {
        "discovered": "Discovered from client architecture documents (treat as the CURRENT system).",
        "proposed": "Proposed by the agent because no documentation exists (treat as the TARGET design).",
    }.get(origin or "", "")
    lines = [head, f"Summary: {baseline.get('summary', '')}", "Components:"]
    for c in baseline.get("components") or []:
        tech = f"; {c['technology']}" if c.get("technology") else ""
        lines.append(f"- {c.get('name')} ({c.get('type')}{tech}, {c.get('status')}): {c.get('responsibility')}")
    for key, label, fmt in (
        ("data_stores", "Data stores", lambda d: f"{d.get('name')}: {d.get('holds')}"),
        ("integrations", "Integrations", lambda d: f"{d.get('name')}: {d.get('purpose')}"),
        ("constraints", "Constraints", lambda d: d.get("item")),
        ("nfrs", "Project NFRs", lambda d: f"{d.get('category')}: {d.get('requirement')}"),
    ):
        items = baseline.get(key) or []
        if items:
            lines.append(f"{label}:")
            lines += [f"- {fmt(i)}" for i in items]
    return "\n".join(lines)


def build_blueprint_prompt(cwi: dict, member_docs: List[dict], member_aliases: List[str],
                           related: List[dict], baseline_text: str, doc_aliases: Dict[str, str]) -> str:
    lines = ["ARCHITECTURE BASELINE", baseline_text, ""]
    if doc_aliases:
        lines.append("Baseline documents: " + ", ".join(f"{a} = {t}" for a, t in doc_aliases.items()))
        lines.append("")
    lines += [
        "WORK ITEM",
        f"Title: {cwi.get('title')}",
        f"Type: {cwi.get('signal_type')} | Readiness: {cwi.get('readiness')}",
        f"Intent: {cwi.get('business_intent') or '-'}",
        f"Summary: {cwi.get('summary') or '-'}",
    ]
    for key in ("decisions", "gaps", "dependencies", "open_questions"):
        if cwi.get(key):
            lines.append(f"{key.replace('_', ' ').capitalize()}: {'; '.join(cwi[key])}")
    if related:
        lines.append("Related work: " + "; ".join(
            f"{r['ref']} {r['title']} ({r.get('signal_type') or '-'})" for r in related))
    lines += ["", "SOURCE TICKETS / DOCUMENTS"]
    for alias, doc in zip(member_aliases, member_docs):
        lines += [f"ID: {alias}", f"TITLE: {doc.get('title')}",
                  _clip(doc.get("content"), MAX_TICKET_CHARS), "-" * 40]
    lines.append("\nReturn the JSON object for this one work item.")
    return "\n".join(lines)