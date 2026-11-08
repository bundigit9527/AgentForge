"""Per-run routing around a single Python Harness; never replay a tool loop."""

import json
from contextlib import contextmanager

from agentforge_core.model_management import ModelManagementError
from agentforge_core.models import RunErrorCode
from agentforge_runtime.chat_harness import ChatHarness
from agentforge_runtime.chat_model import ChatCompletionsModel
from agentforge_runtime.context import context_size
from agentforge_runtime.errors import ExecutionStopped, RuntimeFailure

from agentforge_connectors.model_endpoint import ModelEndpointTransport

RETRYABLE = frozenset(
    {
        RunErrorCode.MODEL_AUTH_FAILED,
        RunErrorCode.MODEL_RATE_LIMITED,
        RunErrorCode.MODEL_UNAVAILABLE,
        RunErrorCode.MODEL_TIMEOUT,
    }
)


class RoutedModel:
    def __init__(self, manager, scope, snapshot, checkpoint):
        self.manager, self.scope = manager, scope
        self.routes = (snapshot.model, *snapshot.model_backups)
        self.index = 0
        self.checkpoint = checkpoint
        self.name = snapshot.model.model

    def checked_settings(self, route):
        if not self.checkpoint():
            raise ExecutionStopped()
        try:
            return self.manager.resolve(self.scope, route)
        except (ModelManagementError, ValueError):
            raise RuntimeFailure(RunErrorCode.MODEL_CONFIGURATION_MISMATCH) from None

    def complete(self, messages, tools, *, on_text=None, checkpoint=lambda: True, tool_choice=None):
        emitted = False

        def check():
            return self.checkpoint() and checkpoint()

        def text(value):
            nonlocal emitted
            if value:
                emitted = True
            if on_text is not None:
                return on_text(value)

        while True:
            route = self.routes[self.index]
            settings = self.checked_settings(route)
            if not check():
                raise ExecutionStopped()
            # The Harness already trimmed the primary context. A smaller backup
            # must reject oversized input rather than silently drop tool state.
            if context_size(messages, tools) > route.context_max_chars or (
                route.context_max_bytes is not None
                and len(
                    json.dumps({"messages": messages, "tools": tools}, ensure_ascii=False).encode()
                )
                > route.context_max_bytes
            ):
                raise RuntimeFailure(RunErrorCode.MODEL_CONTEXT_LIMIT_EXCEEDED)
            self.name = settings.model
            try:
                model = ChatCompletionsModel(
                    settings,
                    transport=ModelEndpointTransport(settings.base_url)
                    if route.connection_id
                    else None,
                )
                try:
                    result = model.complete(
                        messages, tools, on_text=text, checkpoint=check, tool_choice=tool_choice
                    )
                finally:
                    model.close()
            except ModelManagementError:
                raise RuntimeFailure(RunErrorCode.MODEL_CONFIGURATION_MISMATCH) from None
            except RuntimeFailure as exc:
                self.checked_settings(route)  # Revocation must never become a fallback.
                if not check():
                    raise ExecutionStopped() from None
                if emitted or exc.code not in RETRYABLE or self.index + 1 == len(self.routes):
                    raise
                self.index += 1
                continue
            self.checked_settings(route)
            if not check():
                raise ExecutionStopped()
            return result


@contextmanager
def managed_harness(manager, scope, snapshot, checkpoint):
    yield ChatHarness(
        RoutedModel(manager, scope, snapshot, checkpoint),
        max_rounds=snapshot.model.max_rounds,
        model_config=snapshot.model,
    )
