import asyncio

import pytest

from src.agent.executor import AgentExecutor


async def _wait_for_active_execution(executor: AgentExecutor) -> str:
    for _ in range(100):
        if executor._active_tasks:
            return next(iter(executor._active_tasks))
        await asyncio.sleep(0.01)
    raise AssertionError("execution did not become active")


def test_cancelled_execution_stores_terminal_result():
    async def scenario():
        executor = AgentExecutor()
        started = asyncio.Event()

        async def handler(agent_id, task):
            started.set()
            await asyncio.sleep(60)

        execute_task = asyncio.create_task(
            executor.execute("agent-1", {"id": "task-1"}, handler)
        )
        await started.wait()
        execution_id = await _wait_for_active_execution(executor)

        assert executor.cancel(execution_id)
        returned_id = await asyncio.wait_for(execute_task, timeout=1)

        result = executor.get_result(execution_id)
        assert returned_id == execution_id
        assert result["status"] == "cancelled"
        assert result["result"] is None
        assert result["error"] == "cancelled"
        assert result["execution_id"] == execution_id
        assert result["agent_id"] == "agent-1"
        assert result["task_id"] == "task-1"
        assert result["duration"] >= 0

    asyncio.run(scenario())


def test_outer_execution_cancellation_records_result_and_propagates():
    async def scenario():
        executor = AgentExecutor()
        started = asyncio.Event()

        async def handler(agent_id, task):
            started.set()
            await asyncio.sleep(60)

        execute_task = asyncio.create_task(
            executor.execute("agent-2", {"id": "task-2"}, handler)
        )
        await started.wait()
        execution_id = await _wait_for_active_execution(executor)

        execute_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await execute_task

        result = executor.get_result(execution_id)
        assert result["status"] == "cancelled"
        assert result["execution_id"] == execution_id
        assert result["agent_id"] == "agent-2"
        assert result["task_id"] == "task-2"

    asyncio.run(scenario())


def test_shutdown_records_cancelled_results_for_active_executions():
    async def scenario():
        executor = AgentExecutor()
        started = asyncio.Event()

        async def handler(agent_id, task):
            started.set()
            await asyncio.sleep(60)

        execute_task = asyncio.create_task(
            executor.execute("agent-3", {"id": "task-3"}, handler)
        )
        await started.wait()
        execution_id = await _wait_for_active_execution(executor)

        await executor.shutdown()
        returned_id = await asyncio.wait_for(execute_task, timeout=1)

        result = executor.get_result(execution_id)
        assert returned_id == execution_id
        assert result["status"] == "cancelled"
        assert result["execution_id"] == execution_id
        assert result["agent_id"] == "agent-3"
        assert result["task_id"] == "task-3"

    asyncio.run(scenario())
