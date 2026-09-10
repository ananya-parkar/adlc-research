"""
Maps a raw Confluence page into the CWI schema.

Important distinction from Jira/ADO mappers: a Confluence page is
usually *context* (a requirement doc, ADR, meeting note), not
directly a "candidate work item" the way a Jira ticket is. It might
describe a future feature, or it might just be background reading.

So every Confluence-sourced CWI gets `needs_review: true` — Spec
Synthesizer (or a human) should decide whether this page actually
represents actionable work, rather than treating every page as a
work item automatically.
"""

import re
from typing import Any, Dict


def strip_storage_format(html: str) -> str:
    """Confluence's 'storage format' is XHTML-like — simple tag strip is enough for now."""
    if not html:
        return ""
    text = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text).strip()


def map_page_to_cwi(page: Dict[str, Any]) -> Dict[str, Any]:
    page_id = page["id"]
    title = page.get("title", "")

    body_storage = page.get("body", {}).get("storage", {}).get("value", "")
    plain_text = strip_storage_format(body_storage)

    version = page.get("version", {})

    return {
        "cwi_id": f"cwi-confluence-{page_id}",
        "title": title,
        "raw_summary": plain_text[:500] if plain_text else title,
        "source_type": "confluence",
        "source_refs": [str(page_id)],
        "signal_type": "context",  # not bug/feature_request — this is background material
        "needs_review": True,      # human/Spec Synthesizer decides if this is actionable
        "affected_component": None,
        "status": page.get("status"),
        "first_seen": version.get("createdAt"),
        "last_seen": version.get("createdAt"),
    }