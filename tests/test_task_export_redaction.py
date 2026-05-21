import csv
from io import StringIO

import pytest
from fastapi.testclient import TestClient

from src.api.routes import task_scheduler
from src.api.server import create_app
from src.common.export_policy import (
    ExportFormat,
    ExportPolicyError,
    MASKED_VALUE,
    export_task_records,
    serialize_task_record,
)
from src.orchestrator.scheduler import TaskScheduler


def _task_record():
    return {
        "id": "task-1",
        "type": "download",
        "queue": "exports",
        "priority": 5,
        "status": "queued",
        "retries": 0,
        "payload": {
            "dataset": "tasks",
            "access_token": "raw-token",
            "nested": {
                "password": "raw-password",
                "visible": "keep",
            },
        },
        "metadata": {
            "owner": "analytics",
            "client_secret": "raw-secret",
        },
        "config": {
            "api_key": "raw-api-key",
            "region": "eu",
        },
        "error": "connection failed with raw-token",
    }


def _reset_route_scheduler():
    task_scheduler._queues.clear()
    task_scheduler._scheduled.clear()
    task_scheduler._in_flight.clear()


def test_json_csv_and_ui_exports_share_redaction_policy():
    record = _task_record()

    json_records = export_task_records([record], ExportFormat.JSON)
    ui_records = export_task_records([record], ExportFormat.UI)
    csv_text = export_task_records([record], ExportFormat.CSV)

    assert json_records == ui_records
    exported = json_records[0]
    assert exported["payload"]["access_token"] == MASKED_VALUE
    assert "password" not in exported["payload"]["nested"]
    assert exported["payload"]["nested"]["visible"] == "keep"
    assert "client_secret" not in exported["metadata"]
    assert exported["config"]["api_key"] == MASKED_VALUE
    assert exported["error"] == MASKED_VALUE

    restricted_values = (
        "raw-token",
        "raw-password",
        "raw-secret",
        "raw-api-key",
    )
    for restricted in restricted_values:
        assert restricted not in csv_text

    csv_rows = list(csv.DictReader(StringIO(csv_text)))
    assert csv_rows[0]["id"] == "task-1"
    assert MASKED_VALUE in csv_rows[0]["payload"]


def test_new_top_level_export_fields_require_policy_classification():
    record = _task_record()
    record["download_url"] = "https://example.test/raw-export.json"

    with pytest.raises(ExportPolicyError, match="download_url"):
        serialize_task_record(record, ExportFormat.JSON)


def test_scheduler_exports_redacted_task_records():
    scheduler = TaskScheduler()
    scheduler.enqueue(
        {
            "type": "download",
            "payload": {
                "visible": "ok",
                "token": "scheduler-token",
            },
            "metadata": {
                "secret": "scheduler-secret",
                "source": "unit-test",
            },
        },
        queue="exports",
        priority=10,
    )

    json_records = scheduler.export_tasks(ExportFormat.JSON)
    ui_records = scheduler.export_tasks(ExportFormat.UI)
    csv_text = scheduler.export_tasks(ExportFormat.CSV)

    assert json_records == ui_records
    assert json_records[0]["queue"] == "exports"
    assert json_records[0]["payload"]["token"] == MASKED_VALUE
    assert "secret" not in json_records[0]["metadata"]
    assert "scheduler-token" not in csv_text
    assert "scheduler-secret" not in csv_text


def test_api_task_export_paths_use_shared_redaction_policy():
    _reset_route_scheduler()
    task_scheduler.enqueue(
        {
            "type": "download",
            "payload": {
                "visible": "ok",
                "authorization": "Bearer raw-api-token",
            },
        },
        queue="exports",
    )

    client = TestClient(create_app())
    headers = {"Authorization": "Bearer test"}

    json_response = client.get("/api/v2/tasks/export/json", headers=headers)
    csv_response = client.get("/api/v2/tasks/export/csv", headers=headers)
    ui_response = client.get("/api/v2/tasks/ui", headers=headers)

    assert json_response.status_code == 200
    assert csv_response.status_code == 200
    assert ui_response.status_code == 200

    json_task = json_response.json()["tasks"][0]
    ui_task = ui_response.json()["tasks"][0]
    assert json_task == ui_task
    assert json_task["payload"]["authorization"] == MASKED_VALUE
    assert "raw-api-token" not in csv_response.text
