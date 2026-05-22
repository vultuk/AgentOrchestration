from fastapi.testclient import TestClient

from src.api import routes
from src.api.server import create_app
from src.agent import AgentStatus


class RecordingRegistry:
    def __init__(self):
        self.list_calls = []
        self.get_calls = []
        self.delete_calls = []
        self.update_status_calls = []
        self.register_calls = []
        self.count_calls = 0

    def list(self, status=None, group=None):
        self.list_calls.append({"status": status, "group": group})
        return [{"id": "agent-1", "status": status.value if status else "any"}]

    def get(self, agent_id):
        self.get_calls.append(agent_id)
        return None

    def register(self, name, agent_type, config=None):
        self.register_calls.append(
            {"name": name, "agent_type": agent_type, "config": config},
        )
        return "agent-id"

    def delete(self, agent_id):
        self.delete_calls.append(agent_id)
        return False

    def update_status(self, agent_id, status):
        self.update_status_calls.append(
            {"agent_id": agent_id, "status": status},
        )
        return False

    def count(self):
        self.count_calls += 1
        return 7


def client_with_registry(monkeypatch):
    registry = RecordingRegistry()
    monkeypatch.setattr(routes, "registry", registry)
    return TestClient(create_app()), registry


def auth_headers():
    return {"Authorization": "Bearer test-token"}


def test_authorized_agents_status_filter_calls_registry(monkeypatch):
    client, registry = client_with_registry(monkeypatch)

    response = client.get(
        "/api/v2/agents?status=pending",
        headers=auth_headers(),
    )

    assert response.status_code == 200
    assert registry.list_calls == [
        {"status": AgentStatus.PENDING, "group": None},
    ]


def test_unauthorized_agents_status_filter_stops_before_lookup(monkeypatch):
    client, registry = client_with_registry(monkeypatch)

    response = client.get("/api/v2/agents?status=pending")

    assert response.status_code == 401
    assert registry.list_calls == []


def test_malformed_agents_status_returns_400_before_lookup(monkeypatch):
    client, registry = client_with_registry(monkeypatch)

    response = client.get(
        "/api/v2/agents?status=started",
        headers=auth_headers(),
    )

    assert response.status_code == 400
    assert response.json() == {
        "detail": {
            "code": "validation_error",
            "field": "status",
            "message": "status must be a known agent status",
            "allowed": [
                "pending",
                "running",
                "paused",
                "stopped",
                "failed",
                "terminated",
            ],
        },
    }
    assert registry.list_calls == []


def test_malformed_agent_id_returns_400_before_lookup(monkeypatch):
    client, registry = client_with_registry(monkeypatch)

    response = client.get(
        "/api/v2/agents/not-a-uuid",
        headers=auth_headers(),
    )

    assert response.status_code == 400
    assert response.json() == {
        "detail": {
            "code": "validation_error",
            "field": "agent_id",
            "message": "agent_id must be a valid UUID",
        },
    }
    assert registry.get_calls == []


def test_malformed_agent_id_returns_400_before_mutation(monkeypatch):
    client, registry = client_with_registry(monkeypatch)

    response = client.delete(
        "/api/v2/agents/not-a-uuid",
        headers=auth_headers(),
    )

    assert response.status_code == 400
    assert response.json() == {
        "detail": {
            "code": "validation_error",
            "field": "agent_id",
            "message": "agent_id must be a valid UUID",
        },
    }
    assert registry.delete_calls == []
    assert registry.update_status_calls == []


def test_malformed_agent_id_returns_400_before_status_mutation(monkeypatch):
    client, registry = client_with_registry(monkeypatch)

    response = client.post(
        "/api/v2/agents/not-a-uuid/start",
        headers=auth_headers(),
    )

    assert response.status_code == 400
    assert response.json() == {
        "detail": {
            "code": "validation_error",
            "field": "agent_id",
            "message": "agent_id must be a valid UUID",
        },
    }
    assert registry.update_status_calls == []


def test_blank_registration_returns_400_before_mutation(monkeypatch):
    client, registry = client_with_registry(monkeypatch)

    response = client.post(
        "/api/v2/agents?name=  &agent_type=worker",
        headers=auth_headers(),
    )

    assert response.status_code == 400
    assert response.json() == {
        "detail": {
            "code": "validation_error",
            "field": "name",
            "message": "name is required",
        },
    }
    assert registry.register_calls == []


def test_static_count_route_is_not_treated_as_malformed_agent_id(monkeypatch):
    client, registry = client_with_registry(monkeypatch)

    response = client.get("/api/v2/agents/count", headers=auth_headers())

    assert response.status_code == 200
    assert response.json() == {"count": 7}
    assert registry.count_calls == 1
    assert registry.get_calls == []
