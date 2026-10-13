import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import psycopg.rows

from substrate.db import get_connection


def upsert_artifact(
    cwi: Dict[str, Any],
    source_id: str,
    artifact_type: str = "source_artifact",
    content_override: Optional[str] = None,
    extra_metadata: Optional[Dict[str, Any]] = None,
    project_id: Optional[str] = None,
) -> str:
    """
    Insert or update an artifact row.

    artifact_type defaults to "source_artifact" — raw data straight
    from a connector, NOT yet a CWI. A CWI only exists after the
    Discovery Agent has classified, judged actionability, and
    deduplicated it — callers doing that pass
    artifact_type="candidate_work_item" explicitly.

    content_override lets a caller store different text than
    cwi["raw_summary"] (e.g. Discovery Agent's refined_summary).
    extra_metadata is merged into the base metadata dict — use it
    for fields specific to the artifact_type (e.g. confidence,
    is_actionable, reasoning for refined artifacts).

    On update, metadata is MERGED (existing || new), so keys written by
    someone else — e.g. a human's review_status — survive re-runs.
    """
    source_refs = cwi.get("source_refs", [])
    if not source_refs:
        raise ValueError("CWI must contain at least one source reference")
    source_ref = source_refs[0]

    base_metadata = {
        "cwi_id": cwi.get("cwi_id"),
        "source_type": cwi.get("source_type"),
        "signal_type": cwi.get("signal_type"),
        "affected_component": cwi.get("affected_component"),
        "status": cwi.get("status"),
        "needs_review": cwi.get("needs_review", False),
        "project_id": project_id or cwi.get("project_id"),
    }

    # Merge the rich fields from the mapper (priority, links, parent,
    # comments, quality_flags, ...). Without this they are dropped.
    base_metadata.update(cwi.get("metadata") or {})

    if extra_metadata:
        base_metadata.update(extra_metadata)

    now = datetime.now(timezone.utc)
    created_at = cwi.get("first_seen") or now
    updated_at = cwi.get("last_seen") or now

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO artifacts (
                    artifact_type,
                    title,
                    content,
                    source_id,
                    source_ref,
                    metadata,
                    created_at,
                    updated_at
                )
                VALUES (
                    %(artifact_type)s,
                    %(title)s,
                    %(content)s,
                    %(source_id)s,
                    %(source_ref)s,
                    %(metadata)s,
                    %(created_at)s,
                    %(updated_at)s
                )
                ON CONFLICT (source_id, source_ref, artifact_type)
                DO UPDATE SET
                    title = EXCLUDED.title,
                    content = EXCLUDED.content,
                    metadata = artifacts.metadata || EXCLUDED.metadata,
                    updated_at = EXCLUDED.updated_at,
                    version = artifacts.version + 1
                RETURNING artifact_id
                """,
                {
                    "artifact_type": artifact_type,
                    "title": cwi.get("title"),
                    "content": content_override if content_override is not None else cwi.get("raw_summary"),
                    "source_id": source_id,
                    "source_ref": source_ref,
                    "metadata": json.dumps(base_metadata),
                    "created_at": created_at,
                    "updated_at": updated_at,
                },
            )
            artifact_id = cur.fetchone()[0]
        conn.commit()

    return str(artifact_id)


def supersede_artifacts(artifact_type: str, project_id: str, keep_artifact_ids: List[str]) -> int:
    """
    Mark artifacts of this type/project from earlier runs that were NOT
    re-produced this run as lifecycle="superseded".

    Rows a human has already reviewed (metadata.review_status set to
    anything other than 'proposed') are never touched.

    Returns the number of rows marked.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE artifacts
                   SET metadata = metadata || '{"lifecycle": "superseded"}'::jsonb,
                       updated_at = NOW()
                 WHERE artifact_type = %s
                   AND metadata->>'project_id' = %s
                   AND NOT (artifact_id::text = ANY(%s))
                   AND COALESCE(metadata->>'review_status', 'proposed') = 'proposed'
                   AND COALESCE(metadata->>'lifecycle', 'active') <> 'superseded'
                """,
                (artifact_type, project_id, [str(i) for i in keep_artifact_ids]),
            )
            count = cur.rowcount
        conn.commit()
    return count


def get_artifacts_by_type(artifact_type: str) -> List[Dict[str, Any]]:
    with get_connection() as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                """
                SELECT artifact_id, artifact_type, title, content,
                       source_id, source_ref, metadata, version,
                       created_at, updated_at
                FROM artifacts
                WHERE artifact_type = %s
                ORDER BY updated_at DESC
                """,
                (artifact_type,),
            )
            rows = cur.fetchall()

    for row in rows:
        if isinstance(row["metadata"], str):
            row["metadata"] = json.loads(row["metadata"])

    return rows


def get_artifact_by_id(artifact_id: str) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                "SELECT * FROM artifacts WHERE artifact_id = %s",
                (artifact_id,),
            )
            row = cur.fetchone()

    if row and isinstance(row["metadata"], str):
        row["metadata"] = json.loads(row["metadata"])

    return row