import pytest

from src.orchestrator.webhooks import WebhookManager, WebhookValidationError


def test_production_registration_rejects_non_https_before_persistence():
    manager = WebhookManager(environment="production")

    with pytest.raises(WebhookValidationError, match="HTTPS"):
        manager.register_endpoint(
            workspace_id="alpha",
            url="http://hooks.example.com/events",
            events=["task.completed"],
        )

    assert manager.endpoints == {}


def test_valid_delivery_sanitizes_callback_payload_and_public_record():
    calls = []

    def sender(url, payload):
        calls.append((url, payload))
        return {"status_code": 204}

    manager = WebhookManager(environment="production", callback_sender=sender)
    endpoint = manager.register_endpoint(
        workspace_id="alpha",
        url="https://hooks.example.com/events",
        events=["task.completed"],
        endpoint_id="endpoint-1",
    )

    record = manager.deliver_event(
        workspace_id="alpha",
        endpoint_id=endpoint.id,
        event="task.completed",
        delivery_id="delivery-1",
        payload={
            "task_id": "task-1",
            "_internal_token": "secret",
            "nested": {
                "visible": True,
                "internal_trace": "trace-id",
            },
        },
    )

    assert record.delivered is True
    assert calls == [
        (
            "https://hooks.example.com/events",
            {
                "delivery_id": "delivery-1",
                "event": "task.completed",
                "data": {
                    "task_id": "task-1",
                    "nested": {"visible": True},
                },
            },
        )
    ]
    public_records = manager.delivery_records("alpha")
    public_record = public_records[record.id]
    assert public_record["payload"] == calls[0][1]
    assert "workspace_id" not in public_record
    assert "secret_version" not in public_record


def test_retry_delivery_is_idempotent_for_same_delivery_id():
    calls = []

    def sender(url, payload):
        calls.append(payload)
        return {"status_code": 202}

    manager = WebhookManager(environment="production", callback_sender=sender)
    endpoint = manager.register_endpoint(
        workspace_id="alpha",
        url="https://hooks.example.com/events",
        events=["task.completed"],
    )

    first = manager.deliver_event(
        workspace_id="alpha",
        endpoint_id=endpoint.id,
        event="task.completed",
        delivery_id="delivery-1",
        payload={"task_id": "task-1"},
    )
    retry = manager.deliver_event(
        workspace_id="alpha",
        endpoint_id=endpoint.id,
        event="task.completed",
        delivery_id="delivery-1",
        payload={"task_id": "task-1", "attempt": 2},
    )

    assert retry is first
    assert len(calls) == 1
    assert calls[0]["data"] == {"task_id": "task-1"}


def test_delivery_rejects_wrong_workspace_before_callback():
    calls = []
    manager = WebhookManager(
        environment="production",
        callback_sender=(
            lambda url, payload: calls.append(payload) or {"status_code": 202}
        ),
    )
    endpoint = manager.register_endpoint(
        workspace_id="alpha",
        url="https://hooks.example.com/events",
        events=["task.completed"],
    )

    with pytest.raises(WebhookValidationError, match="workspace"):
        manager.deliver_event(
            workspace_id="beta",
            endpoint_id=endpoint.id,
            event="task.completed",
            payload={"task_id": "task-1"},
        )

    assert calls == []


def test_disabled_or_rotated_endpoint_rejected_before_callback():
    calls = []
    manager = WebhookManager(
        environment="production",
        callback_sender=(
            lambda url, payload: calls.append(payload) or {"status_code": 202}
        ),
    )
    endpoint = manager.register_endpoint(
        workspace_id="alpha",
        url="https://hooks.example.com/events",
        events=["task.completed"],
        secret_version="v1",
    )

    manager.disable_endpoint(endpoint.id)
    with pytest.raises(WebhookValidationError, match="disabled"):
        manager.deliver_event(
            workspace_id="alpha",
            endpoint_id=endpoint.id,
            event="task.completed",
            payload={"task_id": "task-1"},
            secret_version="v1",
        )

    active_endpoint = manager.register_endpoint(
        workspace_id="alpha",
        url="https://hooks.example.com/rotated",
        events=["task.completed"],
        endpoint_id="endpoint-2",
        secret_version="v1",
    )
    manager.rotate_secret(active_endpoint.id, "v2")
    with pytest.raises(WebhookValidationError, match="rotated"):
        manager.deliver_event(
            workspace_id="alpha",
            endpoint_id=active_endpoint.id,
            event="task.completed",
            payload={"task_id": "task-1"},
            secret_version="v1",
        )

    manager.deliver_event(
        workspace_id="alpha",
        endpoint_id=active_endpoint.id,
        event="task.completed",
        payload={"task_id": "task-1"},
        secret_version="v2",
    )
    assert len(calls) == 1
