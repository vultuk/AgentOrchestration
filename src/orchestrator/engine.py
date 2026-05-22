"""Orchestration Engine — Core execution and coordination logic."""

import asyncio
import hashlib
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from src.agent import AgentRegistry, AgentStatus
from src.common.metrics import MetricsCollector
from src.orchestrator.scheduler import TaskScheduler

logger = logging.getLogger(__name__)

_UNKNOWN_RUN_REF = "unknown"
_EVENT_TRANSITIONS: Dict[str, Tuple[Set[str], str]] = {
    "run.started": ({"pending"}, "running"),
    "run.completed": ({"running"}, "completed"),
    "run.failed": ({"running"}, "failed"),
    "run.cancelled": ({"pending", "running"}, "cancelled"),
}


class OrchestrationEngine:
    def __init__(self, max_workers: int = 10, agent_timeout: int = 300):
        self.registry = AgentRegistry()
        self.scheduler = TaskScheduler()
        self.metrics = MetricsCollector()
        self.executor = ThreadPoolExecutor(max_workers=max_workers)
        self.agent_timeout = agent_timeout
        self._run_events: Dict[str, Dict[str, Any]] = {}
        self._event_audit: List[Dict[str, Any]] = []
        self._running = False
        self._hooks: Dict[str, List[Callable]] = {
            "pre_execute": [],
            "post_execute": [],
            "on_error": [],
            "on_complete": [],
        }

    def register_hook(self, event: str, callback: Callable) -> None:
        if event in self._hooks:
            self._hooks[event].append(callback)

    def dispatch_event(self, event: Dict[str, Any]) -> Dict[str, Any]:
        event_type = str(event.get("type") or "")
        run_id = str(event.get("run_id") or "")
        attempt = self._integer_event_field(event, "attempt", default=1)
        revision = self._integer_event_field(event, "revision", default=0)
        run_state = self._run_events.get(run_id)

        if not run_id:
            return self._quarantine_event(
                event_type,
                run_id,
                "missing_run_id",
                attempt,
                revision,
            )
        if event_type not in _EVENT_TRANSITIONS:
            return self._quarantine_event(
                event_type,
                run_id,
                "unknown_event_type",
                attempt,
                revision,
            )

        if run_state:
            current_attempt = run_state["attempt"]
            current_revision = run_state["revision"]
            if attempt < current_attempt:
                return self._quarantine_event(
                    event_type,
                    run_id,
                    "stale_attempt",
                    attempt,
                    revision,
                )
            if attempt == current_attempt and revision <= current_revision:
                return self._quarantine_event(
                    event_type,
                    run_id,
                    "stale_revision",
                    attempt,
                    revision,
                )

        allowed_sources, target_lifecycle = _EVENT_TRANSITIONS[event_type]
        current_lifecycle = run_state["lifecycle"] if run_state else "pending"
        if current_lifecycle not in allowed_sources:
            return self._quarantine_event(
                event_type,
                run_id,
                "invalid_lifecycle",
                attempt,
                revision,
            )

        next_state = {
            "lifecycle": target_lifecycle,
            "attempt": attempt,
            "revision": revision,
            "event_type": event_type,
        }
        self._run_events[run_id] = next_state
        return self._record_event_decision(
            "accepted",
            event_type,
            run_id,
            "",
            attempt,
            revision,
        )

    def get_run_event_state(self, run_id: str) -> Optional[Dict[str, Any]]:
        state = self._run_events.get(run_id)
        return dict(state) if state else None

    def event_audit_records(self) -> List[Dict[str, Any]]:
        return [dict(record) for record in self._event_audit]

    def _integer_event_field(
        self,
        event: Dict[str, Any],
        field: str,
        default: int,
    ) -> int:
        try:
            return int(event.get(field, default))
        except (TypeError, ValueError):
            return default

    def _run_ref(self, run_id: str) -> str:
        if not run_id:
            return _UNKNOWN_RUN_REF
        return hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:12]

    def _quarantine_event(
        self,
        event_type: str,
        run_id: str,
        reason: str,
        attempt: int,
        revision: int,
    ) -> Dict[str, Any]:
        return self._record_event_decision(
            "quarantined",
            event_type,
            run_id,
            reason,
            attempt,
            revision,
        )

    def _record_event_decision(
        self,
        decision: str,
        event_type: str,
        run_id: str,
        reason: str,
        attempt: int,
        revision: int,
    ) -> Dict[str, Any]:
        record = {
            "decision": decision,
            "event_type": event_type or "missing",
            "reason": reason,
            "run_ref": self._run_ref(run_id),
            "attempt": attempt,
            "revision": revision,
        }
        self._event_audit.append(record)
        self.metrics.increment(f"orchestrator.events.{decision}")
        logger.info(
            "orchestrator event decision=%s type=%s reason=%s run_ref=%s",
            decision,
            record["event_type"],
            reason or "none",
            record["run_ref"],
        )
        return dict(record)

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
