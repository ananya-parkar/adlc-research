# agents/architecture/architecture_discovery_agent.py
"""
Architecture Discovery & Blueprint Agent (Planning phase) — v2

Step A · BASELINE (once per project, reused while its inputs are unchanged)
    discovery mode — client architecture material exists (Confluence pages / uploaded
                     docs): reconstruct the current architecture from it.
    creation mode  — no material: propose ONE shared solution architecture from the
                     candidate work items, so every blueprint designs against it.

Step B · BLUEPRINT (per eligible candidate work item)
    How the change fits the baseline: components used / modified / new, interfaces,
    data changes, integrations, NFRs, options, ADR candidates, risks.
    Consumed by the Spec Synthesizer to write stories, acceptance criteria and NFRs.

What changed vs v1
  * Only ACTIVE, eligible CWIs (no superseded, epics, context, not_ready, rejected).
  * No silent 25-item limit.
  * Client architecture docs reach every blueprint (via the baseline).
  * Related work (dependencies) is passed in, so shared enablers are reused, not redesigned.
  * Slim prompts with short ids instead of the whole package as JSON (~3-4x fewer tokens).
  * Resumable: CWIs whose blueprint is still current are skipped. A rerun after a
    quota stop only does the missing ones.
  * Rate limits: waits the provider's "try again in …" when short; stops cleanly when long.
  * No fake success: an empty/invalid LLM answer is retried, never stored as a blueprint.

Usage
    python -m agents.architecture.architecture_discovery_agent --project-id KAN
    python -m agents.architecture.architecture_discovery_agent --project-id KAN --dry-run
    python -m agents.architecture.architecture_discovery_agent --project-id KAN --rebuild-baseline
    python -m agents.architecture.architecture_discovery_agent --project-id KAN --force --limit 3
"""

import argparse
import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from dotenv import load_dotenv

load_dotenv()

