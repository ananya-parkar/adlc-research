"""
Auth is kept separate from the connector itself. Right now only
ApiTokenAuth exists (Basic Auth with email + token). When you're
ready for multi-client OAuth 2.0 (3LO), add an OAuth2Auth class here
implementing the same `.credentials()` interface — JiraConnector
won't need to change at all.
"""

from abc import ABC, abstractmethod
from typing import Tuple


class AuthProvider(ABC):
    @abstractmethod
    def credentials(self) -> Tuple[str, str]:
        """Return whatever `requests` needs for the `auth=` param."""
        raise NotImplementedError


class ApiTokenAuth(AuthProvider):
    """Basic auth using an Atlassian account email + API token."""

    def __init__(self, email: str, api_token: str):
        if not email or not api_token:
            raise ValueError("email and api_token are both required")
        self.email = email
        self.api_token = api_token

    def credentials(self) -> Tuple[str, str]:
        return (self.email, self.api_token)