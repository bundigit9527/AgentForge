"""Bounded request context without changing persisted conversation history."""

import json
from copy import deepcopy

from agentforge_core.execution import ContextMessage, ModelConfig
from agentforge_core.models import RunErrorCode

from agentforge_runtime.context_tools import VERBATIM, shorten, tool_results
from agentforge_runtime.errors import RuntimeFailure

OMITTED_HISTORY_NOTICE = (
    "Some earlier conversation turns were omitted to fit the context budget. "
    "Do not claim to remember omitted details; ask the user when they are needed."
)


def context_size(messages: list[dict], tools: list[dict]) -> int:
    """Unicode characters in canonical messages/tools JSON, not model tokens or bytes."""
    return len(
        json.dumps(
            {"messages": messages, "tools": tools},
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


def context_bytes(messages: list[dict], tools: list[dict]) -> int:
    """UTF-8 request-context size, shared by selection and audit statistics."""
    return len(json.dumps({"messages": messages, "tools": tools}, ensure_ascii=False).encode())


class ContextWindow:
    def __init__(
        self,
        prefix: list[dict],
        history: tuple[ContextMessage, ...],
        prompt: str,
        tools: list[dict],
        *,
        config: ModelConfig,
    ):
        # Storage captures up to 20 complete successful turns. A malformed snapshot
        # must never promote an orphan assistant response to conversation history.
        if (
            len(history) > 40
            or len(history) % 2
            or any(
                item.role != ("user" if index % 2 == 0 else "assistant")
                for index, item in enumerate(history)
            )
        ):
            raise RuntimeFailure(RunErrorCode.EXECUTION_FAILED)
        self.prefix = list(prefix)
        self.history = [item.model_dump() for item in history]
        self.current = [{"role": "user", "content": prompt}]
        self.tools = tools
        self.config = config
        self.dropped_turns = 0

    def _messages(self, history, dropped):
        notice = [{"role": "system", "content": OMITTED_HISTORY_NOTICE}] if dropped else []
        return [*self.prefix, *notice, *history, *self.current]

    def within_budget(self, messages):
        if context_size(messages, self.tools) > self.config.context_max_chars:
            return False
        if self.config.context_max_bytes is not None:
            if context_bytes(messages, self.tools) > self.config.context_max_bytes:
                return False
        return True

    def fit(self) -> tuple[list[dict], dict | None]:
        if self.config.compact_tool_results and self.config.context_policy == "trim_history":
            return self._fit_projection()
        messages = self._messages(self.history, self.dropped_turns)
        if self.config.context_policy == "reject":
            # Keep the historical snapshot's exact accounting/error behavior.
            if len(json.dumps(messages, ensure_ascii=False)) > self.config.context_max_chars:
                raise RuntimeFailure(RunErrorCode.MODEL_LIMIT_EXCEEDED)
            return messages, None
        before = context_size(messages, self.tools)
        if self.within_budget(messages):
            return messages, None
        byte_budget = self.config.context_max_bytes
        before_bytes = context_bytes(messages, self.tools) if byte_budget is not None else None
        remaining = list(self.history)
        removed = 0
        while remaining:
            remaining = remaining[2:]
            removed += 1
            messages = self._messages(remaining, self.dropped_turns + removed)
            after = context_size(messages, self.tools)
            if self.within_budget(messages):
                self.history = remaining
                self.dropped_turns += removed
                return messages, {
                    "budget_chars": self.config.context_max_chars,
                    "before_chars": before,
                    "after_chars": after,
                    "dropped_turns": removed,
                    "retained_turns": len(remaining) // 2,
                    **(
                        {
                            "budget_bytes": byte_budget,
                            "before_bytes": before_bytes,
                            "after_bytes": context_bytes(messages, self.tools),
                        }
                        if byte_budget is not None
                        else {}
                    ),
                }
        # Never truncate instructions, the current prompt, tool schemas or any
        # current-turn tool arguments/results. Stop before the next model request.
        raise RuntimeFailure(RunErrorCode.MODEL_CONTEXT_LIMIT_EXCEEDED)

    def _fit_projection(self):
        # Approval checkpoints and the tool journal keep the original current list.
        # Only this request's projection changes; calls/arguments are never edited.
        current = deepcopy(self.current)
        try:
            results = tool_results(current)
        except (KeyError, TypeError, ValueError):
            raise RuntimeFailure(RunErrorCode.EXECUTION_FAILED) from None
        remaining = list(self.history)
        removed = 0
        shortened = set()

        def messages():
            notice = (
                [{"role": "system", "content": OMITTED_HISTORY_NOTICE}]
                if self.dropped_turns + removed
                else []
            )
            return [*self.prefix, *notice, *remaining, *current]

        before = deepcopy(messages())
        latest = max((group for _, group, _ in results), default=0)

        def shrink(index, limit):
            original = current[index]["content"]
            value = shorten(original, limit)
            if value != original:
                current[index]["content"] = value
                shortened.add(index)
                return True
            return False

        # Like Rakazo, older evidence gets a bounded representation. Keep URLs,
        # IDs and operation outcomes so the agent does not restart completed work.
        for index, group, name in results:
            if group < latest and name not in VERBATIM:
                shrink(index, 1024)
        while not self.within_budget(messages()) and remaining:
            remaining = remaining[2:]
            removed += 1
        while not self.within_budget(messages()):
            changed = False
            for index, _, name in results:
                if name in VERBATIM:
                    continue
                size = len(current[index]["content"].encode())
                changed = shrink(index, max(256, size // 2)) or changed
            if not changed:
                # Required controls/instructions and large arguments still fail.
                raise RuntimeFailure(RunErrorCode.MODEL_CONTEXT_LIMIT_EXCEEDED)
        selected = messages()
        self.history = remaining
        self.dropped_turns += removed
        if not removed and not shortened:
            return selected, None
        stats = {
            "budget_chars": self.config.context_max_chars,
            "before_chars": context_size(before, self.tools),
            "after_chars": context_size(selected, self.tools),
            "dropped_turns": removed,
            "retained_turns": len(remaining) // 2,
            "truncated_tool_results": len(shortened),
        }
        if self.config.context_max_bytes is not None:
            stats.update(
                budget_bytes=self.config.context_max_bytes,
                before_bytes=context_bytes(before, self.tools),
                after_bytes=context_bytes(selected, self.tools),
            )
        return selected, stats
