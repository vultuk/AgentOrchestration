from src.orchestrator.scheduler import TaskScheduler


class TestTaskScheduler:
    def setup_method(self):
        self.scheduler = TaskScheduler()

    def test_enqueue_task(self):
        task_id = self.scheduler.enqueue({"type": "test", "payload": {}})
        assert task_id is not None

    def test_dequeue_task(self):
        self.scheduler.enqueue({"type": "test", "payload": {"data": 1}})
        import asyncio
        task = asyncio.run(self.scheduler.dequeue())
        assert task is not None
        assert task["type"] == "test"

    def test_enqueue_multiple_priorities(self):
        self.scheduler.enqueue({"type": "low"}, priority=1)
        self.scheduler.enqueue({"type": "high"}, priority=10)
        import asyncio
        task = asyncio.run(self.scheduler.dequeue())
        assert task["type"] == "high"

    def test_complete_task(self):
        self.scheduler.enqueue({"type": "test"})
        import asyncio
        task = asyncio.run(self.scheduler.dequeue())
        assert self.scheduler.complete(task["id"])

    def test_fail_task_with_retry(self):
        self.scheduler.enqueue({"type": "test"})
        import asyncio
        task = asyncio.run(self.scheduler.dequeue())
        assert self.scheduler.fail(task["id"])

    def test_recovery_defers_over_capacity_tenant_after_restart(self):
        import asyncio

        scheduler = TaskScheduler(max_concurrent_per_tenant=1)
        first_task = {
            "id": "task-a",
            "tenant_id": "tenant-1",
            "state": "running",
            "payload": {"private": "not-for-audit"},
        }
        second_task = {
            "id": "task-b",
            "tenant_id": "tenant-1",
            "state": "running",
            "payload": {"private": "not-for-audit"},
        }

        result = scheduler.recover_after_restart([first_task, second_task])

        assert result == {
            "accepted": ["task-a"],
            "deferred": ["task-b"],
            "rejected": [],
        }
        recovered = asyncio.run(scheduler.dequeue())
        assert recovered["id"] == "task-a"
        assert recovered["state"] == "running"
        assert asyncio.run(scheduler.dequeue()) is None
        assert "task-b" in scheduler.deferred_recovery
        deferred_record = scheduler.deferred_recovery["task-b"]
        assert deferred_record["task"]["state"] == "running"
        assert deferred_record["task"]["recovery_state"] == "deferred"
        assert (
            deferred_record["task"]["deferred_reason"]
            == "tenant_concurrency_limit"
        )

        deferred_audit = [
            entry for entry in scheduler.audit_log
            if entry["task_id"] == "task-b"
        ][-1]
        assert deferred_audit["decision"] == "deferred"
        assert deferred_audit["reason"] == "tenant_concurrency_limit"
        assert deferred_audit["tenant_id"] == "tenant-1"
        assert "payload" not in deferred_audit

        assert scheduler.complete("task-a")
        assert "task-b" not in scheduler.deferred_recovery
        released = asyncio.run(scheduler.dequeue())
        assert released["id"] == "task-b"
        assert released["state"] == "running"
        assert released["recovered_after_restart"] is True
        assert released["recovery_state"] == "queued"
        assert "deferred_reason" not in released

    def test_recovery_rejects_duplicate_restart_task(self):
        scheduler = TaskScheduler(max_concurrent_per_tenant=1)
        task_id = scheduler.enqueue({
            "id": "already-queued",
            "tenant_id": "tenant-1",
        })

        result = scheduler.recover_after_restart([
            {"id": task_id, "tenant_id": "tenant-1", "state": "running"}
        ])

        assert result == {
            "accepted": [],
            "deferred": [],
            "rejected": [task_id],
        }
        rejection = [
            entry for entry in scheduler.audit_log
            if entry["task_id"] == task_id
        ][-1]
        assert rejection["decision"] == "rejected"
        assert rejection["reason"] == "duplicate_task"

    def test_recovery_counts_existing_in_flight_capacity(self):
        import asyncio

        scheduler = TaskScheduler(max_concurrent_per_tenant=2)
        scheduler.enqueue({"id": "active-a", "tenant_id": "tenant-1"})
        active = asyncio.run(scheduler.dequeue())
        assert active["id"] == "active-a"

        result = scheduler.recover_after_restart([
            {"id": "task-b", "tenant_id": "tenant-1", "state": "running"},
            {"id": "task-c", "tenant_id": "tenant-1", "state": "running"},
        ])

        assert result == {
            "accepted": ["task-b"],
            "deferred": ["task-c"],
            "rejected": [],
        }
        recovered = asyncio.run(scheduler.dequeue())
        assert recovered["id"] == "task-b"
        assert asyncio.run(scheduler.dequeue()) is None
        assert "task-c" in scheduler.deferred_recovery

    def test_dequeue_skips_blocked_tenant_and_preserves_priority(self):
        import asyncio

        scheduler = TaskScheduler(max_concurrent_per_tenant=1)
        scheduler.enqueue({
            "id": "active-a",
            "tenant_id": "tenant-a",
        })
        active = asyncio.run(scheduler.dequeue())
        assert active["id"] == "active-a"

        scheduler.enqueue({
            "id": "blocked-high",
            "tenant_id": "tenant-a",
        }, priority=10)
        scheduler.enqueue({
            "id": "eligible-low",
            "tenant_id": "tenant-b",
        }, priority=1)

        eligible = asyncio.run(scheduler.dequeue())
        assert eligible["id"] == "eligible-low"

        deferred_audit = [
            entry for entry in scheduler.audit_log
            if entry["task_id"] == "blocked-high"
        ][-1]
        assert deferred_audit["source"] == "queued_dispatch"
        assert deferred_audit["decision"] == "deferred"

        assert scheduler.complete("active-a")
        blocked = asyncio.run(scheduler.dequeue())
        assert blocked["id"] == "blocked-high"

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
