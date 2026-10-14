# mappers/jira_mapper.py
"""
Maps a raw Jira issue into the source-artifact record used by the
Common Substrate.

What changed vs v1:
  * `raw_summary` is no longer truncated to 500 chars. It is now a
    composed, LLM-friendly document (header facts + description +
    acceptance criteria + links + recent comments). This is what gets
    stored as artifacts.content and embedded, so Discovery and RAG
    actually see the ticket.
  * ADF flattening keeps structure (paragraphs, lists, tables, code,
    mentions) and no longer inserts spaces mid-word between marks.
  * All planning-relevant fields are returned under `metadata`
    (upsert_artifact must merge cwi["metadata"] — see note in README).
  * `relations` lists parent/epic, subtasks and issue links so they
    can be written to an artifact_relations table.
  * `quality_flags` gives Discovery cheap, deterministic gap signals
    (missing AC, unestimated, unassigned, blocked, ...).
"""

import re
from datetime import datetime
from typing import Any, Dict, List, Optional

MAX_COMMENTS_IN_CONTENT = 10
MAX_COMMENT_CHARS = 1500

# ----------------------------------------------------------------------
# ADF -> text
# ----------------------------------------------------------------------
_PARAGRAPH_LIKE = {"paragraph", "blockquote", "panel", "expand",
                   "nestedExpand", "decisionItem", "mediaSingle"}


def extract_text_from_adf(node: Any) -> str:
    """Flatten Atlassian Document Format to readable markdown-ish text."""
    text = _render(node)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _render(node: Any) -> str:
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(_render(n) for n in node)
    if not isinstance(node, dict):
        return ""

    t = node.get("type")
    attrs = node.get("attrs") or {}
    content = node.get("content") or []
    inner = lambda: "".join(_render(c) for c in content)  # noqa: E731

    if t == "text":
        return node.get("text", "")
    if t == "hardBreak":
        return "\n"
    if t == "mention":
        return attrs.get("text") or "@user"
    if t == "emoji":
        return attrs.get("text") or attrs.get("shortName", "")
    if t in ("inlineCard", "blockCard", "embedCard"):
        return attrs.get("url", "")
    if t == "status":
        return f"[{attrs.get('text', '')}]"
    if t == "date":
        try:
            return datetime.utcfromtimestamp(int(attrs["timestamp"]) / 1000).strftime("%Y-%m-%d")
        except (KeyError, ValueError, TypeError):
            return ""
    if t == "rule":
        return "\n---\n"
    if t == "heading":
        return "#" * int(attrs.get("level", 1)) + " " + inner().strip() + "\n\n"
    if t in ("bulletList", "orderedList", "taskList", "decisionList"):
        lines = []
        for i, item in enumerate(content, 1):
            if t == "orderedList":
                marker = f"{i}."
            elif t == "taskList":
                marker = "[x]" if (item.get("attrs") or {}).get("state") == "DONE" else "[ ]"
            else:
                marker = "-"
            body = _render(item).strip().replace("\n\n", "\n").replace("\n", "\n  ")
            lines.append(f"{marker} {body}")
        return "\n".join(lines) + "\n\n"
    if t == "codeBlock":
        return "```\n" + inner().strip("\n") + "\n```\n\n"
    if t == "table":
        return inner() + "\n"
    if t == "tableRow":
        return "| " + " | ".join(_render(c).strip().replace("\n", " ") for c in content) + " |\n"
    if t in ("media", "mediaGroup"):
        return ""
    if t in _PARAGRAPH_LIKE:
        return inner().rstrip() + "\n\n"
    return inner()  # doc, listItem, taskItem, tableCell, unknown


def _text(value: Any) -> str:
    """Custom fields may be plain strings or ADF."""
    if isinstance(value, dict) and value.get("type") == "doc":
        return extract_text_from_adf(value)
    return value.strip() if isinstance(value, str) else ""


_AC_HEADING = re.compile(
    r"^\s*(?:#+\s*)?[*_]*\s*(?:acceptance\s+criteria|AC)\s*[*_]*\s*:?[*_]*\s*(.*)$",
    re.IGNORECASE)


def _ac_from_description(description: str) -> str:
    """Pull an 'Acceptance Criteria' section out of the description text, if present."""
    if not description:
        return ""
    lines = description.splitlines()
    for i, line in enumerate(lines):
        m = _AC_HEADING.match(line)
        if not m:
            continue
        body = [m.group(1)] if m.group(1).strip() else []
        for nxt in lines[i + 1:]:
            if nxt.lstrip().startswith("#"):  # next heading ends the section
                break
            body.append(nxt)
        return "\n".join(body).strip()
    return ""


# ----------------------------------------------------------------------
# field helpers
# ----------------------------------------------------------------------
def _name(obj: Optional[dict], key: str = "name") -> Optional[str]:
    return obj.get(key) if isinstance(obj, dict) else None


def _person(obj: Optional[dict]) -> Optional[str]:
    return _name(obj, "displayName")


