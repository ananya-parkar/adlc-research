# mappers/jira_mapper.py
"""
Maps a raw Jira issue payload into the MVP Candidate Work Item (CWI)
schema used across the Planning phase.

Discovery Agent stays "thin" here: it identifies and links signals,
it doesn't try to write a polished summary. That's Spec Synthesizer's
job downstream.
"""


from typing import Any, Dict, List


def extract_text_from_adf(node: Any) -> str:
    """
    Jira's `description` field is returned in Atlassian Document
    Format (ADF), a nested JSON tree, not plain text. This flattens
    it to plain text good enough for an LLM prompt.
    """
    if node is None:
        return ""
    if isinstance(node, str):
        return node

    text_parts: List[str] = []
    if isinstance(node, dict):
        if node.get("type") == "text":
            text_parts.append(node.get("text", ""))
        for child in node.get("content", []) or []:
            text_parts.append(extract_text_from_adf(child))

    return " ".join(part for part in text_parts if part)


def classify_signal_type(issue_type_name: str) -> str:
    """Rough rule-based classification — swap for something smarter later."""
    name = issue_type_name.lower()
    if "bug" in name:
        return "bug"
    return "feature_request"


def map_issue_to_cwi(issue: Dict[str, Any]) -> Dict[str, Any]:
    fields = issue["fields"]
    key = issue["key"]

    description = extract_text_from_adf(fields.get("description"))
    components = [c["name"] for c in (fields.get("components") or [])]

    return {
        "cwi_id": f"cwi-jira-{key}",
        "title": fields.get("summary", ""),
        "raw_summary": (description[:500] if description else fields.get("summary", "")),
        "source_type": "jira",
        "source_refs": [key],
        "signal_type": classify_signal_type(fields.get("issuetype", {}).get("name", "")),
        "affected_component": components[0] if components else None,
        "status": fields.get("status", {}).get("name"),
        "first_seen": fields.get("created"),
        "last_seen": fields.get("updated"),
    }