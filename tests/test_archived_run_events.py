import asyncio

from src.agent import AgentStatus
from src.common.metrics import MetricsCollector
from src.orchestrator.engine import OrchestrationEngine


def test_archived_run_message_rejected_before_hooks_or_agent_state_changes():
    metrics = MetricsCollector()
    engine = OrchestrationEngine(metrics_collector=metrics)
    agent_id = engine.registry.register("worker", "worker.processor")
    pre_execute_calls = []
    error_calls = []

    async def pre_execute(task):
        pre_execute_calls.append(task["id"])

    async def on_error(task, error):
        error_calls.append((task["id"], error))

    engine.register_hook("pre_execute", pre_execute)
    engine.register_hook("on_error", on_error)
    engine.register_run(
        "run-private-1",
        attempt_id="attempt-current",
        revision=4,
    )
    engine.archive_run("run-private-1", revision=5)

    task = {
        "id": "task-private-1",
        "target_agent": agent_id,
        "type": "worker.message",
        "run_id": "run-private-1",
        "run_attempt_id": "attempt-current",
        "run_revision": 5,
    }
    engine.scheduler._in_flight[task["id"]] = task

    asyncio.run(engine._execute_task(task))

    assert pre_execute_calls == []
    assert error_calls == []
    assert engine.registry.get(agent_id)["status"] == AgentStatus.PENDING.value
    assert task["id"] not in engine.scheduler._in_flight

    audit = engine.audit_records()
    assert audit[-1]["reason"] == "archived_run"
    assert audit[-1]["lifecycle_state"] == "archived"
    assert audit[-1]["run_ref"] != "run-private-1"
    assert audit[-1]["task_ref"] != "task-private-1"
    assert audit[-1]["attempt_ref"] != "attempt-current"

    snapshot = metrics.snapshot()
    assert (
        snapshot["counters"]["orchestrator.run_events.rejected.archived_run"]
        == 1
    )


def test_stale_attempt_or_revision_rejected_with_sanitized_audit():
    metrics = MetricsCollector()
    engine = OrchestrationEngine(metrics_collector=metrics)
    agent_id = engine.registry.register("worker", "worker.processor")
    engine.register_run(
        "run-private-2",
        attempt_id="attempt-current",
        revision=7,
    )

    task = {
        "id": "task-private-2",
        "target_agent": agent_id,
        "type": "worker.message",
        "run_id": "run-private-2",
        "run_attempt_id": "attempt-old",
        "run_revision": 6,
    }
    engine.scheduler._in_flight[task["id"]] = task

    asyncio.run(engine._execute_task(task))

    assert engine.registry.get(agent_id)["status"] == AgentStatus.PENDING.value
    assert task["id"] not in engine.scheduler._in_flight

    audit = engine.audit_records()
    assert audit[-1]["reason"] == "stale_attempt"
    assert audit[-1]["reasons"] == ("stale_attempt", "stale_revision")
    assert audit[-1]["run_ref"] != "run-private-2"
    assert audit[-1]["task_ref"] != "task-private-2"
    assert audit[-1]["attempt_ref"] != "attempt-old"
    assert audit[-1]["current_attempt_ref"] != "attempt-current"

    snapshot = metrics.snapshot()
    assert (
        snapshot["counters"]["orchestrator.run_events.rejected.stale_attempt"]
        == 1
    )


def test_missing_revision_for_tracked_run_is_rejected_before_execution():
    metrics = MetricsCollector()
    engine = OrchestrationEngine(metrics_collector=metrics)
    agent_id = engine.registry.register("worker", "worker.processor")
    engine.register_run(
        "run-private-3",
        attempt_id="attempt-current",
        revision=1,
    )

    task = {
        "id": "task-private-3",
        "target_agent": agent_id,
        "type": "worker.message",
        "run_id": "run-private-3",
        "run_attempt_id": "attempt-current",
    }
    engine.scheduler._in_flight[task["id"]] = task

    asyncio.run(engine._execute_task(task))

    assert engine.registry.get(agent_id)["status"] == AgentStatus.PENDING.value
    assert engine.audit_records()[-1]["reason"] == "missing_revision"
    assert task["id"] not in engine.scheduler._in_flight


def test_matching_run_metadata_still_allows_execution():
    metrics = MetricsCollector()
    engine = OrchestrationEngine(metrics_collector=metrics)
    agent_id = engine.registry.register("worker", "worker.processor")
    pre_execute_calls = []
    post_execute_calls = []
    engine.register_run(
        "run-private-4",
        attempt_id="attempt-current",
        revision=3,
    )

    async def pre_execute(task):
        pre_execute_calls.append(task["id"])

    async def post_execute(task, result):
        post_execute_calls.append((task["id"], result["status"]))

    engine.register_hook("pre_execute", pre_execute)
    engine.register_hook("post_execute", post_execute)
    task = {
        "id": "task-private-4",
        "target_agent": agent_id,
        "type": "worker.message",
        "run_id": "run-private-4",
        "run_attempt_id": "attempt-current",
        "run_revision": 3,
    }

    asyncio.run(engine._execute_task(task))

    assert pre_execute_calls == ["task-private-4"]
    assert post_execute_calls == [("task-private-4", "completed")]
    assert engine.registry.get(agent_id)["status"] == AgentStatus.PAUSED.value
    assert engine.audit_records() == []
