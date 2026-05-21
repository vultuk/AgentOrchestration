import asyncio
import signal

from src.agent.executor import AgentExecutor
from src.agent.runtime import AgentRuntime, RuntimeState


class FakeProcess:
    def __init__(self, on_signal=None):
        self.pid = 4321
        self.returncode = None
        self.signals = []
        self._on_signal = on_signal

    def poll(self):
        return self.returncode

    def send_signal(self, sig):
        if self._on_signal:
            self._on_signal()
        self.signals.append(sig)

    def wait(self, timeout=None):
        self.returncode = 0
        return self.returncode

    def kill(self):
        self.returncode = -9


def test_runtime_records_shutdown_reason_before_signal():
    runtime = AgentRuntime()
    observed_before_signal = []

    def capture_terminal_outcome():
        observed_before_signal.append(runtime.get_terminal_outcome("agent-1"))

    proc = FakeProcess(on_signal=capture_terminal_outcome)
    runtime._processes["agent-1"] = proc
    runtime._states["agent-1"] = RuntimeState.RUNNING

    assert runtime.stop("agent-1", reason="worker_shutdown_failure")

    assert proc.signals == [signal.SIGTERM]
    assert observed_before_signal[0]["reason"] == "worker_shutdown_failure"
    assert observed_before_signal[0]["state"] == RuntimeState.STOPPED.value
    assert runtime.get_state("agent-1") is RuntimeState.STOPPED


def test_runtime_preserves_first_terminal_outcome_after_polling():
    runtime = AgentRuntime()
    proc = FakeProcess()
    runtime._processes["agent-1"] = proc
    runtime._states["agent-1"] = RuntimeState.RUNNING

    assert runtime.stop("agent-1", reason="worker_shutdown_failure")
    proc.returncode = 1

    assert runtime.get_state("agent-1") is RuntimeState.STOPPED
    outcome = runtime.get_terminal_outcome("agent-1")
    assert outcome["reason"] == "worker_shutdown_failure"
    assert outcome["state"] == RuntimeState.STOPPED.value


def test_runtime_records_unexpected_process_exit_as_terminal_failure():
    runtime = AgentRuntime()
    proc = FakeProcess()
    proc.returncode = 7
    runtime._processes["agent-1"] = proc
    runtime._states["agent-1"] = RuntimeState.RUNNING

    assert runtime.get_state("agent-1") is RuntimeState.CRASHED
    outcome = runtime.get_terminal_outcome("agent-1")
    assert outcome["state"] == RuntimeState.CRASHED.value
    assert outcome["reason"] == "process_exited_before_shutdown: 7"
    assert outcome["returncode"] == 7


def test_executor_cancel_records_terminal_outcome_before_task_stops():
    async def scenario():
        executor = AgentExecutor()
        started = asyncio.Event()
        observed_on_cancel = []
        execution_id = None

        async def handler(agent_id, task):
            started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                observed_on_cancel.append(
                    executor.get_terminal_outcome(execution_id)
                )
                raise

        runner = asyncio.create_task(
            executor.execute("agent-1", {"id": "task-1"}, handler)
        )
        await started.wait()
        execution_id = executor.active_execution_ids()[0]

        assert executor.cancel(execution_id)
        assert await runner == execution_id

        assert observed_on_cancel[0]["reason"] == "execution_cancelled"
        outcome = executor.get_result(execution_id)
        assert outcome["reason"] == "execution_cancelled"
        assert executor.active_execution_ids() == []

    asyncio.run(scenario())


def test_executor_cancelled_handler_cannot_overwrite_terminal_outcome():
    async def scenario():
        executor = AgentExecutor()
        started = asyncio.Event()

        async def handler(agent_id, task):
            started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                return {"ignored": "cancel"}

        runner = asyncio.create_task(
            executor.execute("agent-1", {"id": "task-1"}, handler)
        )
        await started.wait()
        execution_id = executor.active_execution_ids()[0]

        assert executor.cancel(execution_id)
        assert await runner == execution_id

        result = executor.get_result(execution_id)
        assert result["state"] == "cancelled"
        assert result["reason"] == "execution_cancelled"
        assert "ignored" not in result

    asyncio.run(scenario())


def test_executor_records_completed_outcome_without_replacing_result():
    async def scenario():
        executor = AgentExecutor()

        async def handler(agent_id, task):
            return {"ok": True}

        execution_id = await executor.execute(
            "agent-1",
            {"id": "task-1"},
            handler,
        )

        assert executor.get_result(execution_id)["result"] == {"ok": True}
        outcome = executor.get_terminal_outcome(execution_id)
        assert outcome["state"] == "completed"
        assert outcome["reason"] == "execution_completed"

    asyncio.run(scenario())


def test_executor_shutdown_records_reason_before_cancelling_tasks():
    async def scenario():
        executor = AgentExecutor()
        started = asyncio.Event()
        observed_on_cancel = []
        execution_id = None

        async def handler(agent_id, task):
            started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                observed_on_cancel.append(
                    executor.get_terminal_outcome(execution_id)
                )
                raise

        runner = asyncio.create_task(
            executor.execute("agent-1", {"id": "task-1"}, handler)
        )
        await started.wait()
        execution_id = executor.active_execution_ids()[0]

        await executor.shutdown()
        assert await runner == execution_id

        assert observed_on_cancel[0]["reason"] == "executor_shutdown"
        outcome = executor.get_result(execution_id)
        assert outcome["reason"] == "executor_shutdown"
        assert executor.active_execution_ids() == []

    asyncio.run(scenario())
