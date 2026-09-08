"""
Thin wrapper around the Jira Cloud REST API v3 search endpoint.
Handles auth + pagination. Reads credentials from environment variables
(loaded via .env by the caller) rather than at import time, so this
module can be imported safely before env vars are set.
"""

import os
from typing import Any, Dict, Iterator

import requests

FIELDS = [
    "summary",
    "description",
    "issuetype",
    "status",
    "created",
    "updated",
    "components",
]


def _get_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def fetch_issues(jql: str, page_size: int = 50) -> Iterator[Dict[str, Any]]:
    """
    Yield raw Jira issues matching the given JQL query, transparently
    paging through results using the nextPageToken cursor.
    """
    jira_url = _get_env("JIRA_URL").rstrip("/")
    jira_email = _get_env("JIRA_EMAIL")
    jira_token = _get_env("JIRA_API_TOKEN")

    url = f"{jira_url}/rest/api/3/search/jql"
    auth = (jira_email, jira_token)
    params: Dict[str, Any] = {
        "jql": jql,
        "fields": ",".join(FIELDS),
        "maxResults": page_size,
    }

    next_token = None
    while True:
        if next_token:
            params["nextPageToken"] = next_token

        resp = requests.get(url, params=params, auth=auth, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        for issue in data.get("issues", []):
            yield issue

        next_token = data.get("nextPageToken")
        if not next_token:
            break


def fetch_fields_metadata() -> list:
    """
    Discovery helper — call this once per new client to see all
    standard + custom fields available on their instance (useful for
    finding custom Acceptance Criteria / Story Points field IDs).
    """
    jira_url = _get_env("JIRA_URL").rstrip("/")
    jira_email = _get_env("JIRA_EMAIL")
    jira_token = _get_env("JIRA_API_TOKEN")

    url = f"{jira_url}/rest/api/3/field"
    resp = requests.get(url, auth=(jira_email, jira_token), timeout=30)
    resp.raise_for_status()
    return resp.json()