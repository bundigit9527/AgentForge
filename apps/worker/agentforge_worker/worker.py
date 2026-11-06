import json
import logging
import math
from contextlib import nullcontext
from threading import Event

from agentforge_connectors.browser import browser_request
from agentforge_connectors.mcp import McpService
from agentforge_core.browser import browser_tools
from agentforge_core.models import RunErrorCode
from agentforge_runtime.attachment_tools import (
    READ_ATTACHMENT,
    WRITE_ATTACHMENT,
    with_attachment_tools,
)
from agentforge_runtime.browser_tools import with_browser_tools
from agentforge_runtime.chat_harness import configured_harness
from agentforge_runtime.chat_model import RuntimeFailure
from agentforge_runtime.config import ModelSettings
from agentforge_runtime.harness import ExecutionStopped, Harness
from agentforge_runtime.mcp_tools import with_mcp_tools
from agentforge_runtime.skill_tools import SKILL_READ, with_skill_reader
from agentforge_runtime.tools import BUILTIN_TOOLS, DEFAULT_TOOLS, ToolExecutor, ToolRegistry
from agentforge_storage.repository import RunRepository
from sqlalchemy.exc import SQLAlchemyError

from agentforge_worker.approvals import ApprovalExecution
from agentforge_worker.lease import LeaseHeartbeat
from agentforge_worker.tools import RunToolJournal

logger = logging.getLogger(__name__)


