"""Scheduler container health checks."""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import urlparse


DEFAULT_STORAGE_PATH = "/var/lib/agent-orchestrator/scheduler"
DEFAULT_TIMEOUT_SECONDS = 2.0


@dataclass(frozen=True)
class SchedulerHealthConfig:
    queue_url: Optional[str] = None
    storage_path: str = DEFAULT_STORAGE_PATH
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    @classmethod
    def from_env(cls) -> "SchedulerHealthConfig":
        timeout = os.getenv(
            "AO_SCHEDULER_HEALTH_TIMEOUT_SECONDS",
            str(DEFAULT_TIMEOUT_SECONDS),
        )
        return cls(
            queue_url=os.getenv("AO_SCHEDULER_QUEUE_URL"),
            storage_path=os.getenv(
                "AO_SCHEDULER_STORAGE_PATH",
                DEFAULT_STORAGE_PATH,
            ),
            timeout_seconds=float(timeout),
        )


@dataclass(frozen=True)
class SchedulerHealthReport:
    healthy: bool
    checks: Dict[str, str]
    errors: Dict[str, str]

    def to_dict(self) -> Dict[str, object]:
        return {
            "healthy": self.healthy,
            "checks": self.checks,
            "errors": self.errors,
        }


def check_scheduler_health(
    config: Optional[SchedulerHealthConfig] = None,
) -> SchedulerHealthReport:
    """Verify scheduler dependencies used by the container healthcheck."""

    config = config or SchedulerHealthConfig.from_env()
    checks: Dict[str, str] = {}
    errors: Dict[str, str] = {}

    _check_storage(config.storage_path, checks, errors)
    _check_queue(config.queue_url, config.timeout_seconds, checks, errors)

    return SchedulerHealthReport(
        healthy=not errors,
        checks=checks,
        errors=errors,
    )


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check scheduler queue and storage dependencies.",
    )
    parser.add_argument("--queue-url")
    parser.add_argument("--storage-path", default=DEFAULT_STORAGE_PATH)
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
    )
    args = parser.parse_args(argv)

    config = SchedulerHealthConfig(
        queue_url=args.queue_url or os.getenv("AO_SCHEDULER_QUEUE_URL"),
        storage_path=os.getenv(
            "AO_SCHEDULER_STORAGE_PATH",
            args.storage_path,
        ),
        timeout_seconds=float(
            os.getenv(
                "AO_SCHEDULER_HEALTH_TIMEOUT_SECONDS",
                args.timeout,
            )
        ),
    )
    report = check_scheduler_health(config)
    print(json.dumps(report.to_dict(), sort_keys=True))
    return 0 if report.healthy else 1


def _check_storage(
    storage_path: str,
    checks: Dict[str, str],
    errors: Dict[str, str],
) -> None:
    path = Path(storage_path)
    try:
        if path.exists() and not path.is_dir():
            raise OSError("storage path is not a directory")
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=str(path),
            prefix=".scheduler-health-",
            delete=True,
        ) as probe:
            probe.write(b"ok")
            probe.flush()
        checks["storage"] = f"writable:{path}"
    except OSError as exc:
        errors["storage"] = f"{path}: {exc}"


def _check_queue(
    queue_url: Optional[str],
    timeout_seconds: float,
    checks: Dict[str, str],
    errors: Dict[str, str],
) -> None:
    if not queue_url:
        checks["queue"] = "not configured"
        return

    parsed = _parse_queue_url(queue_url)
    host = parsed.hostname
    port = parsed.port or _default_port(parsed.scheme)
    if not host or not port:
        errors["queue"] = f"{queue_url}: requires host and port"
        return

    try:
        with socket.create_connection((host, port), timeout_seconds):
            checks["queue"] = f"reachable:{host}:{port}"
    except OSError as exc:
        errors["queue"] = f"{host}:{port}: {exc}"


def _parse_queue_url(queue_url: str):
    if "://" not in queue_url:
        queue_url = f"tcp://{queue_url}"
    return urlparse(queue_url)


def _default_port(scheme: str) -> Optional[int]:
    return {
        "http": 80,
        "https": 443,
        "postgres": 5432,
        "postgresql": 5432,
        "redis": 6379,
    }.get(scheme)


if __name__ == "__main__":
    sys.exit(main())
