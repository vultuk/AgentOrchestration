import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from src.api.middleware import (
    UPLOAD_BOUNDARY_HEADER,
    UploadBoundaryMiddleware,
    get_current_upload_boundary,
)


def build_client(endpoint):
    app = Starlette(routes=[Route("/upload", endpoint, methods=["POST"])])
    app.add_middleware(UploadBoundaryMiddleware)
    return TestClient(app)


def test_valid_multipart_boundary_reaches_handler_and_clears_state():
    async def endpoint(request):
        await request.body()
        return JSONResponse({"boundary": get_current_upload_boundary()})

    client = build_client(endpoint)
    response = client.post(
        "/upload",
        content=b"--abc123\r\n\r\n--abc123--\r\n",
        headers={"content-type": "multipart/form-data; boundary=abc123"},
    )

    assert response.status_code == 200
    assert response.json() == {"boundary": "abc123"}
    assert response.headers[UPLOAD_BOUNDARY_HEADER] == "accepted"
    assert get_current_upload_boundary() is None


def test_non_multipart_request_passes_without_boundary_header():
    async def endpoint(request):
        return PlainTextResponse("ok")

    client = build_client(endpoint)
    response = client.post(
        "/upload",
        content=b'{"name":"agent"}',
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 200
    assert response.text == "ok"
    assert UPLOAD_BOUNDARY_HEADER not in response.headers
    assert get_current_upload_boundary() is None


@pytest.mark.parametrize(
    "content_type",
    [
        "multipart/form-data",
        "multipart/form-data; boundary=",
        "multipart/form-data; boundary=\"\"",
        f"multipart/form-data; boundary={'a' * 71}",
        "multipart/form-data; boundary=bad;name",
        "multipart/form-data; boundary=bad boundary",
        "multipart/form-data; boundary=bad\r\nname",
    ],
)
def test_invalid_multipart_boundary_rejects_before_body_read(content_type):
    called = False

    async def endpoint(request):
        nonlocal called
        called = True
        await request.body()
        return PlainTextResponse("unexpected")

    client = build_client(endpoint)
    response = client.post(
        "/upload?token=sensitive",
        content=b"secret upload payload",
        headers={"content-type": content_type},
    )

    assert response.status_code == 400
    assert response.text == "Invalid multipart boundary"
    assert response.headers[UPLOAD_BOUNDARY_HEADER] == "rejected"
    assert "secret" not in response.text
    assert "token=sensitive" not in response.text
    assert called is False
    assert get_current_upload_boundary() is None


def test_exception_path_clears_boundary_state():
    async def endpoint(request):
        assert get_current_upload_boundary() == "abc123"
        raise RuntimeError("boom")

    client = build_client(endpoint)

    with pytest.raises(RuntimeError):
        client.post(
            "/upload",
            content=b"--abc123\r\n\r\n--abc123--\r\n",
            headers={"content-type": "multipart/form-data; boundary=abc123"},
        )

    assert get_current_upload_boundary() is None
