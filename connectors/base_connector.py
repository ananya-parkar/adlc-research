# connectors/base_connector.py
"""
Standard interface every source connector (Jira, Confluence, ADO, ...)
implements. Keeping this abstract means Discovery Agent's orchestration
code never needs to know which source it's talking to — it just calls
test_connection() and fetch_items() on whatever connector it's given.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Dict, Iterator, Optional


class SourceConnector(ABC):
    """Base class for all context-source connectors."""

    @property
    @abstractmethod
    def source_name(self) -> str:
        """Short identifier, e.g. 'jira', 'confluence'. Used in CWI source_type."""
        raise NotImplementedError

    @abstractmethod
    def test_connection(self) -> bool:
        """
        Verify credentials/config work before running a full fetch.
        Should be cheap — one lightweight API call, not a full sync.
        """
        raise NotImplementedError

    @abstractmethod
    def fetch_items(self, since: Optional[datetime] = None) -> Iterator[Dict[str, Any]]:
        """
        Yield raw items from the source. If `since` is provided, only
        return items updated after that timestamp (incremental sync).
        If `since` is None, fetch everything (full sync).
        """
        raise NotImplementedError