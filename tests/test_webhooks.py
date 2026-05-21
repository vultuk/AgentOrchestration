import pytest

from src.orchestrator.webhooks import WebhookRegistry


def event_payload():
    return {
        "task_id": "task-1",
        "status": "completed",
        "run_id": "run-internal",
        "attempt_id": "attempt-internal",
        "worker_pid": 4242,
        "_debug": "private",
        "result": {
            "value": 7,
            "internal_metadata": {"queue": "secret"},
            "nested": [{"secret": "token", "public": "safe"}],
        },
    }


def test_valid_delivery_shapes_payload_before_transport_and_recording():
    registry = WebhookRegistry()
    endpoint_id = registry.register_endpoint(
        "workspace-a",
        "https://hooks.example.com/agent",
        event_types=["task.completed"],
    )
    sent = []

    records = registry.deliver_event(
        "workspace-a",
        "task.completed",
        "event-1",
        event_payload(),
        deliver=lambda url, payload: sent.append((url, payload)) or True,
    )

    assert records[0].status == "delivered"
    assert records[0].endpoint_id == endpoint_id
    assert sent == [("https://hooks.example.com/agent", records[0].payload)]
    payload_text = str(records[0].payload)
    assert "run-internal" not in payload_text
    assert "attempt-internal" not in payload_text
    assert "worker_pid" not in payload_text
    assert "secret" not in payload_text
    assert (
        records[0].payload["payload"]["result"]["nested"][0]["public"]
        == "safe"
    )


def test_rejects_unscoped_and_disabled_endpoint_delivery():
    registry = WebhookRegistry()

    with pytest.raises(ValueError):
        registry.register_endpoint(
            "workspace-a",
            "http://hooks.example.com/plain",
        )

    workspace_a_endpoint = registry.register_endpoint(
        "workspace-a",
        "https://hooks.example.com/a",
    )
    registry.register_endpoint("workspace-b", "https://hooks.example.com/b")
    called = []

    records = registry.deliver_event(
        "workspace-a",
        "task.completed",
        "event-1",
        event_payload(),
        deliver=lambda url, payload: called.append(url) or True,
    )

    assert [record.endpoint_id for record in records] == [workspace_a_endpoint]
    assert called == ["https://hooks.example.com/a"]

    assert registry.disable_endpoint("workspace-a", workspace_a_endpoint)
    records = registry.deliver_event(
        "workspace-a",
        "task.completed",
        "event-2",
        event_payload(),
        deliver=lambda url, payload: called.append(url) or True,
    )

    assert records[0].status == "rejected"
    assert records[0].error == "endpoint_disabled"
    assert called == ["https://hooks.example.com/a"]


def test_retry_behavior_is_idempotent_after_success_and_retries_failures():
    registry = WebhookRegistry()
    registry.register_endpoint("workspace-a", "https://hooks.example.com/a")
    calls = []
    payloads = []

    def capture_failure(url, payload):
        calls.append(url)
        payloads.append(payload)
        return False

    def capture_success(url, payload):
        calls.append(url)
        payloads.append(payload)
        return True

    first = registry.deliver_event(
        "workspace-a",
        "task.completed",
        "event-1",
        event_payload(),
        deliver=capture_failure,
    )[0]

    second = registry.deliver_event(
        "workspace-a",
        "task.completed",
        "event-1",
        {"status": "changed", "run_id": "new-internal"},
        deliver=capture_success,
    )[0]

    third = registry.deliver_event(
        "workspace-a",
        "task.completed",
        "event-1",
        event_payload(),
        deliver=lambda url, payload: calls.append(url) or True,
    )[0]

    assert first.delivery_id == second.delivery_id == third.delivery_id
    assert first is second is third
    assert second.status == "delivered"
    assert second.attempts == 2
    assert calls == [
        "https://hooks.example.com/a",
        "https://hooks.example.com/a",
    ]
    assert payloads == [first.payload, first.payload]
    assert "changed" not in str(first.payload)


def test_rotated_endpoint_uses_new_delivery_scope_for_same_event():
    registry = WebhookRegistry()
    endpoint_id = registry.register_endpoint(
        "workspace-a",
        "https://old.example.com",
    )
    urls = []

    first = registry.deliver_event(
        "workspace-a",
        "task.completed",
        "event-1",
        event_payload(),
        deliver=lambda url, payload: urls.append(url) or True,
    )[0]
    assert registry.rotate_endpoint_url(
        "workspace-a",
        endpoint_id,
        "https://new.example.com",
    )
    second = registry.deliver_event(
        "workspace-a",
        "task.completed",
        "event-1",
        event_payload(),
        deliver=lambda url, payload: urls.append(url) or True,
    )[0]

    assert first.delivery_id != second.delivery_id
    assert first.endpoint_version == 1
    assert second.endpoint_version == 2
    assert urls == ["https://old.example.com", "https://new.example.com"]


def test_callbacks_are_workspace_scoped_idempotent_and_sanitized():
    registry = WebhookRegistry()
    endpoint_id = registry.register_endpoint(
        "workspace-a",
        "https://hooks.test/a",
    )

    assert registry.record_callback(
        "workspace-b",
        endpoint_id,
        "callback-1",
        event_payload(),
    ) == {"status": "rejected", "reason": "endpoint_not_found"}

    first = registry.record_callback(
        "workspace-a",
        endpoint_id,
        "callback-1",
        event_payload(),
    )
    second = registry.record_callback(
        "workspace-a",
        endpoint_id,
        "callback-1",
        {"status": "changed", "run_id": "leak"},
    )

    assert first is second
    assert first["status"] == "accepted"
    assert "run-internal" not in str(first["payload"])
    assert "leak" not in str(second["payload"])
