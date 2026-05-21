import os
import stat

import pytest

from src.agent.sandbox import AgentSandbox, PRIVATE_DIRECTORY_MODE


pytestmark = pytest.mark.skipif(
    os.name != "posix",
    reason="POSIX permission bits are required for mode assertions",
)


def path_mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_sandbox_root_and_agent_directory_ignore_permissive_umask(tmp_path):
    original_umask = os.umask(0)
    try:
        sandbox = AgentSandbox(str(tmp_path / "sandbox-root"))
        sandbox_path = sandbox.create("agent-1")
    finally:
        os.umask(original_umask)

    assert path_mode(sandbox.base_path) == PRIVATE_DIRECTORY_MODE
    assert path_mode(sandbox_path) == PRIVATE_DIRECTORY_MODE


def test_nested_agent_directory_parents_are_restricted(tmp_path):
    original_umask = os.umask(0)
    try:
        sandbox = AgentSandbox(str(tmp_path / "sandbox-root"))
        sandbox_path = sandbox.create("team-a/agent-1")
    finally:
        os.umask(original_umask)

    assert path_mode(sandbox_path.parent) == PRIVATE_DIRECTORY_MODE
    assert path_mode(sandbox_path) == PRIVATE_DIRECTORY_MODE


def test_new_base_path_parents_are_restricted(tmp_path):
    original_umask = os.umask(0)
    try:
        sandbox = AgentSandbox(str(tmp_path / "sandboxes" / "sandbox-root"))
    finally:
        os.umask(original_umask)

    assert path_mode(sandbox.base_path.parent) == PRIVATE_DIRECTORY_MODE
    assert path_mode(sandbox.base_path) == PRIVATE_DIRECTORY_MODE


def test_existing_sandbox_root_is_tightened(tmp_path):
    base_path = tmp_path / "existing-root"
    base_path.mkdir(mode=0o777)
    base_path.chmod(0o777)

    sandbox = AgentSandbox(str(base_path))

    assert path_mode(sandbox.base_path) == PRIVATE_DIRECTORY_MODE