class Worker:
    def __init__(
        self,
        repository: RunRepository,
        harness: Harness | None = None,
        *,
        lease_seconds: int = 60,
        heartbeat_interval: float | None = None,
        harness_factory=None,
        tool_registry: ToolRegistry = BUILTIN_TOOLS,
        allowed_tools: tuple[str, ...] = DEFAULT_TOOLS,
        mcp_service: McpService | None = None,
    ):
        interval = lease_seconds / 3 if heartbeat_interval is None else heartbeat_interval
        if lease_seconds < 1 or not math.isfinite(interval) or not 0 < interval < lease_seconds:
            raise ValueError("Heartbeat interval must be positive and shorter than the lease")
        tool_registry.validate_allowlist(allowed_tools)
        self.tool_registry = tool_registry
        self.mcp_service = mcp_service or McpService()
        self.allowed_tools = allowed_tools
        self.repository = repository
        self.harness = harness
        self.harness_factory = harness_factory or (
            lambda snapshot: configured_harness(ModelSettings().for_snapshot(snapshot.model))
        )
        self.lease_seconds = lease_seconds
        self.heartbeat_interval = interval

    def run_once(self) -> bool:
        claim = self.repository.claim_next(lease_seconds=self.lease_seconds)
        if claim is None:
            return False
        heartbeat = LeaseHeartbeat(
            self.repository, claim, self.lease_seconds, self.heartbeat_interval
        )
        try:
            with heartbeat:
                snapshot = claim.run.execution_config
                if snapshot is None:
                    raise RuntimeFailure(RunErrorCode.EXECUTION_CONFIG_MISSING)

                def owns_execution():
                    return not heartbeat.lost.is_set() and self.repository.owns_claim(claim)

                registry = self.tool_registry
                runtime_tools = snapshot.tools
                runtime_allowed = self.allowed_tools
                mcp_refs = tuple(tool.reference for tool in snapshot.mcp_tools)
                if snapshot.mcp_tools:
                    registry = with_mcp_tools(
                        registry,
                        self.mcp_service,
                        claim.run.scope,
                        snapshot.mcp_tools,
                        owns_execution,
                        bot_id=claim.run.agent_id,
                    )
                    runtime_tools = (*runtime_tools, *mcp_refs)
                    runtime_allowed = (*runtime_allowed, *mcp_refs)
                if snapshot.skills:
                    registry = with_skill_reader(registry, snapshot.skills)
                    runtime_tools = (*runtime_tools, SKILL_READ)
                    runtime_allowed = (*runtime_allowed, SKILL_READ)
                prompt = claim.run.prompt
                allowed_reads = mcp_refs
                if snapshot.browser_origins:
                    browser_refs = browser_tools(snapshot.browser_reading)
                    registry = with_browser_tools(
                        registry,
                        lambda arguments: browser_request(claim, arguments),
                        snapshot.browser_origins,
                        reading=snapshot.browser_reading,
                    )
                    runtime_tools = (*runtime_tools, *browser_refs)
                    runtime_allowed = (*runtime_allowed, *browser_refs)
                    allowed_reads = (*allowed_reads, *browser_refs)
                if claim.run.input_files and snapshot.model.provider == "chat_completions":
                    registry = with_attachment_tools(
                        registry,
                        claim.run.input_files,
                        lambda ref, offset: self.repository.read_attachment(claim, ref, offset),
                    )
                    runtime_tools = (*runtime_tools, READ_ATTACHMENT, WRITE_ATTACHMENT)
                    runtime_allowed = (*runtime_allowed, READ_ATTACHMENT, WRITE_ATTACHMENT)
                    allowed_reads = (*allowed_reads, READ_ATTACHMENT)
                    prompt = json.dumps(
                        {
                            "request": prompt
                            or "Describe the attached files based on what you can read.",
                            "attachments": claim.run.input_files,
                        },
                        ensure_ascii=False,
                    )
                executor = ToolExecutor(
                    runtime_tools,
                    registry=registry,
                    allowed=runtime_allowed,
                    journal=RunToolJournal(self.repository, claim),
                    checkpoint=owns_execution,
                    read_allowed=allowed_reads,
                )
                if snapshot.approval_tools:
                    executor.approvals = ApprovalExecution(self.repository, claim)
                try:
                    runtime = (
                        nullcontext(self.harness)
                        if self.harness is not None
                        else self.harness_factory(snapshot)
                    )
                except ValueError:
                    raise RuntimeFailure(RunErrorCode.MODEL_CONFIGURATION_MISMATCH) from None
                with runtime as harness:
                    if harness.model_config != snapshot.model:
                        raise RuntimeFailure(RunErrorCode.MODEL_CONFIGURATION_MISMATCH)
                    result = harness.execute(
                        prompt,
                        before_tool=lambda _name: owns_execution(),
                        checkpoint=lambda: (
                            not heartbeat.lost.is_set() and self.repository.owns_claim(claim)
                        ),
                        on_model=lambda **data: (
                            not heartbeat.lost.is_set()
                            and self.repository.record_model(claim, **data)
                        ),
                        on_context=lambda **data: (
                            not heartbeat.lost.is_set()
                            and self.repository.record_context(claim, **data)
                        ),
                        on_text=lambda **data: (
                            not heartbeat.lost.is_set()
                            and self.repository.record_text(claim, **data)
                        ),
                        execution_config=snapshot,
                        context=claim.run.context,
                        tool_executor=executor,
                    )
        except ExecutionStopped:
            logger.info("Execution stopped for run %s", claim.run.run_id)
        except Exception as exc:
            # Do not expose provider exceptions, credentials or tool input in the public API.
            logger.error("Execution failed for run %s", claim.run.run_id)
            if not heartbeat.lost.is_set():
                code = (
                    exc.code if isinstance(exc, RuntimeFailure) else RunErrorCode.EXECUTION_FAILED
                )
                self.repository.finish(claim, failed=True, error_code=code)
        else:
            if not heartbeat.lost.is_set():
                self.repository.finish(claim, result=result)
        finally:
            # Only a still-owned lease with revoked access can take this transition.
            self.repository.finish(claim, failed=True, error_code=RunErrorCode.ACCESS_REVOKED)
        return True

    def run_forever(self, stop: Event, *, poll_interval: float = 1) -> None:
        """Stop claiming on shutdown, finish the current task, and wake promptly when idle."""
        if not math.isfinite(poll_interval) or poll_interval <= 0:
            raise ValueError("Poll interval must be positive and finite")
        while not stop.is_set():
            try:
                handled = self.run_once()
            except SQLAlchemyError:
                logger.error("Task storage is unavailable; retrying after the poll interval")
                handled = False
            if not handled:
                stop.wait(poll_interval)
