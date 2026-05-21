import sys

from src.agent.runtime import AgentRuntime, RuntimeState


def test_runtime_drains_large_stdout_and_stderr_without_pipe_deadlock():
    runtime = AgentRuntime()
    code = (
        "import os\n"
        "chunk = b'x' * 65536\n"
        "for _ in range(128):\n"
        "    os.write(1, chunk)\n"
        "for _ in range(128):\n"
        "    os.write(2, chunk)\n"
    )

    assert runtime.start("noisy-agent", [sys.executable, "-c", code])
    proc = runtime._processes["noisy-agent"]

    assert proc.wait(timeout=5) == 0
    runtime.get_state("noisy-agent")
    assert "noisy-agent" not in runtime._drain_threads


def test_stop_joins_runtime_pipe_drain_threads():
    runtime = AgentRuntime()
    code = (
        "import os, time\n"
        "os.write(1, b'x' * 131072)\n"
        "os.write(2, b'y' * 131072)\n"
        "time.sleep(30)\n"
    )

    assert runtime.start("long-noisy-agent", [sys.executable, "-c", code])
    assert runtime._drain_threads["long-noisy-agent"]

    assert runtime.stop("long-noisy-agent", timeout=1)

    assert runtime.get_state("long-noisy-agent") == RuntimeState.STOPPED
    assert "long-noisy-agent" not in runtime._drain_threads
