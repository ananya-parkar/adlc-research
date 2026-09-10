"""
Confluence Cloud connector implementing the standard SourceConnector
interface (same shape as JiraConnector / AdoConnector).

Confluence lives on the same Atlassian site as Jira, so it reuses
the exact same ApiTokenAuth (email + API token) — no new credentials
needed if you're already connected to Jira on the same instance.

API notes:
  - Uses Confluence REST API v2 (newer, cursor-based pagination)
  - Space key must first be resolved to a space ID
  - Page body comes back in "storage format" (XHTML-like), not
    plain text or ADF like Jira — needs its own stripping logic
"""

import logging
from datetime import datetime
from typing import Any, Dict, Iterator, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from auth import AuthProvider
from connectors.base_connector import SourceConnector

logger = logging.getLogger(__name__)


class ConfluenceConnector(SourceConnector):
    def __init__(
        self,
        base_url: str,
        auth: AuthProvider,
        space_key: str,
        page_size: int = 50,
    ):
        # Confluence sits under /wiki on the same Atlassian site as Jira
        self.base_url = base_url.rstrip("/")
        self.auth = auth
        self.space_key = space_key
        self.page_size = page_size
        self._session = self._build_session()
        self._space_id: Optional[str] = None  # resolved lazily on first use

    @property
    def source_name(self) -> str:
        return "confluence"

    def _build_session(self) -> requests.Session:
        session = requests.Session()
        retry = Retry(
            total=4,
            backoff_factor=1.5,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        return session

    def test_connection(self) -> bool:
        try:
            resp = self._session.get(
                f"{self.base_url}/wiki/rest/api/user/current",
                auth=self.auth.credentials(),
                timeout=15,
            )
            resp.raise_for_status()
            logger.info(
                "Confluence connection OK: authenticated as %s",
                resp.json().get("displayName"),
            )
            return True
        except requests.RequestException as exc:
            logger.error("Confluence connection test failed: %s", exc)
            return False

    def _resolve_space_id(self) -> str:
        if self._space_id:
            return self._space_id

        resp = self._session.get(
            f"{self.base_url}/wiki/api/v2/spaces",
            params={"keys": self.space_key},
            auth=self.auth.credentials(),
            timeout=15,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if not results:
            raise ValueError(f"No Confluence space found with key '{self.space_key}'")

        self._space_id = results[0]["id"]
        return self._space_id

    def fetch_items(self, since: Optional[datetime] = None) -> Iterator[Dict[str, Any]]:
        space_id = self._resolve_space_id()

        url = f"{self.base_url}/wiki/api/v2/pages"
        params: Dict[str, Any] = {
            "space-id": space_id,
            "limit": self.page_size,
            "body-format": "storage",
        }

        total_fetched = 0
        while True:
            resp = self._session.get(
                url, params=params, auth=self.auth.credentials(), timeout=30
            )
            resp.raise_for_status()
            data = resp.json()

            for page in data.get("results", []):
                # Client-side filter for incremental sync — v2 API doesn't
                # support a direct "updated since" query param on this endpoint
                if since:
                    updated_at = page.get("version", {}).get("createdAt")
                    if updated_at and datetime.fromisoformat(updated_at.replace("Z", "+00:00")) < since:
                        continue
                total_fetched += 1
                yield page

            next_link = data.get("_links", {}).get("next")
            if not next_link:
                break
            # next_link is a relative path with its own query params — use it directly
            url = f"{self.base_url}{next_link}"
            params = {}  # already encoded in next_link

        logger.info("Fetched %d pages from space %s", total_fetched, self.space_key)