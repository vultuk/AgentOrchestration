import pytest

from src.api.webhooks import WebhookDeliveryService
from src.common.metrics import MetricsCollector


def make_service():
    return WebhookDeliveryService(metrics_collector=MetricsCollector())


def test_register_endpoint_returns_sanitized_endpoint_record():
    service = make_service()

    result = service.register_endpoint(
        "workspace-secret",
        "endpoint-secret",
        "https://hooks.example.com/agent",
    )

    assert result["version"] == 1
    assert result["enabled"] is True
    assert "workspace-secret" not in str(result)
    assert "endpoint-secret" not in str(result)
    assert "hooks.example.com" not in str(result)


def test_valid_delivery_uses_stable_idempotency_key_without_payload_leak():
    service = make_service()
    service.register_endpoint("ws-a", "ep-a", "https://hooks.example.com/a")

    result = service.deliver_event(
        "ws-a",
        "ep-a",
        "event-private-id",
        "agent.started",
        payload={"token": "do-not-expose"},
        event_revision=3,
    )

    assert result["decision"] == "accepted"
    assert result["status"] == "queued"
    assert result["event_revision"] == 3
    assert "event-private-id" not in str(result)
    assert "do-not-expose" not in str(result)
    assert "hooks.example.com" not in str(result)

    audit_text = str(service.audit_records())
    assert "event-private-id" not in audit_text
    assert "do-not-expose" not in audit_text


def test_retry_with_same_idempotency_key_is_duplicate_not_new_record():
    service = make_service()
    service.register_endpoint("ws-a", "ep-a", "https://hooks.example.com/a")

    first = service.deliver_event(
        "ws-a",
        "ep-a",
        "event-1",
        "agent.completed",
        event_revision=1,
    )
    retry = service.deliver_event(
        "ws-a",
        "ep-a",
        "event-1",
        "agent.completed",
        event_revision=1,
    )

    assert retry["decision"] == "duplicate"
    assert retry["idempotency_key"] == first["idempotency_key"]
    assert retry["attempts"] == 2
    assert service.delivery_count() == 1

    counters = service.metrics.snapshot()["counters"]
    assert counters["webhooks.delivery.accepted"] == 1
    assert counters["webhooks.delivery.duplicate"] == 1


def test_workspace_isolation_keeps_identical_endpoint_and_event_separate():
    service = make_service()
    service.register_endpoint("ws-a", "shared-ep", "https://hooks.example/a")
    service.register_endpoint("ws-b", "shared-ep", "https://hooks.example/b")

    first = service.deliver_event(
        "ws-a",
        "shared-ep",
        "event-1",
        "agent.updated",
    )
    second = service.deliver_event(
        "ws-b",
        "shared-ep",
        "event-1",
        "agent.updated",
    )

    assert first["decision"] == "accepted"
    assert second["decision"] == "accepted"
    assert first["idempotency_key"] != second["idempotency_key"]
    assert first["workspace_ref"] != second["workspace_ref"]
    assert service.delivery_count() == 2


def test_wrong_workspace_rejected_without_exposing_endpoint_details():
    service = make_service()
    service.register_endpoint("ws-a", "ep-a", "https://hooks.example.com/a")

    rejected = service.deliver_event(
        "ws-b",
        "ep-a",
        "event-1",
        "agent.updated",
    )

    assert rejected["decision"] == "rejected"
    assert rejected["reason"] == "endpoint_not_found"
    assert "https://hooks.example.com/a" not in str(rejected)
    assert "ws-a" not in str(rejected)


def test_disabled_endpoint_rejection_is_idempotent():
    service = make_service()
    service.register_endpoint("ws-a", "ep-a", "https://hooks.example.com/a")
    service.disable_endpoint("ws-a", "ep-a")

    first = service.deliver_event("ws-a", "ep-a", "event-1", "agent.updated")
    second = service.deliver_event("ws-a", "ep-a", "event-1", "agent.updated")

    assert first["decision"] == "rejected"
    assert first["reason"] == "endpoint_disabled"
    assert second["decision"] == "duplicate"
    assert second["status"] == "rejected"
    assert service.delivery_count() == 1


def test_rotated_endpoint_version_cannot_overwrite_newer_state():
    service = make_service()
    service.register_endpoint("ws-a", "ep-a", "https://hooks.example.com/a")
    service.rotate_endpoint("ws-a", "ep-a", url="https://hooks.example.com/b")

    stale = service.deliver_event(
        "ws-a",
        "ep-a",
        "event-1",
        "agent.updated",
        endpoint_version=1,
    )
    current = service.deliver_event(
        "ws-a",
        "ep-a",
        "event-1",
        "agent.updated",
        endpoint_version=2,
    )

    assert stale["decision"] == "rejected"
    assert stale["reason"] == "endpoint_rotated"
    assert current["decision"] == "accepted"
    assert current["endpoint_version"] == 2
    assert stale["idempotency_key"] != current["idempotency_key"]


def test_stale_event_revision_is_rejected_without_replacing_latest_record():
    service = make_service()
    service.register_endpoint("ws-a", "ep-a", "https://hooks.example.com/a")

    latest = service.deliver_event(
        "ws-a",
        "ep-a",
        "event-1",
        "agent.updated",
        event_revision=4,
    )
    stale = service.deliver_event(
        "ws-a",
        "ep-a",
        "event-1",
        "agent.updated",
        event_revision=3,
        idempotency_key="explicit-stale-key",
    )

    assert stale["decision"] == "rejected"
    assert stale["reason"] == "stale_event_revision"
    latest_record = service.get_delivery(latest["idempotency_key"])
    assert latest_record["status"] == "queued"


def test_callback_is_idempotent_and_cannot_cross_workspace():
    service = make_service()
    service.register_endpoint("ws-a", "ep-a", "https://hooks.example.com/a")
    delivery = service.deliver_event(
        "ws-a",
        "ep-a",
        "event-1",
        "agent.updated",
    )

    accepted = service.record_callback(
        "ws-a",
        "ep-a",
        delivery["idempotency_key"],
        "delivered",
        endpoint_version=1,
    )
    duplicate = service.record_callback(
        "ws-a",
        "ep-a",
        delivery["idempotency_key"],
        "failed",
        endpoint_version=1,
    )
    wrong_workspace = service.record_callback(
        "ws-b",
        "ep-a",
        delivery["idempotency_key"],
        "delivered",
    )

    assert accepted["decision"] == "callback_accepted"
    assert accepted["status"] == "delivered"
    assert duplicate["decision"] == "duplicate"
    assert duplicate["status"] == "delivered"
    assert wrong_workspace["reason"] == "workspace_mismatch"


def test_callback_from_old_endpoint_version_is_rejected():
    service = make_service()
    service.register_endpoint("ws-a", "ep-a", "https://hooks.example.com/a")
    delivery = service.deliver_event(
        "ws-a",
        "ep-a",
        "event-1",
        "agent.updated",
    )
    service.rotate_endpoint("ws-a", "ep-a", url="https://hooks.example.com/b")

    rejected = service.record_callback(
        "ws-a",
        "ep-a",
        delivery["idempotency_key"],
        "delivered",
        endpoint_version=2,
    )

    assert rejected["decision"] == "rejected"
    assert rejected["reason"] == "endpoint_version_mismatch"
    assert service.get_delivery(delivery["idempotency_key"])["status"] == (
        "queued"
    )


def test_invalid_endpoint_url_is_rejected_before_registration():
    service = make_service()

    with pytest.raises(ValueError):
        service.register_endpoint("ws-a", "ep-a", "http://hooks.example.com/a")
