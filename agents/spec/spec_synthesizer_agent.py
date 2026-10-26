# agents/spec/spec_synthesizer_agent.py
"""
Spec Synthesizer (Planning phase)

Input  : eligible candidate work items (CWIs) + their architecture blueprints + the
         project architecture baseline, grouped by epic (build_spec_package).
Output : `spec_story` artifacts — backlog-ready user stories with Given/When/Then
         acceptance criteria, testable NFRs, components, dependencies, open questions.

Key behaviour
  * One LLM call per epic group (max 5 work items) → consistent stories, fewer calls.
    If a group fails, it is retried per work item.
  * REFINE, don't duplicate: a story that improves an existing Jira ticket carries
    refines="KAN-21"; new stories only for genuinely missing pieces.
  * Code rules (deterministic, not left to the LLM):
      - every work item gets ≥1 valid story; AC must be complete Given/When/Then
        (≥2 for stories/enablers, ≥1 for bug fixes)
      - refines must be one of the work item's own tickets, each ticket refined once
      - components must match baseline/blueprint names (others are flagged)
      - conflicts and open questions are carried into stories, never silently resolved
      - dependencies come from Discovery's dependency data
  * Resumable: work items whose stories are still current are skipped.
  * Rate limits / quota handled like the Architecture agent (exit 3 = stopped on quota).

Usage
    python -m agents.spec.spec_synthesizer_agent --project-id KAN --dry-run --limit 3
    python -m agents.spec.spec_synthesizer_agent --project-id KAN
    python -m agents.spec.spec_synthesizer_agent --project-id KAN --force
"""

import argparse
import hashlib
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from dotenv import load_dotenv

load_dotenv()

