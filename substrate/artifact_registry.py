# substrate/artifact_registry.py
import json
from typing import Dict, Any
from substrate.db import get_connection


def upsert_artifact(cwi: Dict[str, Any], source_id: str,) -> str:

    source_refs = cwi.get("source_refs", [])

    if not source_refs:
        raise ValueError("CWI must contain at least one source reference")

    source_ref = source_refs[0]

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
                ON CONFLICT (source_id, source_ref)
                DO UPDATE SET
                    artifact_type = EXCLUDED.artifact_type,
                    title = EXCLUDED.title,
                    content = EXCLUDED.content,
                    metadata = EXCLUDED.metadata,
                    updated_at = EXCLUDED.updated_at,
                    version = artifacts.version + 1
                RETURNING artifact_id
                """,
                {
                    "artifact_type": "candidate_work_item",
                    "title": cwi.get("title"),
                    "content": cwi.get("raw_summary"),
                    "source_id": source_id,
                    "source_ref": source_ref,
                    "metadata": json.dumps({
                        "cwi_id": cwi.get("cwi_id"),
                        "source_type": cwi.get("source_type"),
                        "signal_type": cwi.get("signal_type"),
                        "affected_component": cwi.get("affected_component"),
                        "status": cwi.get("status"),
                    }),
                    "created_at": cwi.get("first_seen"),
                    "updated_at": cwi.get("last_seen"),
                },
            )

            artifact_id = cur.fetchone()[0]

        conn.commit()

    return str(artifact_id)