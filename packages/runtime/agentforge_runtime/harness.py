import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from agentforge_core.execution import ContextMessage, ExecutionSnapshot, ModelConfig

from agentforge_runtime.errors import ExecutionStopped as ExecutionStopped
from agentforge_runtime.tools import ToolExecutor


@dataclass(frozen=True)
class ToolCall:
    name: str
    text: str


class Model(Protocol):
    def plan(self, prompt: str) -> ToolCall: ...


class MockModel:
    """Deterministic local fixture, not a real language model."""

    def plan(self, prompt: str) -> ToolCall:
        return ToolCall(name="example_report", text=prompt)


class Harness:
    def __init__(self, model: Model | None = None):
        self.model = model or MockModel()
        self.model_config = ModelConfig()

    def execute(
        self,
        prompt: str,
        before_tool: Callable[[str], bool],
        *,
        checkpoint: Callable[[], bool] = lambda: True,
        on_model: Callable[..., bool] | None = None,
        execution_config: ExecutionSnapshot | None = None,
        context: tuple[ContextMessage, ...] = (),
        tool_executor: ToolExecutor | None = None,
    ) -> str:
        executor = tool_executor or ToolExecutor(
            execution_config.tools if execution_config else ("example_report@1",)
        )
        if not checkpoint():
            raise ExecutionStopped()
        if isinstance(self.model, MockModel):
            if (
                execution_config
                and execution_config.skills
                and "skill_read" in executor.definitions
            ):
                prepared_skill = executor.prepare(
                    "mock-skill-1",
                    "skill_read",
                    json.dumps({"reference": execution_config.skills[0].reference}),
                )
                if not before_tool("skill_read"):
                    raise ExecutionStopped()
                executor.execute(prepared_skill)
            business_tools = [name for name in executor.definitions if name != "skill_read"]
            if not business_tools:
                return "离线模拟回复（未调用真实模型）：" + prompt
            call = ToolCall(business_tools[0], prompt)
        else:
            call = self.model.plan(prompt)
        prepared = executor.prepare("mock-call-1", call.name, json.dumps({"text": call.text}))
        if not checkpoint() or not before_tool(call.name):
            raise ExecutionStopped()
        result = executor.execute(prepared)
        return (
            "# AgentForge 示例报告\n\n"
            "这是离线模拟结果，未调用真实大模型，也未执行输入中的命令。\n\n" + result
        )
