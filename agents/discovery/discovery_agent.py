# agents/discovery/discovery_agent.py
"""
Discovery Agent (Planning phase) — v2

Flow, per project:
  1. Load source artifacts from the Discovery context package.
  2. Group related artifacts: Jira hierarchy (subtask->story->epic) +
     embedding similarity. Groups are size-capped so one big doc can't
     pull everything into a single group.
  3. Pack groups into batches bounded by artifact count and characters.
  4. One LLM call per batch -> candidate work items. Short ids (A1, A2..)
     are used in the prompt. If an answer is truncated or invalid, the
     batch is split in half and retried — no more all-or-nothing failure.
  5. One LLM call over all CWIs -> cross-cutting patterns + possible
     duplicates that landed in different batches.
  6. Store CWIs and patterns. CWIs/patterns from earlier runs that were
     not reproduced (and not yet reviewed by a human) are marked
     lifecycle="superseded" instead of piling up.

Usage:
    python -m agents.discovery.discovery_agent
    python -m agents.discovery.discovery_agent --dry-run          # no DB writes, just print the report
    python -m agents.discovery.discovery_agent --project-id KAN
"""

import argparse
import hashlib
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from collections import defaultdict
from typing import Dict, List, Tuple

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv

load_dotenv()

from agents.discovery.prompts import (  # noqa: E402
    DISCOVERY_PATTERNS_SYSTEM_PROMPT,
    DISCOVERY_SYNTHESIS_SYSTEM_PROMPT,
    MAX_CONTENT_CHARS,
    build_patterns_user_prompt,
    build_synthesis_user_prompt,
)
from llm_client import LLMResponseError, call_llm_json  # noqa: E402
from substrate.artifact_registry import supersede_artifacts, upsert_artifact  # noqa: E402
from substrate.context_assembler import build_discovery_package  # noqa: E402
from substrate.context_registry import record_provenance  # noqa: E402
from substrate.vector_store import _get_model, store_chunks  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
for noisy in ("httpx", "httpcore", "sentence_transformers", "urllib3", "groq", "openai", "anthropic"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

RAW_TYPE = "source_artifact"
CWI_TYPE = "candidate_work_item"
PATTERN_TYPE = "discovery_pattern"

# Tunables (override in .env)
MAX_GROUP_SIZE = int(os.environ.get("DISCOVERY_MAX_GROUP_SIZE", 8))
MAX_BATCH_ARTIFACTS = int(os.environ.get("DISCOVERY_MAX_BATCH_ARTIFACTS", 12))
MAX_BATCH_CHARS = int(os.environ.get("DISCOVERY_MAX_BATCH_CHARS", 20000))
SIMILARITY_THRESHOLD = float(os.environ.get("DISCOVERY_SIMILARITY_THRESHOLD", 0.55))
BATCH_MAX_TOKENS = int(os.environ.get("DISCOVERY_BATCH_MAX_TOKENS", 6000))
PATTERN_MAX_TOKENS = int(os.environ.get("DISCOVERY_PATTERN_MAX_TOKENS", 3000))
REVIEW_CONFIDENCE = 0.70

LIST_FIELDS = ("gaps", "conflicts", "decisions", "dependencies", "open_questions")
READINESS_VALUES = ("ready", "needs_refinement", "not_ready")
_OWNERSHIP_WORDS = ("unassigned", "assignee", "owner", "no epic", "epic link", "epic association")


# ----------------------------------------------------------------------
# small helpers
# ----------------------------------------------------------------------
def _meta(a: dict) -> dict:
    return a.get("metadata") or {}


def _source(a: dict) -> dict:
    return a.get("source") or {}


def _ref(a: dict) -> str:
    return _source(a).get("source_ref") or a["artifact_id"][:8]


def _project(a: dict) -> str:
    return _meta(a).get("project_id") or "default"


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _cluster_ref(member_ids: List[str]) -> str:
    return "cluster:" + _hash(",".join(sorted(member_ids)))


def _embedding_text(a: dict) -> str:
    content = a.get("content") or ""
    if "Description:\n" in content:  # skip the Jira header lines (Type/Status/...) — they add noise
        content = content.split("Description:\n", 1)[1]
    return f"{a.get('title') or ''}\n{content[:600]}"


# ----------------------------------------------------------------------
# 1. grouping
# ----------------------------------------------------------------------
class _CappedUnionFind:
    def __init__(self, ids: List[str]):
        self.parent = {i: i for i in ids}
        self.size = {i: 1 for i in ids}

    def find(self, x: str) -> str:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> bool:
        ra, rb = self.find(a), self.find(b)
        if ra == rb or self.size[ra] + self.size[rb] > MAX_GROUP_SIZE:
            return False
        if self.size[ra] < self.size[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        self.size[ra] += self.size[rb]
        return True

    def groups(self) -> Dict[str, List[str]]:
        out: Dict[str, List[str]] = defaultdict(list)
        for i in self.parent:
            out[self.find(i)].append(i)
        return out


def build_groups(artifacts: List[dict]) -> List[List[dict]]:
    by_id = {a["artifact_id"]: a for a in artifacts}
    uf = _CappedUnionFind(list(by_id))

    # (a) Jira hierarchy — subtask->story first, then story->epic
    jira_ref_to_id = {_ref(a): a["artifact_id"] for a in artifacts if _source(a).get("source_type") == "jira"}
    edges = []
    for a in artifacts:
        m = _meta(a)
        parent = m.get("parent") or {}
        parent_key = parent.get("key") or m.get("epic_key")
        if parent_key and parent_key in jira_ref_to_id:
            is_epic_link = (parent.get("type") or "").lower() == "epic" or (not parent and m.get("epic_key"))
            edges.append((1 if is_epic_link else 0, a["artifact_id"], jira_ref_to_id[parent_key]))
    for _, child, parent in sorted(edges):
        uf.union(child, parent)

    # (b) embedding similarity — strongest pairs first, across all sources
    ids = list(by_id)
    if len(ids) > 1:
        vectors = _get_model().encode([_embedding_text(by_id[i]) for i in ids], normalize_embeddings=True)
        sims = vectors @ vectors.T
        pairs = [
            (float(sims[i, j]), ids[i], ids[j])
            for i in range(len(ids)) for j in range(i + 1, len(ids))
            if sims[i, j] >= SIMILARITY_THRESHOLD
        ]
        for _, a, b in sorted(pairs, reverse=True):
            uf.union(a, b)

    return [[by_id[i] for i in members] for members in uf.groups().values()]


# ----------------------------------------------------------------------
# 2. batching
# ----------------------------------------------------------------------
def _prompt_size(a: dict) -> int:
    return min(len(a.get("content") or ""), MAX_CONTENT_CHARS) + 300


def pack_batches(groups: List[List[dict]]) -> List[List[dict]]:
    """First-fit decreasing: related artifacts always stay in the same batch."""
    batches: List[dict] = []
    for group in sorted(groups, key=lambda g: sum(_prompt_size(a) for a in g), reverse=True):
        chars = sum(_prompt_size(a) for a in group)
        for b in batches:
            if len(b["items"]) + len(group) <= MAX_BATCH_ARTIFACTS and b["chars"] + chars <= MAX_BATCH_CHARS:
                b["items"].extend(group)
                b["chars"] += chars
                break
        else:
            batches.append({"items": list(group), "chars": chars})
    return [b["items"] for b in batches]


_ALIAS_RE = {"A": __import__("re").compile(r"\bA(\d+)\b"), "C": __import__("re").compile(r"\bC(\d+)\b")}


def _replace_aliases(value, prefix: str, mapping: Dict[str, str]):
    """Safety net: rewrite leaked batch ids (A4 / C11) in LLM text to real refs (KAN-20)."""
    if isinstance(value, str):
        return _ALIAS_RE[prefix].sub(lambda m: mapping.get(m.group(0), m.group(0)), value)
    if isinstance(value, list):
        return [_replace_aliases(v, prefix, mapping) for v in value]
    return value


# ----------------------------------------------------------------------
# 3. LLM synthesis per batch
# ----------------------------------------------------------------------
def _fallback_item(a: dict, reason: str) -> dict:
    return {
        "member_ids": [a["artifact_id"]],
        "title": a.get("title") or "Unclassified discovery item",
        "business_intent": "",
        "refined_summary": (a.get("content") or "")[:1000],
        "signal_type": _meta(a).get("signal_type") or "unclear",
        "is_actionable": False,
        "readiness": "not_ready",
        "confidence": 0.2,
        "reasoning": reason,
        "duplicate_refs": [],
        "possible_duplicates": [],
        **{f: [] for f in LIST_FIELDS},
    }


_BLOCKING_GAP_WORDS = ("missing description", "vague")
THIN_DESCRIPTION_WORDS = int(os.environ.get("DISCOVERY_THIN_DESCRIPTION_WORDS", 8))
_SECTION_ENDS = ("\n\nAcceptance criteria:", "\n\nLinks:", "\n\nSubtasks:", "\n\nRecent comments:",
                 "\n\nQuality flags:", "\n## acceptance", "\n## Acceptance")


def _description_words(a: dict) -> int:
    """Word count of a Jira issue's description (0 if it has none)."""
    content = a.get("content") or ""
    if "Description:\n" not in content:
        return 0
    desc = content.split("Description:\n", 1)[1]
    for end in _SECTION_ENDS:
        desc = desc.split(end, 1)[0]
    return len(desc.split())


def _thin_evidence(members: List[dict]) -> bool:
    """
    Deterministic vagueness check: every member is a Jira issue with no acceptance
    criteria and a description shorter than THIN_DESCRIPTION_WORDS (e.g. "The app feels slow.").
    Other sources (Confluence, docs) are never judged thin by this rule.
    """
    for a in members:
        if _source(a).get("source_type") != "jira" or _meta(a).get("acceptance_criteria"):
            return False
        if _description_words(a) >= THIN_DESCRIPTION_WORDS:
            return False
    return True


def _derive_readiness(item: dict) -> str:
    """
    Readiness is decided in code from what the LLM found, not by the LLM itself —
    the LLM's own label varied run to run for identical input.
      not_ready        : conflict, unclear signal, no description or scope too vague
      needs_refinement : any other gap, or open questions
      ready            : nothing missing
    """
    gaps = [g.lower() for g in item.get("gaps") or []]
    if (item.get("conflicts") or item.get("signal_type") == "unclear"
            or any(w in g for g in gaps for w in _BLOCKING_GAP_WORDS)):
        return "not_ready"
    if gaps or item.get("open_questions"):
        return "needs_refinement"
    return "ready"


def _validate_work_items(work_items: list, artifacts_by_id: Dict[str, dict]) -> list:
    """Drop unknown ids, enforce one-artifact-one-item, never lose an artifact."""
    validated, assigned = [], set()

    for item in work_items:
        if not isinstance(item, dict):
            continue
        members = [m for m in item.get("member_ids") or [] if m in artifacts_by_id and m not in assigned]
        if not members:
            logger.warning("Skipping work item with no valid/unassigned members: %s", item.get("title"))
            continue
        assigned.update(members)
        item["member_ids"] = members
        for field in LIST_FIELDS:
            value = item.get(field)
            item[field] = [str(v) for v in value] if isinstance(value, list) else []
        item["gaps"] = [g for g in item["gaps"] if not any(w in g.lower() for w in _OWNERSHIP_WORDS)]
        if item.get("signal_type") == "epic":  # epics are containers — readiness comes from their children
            item["gaps"] = []
        elif item.get("signal_type") == "bug":  # a bug's expected behaviour is "it works"; AC is not a gap
            item["gaps"] = [g for g in item["gaps"] if "acceptance" not in g.lower()]
        # Safety net: don't depend on the LLM's wording to spot vague items.
        if (item.get("signal_type") not in ("epic", "bug", "context")
                and not any(w in g.lower() for g in item["gaps"] for w in _BLOCKING_GAP_WORDS)
                and _thin_evidence([artifacts_by_id[m] for m in members])):
            item["gaps"].append(f"scope too vague to estimate (description under {THIN_DESCRIPTION_WORDS} words)")
        dup_refs = item.get("possible_duplicates")
        item["duplicate_refs"] = [str(r) for r in dup_refs] if isinstance(dup_refs, list) else []
        item["possible_duplicates"] = []
        item["llm_readiness"] = item.get("readiness") if item.get("readiness") in READINESS_VALUES else None
        item["readiness"] = _derive_readiness(item)
        try:
            item["confidence"] = max(0.0, min(1.0, float(item.get("confidence", 0.0))))
        except (TypeError, ValueError):
            item["confidence"] = 0.0
        # Little or no evidence => our interpretation can't be confident, whatever the LLM says.
        if any(w in g.lower() for g in item["gaps"] for w in _BLOCKING_GAP_WORDS):
            item["confidence"] = min(item["confidence"], 0.6)
        validated.append(item)

    for artifact_id in set(artifacts_by_id) - assigned:
        validated.append(_fallback_item(artifacts_by_id[artifact_id],
                                        "Artifact was not assigned by the LLM and requires review."))
    return validated


def run_batch(batch: List[dict], label: str) -> list:
    aliases = [f"A{i}" for i in range(1, len(batch) + 1)]
    alias_to_id = {alias: a["artifact_id"] for alias, a in zip(aliases, batch)}
    by_id = {a["artifact_id"]: a for a in batch}

    try:
        result = call_llm_json(
            DISCOVERY_SYNTHESIS_SYSTEM_PROMPT,
            build_synthesis_user_prompt(batch, aliases),
            max_tokens=BATCH_MAX_TOKENS,
        )
    except LLMResponseError as exc:
        if len(batch) == 1:
            logger.error("Batch %s (single artifact %s) failed: %s", label, _ref(batch[0]), exc)
            return [_fallback_item(batch[0], f"LLM failed on this artifact: {exc}")]
        mid = len(batch) // 2
        logger.warning("Batch %s failed (%s) — splitting into %d + %d", label, exc, mid, len(batch) - mid)
        return run_batch(batch[:mid], label + "a") + run_batch(batch[mid:], label + "b")

    items = result.get("work_items") if isinstance(result, dict) else None
    items = items if isinstance(items, list) else []
    alias_to_ref = {alias: _ref(a) for alias, a in zip(aliases, batch)}
    for item in items:
        if isinstance(item, dict):
            item["member_ids"] = [alias_to_id[m] for m in item.get("member_ids") or [] if m in alias_to_id]
            item.pop("evidence", None)
            for field in (*LIST_FIELDS, "possible_duplicates", "reasoning", "refined_summary",
                          "business_intent", "title"):
                if field in item:
                    item[field] = _replace_aliases(item[field], "A", alias_to_ref)
    return _validate_work_items(items, by_id)


# ----------------------------------------------------------------------
# 4. cross-batch patterns + duplicates
# ----------------------------------------------------------------------
def run_patterns(items: List[dict]) -> Tuple[list, list]:
    if len(items) < 2:
        return [], []
    cid_map = {f"C{i}": item for i, item in enumerate(items, 1)}
    try:
        result = call_llm_json(
            DISCOVERY_PATTERNS_SYSTEM_PROMPT,
            build_patterns_user_prompt(cid_map),
            max_tokens=PATTERN_MAX_TOKENS,
        )
    except LLMResponseError as exc:
        logger.warning("Pattern pass failed (%s) — continuing without patterns", exc)
        return [], []

    cid_to_ref = {cid: ", ".join(item["source_refs"]) for cid, item in cid_map.items()}

    patterns = []
    for p in result.get("patterns") or []:
        p["pattern"] = _replace_aliases(p.get("pattern"), "C", cid_to_ref)
        p["description"] = _replace_aliases(p.get("description"), "C", cid_to_ref)
        members = [cid_map[c] for c in p.get("work_item_ids") or [] if c in cid_map]
        if len(members) >= 2 and p.get("pattern"):
            patterns.append({
                "pattern": p["pattern"],
                "description": p.get("description") or "",
                "confidence": p.get("confidence"),
                "cwi_ids": [m["cwi_id"] for m in members],
                "titles": [m["title"] for m in members],
                "source_refs": sorted({r for m in members for r in m["source_refs"]}),
                "members": members,
            })

    duplicates = []
    for d in result.get("possible_duplicates") or []:
        members = [cid_map[c] for c in d.get("work_item_ids") or [] if c in cid_map]
        if len(members) >= 2:
            duplicates.append({"members": members,
                               "reason": _replace_aliases(d.get("reason") or "", "C", cid_to_ref)})
    return patterns, duplicates


# ----------------------------------------------------------------------
# 5. storage
# ----------------------------------------------------------------------
def _needs_review(item: dict) -> bool:
    return bool(
        item.get("readiness") != "ready"
        or not item.get("is_actionable", True)
        or item.get("confidence", 0.0) < REVIEW_CONFIDENCE
        or item.get("signal_type") == "unclear"
        or item.get("conflicts")
        or item.get("possible_duplicates")
    )


def _add_duplicate(item: dict, other: dict, reason: str) -> None:
    if other is item or any(d["cwi_id"] == other["cwi_id"] for d in item["possible_duplicates"]):
        return
    item["possible_duplicates"].append({"cwi_id": other["cwi_id"], "title": other["title"], "reason": reason})


def _hygiene(artifacts: List[dict]) -> Dict[str, int]:
    """Deterministic backlog-hygiene counts from the mapper's quality flags (no LLM needed)."""
    counts: Dict[str, int] = defaultdict(int)
    for a in artifacts:
        for flag in _meta(a).get("quality_flags") or []:
            counts[flag] += 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def store_work_item(item: dict, by_id: Dict[str, dict], project_id: str) -> str:
    members = [by_id[m] for m in item["member_ids"]]
    primary_source = _source(members[0])

    artifact_id = upsert_artifact(
        cwi={"title": item.get("title"), "source_refs": [_cluster_ref(item["member_ids"])]},
        source_id=primary_source.get("source_id"),
        artifact_type=CWI_TYPE,
        content_override=item.get("refined_summary"),
        extra_metadata={
            "cwi_id": item["cwi_id"],
            "lifecycle": "active",
            "business_intent": item.get("business_intent"),
            "signal_type": item.get("signal_type"),
            "is_actionable": item.get("is_actionable"),
            "readiness": item.get("readiness"),
            "needs_review": item["needs_review"],
            "confidence": item.get("confidence"),
            "reasoning": item.get("reasoning"),
            **{f: item.get(f) or [] for f in LIST_FIELDS},
            "possible_duplicates": item.get("possible_duplicates") or [],
            "source_refs": item["source_refs"],
            "member_artifact_ids": item["member_ids"],
            "member_count": len(members),
            "evidence": [
                {
                    "artifact_id": m["artifact_id"],
                    "source_type": _source(m).get("source_type") or _meta(m).get("source_type"),
                    "source_ref": _source(m).get("source_ref"),
                }
                for m in members
            ],
        },
        project_id=project_id,
    )

    for m in members:
        record_provenance(
            artifact_id=artifact_id,
            source_id=_source(m).get("source_id"),
            source_ref=_source(m).get("source_ref"),
            extraction_method="discovery_agent_llm_merge" if len(members) > 1 else "discovery_agent_llm",
        )

    store_chunks(
        artifact_id,
        "\n".join(filter(None, [item.get("title"), item.get("business_intent"), item.get("refined_summary")])),
        project_id=project_id,
        metadata={"member_count": len(members), "artifact_type": CWI_TYPE},
    )

    logger.info("Created/updated CWI '%s' from %d artifact(s)", item.get("title"), len(members))
    return artifact_id


def store_pattern(pattern: dict, by_id: Dict[str, dict], project_id: str) -> str:
    first_member = by_id[pattern["members"][0]["member_ids"][0]]
    return upsert_artifact(
        cwi={"title": pattern["pattern"], "source_refs": [f"pattern:{_hash(pattern['pattern'].lower())}"]},
        source_id=_source(first_member).get("source_id"),
        artifact_type=PATTERN_TYPE,
        content_override=pattern["description"],
        extra_metadata={
            "lifecycle": "active",
            "confidence": pattern.get("confidence"),
            "cwi_ids": pattern["cwi_ids"],
            "cwi_titles": pattern["titles"],
            "source_refs": pattern["source_refs"],
        },
        project_id=project_id,
    )


# ----------------------------------------------------------------------
# report
# ----------------------------------------------------------------------
def print_report(project_id: str, items: List[dict], patterns: List[dict], hygiene: Dict[str, int]) -> None:
    print("\n" + "=" * 80)
    print(f"  DISCOVERY REPORT — project {project_id}: {len(items)} candidate work items, {len(patterns)} patterns")
    print("=" * 80)
    for n, item in enumerate(sorted(items, key=lambda i: (not i["needs_review"], i["title"] or "")), 1):
        flag = "REVIEW" if item["needs_review"] else "ok"
        print(f"\n[{n}] {item['title']}  ({item.get('signal_type')}, {item.get('readiness')}, "
              f"conf {item.get('confidence', 0):.2f})  [{flag}]")
        print(f"     sources: {', '.join(item['source_refs'])}")
        for field in LIST_FIELDS:
            if item.get(field):
                print(f"     {field}: {'; '.join(item[field])}")
        for dup in item.get("possible_duplicates") or []:
            print(f"     possible duplicate of: {dup['title']} ({dup['reason']})")
    if hygiene:
        print("\nBacklog hygiene (source artifacts)")
        print("  " + ", ".join(f"{k}: {v}" for k, v in hygiene.items()))
    if patterns:
        print("\nPatterns")
        for p in patterns:
            print(f"  • {p['pattern']}: {p['description']}  [{', '.join(p['source_refs'])}]")
    print()


# ----------------------------------------------------------------------
# orchestration
# ----------------------------------------------------------------------
RESULTS_DIR = Path(__file__).resolve().parents[2] / "storage" / "discovery_results"


def save_results(project_id: str, items: List[dict], patterns: List[dict], by_id: Dict[str, dict],
                 dry_run: bool) -> Path:
    """Write a JSON snapshot of this run — used by eval_discovery.py to score and compare runs."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / f"{project_id}_{datetime.now():%Y%m%d_%H%M%S}.json"
    payload = {
        "project_id": project_id,
        "generated_at": datetime.now().isoformat(),
        "dry_run": dry_run,
        "work_items": [
            {
                "title": i.get("title"),
                "signal_type": i.get("signal_type"),
                "readiness": i.get("readiness"),
                "llm_readiness": i.get("llm_readiness"),
                "confidence": i.get("confidence"),
                "needs_review": i.get("needs_review"),
                "source_refs": i["source_refs"],
                "source_titles": [by_id[m].get("title") for m in i["member_ids"]],
                **{f: i.get(f) or [] for f in LIST_FIELDS},
                "possible_duplicates": [d["title"] for d in i.get("possible_duplicates") or []],
                "reasoning": i.get("reasoning"),
            }
            for i in items
        ],
        "patterns": [{k: p[k] for k in ("pattern", "description", "source_refs")} for p in patterns],
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Results saved to %s", path)
    return path


def discover_project(project_id: str, artifacts: List[dict], dry_run: bool) -> None:
    by_id = {a["artifact_id"]: a for a in artifacts}

    groups = build_groups(artifacts)
    batches = pack_batches(groups)
    logger.info("Project %s: %d artifacts -> %d related groups -> %d LLM batch(es)",
                project_id, len(artifacts), len(groups), len(batches))

    items: List[dict] = []
    for n, batch in enumerate(batches, 1):
        logger.info("Batch %d/%d: %d artifacts (%s)", n, len(batches), len(batch),
                    ", ".join(_ref(a) for a in batch))
        items.extend(run_batch(batch, str(n)))

    for item in items:
        item["cwi_id"] = "cwi-" + _cluster_ref(item["member_ids"]).replace("cluster:", "")
        item["source_refs"] = [_ref(by_id[m]) for m in item["member_ids"]]

    ref_to_item = {ref: item for item in items for ref in item["source_refs"]}
    for item in items:
        for ref in item.pop("duplicate_refs", []):
            other = ref_to_item.get(ref)
            if other:
                _add_duplicate(item, other, f"flagged in batch review (overlaps {ref})")
                _add_duplicate(other, item, f"flagged in batch review (overlaps {item['source_refs'][0]})")

    patterns, duplicates = run_patterns(items)
    for dup in duplicates:
        for member in dup["members"]:
            for other in dup["members"]:
                _add_duplicate(member, other, dup["reason"])

    hygiene = _hygiene(artifacts)
    for item in items:
        item["needs_review"] = _needs_review(item)

    logger.info("Discovery Agent produced %d candidate work items and %d patterns", len(items), len(patterns))

    if dry_run:
        print_report(project_id, items, patterns, hygiene)
        save_results(project_id, items, patterns, by_id, dry_run=True)
        logger.info("Dry run — nothing written to the database.")
        return

    stored_cwis = []
    for item in items:
        try:
            stored_cwis.append(store_work_item(item, by_id, project_id))
        except Exception:
            logger.exception("Failed to store CWI '%s'", item.get("title"))

    stored_patterns = []
    for pattern in patterns:
        try:
            stored_patterns.append(store_pattern(pattern, by_id, project_id))
            logger.info("DISCOVERY PATTERN: %s", pattern["pattern"])
        except Exception:
            logger.exception("Failed to store pattern '%s'", pattern["pattern"])

    old_cwis = supersede_artifacts(CWI_TYPE, project_id, stored_cwis)
    old_patterns = supersede_artifacts(PATTERN_TYPE, project_id, stored_patterns)
    if old_cwis or old_patterns:
        logger.info("Marked %d old CWI(s) and %d old pattern(s) from earlier runs as superseded",
                    old_cwis, old_patterns)

    logger.info("Discovery complete: %d CWIs stored", len(stored_cwis))
    print_report(project_id, items, patterns, hygiene)
    save_results(project_id, items, patterns, by_id, dry_run=False)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Discovery Agent")
    parser.add_argument("--project-id", help="Only run for this project (default: all projects in the package)")
    parser.add_argument("--dry-run", action="store_true", help="Print results without writing to the database")
    args = parser.parse_args()

    package_path = build_discovery_package()
    logger.info("Package built at %s", package_path)
    package = json.loads(package_path.read_text(encoding="utf-8"))

    all_artifacts = package["context"]["artifacts"]
    raw_artifacts = [a for a in all_artifacts if a.get("artifact_type") == RAW_TYPE]
    logger.info("Package contains %d total artifacts; %d source artifacts for discovery",
                len(all_artifacts), len(raw_artifacts))

    if not raw_artifacts:
        logger.warning("No source_artifact records found. Run source ingestion before the Discovery Agent.")
        return

    by_project: Dict[str, List[dict]] = defaultdict(list)
    for a in raw_artifacts:
        by_project[_project(a)].append(a)

    for project_id, artifacts in sorted(by_project.items()):
        if args.project_id and project_id != args.project_id:
            continue
        discover_project(project_id, artifacts, args.dry_run)


if __name__ == "__main__":
    main()