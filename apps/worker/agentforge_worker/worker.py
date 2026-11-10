import json
import logging
import math
from contextlib import nullcontext
from threading import Event

from agentforge_connectors.browser import browser_request
from agentforge_connectors.mcp import McpService
from agentforge_connectors.model_execution import managed_harness
from agentforge_connectors.python_execution import PythonPolicy, PythonService, PythonSettings
from agentforge_connectors.web import WebService
from agentforge_connectors.workspace import workspace_policy, workspace_request
from agentforge_core.browser import browser_tools
from agentforge_core.models import RunErrorCode
from agentforge_core.workspace import WORKSPACE_TOOLS
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
from agentforge_runtime.python_tools import with_python_tool
from agentforge_runtime.skill_tools import SKILL_READ, with_skill_reader
from agentforge_runtime.tools import BUILTIN_TOOLS, DEFAULT_TOOLS, ToolExecutor, ToolRegistry
from agentforge_runtime.web_tools import with_web_tools
from agentforge_runtime.workspace_tools import with_workspace_tools
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
        model_manager=None,
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
        self.model_manager = model_manager
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
                    if heartbeat.lost.is_set() or not self.repository.owns_claim(claim):
                        return False
                    if snapshot.python_image and (
                        not PythonPolicy.from_env().permits(claim.run.scope)
                        or PythonSettings.from_env().image != snapshot.python_image
                    ):
                        raise RuntimeFailure(RunErrorCode.TOOL_NOT_ALLOWED)
                    if snapshot.workspace_enabled and not workspace_policy().permits(
                        claim.run.scope
                    ):
                        raise RuntimeFailure(RunErrorCode.TOOL_NOT_ALLOWED)
                    return True

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
                if snapshot.available_skills:
                    registry = with_skill_reader(registry, snapshot.available_skills)
                    runtime_tools = (*runtime_tools, SKILL_READ)
                    runtime_allowed = (*runtime_allowed, SKILL_READ)
                prompt = claim.run.prompt
                allowed_reads = mcp_refs
                if snapshot.web_origins:
                    web_refs = ("web_fetch@1",) + (
                        ("web_search@1",) if snapshot.web_search_endpoint else ()
                    )
                    registry = with_web_tools(
                        registry,
                        WebService(claim.run.scope, snapshot, owns_execution),
                        search=bool(snapshot.web_search_endpoint),
                    )
                    runtime_tools = (*runtime_tools, *web_refs)
                    runtime_allowed = (*runtime_allowed, *web_refs)
                    allowed_reads = (*allowed_reads, *web_refs)
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
                if snapshot.python_image:
                    registry = with_python_tool(
                        registry,
                        PythonService(claim.run.scope, snapshot.python_image, owns_execution),
                        load_inputs=(lambda inputs: self.repository.python_inputs(claim, inputs))
                        if snapshot.python_continuity
                        else None,
                    )
                    runtime_tools = (*runtime_tools, snapshot.python_tool)
                    runtime_allowed = (*runtime_allowed, snapshot.python_tool)
                if snapshot.workspace_enabled:
                    registry = with_workspace_tools(
                        registry,
                        lambda tool, args: workspace_request(claim, tool, args),
                        snapshot.workspace_mode,
                    )
                    runtime_tools = (*runtime_tools, *WORKSPACE_TOOLS)
                    runtime_allowed = (*runtime_allowed, *WORKSPACE_TOOLS)
                    allowed_reads = (*allowed_reads, "list_files@1", "attach_file@1")
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
                    write_allowed=snapshot.approval_tools,
                )
                if snapshot.approval_tools:
                    executor.approvals = ApprovalExecution(self.repository, claim)
                try:
                    runtime = (
                        nullcontext(self.harness)
                        if self.harness is not None
                        else managed_harness(
                            self.model_manager, claim.run.scope, snapshot, owns_execution
                        )
                        if self.model_manager is not None
                        and (snapshot.model.connection_id or snapshot.model_backups)
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
                        checkpoint=owns_execution,
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
            logger.error("Execution failed for run %s (%s)", claim.run.run_id, type(exc).__name__)
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
