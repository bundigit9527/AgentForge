import argparse
import logging
import math
import signal
from contextlib import ExitStack
from threading import Event

from agentforge_core.settings import Settings
from agentforge_runtime.chat_harness import configured_harness
from agentforge_runtime.config import ModelSettings
from agentforge_storage.database import create_database
from agentforge_storage.repository import RunRepository
from sqlalchemy.exc import SQLAlchemyError

from agentforge_worker.worker import Worker


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Process AgentForge tasks with a mock or Chat Completions model"
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="Process at most one task and exit")
    mode.add_argument("--watch", action="store_true", help="Poll continuously until SIGINT/SIGTERM")
    parser.add_argument("--poll-interval", type=float, default=1)
    parser.add_argument("--lease-seconds", type=int, default=60)
    parser.add_argument("--heartbeat-interval", type=float)
    args = parser.parse_args()
    interval = args.heartbeat_interval
    if not math.isfinite(args.poll_interval) or args.poll_interval <= 0:
        parser.error("--poll-interval must be positive and finite")
    if args.lease_seconds < 1 or (
        interval is not None
        and (not math.isfinite(interval) or not 0 < interval < args.lease_seconds)
    ):
        parser.error("--lease-seconds must be positive; heartbeat must be shorter than the lease")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        settings = Settings.from_env()
        model_settings = ModelSettings.from_env()
    except ValueError as exc:
        parser.error(str(exc))
    previous_handlers = {}
    try:
        with ExitStack() as resources:
            engine = create_database(settings.database_url)
            resources.callback(engine.dispose)
            worker = Worker(
                RunRepository(engine),
                harness_factory=lambda snapshot: configured_harness(
                    model_settings.for_snapshot(snapshot.model)
                ),
                lease_seconds=args.lease_seconds,
                heartbeat_interval=interval,
            )
            if args.once:
                handled = worker.run_once()
                print("Task processed" if handled else "No eligible task")
            else:
                stop = Event()
                for signum in (signal.SIGINT, signal.SIGTERM):
                    previous_handlers[signum] = signal.signal(signum, lambda *_: stop.set())
                worker.run_forever(stop, poll_interval=args.poll_interval)
    except SQLAlchemyError:
        logging.error("Task storage is unavailable; check database access and migrations")
        raise SystemExit(1) from None
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    main()
