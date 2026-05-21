import base64
import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from src.api.auth import AuthService
from src.api.server import create_app


SECRET = "test-secret"
NOW = 1_700_000_000


def make_token(**claims):
    payload = {
        "sub": "worker-1",
        "aud": "agent-workers",
        "exp": NOW + 60,
        "scope": "agents:read agents:write",
        "workspace_roles": {"workspace-a": ["viewer", "worker"]},
        **claims,
    }
    header = {"alg": "HS256", "typ": "JWT"}
    signing_input = ".".join([b64_json(header), b64_json(payload)]).encode(
        "ascii"
    )
    signature = hmac.new(
        SECRET.encode("utf-8"),
        signing_input,
        hashlib.sha256,
    ).digest()
    return f"{signing_input.decode('ascii')}.{b64_bytes(signature)}"


def b64_json(value):
    return b64_bytes(json.dumps(value, separators=(",", ":")).encode("utf-8"))


def b64_bytes(value):
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def client():
    auth_service = AuthService(
        secret=SECRET,
        expected_audience="agent-workers",
        revoked_token_ids={"revoked-token"},
        now=lambda: NOW,
    )
    return TestClient(create_app({"auth_service": auth_service}))


def client_with_strict_staleness():
    auth_service = AuthService(
        secret=SECRET,
        expected_audience="agent-workers",
        expected_issuer="agent-control-plane",
        max_token_age_seconds=30,
        now=lambda: NOW,
    )
    return TestClient(create_app({"auth_service": auth_service}))


def headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "X-Workspace-ID": "workspace-a",
    }


def test_agent_worker_rejects_anonymous_request():
    response = client().get("/api/v2/agents")

    assert response.status_code == 401


def test_agent_worker_rejects_malformed_token():
    response = client().get("/api/v2/agents", headers=headers("not-a-jwt"))

    assert response.status_code == 401


def test_agent_worker_rejects_stale_token():
    token = make_token(exp=NOW - 1)

    response = client().get("/api/v2/agents", headers=headers(token))

    assert response.status_code == 401


def test_agent_worker_rejects_revoked_token():
    token = make_token(jti="revoked-token")

    response = client().get("/api/v2/agents", headers=headers(token))

    assert response.status_code == 401


def test_agent_worker_rejects_wrong_jwt_audience():
    token = make_token(aud="browser")

    response = client().get("/api/v2/agents", headers=headers(token))

    assert response.status_code == 401


def test_agent_worker_rejects_wrong_jwt_issuer_when_configured():
    token = make_token(iss="unknown-issuer", iat=NOW)

    response = client_with_strict_staleness().get(
        "/api/v2/agents",
        headers=headers(token),
    )

    assert response.status_code == 401


def test_agent_worker_rejects_token_older_than_configured_max_age():
    token = make_token(iss="agent-control-plane", iat=NOW - 31)

    response = client_with_strict_staleness().get(
        "/api/v2/agents",
        headers=headers(token),
    )

    assert response.status_code == 401


def test_agent_worker_accepts_recent_token_when_strict_staleness_configured():
    token = make_token(iss="agent-control-plane", iat=NOW - 10)

    response = client_with_strict_staleness().get(
        "/api/v2/agents",
        headers=headers(token),
    )

    assert response.status_code == 200


def test_agent_worker_rejects_insufficient_scope():
    token = make_token(scope="agents:read")

    response = client().post(
        "/api/v2/agents",
        params={"name": "worker", "agent_type": "worker.processor"},
        headers=headers(token),
    )

    assert response.status_code == 403


def test_agent_worker_rejects_insufficient_workspace_role():
    token = make_token(workspace_roles={"workspace-a": ["viewer"]})

    response = client().post(
        "/api/v2/agents",
        params={"name": "worker", "agent_type": "worker.processor"},
        headers=headers(token),
    )

    assert response.status_code == 403


def test_authorized_agent_worker_can_complete_workflow():
    test_client = client()
    token = make_token()

    created = test_client.post(
        "/api/v2/agents",
        params={"name": "worker", "agent_type": "worker.processor"},
        headers=headers(token),
    )
    assert created.status_code == 200
    agent_id = created.json()["agent_id"]

    started = test_client.post(
        f"/api/v2/agents/{agent_id}/start",
        headers=headers(token),
    )
    assert started.status_code == 200
    assert started.json()["status"] == "started"

    listed = test_client.get("/api/v2/agents", headers=headers(token))
    assert listed.status_code == 200
    assert listed.json()["agents"][0]["status"] == "running"


def test_browser_session_cookie_uses_same_agent_worker_checks():
    token = make_token()
    test_client = client()
    test_client.cookies.set("ao_session", token)

    response = test_client.get(
        "/api/v2/agents",
        headers={"X-Workspace-ID": "workspace-a"},
    )

    assert response.status_code == 200
