"""Fixed computation in a disposable, resource-limited Docker container."""

import json
import re
import subprocess
from threading import BoundedSemaphore
from typing import Annotated
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

Integer = Annotated[int, Field(strict=True, ge=-(10**12), le=10**12)]
Values = Annotated[list[Integer], Field(min_length=1, max_length=512)]
values_adapter = TypeAdapter(Values)

PROGRAM = """
import json, signal, sys
signal.alarm(8)
values = json.load(sys.stdin)
total = sum(values)
print(json.dumps(dict(count=len(values), minimum=min(values), maximum=max(values),
                      total=total, mean=total / len(values))))
"""


class Summary(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    count: int = Field(ge=1, le=512)
    minimum: int
    maximum: int
    total: int
    mean: float


class DockerComputer:
    def __init__(self, image_id: str):
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id):
            raise ValueError("Set AGENTFORGE_COMPUTER_IMAGE to a locally installed Docker image ID")
        self.image_id = image_id
        self.slots = BoundedSemaphore(2)

    def summarize(self, values: Values) -> Summary:
        data = values_adapter.validate_python(values)
        if not self.slots.acquire(blocking=False):
            raise RuntimeError("Computer is busy")
        name = f"agentforge-computer-{uuid4().hex}"
        try:
            completed = subprocess.run(
                [
                    "docker",
                    "run",
                    "--pull=never",
                    "--no-healthcheck",
                    "--rm",
                    "--interactive",
                    "--name",
                    name,
                    "--network=none",
                    "--read-only",
                    "--user=65534:65534",
                    "--cap-drop=ALL",
                    "--security-opt=no-new-privileges",
                    "--cpus=0.5",
                    "--memory=64m",
                    "--memory-swap=64m",
                    "--pids-limit=16",
                    "--log-driver=none",
                    "--entrypoint=python3",
                    self.image_id,
                    "-I",
                    "-S",
                    "-c",
                    PROGRAM,
                ],
                input=json.dumps(data).encode(),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=12,
                check=True,
            )
            return Summary.model_validate_json(completed.stdout)
        except (subprocess.SubprocessError, OSError, ValueError):
            # The client may have timed out after Docker created the container.
            try:
                subprocess.run(
                    ["docker", "rm", "--force", name],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=4,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                pass  # The fixed program also has an in-container deadline and --rm.
            raise RuntimeError("Computer execution failed") from None
        finally:
            self.slots.release()
