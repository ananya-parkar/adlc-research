# connectors/jira_connector.py
"""
Jira Cloud connector implementing the standard SourceConnector
interface. Handles auth (via an injected AuthProvider, so OAuth can
be swapped in later), automatic retry on transient failures (429s,
5xx, network errors), and incremental sync using JQL's `updated >=`
clause.
"""

import logging
from datetime import datetime
from typing import Any, Dict, Iterator, List, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from auth import AuthProvider
from connectors.base_connector import SourceConnector

logger = logging.getLogger(__name__)

DEFAULT_FIELDS = [
    "summary",
    "description",
    "issuetype",
    "status",
    "created",
    "updated",
    "components",
]


class JiraConnector(SourceConnector):
    def __init__(
        self,
        base_url: str,
        auth: AuthProvider,
        project_key: str,
        fields: Optional[List[str]] = None,
        page_size: int = 50,
    ):
        self.base_url = base_url.rstrip("/")
        self.auth = auth
        self.project_key = project_key
        self.fields = fields or DEFAULT_FIELDS
        self.page_size = page_size
        self._session = self._build_session()

    @property
    def source_name(self) -> str:
        return "jira"

    def _build_session(self) -> requests.Session:
        """Session with automatic retry/backoff on transient errors."""
        session = requests.Session()
        retry = Retry(
            total=4,
            backoff_factor=1.5,  # waits ~1.5s, 3s, 6s, 12s between retries
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    def test_connection(self) -> bool:
        """Cheap call to verify credentials work before a full sync."""
        try:
            resp = self._session.get(
                f"{self.base_url}/rest/api/3/myself",
                auth=self.auth.credentials(),
                timeout=15,
            )
            resp.raise_for_status()
            logger.info("Jira connection OK: authenticated as %s", resp.json().get("displayName"))
            return True
        except requests.RequestException as exc:
            logger.error("Jira connection test failed: %s", exc)
            return False

    def fetch_fields_metadata(self) -> list:
        """Discovery helper — list all standard + custom fields on this instance."""
        resp = self._session.get(
            f"{self.base_url}/rest/api/3/field",
            auth=self.auth.credentials(),
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def fetch_items(self, since: Optional[datetime] = None) -> Iterator[Dict[str, Any]]:
        jql = f"project = {self.project_key}"
        if since:
            # Jira JQL expects "yyyy/MM/dd HH:mm" in local time
            since_str = since.strftime("%Y/%m/%d %H:%M")
            jql += f' AND updated >= "{since_str}"'
        jql += " ORDER BY updated DESC"

        url = f"{self.base_url}/rest/api/3/search/jql"
        params: Dict[str, Any] = {
            "jql": jql,
            "fields": ",".join(self.fields),
            "maxResults": self.page_size,
        }

        next_token = None
        total_fetched = 0
        while True:
            if next_token:
                params["nextPageToken"] = next_token

            try:
                resp = self._session.get(
                    url, params=params, auth=self.auth.credentials(), timeout=30
                )
                resp.raise_for_status()
            except requests.RequestException as exc:
                logger.error("Jira fetch failed after retries: %s", exc)
                raise

            data = resp.json()
            issues = data.get("issues", [])
            total_fetched += len(issues)
            for issue in issues:
                yield issue

            next_token = data.get("nextPageToken")
            if not next_token:
                break

        logger.info("Fetched %d issues from project %s", total_fetched, self.project_key)