from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from agentforge_core.execution import ContextMessage, ExecutionSnapshot, ModelConfig


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


class ExecutionStopped(Exception):
    """The run was cancelled or this execution no longer owns its lease."""


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
    ) -> str:
        if not checkpoint():
            raise ExecutionStopped()
        call = self.model.plan(prompt)
        # Only this pure, built-in tool is allowed in the first milestone.
        if call.name != "example_report":
            raise ValueError("Unknown or unauthorized tool")
        if not call.text.strip() or len(call.text) > 10000:
            raise ValueError("Invalid tool input")
        if not before_tool(call.name):
            raise ExecutionStopped()
        return (
            "# AgentForge 示例报告\n\n"
            "这是离线模拟结果，未调用真实大模型，也未执行输入中的命令。\n\n"
            f"## 任务输入\n\n{call.text}\n\n"
            "## 执行记录\n\n已完成模拟决策和内置报告工具调用。\n"
        )
