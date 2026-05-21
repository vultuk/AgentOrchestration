"""Task Scheduler — Priority-based task queuing and dispatch."""

import hashlib
import heapq
import time
from enum import Enum
from threading import RLock
from typing import Any, Callable, Dict, List, Optional
from uuid import uuid4


class TaskState(str, Enum):
    QUEUED = "queued"
    SCHEDULED = "scheduled"
    IN_FLIGHT = "in_flight"
    COMPLETED = "completed"
    DEAD_LETTERED = "dead_lettered"
    CANCELLED = "cancelled"


TERMINAL_STATES = {
    TaskState.COMPLETED,
    TaskState.DEAD_LETTERED,
    TaskState.CANCELLED,
}


VALID_TRANSITIONS = {
    TaskState.QUEUED: {TaskState.IN_FLIGHT, TaskState.CANCELLED},
    TaskState.SCHEDULED: {TaskState.QUEUED, TaskState.CANCELLED},
    TaskState.IN_FLIGHT: {
        TaskState.SCHEDULED,
        TaskState.COMPLETED,
        TaskState.DEAD_LETTERED,
        TaskState.CANCELLED,
    },
}


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
    def __init__(
        self,
        max_retries: int = 3,
        retry_delay: float = 0.0,
        clock: Optional[Callable[[], float]] = None,
    ):
        self._queues: Dict[str, PriorityQueue] = {}
        self._scheduled: Dict[str, Dict] = {}
        self._in_flight: Dict[str, Dict] = {}
        self._states: Dict[str, TaskState] = {}
        self._attempts: Dict[str, int] = {}
        self._dead_letters: Dict[str, Dict[str, Any]] = {}
        self._audit_records: List[Dict[str, str]] = []
        self._lock = RLock()
        self._max_retries = max(0, max_retries)
        self._retry_delay = max(0.0, retry_delay)
        self._clock = clock or time.time

    def enqueue(
        self,
        task: Dict,
        queue: str = "default",
        priority: int = 0,
    ) -> str:
        with self._lock:
            task_id = str(uuid4())
            task["id"] = task_id
            task["enqueued_at"] = self._clock()
            task["retries"] = 0
            task["attempt"] = 0
            self._attempts[task_id] = 0
            self._push_current_attempt(task, queue, priority)
            return task_id

    def schedule(
        self,
        task: Dict,
        delay: float,
        queue: str = "default",
        priority: int = 0,
    ) -> str:
        with self._lock:
            task_id = str(uuid4())
            task["id"] = task_id
            task["queue"] = queue
            task["priority"] = priority
            task["retries"] = 0
            task["attempt"] = 0
            task["scheduled_for"] = self._clock() + max(0.0, delay)
            self._attempts[task_id] = 0
            self._transition(task_id, TaskState.SCHEDULED)
            self._scheduled[task_id] = task
            return task_id

    async def dequeue(
        self,
        queue: str = "default",
        timeout: float = 1.0,
    ) -> Optional[Dict]:
        del timeout
        with self._lock:
            self._promote_ready_tasks()
            if queue not in self._queues:
                return None

            task = self._queues[queue].pop()
            while task:
                task_id = task.get("id")
                if self._is_current_attempt(task, TaskState.QUEUED):
                    self._transition(task_id, TaskState.IN_FLIGHT)
                    task["started_at"] = self._clock()
                    self._in_flight[task_id] = task
                    return task
                task = self._queues[queue].pop()
            return None

    def complete(self, task_id: str) -> bool:
        with self._lock:
            task = self._in_flight.get(task_id)
            if not task:
                return False

            self._in_flight.pop(task_id, None)
            if not self._transition(task_id, TaskState.COMPLETED):
                return False
            task["completed_at"] = self._clock()
            return True

    def fail(
        self,
        task_id: str,
        queue: Optional[str] = None,
        reason: str = "handler_failed",
        retryable: bool = True,
    ) -> bool:
        with self._lock:
            task = self._in_flight.get(task_id)
            if not task:
                return self._handle_duplicate_ack(task_id)

            next_retry = int(task.get("retries", 0)) + 1
            task["retries"] = next_retry

            if retryable and next_retry < self._max_retries:
                self._in_flight.pop(task_id, None)
                self._attempts[task_id] = self._attempts.get(task_id, 0) + 1
                task["attempt"] = self._attempts[task_id]
                task["queue"] = queue or task.get("queue", "default")
                task["scheduled_for"] = self._clock() + self._retry_delay
                self._transition(task_id, TaskState.SCHEDULED)
                self._scheduled[task_id] = task
                self._audit(
                    "ack_retry_scheduled",
                    task_id,
                    "retry_budget_available",
                )
                return True

            return self._write_dead_letter(task, reason)

    def cancel(self, task_id: str) -> bool:
        with self._lock:
            if (
                task_id not in self._states
                or self._states[task_id] in TERMINAL_STATES
            ):
                return False

            self._in_flight.pop(task_id, None)
            self._scheduled.pop(task_id, None)
            return self._transition(task_id, TaskState.CANCELLED)

    def get_state(self, task_id: str) -> Optional[TaskState]:
        with self._lock:
            return self._states.get(task_id)

    def dead_letters(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(record) for record in self._dead_letters.values()]

    def audit_records(self) -> List[Dict[str, str]]:
        with self._lock:
            return [dict(record) for record in self._audit_records]

    def _push_current_attempt(
        self,
        task: Dict,
        queue: str,
        priority: int,
    ) -> None:
        task_id = task["id"]
        task["queue"] = queue
        task["priority"] = priority
        task["attempt"] = self._attempts[task_id]
        if queue not in self._queues:
            self._queues[queue] = PriorityQueue()
        self._transition(task_id, TaskState.QUEUED)
        self._queues[queue].push(task, priority)

    def _promote_ready_tasks(self) -> None:
        now = self._clock()
        ready = [
            task_id
            for task_id, task in self._scheduled.items()
            if task.get("scheduled_for", 0) <= now
        ]
        for task_id in ready:
            task = self._scheduled.pop(task_id)
            if not self._is_current_attempt(task, TaskState.SCHEDULED):
                continue
            self._push_current_attempt(
                task,
                task.get("queue", "default"),
                int(task.get("priority", 0)),
            )

    def _is_current_attempt(self, task: Dict, state: TaskState) -> bool:
        task_id = task.get("id")
        return (
            task_id in self._states
            and self._states.get(task_id) == state
            and task.get("attempt", 0) == self._attempts.get(task_id, 0)
        )

    def _handle_duplicate_ack(self, task_id: str) -> bool:
        state = self._states.get(task_id)
        if task_id in self._dead_letters:
            self._audit(
                "dead_letter_duplicate",
                task_id,
                "already_dead_lettered",
            )
            return True
        if state == TaskState.SCHEDULED:
            self._audit(
                "ack_retry_deferred",
                task_id,
                "scheduled_retry_pending",
            )
            return True
        if state == TaskState.QUEUED:
            self._audit("ack_retry_deferred", task_id, "retry_already_queued")
            return True
        self._audit("ack_retry_rejected", task_id, "invalid_or_unknown_task")
        return False

    def _write_dead_letter(self, task: Dict, reason: str) -> bool:
        task_id = task["id"]
        if task_id in self._dead_letters:
            self._in_flight.pop(task_id, None)
            self._audit(
                "dead_letter_duplicate",
                task_id,
                "already_dead_lettered",
            )
            return True

        record = {
            "task_ref": self._task_ref(task_id),
            "type": str(task.get("type", "")),
            "retries": int(task.get("retries", 0)),
            "attempt": int(task.get("attempt", 0)),
            "reason": self._safe_reason(reason),
            "dead_lettered_at": self._clock(),
        }
        self._dead_letters[task_id] = record
        self._in_flight.pop(task_id, None)
        if not self._transition(task_id, TaskState.DEAD_LETTERED):
            return False
        self._audit("dead_letter_written", task_id, "retry_budget_exhausted")
        return True

    def _audit(self, event: str, task_id: str, decision: str) -> None:
        self._audit_records.append({
            "event": event,
            "task_ref": self._task_ref(task_id),
            "decision": decision,
        })

    def _transition(self, task_id: str, next_state: TaskState) -> bool:
        current_state = self._states.get(task_id)
        if current_state in TERMINAL_STATES:
            return False
        if (
            current_state is not None
            and next_state not in VALID_TRANSITIONS.get(current_state, set())
        ):
            return False
        self._states[task_id] = next_state
        return True

    @staticmethod
    def _task_ref(task_id: str) -> str:
        return hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:12]

    @staticmethod
    def _safe_reason(reason: str) -> str:
        normalized = str(reason).lower()
        if "timeout" in normalized:
            return "timeout"
        if "cancel" in normalized:
            return "cancelled"
        if "worker" in normalized:
            return "worker_error"
        if "handler" in normalized:
            return "handler_failed"
        return "unspecified"

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
