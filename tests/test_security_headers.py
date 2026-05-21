import logging

from fastapi import Request
from fastapi.testclient import TestClient

from src.api.middleware import SECURITY_HEADERS
from src.api.server import create_app


def build_test_client():
    app = create_app()

    @app.get("/api/v2/security-ok")
    async def security_ok():
        return {"status": "ok"}

    @app.get("/api/v2/security-error")
    async def security_error(request: Request):
        request.state.security_context = {"token": "super-secret-token"}
        raise RuntimeError("super-secret-token should not leak")

    return TestClient(app)


def assert_security_headers(response):
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value


def test_security_headers_are_added_to_normal_responses():
    client = build_test_client()

    response = client.get(
        "/api/v2/security-ok",
        headers={"Authorization": "Bearer test-token"},
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert_security_headers(response)


def test_security_headers_are_added_to_rejected_responses():
    client = build_test_client()

    response = client.get("/api/v2/security-ok")

    assert response.status_code == 401
    assert response.text == "Unauthorized"
    assert_security_headers(response)


def test_exception_response_is_sanitized_and_has_security_headers(caplog):
    client = build_test_client()

    with caplog.at_level(logging.ERROR, logger="src.api.middleware"):
        response = client.get(
            "/api/v2/security-error?token=super-secret-token",
            headers={"Authorization": "Bearer test-token"},
        )

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error"}
    assert_security_headers(response)
    assert "super-secret-token" not in response.text
    assert "super-secret-token" not in caplog.text


def test_request_state_is_cleared_after_exception_path():
    client = build_test_client()
    headers = {"Authorization": "Bearer test-token"}

    error_response = client.get("/api/v2/security-error", headers=headers)
    next_response = client.get("/api/v2/security-ok", headers=headers)

    assert error_response.status_code == 500
    assert next_response.status_code == 200
    assert next_response.json() == {"status": "ok"}
    assert_security_headers(next_response)
