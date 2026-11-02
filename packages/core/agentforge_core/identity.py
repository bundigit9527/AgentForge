"""Identity providers authenticate; resource policy is resolved from current membership."""

from typing import Literal, Protocol

from agentforge_core.models import Scope

Role = Literal["admin", "member", "viewer"]
ResourceKind = Literal["agent", "skill", "thread", "run"]
AuthMode = Literal["development", "database"]


class AuthenticationFailed(Exception):
    pass


class AuthenticationUnavailable(Exception):
    pass


class PermissionDenied(Exception):
    pass


class IdentityProvider(Protocol):
    def authenticate(self, token: str | None) -> Scope:
        """Return a verified scope; external providers must verify their own credentials."""
        ...
