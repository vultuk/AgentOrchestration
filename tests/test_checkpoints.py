import pytest

from src.common.metrics import metrics
from src.orchestrator import CheckpointDigestMismatchError, CheckpointStore
from src.orchestrator.engine import OrchestrationEngine


def counter_value(name):
    return metrics.snapshot()["counters"].get(name, 0)


def test_retry_after_timeout_upserts_one_checkpoint_for_resume():
    store = CheckpointStore()
    payload = {"cursor": "page-42", "rows": [1, 2, 3]}
    created_start = counter_value("checkpoint.write.created")
    idempotent_start = counter_value("checkpoint.write.idempotent")

    with pytest.raises(TimeoutError):
        store.write("task-1", "extract", 2, payload)
        raise TimeoutError("worker timed out after checkpoint persisted")

    retry = store.write(
        "task-1",
        "extract",
        2,
        {"rows": [1, 2, 3], "cursor": "page-42"},
    )

    records = store.list_records()
    assert len(records) == 1
    assert retry.key == CheckpointStore.make_key("task-1", "extract", 2)
    assert retry.write_count == 2

    resumed = store.latest_for_step("task-1", "extract")
    assert resumed is not None
    assert resumed.payload == payload
    assert resumed.digest == retry.digest
    assert counter_value("checkpoint.write.created") == created_start + 1
    assert counter_value("checkpoint.write.idempotent") == idempotent_start + 1

    audit_events = store.list_audit_events()
    assert [event["decision"] for event in audit_events] == [
        "created",
        "idempotent",
    ]
    assert "task-1" not in audit_events[0]["checkpoint"]


def test_digest_mismatch_for_same_checkpoint_key_fails_loudly():
    store = CheckpointStore()
    mismatch_start = counter_value("checkpoint.write.digest_mismatch")
    store.write("task-1", "transform", 1, {"version": 1, "status": "ok"})

    with pytest.raises(CheckpointDigestMismatchError, match="does not match"):
        store.write("task-1", "transform", 1, {"version": 2, "status": "ok"})

    assert len(store.list_records()) == 1
    assert store.get("task-1", "transform", 1).payload["version"] == 1
    assert (
        counter_value("checkpoint.write.digest_mismatch")
        == mismatch_start + 1
    )
    assert store.list_audit_events()[-1]["decision"] == "rejected"


def test_latest_resume_checkpoint_uses_highest_attempt():
    store = CheckpointStore()
    store.write("task-7", "load", 1, {"cursor": "old"})
    store.write("task-7", "load", 3, {"cursor": "new"})

    resumed = store.latest_for_step("task-7", "load")

    assert resumed is not None
    assert resumed.attempt == 3
    assert resumed.payload == {"cursor": "new"}


def test_engine_checkpoint_helpers_use_task_attempt_metadata():
    engine = OrchestrationEngine()
    task = {"id": "task-9", "retries": 1}

    engine.save_checkpoint(task, "fetch", {"offset": 10})
    engine.save_checkpoint(task, "fetch", {"offset": 10})

    resumed = engine.resume_checkpoint("task-9", "fetch", attempt=1)
    assert resumed is not None
    assert resumed.payload == {"offset": 10}
    assert resumed.write_count == 2
    assert len(engine.checkpoints.list_records()) == 1
