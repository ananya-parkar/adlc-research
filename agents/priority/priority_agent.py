# agents/priority/priority_agent.py
"""
Priority Agent (Planning phase) — "ranks backlog by value, effort and risk".

Input  : active spec stories + their Jira priority/labels + Estimation & Risk estimates
         and risk register (build_priority_package).
Output : one `priority_backlog` artifact per project — the ranked backlog proposal for
         the Product Owner gate (review_status = proposed).

Design
  * Ranking is deterministic Python (agents/priority/scoring.py, weights in policy.py).
  * The LLM only writes a short rationale per item; if it fails, a template rationale
    is used — the ranking never depends on the LLM.
  * Stories without estimates or with unresolved blockers are listed as unranked, never
    silently ranked low.
  * Resumable: if inputs and policy are unchanged the stored backlog is kept (--force reruns).

Usage
    python -m agents.priority.priority_agent --project-id KAN --dry-run
    python -m agents.priority.priority_agent --project-id KAN
    python -m agents.priority.priority_agent --project-id KAN --force --no-llm
Exit codes: 0 ok · 2 failed (e.g. no estimates) · 3 LLM quota reached (ranking stored, template rationales)
"""

import argparse
import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

from dotenv import load_dotenv

load_dotenv()

from agents.common.llm_calls import GenerationFailed, QuotaExhausted, llm_json  # noqa: E402
from agents.priority import policy  # noqa: E402
from agents.priority.prompts import (MAX_RATIONALE_WORDS, RATIONALE_SYSTEM_PROMPT,  # noqa: E402
                                     build_rationale_prompt)
