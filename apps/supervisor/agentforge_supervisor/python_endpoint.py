"""Bounded authenticated job endpoint on the existing computation Supervisor."""

import json
import threading

import anyio
from agentforge_core.python_execution import PythonJob
from starlette.responses import JSONResponse
from starlette.routing import Route

from agentforge_supervisor.python_runner import PythonComputer


def mount_python(app, image):
    computer = PythonComputer(image)

    async def execute(request):
        stopped = threading.Event()
        try:
            data = bytearray()
            async for part in request.stream():
                data.extend(part)
                if len(data) > 100000:
                    return JSONResponse({"error": "input_too_large"}, status_code=413)
            payload = PythonJob.model_validate_json(data).model_dump()
            if payload["image"] != image:
                return JSONResponse({"error": "image_mismatch"}, status_code=409)
        except (ValueError, json.JSONDecodeError):
            return JSONResponse({"error": "invalid_python_job"}, status_code=422)
        try:
            async with anyio.create_task_group() as group:

                async def watch():
                    while True:
                        if await request.is_disconnected():
                            stopped.set()
                            return
                        await anyio.sleep(0.2)

                group.start_soon(watch)
                result = await anyio.to_thread.run_sync(computer.execute, payload, stopped)
                group.cancel_scope.cancel()
            return JSONResponse(result.model_dump())
        except Exception:
            return JSONResponse({"error": "python_execution_unavailable"}, status_code=503)
        finally:
            stopped.set()

    app.router.routes.append(Route("/python/execute", execute, methods=["POST"]))
