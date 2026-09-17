"""
Maps extracted document text into the source_artifact schema.
Same shape as jira_mapper/confluence_mapper output, but for
user-uploaded files. Treated as context (not a direct work item),
same reasoning as Confluence pages — a requirement doc describes
things, it isn't itself a ticket.
"""

import hashlib
from datetime import datetime, timezone
from typing import Any, Dict


def map_document_to_cwi(filename: str, text: str, project_id: str) -> Dict[str, Any]:
    # Stable ID from filename — re-uploading the same file updates
    # the same artifact instead of creating a duplicate.
    file_id = hashlib.sha256(filename.encode()).hexdigest()[:16]
    now = datetime.now(timezone.utc).isoformat()

    return {
        "cwi_id": f"cwi-file-{file_id}",
        "title": filename,
        "raw_summary": text[:2000],  # full text goes to the vector store separately; this is a preview
        "source_type": "file_upload",
        "source_refs": [filename],
        "signal_type": "context",
        "needs_review": True,   # same reasoning as Confluence — a doc isn't automatically a work item
        "affected_component": None,
        "status": "uploaded",
        "first_seen": now,
        "last_seen": now,
        "project_id": project_id,
    }