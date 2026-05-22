from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from src.api.auth import AuthTokenStore, make_principal
from src.api.server import create_app


def _client_with_store(store: AuthTokenStore) -> TestClient:
    app = create_app()
    app.state.auth_token_store = store
    return TestClient(app, follow_redirects=False)


def _auth_store() -> AuthTokenStore:
    now = datetime.now(timezone.utc)
    return AuthTokenStore({
        "token-valid": make_principal(
            "valid-token-client",
            {"agents:read", "agents:write"},
            {"default": "operator"},
        ),
        "session-valid": make_principal(
            "valid-browser-client",
            {"agents:read", "agents:write"},
            {"default": "admin"},
            client_type="browser",
        ),
        "token-stale": make_principal(
            "stale-client",
            {"agents:read", "agents:write"},
            {"default": "operator"},
            expires_at=now - timedelta(minutes=1),
        ),
        "token-revoked": make_principal(
            "revoked-client",
            {"agents:read", "agents:write"},
            {"default": "operator"},
            revoked=True,
        ),
        "token-readonly": make_principal(
            "readonly-client",
            {"agents:read"},
            {"default": "viewer"},
        ),
        "token-wrong-workspace": make_principal(
            "wrong-workspace-client",
            {"agents:read", "agents:write"},
            {"other-workspace": "admin"},
        ),
    })


class TestProtectedRouteAuth:
    def test_anonymous_trailing_slash_request_denied_before_redirect(self):
        client = _client_with_store(_auth_store())

        response = client.get("/api/v2/agents/")

        assert response.status_code == 401
        assert "location" not in response.headers

    def test_stale_token_trailing_slash_request_denied_before_redirect(self):
        client = _client_with_store(_auth_store())

        response = client.get(
            "/api/v2/agents/",
            headers={"Authorization": "Bearer token-stale"},
        )

        assert response.status_code == 401
        assert "location" not in response.headers

    def test_unknown_token_trailing_slash_request_denied_before_redirect(self):
        client = _client_with_store(_auth_store())

        response = client.get(
            "/api/v2/agents/",
            headers={"Authorization": "Bearer token-unknown"},
        )

        assert response.status_code == 401
        assert "location" not in response.headers

    def test_blank_bearer_token_denied(self):
        client = _client_with_store(_auth_store())

        response = client.get(
            "/api/v2/agents",
            headers={"Authorization": "Bearer "},
        )

        assert response.status_code == 401

    def test_stale_browser_session_denied_before_redirect(self):
        client = _client_with_store(_auth_store())
        client.cookies.set("ao_session", "token-stale")

        response = client.get("/api/v2/agents/")

        assert response.status_code == 401
        assert "location" not in response.headers

    def test_revoked_token_denied(self):
        client = _client_with_store(_auth_store())

        response = client.get(
            "/api/v2/agents",
            headers={"Authorization": "Bearer token-revoked"},
        )

        assert response.status_code == 401

    def test_insufficient_scope_denied_before_mutation(self):
        client = _client_with_store(_auth_store())

        response = client.post(
            "/api/v2/agents",
            params={"name": "blocked-agent", "agent_type": "worker"},
            headers={"Authorization": "Bearer token-readonly"},
        )

        assert response.status_code == 403

    def test_wrong_workspace_role_denied(self):
        client = _client_with_store(_auth_store())

        response = client.get(
            "/api/v2/agents",
            headers={
                "Authorization": "Bearer token-wrong-workspace",
                "X-Workspace-ID": "default",
            },
        )

        assert response.status_code == 403

    def test_authorized_token_client_can_complete_protected_workflow(self):
        client = _client_with_store(_auth_store())
        headers = {
            "Authorization": "Bearer token-valid",
            "X-Workspace-ID": "default",
        }

        created = client.post(
            "/api/v2/agents",
            params={"name": "worker", "agent_type": "worker.fast"},
            headers=headers,
        )
        listed = client.get("/api/v2/agents", headers=headers)

        assert created.status_code == 200
        assert created.json()["status"] == "registered"
        assert listed.status_code == 200
        assert any(
            agent["id"] == created.json()["agent_id"]
            for agent in listed.json()["agents"]
        )

    def test_authorized_browser_session_can_write_protected_route(self):
        client = _client_with_store(_auth_store())
        client.cookies.set("ao_session", "session-valid")

        created = client.post(
            "/api/v2/agents",
            params={"name": "browser-worker", "agent_type": "worker.browser"},
            headers={"X-Workspace-ID": "default"},
        )
        listed = client.get(
            "/api/v2/agents",
            headers={"X-Workspace-ID": "default"},
        )

        assert created.status_code == 200
        assert created.json()["status"] == "registered"
        assert listed.status_code == 200
        assert any(
            agent["id"] == created.json()["agent_id"]
            for agent in listed.json()["agents"]
        )

    def test_authorized_browser_session_can_read_protected_route(self):
        client = _client_with_store(_auth_store())
        client.cookies.set("ao_session", "session-valid")

        response = client.get(
            "/api/v2/agents",
            headers={"X-Workspace-ID": "default"},
        )

        assert response.status_code == 200
        assert "agents" in response.json()
