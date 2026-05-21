import asyncio

import pytest

from src.agent import AgentStatus
from src.orchestrator.engine import OrchestrationEngine, PluginManifestError


def test_invalid_manifest_fails_closed_before_registering_hooks():
    engine = OrchestrationEngine()

    async def hook(*args):
        return None

    before = {event: list(hooks) for event, hooks in engine._hooks.items()}

    with pytest.raises(PluginManifestError):
        engine.load_plugin_manifest(
            {
                "name": "bad-plugin",
                "hooks": [{"event": "before_anything", "callback": hook}],
            }
        )

    assert engine._hooks == before


def test_valid_manifest_registers_hooks_atomically():
    engine = OrchestrationEngine()
    calls = []

    async def pre_execute(task):
        calls.append(task["id"])

    engine.load_plugin_manifest(
        {
            "name": "audit-plugin",
            "hooks": [{"event": "pre_execute", "callback": pre_execute}],
        }
    )

    assert engine._hooks["pre_execute"] == [pre_execute]


def test_invalid_task_manifest_records_failure_without_hook_side_effects():
    engine = OrchestrationEngine()
    agent_id = engine.registry.register("plugin-worker", "worker.plugin")
    hook_calls = []

    async def pre_execute(task):
        hook_calls.append(task["id"])

    engine.register_hook("pre_execute", pre_execute)
    task_id = engine.scheduler.enqueue(
        {
            "type": "plugin-load",
            "target_agent": agent_id,
            "plugin_manifest": {
                "name": "bad-plugin",
                "hooks": [{"event": "unknown", "callback": pre_execute}],
            },
        }
    )
    task = asyncio.run(engine.scheduler.dequeue())

    asyncio.run(engine._execute_task(task))

    outcome = engine.get_task_outcome(task_id)
    assert outcome["status"] == "failed"
    assert outcome["error_type"] == "PluginManifestError"
    assert hook_calls == []
    assert task_id not in engine.scheduler._in_flight
    assert engine.registry.get(agent_id)["status"] == AgentStatus.FAILED.value


def test_duplicate_concurrent_execution_records_one_terminal_outcome():
    async def run_test():
        engine = OrchestrationEngine()
        agent_id = engine.registry.register("plugin-worker", "worker.plugin")
        task_id = engine.scheduler.enqueue(
            {
                "type": "plugin-load",
                "target_agent": agent_id,
                "plugin_manifest": {"name": "plugin", "hooks": []},
            }
        )
        task = await engine.scheduler.dequeue()

        await asyncio.gather(
            engine._execute_task(task),
            engine._execute_task(task),
        )

        outcome = engine.get_task_outcome(task_id)
        assert outcome["status"] == "completed"
        assert task_id not in engine.scheduler._in_flight
        assert (
            engine.registry.get(agent_id)["status"]
            == AgentStatus.PAUSED.value
        )

    asyncio.run(run_test())
