import asyncio

import pytest

from src.agent.executor import AgentExecutor


class TestAgentExecutor:
    @pytest.mark.parametrize("max_concurrent", [0, -1, -10])
    def test_rejects_non_positive_max_concurrent(self, max_concurrent):
        with pytest.raises(ValueError, match="positive integer"):
            AgentExecutor(max_concurrent=max_concurrent)

    @pytest.mark.parametrize("max_concurrent", [True, False, 1.5, "2", None])
    def test_rejects_non_integer_max_concurrent(self, max_concurrent):
        with pytest.raises(ValueError, match="positive integer"):
            AgentExecutor(max_concurrent=max_concurrent)

    def test_default_max_concurrent_remains_valid(self):
        executor = AgentExecutor()

        assert executor.max_concurrent == 5

    def test_positive_max_concurrent_executes_task(self):
        async def handler(agent_id, task):
            return {"agent_id": agent_id, "task_id": task["id"]}

        async def run_execution():
            executor = AgentExecutor(max_concurrent=1)
            execution_id = await executor.execute(
                "agent-1",
                {"id": "task-1"},
                handler,
            )
            return executor.get_result(execution_id)

        result = asyncio.run(run_execution())

        assert result["agent_id"] == "agent-1"
        assert result["task_id"] == "task-1"
        assert result["result"] == {
            "agent_id": "agent-1",
            "task_id": "task-1",
        }
