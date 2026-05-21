import asyncio
import time

import pytest

from src.sdk.decorators import task


class TestTaskDecorator:
    def test_sync_task_returns_value(self):
        @task(name="add")
        def add(left, right):
            return left + right

        assert asyncio.run(add(2, 3)) == 5
        assert add.__task_config__["name"] == "add"

    def test_sync_task_accepts_keyword_arguments(self):
        @task()
        def greet(*, name):
            return f"hello {name}"

        assert asyncio.run(greet(name="agent")) == "hello agent"

    def test_sync_task_exceptions_point_to_handler(self):
        @task()
        def fail():
            raise ValueError("bad handler")

        with pytest.raises(ValueError, match="bad handler"):
            asyncio.run(fail())

    def test_sync_task_timeout_uses_task_name(self):
        @task(name="slow-sync", timeout=0.01)
        def slow():
            time.sleep(0.05)
            return "late"

        with pytest.raises(TimeoutError, match="slow-sync"):
            asyncio.run(slow())

    def test_async_task_keeps_existing_await_path(self):
        @task(timeout=1)
        async def async_add(left, right):
            await asyncio.sleep(0)
            return left + right

        assert asyncio.run(async_add(4, 6)) == 10
