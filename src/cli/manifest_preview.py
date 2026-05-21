"""Deployment manifest dry-run rendering and review output."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

import yaml


class ManifestPreviewError(ValueError):
    """Raised when a release candidate manifest cannot be approved."""


SENSITIVE_KEY_PARTS = (
    "api_key",
    "auth",
    "credential",
    "password",
    "private",
    "secret",
    "token",
)
MISSING = object()


def load_manifest(path: str) -> Dict[str, Any]:
    manifest_path = Path(path)
    if not manifest_path.exists():
        raise ManifestPreviewError(f"manifest not found: {path}")
    if not manifest_path.is_file():
        raise ManifestPreviewError(f"manifest is not a file: {path}")

    raw = manifest_path.read_text(encoding="utf-8")
    if not raw.strip():
        raise ManifestPreviewError("manifest is empty")

    try:
        if manifest_path.suffix.lower() == ".json":
            manifest = json.loads(raw)
        elif manifest_path.suffix.lower() in {".yaml", ".yml"}:
            manifest = yaml.safe_load(raw)
        else:
            raise ManifestPreviewError(
                "manifest must be JSON, YAML, or YML"
            )
    except ManifestPreviewError:
        raise
    except Exception as exc:
        raise ManifestPreviewError(f"manifest parse failed: {exc}") from exc

    if not isinstance(manifest, dict):
        raise ManifestPreviewError("manifest root must be an object")
    return manifest


def apply_overrides(
    manifest: Dict[str, Any],
    assignments: List[str],
) -> Dict[str, Any]:
    rendered = copy.deepcopy(manifest)
    for assignment in assignments:
        if "=" not in assignment:
            raise ManifestPreviewError(
                f"override must use key=value: {assignment}"
            )
        key, value = assignment.split("=", 1)
        path = [part for part in key.split(".") if part]
        if not path:
            raise ManifestPreviewError(
                f"override key cannot be empty: {assignment}"
            )
        _set_value(rendered, path, value)
    return rendered


def validate_manifest(manifest: Dict[str, Any]) -> None:
    name = _manifest_name(manifest)
    if not name:
        raise ManifestPreviewError(
            "manifest requires name or metadata.name"
        )

    resources = manifest.get("resources", [])
    if resources is None:
        resources = []
    if not isinstance(resources, list):
        raise ManifestPreviewError("resources must be a list")

    for index, resource in enumerate(resources):
        if not isinstance(resource, dict):
            raise ManifestPreviewError(
                f"resources[{index}] must be an object"
            )
        if not _resource_name(resource):
            raise ManifestPreviewError(
                f"resources[{index}] requires name or metadata.name"
            )
        if not resource.get("kind"):
            raise ManifestPreviewError(f"resources[{index}] requires kind")
        _validate_environment(resource, f"resources[{index}]")

    _validate_environment(manifest, "manifest")


def render_preview(
    path: str,
    assignments: List[str],
) -> Tuple[Dict[str, Any], List[str]]:
    original = load_manifest(path)
    rendered = apply_overrides(original, assignments)
    validate_manifest(rendered)
    return rendered, build_review_diff(original, rendered)


def build_review_diff(
    before: Dict[str, Any],
    after: Dict[str, Any],
) -> List[str]:
    changes: List[str] = []
    for path in sorted(_changed_paths(before, after)):
        before_value = _value_at_path(before, path)
        after_value = _value_at_path(after, path)
        changes.append(
            f"{'.'.join(path)}: "
            f"{_review_value(path, before_value)} -> "
            f"{_review_value(path, after_value)}"
        )
    return changes


def _changed_paths(
    before: Any,
    after: Any,
    prefix: Tuple[str, ...] = (),
) -> List[Tuple[str, ...]]:
    if isinstance(before, dict) and isinstance(after, dict):
        changes: List[Tuple[str, ...]] = []
        for key in set(before) | set(after):
            changes.extend(
                _changed_paths(
                    before.get(key),
                    after.get(key),
                    prefix + (str(key),),
                )
            )
        return changes
    if isinstance(before, list) and isinstance(after, list):
        changes = []
        for index in range(max(len(before), len(after))):
            before_item = before[index] if index < len(before) else MISSING
            after_item = after[index] if index < len(after) else MISSING
            changes.extend(
                _changed_paths(
                    before_item,
                    after_item,
                    prefix + (str(index),),
                )
            )
        return changes
    if before != after:
        return [prefix]
    return []


def _value_at_path(data: Any, path: Tuple[str, ...]) -> Any:
    current = data
    for part in path:
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list):
            if not part.isdigit():
                return None
            index = int(part)
            if index >= len(current):
                return None
            current = current[index]
        else:
            return None
    return current


def _set_value(
    data: Dict[str, Any],
    path: List[str],
    value: str,
) -> None:
    current: Any = data
    for offset, part in enumerate(path[:-1]):
        next_part = path[offset + 1]
        if isinstance(current, list):
            if not part.isdigit():
                raise ManifestPreviewError(
                    f"override list index must be numeric: {part}"
                )
            index = int(part)
            if index >= len(current):
                raise ManifestPreviewError(
                    f"override list index out of range: {part}"
                )
            current = current[index]
            continue

        if not isinstance(current, dict):
            raise ManifestPreviewError(
                f"override parent is not an object: {'.'.join(path[:offset])}"
            )
        child = current.setdefault(part, [] if next_part.isdigit() else {})
        current = child

    final = path[-1]
    if isinstance(current, list):
        if not final.isdigit():
            raise ManifestPreviewError(
                f"override list index must be numeric: {final}"
            )
        index = int(final)
        if index >= len(current):
            raise ManifestPreviewError(
                f"override list index out of range: {final}"
            )
        current[index] = value
        return
    if not isinstance(current, dict):
        raise ManifestPreviewError(
            f"override parent is not an object: {'.'.join(path[:-1])}"
        )
    current[final] = value


def _review_value(path: Tuple[str, ...], value: Any) -> str:
    if _is_sensitive_path(path):
        return "<redacted>"
    return json.dumps(value, sort_keys=True)


def _is_sensitive_path(path: Tuple[str, ...]) -> bool:
    joined = ".".join(part.lower() for part in path)
    return any(part in joined for part in SENSITIVE_KEY_PARTS)


def _manifest_name(manifest: Dict[str, Any]) -> Any:
    metadata = manifest.get("metadata")
    if isinstance(metadata, dict):
        return metadata.get("name")
    return manifest.get("name")


def _resource_name(resource: Dict[str, Any]) -> Any:
    metadata = resource.get("metadata")
    if isinstance(metadata, dict):
        return metadata.get("name")
    return resource.get("name")


def _validate_environment(
    container: Dict[str, Any],
    label: str,
) -> None:
    for key in ("env", "environment"):
        values = container.get(key)
        if values is None:
            continue
        if isinstance(values, dict):
            for env_key, env_value in values.items():
                if not isinstance(env_key, str) or not env_key:
                    raise ManifestPreviewError(
                        f"{label}.{key} keys must be non-empty strings"
                    )
                if env_value is None:
                    raise ManifestPreviewError(
                        f"{label}.{key}.{env_key} cannot be null"
                    )
        elif isinstance(values, list):
            for index, item in enumerate(values):
                if not isinstance(item, dict):
                    raise ManifestPreviewError(
                        f"{label}.{key}[{index}] must be an object"
                    )
                if not item.get("name"):
                    raise ManifestPreviewError(
                        f"{label}.{key}[{index}] requires name"
                    )
                if item.get("value") is None:
                    raise ManifestPreviewError(
                        f"{label}.{key}[{index}] requires value"
                    )
        else:
            raise ManifestPreviewError(
                f"{label}.{key} must be an object or list"
            )
