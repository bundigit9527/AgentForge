from agentforge_core.models import RunErrorCode


class RuntimeFailure(Exception):
    """Only a stable public code, never a tool or upstream exception message."""

    def __init__(self, code: RunErrorCode):
        if not isinstance(code, RunErrorCode):
            raise TypeError("Expected a known run error code")
        self.code = code
        super().__init__(code.value)


class ExecutionStopped(Exception):
    """The run was cancelled or this execution no longer owns its lease."""
