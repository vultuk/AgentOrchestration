import json

from src.sdk.client import OrchestratorClient


class _FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return json.dumps({"ok": True}).encode()


def test_base_url_trailing_slash_is_normalized():
    client = OrchestratorClient(
        base_url="https://example.test/",
        api_key="token",
    )

    assert client.base_url == "https://example.test"
    assert (
        client._build_url("/agents")
        == "https://example.test/api/v2/agents"
    )


def test_request_uses_single_api_prefix_with_trailing_slash_base(monkeypatch):
    requests = []

    def fake_urlopen(req):
        requests.append(req)
        return _FakeResponse()

    monkeypatch.setattr("src.sdk.client.urlopen", fake_urlopen)
    client = OrchestratorClient(
        base_url="https://example.test///",
        api_key="token",
    )

    response = client._request("GET", "/agents")

    assert response == {"ok": True}
    assert (
        requests[0].full_url
        == "https://example.test/api/v2/agents"
    )


def test_request_normalizes_paths_without_leading_slash(monkeypatch):
    requests = []

    def fake_urlopen(req):
        requests.append(req)
        return _FakeResponse()

    monkeypatch.setattr("src.sdk.client.urlopen", fake_urlopen)
    client = OrchestratorClient(
        base_url="https://example.test",
        api_key="token",
    )

    response = client._request("GET", "agents")

    assert response == {"ok": True}
    assert (
        requests[0].full_url
        == "https://example.test/api/v2/agents"
    )