from agents.priority.scoring import score_backlog  # noqa: E402
from substrate.artifact_registry import upsert_artifact  # noqa: E402
from substrate.context_assembler import build_priority_package  # noqa: E402
from substrate.context_registry import record_provenance  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
for noisy in ("httpx", "httpcore", "sentence_transformers", "urllib3", "groq", "openai", "anthropic"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

PRIORITY_TYPE = "priority_backlog"
SCHEMA_VERSION = "1.0"
RATIONALE_MAX_TOKENS = 4000
RESULTS_DIR = Path(__file__).resolve().parents[2] / "storage" / "priority_results"


def _json_safe(v):
    """Postgres JSON has no Infinity/NaN and no sets."""
    if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))):
        return "inf" if v > 0 else ("-inf" if v < 0 else None)
    if isinstance(v, set):
        return sorted(_json_safe(x) for x in v)
    if isinstance(v, dict):
        return {k: _json_safe(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_json_safe(x) for x in v]
    return v


def policy_snapshot() -> dict:
    return {k: _json_safe(v) for k, v in vars(policy).items() if k.isupper()}


def _hash(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:16]


# ----------------------------------------------------------------------
# rationale
# ----------------------------------------------------------------------
def template_rationale(it: dict) -> str:
    f = it["factor_breakdown"]
    parts = [f"Value {f['business_value']['score']} ({f['business_value']['source']})"]
    if f["time_criticality"]["score"] >= 4:
        parts.append(f"time-critical ({f['time_criticality']['source']})")
    if f["enablement"]["unblocks"]:
        parts.append("unblocks " + ", ".join(f["enablement"]["unblocks"]))
    if f["risk_reduction"]["risk_ids"]:
        parts.append("reduces " + ", ".join(f["risk_reduction"]["risk_ids"]))
    parts.append(f"~{f['job_size']['expected_hours']:g} h effort")
    text = "; ".join(parts) + "."
    if it.get("ordering_note"):
        text += " " + it["ordering_note"] + "."
    return text


def _make_validator(aliases: List[str]):
    def validate(raw: dict) -> dict:
        if not isinstance(raw, dict) or not isinstance(raw.get("rationales"), list):
            raise ValueError("answer has no 'rationales' list")
        out = {}
        for r in raw["rationales"]:
            if not isinstance(r, dict):
                continue
            alias, text = r.get("item"), str(r.get("rationale") or "").strip()
            if alias in aliases and text:
                words = text.split()
                out[alias] = " ".join(words[:MAX_RATIONALE_WORDS + 10])
        missing = [a for a in aliases if a not in out]
        if missing:
            raise ValueError(f"no rationale for {', '.join(missing[:8])}")
        questions = [str(q).strip() for q in raw.get("open_questions") or [] if str(q).strip()]
        return {"rationales": out, "open_questions": questions[:5]}
    return validate


def add_rationales(ranked: List[dict], use_llm: bool, retries: int) -> tuple:
    """Returns (source, open_questions, quota_hit). Always fills it['rationale']."""
    for it in ranked:
        it["rationale"] = template_rationale(it)
        it["rationale_source"] = "template"
    if not use_llm or not ranked:
        return "template", [], False
    aliases = [f"R{i}" for i in range(1, len(ranked) + 1)]
    try:
        res = llm_json(RATIONALE_SYSTEM_PROMPT, build_rationale_prompt(ranked, aliases),
                       RATIONALE_MAX_TOKENS, attempts=retries + 1, validate=_make_validator(aliases))
    except QuotaExhausted as exc:
        logger.error("Provider quota reached — keeping template rationales. %s", exc)
        return "template", [], True
    except GenerationFailed as exc:
        logger.warning("Rationale generation failed (%s) — keeping template rationales", str(exc)[:160])
        return "template", [], False
    for alias, it in zip(aliases, ranked):
        text = _clean_text(res["rationales"][alias], aliases, ranked)
        note = it.get("ordering_note")
        plain = re.sub(r"[\u2010-\u2015\u2212]", "-", text).lower()
        if note and not any(k in plain for k in ("placed after", "per policy", note.split(" — ")[0].lower())):
            text = text.rstrip(". ") + ". " + note + "."
        it["rationale"] = text
        it["rationale_source"] = "llm"
    questions = [_clean_text(q, aliases, ranked) for q in res["open_questions"]]
    return "llm", questions, False


def _clean_text(text: str, aliases: List[str], ranked: List[dict]) -> str:
    """Replace leaked R-aliases with ticket keys and drop 'KAN-24 (KAN-24)' repeats."""
    text = re.sub(r"\bR\d+\b", lambda m: _alias_to_key(m.group(0), aliases, ranked), text)
    text = re.sub(r"\b([A-Z][A-Z0-9]+-\d+)\s*\(\1\)", r"\1", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def _alias_to_key(alias: str, aliases: List[str], ranked: List[dict]) -> str:
    if alias in aliases:
        it = ranked[aliases.index(alias)]
        return it.get("key") or it["title"][:30]
    return alias


def build_open_questions(scored: dict) -> List[str]:
    name = lambda it: it.get("key") or it["title"][:30]  # noqa: E731
    ranked = scored["ranked"]
    q = []
    for n in scored["notes"]:
        if n.startswith("Circular dependency"):
            q.append("Which comes first? " + n.split(": ", 1)[-1] + " depend on each other")
    no_value = [name(it) for it in ranked if it["factor_breakdown"]["business_value"]["source"].startswith("default")]
    if no_value:
        q.append("Set a Jira priority for " + ", ".join(no_value) + " (business value was defaulted)")
    low = [name(it) for it in ranked
           if (it["factor_breakdown"]["job_size"].get("estimate_confidence") or "").lower()
           in policy.LOW_ESTIMATE_CONFIDENCE]
    if low:
        q.append("Confirm effort for low-confidence estimates: " + ", ".join(low))
    ties = sorted({name(it) for it in ranked if any(r.startswith("Same score") for r in it["review_reasons"])})
    if ties:
        q.append("Tie on a priority boundary — confirm order of " + ", ".join(ties))
    if scored["unranked"]:
        q.append("Resolve blockers for unranked items: " + ", ".join(u.get("key") or u["title"][:30]
                                                                  for u in scored["unranked"]))
    return q


# ----------------------------------------------------------------------
# storage / report
# ----------------------------------------------------------------------
def backlog_text(result: dict) -> str:
    lines = [f"Prioritized backlog — project {result['project_id']} (policy {result['policy_version']})"]
    for it in result["ranked_items"]:
        flag = " [REVIEW]" if it["needs_review"] else ""
        lines.append(f"{it['rank']:>2}. {it['priority_label']} {it.get('key') or 'NEW'} — {it['title']} "
                     f"(score {it['score']}){flag}: {it['rationale']}")
    if result["unranked_or_blocked_items"]:
        lines.append("Not ranked:")
        lines += [f"- {u.get('key') or 'NEW'} — {u['title']}: {u['reason']}" for u in result["unranked_or_blocked_items"]]
    return "\n".join(lines)


def store_backlog(result: dict, ctx: dict, fingerprint: str) -> str:
    source_id = next((s["source_id"] for s in ctx["stories"] if s.get("source_id")), None)
    if not source_id:
        raise ValueError("No source_id on any story — cannot store the backlog")
    project_key = ctx["project_key"]
    aid = upsert_artifact(
        cwi={"title": f"Prioritized backlog — {project_key}", "source_refs": [f"priority-backlog:{project_key}"]},
        source_id=source_id,
        artifact_type=PRIORITY_TYPE,
        content_override=backlog_text(result),
        extra_metadata={**result, "lifecycle": "active", "fingerprint": fingerprint},
        project_id=ctx["project_id"],
    )
    for it in result["ranked_items"] + result["unranked_or_blocked_items"]:
        record_provenance(artifact_id=aid, source_id=source_id, source_ref=it["work_item_id"],
                          extraction_method="priority_agent")
    if ctx["estimation"].get("artifact_id"):
        record_provenance(artifact_id=aid, source_id=source_id, source_ref=ctx["estimation"]["artifact_id"],
                          extraction_method="priority_agent_estimation")
    return aid


def print_report(result: dict):
    print("\n" + "=" * 92)
    print(f"  PRIORITIZED BACKLOG — project {result['project_id']}: {len(result['ranked_items'])} ranked, "
          f"{len(result['unranked_or_blocked_items'])} not ranked   (policy {result['policy_version']})")
    print("=" * 92)
    print(f"{'#':>3} {'P':<3} {'key':<8} {'title':<44} {'score':>6} {'hrs':>6}  review")
    for it in result["ranked_items"]:
        f = it["factor_breakdown"]
        print(f"{it['rank']:>3} {it['priority_label']:<3} {(it.get('key') or 'NEW'):<8} {it['title'][:44]:<44} "
              f"{it['score']:>6} {f['job_size']['expected_hours']:>6g}  {'REVIEW' if it['needs_review'] else 'ok'}")
        print(f"      why: {it['rationale']}")
        for r in it["review_reasons"]:
            print(f"      review: {r}")
    if result["unranked_or_blocked_items"]:
        print("\nNot ranked")
        for u in result["unranked_or_blocked_items"]:
            print(f"  - {u.get('key') or 'NEW'} {u['title']}: {u['reason']}")
    for n in result["notes"]:
        print(f"\nNote: {n}")
    if result["open_questions"]:
        print("\nOpen questions for the PO:")
        for q in result["open_questions"]:
            print(f"  - {q}")
    print()


def save_results(result: dict, dry_run: bool) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / f"{result['project_id'] or 'all'}_{datetime.now():%Y%m%d_%H%M%S}.json"
    path.write_text(json.dumps({**result, "dry_run": dry_run}, indent=2, ensure_ascii=False, default=str),
                    encoding="utf-8")
    logger.info("Results saved to %s", path)
    return path


# ----------------------------------------------------------------------
def main() -> int:
    p = argparse.ArgumentParser(description="Priority Agent")
    p.add_argument("--project-id", default=None)
    p.add_argument("--retries", type=int, default=2)
    p.add_argument("--dry-run", action="store_true", help="rank and save results JSON, write nothing to the DB")
    p.add_argument("--force", action="store_true", help="re-rank even if inputs and policy are unchanged")
    p.add_argument("--no-llm", action="store_true", help="template rationales only (no LLM call)")
    args = p.parse_args()

    path = build_priority_package(project_id=args.project_id)
    ctx = json.loads(path.read_text(encoding="utf-8"))["context"]
    c = ctx["counts"]
    logger.info("Priority package built: %s", path)
    logger.info("Package: %d stories, %d with estimates, %d with Jira priority, %d risks (estimation from %s)",
                c["stories"], c["with_estimate"], c["with_jira_priority"], c["risks"], ctx["estimation"]["source"])
    if not c["stories"]:
        logger.warning("No active spec stories. Run the Spec Synthesizer first.")
        return 0
    if not c["with_estimate"]:
        logger.error("No estimates found. Run Estimation & Risk first: python run_estimation_risk.py --project-id %s",
                     args.project_id)
        return 2

    snapshot = policy_snapshot()
    fingerprint = _hash(ctx["input_fingerprint"], snapshot, SCHEMA_VERSION)
    existing = ctx.get("existing_priority") or {}
    if existing.get("fingerprint") == fingerprint and not args.force and not args.dry_run:
        logger.info("Backlog is current (inputs and policy unchanged) — artifact %s kept. Use --force to re-rank.",
                    existing.get("artifact_id"))
        logger.info("Priority Agent complete: backlog current")
        return 0

    scored = score_backlog(ctx["stories"], ctx["risk_register"], ctx["ticket_status"], policy)
    source, _, quota_hit = add_rationales(scored["ranked"], not args.no_llm, args.retries)

    # open questions are written by code from the actual flags (the LLM could misattribute them)
    questions = build_open_questions(scored)

    result = {
        "artifact_type": PRIORITY_TYPE,
        "schema_version": SCHEMA_VERSION,
        "project_id": ctx["project_id"],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_fingerprint": fingerprint,
        "policy_version": policy.POLICY_VERSION,
        "policy": snapshot,
        "method": "wsjf: (value + time_criticality + risk_reduction + enablement) / job_size, dependency-ordered",
        "estimate_reference": ctx["estimation"],
        "ranked_items": scored["ranked"],
        "unranked_or_blocked_items": scored["unranked"],
        "notes": scored["notes"],
        "open_questions": questions,
        "rationale_source": source,
        "needs_review": True,  # every backlog goes to the PO gate
        "items_requiring_review": [it["work_item_id"] for it in scored["ranked"] if it["needs_review"]]
                                  + [u["work_item_id"] for u in scored["unranked"]],
        "review_status": "proposed",
    }

    if not args.dry_run:
        try:
            aid = store_backlog(result, ctx, fingerprint)
            logger.info("Stored priority_backlog artifact %s", aid)
        except Exception:
            logger.exception("Failed to store the prioritized backlog")
            print_report(result)
            save_results(result, args.dry_run)
            return 2

    print_report(result)
    save_results(result, args.dry_run)
    logger.info("Priority Agent complete: %d/%d stories ranked%s", len(scored["ranked"]), c["stories"],
                " (dry run)" if args.dry_run else "")
    return 3 if quota_hit else 0


if __name__ == "__main__":
    raise SystemExit(main())