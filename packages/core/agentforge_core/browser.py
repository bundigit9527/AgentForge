"""Credential-free browser contracts. Browser tools only navigate guest HTML documents."""

from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator


def public_https_url(value):
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
        or "\\" in value
        or any(ord(c) < 33 or ord(c) == 127 for c in value)
        or not parsed.hostname.isascii()
    ):
        raise ValueError("Use a public HTTPS URL without credentials")
    return value


def exact_origin(value):
    if value == "*":
        return value
    public_https_url(value)
    p = urlsplit(value)
    if p.path or p.query or p.fragment or value != "https://" + p.hostname:
        raise ValueError("Use an exact HTTPS origin, without a path or port")
    return value


Origin = Annotated[str, Field(max_length=255), AfterValidator(exact_origin)]
WebUrl = Annotated[str, Field(min_length=1, max_length=2048), AfterValidator(public_https_url)]
BROWSER_TOOLS = ("browser_open@1", "browser_observe@1", "browser_follow@1")
READING_TOOLS = ("browser_read@1", "browser_scroll@1", "browser_find@1")
READING_ACTIONS = {"read", "scroll", "find"}


def browser_tools(reading=False):
    return BROWSER_TOOLS + (READING_TOOLS if reading else ())


def permitted_url(url, origins):
    public_https_url(url)
    return "*" in origins or "https://" + urlsplit(url).hostname in origins


class BrowserInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["open", "observe", "follow", "read", "scroll", "find"]
    url: WebUrl | None = None
    snapshot: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    link: str | None = Field(default=None, pattern=r"^[0-9]{1,3}$")

    offset: int | None = Field(default=None, ge=0, le=200000)
    links_offset: int | None = Field(default=None, ge=0, le=480)
    direction: Literal["up", "down", "top", "bottom"] | None = None
    text: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def fields_for_action(self):
        allowed = {
            "open": {"url"},
            "observe": set(),
            "follow": {"snapshot", "link"},
            "read": {"snapshot", "offset", "links_offset"},
            "scroll": {"snapshot", "direction"},
            "find": {"snapshot", "text"},
        }[self.action]
        present = {k for k, v in self.model_dump().items() if k != "action" and v is not None}
        if not present <= allowed:
            raise ValueError("Unexpected fields for browser action")
        if self.action in READING_ACTIONS and not self.snapshot:
            raise ValueError("Reading actions require a snapshot")
        if self.action == "scroll" and not self.direction:
            raise ValueError("Scroll requires a direction")
        if self.action == "find" and (not self.text or not self.text.strip()):
            raise ValueError("Find requires visible text")
        if self.action == "open" and (not self.url or self.snapshot or self.link):
            raise ValueError("Open requires only a URL")
        if self.action == "observe" and (self.url or self.snapshot or self.link):
            raise ValueError("Observe takes no target")
        if self.action == "follow" and (self.url or not self.snapshot or not self.link):
            raise ValueError("Follow requires a snapshot and link")
        return self


class BrowserLink(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(pattern=r"^[0-9]{1,3}$")
    text: str = Field(max_length=80)
    url: WebUrl


class BrowserPage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    snapshot: str = Field(pattern=r"^[a-f0-9]{32}$")
    url: WebUrl
    title: str = Field(max_length=200)
    text: str = Field(max_length=4000)
    links: list[BrowserLink] = Field(max_length=12)


class BrowserReading(BrowserPage):
    offset: int = Field(ge=0, le=200000)
    next_offset: int | None = Field(ge=0, le=200000)
    total_chars: int = Field(ge=0, le=200000)
    links_offset: int = Field(ge=0, le=480)
    next_link_offset: int | None = Field(ge=0, le=480)
    total_links: int = Field(ge=0, le=480)
    truncated: bool
    match_offset: int | None = Field(ge=0, le=200000)
    scroll_y: int = Field(ge=0)
    viewport_height: int = Field(gt=0)
    document_height: int = Field(ge=0)


def browser_result(action, value):
    model = BrowserReading if action in READING_ACTIONS else BrowserPage
    return model.model_validate(value).model_dump()


class BrowserFailure(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)
