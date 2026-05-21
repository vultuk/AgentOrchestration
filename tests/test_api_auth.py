"""Authentication tests for API documentation endpoints."""

import time

from fastapi.testclient import TestClient

from src.api.server import create_app


def make_client():
    app = create_app()
    app.state.auth_tokens = {
        "docs-admin": {
            "scopes": ["docs:read"],
            "workspace_roles": {"default": "admin"},
            "expires_at": time.time() + 3600,
        },
        "browser-admin": {
            "scopes": ["docs:read"],
            "workspace_roles": {"default": "maintainer"},
            "expires_at": time.time() + 3600,
        },
        "stale": {
            "scopes": ["docs:read"],
            "workspace_roles": {"default": "admin"},
            "expires_at": time.time() - 1,
        },
        "revoked": {
            "scopes": ["docs:read"],
            "workspace_roles": {"default": "admin"},
            "revoked": True,
            "expires_at": time.time() + 3600,
        },
        "wrong-scope": {
            "scopes": ["agents:read"],
            "workspace_roles": {"default": "admin"},
            "expires_at": time.time() + 3600,
        },
        "wrong-role": {
            "scopes": ["docs:read"],
            "workspace_roles": {"default": "viewer"},
            "expires_at": time.time() + 3600,
        },
    }
    return TestClient(app)


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def test_openapi_schema_rejects_anonymous_and_malformed_credentials():
    client = make_client()

    assert client.get("/api/openapi.json").status_code == 401
    assert client.get(
        "/api/openapi.json",
        headers={"Authorization": "Basic docs-admin"},
    ).status_code == 401
    assert client.get(
        "/api/openapi.json",
        headers={"Authorization": "Bearer "},
    ).status_code == 401


def test_openapi_schema_rejects_stale_revoked_and_unknown_tokens():
    client = make_client()

    assert client.get(
        "/api/openapi.json",
        headers=bearer("stale"),
    ).status_code == 401
    assert client.get(
        "/api/openapi.json",
        headers=bearer("revoked"),
    ).status_code == 401
    assert client.get(
        "/api/openapi.json",
        headers=bearer("missing"),
    ).status_code == 401


def test_openapi_schema_rejects_insufficient_scope_and_workspace_role():
    client = make_client()

    assert client.get(
        "/api/openapi.json",
        headers=bearer("wrong-scope"),
    ).status_code == 403
    assert client.get(
        "/api/openapi.json",
        headers=bearer("wrong-role"),
    ).status_code == 403


def test_authorized_token_client_can_read_openapi_schema():
    client = make_client()

    response = client.get("/api/openapi.json", headers=bearer("docs-admin"))

    assert response.status_code == 200
    assert "/api/v2/agents" in response.json()["paths"]


def test_authorized_browser_client_can_read_docs_and_openapi_schema():
    client = make_client()
    client.cookies.set("ao_session", "browser-admin")

    docs_response = client.get("/api/docs")
    schema_response = client.get("/api/openapi.json")

    assert docs_response.status_code == 200
    assert schema_response.status_code == 200
    assert "/api/v2/agents" in schema_response.json()["paths"]


def test_legacy_openapi_schema_path_fails_closed():
    client = make_client()

    assert client.get("/openapi.json").status_code == 401
