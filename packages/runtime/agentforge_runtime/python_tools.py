"""Generic Python execution with small model results and out-of-band file publication."""

from agentforge_core.attachments import MAX_FILE
from agentforge_core.models import RunErrorCode
from agentforge_core.python_execution import GeneratedFile, PythonArguments
from pydantic import BaseModel, ConfigDict, Field

from agentforge_runtime.errors import RuntimeFailure
from agentforge_runtime.tools import GeneratedToolResult, ToolDefinition


class FileReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    mime_type: str
    size: int


class PythonResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    exit_code: int
    stdout: str = Field(max_length=3000)
    stderr: str = Field(max_length=3000)
    files: list[FileReceipt] = Field(default_factory=list, max_length=4)


def with_python_tool(registry, service):
    def execute(arguments):
        response = service.execute(arguments.model_dump())
        try:
            files = tuple(GeneratedFile.decode(f) for f in response.files)
            names = [f.name for f in files]
            if (
                len(set(names)) != len(names)
                or (response.exit_code == 0 and set(names) != set(arguments.exports))
                or (response.exit_code != 0 and files)
                or sum(len(f.data) for f in files) > MAX_FILE
            ):
                raise ValueError("Unexpected generated files")
        except Exception:
            raise RuntimeFailure(RunErrorCode.TOOL_INVALID_RESULT) from None
        return GeneratedToolResult(
            dict(
                exit_code=response.exit_code,
                stdout=response.stdout,
                stderr=response.stderr,
                files=[dict(name=f.name, mime_type=f.mime_type, size=len(f.data)) for f in files],
            ),
            files,
        )

    return registry.extended(
        [
            ToolDefinition(
                "python_execute",
                1,
                "Run Python code in a fresh isolated offline container. Packages: python-pptx, "
                "openpyxl, python-docx, Pillow, standard library. Current directory: /workspace. "
                "Write files there and specify their simple filenames in exports to publish chat "
                "attachments (max 4 files, combined 10 MiB). Every call starts empty; "
                "no persistent desktop files, network, credentials or dependency installation. "
                "45 second deadline, 384 MiB memory. A nonzero exit_code means failure: inspect "
                "stderr, correct the code and retry within the task budget. Only claim file "
                "delivery when a successful result lists it. Do not print file bytes or base64.",
                PythonArguments,
                PythonResult,
                execute,
                effect="artifact",
            )
        ]
    )
