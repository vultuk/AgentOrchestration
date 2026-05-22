import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.middleware import (
    AuthMiddleware,
    LoggingMiddleware,
    RequestContextMiddleware,
    get_request_agent_context,
)


def _client() -> TestClient:
    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    app.add_middleware(LoggingMiddleware)
    app.add_middleware(RequestContextMiddleware)

    @app.get("/api/v2/agents/{agent_id}/context")
    async def agent_context():
        return {"context": get_request_agent_context()}

    @app.get("/api/v2/agents/{agent_id}/boom")
    async def boom():
        raise RuntimeError("secret agent id leaked")

    @app.get("/context")
    async def context():
        return {"context": get_request_agent_context()}

    return TestClient(app, raise_server_exceptions=False)


def test_request_context_visible_during_normal_request_and_cleared_after():
    client = _client()

    response = client.get(
        "/api/v2/agents/agent-secret-123/context",
        headers={"Authorization": "Bearer token"},
    )

    assert response.status_code == 200
    assert response.headers["X-Agent-Context-Cleared"] == "true"
    assert response.json()["context"] == {
        "agent_ref": "563b7e60e24d",
        "source": "path",
    }
    assert get_request_agent_context() is None

    follow_up = client.get("/context")
    assert follow_up.status_code == 200
    assert follow_up.json()["context"] is None


def test_rejected_request_clears_context_and_sanitizes_logs(caplog):
    client = _client()
    caplog.set_level(logging.INFO, logger="src.api.middleware")

    response = client.post("/api/v2/agents/agent-secret-123/start")

    assert response.status_code == 401
    assert response.headers["X-Agent-Context-Cleared"] == "true"
    assert "agent-secret-123" not in response.text
    assert get_request_agent_context() is None

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "/api/v2/agents/{agent_id}/start" in messages
    assert "agent-secret-123" not in messages


def test_exception_request_clears_context_and_sanitizes_response(caplog):
    client = _client()
    caplog.set_level(logging.INFO, logger="src.api.middleware")

    response = client.get(
        "/api/v2/agents/agent-secret-123/boom",
        headers={"Authorization": "Bearer token"},
    )

    assert response.status_code == 500
    assert response.headers["X-Agent-Context-Cleared"] == "true"
    assert response.text == "Internal Server Error"
    assert get_request_agent_context() is None

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "/api/v2/agents/{agent_id}/boom" in messages
    assert "agent-secret-123" not in messages
    assert "secret agent id leaked" not in messages
