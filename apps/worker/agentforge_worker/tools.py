from agentforge_core.models import Claim, RunConflict, RunErrorCode
from agentforge_core.tools import ToolInvocation, ToolTicket
from agentforge_runtime.errors import RuntimeFailure
from agentforge_storage.repository import RunRepository


class RunToolJournal:
    def __init__(self, repository: RunRepository, claim: Claim):
        self.repository = repository
        self.claim = claim

    def start(self, invocation: ToolInvocation) -> ToolTicket | None:
        try:
            return self.repository.start_tool(self.claim, invocation)
        except RunConflict:
            raise RuntimeFailure(RunErrorCode.TOOL_CALL_CONFLICT) from None

    def complete(self, ticket: ToolTicket, output: dict, content: str) -> bool:
        return self.repository.complete_tool(self.claim, ticket, output=output, content=content)

    def fail(self, ticket: ToolTicket, code: RunErrorCode) -> bool:
        return self.repository.complete_tool(self.claim, ticket, error_code=code)
