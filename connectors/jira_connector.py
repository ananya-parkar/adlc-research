# connectors/jira_connector.py
"""
Jira Cloud connector implementing the standard SourceConnector interface.

What changed vs v1:
  * Fetches the full set of planning-relevant standard fields (links,
    parent/epic, labels, priority, assignee, versions, comments, ...),
    not just 7.
  * Resolves custom fields (Story Points, Sprint, Acceptance Criteria,
    Epic Name, Team, ...) BY NAME via /rest/api/3/field, because their
    customfield_XXXXX ids differ per Jira instance. Ids can be pinned
    with `custom_field_overrides` if an instance uses unusual names.
  * Pulls the changelog (status history / scope churn) and the FULL
    comment list (search results truncate comments).
  * Incremental sync is timezone-correct: JQL `updated >=` is evaluated
    in the authenticated user's Jira timezone, so we convert the stored
    UTC timestamp into that zone and subtract an overlap buffer.
  * Pages in ASC order so issues updated mid-sync aren't skipped.

Each yielded issue gets an extra `_adlc` key carrying the resolved
custom-field ids and base URL, so the mapper can read custom fields
without hardcoding ids.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterator, List, Optional
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from auth import AuthProvider
from connectors.base_connector import SourceConnector

logger = logging.getLogger(__name__)

STANDARD_FIELDS: List[str] = [
    # identity / classification
    "summary", "description", "issuetype", "status", "priority",
    "labels", "components", "resolution", "environment",
    # hierarchy & dependencies
    "parent", "subtasks", "issuelinks",
    # people
    "assignee", "reporter", "creator",
    # planning / delivery
    "fixVersions", "versions", "duedate",
    "timeoriginalestimate", "timeestimate", "timespent",
    # dates
    "created", "updated", "resolutiondate",
    # discussion & evidence
    "comment", "attachment",
]

# logical name -> possible display names (lowercase) across Jira instances
CUSTOM_FIELD_ALIASES: Dict[str, List[str]] = {
    "story_points": ["story points", "story point estimate"],
    "sprint": ["sprint"],
    "acceptance_criteria": ["acceptance criteria", "acceptance criterion"],
    "epic_name": ["epic name"],
    "epic_link": ["epic link"],
    "team": ["team"],
    "start_date": ["start date"],
    "flagged": ["flagged"],
}

SYNC_OVERLAP = timedelta(minutes=5)
MAX_PAGE = 100


class JiraConnector(SourceConnector):
    def __init__(
        self,
        base_url: str,
        auth: AuthProvider,
        project_key: str,
        extra_fields: Optional[List[str]] = None,
        custom_field_overrides: Optional[Dict[str, str]] = None,
        page_size: int = 50,
        fetch_full_comments: bool = True,
        fetch_full_changelog: bool = True,
    ):
        self.base_url = base_url.rstrip("/")
        self.auth = auth
        self.project_key = project_key
        self.extra_fields = extra_fields or []
        self.custom_field_overrides = custom_field_overrides or {}
        self.page_size = min(page_size, MAX_PAGE)
        self.fetch_full_comments = fetch_full_comments
        self.fetch_full_changelog = fetch_full_changelog

        self._session = self._build_session()
        self._custom_fields: Optional[Dict[str, str]] = None
        self._user_tz: ZoneInfo = ZoneInfo("UTC")

    # ------------------------------------------------------------------
    # plumbing
    # ------------------------------------------------------------------
    @property
    def source_name(self) -> str:
        return "jira"

    def _build_session(self) -> requests.Session:
        session = requests.Session()
        retry = Retry(
            total=4,
            backoff_factor=1.5,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        session.headers.update({"Accept": "application/json"})
        return session

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None, timeout: int = 30) -> Any:
        resp = self._session.get(
            f"{self.base_url}{path}",
            params=params,
            auth=self.auth.credentials(),
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # connection + metadata
    # ------------------------------------------------------------------
    def test_connection(self) -> bool:
        try:
            me = self._get("/rest/api/3/myself", timeout=15)
            tz_name = me.get("timeZone") or "UTC"
            try:
                self._user_tz = ZoneInfo(tz_name)
            except Exception:  # unknown tz string
                logger.warning("Unknown Jira timezone %r, falling back to UTC", tz_name)
                self._user_tz = ZoneInfo("UTC")
            logger.info(
                "Jira connection OK: authenticated as %s (tz=%s)",
                me.get("displayName"), self._user_tz,
            )
            return True
        except requests.RequestException as exc:
            logger.error("Jira connection test failed: %s", exc)
            return False

    def fetch_fields_metadata(self) -> list:
        """All standard + custom fields on this instance."""
        return self._get("/rest/api/3/field")

    def resolve_custom_fields(self) -> Dict[str, str]:
        """Map logical names (story_points, sprint, ...) -> customfield ids."""
        if self._custom_fields is not None:
            return self._custom_fields

        by_name: Dict[str, str] = {}
        for f in self.fetch_fields_metadata():
            if f.get("custom"):
                by_name.setdefault((f.get("name") or "").strip().lower(), f["id"])

        resolved: Dict[str, str] = {}
        for logical, aliases in CUSTOM_FIELD_ALIASES.items():
            for alias in aliases:
                if alias in by_name:
                    resolved[logical] = by_name[alias]
                    break
        resolved.update(self.custom_field_overrides)

        missing = sorted(set(CUSTOM_FIELD_ALIASES) - set(resolved))
        logger.info("Resolved Jira custom fields: %s", resolved)
        if missing:
            logger.info("Custom fields not found on this instance (ok if unused): %s", missing)

        self._custom_fields = resolved
        return resolved

    # ------------------------------------------------------------------
    # fetching
    # ------------------------------------------------------------------
    def _since_clause(self, since: datetime) -> str:
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)  # stored state is UTC
        local = (since - SYNC_OVERLAP).astimezone(self._user_tz)
        return f' AND updated >= "{local:%Y-%m-%d %H:%M}"'

    def fetch_items(self, since: Optional[datetime] = None) -> Iterator[Dict[str, Any]]:
        custom = self.resolve_custom_fields()
        fields = list(dict.fromkeys(STANDARD_FIELDS + self.extra_fields + list(custom.values())))

        jql = f'project = "{self.project_key}"'
        if since:
            jql += self._since_clause(since)
        jql += " ORDER BY updated ASC, key ASC"
        logger.info("JQL: %s", jql)

        params: Dict[str, Any] = {
            "jql": jql,
            "fields": ",".join(fields),
            "expand": "changelog",
            "maxResults": self.page_size,
        }

        total = 0
        next_token = None
        while True:
            if next_token:
                params["nextPageToken"] = next_token
            try:
                data = self._get("/rest/api/3/search/jql", params=params)
            except requests.RequestException as exc:
                logger.error("Jira fetch failed after retries: %s", exc)
                raise

            for issue in data.get("issues", []):
                self._enrich(issue)
                issue["_adlc"] = {"custom_fields": custom, "base_url": self.base_url}
                total += 1
                yield issue

            next_token = data.get("nextPageToken")
            if not next_token or data.get("isLast"):
                break

        logger.info("Fetched %d issues from project %s", total, self.project_key)

    # ------------------------------------------------------------------
    # enrichment: full comments / changelog when search truncated them
    # ------------------------------------------------------------------
    def _enrich(self, issue: Dict[str, Any]) -> None:
        key = issue["key"]
        fields = issue.setdefault("fields", {})

        if self.fetch_full_comments:
            block = fields.get("comment") or {}
            have = len(block.get("comments") or [])
            if block.get("total", 0) > have:
                fields["comment"] = {"comments": self._paged(
                    f"/rest/api/3/issue/{key}/comment", "comments")}

        if self.fetch_full_changelog:
            log = issue.get("changelog") or {}
            have = len(log.get("histories") or [])
            if log.get("total", 0) > have:
                issue["changelog"] = {"histories": self._paged(
                    f"/rest/api/3/issue/{key}/changelog", "values")}

    def _paged(self, path: str, items_key: str) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        start = 0
        while True:
            data = self._get(path, params={"startAt": start, "maxResults": MAX_PAGE})
            batch = data.get(items_key) or []
            out.extend(batch)
            start += len(batch)
            if not batch or data.get("isLast") or start >= data.get("total", start):
                break
        return out