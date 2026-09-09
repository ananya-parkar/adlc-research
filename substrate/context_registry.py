# substrate/context_registry.py
import json
from datetime import datetime, timezone
from typing import Optional
from substrate.db import get_connection

def register_source(
    source_type: str,
    source_name: str,
    source_config: Optional[dict] = None,
) -> str:
    """
    Register a context source and return its UUID.

    The source is uniquely identified by:
        (source_type, source_name)
    """

    with get_connection() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                INSERT INTO context_sources (
                    source_type,
                    source_name,
                    metadata,
                    status
                )
                VALUES (%s, %s, %s, 'active')
                ON CONFLICT (source_type, source_name)
                DO UPDATE SET
                    metadata = EXCLUDED.metadata,
                    updated_at = NOW()
                RETURNING source_id
                """,
                (
                    source_type,
                    source_name,
                    json.dumps(source_config or {}),
                ),
            )

            source_id = cur.fetchone()[0]

        conn.commit()

    return str(source_id)

def record_provenance(
    artifact_id: str,
    source_id: str,
    source_ref: str,
    extraction_method: str = "connector",
) -> None:

    with get_connection() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT 1
                FROM artifact_provenance
                WHERE artifact_id = %s
                  AND source_id = %s
                  AND source_ref = %s
                """,
                (
                    artifact_id,
                    source_id,
                    source_ref,
                ),
            )

            if cur.fetchone() is None:

                cur.execute(
                    """
                    INSERT INTO artifact_provenance (
                        artifact_id,
                        source_id,
                        source_ref,
                        extraction_method
                    )
                    VALUES (%s, %s, %s, %s)
                    """,
                    (
                        artifact_id,
                        source_id,
                        source_ref,
                        extraction_method,
                    ),
                )

        conn.commit()
def record_sync(
    source_id: str,
    status: str = "success",
    items_fetched: int = 0,
    items_created: int = 0,
    items_updated: int = 0,
    error_message: Optional[str] = None,
) -> None:

    now = datetime.now(timezone.utc)

    with get_connection() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                INSERT INTO context_syncs (
                    source_id,
                    started_at,
                    completed_at,
                    status,
                    items_fetched,
                    items_created,
                    items_updated,
                    error_message
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    source_id,
                    now,
                    now,
                    status,
                    items_fetched,
                    items_created,
                    items_updated,
                    error_message,
                ),
            )

        conn.commit()