"""Task Scheduler — Priority-based task queuing and dispatch."""

import heapq
import logging
import time
from typing import Any, Dict, List, Optional
from uuid import uuid4

from src.common.metrics import metrics
from src.orchestrator.artifact_retention import (
    ArtifactRetentionPolicyValidator,
)


logger = logging.getLogger(__name__)


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


class TaskScheduler:
    def __init__(self):
        self._queues: Dict[str, PriorityQueue] = {}
        self._scheduled: Dict[str, Dict[str, Any]] = {}
        self._in_flight: Dict[str, Dict] = {}
        self._artifact_cleanup_bindings: Dict[str, str] = {}
        self._audit_records: List[Dict[str, Any]] = []
        self._artifact_retention_validator = ArtifactRetentionPolicyValidator()
        self._max_retries = 3

    def enqueue(
        self,
        task: Dict,
        queue: str = "default",
        priority: int = 0,
    ) -> str:
        decision = self._validate_artifact_cleanup(task)
        artifact_id = decision.artifact_id if decision.allowed else None
        task_id = str(uuid4())
        task["id"] = task_id
        task["enqueued_at"] = time.time()
        task["retries"] = 0

        if artifact_id:
            self._bind_artifact_cleanup(artifact_id, task_id)
            self._audit("cleanup_queued", task_id, decision)
            metrics.increment("artifact_retention.cleanup_queued")

        self._put_queue(task, queue, priority)
        return task_id

    def _put_queue(
        self,
        task: Dict,
        queue: str = "default",
        priority: int = 0,
    ) -> None:
        if queue not in self._queues:
            self._queues[queue] = PriorityQueue()
        self._queues[queue].push(task, priority)

    def schedule(
        self,
        task: Dict,
        delay: float,
        queue: str = "default",
        priority: int = 0,
    ) -> str:
        decision = self._validate_artifact_cleanup(task)
        task_id = str(uuid4())
        task["id"] = task_id
        task["scheduled_at"] = time.time()
        task["scheduled_for"] = task["scheduled_at"] + delay
        task["retries"] = task.get("retries", 0)
        self._scheduled[task_id] = {
            "task": task,
            "due_at": task["scheduled_for"],
            "queue": queue,
            "priority": priority,
        }
        if decision.artifact_id:
            self._bind_artifact_cleanup(decision.artifact_id, task_id)
            self._audit("cleanup_scheduled", task_id, decision)
            metrics.increment("artifact_retention.cleanup_scheduled")
        return task_id

    async def dequeue(
        self,
        queue: str = "default",
        timeout: float = 1.0,
    ) -> Optional[Dict]:
        now = time.time()
        expired = [
            tid
            for tid, item in self._scheduled.items()
            if item["due_at"] <= now
        ]
        for tid in expired:
            item = self._scheduled.pop(tid)
            task = item["task"]
            if task:
                self._put_queue(task, item["queue"], item["priority"])

        if queue in self._queues and len(self._queues[queue]) > 0:
            task = self._queues[queue].pop()
            if task:
                self._in_flight[task["id"]] = task
                return task
        return None

    def complete(self, task_id: str) -> bool:
        task = self._in_flight.pop(task_id, None)
        if task is None:
            return False
        self._release_artifact_cleanup(task_id)
        return True

    def fail(self, task_id: str, queue: str = "default") -> bool:
        task = self._in_flight.pop(task_id, None)
        if task:
            task["retries"] += 1
            if task["retries"] < self._max_retries:
                self._put_queue(task, queue, priority=task.get("priority", 0))
                return True
            self._release_artifact_cleanup(task_id)
        return False

    def audit_records(self) -> List[Dict[str, Any]]:
        return [dict(record) for record in self._audit_records]

    def _validate_artifact_cleanup(self, task: Dict) -> Any:
        decision = (
            self._artifact_retention_validator.validate_cleanup_schedule(task)
        )
        if not decision.allowed:
            self._audit("cleanup_rejected", None, decision)
            metrics.increment(
                f"artifact_retention.cleanup_rejected.{decision.reason}"
            )
            logger.warning(
                "artifact cleanup scheduling rejected: %s",
                decision.audit_context(),
            )
            raise ValueError(f"artifact cleanup rejected: {decision.reason}")

        if (
            decision.artifact_id
            and decision.artifact_id in self._artifact_cleanup_bindings
        ):
            duplicate = decision.__class__(
                False,
                "reject",
                "duplicate-cleanup-scheduled",
                decision.artifact_id,
                decision.lifecycle_state,
            )
            self._audit("cleanup_rejected", None, duplicate)
            metrics.increment(
                "artifact_retention.cleanup_rejected."
                "duplicate-cleanup-scheduled"
            )
            logger.warning(
                "artifact cleanup scheduling rejected: %s",
                duplicate.audit_context(),
            )
            raise ValueError(
                "artifact cleanup rejected: duplicate-cleanup-scheduled"
            )
        return decision

    def _bind_artifact_cleanup(self, artifact_id: str, task_id: str) -> None:
        self._artifact_cleanup_bindings[artifact_id] = task_id

    def _release_artifact_cleanup(self, task_id: str) -> None:
        for artifact_id, bound_task_id in list(
            self._artifact_cleanup_bindings.items()
        ):
            if bound_task_id == task_id:
                del self._artifact_cleanup_bindings[artifact_id]

    def _audit(
        self,
        event: str,
        task_id: Optional[str],
        decision: Any,
    ) -> None:
        record = {
            "event": event,
            "task_id": task_id,
            "reason": decision.reason,
            "artifact_ref": decision.artifact_ref(),
            "lifecycle_state": decision.lifecycle_state,
            "timestamp": time.time(),
        }
        self._audit_records.append(record)

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
