"""Orchestration Engine — Core execution and coordination logic."""

import asyncio
import hashlib
import logging
from concurrent.futures import ThreadPoolExecutor
from threading import RLock
from typing import Any, Callable, Dict, List, Optional

from src.agent import AgentRegistry, AgentStatus
from src.common.metrics import MetricsCollector, metrics
from src.orchestrator.scheduler import TaskScheduler

logger = logging.getLogger(__name__)


class OrchestrationEngine:
    def __init__(
        self,
        max_workers: int = 10,
        agent_timeout: int = 300,
        metrics_collector: Optional[MetricsCollector] = None,
    ):
        self.registry = AgentRegistry()
        self.scheduler = TaskScheduler()
        self.executor = ThreadPoolExecutor(max_workers=max_workers)
        self.agent_timeout = agent_timeout
        self.metrics = metrics_collector or metrics
        self._running = False
        self._lifecycle_lock = RLock()
        self._run_lifecycle: Dict[str, Dict[str, Any]] = {}
        self._run_audit_records: List[Dict[str, Any]] = []
        self._hooks: Dict[str, List[Callable]] = {
            "pre_execute": [],
            "post_execute": [],
            "on_error": [],
            "on_complete": [],
        }

    def register_hook(self, event: str, callback: Callable) -> None:
        if event in self._hooks:
            self._hooks[event].append(callback)

    def register_run(
        self,
        run_id: str,
        attempt_id: str,
        revision: int = 0,
        lifecycle_state: str = "active",
    ) -> None:
        with self._lifecycle_lock:
            self._run_lifecycle[run_id] = {
                "attempt_id": attempt_id,
                "revision": revision,
                "state": lifecycle_state,
            }

    def archive_run(self, run_id: str, revision: Optional[int] = None) -> None:
        with self._lifecycle_lock:
            run = self._run_lifecycle.setdefault(
                run_id,
                {"attempt_id": None, "revision": 0, "state": "active"},
            )
            if revision is not None:
                run["revision"] = max(run.get("revision", 0), revision)
            run["state"] = "archived"

    def audit_records(self) -> List[Dict[str, Any]]:
        with self._lifecycle_lock:
            return [dict(record) for record in self._run_audit_records]

    async def start(self) -> None:
        self._running = True
        logger.info("Orchestration engine started")
        while self._running:
            task = await self.scheduler.dequeue()
            if task:
                asyncio.create_task(self._execute_task(task))
            await asyncio.sleep(0.1)

    def stop(self) -> None:
        self._running = False
        logger.info("Orchestration engine stopped")

    async def _execute_task(self, task: Dict[str, Any]) -> None:
        task_id = task["id"]
        agent_id = task["target_agent"]
        logger.info(f"Executing task {task_id} on agent {agent_id}")

        if self._reject_invalid_run_event(task):
            self.scheduler.complete(task_id)
            return

        for hook in self._hooks["pre_execute"]:
            await hook(task)

        try:
            agent = self.registry.get(agent_id)
            if not agent:
                raise ValueError(f"Agent {agent_id} not found")

            self.registry.update_status(agent_id, AgentStatus.RUNNING)
            result = await asyncio.wait_for(
                self._run_agent_task(agent, task),
                timeout=self.agent_timeout,
            )
            self.registry.update_status(agent_id, AgentStatus.PAUSED)

            for hook in self._hooks["post_execute"]:
                await hook(task, result)

            logger.info(f"Task {task_id} completed successfully")

        except Exception as e:
            logger.error(f"Task {task_id} failed: {e}")
            for hook in self._hooks["on_error"]:
                await hook(task, e)

    async def _run_agent_task(self, agent: Dict, task: Dict) -> Any:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self.executor,
            self._execute_in_thread,
            agent,
            task,
        )

    def _execute_in_thread(self, agent: Dict, task: Dict) -> Any:
        return {
            "status": "completed",
            "output": f"Task {task['id']} processed by {agent['name']}",
        }

    def _reject_invalid_run_event(self, task: Dict[str, Any]) -> bool:
        record = self._run_event_rejection_record(task)
        if not record:
            return False

        self.metrics.increment(
            f"orchestrator.run_events.rejected.{record['reason']}",
        )
        logger.warning(
            "Rejected worker event for run_ref=%s task_ref=%s reason=%s",
            record["run_ref"],
            record["task_ref"],
            record["reason"],
        )
        return True

    def _run_event_rejection_record(
        self,
        task: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        run_id = task.get("run_id")
        if not run_id:
            return None

        task_attempt = task.get("run_attempt_id")
        raw_task_revision = task.get("run_revision")
        task_revision = self._coerce_revision(raw_task_revision)
        with self._lifecycle_lock:
            run = self._run_lifecycle.get(run_id)
            if not run:
                reason = "unknown_run"
                reasons = [reason]
                current_attempt = None
                current_revision = None
                lifecycle_state = "unknown"
            else:
                current_attempt = run.get("attempt_id")
                current_revision = run.get("revision", 0)
                lifecycle_state = run.get("state", "active")
                reasons = self._run_event_rejection_reasons(
                    lifecycle_state,
                    current_attempt,
                    current_revision,
                    task_attempt,
                    task_revision,
                    raw_task_revision,
                )
                if not reasons:
                    return None
                reason = reasons[0]

            record = {
                "decision": "rejected",
                "reason": reason,
                "reasons": tuple(reasons),
                "run_ref": self._safe_ref(run_id),
                "task_ref": self._safe_ref(task.get("id")),
                "event_type": self._safe_event_type(task.get("type")),
                "attempt_ref": self._safe_ref(task_attempt),
                "current_attempt_ref": self._safe_ref(current_attempt),
                "run_revision": task_revision,
                "current_revision": current_revision,
                "lifecycle_state": lifecycle_state,
            }
            self._run_audit_records.append(record)
            return dict(record)

    @staticmethod
    def _run_event_rejection_reasons(
        lifecycle_state: str,
        current_attempt: Optional[str],
        current_revision: Optional[int],
        task_attempt: Optional[str],
        task_revision: Optional[int],
        raw_task_revision: Any,
    ) -> List[str]:
        reasons: List[str] = []
        if lifecycle_state == "archived":
            reasons.append("archived_run")
        if current_attempt is not None:
            if task_attempt is None:
                reasons.append("missing_attempt")
            elif task_attempt != current_attempt:
                reasons.append("stale_attempt")
        if current_revision is not None:
            if raw_task_revision is None:
                reasons.append("missing_revision")
            elif task_revision is None:
                reasons.append("invalid_revision")
            elif task_revision < current_revision:
                reasons.append("stale_revision")
        return reasons

    @staticmethod
    def _coerce_revision(value: Any) -> Optional[int]:
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdecimal():
            return int(value)
        return None

    @staticmethod
    def _safe_ref(value: Any) -> Optional[str]:
        if value is None:
            return None
        digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()
        return digest[:16]

    @staticmethod
    def _safe_event_type(value: Any) -> str:
        if value is None:
            return "unknown"
        return str(value)[:80]

# 2019-04-24T14:55:39 update

# 2019-05-01T16:01:52 update

# 2019-05-27T19:55:55 update

# 2019-06-02T09:38:08 update

# 2019-07-10T15:36:32 update

# 2019-07-22T11:36:40 update

# 2019-08-28T10:50:39 update

# 2019-08-30T14:21:57 update

# 2019-09-12T18:46:28 update

# 2019-10-02T09:55:59 update

# 2019-10-03T16:01:13 update

# 2019-12-03T13:07:37 update

# 2020-01-10T13:47:02 update

# 2020-01-31T13:14:49 update

# 2020-03-11T08:03:44 update

# 2020-03-31T15:51:14 update

# 2020-04-10T11:21:15 update

# 2020-06-08T09:31:33 update

# 2020-06-16T20:32:00 update

# 2020-07-21T18:48:01 update

# 2020-09-29T15:16:08 update

# 2020-11-18T14:09:09 update

# 2020-11-26T18:02:40 update

# 2021-01-07T11:18:24 update

# 2021-04-05T15:49:29 update

# 2021-04-27T11:58:27 update

# 2021-05-17T14:54:17 update

# 2021-06-07T11:46:07 update

# 2021-08-31T14:55:54 update

# 2021-09-10T17:29:34 update

# 2021-09-14T10:27:30 update

# 2021-10-06T14:04:05 update

# 2022-03-15T18:11:19 update

# 2022-09-15T18:32:09 update

# 2022-11-17T08:15:16 update

# 2023-02-17T12:24:53 update

# 2023-04-25T14:26:37 update

# 2023-05-22T09:03:39 update

# 2023-09-06T20:26:58 update

# 2023-11-28T17:54:23 update

# 2023-12-27T15:38:11 update

# 2024-03-12T20:10:32 update

# 2024-04-04T20:43:06 update

# 2024-05-27T12:23:51 update

# 2024-05-27T16:42:42 update

# 2024-07-23T13:27:05 update

# 2024-07-24T19:24:13 update

# 2024-11-03T18:25:58 update

# 2025-04-23T20:03:19 update

# 2026-02-16T17:12:09 update

# 2026-03-12T11:33:28 update
