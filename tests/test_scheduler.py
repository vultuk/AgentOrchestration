import asyncio

from src.orchestrator.scheduler import TaskScheduler, TaskState


class TestTaskScheduler:
    def setup_method(self):
        self.scheduler = TaskScheduler()

    def test_enqueue_task(self):
        task_id = self.scheduler.enqueue({"type": "test", "payload": {}})
        assert task_id is not None

    def test_dequeue_task(self):
        self.scheduler.enqueue({"type": "test", "payload": {"data": 1}})
        task = asyncio.run(self.scheduler.dequeue())
        assert task is not None
        assert task["type"] == "test"

    def test_enqueue_multiple_priorities(self):
        self.scheduler.enqueue({"type": "low"}, priority=1)
        self.scheduler.enqueue({"type": "high"}, priority=10)
        task = asyncio.run(self.scheduler.dequeue())
        assert task["type"] == "high"

    def test_complete_task(self):
        self.scheduler.enqueue({"type": "test"})
        task = asyncio.run(self.scheduler.dequeue())
        assert self.scheduler.complete(task["id"])
        assert self.scheduler.get_state(task["id"]) == TaskState.COMPLETED

    def test_fail_task_with_retry(self):
        self.scheduler.enqueue({"type": "test"})
        task = asyncio.run(self.scheduler.dequeue())
        assert self.scheduler.fail(task["id"])
        assert self.scheduler.get_state(task["id"]) == TaskState.SCHEDULED

    def test_scheduled_task_preserves_identity_when_ready(self):
        now = [100.0]
        scheduler = TaskScheduler(clock=lambda: now[0])

        task_id = scheduler.schedule({"type": "delayed"}, delay=5)
        assert asyncio.run(scheduler.dequeue()) is None

        now[0] = 105.1
        task = asyncio.run(scheduler.dequeue())

        assert task["id"] == task_id
        assert task["type"] == "delayed"
        assert scheduler.get_state(task_id) == TaskState.IN_FLIGHT

    def test_duplicate_ack_retry_is_deferred_without_state_change(self):
        now = [200.0]
        scheduler = TaskScheduler(
            max_retries=3,
            retry_delay=10,
            clock=lambda: now[0],
        )

        task_id = scheduler.enqueue({
            "type": "ack-retry",
            "payload": {"token": "secret-value"},
        })
        task = asyncio.run(scheduler.dequeue())
        assert task["id"] == task_id

        assert scheduler.fail(task_id, reason="worker_error")
        assert scheduler.get_state(task_id) == TaskState.SCHEDULED
        assert scheduler.fail(task_id, reason="worker_error")

        scheduled = scheduler._scheduled[task_id]
        assert scheduled["retries"] == 1
        assert scheduled["attempt"] == 1
        assert scheduler.get_state(task_id) == TaskState.SCHEDULED

        audits = scheduler.audit_records()
        assert audits[-1]["event"] == "ack_retry_deferred"
        assert all("secret-value" not in str(record) for record in audits)

        now[0] = scheduled["scheduled_for"] + 0.1
        retry = asyncio.run(scheduler.dequeue())
        assert retry["id"] == task_id
        assert retry["retries"] == 1

    def test_dead_letter_write_is_idempotent_and_sanitized(self):
        scheduler = TaskScheduler(max_retries=1)

        task_id = scheduler.enqueue({
            "type": "billing",
            "payload": {"token": "do-not-log"},
        })
        task = asyncio.run(scheduler.dequeue())

        assert scheduler.fail(
            task["id"],
            reason="worker_error token=do-not-log",
        )
        assert scheduler.get_state(task_id) == TaskState.DEAD_LETTERED
        assert scheduler.fail(task_id, reason="worker_error token=do-not-log")

        dead_letters = scheduler.dead_letters()
        assert len(dead_letters) == 1
        assert dead_letters[0]["type"] == "billing"
        assert dead_letters[0]["retries"] == 1
        assert dead_letters[0]["reason"] == "worker_error"
        assert task_id not in str(dead_letters)
        assert "do-not-log" not in str(dead_letters)

        audits = scheduler.audit_records()
        assert [record["event"] for record in audits[-2:]] == [
            "dead_letter_written",
            "dead_letter_duplicate",
        ]
        assert task_id not in str(audits)
        assert "do-not-log" not in str(audits)

# 2019-01-09T19:07:03 update

# 2019-02-18T12:30:02 update

# 2019-04-11T16:04:51 update

# 2019-04-17T16:25:46 update

# 2019-05-24T19:32:13 update

# 2019-07-02T12:54:25 update

# 2019-07-03T20:37:00 update

# 2019-08-21T19:37:17 update

# 2019-10-18T10:30:31 update

# 2019-10-25T09:01:38 update

# 2019-10-29T12:59:34 update

# 2019-11-05T10:07:06 update

# 2019-11-11T10:43:52 update

# 2020-01-17T13:40:02 update

# 2020-02-07T14:06:34 update

# 2020-04-03T08:53:40 update

# 2020-04-06T19:36:29 update

# 2020-05-12T11:51:05 update

# 2020-08-17T08:37:15 update

# 2020-09-15T10:39:38 update

# 2020-10-06T11:26:19 update

# 2020-10-21T13:32:43 update

# 2020-12-14T18:18:36 update

# 2020-12-23T17:15:03 update

# 2021-01-25T16:29:00 update

# 2021-02-23T11:23:50 update

# 2021-03-19T12:21:19 update

# 2021-07-29T18:48:25 update

# 2021-08-25T12:46:58 update

# 2021-09-09T16:27:13 update

# 2021-12-16T12:05:30 update

# 2022-05-07T14:05:12 update

# 2022-07-18T20:52:29 update

# 2022-07-31T18:42:26 update

# 2022-09-09T13:10:08 update

# 2023-01-04T15:16:57 update

# 2023-01-17T14:49:04 update

# 2023-02-15T13:51:30 update

# 2023-03-08T09:15:53 update

# 2023-03-23T16:32:20 update

# 2023-03-28T09:32:01 update

# 2023-05-05T17:28:22 update

# 2023-06-01T08:13:52 update

# 2023-06-20T09:58:10 update

# 2023-07-04T16:14:34 update

# 2023-07-17T20:49:40 update

# 2023-12-26T11:49:18 update

# 2024-05-27T11:00:06 update

# 2024-07-04T08:53:03 update

# 2024-07-18T16:19:02 update

# 2024-08-07T09:35:35 update

# 2024-08-22T14:32:14 update

# 2025-05-20T14:19:23 update

# 2025-07-17T17:54:48 update

# 2025-07-28T13:06:30 update

# 2025-12-22T19:05:25 update

# 2026-01-08T18:43:02 update

# 2026-01-12T16:53:28 update

# 2026-04-16T16:58:23 update
