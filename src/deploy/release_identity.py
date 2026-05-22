"""Immutable release identity rendering for deployment manifests."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


ANNOTATION_PREFIX = "agent-orchestrator.io"
RELEASE_ID_KEY = f"{ANNOTATION_PREFIX}/release-id"
COMMIT_SHA_KEY = f"{ANNOTATION_PREFIX}/commit-sha"
PACKAGE_VERSION_KEY = f"{ANNOTATION_PREFIX}/package-version"
IMAGE_DIGEST_KEY = f"{ANNOTATION_PREFIX}/image-digest"
SOURCE_REF_KEY = f"{ANNOTATION_PREFIX}/source-ref"
SOURCE_REF_TYPE_KEY = f"{ANNOTATION_PREFIX}/source-ref-type"


def _require_text(value: str, field: str) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        raise ValueError(f"{field} is required")
    return cleaned


def _digest_fragment(image_digest: str) -> str:
    if ":" in image_digest:
        return image_digest.rsplit(":", 1)[-1][:12]
    return sha256(image_digest.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class ReleaseIdentity:
    commit_sha: str
    package_version: str
    image_digest: str
    source_ref: str = ""
    source_ref_type: str = "branch"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "commit_sha",
            _require_text(self.commit_sha, "commit_sha"),
        )
        object.__setattr__(
            self,
            "package_version",
            _require_text(self.package_version, "package_version"),
        )
        object.__setattr__(
            self,
            "image_digest",
            _require_text(self.image_digest, "image_digest"),
        )
        object.__setattr__(self, "source_ref", (self.source_ref or "").strip())
        object.__setattr__(
            self,
            "source_ref_type",
            (self.source_ref_type or "branch").strip(),
        )

    @property
    def release_id(self) -> str:
        digest_part = _digest_fragment(self.image_digest)
        return f"{self.package_version}-{self.commit_sha[:12]}-{digest_part}"

    def annotations(self) -> Dict[str, str]:
        annotations = {
            RELEASE_ID_KEY: self.release_id,
            COMMIT_SHA_KEY: self.commit_sha,
            PACKAGE_VERSION_KEY: self.package_version,
            IMAGE_DIGEST_KEY: self.image_digest,
        }
        if self.source_ref:
            annotations[SOURCE_REF_KEY] = self.source_ref
            annotations[SOURCE_REF_TYPE_KEY] = self.source_ref_type
        return annotations

    def release_record(self) -> Dict[str, str]:
        record = {
            "id": self.release_id,
            "commit_sha": self.commit_sha,
            "package_version": self.package_version,
            "image_digest": self.image_digest,
            "primary_identity": "release_id",
        }
        if self.source_ref:
            record["source_ref"] = self.source_ref
            record["source_ref_type"] = self.source_ref_type
        return record


def _metadata_annotations(manifest: Dict[str, Any]) -> Dict[str, str]:
    metadata = manifest.setdefault("metadata", {})
    return metadata.setdefault("annotations", {})


def _pod_template_annotations(
    manifest: Dict[str, Any],
) -> Optional[Dict[str, str]]:
    template = manifest.get("spec", {}).get("template")
    if not isinstance(template, dict):
        return None
    metadata = template.setdefault("metadata", {})
    return metadata.setdefault("annotations", {})


def render_manifest(
    manifest: Dict[str, Any],
    identity: ReleaseIdentity,
) -> Dict[str, Any]:
    rendered = deepcopy(manifest)
    annotations = identity.annotations()
    _metadata_annotations(rendered).update(annotations)

    pod_annotations = _pod_template_annotations(rendered)
    if pod_annotations is not None:
        pod_annotations.update(annotations)

    rendered["release"] = identity.release_record()
    rendered["release_history_key"] = identity.release_id
    return rendered


def render_manifest_file(
    manifest_path: str,
    identity: ReleaseIdentity,
) -> Dict[str, Any]:
    with Path(manifest_path).open(encoding="utf-8") as handle:
        manifest = yaml.safe_load(handle) or {}
    return render_manifest(manifest, identity)


class ReleaseHistory:
    def __init__(self) -> None:
        self._records: Dict[str, Dict[str, Any]] = {}

    def record(self, rendered_manifest: Dict[str, Any]) -> str:
        release = rendered_manifest.get("release") or {}
        release_id = _require_text(release.get("id", ""), "release.id")
        self._records[release_id] = deepcopy(rendered_manifest)
        return release_id

    def get(self, release_id: str) -> Optional[Dict[str, Any]]:
        record = self._records.get(release_id)
        return deepcopy(record) if record is not None else None

    def list_release_ids(self) -> List[str]:
        return list(self._records.keys())
