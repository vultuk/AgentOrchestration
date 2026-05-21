"""Shared service for optimistic agent config updates."""

import re
import uuid
from typing import Any, Dict, Optional

from src.agent import AgentRegistry


_ETAG_PATTERN = re.compile(r'^"([1-9][0-9]*)"$')


class AgentConfigError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message

    @property
    def detail(self) -> Dict[str, str]:
        return {"code": self.code, "message": self.message}


def format_etag(version: int) -> str:
    return f'"{version}"'


def _validate_agent_id(agent_id: str) -> None:
    try:
        uuid.UUID(agent_id)
    except (TypeError, ValueError):
        raise AgentConfigError(
            400,
            "malformed_agent_id",
            "agent_id must be a UUID",
        )


def _parse_if_match(value: Optional[str]) -> int:
    if value is None:
        raise AgentConfigError(
            428,
            "missing_if_match",
            "If-Match header is required",
        )

    match = _ETAG_PATTERN.fullmatch(value.strip())
    if not match:
        raise AgentConfigError(
            400,
            "malformed_if_match",
            'If-Match must be a quoted positive integer ETag such as "1"',
        )
    return int(match.group(1))


def _extract_config(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise AgentConfigError(
            400,
            "malformed_body",
            "request body must be a JSON object",
        )

    config = payload.get("config")
    if not isinstance(config, dict):
        raise AgentConfigError(
            400,
            "malformed_config",
            "request body must include a config object",
        )
    return dict(config)


def read_agent_config(
    registry: AgentRegistry,
    agent_id: str,
) -> Dict[str, Any]:
    _validate_agent_id(agent_id)

    agent = registry.get(agent_id)
    if agent is None:
        raise AgentConfigError(404, "agent_not_found", "Agent not found")

    version = int(agent.get("config_version", 1))
    return {
        "agent_id": agent_id,
        "config": agent.get("config", {}),
        "version": version,
        "etag": format_etag(version),
    }


def update_agent_config(
    registry: AgentRegistry,
    agent_id: str,
    payload: Any,
    if_match: Optional[str],
) -> Dict[str, Any]:
    _validate_agent_id(agent_id)
    config = _extract_config(payload)
    expected_version = _parse_if_match(if_match)

    updated = registry.update_config(agent_id, config, expected_version)
    if updated is None:
        if registry.get(agent_id) is None:
            raise AgentConfigError(404, "agent_not_found", "Agent not found")
        raise AgentConfigError(
            412,
            "stale_if_match",
            "If-Match does not match the current config ETag",
        )

    updated_version = int(updated.get("config_version", 1))
    return {
        "agent_id": agent_id,
        "config": updated.get("config", {}),
        "version": updated_version,
        "etag": format_etag(updated_version),
    }
