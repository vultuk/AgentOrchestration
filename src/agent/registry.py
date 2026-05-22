"""Agent Registry — Manages agent lifecycle and metadata."""

import hashlib
import time
import uuid
from copy import deepcopy
from enum import Enum
from typing import Any, Dict, List, Optional

from src.common.metrics import MetricsCollector, metrics


class AgentStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    FAILED = "failed"
    TERMINATED = "terminated"


class AgentRegistry:
    def __init__(
        self,
        storage_backend: str = "memory",
        metrics_collector: MetricsCollector = metrics,
    ):
        self.storage_backend = storage_backend
        self._agents: Dict[str, Dict[str, Any]] = {}
        self._index: Dict[str, List[str]] = {}
        self._plugins: Dict[str, Dict[str, Any]] = {}
        self._capability_index: Dict[str, str] = {}
        self._capability_cache: Dict[str, Optional[Dict[str, Any]]] = {}
        self._plugin_audit: List[Dict[str, Any]] = []
        self._metrics = metrics_collector

    def register(
        self,
        name: str,
        agent_type: str,
        config: Optional[Dict] = None,
    ) -> str:
        agent_id = str(uuid.uuid4())
        timestamp = time.time()
        self._agents[agent_id] = {
            "id": agent_id,
            "name": name,
            "type": agent_type,
            "status": AgentStatus.PENDING.value,
            "config": config or {},
            "created_at": timestamp,
            "updated_at": timestamp,
            "version": "1.0.0",
            "metrics": {"tasks_completed": 0, "errors": 0, "uptime": 0},
        }
        group = agent_type.split(".")[0]
        if group not in self._index:
            self._index[group] = []
        self._index[group].append(agent_id)
        return agent_id

    def get(self, agent_id: str) -> Optional[Dict[str, Any]]:
        return self._agents.get(agent_id)

    def list(
        self,
        status: Optional[AgentStatus] = None,
        group: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        agents = self._agents.values()
        if status:
            agents = [a for a in agents if a["status"] == status.value]
        if group:
            agent_ids = self._index.get(group, [])
            agents = [a for a in agents if a["id"] in agent_ids]
        return list(agents)

    def update_status(self, agent_id: str, status: AgentStatus) -> bool:
        if agent_id not in self._agents:
            return False
        self._agents[agent_id]["status"] = status.value
        self._agents[agent_id]["updated_at"] = time.time()
        return True

    def delete(self, agent_id: str) -> bool:
        if agent_id not in self._agents:
            return False
        agent = self._agents.pop(agent_id)
        group = agent["type"].split(".")[0]
        if group in self._index and agent_id in self._index[group]:
            self._index[group].remove(agent_id)
        return True

    def count(self) -> int:
        return len(self._agents)

    @property
    def plugin_audit_records(self) -> List[Dict[str, Any]]:
        return deepcopy(self._plugin_audit)

    def register_plugin(
        self,
        plugin_id: str,
        capabilities: List[str],
        metadata: Optional[Dict] = None,
        replace: bool = False,
    ) -> bool:
        normalized_capabilities = self._normalize_capabilities(capabilities)
        reason = self._validate_plugin_registration(
            plugin_id,
            normalized_capabilities,
            replace,
        )
        if reason:
            self._record_plugin_decision(
                "register",
                False,
                reason,
                plugin_id,
                normalized_capabilities,
            )
            return False

        previous_capabilities = set(
            self._plugins.get(plugin_id, {}).get("capabilities", []),
        )
        for capability in previous_capabilities:
            if self._capability_index.get(capability) == plugin_id:
                self._capability_index.pop(capability)

        registered_at = time.time()
        self._plugins[plugin_id] = {
            "id": plugin_id,
            "capabilities": list(normalized_capabilities),
            "metadata": deepcopy(metadata or {}),
            "registered_at": registered_at,
            "updated_at": registered_at,
        }
        for capability in normalized_capabilities:
            self._capability_index[capability] = plugin_id

        self._invalidate_capability_cache(
            previous_capabilities | set(normalized_capabilities),
        )
        self._record_plugin_decision(
            "register",
            True,
            "accepted",
            plugin_id,
            normalized_capabilities,
        )
        return True

    def unregister_plugin(self, plugin_id: str) -> bool:
        plugin = self._plugins.pop(plugin_id, None)
        if not plugin:
            self._record_plugin_decision(
                "unregister",
                False,
                "unknown_plugin",
                plugin_id,
                [],
            )
            return False

        capabilities = set(plugin["capabilities"])
        for capability in capabilities:
            if self._capability_index.get(capability) == plugin_id:
                self._capability_index.pop(capability)

        self._invalidate_capability_cache(capabilities)
        self._record_plugin_decision(
            "unregister",
            True,
            "accepted",
            plugin_id,
            list(capabilities),
        )
        return True

    def resolve_capability(self, capability: str) -> Optional[Dict[str, Any]]:
        normalized_capability = self._normalize_capability(capability)
        if not normalized_capability:
            return None
        if normalized_capability in self._capability_cache:
            cached = self._capability_cache[normalized_capability]
            return deepcopy(cached) if cached else None

        plugin_id = self._capability_index.get(normalized_capability)
        plugin = self._plugins.get(plugin_id) if plugin_id else None
        if not plugin:
            self._capability_cache[normalized_capability] = None
            return None

        resolution = {
            "plugin_id": plugin_id,
            "capability": normalized_capability,
            "metadata": deepcopy(plugin.get("metadata", {})),
        }
        self._capability_cache[normalized_capability] = deepcopy(resolution)
        return resolution

    def _validate_plugin_registration(
        self,
        plugin_id: str,
        capabilities: List[str],
        replace: bool,
    ) -> Optional[str]:
        if not isinstance(plugin_id, str) or not plugin_id.strip():
            return "invalid_plugin_id"
        if plugin_id in self._plugins and not replace:
            return "plugin_already_registered"
        if not capabilities:
            return "missing_capabilities"
        if len(set(capabilities)) != len(capabilities):
            return "duplicate_capability_in_plugin"

        conflicting_capabilities = [
            capability
            for capability in capabilities
            if self._capability_index.get(capability)
            and self._capability_index[capability] != plugin_id
        ]
        if conflicting_capabilities:
            return "duplicate_capability_name"
        return None

    def _normalize_capabilities(self, capabilities: List[str]) -> List[str]:
        if not isinstance(capabilities, list):
            return []
        return [
            normalized
            for normalized in (
                self._normalize_capability(capability)
                for capability in capabilities
            )
            if normalized
        ]

    @staticmethod
    def _normalize_capability(capability: str) -> str:
        return capability.strip() if isinstance(capability, str) else ""

    def _invalidate_capability_cache(self, capabilities: set) -> None:
        invalidated = 0
        for capability in capabilities:
            if capability in self._capability_cache:
                self._capability_cache.pop(capability, None)
                invalidated += 1
        if invalidated:
            self._metrics.increment(
                "registry.capability_cache.invalidated",
                invalidated,
            )

    def _record_plugin_decision(
        self,
        action: str,
        accepted: bool,
        reason: str,
        plugin_id: str,
        capabilities: List[str],
    ) -> None:
        status = "accepted" if accepted else "rejected"
        self._metrics.increment(f"registry.plugin_registration.{status}")
        self._plugin_audit.append(
            {
                "component": "registry_plugin_loader",
                "action": action,
                "accepted": accepted,
                "reason": reason,
                "plugin_ref": self._hash_ref(plugin_id),
                "capability_count": len(capabilities),
                "capability_refs": [
                    self._hash_ref(capability)
                    for capability in sorted(capabilities)
                ],
                "recorded_at": time.time(),
            }
        )

    @staticmethod
    def _hash_ref(value: str) -> str:
        raw_value = value if isinstance(value, str) else ""
        return hashlib.sha256(raw_value.encode("utf-8")).hexdigest()[:16]

# 2019-01-29T11:24:49 update

# 2019-04-09T13:38:38 update

# 2019-04-11T11:24:12 update

# 2019-06-26T17:03:48 update

# 2019-07-03T14:55:48 update

# 2019-07-18T18:18:47 update

# 2019-11-05T11:27:19 update

# 2019-11-20T11:35:05 update

# 2019-11-23T15:28:54 update

# 2020-03-13T09:23:07 update

# 2020-03-30T19:31:18 update

# 2020-04-22T15:03:30 update

# 2020-07-21T10:00:48 update

# 2020-09-10T09:02:08 update

# 2020-09-10T13:39:12 update

# 2020-09-22T16:27:52 update

# 2020-10-15T10:33:14 update

# 2021-05-13T11:15:56 update

# 2021-07-07T14:57:13 update

# 2021-07-13T15:15:19 update

# 2021-07-27T10:18:16 update

# 2022-03-11T15:24:11 update

# 2022-09-22T13:24:20 update

# 2022-11-01T12:20:40 update

# 2023-01-30T12:32:27 update

# 2023-03-10T09:43:50 update

# 2023-05-10T14:28:01 update

# 2023-05-11T20:04:46 update

# 2023-05-30T17:00:59 update

# 2023-07-13T17:54:32 update

# 2023-07-20T19:04:20 update

# 2023-07-31T17:00:02 update

# 2023-09-05T19:42:07 update

# 2024-01-02T10:29:47 update

# 2024-09-17T12:45:29 update

# 2024-09-17T11:51:01 update

# 2024-11-06T18:20:15 update

# 2025-01-12T15:13:14 update

# 2025-01-14T20:24:39 update

# 2025-03-26T20:21:27 update

# 2025-04-10T18:27:06 update

# 2025-06-19T20:34:58 update

# 2025-06-21T20:23:53 update

# 2025-06-24T20:30:30 update

# 2025-07-03T13:28:03 update

# 2025-07-24T17:42:21 update

# 2025-08-19T17:42:23 update

# 2025-08-21T11:06:52 update

# 2025-10-24T09:10:08 update

# 2025-12-18T19:34:38 update

# 2026-02-06T11:22:22 update

# 2026-02-13T15:42:04 update

# 2026-04-10T08:16:30 update

# 2026-04-29T18:16:11 update
