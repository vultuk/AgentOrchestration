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

_UNKNOWN_ENTITY_REF = "unknown"
_TERMINAL_LIFECYCLES = {"completed", "failed", "cancelled"}
_EVENT_TRANSITIONS: Dict[str, Tuple[Set[str], str]] = {
    "run.started": ({"pending"}, "running"),
    "run.completed": ({"running"}, "completed"),
    "run.failed": ({"running"}, "failed"),
    "run.cancelled": ({"pending", "running"}, "cancelled"),
    "task.started": ({"pending"}, "running"),
    "task.completed": ({"running"}, "completed"),
    "task.failed": ({"running"}, "failed"),
    "task.cancelled": ({"pending", "running"}, "cancelled"),
    "handler.started": ({"pending"}, "running"),
    "handler.completed": ({"running"}, "completed"),
    "handler.failed": ({"running"}, "failed"),
    "handler.cancelled": ({"pending", "running"}, "cancelled"),
}
_ENTITY_ID_FIELDS: Dict[str, Tuple[str, ...]] = {
    "run": ("run_id", "entity_id", "id"),
    "task": ("task_id", "entity_id", "id"),
    "handler": ("handler_id", "entity_id", "id"),
    "unknown": ("entity_id", "run_id", "task_id", "handler_id", "id"),
}


