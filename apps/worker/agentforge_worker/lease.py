import logging
from threading import Event, Thread

from agentforge_core.models import Claim
from agentforge_storage.repository import RunRepository

logger = logging.getLogger(__name__)


class LeaseHeartbeat:
    """Keep a claim alive while synchronous model/tool work is in progress."""

    def __init__(
        self,
        repository: RunRepository,
        claim: Claim,
        lease_seconds: int,
        interval: float,
    ):
        self.repository = repository
        self.claim = claim
        self.lease_seconds = lease_seconds
        self.interval = interval
        self.lost = Event()
        self._stop = Event()
        self._thread = Thread(target=self._run, name="agentforge-lease", daemon=True)

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                renewed = self.repository.renew_lease(self.claim, self.lease_seconds)
            except Exception:
                logger.error("Lease renewal failed for run %s", self.claim.run.run_id)
                renewed = False
            if not renewed:
                self.lost.set()
                return

    def __enter__(self) -> "LeaseHeartbeat":
        self._thread.start()
        return self

    def __exit__(self, *_exception) -> None:
        self._stop.set()
        # An unavailable database must not hold worker shutdown indefinitely.
        self._thread.join(timeout=1)
        if self._thread.is_alive():
            self.lost.set()
            logger.error("Lease renewal did not stop promptly for run %s", self.claim.run.run_id)
