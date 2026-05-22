"""Validation helpers for hardened Docker Compose sidecar services."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any, Dict, List, Set, Union

import yaml


SIDECAR_LABEL = "com.agent-orchestration.sidecar"
WRITABLE_PATHS_LABEL = "com.agent-orchestration.writable-paths"


class ComposeSecurityError(ValueError):
    """Raised when compose sidecar filesystem hardening is incomplete."""


def load_compose_file(path: Union[str, Path]) -> Dict[str, Any]:
    compose_path = Path(path)
    with compose_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ComposeSecurityError(
            f"{compose_path} must contain a compose mapping"
        )
    return data


def validate_compose_file(path: Union[str, Path]) -> List[str]:
    return validate_sidecar_filesystems(load_compose_file(path))


def validate_sidecar_filesystems(compose: Dict[str, Any]) -> List[str]:
    services = compose.get("services") or {}
    if not isinstance(services, dict):
        raise ComposeSecurityError("compose services must be a mapping")

    sidecars: List[str] = []
    errors: List[str] = []
    for service_name, service in services.items():
        if not isinstance(service, dict):
            continue
        if not _is_sidecar(service_name, service):
            continue

        sidecars.append(service_name)
        if service.get("read_only") is not True:
            errors.append(f"{service_name}: must set read_only: true")

        if not service.get("healthcheck"):
            errors.append(f"{service_name}: must define a startup healthcheck")

        documented_paths = _documented_writable_paths(service)
        tmpfs_paths = _tmpfs_paths(service.get("tmpfs", []))
        missing_tmpfs = documented_paths - tmpfs_paths
        if missing_tmpfs:
            missing = ", ".join(sorted(missing_tmpfs))
            errors.append(
                f"{service_name}: documented writable paths missing tmpfs: "
                f"{missing}"
            )

        undocumented_tmpfs = tmpfs_paths - documented_paths
        if undocumented_tmpfs:
            extra = ", ".join(sorted(undocumented_tmpfs))
            errors.append(
                f"{service_name}: tmpfs paths are not documented: {extra}"
            )

        if not documented_paths:
            errors.append(
                f"{service_name}: must document writable paths with "
                f"{WRITABLE_PATHS_LABEL}"
            )

        for volume in service.get("volumes", []) or []:
            if _is_writable_volume(volume):
                errors.append(
                    f"{service_name}: writable volume is not allowed: "
                    f"{volume}"
                )

    if not sidecars:
        errors.append(f"no sidecar services marked with {SIDECAR_LABEL}=true")

    if errors:
        raise ComposeSecurityError("; ".join(errors))

    return sidecars


def _is_sidecar(service_name: str, service: Dict[str, Any]) -> bool:
    labels = _labels_as_dict(service.get("labels", {}))
    label_value = str(labels.get(SIDECAR_LABEL, "")).lower()
    return label_value == "true" or service_name.endswith("-sidecar")


def _documented_writable_paths(service: Dict[str, Any]) -> Set[str]:
    labels = _labels_as_dict(service.get("labels", {}))
    raw_paths = labels.get(WRITABLE_PATHS_LABEL, "")
    if isinstance(raw_paths, str):
        return {path.strip() for path in raw_paths.split(",") if path.strip()}
    if isinstance(raw_paths, Iterable):
        return {str(path).strip() for path in raw_paths if str(path).strip()}
    return set()


def _tmpfs_paths(tmpfs: Any) -> Set[str]:
    if isinstance(tmpfs, dict):
        return set(tmpfs)
    if not isinstance(tmpfs, list):
        return set()
    return {_mount_target(entry) for entry in tmpfs if _mount_target(entry)}


def _mount_target(entry: Any) -> str:
    if isinstance(entry, str):
        return entry.split(":", 1)[0].strip()
    if isinstance(entry, dict):
        target = (
            entry.get("target")
            or entry.get("dst")
            or entry.get("destination")
        )
        return str(target).strip() if target else ""
    return ""


def _labels_as_dict(labels: Any) -> Dict[str, Any]:
    if isinstance(labels, dict):
        return labels
    if isinstance(labels, list):
        parsed: Dict[str, Any] = {}
        for label in labels:
            if not isinstance(label, str) or "=" not in label:
                continue
            key, value = label.split("=", 1)
            parsed[key] = value
        return parsed
    return {}


def _is_writable_volume(volume: Any) -> bool:
    if isinstance(volume, dict):
        if volume.get("read_only") is True:
            return False
        mode = str(volume.get("mode", ""))
        return "ro" not in mode.split(",")

    if not isinstance(volume, str):
        return False

    parts = volume.split(":")
    if len(parts) < 3:
        return True
    options = set(parts[2].split(","))
    return "ro" not in options
