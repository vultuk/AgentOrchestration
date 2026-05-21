from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.middleware import AuthMiddleware, ErrorSanitizationMiddleware


def _client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def test_normal_request_marks_response_unsanitized():
    app = FastAPI()
    app.add_middleware(ErrorSanitizationMiddleware)

    @app.get("/ok")
    async def ok():
        return {"status": "ok"}

    response = _client(app).get("/ok")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["X-Error-Sanitized"] == "0"


def test_rejected_request_does_not_leak_request_material():
    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    app.add_middleware(ErrorSanitizationMiddleware)

    @app.get("/api/v2/private")
    async def private():
        return {"status": "private"}

    response = _client(app).get(
        "/api/v2/private?token=secret-token",
        headers={"X-Private-Header": "secret-header"},
    )

    assert response.status_code == 401
    assert response.headers["X-Error-Sanitized"] == "0"
    assert "secret-token" not in response.text
    assert "secret-header" not in response.text


def test_exception_response_and_logs_are_sanitized(caplog):
    app = FastAPI()
    app.add_middleware(ErrorSanitizationMiddleware)

    @app.get("/boom")
    async def boom():
        raise RuntimeError("database password=secret-password")

    with caplog.at_level("ERROR", logger="src.api.middleware"):
        response = _client(app).get("/boom")

    assert response.status_code == 500
    assert response.headers["X-Error-Sanitized"] == "1"
    assert response.json() == {"detail": "Internal server error"}
    assert "secret-password" not in response.text
    assert "secret-password" not in caplog.text
    assert "RuntimeError" not in response.text


def test_exception_state_does_not_leak_to_next_request():
    app = FastAPI()
    app.add_middleware(ErrorSanitizationMiddleware)

    @app.get("/boom")
    async def boom():
        raise RuntimeError("private runtime detail")

    @app.get("/ok")
    async def ok():
        return {"status": "ok"}

    client = _client(app)

    assert client.get("/boom").headers["X-Error-Sanitized"] == "1"
    response = client.get("/ok")

    assert response.status_code == 200
    assert response.headers["X-Error-Sanitized"] == "0"
    assert "private runtime detail" not in response.text
