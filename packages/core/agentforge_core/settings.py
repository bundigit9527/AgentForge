import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from agentforge_core.models import Scope


@dataclass(frozen=True)
class Settings:
    database_url: str = field(repr=False)
    dev_token: str = field(default="", repr=False)
    auth_mode: str = "development"
    web_origin: str = ""
    dev_scope: Scope = Scope("local", "development", "developer")

    def __post_init__(self) -> None:
        if self.auth_mode not in {"development", "database"}:
            raise ValueError("AGENTFORGE_AUTH_MODE must be development or database")
        if self.web_origin:
            url = urlsplit(self.web_origin)
            if (
                url.scheme not in {"http", "https"}
                or not url.hostname
                or url.username
                or url.password
                or url.path
                or url.query
                or url.fragment
                or any(c.isspace() for c in self.web_origin)
            ):
                raise ValueError(
                    "AGENTFORGE_WEB_ORIGIN must be an exact HTTP(S) origin without a path"
                )
            _ = url.port
        if not self.database_url:
            raise ValueError("AGENTFORGE_DATABASE_URL is required")
        if self.dev_token and len(self.dev_token) < 32:
            raise ValueError("AGENTFORGE_DEV_TOKEN must have at least 32 characters")

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_url=os.environ.get("AGENTFORGE_DATABASE_URL", ""),
            web_origin=os.environ.get("AGENTFORGE_WEB_ORIGIN", ""),
            auth_mode=os.environ.get("AGENTFORGE_AUTH_MODE", "development"),
            dev_token=os.environ.get("AGENTFORGE_DEV_TOKEN", ""),
            dev_scope=Scope(
                os.environ.get("AGENTFORGE_DEV_TENANT", "local"),
                os.environ.get("AGENTFORGE_DEV_SPACE", "development"),
                os.environ.get("AGENTFORGE_DEV_USER", "developer"),
            ),
        )
