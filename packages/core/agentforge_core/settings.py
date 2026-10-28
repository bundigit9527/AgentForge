import os
from dataclasses import dataclass, field

from agentforge_core.models import Scope


@dataclass(frozen=True)
class Settings:
    database_url: str = field(repr=False)
    dev_token: str = field(default="", repr=False)
    dev_scope: Scope = Scope("local", "development", "developer")

    def __post_init__(self) -> None:
        if not self.database_url:
            raise ValueError("AGENTFORGE_DATABASE_URL is required")
        if self.dev_token and len(self.dev_token) < 32:
            raise ValueError("AGENTFORGE_DEV_TOKEN must have at least 32 characters")

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_url=os.environ.get("AGENTFORGE_DATABASE_URL", ""),
            dev_token=os.environ.get("AGENTFORGE_DEV_TOKEN", ""),
            dev_scope=Scope(
                os.environ.get("AGENTFORGE_DEV_TENANT", "local"),
                os.environ.get("AGENTFORGE_DEV_SPACE", "development"),
                os.environ.get("AGENTFORGE_DEV_USER", "developer"),
            ),
        )
