"""Generic Python execution with small model results and out-of-band file publication."""

from dataclasses import replace
from uuid import uuid4

from agentforge_core.attachments import MAX_FILE
from agentforge_core.models import RunErrorCode
from agentforge_core.python_execution import GeneratedFile, PythonArguments, PythonArgumentsV2
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


class VersionedReceipt(FileReceipt):
    artifact_id: str
    parent_id: str | None = None


class PythonResultV2(PythonResult):
    files: list[VersionedReceipt] = Field(default_factory=list, max_length=4)


def with_python_tool(registry, service, *, load_inputs=None):
    def execute(arguments):
        payload = arguments.model_dump()
        if load_inputs is not None:
            payload["inputs"] = load_inputs(arguments.inputs)
        response = service.execute(payload)
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
        if load_inputs is not None:
            parents = {f.name: f.artifact_id for f in arguments.inputs}
            files = tuple(
                replace(f, artifact_id=str(uuid4()), parent_id=parents.get(f.name)) for f in files
            )
        return GeneratedToolResult(
            dict(
                exit_code=response.exit_code,
                stdout=response.stdout,
                stderr=response.stderr,
                files=[
                    {
                        "name": f.name,
                        "mime_type": f.mime_type,
                        "size": len(f.data),
                        **(
                            {"artifact_id": f.artifact_id, "parent_id": f.parent_id}
                            if load_inputs is not None
                            else {}
                        ),
                    }
                    for f in files
                ],
            ),
            files,
        )

    return registry.extended(
        [
            ToolDefinition(
                "python_execute",
                2 if load_inputs is not None else 1,
                "Run Python code in a fresh isolated offline container. Packages: python-pptx, "
                "openpyxl, python-docx, Pillow, standard library. Current directory: /workspace. "
                "Write files there and specify their simple filenames in exports to publish chat "
                "attachments (max 4 files, combined 10 MiB). "
                + (
                    "To read or edit existing files, list inputs with "
                    "exact artifact_id and a simple "
                    "local filename alias. Only frozen available files or outputs of this run are "
                    "readable. Inputs are copied into /workspace (max 4, combined 10 MiB). "
                    "Export under the same alias to publish a new "
                    "version based on that exact input; "
                    "a different export name creates an independent file. Originals are immutable. "
                    "Reuse returned artifact_id to continue work within this run. "
                    if load_inputs is not None
                    else "Every call starts empty; "
                )
                + "no persistent desktop files, network, credentials or dependency installation. "
                "45 second deadline, 384 MiB memory. A nonzero exit_code means failure: inspect "
                "stderr, correct the code and retry within the task budget. Only claim file "
                "delivery when a successful result lists it. Do not print file bytes or base64.",
                PythonArgumentsV2 if load_inputs is not None else PythonArguments,
                PythonResultV2 if load_inputs is not None else PythonResult,
                execute,
                effect="artifact",
            )
        ]
    )
