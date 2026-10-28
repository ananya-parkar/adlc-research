# substrate/context_assembler.py
"""
Context Assembly Layer
Builds agent-specific context packages from the Common Substrate.
The agent does NOT query PostgreSQL directly.
The substrate assembles the package and the agent consumes it.
"""

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from substrate.db import get_connection

logger = logging.getLogger(__name__)

PACKAGE_DIR = (
    Path(__file__).resolve().parent.parent
    / "storage"
    / "packages"
)


def build_discovery_package() -> Path:
    """
    Build the context package required by the Planning Discovery Agent.

    Data is assembled from:
        context_sources
        artifacts
        artifact_provenance

    Returns:
        Path to the generated package.
    """

    PACKAGE_DIR.mkdir(parents=True, exist_ok=True)

    with get_connection() as conn:
        with conn.cursor() as cur:

            # 1. Fetch all artifacts with their source information
            cur.execute(
                """
                SELECT
                    a.artifact_id,
                    a.artifact_type,
                    a.title,
                    a.content,
                    a.source_id,
                    a.source_ref,
                    a.metadata,
                    a.version,
                    a.created_at,
                    a.updated_at,

                    cs.source_type,
                    cs.source_name,
                    cs.connection_identifier,
                    cs.metadata AS source_metadata,
                    cs.status AS source_status

                FROM artifacts a

                LEFT JOIN context_sources cs
                    ON a.source_id = cs.source_id

                ORDER BY a.updated_at ASC
                """
            )

            artifact_rows = cur.fetchall()

            # 2. Fetch provenance
            cur.execute(
                """
                SELECT
                    ap.artifact_id,
                    ap.source_id,
                    ap.source_ref,
                    ap.extraction_method,
                    ap.metadata,
                    ap.created_at
                FROM artifact_provenance ap
                ORDER BY ap.created_at ASC
                """
            )

            provenance_rows = cur.fetchall()

    # 3. Build source registry inside the package

    sources: Dict[str, Dict[str, Any]] = {}
    artifacts = []
    for row in artifact_rows:
        (
            artifact_id,
            artifact_type,
            title,
            content,
            source_id,
            source_ref,
            metadata,
            version,
            created_at,
            updated_at,
            source_type,
            source_name,
            connection_identifier,
            source_metadata,
            source_status,
        ) = row

        source_key = str(source_id) if source_id else None

        if source_key and source_key not in sources:
            sources[source_key] = {
                "source_id": source_key,
                "source_type": source_type,
                "source_name": source_name,
                "connection_identifier": connection_identifier,
                "metadata": source_metadata or {},
                "status": source_status,
            }

        artifacts.append(
            {
                "artifact_id": str(artifact_id),
                "artifact_type": artifact_type,
                "title": title,
                "content": content,
                "source": {
                    "source_id": source_key,
                    "source_type": source_type,
                    "source_name": source_name,
                    "source_ref": source_ref,
                },
                "metadata": metadata or {},
                "version": version,
                "created_at": (
                    created_at.isoformat()
                    if created_at
                    else None
                ),
                "updated_at": (
                    updated_at.isoformat()
                    if updated_at
                    else None
                ),
            }
        )

    # 4. Build provenance section

    provenance = []
    for row in provenance_rows:

        (
            artifact_id,
            source_id,
            source_ref,
            extraction_method,
            metadata,
            created_at,
        ) = row

        provenance.append(
            {
                "artifact_id": str(artifact_id),
                "source_id": (
                    str(source_id)
                    if source_id
                    else None
                ),
                "source_ref": source_ref,
                "extraction_method": extraction_method,
                "metadata": metadata or {},
                "created_at": (
                    created_at.isoformat()
                    if created_at
                    else None
                ),
            }
        )

    # 5. Assemble final agent-specific package

    package = {
        "package": {
            "package_type": "agent_context",
            "package_version": "1.0",
            "target_agent": "planning.discovery_agent",
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },

        "context": {
            "sources": list(sources.values()),
            "artifact_count": len(artifacts),
            "artifacts": artifacts,
            "provenance": provenance,
        },
    }

    # 6. Write package
    package_path = PACKAGE_DIR / "discovery_agent_package.json"

    package_path.write_text(
        json.dumps(
            package,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )

    return package_path


# ===========================================================================
# Architecture package (v2)
# ===========================================================================
CWI_TYPE = "candidate_work_item"
SOURCE_TYPE = "source_artifact"
BASELINE_TYPE = "architecture_baseline"
BLUEPRINT_TYPE = "architecture_blueprint"

# CWIs that never get a blueprint: containers and background material.
SKIP_SIGNALS = {"epic", "context"}

# Architecture-document detection (Confluence pages / uploaded files only).
# An explicit metadata flag always wins: metadata.doc_type == "architecture".
ARCH_TITLE_WORDS = (
    "architecture", "system design", "solution design", "technical design", "tech stack",
    "high level design", "high-level design", "low level design", "hld", "lld",
    "infrastructure", "deployment", "integration design", "data model",
)
ARCH_CONTENT_WORDS = (
    "architecture", "microservice", "api gateway", "database", "postgres", "mysql", "mongodb",
    "kubernetes", "docker", "deployment", "load balancer", "message queue", "kafka", "rabbitmq",
    "redis", "aws", "azure", "gcp", "frontend", "backend", "service layer", "component",
    "rest api", "graphql", "webhook", "data flow", "infrastructure", "ci/cd", "cdn", "cache",
)
ARCH_CONTENT_MIN_HITS = 5
MAX_BASELINE_SOURCE_CHARS = 15000


def _decode(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            out = json.loads(value)
            return out if isinstance(out, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _fingerprint(*parts: Any) -> str:
    raw = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _architecture_reason(title: str, content: str, source_type: str, meta: dict) -> Optional[str]:
    """Why this artifact counts as architecture material, or None."""
    if (meta.get("doc_type") or "").lower() == "architecture":
        return "tagged doc_type=architecture"
    if source_type == "jira":
        return None
    t = (title or "").lower()
    for word in ARCH_TITLE_WORDS:
        if re.search(rf"\b{re.escape(word)}\b", t):
            return f"title mentions '{word}'"
    c = (content or "").lower()
    hits = sorted({w for w in ARCH_CONTENT_WORDS if w in c})
    if len(hits) >= ARCH_CONTENT_MIN_HITS:
        return f"content mentions {', '.join(hits[:6])}"
    return None


def _eligibility(meta: dict, include_unready: bool) -> Optional[str]:
    """Reason a CWI is skipped, or None if it should get a blueprint."""
    if (meta.get("signal_type") or "") in SKIP_SIGNALS:
        return f"{meta.get('signal_type')} (no blueprint needed)"
    if meta.get("is_actionable") is False:
        return "not actionable"
    if not include_unready and meta.get("readiness") == "not_ready":
        return "not_ready — refine it first"
    if (meta.get("review_status") or "proposed") == "rejected":
        return "rejected by product owner"
    return None


_REF_RE = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")


def build_architecture_package(
    project_id: Optional[str] = None,
    limit: Optional[int] = None,
    include_unready: bool = False,
) -> Path:
    """
    Package for the Architecture Discovery & Blueprint Agent.

    Contains:
      * architecture_sources  — client architecture material (Confluence / files)
      * candidate_work_items  — ACTIVE, eligible CWIs, each with its source tickets
                                and related work (dependencies resolved to CWIs)
      * skipped_work_items    — CWIs not needing a blueprint, with the reason
      * existing_baseline     — the stored baseline (reused if still current)
      * existing_blueprints   — stored blueprints per CWI (skipped if still current)
    """
    PACKAGE_DIR.mkdir(parents=True, exist_ok=True)
    project_key = project_id or "all"

    with get_connection() as conn:
        with conn.cursor() as cur:
            # 1. Active CWIs
            sql = f"""
                SELECT artifact_id, title, content, source_id, source_ref, metadata, updated_at
                FROM artifacts
                WHERE artifact_type = %s
                  AND COALESCE(metadata->>'lifecycle', 'active') = 'active'
                  {"AND metadata->>'project_id' = %s" if project_id else ""}
                ORDER BY title
            """
            cur.execute(sql, [CWI_TYPE, project_id] if project_id else [CWI_TYPE])
            cwi_rows = cur.fetchall()

            # 2. Source artifacts of this project only (ingestion must tag project_id)
            sql = f"""
                SELECT a.artifact_id, a.title, a.content, a.source_id, a.source_ref,
                       a.metadata, a.updated_at, cs.source_type
                FROM artifacts a
                LEFT JOIN context_sources cs ON cs.source_id = a.source_id
                WHERE a.artifact_type = %s
                  {"AND a.metadata->>'project_id' = %s" if project_id else ""}
            """
            cur.execute(sql, [SOURCE_TYPE, project_id] if project_id else [SOURCE_TYPE])
            source_rows = cur.fetchall()

            # 3. Existing baseline + blueprints
            cur.execute(
                """
                SELECT artifact_id, content, metadata FROM artifacts
                WHERE artifact_type = %s AND source_ref = %s
                ORDER BY updated_at DESC LIMIT 1
                """,
                [BASELINE_TYPE, f"architecture-baseline:{project_key}"],
            )
            baseline_row = cur.fetchone()

            sql = f"""
                SELECT artifact_id, metadata FROM artifacts
                WHERE artifact_type = %s
                  AND COALESCE(metadata->>'lifecycle', 'active') = 'active'
                  {"AND metadata->>'project_id' = %s" if project_id else ""}
            """
            cur.execute(sql, [BLUEPRINT_TYPE, project_id] if project_id else [BLUEPRINT_TYPE])
            blueprint_rows = cur.fetchall()

    # ---- source artifacts -------------------------------------------------
    sources_by_id: Dict[str, dict] = {}
    architecture_sources: List[dict] = []
    for aid, title, content, sid, sref, meta, updated, stype in source_rows:
        meta = _decode(meta)
        doc = {
            "artifact_id": str(aid),
            "source_id": str(sid) if sid else None,
            "source_type": stype or meta.get("source_type"),
            "source_ref": sref,
            "title": title,
            "content": content or "",
            "updated_at": updated.isoformat() if updated else None,
        }
        sources_by_id[doc["artifact_id"]] = doc
        reason = _architecture_reason(title, content, doc["source_type"] or "", meta)
        if reason:
            architecture_sources.append({**doc, "detection_reason": reason})

    architecture_sources.sort(key=lambda d: d["title"] or "")
    total_chars = sum(len(d["content"]) for d in architecture_sources)
    if total_chars > MAX_BASELINE_SOURCE_CHARS:
        logger.warning("Architecture material is %d chars; prompts clip each document (see MAX_DOC_CHARS).",
                       total_chars)

    # ---- CWIs ----------------------------------------------------------------
    all_cwis = []
    for aid, title, content, sid, sref, meta, updated in cwi_rows:
        meta = _decode(meta)
        all_cwis.append({
            "artifact_id": str(aid),
            "cwi_id": meta.get("cwi_id"),
            "source_id": str(sid) if sid else None,
            "title": title,
            "summary": content or "",
            "business_intent": meta.get("business_intent"),
            "signal_type": meta.get("signal_type"),
            "readiness": meta.get("readiness"),
            "review_status": meta.get("review_status") or "proposed",
            "is_actionable": meta.get("is_actionable"),
            "gaps": meta.get("gaps") or [],
            "decisions": meta.get("decisions") or [],
            "dependencies": meta.get("dependencies") or [],
            "open_questions": meta.get("open_questions") or [],
            "source_refs": meta.get("source_refs") or [],
            "member_artifact_ids": [str(m) for m in meta.get("member_artifact_ids") or []],
            "project_id": meta.get("project_id") or project_id,
            "_meta": meta,
        })

    ref_to_cwi = {ref: c for c in all_cwis for ref in c["source_refs"]}

    eligible, skipped = [], []
    for c in all_cwis:
        reason = _eligibility(c.pop("_meta"), include_unready)
        if reason:
            skipped.append({"artifact_id": c["artifact_id"], "title": c["title"], "reason": reason})
            continue
        c["members"] = [sources_by_id[m] for m in c["member_artifact_ids"] if m in sources_by_id]
        related, seen = [], set()
        for dep in c["dependencies"]:
            for ref in _REF_RE.findall(dep):
                other = ref_to_cwi.get(ref)
                if other and other is not c and ref not in seen:
                    seen.add(ref)
                    related.append({"ref": ref, "title": other["title"],
                                    "signal_type": other["signal_type"], "relation": dep})
        c["related"] = related
        eligible.append(c)

    if limit:
        eligible = eligible[:limit]

    # ---- baseline fingerprint: what the baseline is built from -------------------
    if architecture_sources:
        mode = "discovery"
        baseline_fp = _fingerprint("discovery", [(d["artifact_id"], d["updated_at"]) for d in architecture_sources])
    else:
        mode = "creation"
        baseline_fp = _fingerprint("creation", sorted((c["artifact_id"], c["title"]) for c in eligible))

    existing_baseline = None
    if baseline_row:
        meta = _decode(baseline_row[2])
        existing_baseline = {
            "artifact_id": str(baseline_row[0]),
            "fingerprint": meta.get("fingerprint"),
            "origin": meta.get("origin"),
            "baseline": _decode(baseline_row[1]),
            "alias_to_id": meta.get("alias_to_id") or {},
            "doc_aliases": meta.get("doc_aliases") or {},
        }

    existing_blueprints = {}
    for aid, meta in blueprint_rows:
        meta = _decode(meta)
        if meta.get("cwi_artifact_id"):
            existing_blueprints[str(meta["cwi_artifact_id"])] = {
                "artifact_id": str(aid), "fingerprint": meta.get("fingerprint"),
            }

    package = {
        "package": {
            "package_type": "agent_context",
            "package_version": "2.0",
            "target_agent": "planning.architecture_discovery_blueprint_agent",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "project_id": project_id,
        },
        "context": {
            "project_id": project_id,
            "project_key": project_key,
            "baseline_mode": mode,
            "baseline_fingerprint": baseline_fp,
            "architecture_sources": architecture_sources,
            "candidate_work_items": eligible,
            "skipped_work_items": skipped,
            "existing_baseline": existing_baseline,
            "existing_blueprints": existing_blueprints,
            "counts": {
                "active_cwis": len(all_cwis),
                "eligible_cwis": len(eligible),
                "skipped_cwis": len(skipped),
                "architecture_sources": len(architecture_sources),
            },
        },
    }

    package_path = PACKAGE_DIR / "architecture_discovery_package.json"
    package_path.write_text(json.dumps(package, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return package_path


# ===========================================================================
# Spec Synthesizer package
# ===========================================================================
STORY_TYPE = "spec_story"
MAX_CWIS_PER_GROUP = 5


def _slim_blueprint(bp: dict) -> dict:
    """Only what story writing needs from a blueprint."""
    return {
        "summary": bp.get("summary"),
        "mode": bp.get("mode"),
        "components": [{k: c.get(k) for k in ("name", "change", "what_changes")} for c in bp.get("components") or []],
        "interfaces": [{k: i.get(k) for k in ("name", "kind", "purpose")} for i in bp.get("interfaces") or []],
        "data_changes": bp.get("data_changes") or [],
        "integrations": [{k: i.get(k) for k in ("system", "change")} for i in bp.get("integrations") or []],
        "nfrs": [{k: n.get(k) for k in ("category", "requirement")} for n in bp.get("nfrs") or []],
        "recommended_option": bp.get("recommended_option"),
        "risks": [r.get("risk") for r in bp.get("risks") or [] if r.get("risk")],
        "open_questions": bp.get("open_questions") or [],
    }


def build_spec_package(
    project_id: Optional[str] = None,
    limit: Optional[int] = None,
    include_unready: bool = False,
) -> Path:
    """
    Package for the Spec Synthesizer.

    Work items are grouped by EPIC (one LLM call per group, max MAX_CWIS_PER_GROUP).
    Each work item carries: its source tickets (key, type, existing acceptance criteria),
    its architecture blueprint (slim), related work, decisions/gaps/open questions.
    The project baseline (component names) is included so stories reference real components.
    """
    PACKAGE_DIR.mkdir(parents=True, exist_ok=True)
    project_key = project_id or "all"
    pfilter = "AND metadata->>'project_id' = %s" if project_id else ""
    pparams = [project_id] if project_id else []

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT artifact_id, title, content, source_id, metadata FROM artifacts
                WHERE artifact_type = %s AND COALESCE(metadata->>'lifecycle','active') = 'active' {pfilter}
                ORDER BY title""", [CWI_TYPE, *pparams])
            cwi_rows = cur.fetchall()

            cur.execute(f"""
                SELECT artifact_id, title, content, source_ref, metadata FROM artifacts
                WHERE artifact_type = %s {pfilter}""", [SOURCE_TYPE, *pparams])
            source_rows = cur.fetchall()

            cur.execute("""
                SELECT content, metadata
                FROM artifacts
                WHERE artifact_type = %s
                  AND COALESCE(metadata->>'lifecycle', 'active') = 'active'
            """, [BLUEPRINT_TYPE])
            blueprint_rows = cur.fetchall()

            cur.execute("""
                SELECT content, metadata, source_ref
                FROM artifacts
                WHERE artifact_type = %s
                AND COALESCE(metadata->>'lifecycle', 'active') = 'active'
                AND (
                    source_ref = %s
                    OR metadata->>'project_id' = %s
                )
                ORDER BY
                CASE WHEN source_ref = %s THEN 0 ELSE 1 END,
                updated_at DESC
                LIMIT 1
            """, [
                BASELINE_TYPE,
                f"architecture-baseline:{project_key}",
                project_id or "",
                f"architecture-baseline:{project_key}",
            ])
            baseline_row = cur.fetchone()

            cur.execute(f"""
                SELECT artifact_id, metadata FROM artifacts
                WHERE artifact_type = %s AND COALESCE(metadata->>'lifecycle','active') = 'active' {pfilter}""",
                        [STORY_TYPE, *pparams])
            story_rows = cur.fetchall()

    # ---- sources -----------------------------------------------------------------
    sources = {}
    for aid, title, content, sref, meta in source_rows:
        meta = _decode(meta)
        parent = meta.get("parent") or {}
        epic_key = meta.get("epic_key") or (parent.get("key") if (parent.get("type") or "").lower() == "epic" else None)
        sources[str(aid)] = {
            "artifact_id": str(aid),
            "ref": sref,
            "title": title,
            "issue_type": meta.get("issue_type"),
            "status": meta.get("status"),
            "acceptance_criteria": meta.get("acceptance_criteria"),
            "parent_key": parent.get("key"),
            "epic_key": epic_key,
            "content": content or "",
        }

        # ---- blueprints -----------------------------------------------------------------
        blueprints = {}
        project_cwi_ids = {str(row[0]) for row in cwi_rows}

        for content, meta in blueprint_rows:
            meta = _decode(meta)
            cwi_artifact_id = meta.get("cwi_artifact_id")

            if not cwi_artifact_id:
                continue

            cwi_artifact_id = str(cwi_artifact_id)

            # Only attach blueprints to CWIs in this project's package.
            if cwi_artifact_id not in project_cwi_ids:
                continue

            blueprints[cwi_artifact_id] = {
                "fingerprint": meta.get("fingerprint"),
                **_slim_blueprint(_decode(content)),
            }

    baseline = None
    if baseline_row:
        b, bmeta = _decode(baseline_row[0]), _decode(baseline_row[1])
        baseline = {
            "origin": bmeta.get("origin"),
            "fingerprint": bmeta.get("fingerprint"),
            "summary": b.get("summary"),
            "components": [{"name": c.get("name"), "responsibility": c.get("responsibility")}
                           for c in b.get("components") or []],
            "nfrs": [{"category": n.get("category"), "requirement": n.get("requirement")}
                     for n in b.get("nfrs") or []],
            "constraints": [c.get("item") for c in b.get("constraints") or [] if c.get("item")],
            "integrations": [i.get("name") for i in b.get("integrations") or [] if i.get("name")],
        }

    existing_stories: Dict[str, List[dict]] = {}
    for aid, meta in story_rows:
        meta = _decode(meta)
        if meta.get("cwi_artifact_id"):
            existing_stories.setdefault(str(meta["cwi_artifact_id"]), []).append(
                {"artifact_id": str(aid), "fingerprint": meta.get("fingerprint")})

    # ---- CWIs -----------------------------------------------------------------------
    all_cwis = []
    for aid, title, content, sid, meta in cwi_rows:
        meta = _decode(meta)
        all_cwis.append((str(aid), title, content, sid, meta))
    ref_to_cwi = {ref: (aid, title, meta) for aid, title, _, _, meta in all_cwis
                  for ref in meta.get("source_refs") or []}

    eligible, skipped = [], []
    for aid, title, content, sid, meta in all_cwis:
        reason = _eligibility(meta, include_unready)
        if reason:
            skipped.append({"artifact_id": aid, "title": title, "reason": reason})
            continue
        members = [sources[m] for m in (str(x) for x in meta.get("member_artifact_ids") or []) if m in sources]
        related = []
        for dep in meta.get("dependencies") or []:
            for ref in _REF_RE.findall(dep):
                other = ref_to_cwi.get(ref)
                if other and other[0] != aid:
                    related.append({"ref": ref, "title": other[1], "relation": dep})
        epic_key = next((m["epic_key"] for m in members if m.get("epic_key")), None)
        eligible.append({
            "artifact_id": aid,
            "cwi_id": meta.get("cwi_id"),
            "source_id": str(sid) if sid else None,
            "title": title,
            "summary": content or "",
            "business_intent": meta.get("business_intent"),
            "signal_type": meta.get("signal_type"),
            "readiness": meta.get("readiness"),
            "gaps": meta.get("gaps") or [],
            "conflicts": meta.get("conflicts") or [],
            "decisions": meta.get("decisions") or [],
            "dependencies": meta.get("dependencies") or [],
            "open_questions": meta.get("open_questions") or [],
            "source_refs": meta.get("source_refs") or [],
            "members": members,
            "related": related,
            "epic_key": epic_key,
            "blueprint": blueprints.get(aid),
        })

    if limit:
        eligible = eligible[:limit]

    # ---- group by epic --------------------------------------------------------------
    by_epic: Dict[str, List[dict]] = {}
    for c in eligible:
        by_epic.setdefault(c["epic_key"] or "_no_epic", []).append(c)

    groups = []
    for epic_key, items in sorted(by_epic.items()):
        epic = ref_to_cwi.get(epic_key) if epic_key != "_no_epic" else None
        epic_src = next((src for src in sources.values() if src["ref"] == epic_key), None)
        epic_info = {
            "ref": None if epic_key == "_no_epic" else epic_key,
            "title": (epic[1] if epic else epic_src["title"] if epic_src
                      else "No epic" if epic_key == "_no_epic" else epic_key),
            "intent": (epic[2].get("business_intent") if epic else None),
        }
        for i in range(0, len(items), MAX_CWIS_PER_GROUP):
            groups.append({"epic": epic_info, "work_items": items[i:i + MAX_CWIS_PER_GROUP]})

    package = {
        "package": {
            "package_type": "agent_context",
            "package_version": "1.0",
            "target_agent": "planning.spec_synthesizer",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "project_id": project_id,
        },
        "context": {
            "project_id": project_id,
            "project_key": project_key,
            "baseline": baseline,
            "groups": groups,
            "skipped_work_items": skipped,
            "existing_stories": existing_stories,
            "counts": {
                "eligible_cwis": len(eligible),
                "with_blueprint": sum(1 for c in eligible if c["blueprint"]),
                "groups": len(groups),
                "skipped_cwis": len(skipped),
            },
        },
    }
    path = PACKAGE_DIR / "spec_synthesizer_package.json"
    path.write_text(json.dumps(package, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return path


# ===========================================================================
# Estimation & Risk Agent package
# ===========================================================================

def build_estimation_risk_package(
    project_id: Optional[str] = None,
    limit: Optional[int] = None,
) -> Path:
    """
    Build an estimation package from active Spec stories and related
    Architecture artifacts.

    Spec stories are the primary estimation unit. Architecture context
    is attached where available to help assess implementation effort
    and technical risks.
    """
    PACKAGE_DIR.mkdir(parents=True, exist_ok=True)
    project_key = project_id or "all"
    pfilter = "AND metadata->>'project_id' = %s" if project_id else ""
    params = [project_id] if project_id else []

    with get_connection() as conn:
        with conn.cursor() as cur:
            # 1. Active Spec stories
            cur.execute(
                f"""
                SELECT artifact_id, title, content, source_id,
                       source_ref, metadata, updated_at
                FROM artifacts
                WHERE artifact_type = %s
                  AND COALESCE(metadata->>'lifecycle', 'active') = 'active'
                  {pfilter}
                ORDER BY title
                """,
                [STORY_TYPE, *params],
            )
            story_rows = cur.fetchall()

            # 2. Active architecture blueprints
            cur.execute(
                f"""
                SELECT artifact_id, content, metadata, updated_at
                FROM artifacts
                WHERE artifact_type = %s
                  AND COALESCE(metadata->>'lifecycle', 'active') = 'active'
                  {pfilter}
                ORDER BY updated_at DESC
                """,
                [BLUEPRINT_TYPE, *params],
            )
            blueprint_rows = cur.fetchall()

            # 3. Project architecture baseline
            cur.execute(
                """
                SELECT artifact_id, content, metadata, updated_at
                FROM artifacts
                WHERE artifact_type = %s
                  AND source_ref = %s
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                [BASELINE_TYPE, f"architecture-baseline:{project_key}"],
            )
            baseline_row = cur.fetchone()

    blueprints = {}
    for artifact_id, content, metadata, updated_at in blueprint_rows:
        meta = _decode(metadata)
        cwi_artifact_id = meta.get("cwi_artifact_id")
        if not cwi_artifact_id:
            continue

        blueprints[str(cwi_artifact_id)] = {
            "artifact_id": str(artifact_id),
            "fingerprint": meta.get("fingerprint"),
            "blueprint": _decode(content),
            "updated_at": updated_at.isoformat() if updated_at else None,
        }

    baseline = None
    if baseline_row:
        baseline = {
            "artifact_id": str(baseline_row[0]),
            "baseline": _decode(baseline_row[1]),
            "metadata": _decode(baseline_row[2]),
            "updated_at": (
                baseline_row[3].isoformat()
                if baseline_row[3]
                else None
            ),
        }

    stories = []
    for artifact_id, title, content, source_id, source_ref, metadata, updated_at in story_rows:
        meta = _decode(metadata)
        cwi_artifact_id = meta.get("cwi_artifact_id")

        stories.append({
            "work_item_id": str(artifact_id),
            "artifact_id": str(artifact_id),
            "title": title,
            "content": content or "",
            "source_id": str(source_id) if source_id else None,
            "source_ref": source_ref,
            "metadata": meta,
            "cwi_artifact_id": (
                str(cwi_artifact_id) if cwi_artifact_id else None
            ),
            "architecture": blueprints.get(str(cwi_artifact_id)),
            "acceptance_criteria": (
                meta.get("acceptance_criteria")
                or meta.get("acceptanceCriteria")
                or []
            ),
            "dependencies": meta.get("dependencies") or [],
            "open_questions": meta.get("open_questions") or [],
            "updated_at": updated_at.isoformat() if updated_at else None,
        })

    if limit is not None:
        stories = stories[:limit]

    # Include a stable fingerprint of the selected estimation inputs.
    input_fingerprint = _fingerprint(
        project_id,
        [
            (
                s["artifact_id"],
                s["updated_at"],
                s["cwi_artifact_id"],
            )
            for s in stories
        ],
        [
            (
                key,
                value.get("fingerprint"),
                value.get("updated_at"),
            )
            for key, value in sorted(blueprints.items())
        ],
        baseline,
    )

    package = {
        "package": {
            "package_type": "agent_context",
            "package_version": "1.0",
            "target_agent": "planning.estimation_risk_agent",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "project_id": project_id,
            "input_fingerprint": input_fingerprint,
        },
        "context": {
            "project_id": project_id,
            "project_key": project_key,
            "stories": stories,
            "architecture_baseline": baseline,
            "counts": {
                "stories": len(stories),
                "with_architecture": sum(
                    1 for story in stories if story["architecture"]
                ),
            },
        },
    }

    path = PACKAGE_DIR / "estimation_risk_package.json"
    path.write_text(
        json.dumps(
            package,
            indent=2,
            ensure_ascii=False,
            default=str,
        ),
        encoding="utf-8",
    )
    return path