from agents.architecture.prompts import (  # noqa: E402
    BASELINE_CREATION_SYSTEM_PROMPT,
    BASELINE_DISCOVERY_SYSTEM_PROMPT,
    BLUEPRINT_SYSTEM_PROMPT,
    baseline_as_text,
    build_baseline_creation_prompt,
    build_baseline_discovery_prompt,
    build_blueprint_prompt,
)
from llm_client import LLMResponseError, LLMTruncatedError, call_llm_json  # noqa: E402
from substrate.artifact_registry import supersede_artifacts, upsert_artifact  # noqa: E402
from substrate.context_assembler import build_architecture_package  # noqa: E402
from substrate.context_registry import record_provenance  # noqa: E402
from substrate.vector_store import store_chunks  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
for noisy in ("httpx", "httpcore", "sentence_transformers", "urllib3", "groq", "openai", "anthropic"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

BASELINE_TYPE = "architecture_baseline"
BLUEPRINT_TYPE = "architecture_blueprint"

BASELINE_MAX_TOKENS = int(os.environ.get("ARCH_BASELINE_MAX_TOKENS", 3500))
BLUEPRINT_MAX_TOKENS = int(os.environ.get("ARCH_BLUEPRINT_MAX_TOKENS", 3000))
MAX_TOKENS_CAP = int(os.environ.get("ARCH_MAX_TOKENS_CAP", 7000))
MAX_RATE_LIMIT_WAIT = int(os.environ.get("ARCH_MAX_RATE_LIMIT_WAIT", 240))  # seconds
REVIEW_CONFIDENCE = 0.70
GREENFIELD_CONFIDENCE_CAP = 0.80

RESULTS_DIR = Path(__file__).resolve().parents[2] / "storage" / "architecture_results"

COMPONENT_CHANGES = {"use", "modify", "new"}


class QuotaExhausted(Exception):
    """Provider asks us to wait longer than we are willing to — stop the run cleanly."""


class GenerationFailed(Exception):
    """The LLM could not produce a usable answer after all attempts."""


# ----------------------------------------------------------------------
# LLM call with sensible failure handling
# ----------------------------------------------------------------------
_WAIT_RE = re.compile(r"try again in\s*(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:([\d.]+)s)?", re.I)


def _retry_after(message: str) -> Optional[float]:
    m = _WAIT_RE.search(message)
    if not m or not any(m.groups()):
        return None
    h, mins, secs = (float(x) if x else 0.0 for x in m.groups())
    return h * 3600 + mins * 60 + secs


def _is_rate_limit(exc: Exception) -> bool:
    text = str(exc).lower()
    return getattr(exc, "status_code", None) == 429 or "rate limit" in text or "rate_limit" in text


def llm_json(system: str, user: str, max_tokens: int, attempts: int,
             validate) -> dict:
    """
    Call the LLM and run `validate` on the answer. Retries on invalid output
    and truncation (with a bigger token budget), waits out short rate limits,
    raises QuotaExhausted on long ones and GenerationFailed when out of attempts.
    """
    tokens, tried, waits, last_error = max_tokens, 0, 0, None
    while tried < attempts:
        try:
            raw = call_llm_json(system, user, max_tokens=tokens)
            return validate(raw)
        except LLMTruncatedError as exc:
            tried += 1
            last_error = exc
            tokens = min(int(tokens * 1.8), MAX_TOKENS_CAP)
            logger.warning("Answer truncated — retrying with max_tokens=%d", tokens)
        except (LLMResponseError, ValueError) as exc:
            tried += 1
            last_error = exc
            logger.warning("Unusable answer (%s) — attempt %d/%d", str(exc)[:160], tried, attempts)
        except Exception as exc:  # provider errors
            if not _is_rate_limit(exc):
                raise
            wait = _retry_after(str(exc)) or 60
            if wait > MAX_RATE_LIMIT_WAIT or waits >= 5:
                raise QuotaExhausted(f"provider asks to wait {wait:.0f}s: {str(exc)[:200]}") from exc
            waits += 1
            logger.warning("Rate limited — waiting %.0fs as the provider asks", wait + 2)
            time.sleep(wait + 2)
    raise GenerationFailed(str(last_error))


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
def _hash(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _list(value) -> list:
    return value if isinstance(value, list) else []


def _dicts(value, required: str) -> List[dict]:
    return [v for v in _list(value) if isinstance(v, dict) and str(v.get(required) or "").strip()]


def _map_evidence(items: List[dict], alias_to_id: Dict[str, str]) -> None:
    for item in items:
        ev = [str(e).strip() for e in _list(item.get("evidence"))]
        item["evidence"] = [e for e in dict.fromkeys(ev) if e in alias_to_id]


def _all_evidence(obj: dict, keys) -> List[str]:
    return sorted({e for k in keys for i in obj.get(k) or [] for e in i.get("evidence") or []})


# ----------------------------------------------------------------------
# Step A — baseline
# ----------------------------------------------------------------------
BASELINE_LIST_KEYS = {
    "components": "name", "data_stores": "name", "integrations": "name",
    "tech_stack": "layer", "deployment": "item", "constraints": "item", "nfrs": "requirement",
}


def _validate_baseline(raw: dict, mode: str, alias_to_id: Dict[str, str]) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("baseline is not an object")
    out = {"summary": str(raw.get("summary") or "").strip()}
    for key, required in BASELINE_LIST_KEYS.items():
        out[key] = _dicts(raw.get(key), required)
        _map_evidence(out[key], alias_to_id)
    out["gaps"] = [str(g) for g in _list(raw.get("gaps"))]
    out["open_questions"] = [str(q) for q in _list(raw.get("open_questions"))]
    if not out["components"]:
        raise ValueError("baseline has no components")
    for c in out["components"]:
        if mode == "creation":
            c["status"] = "proposed"
        elif c.get("status") not in ("existing", "planned"):
            c["status"] = "existing"
    return out


def ensure_baseline(ctx: dict, args) -> Tuple[Optional[dict], str]:
    """Return (baseline_record, how) where how is reused|generated|unavailable."""
    mode, fp = ctx["baseline_mode"], ctx["baseline_fingerprint"]
    existing = ctx.get("existing_baseline")
    if existing and existing.get("fingerprint") == fp and not args.rebuild_baseline:
        logger.info("Reusing architecture baseline (%s, %d components)",
                    existing.get("origin"), len(existing["baseline"].get("components") or []))
        return existing, "reused"

    if mode == "discovery":
        docs = ctx["architecture_sources"]
        aliases = [f"D{i}" for i in range(1, len(docs) + 1)]
        alias_to_id = {a: d["artifact_id"] for a, d in zip(aliases, docs)}
        system, user = BASELINE_DISCOVERY_SYSTEM_PROMPT, build_baseline_discovery_prompt(docs, aliases)
        logger.info("Step A: DISCOVERING architecture from %d client document(s): %s",
                    len(docs), "; ".join(f"{d['title']} ({d['detection_reason']})" for d in docs))
    else:
        cwis = ctx["candidate_work_items"]
        aliases = [f"C{i}" for i in range(1, len(cwis) + 1)]
        alias_to_id = {a: c["artifact_id"] for a, c in zip(aliases, cwis)}
        system, user = BASELINE_CREATION_SYSTEM_PROMPT, build_baseline_creation_prompt(cwis, aliases)
        logger.info("Step A: no architecture documents found — PROPOSING a solution architecture "
                    "from %d work items", len(cwis))

    baseline = llm_json(system, user, BASELINE_MAX_TOKENS, attempts=args.retries + 1,
                        validate=lambda raw: _validate_baseline(raw, mode, alias_to_id))
    record = {
        "artifact_id": None,
        "fingerprint": fp,
        "origin": "discovered" if mode == "discovery" else "proposed",
        "baseline": baseline,
        "alias_to_id": alias_to_id,
        "doc_aliases": ({a: d["title"] for a, d in zip(aliases, ctx["architecture_sources"])}
                        if mode == "discovery" else {}),
    }
    if not args.dry_run:
        record["artifact_id"] = store_baseline(record, ctx)
    return record, "generated"


def store_baseline(record: dict, ctx: dict) -> str:
    project_key = ctx["project_key"]
    b = record["baseline"]
    source_ids = [d["source_id"] for d in ctx["architecture_sources"] if d.get("source_id")] or \
                 [c["source_id"] for c in ctx["candidate_work_items"] if c.get("source_id")]
    evidence_ids = sorted({record["alias_to_id"][e] for e in _all_evidence(b, BASELINE_LIST_KEYS)})

    artifact_id = upsert_artifact(
        cwi={"title": f"Architecture baseline — {project_key}",
             "source_refs": [f"architecture-baseline:{project_key}"]},
        source_id=source_ids[0] if source_ids else None,
        artifact_type=BASELINE_TYPE,
        content_override=json.dumps(b, ensure_ascii=False),
        extra_metadata={
            "lifecycle": "active",
            "project_id": ctx["project_id"],
            "origin": record["origin"],
            "fingerprint": record["fingerprint"],
            "component_count": len(b["components"]),
            "evidence_artifact_ids": evidence_ids,
            "needs_review": True,  # an architect should always confirm the baseline
            "alias_to_id": record["alias_to_id"],
            "doc_aliases": record["doc_aliases"],
        },
        project_id=ctx["project_id"],
    )
    for doc in ctx["architecture_sources"]:
        record_provenance(artifact_id=artifact_id, source_id=doc.get("source_id"),
                          source_ref=doc.get("source_ref"), extraction_method="architecture_baseline")
    store_chunks(artifact_id, baseline_as_text(b, record["origin"]), project_id=ctx["project_id"],
                 metadata={"artifact_type": BASELINE_TYPE})
    logger.info("Stored architecture baseline (%s) artifact_id=%s", record["origin"], artifact_id)
    return artifact_id


# ----------------------------------------------------------------------
# Step B — blueprints
# ----------------------------------------------------------------------
BLUEPRINT_EVIDENCE_KEYS = ("components", "integrations", "nfrs")


def _validate_blueprint(raw: dict, alias_to_id: Dict[str, str], baseline: Optional[dict]) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("blueprint is not an object")
    summary = str(raw.get("summary") or "").strip()
    components = _dicts(raw.get("components"), "name")
    interfaces = _dicts(raw.get("interfaces"), "name")
    if not summary or not (components or interfaces):
        raise ValueError("blueprint has no summary or no components/interfaces")

    known = {c["name"].strip().lower() for c in (baseline or {}).get("components") or []}
    known |= {i["name"].strip().lower() for i in (baseline or {}).get("integrations") or []}
    for c in components:
        if c.get("change") not in COMPONENT_CHANGES:
            c["change"] = "modify"
        c["in_baseline"] = c["name"].strip().lower() in known
    integrations = _dicts(raw.get("integrations"), "system")
    nfrs = _dicts(raw.get("nfrs"), "requirement")
    for group in (components, integrations, nfrs):
        _map_evidence(group, alias_to_id)

    try:
        confidence = max(0.0, min(1.0, float(raw.get("confidence", 0.0))))
    except (TypeError, ValueError):
        confidence = 0.0

    options = _dicts(raw.get("options"), "name")[:2]
    recommended = raw.get("recommended_option") if options else None
    return {
        "summary": summary,
        "components": components,
        "interfaces": interfaces,
        "data_changes": _dicts(raw.get("data_changes"), "store"),
        "integrations": integrations,
        "nfrs": nfrs,
        "options": options,
        "recommended_option": recommended,
        "adr_candidates": _dicts(raw.get("adr_candidates"), "title"),
        "risks": _dicts(raw.get("risks"), "risk"),
        "assumptions": [str(a) for a in _list(raw.get("assumptions"))],
        "open_questions": [str(q) for q in _list(raw.get("open_questions"))],
        "confidence": confidence,
    }


def generate_blueprint(cwi: dict, baseline_rec: Optional[dict], args) -> dict:
    members = cwi.get("members") or []
    aliases = [m.get("source_ref") or f"S{i}" for i, m in enumerate(members, 1)]
    alias_to_id = {a: m["artifact_id"] for a, m in zip(aliases, members)}
    doc_aliases = (baseline_rec or {}).get("doc_aliases") or {}
    # baseline document ids are valid evidence too (only known when generated this run)
    for a in doc_aliases:
        if a in (baseline_rec or {}).get("alias_to_id", {}):
            alias_to_id[a] = baseline_rec["alias_to_id"][a]

    baseline = (baseline_rec or {}).get("baseline")
    user = build_blueprint_prompt(cwi, members, aliases, cwi.get("related") or [],
                                  baseline_as_text(baseline, (baseline_rec or {}).get("origin")), doc_aliases)
    bp = llm_json(BLUEPRINT_SYSTEM_PROMPT, user, BLUEPRINT_MAX_TOKENS, attempts=args.retries + 1,
                  validate=lambda raw: _validate_blueprint(raw, alias_to_id, baseline))

    # A design against a *proposed* baseline (or none) rests on assumptions, not on a real system.
    if (baseline_rec or {}).get("origin") != "discovered":
        bp["confidence"] = min(bp["confidence"], GREENFIELD_CONFIDENCE_CAP)

    evidence_aliases = _all_evidence(bp, BLUEPRINT_EVIDENCE_KEYS)
    unknown = [c["name"] for c in bp["components"] if c["change"] != "new" and not c["in_baseline"]]
    if unknown and baseline:
        bp["assumptions"].append(f"Not in the baseline but treated as existing: {', '.join(unknown)}")

    bp.update({
        "cwi_artifact_id": cwi["artifact_id"],
        "cwi_id": cwi.get("cwi_id"),
        "title": cwi["title"],
        "mode": {"discovered": "extend_existing", "proposed": "greenfield"}.get(
            (baseline_rec or {}).get("origin"), "no_baseline"),
        "baseline_artifact_id": (baseline_rec or {}).get("artifact_id"),
        "related_work": [{"ref": r["ref"], "title": r["title"], "relation": r["relation"]}
                         for r in cwi.get("related") or []],
        "evidence_artifact_ids": sorted({alias_to_id[a] for a in evidence_aliases}),
        "evidence_refs": evidence_aliases,
    })
    bp["needs_review"] = bool(
        bp["confidence"] < REVIEW_CONFIDENCE
        or not bp["evidence_artifact_ids"]
        or bp["open_questions"]
        or (unknown and baseline)
    )
    return bp


def _blueprint_text(bp: dict) -> str:
    lines = [bp["title"], bp["summary"]]
    lines += [f"{c['change']}: {c['name']} — {c.get('what_changes', '')}" for c in bp["components"]]
    lines += [f"NFR {n.get('category')}: {n['requirement']}" for n in bp["nfrs"]]
    return "\n".join(lines)


def store_blueprint(bp: dict, cwi: dict, fingerprint: str, project_id: Optional[str]) -> str:
    if not cwi.get("source_id"):
        raise ValueError(f"CWI {cwi['artifact_id']} has no source_id")
    artifact_id = upsert_artifact(
        cwi={"title": bp["title"], "source_refs": [f"architecture-blueprint:{cwi['artifact_id']}"]},
        source_id=cwi["source_id"],
        artifact_type=BLUEPRINT_TYPE,
        content_override=json.dumps(bp, ensure_ascii=False),
        extra_metadata={
            "lifecycle": "active",
            "project_id": project_id,
            "cwi_artifact_id": cwi["artifact_id"],
            "cwi_id": cwi.get("cwi_id"),
            "mode": bp["mode"],
            "baseline_artifact_id": bp["baseline_artifact_id"],
            "fingerprint": fingerprint,
            "confidence": bp["confidence"],
            "needs_review": bp["needs_review"],
            "component_changes": {k: sum(1 for c in bp["components"] if c["change"] == k)
                                  for k in COMPONENT_CHANGES},
            "evidence_artifact_ids": bp["evidence_artifact_ids"],
        },
        project_id=project_id,
    )
    record_provenance(artifact_id=artifact_id, source_id=cwi["source_id"],
                      source_ref=cwi["artifact_id"], extraction_method="architecture_blueprint")
    store_chunks(artifact_id, _blueprint_text(bp), project_id=project_id,
                 metadata={"artifact_type": BLUEPRINT_TYPE, "cwi_artifact_id": cwi["artifact_id"]})
    return artifact_id


# ----------------------------------------------------------------------
# report + results
# ----------------------------------------------------------------------
def print_report(ctx, baseline_rec, how, results, skipped_current, pending, failures):
    print("\n" + "=" * 80)
    print(f"  ARCHITECTURE REPORT — project {ctx['project_key']}")
    print("=" * 80)
    if baseline_rec:
        b = baseline_rec["baseline"]
        print(f"\nBaseline: {baseline_rec['origin']} ({how}) — {len(b['components'])} components")
        print(f"  {b.get('summary', '')}")
        for c in b["components"]:
            print(f"  • {c['name']} [{c.get('type')}, {c.get('status')}]: {c.get('responsibility')}")
        if b.get("gaps"):
            print(f"  gaps: {'; '.join(b['gaps'])}")
    else:
        print("\nBaseline: unavailable")

    print(f"\nBlueprints ({len(results)} generated, {len(skipped_current)} already current)")
    for bp in results:
        ch = {k: sum(1 for c in bp["components"] if c["change"] == k) for k in ("new", "modify", "use")}
        flag = "REVIEW" if bp["needs_review"] else "ok"
        print(f"\n  {bp['title']}  ({bp['mode']}, conf {bp['confidence']:.2f})  [{flag}]")
        print(f"    components: {ch['new']} new · {ch['modify']} modify · {ch['use']} use   "
              f"nfrs: {len(bp['nfrs'])}   risks: {len(bp['risks'])}")
        if bp["related_work"]:
            print(f"    related: {', '.join(r['ref'] for r in bp['related_work'])}")
        if bp["open_questions"]:
            print(f"    open questions: {'; '.join(bp['open_questions'][:3])}")

    if ctx["skipped_work_items"]:
        print("\nNot designed (by rule)")
        for s in ctx["skipped_work_items"]:
            print(f"  - {s['title']}: {s['reason']}")
    if failures:
        print("\nFailed")
        for title, reason in failures.items():
            print(f"  ✗ {title}: {reason}")
    if pending:
        print(f"\nPending (run again later to continue): {', '.join(pending)}")
    print()


def save_results(ctx, baseline_rec, results, dry_run) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / f"{ctx['project_key']}_{datetime.now():%Y%m%d_%H%M%S}.json"
    path.write_text(json.dumps({
        "project_id": ctx["project_id"], "dry_run": dry_run,
        "baseline": {k: baseline_rec[k] for k in ("origin", "baseline")} if baseline_rec else None,
        "skipped_work_items": ctx["skipped_work_items"],
        "blueprints": results,
    }, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    logger.info("Results saved to %s", path)
    return path


# ----------------------------------------------------------------------
def main() -> int:
    p = argparse.ArgumentParser(description="Architecture Discovery & Blueprint Agent")
    p.add_argument("--project-id", default=None, help="e.g. KAN")
    p.add_argument("--limit", type=int, default=None, help="only the first N eligible CWIs (testing)")
    p.add_argument("--retries", type=int, default=2, help="extra attempts per LLM call")
    p.add_argument("--dry-run", action="store_true", help="generate and print, write nothing")
    p.add_argument("--force", action="store_true", help="regenerate blueprints even if current")
    p.add_argument("--rebuild-baseline", action="store_true", help="regenerate the baseline")
    p.add_argument("--include-unready", action="store_true", help="also design not_ready CWIs")
    args = p.parse_args()

    package_path = build_architecture_package(project_id=args.project_id, limit=args.limit,
                                              include_unready=args.include_unready)
    ctx = json.loads(package_path.read_text(encoding="utf-8"))["context"]
    cwis = ctx["candidate_work_items"]
    logger.info("Architecture context package built: %s", package_path)
    logger.info("Package contains %d CWIs (%d skipped by rule), %d architecture document(s)",
                len(cwis), len(ctx["skipped_work_items"]), len(ctx["architecture_sources"]))

    if not cwis:
        logger.warning("No eligible candidate work items. Run the Discovery Agent first.")
        return 0

    # ---- Step A
    baseline_rec, how = None, "unavailable"
    try:
        baseline_rec, how = ensure_baseline(ctx, args)
    except QuotaExhausted as exc:
        logger.error("Stopped before the baseline: %s", exc)
        return 3
    except GenerationFailed as exc:
        logger.error("Baseline generation failed (%s) — continuing without a baseline", exc)

    baseline_fp = (baseline_rec or {}).get("fingerprint", "none")

    # ---- Step B
    results, skipped_current, failures, pending = [], [], {}, []
    stored_ids: List[str] = []
    stopped = False
    for n, cwi in enumerate(cwis, 1):
        title = cwi["title"]
        if stopped:
            pending.append(title)
            continue
        fp = _hash(baseline_fp, {k: cwi.get(k) for k in (
            "title", "summary", "business_intent", "decisions", "gaps", "dependencies",
            "open_questions", "member_artifact_ids")})
        existing = ctx["existing_blueprints"].get(cwi["artifact_id"])
        if existing and existing.get("fingerprint") == fp and not args.force:
            skipped_current.append(title)
            stored_ids.append(existing["artifact_id"])
            logger.info("Processing architecture CWI %d/%d: %s — already current, skipped", n, len(cwis), title)
            continue

        logger.info("Processing architecture CWI %d/%d: %s", n, len(cwis), title)
        try:
            bp = generate_blueprint(cwi, baseline_rec, args)
        except QuotaExhausted as exc:
            logger.error("Provider quota reached — stopping. %s", exc)
            stopped = True
            pending.append(title)
            continue
        except GenerationFailed as exc:
            failures[title] = f"generation failed: {exc}"
            logger.error("Architecture generation failed for CWI %s: %s", title, exc)
            continue

        results.append(bp)
        if args.dry_run:
            continue
        try:
            aid = store_blueprint(bp, cwi, fp, ctx["project_id"])
            stored_ids.append(aid)
            logger.info("Created/updated blueprint '%s' (artifact_id=%s, confidence=%.2f, needs_review=%s)",
                        title, aid, bp["confidence"], bp["needs_review"])
        except Exception as exc:
            failures[title] = f"storage failed: {exc}"
            logger.exception("Failed to store blueprint for %s", title)

    # Blueprints for CWIs that are no longer eligible/active → superseded.
    # Existing blueprints of eligible CWIs are always kept, so a partial run never loses work.
    if not args.dry_run and not args.limit:
        keep = set(stored_ids) | {
            ctx["existing_blueprints"][c["artifact_id"]]["artifact_id"]
            for c in cwis if c["artifact_id"] in ctx["existing_blueprints"]
        }
        old = supersede_artifacts(BLUEPRINT_TYPE, ctx["project_id"], sorted(keep)) if ctx["project_id"] else 0
        if old:
            logger.info("Marked %d blueprint(s) of no-longer-eligible CWIs as superseded", old)

    print_report(ctx, baseline_rec, how, results, skipped_current, pending, failures)
    save_results(ctx, baseline_rec, results, args.dry_run)

    done = len(results) + len(skipped_current) - sum(1 for t in failures if "storage" in failures[t])
    logger.info("Architecture Discovery complete: %d/%d blueprints stored%s", done, len(cwis),
                " (dry run)" if args.dry_run else "")
    if stopped:
        return 3
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())