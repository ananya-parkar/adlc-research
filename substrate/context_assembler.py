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