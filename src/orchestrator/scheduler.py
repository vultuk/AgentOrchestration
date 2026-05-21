"""Task Scheduler — Priority-based task queuing and dispatch."""

import heapq
import time
from typing import Any, Dict, List, Optional
from uuid import uuid4


class PriorityQueue:
    def __init__(self):
        self._queue = []
        self._counter = 0

    def push(self, item: Any, priority: int = 0) -> None:
        heapq.heappush(self._queue, (-priority, self._counter, item))
        self._counter += 1

    def pop(self) -> Optional[Any]:
        if self._queue:
            return heapq.heappop(self._queue)[2]
        return None

    def peek(self) -> Optional[Any]:
        if self._queue:
            return self._queue[0][2]
        return None

    def __len__(self) -> int:
        return len(self._queue)

    def items(self) -> List[Any]:
        return [entry[2] for entry in self._queue]


class TaskScheduler:
    def __init__(self, max_concurrent_per_tenant: Optional[int] = None):
        if (
            max_concurrent_per_tenant is not None
            and max_concurrent_per_tenant < 1
        ):
            raise ValueError(
                "max_concurrent_per_tenant must be greater than zero"
            )

        self._queues: Dict[str, PriorityQueue] = {}
        self._scheduled: Dict[str, Dict[str, Any]] = {}
        self._in_flight: Dict[str, Dict[str, Any]] = {}
        self._deferred_recovery: Dict[str, Dict[str, Any]] = {}
        self._audit_log: List[Dict[str, Any]] = []
        self._max_retries = 3
        self._max_concurrent_per_tenant = max_concurrent_per_tenant

    def enqueue(
        self,
        task: Dict,
        queue: str = "default",
        priority: int = 0,
    ) -> str:
        task_id = str(task.get("id") or uuid4())
        task["id"] = task_id
        task["enqueued_at"] = time.time()
        task["priority"] = priority
        task["retries"] = task.get("retries", 0)

        if queue not in self._queues:
            self._queues[queue] = PriorityQueue()
        self._queues[queue].push(task, priority)
        return task_id

    def schedule(
        self,
        task: Dict,
        delay: float,
        queue: str = "default",
        priority: int = 0,
    ) -> str:
        task_id = str(task.get("id") or uuid4())
        task["id"] = task_id
        task["retries"] = task.get("retries", 0)
        self._scheduled[task_id] = {
            "task": task,
            "run_at": time.time() + delay,
            "queue": queue,
            "priority": priority,
        }
        return task_id

    async def dequeue(
        self,
        queue: str = "default",
        timeout: float = 1.0,
    ) -> Optional[Dict]:
        now = time.time()
        expired = [
            tid
            for tid, record in self._scheduled.items()
            if record["run_at"] <= now
        ]
        for tid in expired:
            record = self._scheduled.pop(tid)
            self.enqueue(
                record["task"],
                record.get("queue", queue),
                record.get("priority", 0),
            )

        if queue in self._queues and len(self._queues[queue]) > 0:
            deferred_tasks = []
            while len(self._queues[queue]) > 0:
                task = self._queues[queue].pop()
                if task and self._can_dispatch(task):
                    self._in_flight[task["id"]] = task
                    for deferred_task in deferred_tasks:
                        self._queues[queue].push(
                            deferred_task,
                            deferred_task.get("priority", 0),
                        )
                    return task

                if task:
                    deferred_tasks.append(task)
                    self._record_audit(
                        task,
                        decision="deferred",
                        reason="tenant_concurrency_limit",
                        source="dequeue",
                        queue=queue,
                    )

            for deferred_task in deferred_tasks:
                self._queues[queue].push(
                    deferred_task,
                    deferred_task.get("priority", 0),
                )
        return None

    def complete(self, task_id: str) -> bool:
        completed = self._in_flight.pop(task_id, None) is not None
        if completed:
            self.release_deferred_recovery()
        return completed

    def fail(self, task_id: str, queue: str = "default") -> bool:
        task = self._in_flight.pop(task_id, None)
        if task:
            task["retries"] = task.get("retries", 0) + 1
            if task["retries"] < self._max_retries:
                self.enqueue(task, queue, priority=task.get("priority", 0))
                self.release_deferred_recovery(queue)
                return True
            self.release_deferred_recovery(queue)
        return False

    def recover_after_restart(
        self,
        persisted_tasks: List[Dict[str, Any]],
        queue: str = "default",
        priority: int = 0,
    ) -> Dict[str, List[str]]:
        """Restore restart candidates without exceeding tenant concurrency."""
        result = {"accepted": [], "deferred": [], "rejected": []}
        reserved_by_tenant = self._tenant_counts()

        for persisted_task in persisted_tasks:
            task = dict(persisted_task)
            task_id = str(task.get("id") or uuid4())
            task["id"] = task_id

            if self._task_known(task_id):
                result["rejected"].append(task_id)
                self._record_audit(
                    task,
                    decision="rejected",
                    reason="duplicate_task",
                    source="restart_recovery",
                    queue=queue,
                )
                continue

            tenant_id = self._tenant_id(task)
            active_count = reserved_by_tenant.get(tenant_id, 0)
            if self._max_concurrent_per_tenant is not None:
                if active_count >= self._max_concurrent_per_tenant:
                    self._deferred_recovery[task_id] = {
                        "task": task,
                        "queue": queue,
                        "priority": priority,
                    }
                    result["deferred"].append(task_id)
                    self._record_audit(
                        task,
                        decision="deferred",
                        reason="tenant_concurrency_limit",
                        source="restart_recovery",
                        queue=queue,
                        active_count=active_count,
                    )
                    continue

                reserved_by_tenant[tenant_id] = active_count + 1

            task["recovered_after_restart"] = True
            task["recovered_at"] = time.time()
            self.enqueue(task, queue, priority)
            result["accepted"].append(task_id)
            self._record_audit(
                task,
                decision="queued",
                reason="restart_recovery",
                source="restart_recovery",
                queue=queue,
                active_count=active_count,
            )

        return result

    def release_deferred_recovery(self, queue: str = "default") -> List[str]:
        released = []
        reserved_by_tenant = self._tenant_counts()

        for task_id, record in list(self._deferred_recovery.items()):
            task = record["task"]
            tenant_id = self._tenant_id(task)
            active_count = reserved_by_tenant.get(tenant_id, 0)
            if self._max_concurrent_per_tenant is not None:
                if active_count >= self._max_concurrent_per_tenant:
                    continue
                reserved_by_tenant[tenant_id] = active_count + 1

            self._deferred_recovery.pop(task_id)
            self.enqueue(
                task,
                record.get("queue", queue),
                record.get("priority", 0),
            )
            released.append(task_id)
            self._record_audit(
                task,
                decision="queued",
                reason="capacity_released",
                source="restart_recovery",
                queue=record.get("queue", queue),
                active_count=active_count,
            )

        return released

    @property
    def audit_log(self) -> List[Dict[str, Any]]:
        return list(self._audit_log)

    @property
    def deferred_recovery(self) -> Dict[str, Dict[str, Any]]:
        return dict(self._deferred_recovery)

    def _tenant_id(self, task: Dict[str, Any]) -> str:
        return str(
            task.get("tenant_id")
            or task.get("tenant")
            or task.get("workspace_id")
            or "default"
        )

    def _tenant_counts(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for task in self._in_flight.values():
            tenant_id = self._tenant_id(task)
            counts[tenant_id] = counts.get(tenant_id, 0) + 1
        return counts

    def _can_dispatch(self, task: Dict[str, Any]) -> bool:
        if self._max_concurrent_per_tenant is None:
            return True

        tenant_id = self._tenant_id(task)
        return (
            self._tenant_counts().get(tenant_id, 0)
            < self._max_concurrent_per_tenant
        )

    def _task_known(self, task_id: str) -> bool:
        if task_id in self._in_flight:
            return True
        if task_id in self._scheduled:
            return True
        if task_id in self._deferred_recovery:
            return True

        for task_queue in self._queues.values():
            for task in task_queue.items():
                if isinstance(task, dict) and str(task.get("id")) == task_id:
                    return True
        return False

    def _record_audit(
        self,
        task: Dict[str, Any],
        decision: str,
        reason: str,
        source: str,
        queue: str,
        active_count: Optional[int] = None,
    ) -> None:
        tenant_id = self._tenant_id(task)
        if active_count is None:
            active_count = self._tenant_counts().get(tenant_id, 0)

        self._audit_log.append({
            "timestamp": time.time(),
            "source": source,
            "task_id": task.get("id"),
            "tenant_id": tenant_id,
            "decision": decision,
            "reason": reason,
            "active_count": active_count,
            "limit": self._max_concurrent_per_tenant,
            "queue": queue,
        })

        if len(self._audit_log) > 100:
            self._audit_log = self._audit_log[-100:]

# 2019-04-25T08:37:12 update

# 2019-06-04T16:40:00 update

# 2019-07-11T12:01:28 update

# 2019-08-02T12:20:21 update

# 2019-08-23T10:38:50 update

# 2019-10-31T13:55:52 update

# 2019-11-04T20:12:32 update

# 2019-12-13T12:22:36 update

# 2020-02-01T10:32:37 update

# 2020-02-26T09:44:38 update

# 2020-03-09T19:00:55 update

# 2020-05-01T18:40:34 update

# 2020-05-12T15:10:31 update

# 2020-06-30T13:24:19 update

# 2020-09-22T16:00:45 update

# 2020-10-20T10:52:48 update

# 2020-10-21T12:18:08 update

# 2020-11-06T12:35:01 update

# 2020-12-09T08:09:33 update

# 2021-01-07T08:20:36 update

# 2021-10-02T15:23:16 update

# 2021-10-06T16:14:57 update

# 2021-10-06T09:27:41 update

# 2021-11-19T08:37:40 update

# 2022-03-01T16:39:54 update

# 2022-05-26T13:43:07 update

# 2022-06-02T10:50:58 update

# 2022-06-14T10:46:48 update

# 2022-07-31T16:44:34 update

# 2022-08-30T18:20:12 update

# 2022-11-04T14:47:03 update

# 2022-12-06T10:36:49 update

# 2022-12-22T13:21:12 update

# 2022-12-26T12:24:50 update

# 2023-03-09T08:09:55 update

# 2023-05-01T10:07:37 update

# 2023-06-08T14:32:15 update

# 2023-07-14T17:24:18 update

# 2023-12-14T08:38:31 update

# 2024-02-20T13:43:58 update

# 2024-03-24T08:52:42 update

# 2024-03-28T15:27:17 update

# 2024-03-29T18:10:33 update

# 2024-04-15T20:18:31 update

# 2024-05-27T13:11:52 update

# 2024-05-27T16:42:56 update

# 2024-06-20T13:03:45 update

# 2024-06-28T12:32:58 update

# 2024-07-10T14:10:16 update

# 2024-07-26T14:18:59 update

# 2024-08-12T08:21:05 update

# 2024-08-21T16:58:40 update

# 2024-09-27T19:54:30 update

# 2024-10-21T13:47:42 update

# 2024-11-11T09:19:27 update

# 2024-12-24T08:23:41 update

# 2025-02-14T10:35:15 update

# 2025-03-31T18:09:40 update

# 2025-06-21T17:32:49 update

# 2025-07-21T16:52:28 update

# 2025-08-20T19:45:16 update

# 2025-11-04T18:54:24 update

# 2025-12-09T20:17:36 update

# 2026-01-12T15:42:32 update

# 2026-01-23T14:41:20 update

# 2026-03-18T14:43:07 update

# 2026-04-13T11:43:19 update
