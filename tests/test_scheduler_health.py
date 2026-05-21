import socket
from threading import Event

import yaml

from src.orchestrator.scheduler_service import run_scheduler_service
from src.orchestrator.scheduler_health import (
    SchedulerHealthConfig,
    check_scheduler_health,
    main,
)


def test_scheduler_health_succeeds_with_writable_storage(tmp_path, capsys):
    report = check_scheduler_health(
        SchedulerHealthConfig(storage_path=str(tmp_path))
    )

    assert report.healthy
    assert report.checks["storage"].startswith("writable:")
    assert report.checks["queue"] == "not configured"
    assert report.checks["storage_service"] == "not configured"

    code = main(["--storage-path", str(tmp_path)])
    output = capsys.readouterr().out

    assert code == 0
    assert '"healthy": true' in output


def test_scheduler_health_fails_when_storage_path_is_not_directory(
    tmp_path,
):
    storage_file = tmp_path / "scheduler-state"
    storage_file.write_text("not a directory", encoding="utf-8")

    report = check_scheduler_health(
        SchedulerHealthConfig(storage_path=str(storage_file))
    )

    assert not report.healthy
    assert "storage" in report.errors


def test_scheduler_health_fails_when_queue_dependency_is_unreachable(
    tmp_path,
):
    port = _unused_local_port()

    report = check_scheduler_health(
        SchedulerHealthConfig(
            queue_url=f"tcp://127.0.0.1:{port}",
            storage_path=str(tmp_path),
            timeout_seconds=0.1,
        )
    )

    assert not report.healthy
    assert "queue" in report.errors


def test_scheduler_health_fails_when_storage_service_is_unreachable(
    tmp_path,
):
    port = _unused_local_port()

    report = check_scheduler_health(
        SchedulerHealthConfig(
            storage_url=f"tcp://127.0.0.1:{port}",
            storage_path=str(tmp_path),
            timeout_seconds=0.1,
        )
    )

    assert not report.healthy
    assert "storage_service" in report.errors


def test_scheduler_image_defines_healthcheck_and_compose_consumes_status():
    dockerfile = _read("infra/Dockerfile.scheduler")
    compose = yaml.safe_load(_read("infra/docker-compose.yml"))

    assert "HEALTHCHECK" in dockerfile
    assert "--start-period=30s" in dockerfile
    assert "src.orchestrator.scheduler_health" in dockerfile
    assert "src.orchestrator.scheduler_service" in dockerfile

    scheduler = compose["services"]["scheduler"]
    assert scheduler["healthcheck"]["test"] == [
        "CMD",
        "python",
        "-m",
        "src.orchestrator.scheduler_health",
    ]
    assert scheduler["healthcheck"]["start_period"] == "30s"
    assert scheduler["environment"]["AO_SCHEDULER_QUEUE_URL"]
    assert scheduler["environment"]["AO_SCHEDULER_STORAGE_URL"]
    assert scheduler["environment"]["AO_SCHEDULER_STORAGE_PATH"]
    assert scheduler["depends_on"]["queue"]["condition"] == "service_healthy"
    assert scheduler["depends_on"]["storage"]["condition"] == (
        "service_healthy"
    )

    consumer = compose["services"]["scheduler-consumer"]
    assert consumer["depends_on"]["scheduler"]["condition"] == (
        "service_healthy"
    )


def test_scheduler_service_exits_when_startup_health_fails(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("AO_SCHEDULER_STORAGE_PATH", str(tmp_path / "state"))
    monkeypatch.setenv(
        "AO_SCHEDULER_QUEUE_URL",
        f"tcp://127.0.0.1:{_unused_local_port()}",
    )
    monkeypatch.setenv("AO_SCHEDULER_HEALTH_TIMEOUT_SECONDS", "0.1")

    assert run_scheduler_service(Event(), poll_seconds=0.01) == 1


def test_scheduler_service_stays_running_after_startup_health_passes(
    monkeypatch,
    tmp_path,
):
    stop_event = Event()
    stop_event.set()
    monkeypatch.setenv("AO_SCHEDULER_STORAGE_PATH", str(tmp_path / "state"))
    monkeypatch.delenv("AO_SCHEDULER_QUEUE_URL", raising=False)
    monkeypatch.delenv("AO_SCHEDULER_STORAGE_URL", raising=False)

    assert run_scheduler_service(stop_event, poll_seconds=0.01) == 0


def _unused_local_port():
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()
