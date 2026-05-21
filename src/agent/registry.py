"""Agent Registry — Manages agent lifecycle and metadata."""

import logging
import time
import uuid
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from src.common.metrics import metrics


logger = logging.getLogger(__name__)

SUPPORTED_PROTOCOL_VERSION = "1.0.0"


class AgentStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    FAILED = "failed"
    TERMINATED = "terminated"


UPGRADE_BLOCKING_STATUSES = {
    AgentStatus.RUNNING.value,
    AgentStatus.PAUSED.value,
}


class AgentRegistry:
    def __init__(self, storage_backend: str = "memory"):
        self.storage_backend = storage_backend
        self._agents: Dict[str, Dict[str, Any]] = {}
        self._index: Dict[str, List[str]] = {}
        self._resolution_cache: Dict[Tuple[str, str], str] = {}
        self._audit_log: List[Dict[str, Any]] = []

    def register(
        self,
        name: str,
        agent_type: str,
        config: Optional[Dict] = None,
        protocol_version: Optional[str] = None,
    ) -> str:
        config = config or {}
        requested_protocol = str(
            protocol_version
            or config.get(
                "protocol_version",
                SUPPORTED_PROTOCOL_VERSION,
            ),
        )
        if not self._is_compatible_protocol(requested_protocol):
            self._record_protocol_decision(
                "rejected",
                "incompatible_registration_protocol",
                agent_type=agent_type,
                requested_protocol=requested_protocol,
            )
            raise ValueError(
                f"Unsupported agent protocol version: {requested_protocol}",
            )

        agent_id = str(uuid.uuid4())
        timestamp = time.time()
        self._agents[agent_id] = {
            "id": agent_id,
            "name": name,
            "type": agent_type,
            "status": AgentStatus.PENDING.value,
            "config": config,
            "created_at": timestamp,
            "updated_at": timestamp,
            "version": requested_protocol,
            "protocol_version": requested_protocol,
            "protocol_generation": 1,
            "metrics": {"tasks_completed": 0, "errors": 0, "uptime": 0},
        }
        group = agent_type.split(".")[0]
        if group not in self._index:
            self._index[group] = []
        self._index[group].append(agent_id)
        self._invalidate_resolution_cache(agent_type)
        self._record_protocol_decision(
            "accepted",
            "registered",
            agent_id=agent_id,
            agent_type=agent_type,
            requested_protocol=requested_protocol,
        )
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
        self._invalidate_resolution_cache(self._agents[agent_id]["type"])
        return True

    def resolve(
        self,
        agent_type: str,
        required_protocol_version: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        required_protocol = str(
            required_protocol_version
            or SUPPORTED_PROTOCOL_VERSION
        )
        cache_key = (agent_type, required_protocol)
        cached_agent_id = self._resolution_cache.get(cache_key)
        cached_agent = self._agents.get(cached_agent_id or "")
        if cached_agent and self._agent_matches_resolution(
            cached_agent,
            agent_type,
            required_protocol,
        ):
            return cached_agent

        if not self._is_compatible_protocol(required_protocol):
            self._record_protocol_decision(
                "rejected",
                "incompatible_resolution_protocol",
                agent_type=agent_type,
                requested_protocol=required_protocol,
            )
            return None

        for agent in self._agents.values():
            if self._agent_matches_resolution(
                agent,
                agent_type,
                required_protocol,
            ):
                self._resolution_cache[cache_key] = agent["id"]
                return agent

        self._record_protocol_decision(
            "rejected",
            "no_compatible_handler",
            agent_type=agent_type,
            requested_protocol=required_protocol,
        )
        return None

    def negotiate_protocol_upgrade(
        self,
        agent_id: str,
        requested_protocol_version: str,
        actor: str = "agent_rpc_negotiation",
    ) -> bool:
        agent = self._agents.get(agent_id)
        if not agent:
            return False

        requested_protocol_version = str(requested_protocol_version)
        if not self._is_compatible_protocol(requested_protocol_version):
            self._record_protocol_decision(
                "rejected",
                "incompatible_upgrade_protocol",
                agent_id=agent_id,
                agent_type=agent["type"],
                requested_protocol=requested_protocol_version,
                current_protocol=agent.get("protocol_version"),
                status=agent["status"],
                actor=actor,
            )
            return False

        current_protocol = agent.get(
            "protocol_version",
            SUPPORTED_PROTOCOL_VERSION,
        )
        if requested_protocol_version == current_protocol:
            self._record_protocol_decision(
                "rejected",
                "duplicate_upgrade_protocol",
                agent_id=agent_id,
                agent_type=agent["type"],
                requested_protocol=requested_protocol_version,
                current_protocol=current_protocol,
                status=agent["status"],
                actor=actor,
            )
            return False

        if agent["status"] in UPGRADE_BLOCKING_STATUSES:
            self._record_protocol_decision(
                "deferred",
                "lifecycle_state_not_stable",
                agent_id=agent_id,
                agent_type=agent["type"],
                requested_protocol=requested_protocol_version,
                current_protocol=current_protocol,
                status=agent["status"],
                actor=actor,
            )
            return False

        agent["version"] = requested_protocol_version
        agent["protocol_version"] = requested_protocol_version
        agent["protocol_generation"] += 1
        agent["updated_at"] = time.time()
        self._invalidate_resolution_cache(agent["type"])
        self._record_protocol_decision(
            "accepted",
            "upgraded",
            agent_id=agent_id,
            agent_type=agent["type"],
            requested_protocol=requested_protocol_version,
            current_protocol=current_protocol,
            status=agent["status"],
            actor=actor,
        )
        return True

    def delete(self, agent_id: str) -> bool:
        if agent_id not in self._agents:
            return False
        agent = self._agents.pop(agent_id)
        group = agent["type"].split(".")[0]
        if group in self._index and agent_id in self._index[group]:
            self._index[group].remove(agent_id)
        self._invalidate_resolution_cache(agent["type"])
        return True

    def count(self) -> int:
        return len(self._agents)

    def audit_log(self) -> List[Dict[str, Any]]:
        return [dict(entry) for entry in self._audit_log]

    def _agent_matches_resolution(
        self,
        agent: Dict[str, Any],
        agent_type: str,
        required_protocol: str,
    ) -> bool:
        if agent["type"] != agent_type:
            return False
        if agent["status"] in {
            AgentStatus.FAILED.value,
            AgentStatus.TERMINATED.value,
        }:
            return False
        return self._protocol_major(
            agent.get("protocol_version"),
        ) == self._protocol_major(required_protocol)

    def _invalidate_resolution_cache(self, agent_type: str) -> None:
        stale_keys = [
            key
            for key, agent_id in self._resolution_cache.items()
            if self._agents.get(agent_id, {}).get("type") == agent_type
            or key[0] == agent_type
        ]
        for key in stale_keys:
            self._resolution_cache.pop(key, None)

    def _is_compatible_protocol(self, protocol_version: str) -> bool:
        try:
            return self._protocol_major(
                protocol_version,
            ) == self._protocol_major(SUPPORTED_PROTOCOL_VERSION)
        except ValueError:
            return False

    @staticmethod
    def _protocol_major(protocol_version: Optional[str]) -> int:
        if not protocol_version:
            raise ValueError("protocol version is required")
        parts = protocol_version.split(".")
        if len(parts) != 3 or not all(part.isdigit() for part in parts):
            raise ValueError(f"invalid protocol version: {protocol_version}")
        return int(parts[0])

    def _record_protocol_decision(
        self,
        decision: str,
        reason: str,
        *,
        agent_id: str = "",
        agent_type: str = "",
        requested_protocol: str = "",
        current_protocol: str = "",
        status: str = "",
        actor: str = "",
    ) -> None:
        entry = {
            "timestamp": time.time(),
            "decision": decision,
            "reason": reason,
            "agent_id": agent_id,
            "agent_type": agent_type,
            "requested_protocol": requested_protocol,
            "current_protocol": current_protocol,
            "status": status,
            "actor": actor,
        }
        self._audit_log.append(entry)
        metrics.increment(
            f"agent_registry.protocol_negotiation.{decision}",
        )
        logger.info(
            "agent_protocol_decision decision=%s reason=%s "
            "agent_id=%s agent_type=%s",
            decision,
            reason,
            agent_id,
            agent_type,
        )

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
