"""Task Scheduler — Priority-based task queuing and dispatch."""

import heapq
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
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


class TaskScheduler:
    def __init__(self, time_fn: Optional[Callable[[], float]] = None):
        self._queues: Dict[str, PriorityQueue] = {}
        self._scheduled: Dict[str, Dict[str, Any]] = {}
        self._in_flight: Dict[str, Dict] = {}
        self._task_routing: Dict[str, Dict[str, Any]] = {}
        self._workflow_blackouts: Dict[str, List[Tuple[float, float]]] = {}
        self._audit_events: List[Dict[str, Any]] = []
        self._metrics: Dict[str, int] = {"blackout_deferrals": 0}
        self._max_retries = 3
        self._time = time_fn or time.time

    def enqueue(
        self, task: Dict, queue: str = "default", priority: int = 0
    ) -> str:
        task_id = str(uuid4())
        task["id"] = task_id
        task["enqueued_at"] = self._time()
        task["retries"] = 0
        self._task_routing[task_id] = {"queue": queue, "priority": priority}

        self._push_task(task, queue, priority)
        return task_id

    def schedule(
        self,
        task: Dict,
        delay: float,
        queue: str = "default",
        priority: int = 0,
    ) -> str:
        task_id = str(uuid4())
        task["id"] = task_id
        task.setdefault("retries", 0)
        self._task_routing[task_id] = {"queue": queue, "priority": priority}
        self._scheduled[task_id] = {
            "task": task,
            "queue": queue,
            "priority": priority,
            "run_at": self._time() + delay,
        }
        return task_id

    def set_workflow_blackout(
        self, workflow_id: str, windows: List[Tuple[float, float]]
    ) -> None:
        normalized = []
        for start, end in windows:
            if end <= start:
                raise ValueError("blackout window end must be after start")
            normalized.append((float(start), float(end)))
        self._workflow_blackouts[workflow_id] = normalized

    def clear_workflow_blackout(self, workflow_id: str) -> None:
        self._workflow_blackouts.pop(workflow_id, None)

    def audit_events(self) -> List[Dict[str, Any]]:
        return list(self._audit_events)

    def metrics(self) -> Dict[str, int]:
        return dict(self._metrics)

    async def dequeue(
        self, queue: str = "default", timeout: float = 1.0
    ) -> Optional[Dict]:
        now = self._time()
        self._promote_due_scheduled(now)

        queue_size = len(self._queues.get(queue, []))
        for _ in range(queue_size):
            task = self._queues[queue].pop()
            if not task:
                continue

            blackout_until = self._active_blackout_until(task, now)
            if blackout_until is not None:
                self._defer_for_blackout(task, queue, blackout_until)
                continue

            self._in_flight[task["id"]] = task
            return task
        return None

    def complete(self, task_id: str) -> bool:
        completed = self._in_flight.pop(task_id, None) is not None
        if completed:
            self._task_routing.pop(task_id, None)
        return completed

    def fail(self, task_id: str, queue: str = "default") -> bool:
        task = self._in_flight.pop(task_id, None)
        route = self._task_routing.pop(task_id, {})
        if task:
            task["retries"] += 1
            if task["retries"] < self._max_retries:
                self.enqueue(task, queue, priority=route.get("priority", 0))
                return True
        return False

    def _push_task(self, task: Dict, queue: str, priority: int) -> None:
        if queue not in self._queues:
            self._queues[queue] = PriorityQueue()
        self._queues[queue].push(task, priority)

    def _promote_due_scheduled(self, now: float) -> None:
        expired = [
            tid
            for tid, record in self._scheduled.items()
            if record["run_at"] <= now
        ]
        for task_id in expired:
            record = self._scheduled.pop(task_id)
            self._push_task(
                record["task"], record["queue"], record["priority"]
            )

    def _active_blackout_until(
        self, task: Dict, now: float
    ) -> Optional[float]:
        workflow_id = task.get("workflow_id")
        if not workflow_id:
            return None

        active_ends = [
            end
            for start, end in self._workflow_blackouts.get(workflow_id, [])
            if start <= now < end
        ]
        return min(active_ends) if active_ends else None

    def _defer_for_blackout(
        self, task: Dict, queue: str, blackout_until: float
    ) -> None:
        task_id = task["id"]
        route = self._task_routing.get(task_id, {})
        priority = route.get("priority", 0)
        self._scheduled[task_id] = {
            "task": task,
            "queue": queue,
            "priority": priority,
            "run_at": blackout_until,
        }
        self._metrics["blackout_deferrals"] += 1
        self._audit_events.append({
            "event": "task_dispatch_deferred",
            "reason": "workflow_blackout_window",
            "task_id": task_id,
            "workflow_id": task.get("workflow_id"),
            "queue": queue,
            "deferred_until": blackout_until,
        })

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
