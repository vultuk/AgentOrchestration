import pytest

from src.agent.sandbox import AgentSandbox


def test_sandbox_resolves_relative_base_path_symlink(tmp_path, monkeypatch):
    real_root = tmp_path / "real-root"
    real_root.mkdir()
    link_root = tmp_path / "link-root"
    link_root.symlink_to(real_root, target_is_directory=True)
    monkeypatch.chdir(tmp_path)

    sandbox = AgentSandbox("link-root")
    created_path = sandbox.create("agent-1")

    assert sandbox.base_path == real_root.resolve()
    assert created_path == real_root.resolve() / "agent-1"
    assert sandbox.get_path("agent-1") == created_path


def test_sandbox_rejects_parent_directory_escape(tmp_path):
    sandbox = AgentSandbox(str(tmp_path / "root"))

    with pytest.raises(ValueError, match="escapes base path"):
        sandbox.create("../escape")

    assert not (tmp_path / "escape").exists()


def test_sandbox_rejects_existing_symlink_escape(tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "agent-link").symlink_to(outside, target_is_directory=True)
    sandbox = AgentSandbox(str(root))

    with pytest.raises(ValueError, match="escapes base path"):
        sandbox.create("agent-link")

    assert sandbox.get_path("agent-link") is None
