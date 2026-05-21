import asyncio

import pytest

from src.agent.executor import AgentExecutor


async def _wait_for_execution_id(
    executor: AgentExecutor,
    execute_task: asyncio.Task,
) -> str:
    for _ in range(100):
        execution_id = executor.execution_id_for(execute_task)
        if execution_id is not None:
            return execution_id
        await asyncio.sleep(0.01)
    raise AssertionError("execution did not become active")


def test_successful_execution_stores_result_context():
    async def scenario():
        executor = AgentExecutor()

        async def handler(agent_id, task):
            return {"processed": True, "agent": agent_id, "task": task["id"]}

        execution_id = await executor.execute(
            "agent-success",
            {"id": "task-success"},
            handler,
        )

        result = executor.get_result(execution_id)
        assert result["execution_id"] == execution_id
        assert result["agent_id"] == "agent-success"
        assert result["task_id"] == "task-success"
        assert result["result"] == {
            "processed": True,
            "agent": "agent-success",
            "task": "task-success",
        }
        assert executor.cancel(execution_id) is False

    asyncio.run(scenario())


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
        execution_id = await _wait_for_execution_id(executor, execute_task)

        assert executor.cancel(execution_id)
        returned_id = await asyncio.wait_for(execute_task, timeout=1)

        result = executor.get_result(execution_id)
        assert returned_id == execution_id
        assert result["status"] == "cancelled"
        assert result["cancelled"] is True
        assert result["result"] is None
        assert result["error"] == "cancelled"
        assert result["execution_id"] == execution_id
        assert result["agent_id"] == "agent-1"
        assert result["task_id"] == "task-1"
        assert result["duration"] >= 0
        assert result["cancelled_at"] >= result["timestamp"] - 1
        assert executor.execution_id_for(execute_task) is None
        assert executor.cancel(execution_id) is False

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
        execution_id = await _wait_for_execution_id(executor, execute_task)

        execute_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await execute_task

        result = executor.get_result(execution_id)
        assert result["status"] == "cancelled"
        assert result["execution_id"] == execution_id
        assert result["agent_id"] == "agent-2"
        assert result["task_id"] == "task-2"
        assert result["cancelled"] is True
        assert executor.execution_id_for(execute_task) is None

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
        execution_id = await _wait_for_execution_id(executor, execute_task)

        await executor.shutdown()
        returned_id = await asyncio.wait_for(execute_task, timeout=1)

        result = executor.get_result(execution_id)
        assert returned_id == execution_id
        assert result["status"] == "cancelled"
        assert result["execution_id"] == execution_id
        assert result["agent_id"] == "agent-3"
        assert result["task_id"] == "task-3"
        assert result["cancelled"] is True

    asyncio.run(scenario())


def test_handler_exception_stores_error_result():
    async def scenario():
        executor = AgentExecutor()

        async def handler(agent_id, task):
            raise RuntimeError("boom")

        execution_id = await executor.execute(
            "agent-error",
            {"id": "task-error"},
            handler,
        )

        assert executor.get_result(execution_id) == {"error": "boom"}
        assert executor.cancel(execution_id) is False

    asyncio.run(scenario())


def test_unknown_execution_lookup_and_cancel_are_empty():
    executor = AgentExecutor()

    assert executor.get_result("missing") is None
    assert executor.cancel("missing") is False
