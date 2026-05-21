"""Orchestration Engine — Core execution and coordination logic."""

import asyncio
import inspect
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional

from src.agent import AgentRegistry, AgentStatus
from src.orchestrator.scheduler import TaskScheduler

logger = logging.getLogger(__name__)


class PluginManifestError(ValueError):
    """Raised when a plugin manifest is unsafe to load."""


class OrchestrationEngine:
    HOOK_EVENTS = frozenset(
        {"pre_execute", "post_execute", "on_error", "on_complete"}
    )

    def __init__(self, max_workers: int = 10, agent_timeout: int = 300):
        self.registry = AgentRegistry()
        self.scheduler = TaskScheduler()
        self.executor = ThreadPoolExecutor(max_workers=max_workers)
        self.agent_timeout = agent_timeout
        self._running = False
        self._hooks: Dict[str, List[Callable]] = {
            "pre_execute": [],
            "post_execute": [],
            "on_error": [],
            "on_complete": [],
        }
        self._active_task_ids = set()
        self._task_outcomes: Dict[str, Dict[str, Any]] = {}
        self._state_lock = asyncio.Lock()

    def register_hook(self, event: str, callback: Callable) -> None:
        self._validate_hook(event, callback)
        self._hooks[event].append(callback)

    def validate_plugin_manifest(
        self,
        manifest: Dict[str, Any],
    ) -> Dict[str, Any]:
        if not isinstance(manifest, dict):
            raise PluginManifestError("plugin manifest must be an object")

        name = manifest.get("name")
        if not isinstance(name, str) or not name.strip():
            raise PluginManifestError(
                "plugin manifest requires a non-empty name"
            )

        hooks = manifest.get("hooks", [])
        if not isinstance(hooks, list):
            raise PluginManifestError("plugin manifest hooks must be a list")

        for index, hook in enumerate(hooks):
            if not isinstance(hook, dict):
                raise PluginManifestError(
                    f"plugin hook #{index} must be an object"
                )
            if hook.get("enabled", True) is False:
                continue
            self._validate_hook(hook.get("event"), hook.get("callback"))

        return manifest

    def load_plugin_manifest(self, manifest: Dict[str, Any]) -> None:
        self.validate_plugin_manifest(manifest)
        for hook in manifest.get("hooks", []):
            if hook.get("enabled", True) is not False:
                self.register_hook(hook["event"], hook["callback"])

    def get_task_outcome(self, task_id: str) -> Optional[Dict[str, Any]]:
        outcome = self._task_outcomes.get(task_id)
        return dict(outcome) if outcome else None

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

        if not await self._begin_task(task_id):
            logger.warning(
                f"Task {task_id} already has an active or terminal execution"
            )
            return

        try:
            manifest = task.get("plugin_manifest")
            if manifest is not None:
                self.validate_plugin_manifest(manifest)

            for hook in self._hooks["pre_execute"]:
                await self._call_hook(hook, task)

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
                await self._call_hook(hook, task, result)

            await self._record_task_outcome(
                task_id,
                "completed",
                result=result,
            )
            self.scheduler.complete(task_id)
            logger.info(f"Task {task_id} completed successfully")

        except asyncio.CancelledError:
            self.registry.update_status(agent_id, AgentStatus.FAILED)
            self.scheduler.discard(task_id)
            await self._record_task_outcome(task_id, "cancelled")
            raise
        except PluginManifestError as e:
            self.registry.update_status(agent_id, AgentStatus.FAILED)
            self.scheduler.discard(task_id)
            await self._record_task_outcome(
                task_id,
                "failed",
                error=str(e),
                error_type=type(e).__name__,
            )
            logger.error(
                f"Task {task_id} rejected before plugin hooks loaded: {e}"
            )
        except Exception as e:
            logger.error(f"Task {task_id} failed: {e}")
            self.registry.update_status(agent_id, AgentStatus.FAILED)
            retry_scheduled = self.scheduler.fail(task_id)
            if not retry_scheduled:
                await self._record_task_outcome(
                    task_id,
                    "failed",
                    error=str(e),
                    error_type=type(e).__name__,
                )
            for hook in self._hooks["on_error"]:
                await self._call_hook(hook, task, e)
        finally:
            await self._end_task(task_id)

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

    def _validate_hook(self, event: Any, callback: Any) -> None:
        if event not in self.HOOK_EVENTS:
            raise PluginManifestError(
                f"unsupported plugin hook event: {event!r}"
            )
        if not callable(callback):
            raise PluginManifestError(
                f"plugin hook {event!r} requires a callable"
            )

    async def _call_hook(self, hook: Callable, *args: Any) -> None:
        result = hook(*args)
        if inspect.isawaitable(result):
            await result

    async def _begin_task(self, task_id: str) -> bool:
        async with self._state_lock:
            if (
                task_id in self._task_outcomes
                or task_id in self._active_task_ids
            ):
                return False
            self._active_task_ids.add(task_id)
            return True

    async def _end_task(self, task_id: str) -> None:
        async with self._state_lock:
            self._active_task_ids.discard(task_id)

    async def _record_task_outcome(
        self,
        task_id: str,
        status: str,
        **fields: Any,
    ) -> bool:
        async with self._state_lock:
            if task_id in self._task_outcomes:
                return False
            self._task_outcomes[task_id] = {
                "task_id": task_id,
                "status": status,
                **fields,
            }
            return True

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
