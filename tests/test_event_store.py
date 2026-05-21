import asyncio

from src.orchestrator.event_store import (
    EventRetentionPolicy,
    EventRetentionStore,
)
from src.orchestrator.engine import OrchestrationEngine


class Clock:
    def __init__(self, start=1_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def test_task_events_are_written_to_separate_stores():
    clock = Clock()
    store = EventRetentionStore(clock=clock)

    result = store.record_task_event(
        "task.completed",
        "task-1",
        {"duration_ms": 15, "result": {"status": "ok"}},
        actor="agent-1",
    )

    operational = store.operational_logs()
    audit = store.audit_records()

    assert len(operational) == 1
    assert len(audit) == 1
    assert operational[0]["id"] != audit[0]["id"]
    assert operational[0]["event_type"] == "task.completed"
    assert audit[0]["digest"]
    assert audit[0]["previous_digest"] is None
    assert result["audit_record"]["actor"] == "agent-1"


def test_engine_lifecycle_events_are_written_to_both_stores():
    store = EventRetentionStore(clock=Clock())
    engine = OrchestrationEngine(event_store=store)
    agent_id = engine.registry.register("worker", "worker.default")

    asyncio.run(
        engine._execute_task(
            {"id": "task-1", "target_agent": agent_id}
        )
    )

    operational_events = [
        record["event_type"] for record in store.operational_logs()
    ]
    audit_events = [
        record["event_type"] for record in store.audit_records()
    ]

    assert operational_events == ["task.started", "task.completed"]
    assert audit_events == ["task.started", "task.completed"]
    assert store.verify_audit_chain()


def test_audit_records_are_append_only_defensive_copies_with_digest_chain():
    store = EventRetentionStore(clock=Clock())

    first = store.append_audit_record(
        "task.started",
        "task-1",
        {"status": "start"},
    )
    second = store.append_audit_record(
        "task.completed",
        "task-1",
        {"status": "done"},
    )

    returned_records = store.audit_records()
    returned_records.pop()
    returned_records[0]["payload"]["status"] = "tampered"

    audit = store.audit_records()

    assert len(audit) == 2
    assert audit[0]["payload"]["status"] == "start"
    assert first["sequence"] == 1
    assert second["sequence"] == 2
    assert second["previous_digest"] == first["digest"]
    assert store.verify_audit_chain()


def test_audit_chain_verification_detects_tampering():
    store = EventRetentionStore(clock=Clock())
    store.append_audit_record(
        "task.started",
        "task-1",
        {"status": "start"},
    )
    store.append_audit_record(
        "task.completed",
        "task-1",
        {"status": "done"},
    )

    store._audit_records[0]["payload"]["status"] = "changed"

    assert not store.verify_audit_chain()


def test_operational_cleanup_does_not_delete_audit_records():
    clock = Clock()
    policy = EventRetentionPolicy(operational_seconds=60, audit_seconds=3_600)
    store = EventRetentionStore(retention_policy=policy, clock=clock)
    store.record_task_event("task.started", "task-1", {"queue": "default"})

    clock.advance(120)
    summary = store.cleanup_expired()

    assert summary == {"operational_deleted": 1, "audit_deleted": 0}
    assert store.operational_logs() == []
    assert len(store.audit_records()) == 1
    assert store.verify_audit_chain()


def test_audit_retention_reporting_is_separate_from_operational_cleanup():
    clock = Clock()
    policy = EventRetentionPolicy(operational_seconds=60, audit_seconds=120)
    store = EventRetentionStore(retention_policy=policy, clock=clock)
    store.record_task_event("task.started", "task-1")

    clock.advance(180)
    expired_audit = store.audit_records_expired_before()

    assert len(expired_audit) == 1
    assert store.cleanup_operational_logs() == 1
    assert len(store.audit_records()) == 1
