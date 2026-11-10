"""Deterministic evidence shortening; never edit tool calls or persisted results."""

import json

NOTICE = "[Evidence shortened for context; omitted details must be read again.]"
VERBATIM = frozenset({"skill_read", "read_history", "search_history"})
# Only prose fields can shrink. IDs, URLs, paths, cursors, dates, statuses, numbers,
# code and all other control fields remain exact, even when that means failure.
PROSE = frozenset({"text", "content", "snippet", "description", "stdout", "stderr"})


def shorten(source: str, limit: int) -> str:
    try:
        payload = json.loads(source)
    except (ValueError, TypeError):
        return source
    if not isinstance(payload, dict):
        return source
    leaves = []

    def visit(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in PROSE and isinstance(child, str):
                    leaves.append((value, key))
                elif isinstance(child, (dict, list)):
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    changed = False
    while True:
        encoded = json.dumps(payload, ensure_ascii=False)
        if len(encoded.encode()) <= limit:
            break
        candidates = [(parent, key) for parent, key in leaves if len(parent[key]) > 160]
        if not candidates:
            break
        parent, key = max(candidates, key=lambda pair: len(pair[0][pair[1]].encode()))
        value = parent[key]
        keep = max(8, len(value) // 4)
        parent[key] = value[:keep] + "\n" + NOTICE + "\n" + value[-keep:]
        payload["contextTruncated"] = True
        changed = True
    return encoded if changed and len(encoded.encode()) < len(source.encode()) else source


def tool_results(current):
    """Validate complete groups before exposing a request projection to the model."""
    found = []
    seen = set()
    pending = {}
    group = 0
    for index, message in enumerate(current):
        if message["role"] == "tool":
            call_id = message.get("tool_call_id")
            if call_id not in pending:
                raise ValueError("Orphaned tool result")
            name = pending.pop(call_id)
            found.append((index, group, name))
        else:
            if pending:
                raise ValueError("Incomplete tool group")
            calls = message.get("tool_calls") or []
            if calls:
                group += 1
            for call in calls:
                if call["id"] in seen:
                    raise ValueError("Duplicate tool call")
                seen.add(call["id"])
                pending[call["id"]] = call["function"]["name"]
    if pending:
        raise ValueError("Incomplete tool group")
    return found