from agents.common.llm_calls import GenerationFailed, QuotaExhausted, llm_json  # noqa: E402
from agents.spec.prompts import SPEC_SYSTEM_PROMPT, build_spec_prompt  # noqa: E402
from substrate.artifact_registry import supersede_artifacts, upsert_artifact  # noqa: E402
from substrate.context_assembler import build_spec_package  # noqa: E402
from substrate.context_registry import record_provenance  # noqa: E402
from substrate.vector_store import store_chunks  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
for noisy in ("httpx", "httpcore", "sentence_transformers", "urllib3", "groq", "openai", "anthropic"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

STORY_TYPE = "spec_story"
GROUP_MAX_TOKENS = int(os.environ.get("SPEC_GROUP_MAX_TOKENS", 5000))
SINGLE_MAX_TOKENS = int(os.environ.get("SPEC_SINGLE_MAX_TOKENS", 3000))
REVIEW_CONFIDENCE = 0.70
KINDS = {"story", "enabler", "bug_fix"}
NFR_CATEGORIES = {"performance", "security", "reliability", "scalability", "compliance", "usability", "other"}
RESULTS_DIR = Path(__file__).resolve().parents[2] / "storage" / "spec_results"
_REF_RE = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
def _hash(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _strs(value) -> List[str]:
    return [str(v).strip() for v in value if str(v).strip()] if isinstance(value, list) else []


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:48] or "story"


def _default_kind(cwi: dict) -> str:
    return {"bug": "bug_fix", "enabler": "enabler", "tech_debt": "enabler"}.get(cwi.get("signal_type"), "story")


def _cwi_fingerprint(cwi: dict, baseline_fp: Optional[str]) -> str:
    return _hash(baseline_fp, (cwi.get("blueprint") or {}).get("fingerprint"),
                 {k: cwi.get(k) for k in ("title", "summary", "business_intent", "decisions", "conflicts",
                                          "gaps", "dependencies", "open_questions", "source_refs")})


def _depends_on(cwi: dict) -> List[str]:
    """Refs this work item is blocked by (from Discovery's dependency data)."""
    out = []
    for dep in cwi.get("dependencies") or []:
        low = dep.lower()
        if "blocks" in low and "blocked" not in low:
            continue  # this item blocks others — not a dependency of ours
        out += [r for r in _REF_RE.findall(dep) if r not in cwi.get("source_refs", [])]
    return sorted(set(out))


# ----------------------------------------------------------------------
# validation
# ----------------------------------------------------------------------
_DASHES = dict.fromkeys(map(ord, "\u2010\u2011\u2012\u2013\u2014\u2212\ufe63\uff0d"), "-")


def _norm_key(value) -> Optional[str]:
    """'KAN‑24 ' (non-breaking hyphen, spaces, lowercase) -> 'KAN-24'."""
    if not value or not isinstance(value, str):
        return None
    key = value.translate(_DASHES).strip().upper().replace(" ", "")
    return key or None


def _match_component(name: str, known: Dict[str, str]) -> Optional[str]:
    """Exact match, else a unique known name that contains it or is contained in it ('PostgreSQL' → 'PostgreSQL Database')."""
    low = name.strip().lower()
    if low in known:
        return known[low]
    hits = {v for k, v in known.items() if len(low) >= 4 and (low in k or k in low)}
    return hits.pop() if len(hits) == 1 else None


def _clean_story(raw: dict, cwi: dict, known_components: Dict[str, str], used_refines: set) -> Optional[dict]:
    if not isinstance(raw, dict) or not str(raw.get("title") or "").strip():
        return None
    kind = raw.get("kind") if raw.get("kind") in KINDS else _default_kind(cwi)

    ac = []
    for item in raw.get("acceptance_criteria") or []:
        if isinstance(item, dict) and all(str(item.get(k) or "").strip() for k in ("given", "when", "then")):
            ac.append({k: re.sub(r"^\s*(given|when|then|and)\b[\s,:]*", "", str(item[k]).strip(), flags=re.I)
                       for k in ("given", "when", "then")})
    if len(ac) < (1 if kind == "bug_fix" else 2):
        return None
    if kind != "bug_fix" and not all(str(raw.get(k) or "").strip() for k in ("as_a", "i_want", "so_that")):
        return None

    member_refs = {m.get("ref") for m in cwi.get("members") or [] if m.get("ref")}
    refines = _norm_key(raw.get("refines"))
    refines = refines if refines in member_refs and refines not in used_refines else None
    if refines:
        used_refines.add(refines)

    components, unmatched = [], []
    for name in _strs(raw.get("components")):
        canonical = _match_component(name, known_components)
        (components if canonical else unmatched).append(canonical or name)

    nfrs = []
    for n in raw.get("nfrs") or []:
        if isinstance(n, dict) and str(n.get("requirement") or "").strip():
            cat = n.get("category") if n.get("category") in NFR_CATEGORIES else "other"
            nfrs.append({"category": cat, "requirement": str(n["requirement"]).strip()})

    try:
        confidence = max(0.0, min(1.0, float(raw.get("confidence", 0.0))))
    except (TypeError, ValueError):
        confidence = 0.0

    return {
        "title": str(raw["title"]).strip(),
        "kind": kind,
        "refines": refines,
        "as_a": str(raw.get("as_a") or "").strip(),
        "i_want": str(raw.get("i_want") or "").strip(),
        "so_that": str(raw.get("so_that") or "").strip(),
        "acceptance_criteria": ac,
        "nfrs": nfrs,
        "components": sorted(set(components)),
        "unmatched_components": sorted(set(unmatched)),
        "open_questions": _strs(raw.get("open_questions")),
        "out_of_scope": _strs(raw.get("out_of_scope")),
        "confidence": confidence,
    }


def _make_validator(work_items: List[dict], aliases: List[str], known_components: Dict[str, str]):
    alias_to_cwi = dict(zip(aliases, work_items))

    def validate(raw: dict) -> Dict[str, List[dict]]:
        if not isinstance(raw, dict) or not isinstance(raw.get("stories"), list):
            raise ValueError("answer has no 'stories' list")
        out: Dict[str, List[dict]] = {a: [] for a in aliases}
        used = {a: set() for a in aliases}
        dropped = 0
        for s in raw["stories"]:
            alias = (s or {}).get("work_item") if isinstance(s, dict) else None
            if alias not in alias_to_cwi:
                dropped += 1
                continue
            story = _clean_story(s, alias_to_cwi[alias], known_components, used[alias])
            if story:
                out[alias].append(story)
            else:
                dropped += 1
        missing = [a for a, stories in out.items() if not stories]
        if missing:
            raise ValueError(f"no valid stories for {', '.join(missing)} ({dropped} invalid dropped)")
        if dropped:
            logger.warning("Dropped %d invalid story/stories (incomplete AC or story statement)", dropped)
        return out

    return validate


# ----------------------------------------------------------------------
# post-processing (deterministic rules)
# ----------------------------------------------------------------------
_NUM_RE = re.compile(
    r"(?<![\w.\-])(\d{1,3}(?:[ ,\u00a0\u202f]\d{3})+|\d+(?:\.\d+)?)\s*"
    r"(%|ms\b|seconds?\b|secs?\b|s\b|minutes?\b|mins?\b|hours?\b|h\b|days?\b|k\b|rows\b|records\b|"
    r"widgets\b|items\b|users\b|attempts\b|mb\b|gb\b)?", re.I)
_HTTP_WORDS = re.compile(r"\s*(ok|created|accepted|no content|bad request|unauthori[sz]ed|forbidden|not found|"
                         r"conflict|too many|internal server error|service unavailable)\b", re.I)
_PLACEHOLDER = re.compile(r"(<[^>]*>)")
GREENFIELD_STORY_CAP = 0.85
UNVERIFIED_STORY_CAP = 0.75

# topic -> terms. A story element mentioning a topic that the work item's evidence never mentions is dropped.
TOPIC_TERMS = {
    "card data / PCI": ("pci", "card number", "card data", "cardholder", "card details"),
    "payments": ("payment", "razorpay", "upi", "refund", "checkout"),
    "OTP / SMS": ("otp", "sms"),
    "GDPR / privacy law": ("gdpr", "ccpa", "hipaa"),
}
_STOP = set("should would could which their there these those about after before shall must every other "
            "please where while being doing users".split())
_REQ_HINT = re.compile(r"\b(should|must|shall|needs? to|required?|include|respect|only)\b", re.I)


def _is_http_code(text: str, m: re.Match) -> bool:
    if not re.fullmatch(r"[1-5]\d\d", m.group(1)):
        return False
    before = text[max(0, m.start() - 30):m.start()]
    return bool(re.search(r"(http|status|code|error|returns?|returned|responds?(\s+with)?|respond\s+with|"
                          r"rejects?\s+with|fails?\s+with)\s*(an?\s+)?$", before, re.I)
                or _HTTP_WORDS.match(text, m.end(1))
                or re.match(r"\s*(error|response|status)\b", text[m.end(1):], re.I))


def _norm(num: str) -> str:
    return re.sub(r"[ ,\u00a0\u202f]", "", num)


def _numbers(text: str) -> Dict[str, str]:
    """{normalized number: display text}; ignores 0/1, HTTP status codes and <…> placeholders."""
    out = {}
    for part in _PLACEHOLDER.split(text or ""):
        if part.startswith("<"):
            continue
        for m in _NUM_RE.finditer(part):
            norm = _norm(m.group(1))
            if norm in ("0", "1") or _is_http_code(part, m):
                continue
            out.setdefault(norm, m.group(0).strip())
    return out


def _mark_proposed(text: str, evidence: set, found: Dict[str, str]) -> str:
    """Wrap every number that is not in the evidence as <proposed: …>."""
    pieces = []
    for part in _PLACEHOLDER.split(text or ""):
        if part.startswith("<"):
            pieces.append(part)
            continue

        def repl(m):
            norm = _norm(m.group(1))
            if norm in ("0", "1") or norm in evidence or _is_http_code(part, m):
                return m.group(0)
            shown = m.group(0).strip()
            found.setdefault(norm, shown)
            trail = m.group(0)[len(m.group(0).rstrip()):]
            return f"<proposed: {shown}>{trail}"

        pieces.append(_NUM_RE.sub(repl, part))
    return "".join(pieces)


def _words_for_signs(text: str) -> str:
    """'<2 s' -> 'under 2 s', '>10 000' -> 'over 10 000' (so they don't collide with <proposed: …>)."""
    text = re.sub(r"<=\s*(?=\d)", "at most ", text or "")
    text = re.sub(r">=\s*(?=\d)", "at least ", text)
    text = re.sub(r"<\s*(?=\d)", "under ", text)
    return re.sub(r">\s*(?=\d)", "over ", text)


def _evidence_text(cwi: dict, baseline: Optional[dict], with_blueprint: bool = True) -> str:
    parts = [cwi.get("title") or "", cwi.get("summary") or "", cwi.get("business_intent") or "",
             " ".join(cwi.get("decisions") or []), " ".join(cwi.get("conflicts") or [])]
    parts += [(m.get("title") or "") + " " + (m.get("content") or "") + " " + (m.get("acceptance_criteria") or "")
              for m in cwi.get("members") or []]
    if with_blueprint and cwi.get("blueprint"):
        parts.append(json.dumps(cwi["blueprint"], ensure_ascii=False))
    if (baseline or {}).get("origin") == "discovered":  # client documents are evidence too
        parts += [" ".join(baseline.get("constraints") or []),
                  " ".join(n.get("requirement", "") for n in baseline.get("nfrs") or [])]
    return " ".join(parts)


def _evidence_numbers(cwi: dict, baseline: Optional[dict]) -> set:
    """Numbers count as facts only from raw tickets, decisions, conflicts and client documents —
    not from LLM-written summaries or blueprints."""
    parts = [" ".join(cwi.get("decisions") or []), " ".join(cwi.get("conflicts") or [])]
    parts += [(m.get("content") or "") + " " + (m.get("acceptance_criteria") or "") for m in cwi.get("members") or []]
    if (baseline or {}).get("origin") == "discovered":
        parts += [" ".join(baseline.get("constraints") or []),
                  " ".join(n.get("requirement", "") for n in baseline.get("nfrs") or [])]
    return set(_numbers(" ".join(parts)))


def _leaked_topics(text: str, evidence_low: str) -> List[str]:
    low = text.lower()
    return [topic for topic, terms in TOPIC_TERMS.items()
            if any(t in low for t in terms) and not any(t in evidence_low for t in terms)]


def _strip_topic_clauses(text: str, evidence_low: str) -> str:
    """Remove only the clauses that mention an off-topic subject; keep the rest of the sentence."""
    clauses = re.split(r"(;|,\s*and\s+|\s+and\s+|,\s*(?=no\b|not\b|any\b|without\b)|\.\s+)", text or "")
    kept, sep = [], ""
    for i in range(0, len(clauses), 2):
        part = clauses[i]
        if part and not _leaked_topics(part, evidence_low):
            kept.append((sep if kept else "") + part)
        sep = clauses[i + 1] if i + 1 < len(clauses) else ""
    out = "".join(kept).strip(" ,;")
    return out


def _requirement_sentences(cwi: dict) -> List[tuple]:
    """(ticket ref, sentence) for sentences in ticket descriptions that state a requirement."""
    out = []
    for m in cwi.get("members") or []:
        content = m.get("content") or ""
        desc = content.split("Description:\n", 1)[1] if "Description:\n" in content else content
        desc = re.split(r"\n\n(?:Acceptance criteria|Links|Subtasks|Recent comments|Quality flags):", desc)[0]
        for sent in re.split(r"(?<=[.!?])\s+|\n+", desc):
            sent = sent.strip(" -•\t")
            if len(sent) > 12 and not sent.lower().startswith("as a") and _REQ_HINT.search(sent):
                out.append((m.get("ref"), sent))
    return out


def _covered(sentence: str, text_low: str) -> bool:
    words = [w for w in re.findall(r"[a-z]{5,}", sentence.lower()) if w not in _STOP]
    if not words:
        return True
    hits = sum(1 for w in words if w[:5] in text_low)
    return hits / len(words) >= 0.5


def finalize_stories(cwi: dict, stories: List[dict], baseline: Optional[dict] = None,
                     group_evidence: str = "") -> dict:
    """Carry conflicts/questions, attach dependencies, verify values, compute review flags and coverage."""
    all_q = " ".join(q.lower() for s in stories for q in s["open_questions"])
    for text in [f"Resolve conflict: {c}" for c in cwi.get("conflicts") or []] + list(cwi.get("open_questions") or []):
        key = text.lower().replace("resolve conflict: ", "")[:40]
        if key not in all_q:
            stories[0]["open_questions"].append(text)
            all_q += " " + text.lower()

    deps = _depends_on(cwi)
    has_bp = bool(cwi.get("blueprint"))
    evidence = _evidence_numbers(cwi, baseline)
    # topics are judged against the whole epic group (e.g. a card decision on 'Save cards' is relevant to
    # the Razorpay enabler in the same epic), never against the project-wide baseline
    evidence_low = (_evidence_text(cwi, None, with_blueprint=False) + " " + group_evidence).lower()
    greenfield = (cwi.get("blueprint") or {}).get("mode") != "extend_existing"
    min_ac = {"bug_fix": 1}
    for s in stories:
        s["depends_on"] = deps

        # 1. drop content about topics this work item never mentions (e.g. PCI on a CSV export)
        s["removed_content"] = []
        kept_nfrs = []
        for n in s["nfrs"]:
            topics = _leaked_topics(n["requirement"], evidence_low)
            (s["removed_content"].append(f"NFR '{n['requirement']}' ({', '.join(topics)})") if topics
             else kept_nfrs.append(n))
        s["nfrs"] = kept_nfrs
        kept_ac = []
        for ac in s["acceptance_criteria"]:
            topics = _leaked_topics(f"{ac['given']} {ac['when']} {ac['then']}", evidence_low)
            if not topics:
                kept_ac.append(ac)
                continue
            trimmed = {k: _strip_topic_clauses(ac[k], evidence_low) for k in ("given", "when", "then")}
            if all(trimmed.values()):
                kept_ac.append(trimmed)
                s["removed_content"].append(f"clause from AC 'then {ac['then'][:50]}…' ({', '.join(topics)})")
            else:
                s["removed_content"].append(f"AC 'then {ac['then'][:60]}' ({', '.join(topics)})")
        if len(kept_ac) >= min_ac.get(s["kind"], 2):
            s["acceptance_criteria"] = kept_ac
        elif any(r.startswith("AC ") for r in s["removed_content"]):
            s["removed_content"] = [r for r in s["removed_content"] if not r.startswith("AC ")]
            s["removed_content"].append("off-topic AC kept (removing it would leave too few AC)")

        # 2. numbers not in the tickets/decisions become <proposed: …>
        found: Dict[str, str] = {}
        for a in s["acceptance_criteria"]:
            for k in ("given", "when", "then"):
                a[k] = _mark_proposed(_words_for_signs(a[k]), evidence, found)
        for n in s["nfrs"]:
            n["requirement"] = _mark_proposed(_words_for_signs(n["requirement"]), evidence, found)
        # include <proposed: …> markers the model wrote itself
        all_story_text = " ".join([f"{a['given']} {a['when']} {a['then']}" for a in s["acceptance_criteria"]]
                                  + [n["requirement"] for n in s["nfrs"]])
        for marked in re.findall(r"<proposed:\s*([^>]+)>", all_story_text):
            found.setdefault(_norm(marked.split()[0]) if marked.split() else marked, marked.strip())
        s["unverified_values"] = list(dict.fromkeys(found.values()))
        if s["unverified_values"]:
            s["open_questions"].append("Confirm proposed values (not in the tickets): "
                                       + ", ".join(s["unverified_values"]))
            s["confidence"] = min(s["confidence"], UNVERIFIED_STORY_CAP)
        if greenfield:
            s["confidence"] = min(s["confidence"], GREENFIELD_STORY_CAP)

    # 3. every requirement sentence in the tickets must be covered by some story's AC/NFRs
    all_text = " ".join(f"{a['given']} {a['when']} {a['then']}" for st in stories for a in st["acceptance_criteria"])
    all_text += " " + " ".join(n["requirement"] for st in stories for n in st["nfrs"])
    all_low = all_text.lower()
    uncovered_reqs = [f"{ref}: {sent}" for ref, sent in _requirement_sentences(cwi) if not _covered(sent, all_low)]
    top_level = [m["ref"] for m in cwi.get("members") or []
                 if m.get("ref") and not any(m.get("parent_key") == o.get("ref") for o in cwi.get("members") or [])]
    if len(stories) == 1 and not stories[0]["refines"] and len(top_level) == 1:
        stories[0]["refines"] = top_level[0]  # the only story of a one-ticket work item refines that ticket
    refined = {st["refines"] for st in stories if st["refines"]}
    for s in stories:
        s["uncovered_requirements"] = [
            u for u in uncovered_reqs
            if (s["refines"] and u.startswith(f"{s['refines']}:"))
            or (s is stories[0] and u.split(":", 1)[0] not in refined)
        ]
        s["needs_review"] = bool(
            s["confidence"] < REVIEW_CONFIDENCE or s["open_questions"] or s["unmatched_components"]
            or s["uncovered_requirements"] or s["removed_content"] or not has_bp
        )

    refined = {s["refines"] for s in stories if s["refines"]}
    not_refined = [
        m["ref"] for m in cwi.get("members") or []
        if m.get("ref") and m["ref"] not in refined and m.get("parent_key") not in refined
    ]
    # Discovery merged these tickets into this work item as the same need: when another ticket of the
    # same work item is refined, they are duplicates to close/link, not gaps.
    duplicates = [{"ticket": r, "keep": sorted(refined)[0]} for r in not_refined] if refined else []
    uncovered = [] if refined else not_refined
    return {"stories": stories, "uncovered_tickets": uncovered, "duplicate_tickets": duplicates,
            "blueprint_used": has_bp}


# ----------------------------------------------------------------------
# generation
# ----------------------------------------------------------------------
def known_components(baseline: Optional[dict], work_items: List[dict]) -> Dict[str, str]:
    names = [c["name"] for c in (baseline or {}).get("components") or [] if c.get("name")]
    names += [n for n in (baseline or {}).get("integrations") or [] if n]
    for w in work_items:
        names += [c["name"] for c in (w.get("blueprint") or {}).get("components") or [] if c.get("name")]
        names += [i["system"] for i in (w.get("blueprint") or {}).get("integrations") or [] if i.get("system")]
    return {n.lower(): n for n in names}


def generate_group(epic: dict, work_items: List[dict], baseline: Optional[dict], retries: int,
                   group_evidence: Optional[str] = None) -> Dict[str, dict]:
    """Returns {cwi_artifact_id: finalized result}; failed work items are absent. May raise QuotaExhausted."""
    if group_evidence is None:
        group_evidence = " ".join(_evidence_text(w, None, with_blueprint=False) for w in work_items)
    aliases = [f"W{i}" for i in range(1, len(work_items) + 1)]
    comps = known_components(baseline, work_items)
    max_tokens = GROUP_MAX_TOKENS if len(work_items) > 1 else SINGLE_MAX_TOKENS
    try:
        by_alias = llm_json(SPEC_SYSTEM_PROMPT, build_spec_prompt(epic, work_items, aliases, baseline),
                            max_tokens, attempts=retries + 1,
                            validate=_make_validator(work_items, aliases, comps))
    except GenerationFailed as exc:
        if len(work_items) == 1:
            logger.error("Spec generation failed for work item %s: %s", work_items[0]["title"], exc)
            return {}
        logger.warning("Group '%s' failed (%s) — retrying one work item at a time", epic["title"], str(exc)[:120])
        out = {}
        for w in work_items:
            out.update(generate_group(epic, [w], baseline, retries, group_evidence))
        return out
    out = {}
    for a, w in zip(aliases, work_items):
        result = finalize_stories(w, by_alias[a], baseline, group_evidence)
        out[w["artifact_id"]] = fix_coverage(epic, w, baseline, result, retries, group_evidence)
    return out


STORY_FIELDS = ("refines", "kind", "title", "as_a", "i_want", "so_that", "acceptance_criteria", "nfrs",
                "components", "open_questions", "out_of_scope", "confidence")


def fix_coverage(epic: dict, w: dict, baseline: Optional[dict], result: dict, retries: int,
                 group_evidence: str = "") -> dict:
    """One follow-up call when ticket requirements are not tested by any AC. Keeps the better answer."""
    uncovered = sorted({u for s in result["stories"] for u in s["uncovered_requirements"]})
    if not uncovered:
        return result
    previous = [{"work_item": "W1", **{k: s[k] for k in STORY_FIELDS}} for s in result["stories"]]
    user = (build_spec_prompt(epic, [w], ["W1"], baseline)
            + "\n\nYOUR PREVIOUS ANSWER FOR W1:\n" + json.dumps({"stories": previous}, ensure_ascii=False)
            + "\n\nThese requirements from the source tickets are NOT tested by any acceptance criterion:\n"
            + "\n".join(f"- {u}" for u in uncovered)
            + "\nReturn the complete corrected JSON for W1. Add acceptance criteria that explicitly test each "
              "of them; keep everything else that was correct.")
    logger.info("Coverage gap in '%s' — asking for a revision (%d requirement(s))", w["title"], len(uncovered))
    try:
        by_alias = llm_json(SPEC_SYSTEM_PROMPT, user, SINGLE_MAX_TOKENS, attempts=min(2, retries + 1),
                            validate=_make_validator([w], ["W1"], known_components(baseline, [w])))
    except GenerationFailed as exc:
        logger.warning("Coverage revision failed (%s) — keeping the first answer", str(exc)[:120])
        return result
    fixed = finalize_stories(w, by_alias["W1"], baseline, group_evidence)
    still = {u for s in fixed["stories"] for u in s["uncovered_requirements"]}
    if len(still) < len(uncovered):
        logger.info("Coverage revision accepted for '%s' (%d → %d uncovered)", w["title"], len(uncovered), len(still))
        return fixed
    logger.info("Coverage revision did not help for '%s' — keeping the first answer", w["title"])
    return result


# ----------------------------------------------------------------------
# storage
# ----------------------------------------------------------------------
def story_text(s: dict) -> str:
    lines = [f"{s['title']}" + (f" (refines {s['refines']})" if s["refines"] else " (new)")]
    if s["as_a"]:
        lines.append(f"As a {s['as_a']}, I want {s['i_want']} so that {s['so_that']}.")
    lines.append("Acceptance criteria:")
    lines += [f"- Given {a['given']}, when {a['when']}, then {a['then']}." for a in s["acceptance_criteria"]]
    if s["nfrs"]:
        lines.append("NFRs:")
        lines += [f"- [{n['category']}] {n['requirement']}" for n in s["nfrs"]]
    if s["open_questions"]:
        lines.append("Open questions: " + "; ".join(s["open_questions"]))
    return "\n".join(lines)


def store_story(s: dict, cwi: dict, epic: dict, fingerprint: str, project_id: Optional[str]) -> str:
    if not cwi.get("source_id"):
        raise ValueError(f"CWI {cwi['artifact_id']} has no source_id")
    ref = f"spec-story:{cwi['artifact_id']}:{s['refines'] or _slug(s['title'])}"
    aid = upsert_artifact(
        cwi={"title": s["title"], "source_refs": [ref]},
        source_id=cwi["source_id"],
        artifact_type=STORY_TYPE,
        content_override=story_text(s),
        extra_metadata={
            **s,
            "lifecycle": "active",
            "cwi_artifact_id": cwi["artifact_id"],
            "cwi_id": cwi.get("cwi_id"),
            "cwi_title": cwi["title"],
            "epic_ref": epic.get("ref"),
            "epic_title": epic.get("title"),
            "fingerprint": fingerprint,
        },
        project_id=project_id,
    )
    record_provenance(artifact_id=aid, source_id=cwi["source_id"], source_ref=cwi["artifact_id"],
                      extraction_method="spec_synthesizer")
    if s["refines"]:
        record_provenance(artifact_id=aid, source_id=cwi["source_id"], source_ref=s["refines"],
                          extraction_method="spec_synthesizer_refines")
    store_chunks(aid, story_text(s), project_id=project_id,
                 metadata={"artifact_type": STORY_TYPE, "cwi_artifact_id": cwi["artifact_id"]})
    return aid


# ----------------------------------------------------------------------
# report
# ----------------------------------------------------------------------
def print_report(ctx, results: Dict[str, dict], skipped_current: List[str], failures: List[str], pending: List[str]):
    print("\n" + "=" * 80)
    total = sum(len(r["stories"]) for r in results.values())
    print(f"  SPEC REPORT — project {ctx['project_key']}: {total} stories for {len(results)} work items")
    print("=" * 80)
    for group in ctx["groups"]:
        rows = [(w, results[w["artifact_id"]]) for w in group["work_items"] if w["artifact_id"] in results]
        if not rows:
            continue
        print(f"\nEPIC: {group['epic']['title']}" + (f" ({group['epic']['ref']})" if group["epic"]["ref"] else ""))
        for w, r in rows:
            print(f"  ▸ {w['title']}" + ("" if r["blueprint_used"] else "   [no blueprint]"))
            for s in r["stories"]:
                tag = f"refines {s['refines']}" if s["refines"] else "NEW"
                flag = "REVIEW" if s["needs_review"] else "ok"
                print(f"     - {s['title']}  ({s['kind']}, {tag}, {len(s['acceptance_criteria'])} AC, "
                      f"{len(s['nfrs'])} NFR, conf {s['confidence']:.2f}) [{flag}]")
                if s["depends_on"]:
                    print(f"         depends on: {', '.join(s['depends_on'])}")
                if s["open_questions"]:
                    print(f"         open: {'; '.join(s['open_questions'][:2])}")
                if s.get("uncovered_requirements"):
                    print(f"         NOT COVERED: {'; '.join(s['uncovered_requirements'])}")
                if s.get("removed_content"):
                    print(f"         removed (off-topic): {'; '.join(s['removed_content'])}")
                if s.get("unverified_values"):
                    print(f"         unverified values: {', '.join(s['unverified_values'])}")
                if s["unmatched_components"]:
                    print(f"         unknown components: {', '.join(s['unmatched_components'])}")
            for d in r.get("duplicate_tickets") or []:
                print(f"     ≈ {d['ticket']} is a duplicate merged by Discovery — close it or link it to {d['keep']}")
            if r["uncovered_tickets"]:
                print(f"     ! tickets not refined by any story: {', '.join(r['uncovered_tickets'])}")
    if skipped_current:
        print(f"\nAlready current (skipped): {len(skipped_current)} work items")
    if ctx["skipped_work_items"]:
        print("\nNot specified (by rule)")
        for s in ctx["skipped_work_items"]:
            print(f"  - {s['title']}: {s['reason']}")
    if failures:
        print("\nFailed: " + "; ".join(failures))
    if pending:
        print("\nPending (run again later): " + "; ".join(pending))
    print()


def save_results(ctx, results, dry_run) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / f"{ctx['project_key']}_{datetime.now():%Y%m%d_%H%M%S}.json"
    titles = {w["artifact_id"]: w["title"] for g in ctx["groups"] for w in g["work_items"]}
    path.write_text(json.dumps({
        "project_id": ctx["project_id"], "dry_run": dry_run,
        "work_items": [{"cwi_artifact_id": k, "title": titles.get(k), **v} for k, v in results.items()],
        "skipped_work_items": ctx["skipped_work_items"],
    }, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    logger.info("Results saved to %s", path)
    return path


# ----------------------------------------------------------------------
def main() -> int:
    p = argparse.ArgumentParser(description="Spec Synthesizer")
    p.add_argument("--project-id", default=None)
    p.add_argument("--limit", type=int, default=None, help="only the first N eligible work items (testing)")
    p.add_argument("--retries", type=int, default=2)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true", help="regenerate even if stories are current")
    p.add_argument("--include-unready", action="store_true")
    args = p.parse_args()

    path = build_spec_package(project_id=args.project_id, limit=args.limit, include_unready=args.include_unready)
    ctx = json.loads(path.read_text(encoding="utf-8"))["context"]
    c = ctx["counts"]
    logger.info("Spec package built: %s", path)
    logger.info("Package contains %d work items (%d with blueprint) in %d epic group(s); %d skipped by rule",
                c["eligible_cwis"], c["with_blueprint"], c["groups"], c["skipped_cwis"])
    if not c["eligible_cwis"]:
        logger.warning("No eligible work items. Run Discovery (and Architecture) first.")
        return 0
    if not ctx.get("baseline"):
        logger.warning("No architecture baseline found — stories will have generic technical AC.")

    baseline = ctx.get("baseline")
    baseline_fp = (baseline or {}).get("fingerprint")
    existing = ctx["existing_stories"]

    results: Dict[str, dict] = {}
    fingerprints: Dict[str, str] = {}
    skipped_current, failures, pending, keep_ids = [], [], [], []
    stopped = False

    for n, group in enumerate(ctx["groups"], 1):
        todo = []
        for w in group["work_items"]:
            fp = _cwi_fingerprint(w, baseline_fp)
            fingerprints[w["artifact_id"]] = fp
            prev = existing.get(w["artifact_id"]) or []
            if prev and all(x.get("fingerprint") == fp for x in prev) and not args.force:
                skipped_current.append(w["title"])
                keep_ids += [x["artifact_id"] for x in prev]
            else:
                todo.append(w)
        if not todo:
            continue
        if stopped:
            pending += [w["title"] for w in todo]
            keep_ids += [x["artifact_id"] for w in todo for x in existing.get(w["artifact_id"]) or []]
            continue

        logger.info("Group %d/%d: epic '%s' — %d work item(s): %s", n, len(ctx["groups"]), group["epic"]["title"],
                    len(todo), "; ".join(w["title"] for w in todo))
        try:
            out = generate_group(group["epic"], todo, baseline, args.retries)
        except QuotaExhausted as exc:
            logger.error("Provider quota reached — stopping. %s", exc)
            stopped = True
            pending += [w["title"] for w in todo]
            keep_ids += [x["artifact_id"] for w in todo for x in existing.get(w["artifact_id"]) or []]
            continue

        for w in todo:
            if w["artifact_id"] not in out:
                failures.append(w["title"])
                keep_ids += [x["artifact_id"] for x in existing.get(w["artifact_id"]) or []]
                continue
            results[w["artifact_id"]] = out[w["artifact_id"]]
            if args.dry_run:
                continue
            for s in out[w["artifact_id"]]["stories"]:
                try:
                    aid = store_story(s, w, group["epic"], fingerprints[w["artifact_id"]], ctx["project_id"])
                    keep_ids.append(aid)
                    logger.info("Created/updated story '%s' (%s, artifact_id=%s, needs_review=%s)",
                                s["title"], f"refines {s['refines']}" if s["refines"] else "new",
                                aid, s["needs_review"])
                except Exception:
                    logger.exception("Failed to store story '%s'", s["title"])

    if not args.dry_run and not args.limit and ctx["project_id"]:
        old = supersede_artifacts(STORY_TYPE, ctx["project_id"], sorted(set(keep_ids)))
        if old:
            logger.info("Marked %d old story/stories as superseded", old)

    print_report(ctx, results, skipped_current, failures, pending)
    save_results(ctx, results, args.dry_run)
    done = len(results) + len(skipped_current)
    logger.info("Spec Synthesizer complete: %d/%d work items specified%s",
                done, c["eligible_cwis"], " (dry run)" if args.dry_run else "")
    if stopped:
        return 3
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())