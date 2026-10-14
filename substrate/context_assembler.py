# substrate/context_assembler.py
"""
Context Assembly Layer
Builds agent-specific context packages from the Common Substrate.
The agent does NOT query PostgreSQL directly.
The substrate assembles the package and the agent consumes it.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict
from substrate.db import get_connection
from substrate.vector_store import search_similar

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

def build_architecture_package(project_id: str | None = None, limit: int = 25,) -> Path:
    with get_connection() as conn:
        with conn.cursor() as cur:

            # 1. Fetch persisted Candidate Work Items.
            conditions = ["a.artifact_type = 'candidate_work_item'"]
            params = []

            if project_id:
                conditions.append(
                    "a.metadata->>'project_id' = %s"
                )
                params.append(project_id)

            cur.execute(
                f"""
                SELECT
                    a.artifact_id,
                    a.title,
                    a.content,
                    a.source_id,
                    a.source_ref,
                    a.metadata,
                    a.version,
                    a.created_at,
                    a.updated_at
                FROM artifacts a
                WHERE {" AND ".join(conditions)}
                ORDER BY a.updated_at DESC
                LIMIT %s
                """,
                [*params, limit],
            )

            cwi_rows = cur.fetchall()

            cwis = []
            member_ids = set()

            for row in cwi_rows:
                metadata = row[5] or {}

                if isinstance(metadata, str):
                    metadata = json.loads(metadata)

                member_ids.update(
                    str(member_id)
                    for member_id in metadata.get(
                        "member_artifact_ids", []
                    )
                )

                cwis.append({
                    "artifact_id": str(row[0]),
                    "artifact_type": "candidate_work_item",
                    "title": row[1],
                    "content": row[2],
                    "source_id": str(row[3]) if row[3] else None,
                    "source_ref": row[4],
                    "metadata": metadata,
                    "version": row[6],
                    "created_at": (
                        row[7].isoformat() if row[7] else None
                    ),
                    "updated_at": (
                        row[8].isoformat() if row[8] else None
                    ),
                })

            # 2. Fetch source artifacts linked to the CWIs.
            source_artifacts = []

            if member_ids:
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
                        cs.source_name
                    FROM artifacts a
                    LEFT JOIN context_sources cs
                        ON cs.source_id = a.source_id
                    WHERE a.artifact_id = ANY(%s::uuid[])
                    ORDER BY a.updated_at DESC
                    """,
                    [list(member_ids)],
                )

                for row in cur.fetchall():
                    metadata = row[6] or {}

                    if isinstance(metadata, str):
                        metadata = json.loads(metadata)

                    source_artifacts.append({
                        "artifact_id": str(row[0]),
                        "artifact_type": row[1],
                        "title": row[2],
                        "content": row[3],
                        "source_id": str(row[4]) if row[4] else None,
                        "source_ref": row[5],
                        "metadata": metadata,
                        "version": row[7],
                        "created_at": (
                            row[8].isoformat() if row[8] else None
                        ),
                        "updated_at": (
                            row[9].isoformat() if row[9] else None
                        ),
                        "source": {
                            "source_type": row[10],
                            "source_name": row[11],
                            "source_id": (
                                str(row[4]) if row[4] else None
                            ),
                            "source_ref": row[5],
                        },
                    })

    # 3. Retrieve supporting knowledge chunks.
    # Restrict results to source artifacts linked to the selected CWIs.
    retrieved_chunks = []
    allowed_artifact_ids = {
        artifact["artifact_id"]
        for artifact in source_artifacts
    }

    for cwi in cwis:
        query = (
            f"{cwi.get('title') or ''}\n"
            f"{cwi.get('content') or ''}"
        ).strip()

        if not query or not allowed_artifact_ids:
            continue

        try:
            chunks = search_similar(
                query_text=query,
                top_k=10,
                project_id=project_id,
            )

            for chunk in chunks:
                if str(chunk.get("artifact_id")) not in allowed_artifact_ids:
                    continue

                retrieved_chunks.append({
                    **chunk,
                    "for_cwi_artifact_id": cwi["artifact_id"],
                })

        except Exception:
            # Source artifact content is still available if vector
            # retrieval fails.
            import logging
            logging.getLogger(__name__).exception(
                "Vector retrieval failed for CWI %s",
                cwi["artifact_id"],
            )

    # 4. Assemble an agent-specific context package.
    package = {
        "package": {
            "package_type": "agent_context",
            "package_version": "1.0",
            "target_agent": (
                "planning.architecture_discovery_blueprint_agent"
            ),
            "generated_at": datetime.now(
                timezone.utc
            ).isoformat(),
            "project_id": project_id,
        },
        "context": {
            "candidate_work_items": cwis,
            "artifacts": source_artifacts,
            "retrieved_chunks": retrieved_chunks,
            "counts": {
                "candidate_work_items": len(cwis),
                "source_artifacts": len(source_artifacts),
                "retrieved_chunks": len(retrieved_chunks),
            },
        },
    }

    package_path = (
        PACKAGE_DIR / "architecture_discovery_package.json"
    )

    package_path.write_text(
        json.dumps(
            package,
            indent=2,
            ensure_ascii=False,
            default=str,
        ),
        encoding="utf-8",
    )

    return package_path
