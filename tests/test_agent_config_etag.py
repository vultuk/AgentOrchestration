from fastapi.testclient import TestClient
import pytest

from src.agent.registry import AgentRegistry
from src.api import routes
from src.api.agent_config import (
    AgentConfigError,
    read_agent_config,
    update_agent_config,
)
from src.api.server import create_app


AUTH = {"Authorization": "Bearer test-token"}


class LookupGuardRegistry:
    def get(self, agent_id):
        raise AssertionError("lookup should not run for malformed input")

    def update_config(self, agent_id, config, expected_version):
        raise AssertionError("mutation should not run for malformed input")


def make_client():
    registry = AgentRegistry()
    routes.registry = registry
    return TestClient(create_app()), registry


def test_read_agent_config_returns_current_etag():
    registry = AgentRegistry()
    agent_id = registry.register("worker", "worker.processor", {"limit": 1})

    result = read_agent_config(registry, agent_id)

    assert result["config"] == {"limit": 1}
    assert result["version"] == 1
    assert result["etag"] == '"1"'


def test_update_agent_config_advances_etag_and_preserves_existing_snapshot():
    registry = AgentRegistry()
    agent_id = registry.register("worker", "worker.processor", {"limit": 1})

    result = update_agent_config(
        registry,
        agent_id,
        {"config": {"limit": 2}},
        '"1"',
    )

    assert result["config"] == {"limit": 2}
    assert result["version"] == 2
    assert result["etag"] == '"2"'
    assert registry.get(agent_id)["config"] == {"limit": 2}


def test_registry_update_config_rejects_stale_version_atomically():
    registry = AgentRegistry()
    agent_id = registry.register("worker", "worker.processor", {"limit": 1})

    updated = registry.update_config(agent_id, {"limit": 2}, 1)
    stale = registry.update_config(agent_id, {"limit": 3}, 1)

    assert updated["config_version"] == 2
    assert stale is None
    assert registry.get(agent_id)["config"] == {"limit": 2}
    assert registry.get(agent_id)["config_version"] == 2


def test_stale_etag_cannot_overwrite_newer_config():
    registry = AgentRegistry()
    agent_id = registry.register("worker", "worker.processor", {"limit": 1})
    update_agent_config(registry, agent_id, {"config": {"limit": 2}}, '"1"')

    with pytest.raises(AgentConfigError) as excinfo:
        update_agent_config(
            registry,
            agent_id,
            {"config": {"limit": 3}},
            '"1"',
        )

    assert excinfo.value.status_code == 412
    assert excinfo.value.code == "stale_if_match"
    assert registry.get(agent_id)["config"] == {"limit": 2}
    assert registry.get(agent_id)["config_version"] == 2


def test_malformed_agent_id_is_rejected_before_lookup():
    with pytest.raises(AgentConfigError) as excinfo:
        read_agent_config(LookupGuardRegistry(), "not-a-uuid")

    assert excinfo.value.status_code == 400
    assert excinfo.value.code == "malformed_agent_id"


def test_malformed_body_is_rejected_before_lookup():
    registry = LookupGuardRegistry()
    agent_id = "12345678-1234-5678-1234-567812345678"

    with pytest.raises(AgentConfigError) as excinfo:
        update_agent_config(registry, agent_id, {"config": "bad"}, '"1"')

    assert excinfo.value.status_code == 400
    assert excinfo.value.code == "malformed_config"


def test_missing_if_match_is_rejected_before_lookup():
    registry = LookupGuardRegistry()
    agent_id = "12345678-1234-5678-1234-567812345678"

    with pytest.raises(AgentConfigError) as excinfo:
        update_agent_config(registry, agent_id, {"config": {}}, None)

    assert excinfo.value.status_code == 428
    assert excinfo.value.code == "missing_if_match"


def test_malformed_if_match_is_rejected_before_lookup():
    registry = LookupGuardRegistry()
    agent_id = "12345678-1234-5678-1234-567812345678"

    with pytest.raises(AgentConfigError) as excinfo:
        update_agent_config(registry, agent_id, {"config": {}}, "1")

    assert excinfo.value.status_code == 400
    assert excinfo.value.code == "malformed_if_match"


def test_authorized_route_update_returns_next_etag():
    client, registry = make_client()
    agent_id = registry.register("worker", "worker.processor", {"limit": 1})

    response = client.put(
        f"/api/v2/agents/{agent_id}/config",
        headers={**AUTH, "If-Match": '"1"'},
        json={"config": {"limit": 2}},
    )

    assert response.status_code == 200
    assert response.headers["etag"] == '"2"'
    assert response.json()["config"] == {"limit": 2}
    assert response.json()["config_version"] == 2


def test_route_rejects_stale_update_without_mutation():
    client, registry = make_client()
    agent_id = registry.register("worker", "worker.processor", {"limit": 1})

    first = client.patch(
        f"/api/v2/agents/{agent_id}/config",
        headers={**AUTH, "If-Match": '"1"'},
        json={"config": {"limit": 2}},
    )
    stale = client.patch(
        f"/api/v2/agents/{agent_id}/config",
        headers={**AUTH, "If-Match": '"1"'},
        json={"config": {"limit": 3}},
    )

    assert first.status_code == 200
    assert stale.status_code == 412
    assert stale.json()["detail"]["code"] == "stale_if_match"
    assert registry.get(agent_id)["config"] == {"limit": 2}


def test_unauthorized_route_update_is_rejected_before_mutation():
    client, registry = make_client()
    agent_id = registry.register("worker", "worker.processor", {"limit": 1})

    response = client.put(
        f"/api/v2/agents/{agent_id}/config",
        headers={"If-Match": '"1"'},
        json={"config": {"limit": 2}},
    )

    assert response.status_code == 401
    assert registry.get(agent_id)["config"] == {"limit": 1}
    assert registry.get(agent_id)["config_version"] == 1


def test_malformed_route_update_is_rejected_before_mutation():
    client, registry = make_client()
    agent_id = registry.register("worker", "worker.processor", {"limit": 1})

    response = client.put(
        f"/api/v2/agents/{agent_id}/config",
        headers={**AUTH, "If-Match": '"1"'},
        json={"config": "not-an-object"},
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "malformed_config"
    assert registry.get(agent_id)["config"] == {"limit": 1}


def test_route_returns_current_config_etag():
    client, registry = make_client()
    agent_id = registry.register("worker", "worker.processor", {"limit": 1})

    response = client.get(f"/api/v2/agents/{agent_id}/config", headers=AUTH)

    assert response.status_code == 200
    assert response.headers["etag"] == '"1"'
    assert response.json()["config"] == {"limit": 1}
