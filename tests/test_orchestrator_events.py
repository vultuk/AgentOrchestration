from src.orchestrator.engine import OrchestrationEngine


def test_unknown_upgrade_event_is_quarantined_without_state_change():
    engine = OrchestrationEngine()
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

    audit_text = str(engine.event_audit_records())
    assert run_id not in audit_text
    assert "do-not-log" not in audit_text

    counters = engine.metrics.snapshot()["counters"]
    assert counters["orchestrator.events.accepted"] == 1
    assert counters["orchestrator.events.quarantined"] == 1


def test_stale_revision_is_quarantined_and_lifecycle_is_preserved():
    engine = OrchestrationEngine()
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
            "revision": 7,
        }
    )

    assert stale["decision"] == "quarantined"
    assert stale["reason"] == "stale_revision"
    assert engine.get_run_event_state(run_id) == {
        "lifecycle": "running",
        "attempt": 2,
        "revision": 7,
        "event_type": "run.started",
    }


def test_invalid_lifecycle_transition_is_quarantined_before_commit():
    engine = OrchestrationEngine()
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
