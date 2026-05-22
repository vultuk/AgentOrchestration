from src.common.metrics import MetricsCollector
from src.orchestrator.engine import OrchestrationEngine


def make_engine():
    return OrchestrationEngine(metrics_collector=MetricsCollector())


def test_unknown_upgrade_event_is_quarantined_without_state_change():
    engine = make_engine()
    run_id = "run-secret-rolling-upgrade"

    accepted = engine.dispatch_event(
        {
            "type": "run.started",
            "run_id": run_id,
            "attempt": 1,
            "revision": 1,
            "payload": {"private": "do-not-log"},
        }
    )
    quarantined = engine.dispatch_event(
        {
            "type": "run.promoted_by_new_version",
            "run_id": run_id,
            "attempt": 1,
            "revision": 2,
            "payload": {"private": "do-not-log"},
        }
    )

    assert accepted["decision"] == "accepted"
    assert quarantined["decision"] == "quarantined"
    assert quarantined["reason"] == "unknown_event_type"
    assert engine.get_run_event_state(run_id)["lifecycle"] == "running"
    assert engine.quarantined_events()[-1] == quarantined

    audit_text = str(engine.event_audit_records())
    assert run_id not in audit_text
    assert "do-not-log" not in audit_text

    counters = engine.metrics.snapshot()["counters"]
    assert counters["orchestrator.events.accepted"] == 1
    assert counters["orchestrator.events.quarantined"] == 1
    assert counters["orchestrator.events.run.accepted"] == 1
    assert counters["orchestrator.events.quarantined.unknown_event_type"] == 1


def test_task_and_handler_event_families_are_guarded_independently():
    engine = make_engine()

    task = engine.dispatch_event(
        {
            "type": "task.started",
            "task_id": "task-secret-42",
            "attempt": 1,
            "revision": 1,
            "lifecycle": "running",
        }
    )
    handler = engine.dispatch_event(
        {
            "type": "handler.started",
            "handler_id": "handler-secret-42",
            "attempt": 1,
            "revision": 1,
            "lifecycle": "running",
        }
    )

    assert task["decision"] == "accepted"
    assert handler["decision"] == "accepted"
    assert engine.get_event_state("task-secret-42", "task")["lifecycle"] == (
        "running"
    )
    assert engine.get_event_state("handler-secret-42")["entity_type"] == (
        "handler"
    )


def test_stale_revision_is_quarantined_and_lifecycle_is_preserved():
    engine = make_engine()
    run_id = "run-secret-stale-revision"

    engine.dispatch_event(
        {
            "type": "run.started",
            "run_id": run_id,
            "attempt": 2,
            "revision": 7,
        }
    )
    stale = engine.dispatch_event(
        {
            "type": "run.completed",
            "run_id": run_id,
            "attempt": 2,
            "revision": 6,
        }
    )

    assert stale["decision"] == "quarantined"
    assert stale["reason"] == "stale_revision"
    assert engine.get_run_event_state(run_id) == {
        "entity_type": "run",
        "lifecycle": "running",
        "attempt": 2,
        "revision": 7,
        "event_type": "run.started",
    }


def test_duplicate_transition_is_quarantined_before_commit():
    engine = make_engine()

    engine.dispatch_event(
        {
            "type": "task.started",
            "entity_id": "task-duplicate",
            "attempt": 1,
            "revision": 1,
        }
    )
    duplicate = engine.dispatch_event(
        {
            "type": "task.started",
            "entity_id": "task-duplicate",
            "attempt": 1,
            "revision": 2,
        }
    )

    assert duplicate["decision"] == "quarantined"
    assert duplicate["reason"] == "duplicate_transition"
    assert engine.get_event_state("task-duplicate")["revision"] == 1


def test_terminal_lifecycle_rewrite_is_quarantined_before_commit():
    engine = make_engine()
    run_id = "run-secret-terminal"

    engine.dispatch_event(
        {
            "type": "run.started",
            "run_id": run_id,
            "attempt": 1,
            "revision": 1,
        }
    )
    engine.dispatch_event(
        {
            "type": "run.completed",
            "run_id": run_id,
            "attempt": 1,
            "revision": 2,
        }
    )
    rewrite = engine.dispatch_event(
        {
            "type": "run.started",
            "run_id": run_id,
            "attempt": 2,
            "revision": 3,
        }
    )

    assert rewrite["decision"] == "quarantined"
    assert rewrite["reason"] == "terminal_lifecycle"
    assert engine.get_run_event_state(run_id)["lifecycle"] == "completed"


def test_invalid_lifecycle_transition_is_quarantined_before_commit():
    engine = make_engine()
    run_id = "run-secret-invalid-lifecycle"

    invalid = engine.dispatch_event(
        {
            "type": "run.completed",
            "run_id": run_id,
            "attempt": 1,
            "revision": 1,
        }
    )

    assert invalid["decision"] == "quarantined"
    assert invalid["reason"] == "invalid_lifecycle"
    assert engine.get_run_event_state(run_id) is None


def test_policy_lifecycle_mismatch_is_rejected_before_state_commit():
    engine = make_engine()

    rejected = engine.dispatch_event(
        {
            "type": "handler.completed",
            "handler_id": "handler-lifecycle-mismatch",
            "attempt": 1,
            "revision": 1,
            "lifecycle": "running",
        }
    )

    assert rejected["decision"] == "quarantined"
    assert rejected["reason"] == "policy_lifecycle_mismatch"
    assert engine.get_event_state("handler-lifecycle-mismatch") is None


def test_missing_entity_ids_are_quarantined_by_family():
    engine = make_engine()

    missing_run = engine.dispatch_event(
        {
            "type": "run.started",
            "attempt": 1,
            "revision": 1,
        }
    )
    missing_task = engine.dispatch_event(
        {
            "type": "task.started",
            "attempt": 1,
            "revision": 1,
        }
    )

    assert missing_run["reason"] == "missing_run_id"
    assert missing_task["reason"] == "missing_task_id"
    assert missing_run["entity_ref"] == "unknown"


def test_invalid_version_marker_is_quarantined_without_state():
    engine = make_engine()

    invalid_revision = engine.dispatch_event(
        {
            "type": "handler.started",
            "handler_id": "handler-invalid-version",
            "revision": "not-a-number",
            "attempt": 1,
            "lifecycle": "running",
        }
    )
    negative_attempt = engine.dispatch_event(
        {
            "type": "handler.started",
            "handler_id": "handler-negative-attempt",
            "revision": 1,
            "attempt": -1,
            "lifecycle": "running",
        }
    )

    assert invalid_revision["reason"] == "invalid_version_marker"
    assert negative_attempt["reason"] == "invalid_version_marker"
    assert engine.get_event_state("handler-invalid-version") is None
    assert engine.get_event_state("handler-negative-attempt") is None
