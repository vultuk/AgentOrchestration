from src.common.metrics import MetricsCollector
from src.orchestrator import EventIntake, OrchestrationEngine


def event(**overrides):
    data = {
        "event_id": "evt-1",
        "run_id": "run-1",
        "tenant_id": "tenant-a",
        "attempt": 1,
        "revision": 1,
        "lifecycle": "pending",
    }
    data.update(overrides)
    return data


def test_accepts_valid_event_and_records_sanitized_audit():
    metrics = MetricsCollector()
    intake = EventIntake(metrics)

    decision = intake.ingest(event(runtime_payload={"secret": "hidden"}))

    assert decision.accepted
    assert decision.reason == "accepted"
    assert intake.get_state("run-1") == {
        "tenant_id": "tenant-a",
        "attempt": 1,
        "revision": 1,
        "lifecycle": "pending",
    }
    audit = intake.audit_records[-1]
    assert audit["accepted"] is True
    assert audit["reason"] == "accepted"
    assert "runtime_payload" not in audit
    assert "tenant_id" not in audit
    assert metrics.snapshot()["counters"]["event_intake.accepted"] == 1


def test_rejects_cross_tenant_transition_without_state_change():
    intake = EventIntake(MetricsCollector())
    intake.ingest(event())

    decision = intake.ingest(
        event(
            event_id="evt-2",
            tenant_id="tenant-b",
            revision=2,
            lifecycle="running",
        ),
    )

    assert not decision.accepted
    assert decision.reason == "tenant_mismatch"
    assert intake.get_state("run-1")["tenant_id"] == "tenant-a"
    assert intake.get_state("run-1")["lifecycle"] == "pending"
    assert intake.audit_records[-1]["reason"] == "tenant_mismatch"


def test_rejects_stale_attempt_and_revision():
    metrics = MetricsCollector()
    intake = EventIntake(metrics)
    intake.ingest(event(attempt=2, revision=3, lifecycle="pending"))

    stale_attempt = intake.ingest(
        event(event_id="evt-2", attempt=1, revision=4, lifecycle="running"),
    )
    stale_revision = intake.ingest(
        event(event_id="evt-3", attempt=2, revision=2, lifecycle="running"),
    )

    assert stale_attempt.reason == "stale_attempt"
    assert stale_revision.reason == "stale_revision"
    assert intake.get_state("run-1")["revision"] == 3
    assert metrics.snapshot()["counters"]["event_intake.rejected"] == 2


def test_rejects_duplicate_revision_without_committing():
    intake = EventIntake(MetricsCollector())
    intake.ingest(event(revision=5, lifecycle="pending"))

    decision = intake.ingest(
        event(event_id="evt-2", revision=5, lifecycle="running"),
    )

    assert not decision.accepted
    assert decision.reason == "duplicate_revision"
    assert intake.get_state("run-1")["lifecycle"] == "pending"


def test_rejects_invalid_terminal_lifecycle_transition():
    intake = EventIntake(MetricsCollector())
    intake.ingest(event(revision=1, lifecycle="pending"))
    intake.ingest(event(event_id="evt-2", revision=2, lifecycle="running"))
    intake.ingest(event(event_id="evt-3", revision=3, lifecycle="completed"))

    decision = intake.ingest(
        event(event_id="evt-4", revision=4, lifecycle="running"),
    )

    assert not decision.accepted
    assert decision.reason == "invalid_lifecycle"
    assert intake.get_state("run-1")["lifecycle"] == "completed"


def test_orchestration_engine_exposes_event_intake_guard():
    engine = OrchestrationEngine()

    decision = engine.ingest_event(event())

    assert decision.accepted
    assert engine.event_intake.get_state("run-1")["tenant_id"] == "tenant-a"
