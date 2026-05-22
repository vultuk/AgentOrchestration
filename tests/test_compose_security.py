import copy
from pathlib import Path

import pytest

from src.common.compose_security import (
    ComposeSecurityError,
    load_compose_file,
    validate_sidecar_filesystems,
)


COMPOSE_FILE = (
    Path(__file__).resolve().parents[1] / "infra" / "docker-compose.yml"
)
INFRA_README = Path(__file__).resolve().parents[1] / "infra" / "README.md"


def test_repo_compose_sidecars_are_read_only_with_documented_tmpfs():
    compose = load_compose_file(COMPOSE_FILE)
    assert validate_sidecar_filesystems(compose) == [
        "metrics-sidecar",
        "log-sidecar",
    ]


def test_sidecar_without_read_only_is_rejected():
    compose = load_compose_file(COMPOSE_FILE)
    compose["services"]["metrics-sidecar"].pop("read_only")

    with pytest.raises(
        ComposeSecurityError,
        match="metrics-sidecar: must set read_only",
    ):
        validate_sidecar_filesystems(compose)


def test_sidecar_without_healthcheck_is_rejected():
    compose = load_compose_file(COMPOSE_FILE)
    compose["services"]["metrics-sidecar"].pop("healthcheck")

    with pytest.raises(
        ComposeSecurityError,
        match="metrics-sidecar: must define a startup healthcheck",
    ):
        validate_sidecar_filesystems(compose)


def test_sidecar_undocumented_tmpfs_is_rejected():
    compose = load_compose_file(COMPOSE_FILE)
    compose["services"]["metrics-sidecar"]["tmpfs"].append("/cache:rw,size=4m")

    with pytest.raises(
        ComposeSecurityError,
        match="tmpfs paths are not documented",
    ):
        validate_sidecar_filesystems(compose)


def test_sidecar_writable_volume_is_rejected():
    compose = load_compose_file(COMPOSE_FILE)
    service = copy.deepcopy(compose["services"]["metrics-sidecar"])
    service["volumes"] = ["./metrics:/metrics"]
    compose["services"]["metrics-sidecar"] = service

    with pytest.raises(
        ComposeSecurityError,
        match="writable volume is not allowed",
    ):
        validate_sidecar_filesystems(compose)


def test_required_writable_paths_are_documented_for_operators():
    docs = INFRA_README.read_text(encoding="utf-8")
    assert "/tmp" in docs
    assert "/var/run/agent" in docs
    assert "read_only: true" in docs
