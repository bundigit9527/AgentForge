import asyncio
import time
from collections.abc import AsyncIterator

from agentforge_contracts.runs import EventResponse
from agentforge_core.models import TERMINAL_STATUSES, RunEvent, RunNotFound, Scope
from agentforge_storage.repository import RunRepository
from fastapi import Request
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool


def encode_event(event: RunEvent) -> str:
    data = EventResponse.from_event(event).model_dump_json()
    return f"id: {event.sequence}\nevent: {event.event_type}\ndata: {data}\n\n"


async def stream_events(
    request: Request,
    repository: RunRepository,
    scope: Scope,
    run_id: str,
    after: int,
    *,
    poll_interval: float = 0.25,
    heartbeat_interval: float = 15,
) -> AsyncIterator[str]:
    """Read durable events without keeping a database connection open while waiting."""
    cursor = after
    last_output = time.monotonic()
    while not await request.is_disconnected():
        try:
            # Read the state BEFORE events. A terminal state guarantees its final event
            # was committed already, so draining the next query cannot lose that event.
            run = await run_in_threadpool(repository.get, scope, run_id)
            batch = await run_in_threadpool(repository.list_events, scope, run_id, cursor, 100)
        except RunNotFound:
            yield 'event: stream_error\ndata: {"code":"access_unavailable"}\n\n'
            return
        except SQLAlchemyError:
            yield 'event: stream_error\ndata: {"code":"storage_unavailable"}\n\n'
            return
        for event in batch:
            if await request.is_disconnected():
                return
            yield encode_event(event)
            cursor = event.sequence
            last_output = time.monotonic()
        if len(batch) == 100:
            continue
        if run.status in TERMINAL_STATUSES:
            return
        if time.monotonic() - last_output >= heartbeat_interval:
            yield ": keep-alive\n\n"
            last_output = time.monotonic()
        await asyncio.sleep(poll_interval)