SIGNAL_BY_TYPE = {
    "bug": "bug", "defect": "bug", "incident": "bug",
    "epic": "epic", "story": "story", "user story": "story",
    "task": "task", "sub-task": "task", "subtask": "task",
    "spike": "spike", "research": "spike",
    "improvement": "improvement", "enhancement": "improvement",
    "new feature": "feature_request", "feature": "feature_request",
}


def classify_signal_type(issue_type_name: str) -> str:
    return SIGNAL_BY_TYPE.get((issue_type_name or "").strip().lower(), "feature_request")


def _story_points(fields: dict, custom: dict) -> Optional[float]:
    val = fields.get(custom.get("story_points", ""))
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def _sprints(fields: dict, custom: dict) -> List[Dict[str, Any]]:
    raw = fields.get(custom.get("sprint", "")) or []
    out = []
    for s in raw if isinstance(raw, list) else [raw]:
        if isinstance(s, dict):
            out.append({"name": s.get("name"), "state": s.get("state"),
                        "start": s.get("startDate"), "end": s.get("endDate")})
    return out


def _links(fields: dict) -> List[Dict[str, Any]]:
    out = []
    for link in fields.get("issuelinks") or []:
        ltype = link.get("type") or {}
        if "outwardIssue" in link:
            other, relation, direction = link["outwardIssue"], ltype.get("outward"), "outward"
        elif "inwardIssue" in link:
            other, relation, direction = link["inwardIssue"], ltype.get("inward"), "inward"
        else:
            continue
        of = other.get("fields") or {}
        out.append({
            "relation": relation, "link_type": ltype.get("name"), "direction": direction,
            "key": other.get("key"), "summary": of.get("summary"),
            "status": _name(of.get("status")),
        })
    return out


def _comments(fields: dict) -> List[Dict[str, Any]]:
    out = []
    for c in (fields.get("comment") or {}).get("comments") or []:
        body = extract_text_from_adf(c.get("body"))
        if body:
            out.append({"author": _person(c.get("author")), "created": c.get("created"),
                        "body": body[:MAX_COMMENT_CHARS]})
    return out


def _history(issue: dict) -> Dict[str, Any]:
    status_changes, churn = [], {"summary": 0, "description": 0, "story_points": 0, "sprint": 0}
    for h in (issue.get("changelog") or {}).get("histories") or []:
        for item in h.get("items") or []:
            field = (item.get("field") or "").lower()
            if field == "status":
                status_changes.append({"from": item.get("fromString"), "to": item.get("toString"),
                                       "at": h.get("created"), "by": _person(h.get("author"))})
            elif field in ("summary", "description", "sprint"):
                churn[field] += 1
            elif "story point" in field:
                churn["story_points"] += 1
    status_changes.sort(key=lambda s: s["at"] or "")
    reopened = sum(1 for s in status_changes
                   if (s["from"] or "").lower() in ("done", "closed", "resolved"))
    return {"status_changes": status_changes, "churn": churn, "reopen_count": reopened}


