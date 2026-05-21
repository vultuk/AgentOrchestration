"""Long-running scheduler container entrypoint."""

from __future__ import annotations

import json
import signal
import sys
from threading import Event
from typing import Optional

from src.orchestrator.scheduler import TaskScheduler
from src.orchestrator.scheduler_health import check_scheduler_health


def run_scheduler_service(
    stop_event: Optional[Event] = None,
    poll_seconds: float = 5.0,
) -> int:
    """Start the scheduler process after dependency health has passed."""

    report = check_scheduler_health()
    print(json.dumps(report.to_dict(), sort_keys=True), flush=True)
    if not report.healthy:
        return 1

    scheduler = TaskScheduler()
    stop_event = stop_event or Event()
    while not stop_event.wait(poll_seconds):
        # Keep the process resident so container health remains meaningful.
        scheduler.size()

    return 0


def main() -> int:
    stop_event = Event()

    def request_stop(_signum, _frame):
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    return run_scheduler_service(stop_event)


if __name__ == "__main__":
    sys.exit(main())
