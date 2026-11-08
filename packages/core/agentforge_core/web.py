"""Credential-free web tool contracts, pinned into each submitted run."""

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agentforge_core.browser import WebUrl

WEB_TOOLS = ("web_search@1", "web_fetch@1")


class SearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    query: str = Field(min_length=1, max_length=1000)
    maxResults: int = Field(default=5, ge=1, le=10)

    @field_validator("query")
    @classmethod
    def nonempty(cls, value):
        if not value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError("Provide a nonempty search query")
        return value.strip()


class FetchInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    url: WebUrl
    maxChars: int = Field(default=8000, ge=1000, le=12000)


class SearchHit(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    title: str = Field(min_length=1, max_length=240)
    url: WebUrl
    snippet: str = Field(max_length=600)


class SearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    query: str = Field(max_length=1000)
    results: list[SearchHit] = Field(max_length=10)
    retrievedAt: str = Field(max_length=40)
    truncated: bool = False


class FetchResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    url: WebUrl
    title: str = Field(max_length=240)
    text: str = Field(max_length=12000)
    truncated: bool
    retrievedAt: str = Field(max_length=40)