class OrchestrationEngine:
    def __init__(
        self,
        max_workers: int = 10,
        agent_timeout: int = 300,
        metrics_collector: Optional[MetricsCollector] = None,
    ):
        self.registry = AgentRegistry()
        self.scheduler = TaskScheduler()
        self.metrics = metrics_collector or MetricsCollector()
        self.executor = ThreadPoolExecutor(max_workers=max_workers)
        self.agent_timeout = agent_timeout
        self._event_states: Dict[str, Dict[str, Any]] = {}
        self._event_audit: List[Dict[str, Any]] = []
        self._quarantined_events: List[Dict[str, Any]] = []
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
        entity_type = self._event_entity_type(event_type)
        entity_id = self._event_entity_id(event, entity_type)
        state_key = self._event_state_key(entity_type, entity_id)
        attempt = self._event_version(event, "attempt")
        revision = self._event_version(event, "revision")
        current_state = self._event_states.get(state_key)

        if event_type not in _EVENT_TRANSITIONS:
            return self._quarantine_event(
                event_type,
                entity_type,
                entity_id,
                "unknown_event_type",
                attempt,
                revision,
                current_state,
            )
        if not entity_id:
            return self._quarantine_event(
                event_type,
                entity_type,
                entity_id,
                self._missing_id_reason(entity_type),
                attempt,
                revision,
                current_state,
            )
        if attempt is None or revision is None:
            return self._quarantine_event(
                event_type,
                entity_type,
                entity_id,
                "invalid_version_marker",
                attempt,
                revision,
                current_state,
            )

        allowed_sources, target_lifecycle = _EVENT_TRANSITIONS[event_type]
        requested_lifecycle = event.get("lifecycle")
        if (
            requested_lifecycle is not None
            and str(requested_lifecycle) != target_lifecycle
        ):
            return self._quarantine_event(
                event_type,
                entity_type,
                entity_id,
                "policy_lifecycle_mismatch",
                attempt,
                revision,
                current_state,
            )

        if current_state:
            current_attempt = current_state["attempt"]
            current_revision = current_state["revision"]
            current_lifecycle = current_state["lifecycle"]
            if attempt < current_attempt:
                return self._quarantine_event(
                    event_type,
                    entity_type,
                    entity_id,
                    "stale_attempt",
                    attempt,
                    revision,
                    current_state,
                )
            if revision < current_revision:
                return self._quarantine_event(
                    event_type,
                    entity_type,
                    entity_id,
                    "stale_revision",
                    attempt,
                    revision,
                    current_state,
                )
            if attempt == current_attempt and revision == current_revision:
                reason = (
                    "duplicate_transition"
                    if current_lifecycle == target_lifecycle
                    else "stale_revision"
                )
                return self._quarantine_event(
                    event_type,
                    entity_type,
                    entity_id,
                    reason,
                    attempt,
                    revision,
                    current_state,
                )
            if current_lifecycle in _TERMINAL_LIFECYCLES:
                return self._quarantine_event(
                    event_type,
                    entity_type,
                    entity_id,
                    "terminal_lifecycle",
                    attempt,
                    revision,
                    current_state,
                )
            if current_lifecycle == target_lifecycle:
                return self._quarantine_event(
                    event_type,
                    entity_type,
                    entity_id,
                    "duplicate_transition",
                    attempt,
                    revision,
                    current_state,
                )

        current_lifecycle = (
            current_state["lifecycle"] if current_state else "pending"
        )
        if current_lifecycle not in allowed_sources:
            return self._quarantine_event(
                event_type,
                entity_type,
                entity_id,
                "invalid_lifecycle",
                attempt,
                revision,
                current_state,
            )

        next_state = {
            "entity_type": entity_type,
            "lifecycle": target_lifecycle,
            "attempt": attempt,
            "revision": revision,
            "event_type": event_type,
        }
        self._event_states[state_key] = next_state
        return self._record_event_decision(
            "accepted",
            event_type,
            entity_type,
            entity_id,
            "",
            attempt,
            revision,
            current_state,
        )

    def get_run_event_state(self, run_id: str) -> Optional[Dict[str, Any]]:
        return self.get_event_state(run_id, "run")

    def get_event_state(
        self,
        entity_id: str,
        entity_type: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        if entity_type:
            state = self._event_states.get(
                self._event_state_key(entity_type, entity_id)
            )
            return dict(state) if state else None

        for family in ("run", "task", "handler"):
            state = self._event_states.get(
                self._event_state_key(family, entity_id)
            )
            if state:
                return dict(state)
        return None

    def quarantined_events(self) -> List[Dict[str, Any]]:
        return [dict(record) for record in self._quarantined_events]

    def event_audit_records(self) -> List[Dict[str, Any]]:
        return [dict(record) for record in self._event_audit]

    def _event_entity_type(self, event_type: str) -> str:
        family = event_type.split(".", 1)[0] if "." in event_type else ""
        if family in _ENTITY_ID_FIELDS:
            return family
        return "unknown"

    def _event_entity_id(self, event: Dict[str, Any], entity_type: str) -> str:
        for field in _ENTITY_ID_FIELDS.get(entity_type, ()):
            value = event.get(field)
            if value:
                return str(value)
        return ""

    def _event_state_key(self, entity_type: str, entity_id: str) -> str:
        return f"{entity_type}:{entity_id}"

    def _missing_id_reason(self, entity_type: str) -> str:
        if entity_type == "run":
            return "missing_run_id"
        if entity_type in {"task", "handler"}:
            return f"missing_{entity_type}_id"
        return "missing_entity_id"

    def _event_version(
        self,
        event: Dict[str, Any],
        field: str,
    ) -> Optional[int]:
        if field not in event:
            return None
        try:
            value = int(event[field])
        except (TypeError, ValueError):
            return None
        return value if value >= 0 else None

    def _entity_ref(self, entity_id: str) -> str:
        if not entity_id:
            return _UNKNOWN_ENTITY_REF
        return hashlib.sha256(entity_id.encode("utf-8")).hexdigest()[:12]

    def _quarantine_event(
        self,
        event_type: str,
        entity_type: str,
        entity_id: str,
        reason: str,
        attempt: Optional[int],
        revision: Optional[int],
        current_state: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        record = self._record_event_decision(
            "quarantined",
            event_type,
            entity_type,
            entity_id,
            reason,
            attempt,
            revision,
            current_state,
        )
        self._quarantined_events.append(record)
        return record

    def _record_event_decision(
        self,
        decision: str,
        event_type: str,
        entity_type: str,
        entity_id: str,
        reason: str,
        attempt: Optional[int],
        revision: Optional[int],
        current_state: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        current_lifecycle = (
            current_state["lifecycle"] if current_state else None
        )
        target_lifecycle = _EVENT_TRANSITIONS.get(
            event_type,
            (set(), "unknown"),
        )[1]
        record = {
            "decision": decision,
            "event_type": event_type or "missing",
            "entity_type": entity_type,
            "reason": reason,
            "entity_ref": self._entity_ref(entity_id),
            "attempt": attempt,
            "revision": revision,
            "current_lifecycle": current_lifecycle,
            "target_lifecycle": target_lifecycle,
        }
        if entity_type == "run":
            record["run_ref"] = record["entity_ref"]
        self._event_audit.append(record)
        self.metrics.increment(f"orchestrator.events.{decision}")
        self.metrics.increment(f"orchestrator.events.{entity_type}.{decision}")
        if decision == "quarantined":
            self.metrics.increment(f"orchestrator.events.quarantined.{reason}")
        logger.info(
            "orchestrator event decision=%s type=%s reason=%s "
            "entity_type=%s entity_ref=%s",
            decision,
            record["event_type"],
            reason or "none",
            entity_type,
            record["entity_ref"],
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
