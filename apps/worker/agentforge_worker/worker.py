import logging
import math
from threading import Event

from agentforge_runtime.harness import ExecutionStopped, Harness
from agentforge_storage.repository import RunRepository
from sqlalchemy.exc import SQLAlchemyError

from agentforge_worker.lease import LeaseHeartbeat

logger = logging.getLogger(__name__)


class Worker:
    def __init__(
        self,
        repository: RunRepository,
        harness: Harness | None = None,
        *,
        lease_seconds: int = 60,
        heartbeat_interval: float | None = None,
    ):
        interval = lease_seconds / 3 if heartbeat_interval is None else heartbeat_interval
        if lease_seconds < 1 or not math.isfinite(interval) or not 0 < interval < lease_seconds:
            raise ValueError("Heartbeat interval must be positive and shorter than the lease")
        self.repository = repository
        self.harness = harness or Harness()
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
                result = self.harness.execute(
                    claim.run.prompt,
                    before_tool=lambda name: (
                        not heartbeat.lost.is_set() and self.repository.record_tool(claim, name)
                    ),
                )
        except ExecutionStopped:
            logger.info("Execution stopped for run %s", claim.run.run_id)
        except Exception:
            # Do not expose provider exceptions, credentials or tool input in the public API.
            logger.error("Execution failed for run %s", claim.run.run_id)
            if not heartbeat.lost.is_set():
                self.repository.finish(claim, failed=True)
        else:
            if not heartbeat.lost.is_set():
                self.repository.finish(claim, result=result)
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
