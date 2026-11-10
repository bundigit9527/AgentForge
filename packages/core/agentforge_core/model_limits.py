"""Reviewed provider metadata, independent of the JavaScript runtime.

DeepSeek official model table and Rakazo's pinned Pi catalog, checked 2026-11-10.
Unknown custom endpoints must declare their own limits; IDs alone prove nothing.
"""

CATALOG_WINDOWS = {
    ("deepseek", "deepseek-v4-pro"): 1_000_000,
    ("deepseek", "deepseek-flash"): 1_000_000,
    ("deepseek", "deepseek-v4-flash"): 1_000_000,
}
DEFAULT_WINDOW = 32768


def connection_window(provider, configuration, model):
    if configuration.get("contextWindowSource") == "catalog":
        return CATALOG_WINDOWS.get((provider, model), DEFAULT_WINDOW)
    # Old records cannot distinguish an explicit override from the old default.
    # Preserve them until the owner saves the connection again.
    return configuration["contextWindow"]


def connect_window(body, previous):
    if body.contextWindow is not None:
        return body.contextWindow, "explicit"
    source = previous.get("contextWindowSource")
    if previous and (source == "explicit" or source is None):
        return previous["contextWindow"], "explicit"
    window = CATALOG_WINDOWS.get((body.provider, body.modelId))
    if window is not None:
        return window, "catalog"
    return DEFAULT_WINDOW, "default"
