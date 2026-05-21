import time

import pytest
from fastapi.testclient import TestClient

from src.api.server import create_app


@pytest.fixture
def client():
    now = time.time()
    app = create_app(
        {
            "auth_tokens": {
                "admin-token": {
                    "subject": "admin-user",
                    "workspace_role": "admin",
                    "scopes": ["agents:read", "agents:write"],
                    "expires_at": now + 3600,
                },
                "viewer-token": {
                    "subject": "read-only-user",
                    "workspace_role": "viewer",
                    "scopes": ["agents:read"],
                    "expires_at": now + 3600,
                },
                "revoked-token": {
                    "subject": "revoked-user",
                    "workspace_role": "admin",
                    "scopes": ["agents:read", "agents:write"],
                    "revoked": True,
                },
                "stale-token": {
                    "subject": "expired-user",
                    "workspace_role": "admin",
                    "scopes": ["agents:read", "agents:write"],
                    "expires_at": now - 1,
                }
            }
        }
    )
    with TestClient(app) as test_client:
        yield test_client


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_trailing_slash_denies_anonymous_before_redirect(client):
    response = client.get("/api/v2/agents/", follow_redirects=False)

    assert response.status_code == 401
    assert response.text == "Unauthorized"


def test_trailing_slash_protected_route_denies_malformed_and_unknown_tokens(
    client,
):
    malformed = client.get(
        "/api/v2/agents/",
        headers={"Authorization": "Bearer "},
        follow_redirects=False,
    )
    unknown = client.get(
        "/api/v2/agents/",
        headers=bearer("unknown-token"),
        follow_redirects=False,
    )

    assert malformed.status_code == 401
    assert unknown.status_code == 401


def test_trailing_slash_denies_revoked_and_stale_tokens(client):
    revoked = client.get(
        "/api/v2/agents/",
        headers=bearer("revoked-token"),
        follow_redirects=False,
    )
    stale = client.get(
        "/api/v2/agents/",
        headers=bearer("stale-token"),
        follow_redirects=False,
    )

    assert revoked.status_code == 401
    assert stale.status_code == 401


def test_trailing_slash_protected_route_accepts_browser_session_cookie(client):
    client.cookies.set("ao_session", "viewer-token")

    response = client.get(
        "/api/v2/agents/",
    )

    assert response.status_code == 200
    assert response.json()["agents"] == []


def test_trailing_slash_rejects_stale_browser_session_before_redirect(client):
    client.cookies.set("ao_session", "stale-token")

    response = client.get(
        "/api/v2/agents/",
        follow_redirects=False,
    )

    assert response.status_code == 401


def test_malformed_bearer_does_not_fall_back_to_valid_session_cookie(client):
    client.cookies.set("ao_session", "viewer-token")

    response = client.get(
        "/api/v2/agents/",
        headers={"Authorization": "Bearer "},
        follow_redirects=False,
    )

    assert response.status_code == 401


def test_trailing_slash_write_denies_read_only_principal_before_redirect(
    client,
):
    response = client.post(
        "/api/v2/agents/",
        params={"name": "blocked-agent", "agent_type": "worker.processor"},
        headers=bearer("viewer-token"),
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert response.text == "Forbidden"


def test_authorized_principals_keep_existing_read_and_write_workflows(client):
    create_response = client.post(
        "/api/v2/agents/",
        params={"name": "allowed-agent", "agent_type": "worker.processor"},
        headers=bearer("admin-token"),
    )
    list_response = client.get(
        "/api/v2/agents/",
        headers=bearer("viewer-token"),
    )

    assert create_response.status_code == 200
    assert create_response.json()["status"] == "registered"
    assert list_response.status_code == 200
    assert list_response.json()["agents"][0]["name"] == "allowed-agent"
