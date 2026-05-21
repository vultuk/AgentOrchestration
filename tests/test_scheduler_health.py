import socket

import yaml

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


def test_scheduler_image_defines_healthcheck_and_compose_consumes_status():
    dockerfile = _read("infra/Dockerfile.scheduler")
    compose = yaml.safe_load(_read("infra/docker-compose.yml"))

    assert "HEALTHCHECK" in dockerfile
    assert "--start-period=30s" in dockerfile
    assert "src.orchestrator.scheduler_health" in dockerfile

    scheduler = compose["services"]["scheduler"]
    assert scheduler["healthcheck"]["test"] == [
        "CMD",
        "python",
        "-m",
        "src.orchestrator.scheduler_health",
    ]
    assert scheduler["healthcheck"]["start_period"] == "30s"
    assert scheduler["environment"]["AO_SCHEDULER_QUEUE_URL"]
    assert scheduler["environment"]["AO_SCHEDULER_STORAGE_PATH"]

    consumer = compose["services"]["scheduler-consumer"]
    assert consumer["depends_on"]["scheduler"]["condition"] == (
        "service_healthy"
    )


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
