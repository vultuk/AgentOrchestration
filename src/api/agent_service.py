"""Shared validation and service helpers for agent API routes."""

from typing import Dict, Optional
from uuid import UUID

from fastapi import HTTPException

from src.agent import AgentRegistry, AgentStatus


def validation_error(
    field: str,
    message: str,
    **extra: object,
) -> HTTPException:
    detail = {
        "code": "validation_error",
        "field": field,
        "message": message,
    }
    detail.update(extra)
    return HTTPException(status_code=400, detail=detail)


def require_text(value: Optional[str], field: str) -> str:
    if value is None or not value.strip():
        raise validation_error(field, f"{field} is required")
    return value.strip()


def parse_agent_status(status: Optional[str]) -> Optional[AgentStatus]:
    if not status:
        return None
    try:
        return AgentStatus(status)
    except ValueError as exc:
        raise validation_error(
            "status",
            "status must be a known agent status",
            allowed=[item.value for item in AgentStatus],
        ) from exc


def parse_agent_id(agent_id: str) -> str:
    try:
        return str(UUID(agent_id))
    except ValueError as exc:
        raise validation_error(
            "agent_id",
            "agent_id must be a valid UUID",
        ) from exc


def list_agents(
    registry: AgentRegistry,
    status: Optional[str] = None,
    group: Optional[str] = None,
) -> Dict:
    status_filter = parse_agent_status(status)
    return {"agents": registry.list(status=status_filter, group=group)}


def register_agent(
    registry: AgentRegistry,
    name: Optional[str],
    agent_type: Optional[str],
    config: Optional[Dict] = None,
) -> Dict:
    valid_name = require_text(name, "name")
    valid_agent_type = require_text(agent_type, "agent_type")
    agent_id = registry.register(valid_name, valid_agent_type, config)
    return {"agent_id": agent_id, "status": "registered"}


def get_agent(registry: AgentRegistry, agent_id: str) -> Dict:
    parsed_agent_id = parse_agent_id(agent_id)
    agent = registry.get(parsed_agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


def delete_agent(registry: AgentRegistry, agent_id: str) -> Dict:
    parsed_agent_id = parse_agent_id(agent_id)
    if not registry.delete(parsed_agent_id):
        raise HTTPException(status_code=404, detail="Agent not found")
    return {"status": "deleted"}


def start_agent(registry: AgentRegistry, agent_id: str) -> Dict:
    parsed_agent_id = parse_agent_id(agent_id)
    if not registry.update_status(parsed_agent_id, AgentStatus.RUNNING):
        raise HTTPException(status_code=404, detail="Agent not found")
    return {"status": "started"}


def stop_agent(registry: AgentRegistry, agent_id: str) -> Dict:
    parsed_agent_id = parse_agent_id(agent_id)
    if not registry.update_status(parsed_agent_id, AgentStatus.PAUSED):
        raise HTTPException(status_code=404, detail="Agent not found")
    return {"status": "stopped"}
