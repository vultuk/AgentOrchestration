import asyncio
import json
import logging

from src.common.exception_tracking import (
    ExceptionTracker,
    sanitize_exception_context,
)
from src.common.logging import StructuredFormatter
from src.orchestrator.engine import OrchestrationEngine


def test_sanitizer_drops_nested_payload_context():
    context = {
        "component": "worker",
        "operation": "process_task",
        "task": {
            "id": "task-123",
            "attempt": 2,
            "payload": {
                "customer": "Alice",
                "card": "4111111111111111",
            },
            "metadata": {"authorization": "Bearer private-token"},
        },
        "nested": {
            "body": {"raw": "do-not-store"},
            "task_id": "task-123",
        },
    }

    event = sanitize_exception_context(
        context=context,
        error=RuntimeError("payload contains Alice"),
    )

    assert event["task_id"] == "task-123"
    assert event["error_class"] == "RuntimeError"
    assert event["context"]["component"] == "worker"
    assert event["context"]["operation"] == "process_task"
    assert event["context"]["attempt"] == 2
    serialized = json.dumps(event)
    assert "payload" not in serialized.lower()
    assert "Alice" not in serialized
    assert "4111111111111111" not in serialized
    assert "private-token" not in serialized


def test_sanitizer_drops_local_variable_capture():
    context = {
        "error_class": "ValueError",
        "locals": {
            "payload": {"ssn": "123-45-6789"},
            "token": "secret-token",
            "task": {
                "id": "task-456",
                "payload": {"email": "user@example.com"},
            },
        },
        "request_id": "req-1",
    }

    event = sanitize_exception_context(context=context)

    assert event["task_id"] == "task-456"
    assert event["error_class"] == "ValueError"
    assert event["context"]["request_id"] == "req-1"
    serialized = json.dumps(event)
    assert "locals" not in serialized.lower()
    assert "payload" not in serialized.lower()
    assert "123-45-6789" not in serialized
    assert "user@example.com" not in serialized
    assert "secret-token" not in serialized


def test_exception_tracker_lookup_uses_safe_keys():
    tracker = ExceptionTracker()

    tracker.capture(
        RuntimeError("secret payload"),
        context={"task_id": "task-789", "operation": "dispatch"},
    )

    by_task = tracker.lookup(task_id="task-789")
    by_error = tracker.lookup(error_class="RuntimeError")
    assert len(by_task) == 1
    assert len(by_error) == 1
    assert by_task[0]["task_id"] == "task-789"
    assert by_error[0]["error_class"] == "RuntimeError"


def test_structured_formatter_does_not_emit_raw_exception_message():
    formatter = StructuredFormatter()
    try:
        raise RuntimeError("payload leaked secret-value")
    except RuntimeError as error:
        record = logging.LogRecord(
            "test",
            logging.ERROR,
            __file__,
            1,
            "task failed with payload secret-value",
            (),
            (type(error), error, error.__traceback__),
        )

    formatted = formatter.format(record)

    assert "RuntimeError" in formatted
    assert "secret-value" not in formatted
    assert "payload" not in formatted.lower()


def test_orchestration_engine_records_sanitized_failure_event():
    engine = OrchestrationEngine()
    agent_id = engine.registry.register("worker", "worker.processor")
    task = {
        "id": "task-engine-1",
        "target_agent": agent_id,
        "payload": {"customer": "Bob", "secret": "engine-token"},
    }
    hook_events = []

    def fail_with_payload(agent, task):
        raise RuntimeError("failed while processing Bob engine-token")

    async def on_error(event):
        hook_events.append(event)

    engine._execute_in_thread = fail_with_payload
    engine.register_hook("on_error", on_error)

    asyncio.run(engine._execute_task(task))

    events = engine.exception_events()
    assert len(events) == 1
    assert hook_events == events
    assert events[0]["task_id"] == "task-engine-1"
    assert events[0]["error_class"] == "RuntimeError"
    serialized = json.dumps(events[0])
    assert "payload" not in serialized.lower()
    assert "Bob" not in serialized
    assert "engine-token" not in serialized