# ----------------------------------------------------------------------
# main mapper
# ----------------------------------------------------------------------
def map_issue_to_cwi(issue: Dict[str, Any]) -> Dict[str, Any]:
    fields = issue.get("fields") or {}
    key = issue["key"]
    adlc = issue.get("_adlc") or {}
    custom = adlc.get("custom_fields") or {}
    base_url = adlc.get("base_url")

    issue_type = _name(fields.get("issuetype")) or ""
    status = fields.get("status") or {}
    description = extract_text_from_adf(fields.get("description"))
    acceptance = _text(fields.get(custom.get("acceptance_criteria", "")))
    ac_source = "field" if acceptance else None
    if not acceptance:
        # Most Jira sites have no AC field; teams write it inside the description.
        acceptance = _ac_from_description(description)
        ac_source = "description" if acceptance else None
    components = [c["name"] for c in fields.get("components") or [] if c.get("name")]
    labels = fields.get("labels") or []
    links = _links(fields)
    comments = _comments(fields)
    history = _history(issue)
    sprints = _sprints(fields, custom)
    points = _story_points(fields, custom)

    parent = fields.get("parent") or {}
    parent_f = parent.get("fields") or {}
    parent_info = {
        "key": parent.get("key"),
        "summary": parent_f.get("summary"),
        "type": _name(parent_f.get("issuetype")),
    } if parent else None
    epic_key = (parent_info or {}).get("key") if (parent_info or {}).get("type", "").lower() == "epic" \
        else fields.get(custom.get("epic_link", ""))

    subtasks = [s.get("key") for s in fields.get("subtasks") or [] if s.get("key")]
    signal = classify_signal_type(issue_type)
    status_category = _name(status.get("statusCategory"))

    # deterministic gap signals for Discovery
    flags = []
    if not description:
        flags.append("missing_description")
    if signal == "story" and not acceptance:
        flags.append("missing_acceptance_criteria")
    # only flag "unestimated" when this Jira actually has an estimation field
    if ("story_points" in custom and signal in ("story", "task", "bug")
            and points is None and not fields.get("timeoriginalestimate")):
        flags.append("unestimated")
    if not fields.get("assignee") and status_category != "Done":
        flags.append("unassigned")
    if any((l["relation"] or "").lower() == "is blocked by" and (l["status"] or "").lower() != "done"
           for l in links):
        flags.append("blocked")
    if signal != "epic" and not epic_key and not parent_info:
        flags.append("no_epic")
    if history["reopen_count"]:
        flags.append("reopened")
    if history["churn"]["description"] + history["churn"]["summary"] >= 3:
        flags.append("high_scope_churn")

    relations = []
    if parent_info:
        relations.append({"relation": "child_of", "target_ref": parent_info["key"]})
    relations += [{"relation": "parent_of", "target_ref": k} for k in subtasks]
    relations += [{"relation": (l["relation"] or "relates to").lower(), "target_ref": l["key"]}
                  for l in links if l["key"]]

    project_id = key.split("-")[0]
    metadata = {
        "project_id": project_id,
        "issue_key": key,
        "issue_type": issue_type,
        "url": f"{base_url}/browse/{key}" if base_url else None,
        "status_category": status_category,
        "priority": _name(fields.get("priority")),
        "resolution": _name(fields.get("resolution")),
        "labels": labels,
        "components": components,
        "fix_versions": [_name(v) for v in fields.get("fixVersions") or []],
        "affects_versions": [_name(v) for v in fields.get("versions") or []],
        "assignee": _person(fields.get("assignee")),
        "reporter": _person(fields.get("reporter")),
        "parent": parent_info,
        "epic_key": epic_key,
        "epic_name": fields.get(custom.get("epic_name", "")),
        "subtasks": subtasks,
        "links": links,
        "story_points": points,
        "original_estimate_sec": fields.get("timeoriginalestimate"),
        "sprints": sprints,
        "current_sprint": next((s["name"] for s in sprints if s["state"] == "active"), None),
        "team": _name(fields.get(custom.get("team", ""))) if isinstance(
            fields.get(custom.get("team", "")), dict) else fields.get(custom.get("team", "")),
        "due_date": fields.get("duedate"),
        "resolved_at": fields.get("resolutiondate"),
        "acceptance_criteria": acceptance or None,
        "acceptance_criteria_source": ac_source,
        "comment_count": len(comments),
        "comments": comments[-20:],
        "attachments": [a.get("filename") for a in fields.get("attachment") or []],
        "history": history,
        "quality_flags": flags,
    }

    return {
        "cwi_id": f"cwi-jira-{key}",
        "project_id": project_id,
        "title": fields.get("summary", ""),
        "description": description,
        "raw_summary": _compose_content(key, fields.get("summary", ""), issue_type,
                                        status.get("name"), metadata, description),
        "source_type": "jira",
        "source_refs": [key],
        "signal_type": signal,
        "needs_review": bool({"missing_description", "missing_acceptance_criteria"} & set(flags)),
        "affected_component": components[0] if components else None,  # back-compat
        "status": status.get("name"),
        "first_seen": fields.get("created"),
        "last_seen": fields.get("updated"),
        "metadata": metadata,
        "relations": relations,
    }


def _compose_content(key, summary, issue_type, status, m, description) -> str:
    """One readable document per issue: what gets stored, embedded and shown to the LLM."""
    head = [f"[{key}] {summary}",
            f"Type: {issue_type} | Status: {status} | Priority: {m['priority'] or '-'}"]
    if m["epic_key"] or m["parent"]:
        p = m["parent"] or {}
        head.append(f"Parent: {p.get('key') or m['epic_key']} {p.get('summary') or ''}".rstrip())
    if m["components"] or m["labels"]:
        head.append(f"Components: {', '.join(m['components']) or '-'} | Labels: {', '.join(m['labels']) or '-'}")
    if m["story_points"] is not None or m["current_sprint"] or m["fix_versions"]:
        head.append(f"Story points: {m['story_points'] if m['story_points'] is not None else '-'}"
                    f" | Sprint: {m['current_sprint'] or '-'}"
                    f" | Fix version: {', '.join(v for v in m['fix_versions'] if v) or '-'}")
    if m["assignee"]:
        head.append(f"Assignee: {m['assignee']}")

    parts = ["\n".join(head)]
    if description:
        parts.append("Description:\n" + description)
    if m["acceptance_criteria"] and m["acceptance_criteria_source"] == "field":
        parts.append("Acceptance criteria:\n" + m["acceptance_criteria"])
    if m["links"]:
        parts.append("Links:\n" + "\n".join(
            f"- {l['relation']} {l['key']} ({l['summary'] or ''}) [{l['status'] or '?'}]" for l in m["links"]))
    if m["subtasks"]:
        parts.append("Subtasks: " + ", ".join(m["subtasks"]))
    if m["comments"]:
        recent = m["comments"][-MAX_COMMENTS_IN_CONTENT:]
        parts.append("Recent comments:\n" + "\n".join(
            f"- {c['author'] or '?'} ({(c['created'] or '')[:10]}): {c['body']}" for c in recent))
    if m["quality_flags"]:
        parts.append("Quality flags: " + ", ".join(m["quality_flags"]))
    return "\n\n".join(parts)